from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .compose import reply_subject
from .imap_client import ImapClient
from .models import Address
from .runner import safe_join, sanitize_filename

if TYPE_CHECKING:
    from .config import BacklogConfig
    from .runner import Harness

log = logging.getLogger("mail_harness.tasks")

STATUSES = ("new", "in_progress", "done")
PRIORITIES = ("high", "normal", "low")
TASK_FILE = "task.json"
EDITABLE = ("title", "summary", "description", "priority", "due_date", "due", "status", "notes")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


class BacklogStore:
    def __init__(self, backlog_dir: Path):
        self.root = backlog_dir.resolve()
        self._lock = threading.RLock()

    def _task_dir(self, task_id: str) -> Path:
        path = safe_join(self.root, task_id)
        if path.parent != self.root or not (path / TASK_FILE).is_file():
            raise FileNotFoundError(task_id)
        return path

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        tasks = []
        for task_json in self.root.glob(f"*/{TASK_FILE}"):
            try:
                data = json.loads(task_json.read_text(encoding="utf-8"))
                folder = task_json.parent
                data["files"] = [
                    {"name": p.name, "size": p.stat().st_size}
                    for p in sorted(folder.iterdir())
                    if p.is_file() and p.name != TASK_FILE and not p.name.startswith((".", "~$")) and p.suffix != ".tmp"
                ]
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            data["id"] = folder.name
            data["status"] = data.get("status") if data.get("status") in STATUSES else "new"
            tasks.append(data)
        tasks.sort(key=lambda t: t.get("received") or "", reverse=True)
        return tasks

    def read(self, task_id: str) -> dict[str, Any]:
        path = self._task_dir(task_id) / TASK_FILE
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, task_id: str, data: dict[str, Any]) -> None:
        path = self._task_dir(task_id) / TASK_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def update(self, task_id: str, changes: dict[str, Any]) -> tuple[dict[str, Any], str]:
        allowed = {k: v for k, v in changes.items() if k in ("status", "notes", "has_update")}
        if "has_update" in allowed:
            allowed["has_update"] = bool(allowed["has_update"])
        if "status" in allowed and allowed["status"] not in STATUSES:
            raise ValueError(f"неизвестный статус: {allowed['status']}")
        with self._lock:
            data = self.read(task_id)
            previous = data.get("status") if data.get("status") in STATUSES else "new"
            data.update(allowed)
            stamp = now_iso()
            data["updated_at"] = stamp
            if allowed.get("status", previous) != previous:
                data["status_changed_at"] = stamp
                if allowed["status"] == "done":
                    data["done_at"] = stamp
                else:
                    data.pop("done_at", None)
            self._write(task_id, data)
        return data, previous

    @staticmethod
    def validate(fields: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in EDITABLE:
                raise ValueError(f"поле {key} менять нельзя")
            if key == "title":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("название не может быть пустым")
                value = value[:200]
            elif key in ("summary", "due"):
                value = str(value).strip()[:1000] if value else None
            elif key in ("description", "notes"):
                value = str(value or "")[:20000]
            elif key == "priority":
                if value not in PRIORITIES:
                    raise ValueError(f"приоритет должен быть одним из {', '.join(PRIORITIES)}")
            elif key == "status":
                if value not in STATUSES:
                    raise ValueError(f"статус должен быть одним из {', '.join(STATUSES)}")
            elif key == "due_date":
                if value:
                    try:
                        value = date.fromisoformat(str(value)[:10]).isoformat()
                    except ValueError:
                        raise ValueError("срок должен быть датой в формате ГГГГ-ММ-ДД") from None
                else:
                    value = None
            clean[key] = value
        return clean

    def create(self, fields: dict[str, Any], author_name: str = "", author_email: str = "") -> str:
        clean = self.validate({"priority": "normal", "status": "new", **fields})
        if "title" not in clean:
            raise ValueError("у задачи должно быть название")
        stamp = now_iso()
        slug = re.sub(r"[^\w\-]+", "_", clean["title"], flags=re.UNICODE).strip("_")[:40].rstrip("_") or "task"
        suffix = hashlib.sha1(f"{stamp}{clean['title']}{time.time_ns()}".encode()).hexdigest()[:8]
        task_id = f"{datetime.now():%Y-%m-%d_%H%M}_{slug}_{suffix}"
        data = {"description": "", **{k: v for k, v in clean.items() if v is not None},
                "source": "chat", "received": stamp, "created_at": stamp, "updated_at": stamp}
        if author_name:
            data["from_name"] = author_name
        if author_email:
            data["from"] = author_email
        if data["status"] == "done":
            data["done_at"] = stamp
        with self._lock:
            folder = safe_join(self.root, task_id)
            folder.mkdir(parents=True, exist_ok=False)
            (folder / TASK_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return task_id

    def edit(self, task_id: str, fields: dict[str, Any]) -> tuple[dict[str, Any], str]:
        clean = self.validate(fields)
        with self._lock:
            data = self.read(task_id)
            previous = data.get("status") if data.get("status") in STATUSES else "new"
            if "description" in clean and "original_description" not in data and data.get("description"):
                data["original_description"] = data["description"]
            for key, value in clean.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            stamp = now_iso()
            data["updated_at"] = stamp
            if clean.get("status", previous) != previous:
                data["status_changed_at"] = stamp
                if clean["status"] == "done":
                    data["done_at"] = stamp
                else:
                    data.pop("done_at", None)
            self._write(task_id, data)
        return data, previous

    def delete(self, task_id: str, trash_root: Path) -> str:
        with self._lock:
            folder = self._task_dir(task_id)
            title = self.read(task_id).get("title") or task_id
            target_dir = trash_root / date.today().isoformat()
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / folder.name
            n = 1
            while target.exists():
                target = target_dir / f"{folder.name}_{n}"
                n += 1
            shutil.move(str(folder), str(target))
        log.info("задача «%s» перенесена в корзину %s", title, target)
        return title

    @staticmethod
    def _thread_ids(task: dict[str, Any]) -> set[str]:
        ids = {task.get("message_id"), task.get("notify_message_id"), *(task.get("references") or [])}
        ids |= {r.get("message_id") for r in task.get("replies") or []}
        ids |= {u.get("message_id") for u in task.get("updates") or []}
        return {i for i in ids if i}

    def find_thread_task(self, message_ids: set[str], sender: str | None, subject: str, is_reply: bool) -> str | None:
        if not self.root.is_dir():
            return None
        by_ids, by_subject = [], []
        norm = _norm_subject(subject)
        for task_json in self.root.glob(f"*/{TASK_FILE}"):
            try:
                task = json.loads(task_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if task.get("source") == "chat":
                continue
            key = (task.get("received") or "", task_json.parent.name)
            if message_ids & self._thread_ids(task):
                by_ids.append(key)
            elif is_reply and norm and sender and sender.lower() == str(task.get("from") or "").lower() \
                    and norm == _norm_subject(task.get("subject") or ""):
                by_subject.append(key)
        match = max(by_ids or by_subject, default=None)
        return match[1] if match else None

    def add_update(self, task_id: str, update: dict[str, Any], files: list[tuple[str, bytes]] = ()) -> dict[str, Any]:
        with self._lock:
            folder = self._task_dir(task_id)
            saved = []
            for name, data in files:
                target = safe_join(folder, sanitize_filename(name))
                n = 1
                while target.exists() and target.read_bytes() != data:
                    target = target.with_name(f"{target.stem} ({n}){target.suffix}")
                    n += 1
                target.write_bytes(data)
                saved.append(target.name)
            task = self.read(task_id)
            if update.get("message_id") and any(u.get("message_id") == update["message_id"] for u in task.get("updates") or []):
                return task
            if not update.get("from_name") and str(update.get("from") or "").lower() == str(task.get("from") or "").lower():
                update = {**update, "from_name": task.get("from_name")}
            task["updates"] = [*(task.get("updates") or []), {**update, "files": saved}]
            task["has_update"] = True
            task["updated_at"] = now_iso()
            self._write(task_id, task)
            return task

    def set_fields(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            data = self.read(task_id)
            for key, value in fields.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            self._write(task_id, data)

    def revision(self) -> str:
        if not self.root.is_dir():
            return "empty"
        digest = hashlib.sha1()
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir():
                continue
            for p in sorted(folder.iterdir()):
                try:
                    st = p.stat()
                except OSError:
                    continue
                digest.update(f"{folder.name}/{p.name}:{st.st_mtime_ns}:{st.st_size};".encode("utf-8", errors="replace"))
        return digest.hexdigest()

    def file_path(self, task_id: str, name: str | None = None) -> Path:
        folder = self._task_dir(task_id)
        if not name:
            return folder
        path = safe_join(folder, name)
        if path.parent != folder or not path.is_file():
            raise FileNotFoundError(name)
        return path


_RE_PREFIX = re.compile(r"^\s*((re|fw|fwd|ответ|пересл|aw|wg)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def _norm_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", _RE_PREFIX.sub("", subject or "")).strip().lower()


def cleanup_done(store: BacklogStore, mode: str, archive_root: Path, today: date | None = None) -> list[str]:
    if mode == "off" or not store.root.is_dir():
        return []
    today = today or date.today()
    removed = []
    with store._lock:
        for task_json in sorted(store.root.glob(f"*/{TASK_FILE}")):
            try:
                data = json.loads(task_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if data.get("status") != "done":
                continue
            done_at = parse_time(data.get("done_at") or data.get("updated_at") or data.get("received"))
            if done_at is None or done_at.astimezone().date() >= today:
                continue
            folder = task_json.parent
            title = data.get("title") or data.get("subject") or folder.name
            try:
                if mode == "delete":
                    shutil.rmtree(folder)
                else:
                    target_dir = archive_root / done_at.astimezone().date().isoformat()
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / folder.name
                    n = 1
                    while target.exists():
                        target = target_dir / f"{folder.name}_{n}"
                        n += 1
                    shutil.move(str(folder), str(target))
            except OSError as e:
                log.warning("не удалось убрать выполненную задачу %s: %s", folder.name, e)
                continue
            removed.append(title)
    if removed:
        action = "удалено" if mode == "delete" else f"перенесено в архив {archive_root}"
        log.info("очистка выполненных: %s — %s", action, "; ".join(removed))
    return removed


class CleanupScheduler(threading.Thread):
    def __init__(self, store: BacklogStore, mode: str, archive_root: Path, check_every: float = 30):
        super().__init__(name="backlog-cleanup", daemon=True)
        self.store = store
        self.mode = mode
        self.archive_root = archive_root
        self.check_every = check_every
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if self.mode == "off":
            return
        last_day = None
        while True:
            today = date.today()
            if today != last_day:
                try:
                    cleanup_done(self.store, self.mode, self.archive_root, today)
                except Exception:
                    log.exception("ошибка очистки выполненных задач")
                last_day = today
            if self._stop.wait(self.check_every):
                return


def task_recipient(task: dict[str, Any], guard: Any) -> Address:
    pairs: list[tuple[str, str]] = []
    for raw in (task.get("reply_to"), task.get("from")):
        if raw:
            pairs = [(n, a.lower()) for n, a in getaddresses([str(raw)]) if "@" in a]
            if pairs:
                break
    if not pairs:
        raise ValueError("у задачи нет автора письма — отвечать некому")
    name, email = pairs[0]
    addr = Address(name or task.get("from_name") or "", email)
    if guard is not None and not guard.is_allowed_recipient(addr):
        raise ValueError(f"адресат {addr.email} запрещён настройками safety")
    return addr


def send_task_reply(harness: Harness | None, store: BacklogStore, task_id: str, body: str) -> dict[str, Any]:
    if harness is None:
        raise ValueError("почта не подключена — отправить ответ не могу")
    body = (body or "").strip()
    if not body:
        raise ValueError("пустой текст ответа")
    task = store.read(task_id)
    to = task_recipient(task, harness.guard)
    msg = EmailMessage()
    msg["From"] = str(harness.from_addr)
    msg["To"] = str(to)
    msg["Subject"] = reply_subject(task.get("subject") or task.get("title") or "")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=harness.from_addr.domain or None)
    if task.get("message_id"):
        msg["In-Reply-To"] = task["message_id"]
        msg["References"] = " ".join([*(task.get("references") or []), task["message_id"]])
    msg.set_content(body)
    if harness.dry_run:
        n = len(task.get("replies") or []) + 1
        path = safe_join(harness.data_dir, f"dry_run/reply_{sanitize_filename(task_id)}_{n}.eml")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(msg.as_bytes())
        log.info("DRY RUN: ответ по задаче «%s» для %s сохранён в %s", task.get("title"), to.email, path)
    else:
        harness.smtp.send(msg, [to.email])
        log.info("ответ по задаче «%s» отправлен: %s", task.get("title"), to.email)
        if harness.cfg.imap.sent_folder:
            imap = ImapClient(harness.cfg.imap)
            try:
                imap.connect()
                imap.append(harness.cfg.imap.sent_folder, msg.as_bytes())
            except Exception as e:
                log.warning("не удалось положить копию ответа в %s: %s", harness.cfg.imap.sent_folder, e)
            finally:
                imap.close()
    record = {"at": now_iso(), "to": str(to), "subject": msg["Subject"], "body": body, "dry_run": harness.dry_run, "message_id": msg["Message-ID"]}
    store.set_fields(task_id, replies=[*(task.get("replies") or []), record])
    return {"task_id": task_id, "sent_to": to.email, "dry_run": harness.dry_run}


class Notifier:
    def __init__(self, store: BacklogStore, harness: Harness | None, cfg: BacklogConfig):
        self.store = store
        self.harness = harness
        self.cfg = cfg
        self.enabled = harness is not None and cfg.notify_in_progress
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    @property
    def owner(self) -> str:
        if self.cfg.owner:
            return self.cfg.owner
        smtp = self.harness.cfg.smtp if self.harness else None
        return (smtp.from_name or smtp.from_address) if smtp else ""

    def schedule(self, task_id: str) -> None:
        if not self.enabled:
            return
        self.cancel(task_id)
        timer = threading.Timer(self.cfg.notify_delay, self._fire, args=(task_id,))
        timer.daemon = True
        with self._lock:
            self._timers[task_id] = timer
        timer.start()

    def cancel(self, task_id: str) -> None:
        with self._lock:
            timer = self._timers.pop(task_id, None)
        if timer:
            timer.cancel()

    def cancel_all(self) -> None:
        with self._lock:
            timers, self._timers = list(self._timers.values()), {}
        for timer in timers:
            timer.cancel()

    def _fire(self, task_id: str) -> None:
        with self._lock:
            self._timers.pop(task_id, None)
        try:
            task = self.store.read(task_id)
        except (FileNotFoundError, ValueError, OSError):
            return
        if (task.get("status") != "in_progress" or task.get("notified_in_progress_at") or task.get("source") == "chat"
                or task.get("kind") in ("meeting", "question") or not (task.get("from") or task.get("reply_to"))):
            return
        try:
            recipient, message_id = self._send(task_id, task)
        except Exception as e:
            log.warning("уведомление по задаче «%s» не отправлено: %s", task.get("title"), e)
            self.store.set_fields(task_id, notify_error=f"{type(e).__name__}: {e}")
            return
        self.store.set_fields(task_id, notified_in_progress_at=now_iso(), notified_to=recipient, notify_message_id=message_id,
                              notify_dry_run=True if self.harness.dry_run else None, notify_error=None)

    def _recipient(self, task: dict[str, Any]) -> Address:
        return task_recipient(task, self.harness.guard)

    def build(self, task: dict[str, Any], to: Address) -> EmailMessage:
        h = self.harness
        msg = EmailMessage()
        msg["From"] = str(h.from_addr)
        msg["To"] = str(to)
        msg["Subject"] = reply_subject(task.get("subject") or task.get("title") or "")
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=h.from_addr.domain or None)
        message_id = task.get("message_id")
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"] = " ".join([*(task.get("references") or []), message_id])
        msg["Auto-Submitted"] = "auto-generated"
        msg["X-Auto-Response-Suppress"] = "All"
        msg.set_content(self.cfg.notify_template.format(
            owner=self.owner,
            title=task.get("title") or task.get("subject") or "без названия",
            subject=task.get("subject") or "",
        ))
        return msg

    def _send(self, task_id: str, task: dict[str, Any]) -> tuple[str, str]:
        h = self.harness
        to = self._recipient(task)
        msg = self.build(task, to)
        if h.dry_run:
            path = safe_join(h.data_dir, f"dry_run/notify_{sanitize_filename(task_id)}.eml")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(msg.as_bytes())
            log.info("DRY RUN: уведомление «в работе» для %s сохранено в %s", to.email, path)
            return to.email, msg["Message-ID"]
        h.smtp.send(msg, [to.email])
        log.info("уведомление «в работе» отправлено: %s («%s»)", to.email, task.get("title"))
        if h.cfg.imap.sent_folder:
            imap = ImapClient(h.cfg.imap)
            try:
                imap.connect()
                imap.append(h.cfg.imap.sent_folder, msg.as_bytes())
            except Exception as e:
                log.warning("не удалось положить копию уведомления в %s: %s", h.cfg.imap.sent_folder, e)
            finally:
                imap.close()
        return to.email, msg["Message-ID"]
