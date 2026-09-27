from __future__ import annotations

import base64
import imaplib
import logging
import re
import ssl
import time

from .config import ImapConfig

log = logging.getLogger(__name__)

_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')


class ImapError(Exception):
    pass


def imap_utf7_encode(name: str) -> str:
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1 : end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            out.append(base64.b64decode(b64).decode("utf-16-be"))
        i = end + 1
    return "".join(out)


def _quote(name: str) -> str:
    encoded = imap_utf7_encode(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def xoauth2_string(username: str, token: str) -> str:
    return f"user={username}\x01auth=Bearer {token}\x01\x01"


class ImapClient:
    def __init__(self, cfg: ImapConfig):
        self.cfg = cfg
        self.conn: imaplib.IMAP4 | None = None
        self.selected: str | None = None


    def connect(self) -> None:
        self.close()
        cfg = self.cfg
        ctx = ssl.create_default_context()
        if cfg.security == "ssl":
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=ctx, timeout=cfg.timeout)
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=cfg.timeout)
            if cfg.security == "starttls":
                conn.starttls(ssl_context=ctx)
        try:
            if cfg.auth == "xoauth2":
                conn.authenticate("XOAUTH2", lambda _: xoauth2_string(cfg.username, cfg.secret).encode())
            else:
                conn.login(cfg.username, cfg.secret)
        except imaplib.IMAP4.error as e:
            conn.shutdown()
            raise ImapError(f"IMAP: не удалось войти как {cfg.username}: {e}") from None
        self.conn = conn
        self.selected = None
        log.info("IMAP: подключено к %s:%s как %s", cfg.host, cfg.port, cfg.username)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None
            self.selected = None

    @property
    def c(self) -> imaplib.IMAP4:
        if self.conn is None:
            raise ImapError("IMAP: нет соединения")
        return self.conn

    def noop(self) -> None:
        self._ok(self.c.noop(), "NOOP")

    @property
    def capabilities(self) -> set[str]:
        return {str(c).upper() for c in self.c.capabilities}


    def select(self, mailbox: str) -> int:
        typ, data = self.c.select(_quote(mailbox))
        if typ != "OK":
            raise ImapError(f"IMAP: не удалось открыть папку {mailbox!r}: {data}")
        self.selected = mailbox
        _, values = self.c.response("UIDVALIDITY")
        if not values or values[0] is None:
            raise ImapError("IMAP: сервер не сообщил UIDVALIDITY")
        return int(values[0])

    def list_folders(self) -> list[str]:
        data = self._ok(self.c.list(), "LIST")
        folders = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            m = _LIST_RE.match(line)
            if not m:
                continue
            name = m.group("name").decode("ascii", errors="replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            folders.append(imap_utf7_decode(name))
        return folders


    def max_uid(self) -> int:
        uids = self._search("ALL")
        return max(uids) if uids else 0

    def uids_after(self, last_uid: int) -> list[int]:
        return sorted(u for u in self._search(f"UID {last_uid + 1}:*") if u > last_uid)

    def _search(self, criteria: str) -> list[int]:
        data = self._ok(self.c.uid("SEARCH", None, criteria), f"SEARCH {criteria}")
        return [int(x) for x in b" ".join(d for d in data if d).split()]

    def message_sizes(self, uids: list[int], chunk: int = 500) -> dict[int, int]:
        sizes: dict[int, int] = {}
        for i in range(0, len(uids), chunk):
            uid_set = ",".join(str(u) for u in uids[i : i + chunk])
            data = self._ok(self.c.uid("FETCH", uid_set, "(UID RFC822.SIZE)"), "FETCH SIZE")
            for item in data:
                raw = item[0] if isinstance(item, tuple) else item
                if not isinstance(raw, bytes):
                    continue
                uid = re.search(rb"UID (\d+)", raw)
                size = re.search(rb"RFC822\.SIZE (\d+)", raw)
                if uid and size:
                    sizes[int(uid.group(1))] = int(size.group(1))
        return sizes

    def fetch(self, uid: int) -> bytes | None:
        data = self._ok(self.c.uid("FETCH", str(uid), "(BODY.PEEK[])"), "FETCH")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
        return None


    def add_flags(self, uid: int, flags: str) -> None:
        self._ok(self.c.uid("STORE", str(uid), "+FLAGS", f"({flags})"), "STORE")

    def move(self, uid: int, folder: str) -> None:
        caps = self.capabilities
        if "MOVE" in caps:
            self._ok(self.c.uid("MOVE", str(uid), _quote(folder)), "MOVE")
            return
        self._ok(self.c.uid("COPY", str(uid), _quote(folder)), "COPY")
        self.add_flags(uid, "\\Deleted")
        if "UIDPLUS" in caps:
            self._ok(self.c.uid("EXPUNGE", str(uid)), "UID EXPUNGE")
        else:
            log.warning("IMAP: сервер без MOVE/UIDPLUS, письмо %s скопировано и помечено \\Deleted", uid)

    def append(self, folder: str, message: bytes, flags: str = "\\Seen") -> None:
        self._ok(
            self.c.append(_quote(folder), f"({flags})", imaplib.Time2Internaldate(time.time()), message),
            "APPEND",
        )

    @staticmethod
    def _ok(result: tuple[str, list], what: str) -> list:
        typ, data = result
        if typ != "OK":
            raise ImapError(f"IMAP {what}: {typ} {data}")
        return data
