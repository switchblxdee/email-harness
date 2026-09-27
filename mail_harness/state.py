from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    mailbox     TEXT PRIMARY KEY,
    uidvalidity INTEGER NOT NULL,
    last_uid    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (
    mailbox     TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    message_id  TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (mailbox, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS processed_message_id ON processed(message_id);
CREATE TABLE IF NOT EXISTS replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key  TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    thread_id   TEXT,
    message_id  TEXT,
    subject     TEXT,
    dry_run     INTEGER NOT NULL,
    sent_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS replies_recipient ON replies(recipient, sent_at);
CREATE INDEX IF NOT EXISTS replies_thread ON replies(thread_id);
CREATE INDEX IF NOT EXISTS replies_source ON replies(source_key);
"""


@dataclass(frozen=True)
class Cursor:
    uidvalidity: int
    last_uid: int


class StateStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()


    def get_cursor(self, mailbox: str) -> Cursor | None:
        row = self.db.execute(
            "SELECT uidvalidity, last_uid FROM cursor WHERE mailbox = ?", (mailbox,)
        ).fetchone()
        return Cursor(*row) if row else None

    def set_cursor(self, mailbox: str, uidvalidity: int, last_uid: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO cursor(mailbox, uidvalidity, last_uid) VALUES (?, ?, ?) "
                "ON CONFLICT(mailbox) DO UPDATE SET uidvalidity = excluded.uidvalidity, "
                "last_uid = excluded.last_uid",
                (mailbox, uidvalidity, last_uid),
            )


    def is_message_done(self, message_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM processed WHERE message_id = ? AND status = 'done' LIMIT 1", (message_id,)
        ).fetchone()
        return row is not None

    def mark(
        self, mailbox: str, uidvalidity: int, uid: int, message_id: str | None, status: str, detail: str = ""
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO processed(mailbox, uidvalidity, uid, message_id, status, detail, attempts, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(mailbox, uidvalidity, uid) DO UPDATE SET message_id = excluded.message_id, "
                "status = excluded.status, detail = excluded.detail, updated_at = excluded.updated_at",
                (mailbox, uidvalidity, uid, message_id, status, detail, time.time()),
            )

    def bump_attempts(
        self, mailbox: str, uidvalidity: int, uid: int, message_id: str | None, detail: str
    ) -> int:
        with self.db:
            self.db.execute(
                "INSERT INTO processed(mailbox, uidvalidity, uid, message_id, status, detail, attempts, updated_at) "
                "VALUES (?, ?, ?, ?, 'retry', ?, 1, ?) "
                "ON CONFLICT(mailbox, uidvalidity, uid) DO UPDATE SET status = 'retry', "
                "detail = excluded.detail, attempts = attempts + 1, updated_at = excluded.updated_at",
                (mailbox, uidvalidity, uid, message_id, detail, time.time()),
            )
        row = self.db.execute(
            "SELECT attempts FROM processed WHERE mailbox = ? AND uidvalidity = ? AND uid = ?",
            (mailbox, uidvalidity, uid),
        ).fetchone()
        return int(row[0])


    def record_reply(
        self,
        source_key: str,
        recipients: list[str],
        thread_id: str | None,
        message_id: str | None,
        subject: str,
        dry_run: bool,
    ) -> None:
        now = time.time()
        with self.db:
            self.db.executemany(
                "INSERT INTO replies(source_key, recipient, thread_id, message_id, subject, dry_run, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(source_key, r, thread_id, message_id, subject, int(dry_run), now) for r in recipients],
            )

    def reply_sent_for(self, source_key: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM replies WHERE source_key = ? AND dry_run = 0 LIMIT 1", (source_key,)
        ).fetchone()
        return row is not None

    def replied_in_thread(self, thread_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM replies WHERE thread_id = ? AND dry_run = 0 LIMIT 1", (thread_id,)
        ).fetchone()
        return row is not None

    def replies_to_since(self, recipient: str, since: float) -> int:
        row = self.db.execute(
            "SELECT COUNT(DISTINCT source_key) FROM replies WHERE recipient = ? AND sent_at >= ? AND dry_run = 0",
            (recipient, since),
        ).fetchone()
        return int(row[0])

    def replies_since(self, since: float) -> int:
        row = self.db.execute(
            "SELECT COUNT(DISTINCT source_key) FROM replies WHERE sent_at >= ? AND dry_run = 0", (since,)
        ).fetchone()
        return int(row[0])
