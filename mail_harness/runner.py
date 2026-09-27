from __future__ import annotations

import imaplib
import logging
import re
import signal
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from .agent import Agent, AgentContext
from .compose import build_reply, reply_recipients
from .config import Config
from .imap_client import ImapClient, ImapError
from .models import (
    ACTION_TYPES,
    Action,
    Address,
    Attachment,
    Flag,
    IncomingEmail,
    Move,
    Reply,
    SaveAttachments,
    Skip,
    WriteFile,
)
from .parsing import parse_message
from .safety import ReplyGuard, automated_reason
from .smtp_client import SmtpClient, SmtpError
from .state import StateStore

log = logging.getLogger("mail_harness")

CONNECTION_ERRORS = (OSError, socket.timeout, ssl.SSLError, imaplib.IMAP4.abort, ImapError)
TRANSIENT_NAMES = ("Timeout", "Connection", "RateLimit", "InternalServer", "ServiceUnavailable", "Overloaded")


class TransientError(ConnectionError):
    pass


def is_transient(e: BaseException) -> bool:
    if isinstance(e, (SmtpError, *CONNECTION_ERRORS)):
        return True
    return any(name in cls.__name__ for cls in type(e).__mro__ for name in TRANSIENT_NAMES)


class MailboxLike(Protocol):
    def connect(self) -> None: ...
    def close(self) -> None: ...
    def select(self, mailbox: str) -> int: ...
    def max_uid(self) -> int: ...
    def uids_after(self, last_uid: int) -> list[int]: ...
    def message_sizes(self, uids: list[int]) -> dict[int, int]: ...
    def fetch(self, uid: int) -> bytes | None: ...
    def add_flags(self, uid: int, flags: str) -> None: ...
    def move(self, uid: int, folder: str) -> None: ...
    def append(self, folder: str, message: bytes, flags: str = "\\Seen") -> None: ...


class SenderLike(Protocol):
    def send(self, msg, recipients: list[str]) -> None: ...


