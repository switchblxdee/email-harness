from __future__ import annotations

import json
import logging
import mimetypes
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import Config
from .runner import Harness
from .assistant import BacklogTools, ChatService
from .memory import MemoryStore
from .stats import EventLog, compute as compute_stats
from .tasks import BacklogStore, CleanupScheduler, Notifier, parse_time, send_task_reply

log = logging.getLogger("mail_harness.web")

PAGE = Path(__file__).resolve().parent / "web.html"


def open_in_os(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


class WebApp:
    def __init__(self, store: BacklogStore, harness: Harness | None, cfg: Config, mail_error: str | None = None):
        self.store = store
        self.harness = harness
        self.cfg = cfg
        self.mail_error = mail_error
        self.notifier = Notifier(store, harness, cfg.backlog)
        self.memory = MemoryStore(cfg.harness.data_dir / "memory.json", default_name=cfg.backlog.author_name or cfg.smtp.from_name)
        tools = BacklogTools(store, cfg.harness.data_dir / "archive", cfg.harness.state_db, owner=cfg.backlog.owner or cfg.smtp.from_name)
        tools.applier = self._apply_proposal
        tools.auto_apply = not cfg.chat.confirm_changes
        tools.memory = self.memory
        tools.seen_path = cfg.harness.data_dir / "chat_seen.json"
        self.events = EventLog(cfg.harness.data_dir / "stats" / "events.jsonl")
        tools.events = self.events
        self.chat = ChatService(cfg.chat.spec, cfg.chat.options, tools, self.memory)

    def update_task(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        data, previous = self.store.update(task_id, changes)
        self._status_changed(task_id, previous, data)
        return data

    def _status_changed(self, task_id: str, previous: str, task: dict[str, Any]) -> None:
        status = task.get("status")
        if status == previous:
            return
        if status == "in_progress":
            self.notifier.schedule(task_id)
        else:
            self.notifier.cancel(task_id)
        try:
            if status == "in_progress":
                self.memory.record_taken(task_id, task)
                self.chat.observe_activity()
            elif previous == "in_progress" and status == "new":
                self.memory.unrecord_taken(task_id)
        except OSError as e:
            log.warning("память: не удалось записать действие: %s", e)
        received = parse_time(task.get("received"))
        info = {"task_id": task_id, "kind": "chat" if task.get("source") == "chat" else task.get("kind") or "task",
                "priority": task.get("priority") or "normal",
                "from": "Вы (через чат)" if task.get("source") == "chat" else task.get("from_name") or task.get("from")}
        if status == "in_progress":
            waited = (datetime.now().astimezone() - received).total_seconds() / 3600 if received else None
            self.events.add("taken", **info, waited_hours=round(waited, 2) if waited is not None else None)
        elif status == "done":
            self.events.add("done", **info)

    def author_name(self) -> str:
        return self.memory.full_name() or self.cfg.backlog.author_name or self.cfg.smtp.from_name

    def settings(self) -> dict[str, Any]:
        m = self.memory.load()
        return {
            "profile": m["profile"],
            "memory": {k: m[k] for k in ("style", "preferences", "facts", "suggestions", "updated_at", "updates")} | {"taken": len(m["taken"])},
            "memory_enabled": self.chat.keeper is not None,
            "memory_path": str(self.memory.path),
        }

    def reset_memory(self) -> dict[str, Any]:
        self.memory.reset()
        self.chat.reset_memory()
        log.info("память ассистента очищена")
        return self.settings()

    def apply_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        result = self._apply_proposal(proposal)
        self.events.add("proposal_applied", action=proposal.get("action"))
        return result

    def stats(self, days: int | None) -> dict[str, Any]:
        return compute_stats(self.cfg.harness.data_dir, self.cfg.harness.state_db, days)

    def _apply_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        action = proposal.get("action")
        if action == "create":
            fields = dict(proposal.get("fields") or {})
            task_id = self.store.create(fields, author_name=self.author_name(), author_email=self.cfg.smtp.from_address)
            log.info("чат: создана задача «%s»", fields.get("title"))
            return {"task_id": task_id}
        task_id = str(proposal.get("task_id") or "")
        if action == "update":
            fields = {k: v.get("to") if isinstance(v, dict) else v for k, v in (proposal.get("changes") or {}).items()}
            note = proposal.get("note")
            if note:
                current = self.store.read(task_id).get("notes") or ""
                fields["notes"] = (current.rstrip() + "\n" if current.strip() else "") + str(note)
            data, previous = self.store.edit(task_id, fields)
            self._status_changed(task_id, previous, data)
            log.info("чат: изменена задача «%s»: %s", data.get("title"), ", ".join(fields))
            return {"task_id": task_id}
        if action == "reply":
            return send_task_reply(self.harness, self.store, task_id, str(proposal.get("body") or ""))
        if action == "delete":
            self.notifier.cancel(task_id)
            title = self.store.delete(task_id, self.cfg.harness.data_dir / "trash")
            return {"task_id": task_id, "deleted": title}
        raise ValueError(f"неизвестное действие: {action}")

    def status(self) -> dict[str, Any]:
        base = {
            "notify": {"enabled": self.notifier.enabled, "delay": self.cfg.backlog.notify_delay},
            "cleanup": self.cfg.backlog.cleanup_done,
        }
        if self.harness is None:
            return {**base, "mail": "off", "detail": self.mail_error or "почта не подключена", "problems": []}
        h = self.harness
        problems = [h.last_error] if h.last_error else []
        problems += self._failed_recently(h)
        return {
            **base,
            "mail": "error" if h.last_error else ("dry_run" if h.dry_run else "live"),
            "detail": h.last_error or "",
            "last_poll_at": h.last_poll_at,
            "mailbox": h.cfg.imap.username,
            "problems": problems,
        }

    @staticmethod
    def _failed_recently(h: Harness) -> list[str]:
        try:
            with sqlite3.connect(f"file:{h.cfg.harness.state_db}?mode=ro", uri=True, timeout=1) as db:
                rows = db.execute(
                    "SELECT uid, detail FROM processed WHERE status = 'error' AND updated_at > ? ORDER BY updated_at DESC LIMIT 5",
                    (time.time() - 86400,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [f"Письмо UID {uid} не обработано: {detail}" for uid, detail in rows]


def make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def _parts(self) -> list[str]:
            return [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]

        def do_GET(self) -> None:
            parts = self._parts()
            try:
                if not parts:
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif parts == ["api", "tasks"]:
                    self._json(app.store.list())
                elif parts == ["api", "chat", "info"]:
                    self._json(app.chat.info())
                elif parts == ["api", "events"]:
                    self._events()
                elif parts == ["api", "status"]:
                    self._json(app.status())
                elif parts == ["api", "settings"]:
                    self._json(app.settings())
                elif parts == ["api", "stats"]:
                    raw = (parse_qs(urlparse(self.path).query).get("days") or ["30"])[0]
                    self._json(app.stats(int(raw) if raw.isdigit() and int(raw) > 0 else None))
                elif len(parts) == 3 and parts[0] == "files":
                    path = app.store.file_path(parts[1], parts[2])
                    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    disposition = "inline" if ctype.startswith(("image/", "text/")) or ctype == "application/pdf" else "attachment"
                    self._send(200, path.read_bytes(), ctype,
                               {"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(path.name)}"})
                else:
                    self._json({"error": "not found"}, 404)
            except (FileNotFoundError, ValueError):
                self._json({"error": "не найдено"}, 404)

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last_rev = last_status = None
            last_ping = time.monotonic()
            try:
                while True:
                    rev = app.store.revision()
                    if rev != last_rev:
                        self._event("change", rev)
                        last_rev = rev
                    status = json.dumps(app.status(), ensure_ascii=False)
                    if status != last_status:
                        self._event("status", status)
                        last_status = status
                    if time.monotonic() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.monotonic()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _chat(self, body: dict[str, Any]) -> None:
            message = str(body.get("message") or "").strip()
            thread = str(body.get("thread") or "default")[:100]
            if not message:
                self._json({"error": "пустое сообщение"}, 400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for event in app.chat.stream(thread, message[:4000]):
                    self.wfile.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def _event(self, name: str, data: str) -> None:
            self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        def do_POST(self) -> None:
            parts = self._parts()
            try:
                if parts == ["api", "chat"]:
                    self._chat(self._body())
                elif parts == ["api", "chat", "apply"]:
                    self._json(app.apply_proposal(self._body().get("proposal") or {}))
                elif parts == ["api", "settings", "profile"]:
                    app.memory.set_profile(self._body())
                    self._json(app.settings())
                elif parts == ["api", "settings", "memory", "reset"]:
                    self._json(app.reset_memory())
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "update":
                    self._json(app.update_task(parts[2], self._body()))
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "open":
                    open_in_os(app.store.file_path(parts[2], self._body().get("file")))
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)
            except FileNotFoundError:
                self._json({"error": "не найдено"}, 404)
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, 400)

    return Handler


def serve(cfg: Config, harness: Harness | None, port: int = 8766, mail_error: str | None = None) -> None:
    store = BacklogStore(cfg.harness.data_dir / "backlog")
    app = WebApp(store, harness, cfg, mail_error)
    cleanup = CleanupScheduler(store, cfg.backlog.cleanup_done, cfg.harness.data_dir / "archive")
    cleanup.start()
    app.chat.start()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    thread = None
    if harness is not None:
        thread = threading.Thread(target=harness.run_forever, name="harness", daemon=True)
        thread.start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    log.info("бэклог: http://127.0.0.1:%d/", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        cleanup.stop()
        app.chat.stop()
        app.notifier.cancel_all()
        if harness is not None:
            harness.stop()
            if thread is not None:
                thread.join(timeout=10)