class Harness:
    def __init__(
        self,
        config: Config,
        agent: Agent,
        imap: MailboxLike | None = None,
        smtp: SenderLike | None = None,
        state: StateStore | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = config
        self.agent = agent
        self.imap = imap or ImapClient(config.imap)
        self.smtp = smtp or SmtpClient(config.smtp)
        self.state = state or StateStore(config.harness.state_db)
        self.guard = ReplyGuard(config.safety, self.state, clock)
        self.mailbox = config.imap.mailbox
        self.data_dir = config.harness.data_dir.resolve()
        self.dry_run = config.harness.dry_run
        self.from_addr = Address(config.smtp.from_name, config.smtp.from_address)
        self.ctx = AgentContext(
            data_dir=self.data_dir,
            dry_run=self.dry_run,
            my_addresses=config.safety.my_addresses,
            log=logging.getLogger("mail_harness.agent"),
        )
        self._connected = False
        self._stop = threading.Event()
        self.last_poll_at: float | None = None
        self.last_error: str | None = None
        self._message_error: str | None = None


    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(self.agent, "setup"):
            self.agent.setup(self.ctx)
        if self.dry_run:
            log.warning("DRY RUN: ответы не отправляются (пишутся в %s), ящик не меняется",
                        self.data_dir / "dry_run")

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        if hasattr(self.agent, "close"):
            try:
                self.agent.close()
            except Exception:
                log.exception("ошибка в agent.close()")
        self.imap.close()
        if hasattr(self.smtp, "close"):
            self.smtp.close()
        self.state.close()

    def run_forever(self) -> None:
        self.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.stop())
            except ValueError:
                pass
        backoff = 5.0
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                    self.last_poll_at = time.time()
                    self.last_error = self._message_error
                    backoff = 5.0
                    self._stop.wait(self.cfg.harness.poll_interval)
                except CONNECTION_ERRORS as e:
                    self.last_error = f"{e} — повтор через {backoff:.0f} с"
                    if not isinstance(e, TransientError):
                        log.warning("соединение потеряно (%s), повтор через %.0f с", e, backoff)
                    self._disconnect()
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 600)
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.exception("ошибка в цикле харнесса, повтор через %.0f с", backoff)
                    self._disconnect()
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 600)
        finally:
            log.info("остановка")
            self.shutdown()

    def _ensure_connected(self) -> None:
        if not self._connected:
            self.imap.connect()
            self._connected = True

    def _disconnect(self) -> None:
        self.imap.close()
        self._connected = False


    def poll_once(self) -> int:
        self._message_error = None
        self._ensure_connected()
        uidvalidity = self.imap.select(self.mailbox)
        cursor = self.state.get_cursor(self.mailbox)

        if cursor is None or cursor.uidvalidity != uidvalidity:
            if cursor is not None:
                log.warning("UIDVALIDITY папки %s сменился — начинаю заново с текущего момента", self.mailbox)
            if cursor is None and self.cfg.harness.start_from == "all":
                last_uid = 0
            else:
                last_uid = self.imap.max_uid()
                log.info("старые письма пропускаю, слежу за новыми (UID > %d)", last_uid)
            self.state.set_cursor(self.mailbox, uidvalidity, last_uid)
        else:
            last_uid = cursor.last_uid

        count = 0
        uids = self.imap.uids_after(last_uid)
        sizes = self.imap.message_sizes(uids) if uids else {}
        for uid in uids:
            if self._stop.is_set():
                break
            if not self._process(uid, uidvalidity, sizes.get(uid)):
                break
            self.state.set_cursor(self.mailbox, uidvalidity, uid)
            count += 1
        return count

    def _process(self, uid: int, uidvalidity: int, size: int | None = None) -> bool:
        key = f"{self.mailbox}/{uidvalidity}/{uid}"

        if size is not None and size > self.cfg.harness.max_message_bytes:
            log.warning("[%s] пропуск: письмо %.1f МБ больше лимита", uid, size / 2**20)
            self.state.mark(self.mailbox, uidvalidity, uid, None, "skipped", "слишком большое")
            return True
        raw = self.imap.fetch(uid)
        if raw is None:
            self.state.mark(self.mailbox, uidvalidity, uid, None, "skipped", "письмо исчезло")
            return True

        email = parse_message(raw, uid)
        who = email.sender.email if email.sender else "?"
        log.info("[%s] от %s: %s", uid, who, email.subject or "(без темы)")

        if email.message_id and self.state.is_message_done(email.message_id):
            self.state.mark(self.mailbox, uidvalidity, uid, email.message_id, "skipped", "дубликат Message-ID")
            log.info("[%s] пропуск: уже обработано (дубликат)", uid)
            return True

        if self.cfg.safety.skip_automated:
            reason = automated_reason(email, self.cfg.safety.my_addresses)
            if reason:
                log.info("[%s] пропуск: %s", uid, reason)
                self.state.mark(self.mailbox, uidvalidity, uid, email.message_id, "skipped", reason)
                return True

        if self.cfg.backlog.link_replies:
            linked = self._link_to_task(email)
            if linked:
                self.state.mark(self.mailbox, uidvalidity, uid, email.message_id, "done", f"UPD → задача {linked}")
                return True

        try:
            actions = self._ask_agent(email)
            summary = self._execute(email, actions, key)
        except Exception as e:
            transient = is_transient(e)
            if not transient:
                log.exception("[%s] ошибка агента/действий", uid)
            return self._fail(uid, uidvalidity, email, e, transient=transient)

        self.state.mark(self.mailbox, uidvalidity, uid, email.message_id, "done", summary)
        return True

    def _link_to_task(self, email: IncomingEmail) -> str | None:
        from .parsing import strip_quoted
        from .tasks import BacklogStore, now_iso

        store = BacklogStore(self.data_dir / "backlog")
        ids = {i for i in (email.in_reply_to, *email.references) if i}
        is_reply = bool(ids) or bool(re.match(r"^\s*(re|ответ|aw)\s*:", email.subject or "", re.IGNORECASE))
        if not is_reply:
            return None
        task_id = store.find_thread_task(ids, email.sender.email if email.sender else None, email.subject, is_reply)
        if not task_id:
            return None
        files = [(a.filename, a.data) for a in email.attachments if not a.inline]
        store.add_update(task_id, {
            "at": now_iso(),
            "received": (email.date.isoformat() if email.date else now_iso()),
            "from": email.sender.email if email.sender else None,
            "from_name": email.sender.name if email.sender else None,
            "subject": email.subject,
            "text": strip_quoted(email.text)[:8000],
            "message_id": email.message_id,
        }, files)
        log.info("[%s] ответ в ветке задачи %s — добавлен UPD, новая задача не создаётся", email.uid, task_id)
        return task_id

    def _fail(self, uid: int, uidvalidity: int, email: IncomingEmail, e: Exception, transient: bool) -> bool:
        attempts = self.state.bump_attempts(self.mailbox, uidvalidity, uid, email.message_id, repr(e))
        subject = email.subject or "(без темы)"
        if transient:
            log.warning("[%s] временная ошибка (попытка %d): %s: %s — повторю позже", uid, attempts, type(e).__name__, e)
            self._message_error = f"«{subject}»: {type(e).__name__} — повторяю"
            raise TransientError(f"«{subject}»: {type(e).__name__}: {e}") from e
        if attempts >= self.cfg.harness.max_attempts:
            log.error("[%s] сдаюсь после %d попыток: %s", uid, attempts, e)
            self.state.mark(self.mailbox, uidvalidity, uid, email.message_id, "error", repr(e))
            self._message_error = f"«{subject}» не обработано: {type(e).__name__}: {e}"
            return True
        log.warning("[%s] попытка %d/%d не удалась: %s", uid, attempts, self.cfg.harness.max_attempts, e)
        self._message_error = f"«{subject}»: {type(e).__name__}: {e} — попытка {attempts}/{self.cfg.harness.max_attempts}"
        return False

    def _ask_agent(self, email: IncomingEmail) -> list[Action]:
        result = self.agent.handle(email, self.ctx)
        actions = list(result or [])
        for a in actions:
            if not isinstance(a, ACTION_TYPES):
                raise TypeError(f"агент вернул не действие: {a!r}")
        if sum(isinstance(a, Move) for a in actions) > 1:
            raise ValueError("агент вернул больше одного Move")
        return actions


    def _execute(self, email: IncomingEmail, actions: list[Action], key: str) -> str:
        if not actions:
            log.info("[%s] агент ничего не сделал", email.uid)
            return "нет действий"
        done: list[str] = []
        move: Move | None = None
        for action in actions:
            if isinstance(action, Move):
                move = action
            elif isinstance(action, Skip):
                log.info("[%s] агент пропустил: %s", email.uid, action.reason)
                done.append(f"skip: {action.reason}")
            elif isinstance(action, Reply):
                done.append(self._reply(email, action, key))
            elif isinstance(action, SaveAttachments):
                done.append(self._save_attachments(email, action))
            elif isinstance(action, WriteFile):
                path = self._write(action.path, action.content)
                done.append(f"file: {path.relative_to(self.data_dir)}")
            elif isinstance(action, Flag):
                if self.dry_run:
                    log.info("[%s] DRY RUN: флаг %s", email.uid, action.flag)
                else:
                    self.imap.add_flags(email.uid, action.flag)
                done.append(f"flag: {action.flag}")
        if move:
            if self.dry_run:
                log.info("[%s] DRY RUN: перемещение в %s", email.uid, move.folder)
            else:
                self.imap.move(email.uid, move.folder)
            done.append(f"move: {move.folder}")
        return "; ".join(done)

    def _reply(self, email: IncomingEmail, action: Reply, key: str) -> str:
        if self.state.reply_sent_for(key):
            log.info("[%s] ответ уже отправлялся (повторная попытка) — не дублирую", email.uid)
            return "reply: уже отправлен"
        to, cc = reply_recipients(email, action.reply_all, self.cfg.safety.my_addresses)
        verdict = self.guard.check_reply(email, to)
        if not verdict.allowed:
            log.warning("[%s] ответ заблокирован: %s", email.uid, verdict.reason)
            return f"reply blocked: {verdict.reason}"
        cc = tuple(a for a in cc if self.guard.is_allowed_recipient(a))

        msg = build_reply(email, action, self.from_addr, to, cc)
        recipients = [a.email for a in to + cc]
        if self.dry_run:
            path = self._write(f"dry_run/{email.uid}_reply.eml", msg.as_bytes())
            log.info("[%s] DRY RUN: ответ для %s сохранён в %s", email.uid, ", ".join(recipients), path)
        else:
            self.smtp.send(msg, recipients)
            log.info("[%s] ответ отправлен: %s", email.uid, ", ".join(recipients))
        self.state.record_reply(key, recipients, email.thread_id, msg["Message-ID"], msg["Subject"], self.dry_run)

        if not self.dry_run and self.cfg.imap.sent_folder:
            try:
                self.imap.append(self.cfg.imap.sent_folder, msg.as_bytes())
            except Exception as e:
                log.warning("[%s] не удалось положить копию в %s: %s", email.uid, self.cfg.imap.sent_folder, e)
        return f"reply: {', '.join(recipients)}"

    def _save_attachments(self, email: IncomingEmail, action: SaveAttachments) -> str:
        exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in action.extensions}
        chosen: list[Attachment] = [
            a for a in email.attachments
            if (action.include_inline or not a.inline) and (not exts or a.extension in exts)
        ]
        saved = []
        for att in chosen:
            path = self._write(f"{action.directory}/{sanitize_filename(att.filename)}", att.data, unique=True)
            saved.append(path.name)
        log.info("[%s] сохранено вложений: %d в %s", email.uid, len(saved), action.directory)
        return f"saved: {', '.join(saved) or '-'}"

    def _write(self, rel_path: str, content: str | bytes, unique: bool = False) -> Path:
        path = safe_join(self.data_dir, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        if unique:
            path = _unique_path(path, data)
        path.write_bytes(data)
        return path


_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def sanitize_filename(name: str, max_len: int = 150) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        return "attachment.bin"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def safe_join(base: Path, rel_path: str) -> Path:
    base = base.resolve()
    path = (base / rel_path).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"путь {rel_path!r} выходит за пределы {base}")
    return path


def _unique_path(path: Path, data: bytes) -> Path:
    candidate = path
    n = 1
    while candidate.exists():
        if candidate.read_bytes() == data:
            return candidate
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate
