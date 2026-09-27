from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .tasks import now_iso, parse_time

log = logging.getLogger("mail_harness.memory")

PROFILE_FIELDS = ("first_name", "last_name", "role")
MAX_TAKEN = 50
MAX_TRANSCRIPT = 80
UNDO_WINDOW = 120


def _empty(default_name: str = "") -> dict[str, Any]:
    first, _, last = default_name.strip().partition(" ")
    return {
        "profile": {"first_name": first, "last_name": last.strip(), "role": ""},
        "style": "",
        "preferences": "",
        "facts": [],
        "suggestions": [],
        "taken": [],
        "updated_at": None,
        "updates": 0,
    }


class MemoryStore:
    def __init__(self, path: Path, default_name: str = ""):
        self.path = path
        self.default_name = default_name
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        data = _empty(self.default_name)
        with self._lock:
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return data
            except (OSError, ValueError) as e:
                log.warning("память %s не читается (%s) — начинаю с пустой", self.path, e)
                return data
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key == "profile" and isinstance(value, dict):
                    data["profile"].update({k: str(value.get(k) or "") for k in PROFILE_FIELDS if k in value})
                elif key in data:
                    data[key] = value
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _change(self, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            data = self.load()
            fn(data)
            self._save(data)
            return data

    def profile(self) -> dict[str, str]:
        return self.load()["profile"]

    def full_name(self) -> str:
        p = self.profile()
        return " ".join(x for x in (p["first_name"], p["last_name"]) if x)

    def set_profile(self, fields: dict[str, Any]) -> dict[str, Any]:
        clean = {}
        for key in PROFILE_FIELDS:
            if key in fields:
                value = " ".join(str(fields[key] or "").split())
                clean[key] = value[:120 if key == "role" else 60]
        return self._change(lambda d: d["profile"].update(clean))

    def record_taken(self, task_id: str, task: dict[str, Any]) -> None:
        received = parse_time(task.get("received"))
        entry = {
            "at": now_iso(),
            "task_id": task_id,
            "title": (task.get("title") or task.get("subject") or "")[:160],
            "from": task.get("from_name") or task.get("from") or ("сам пользователь" if task.get("source") == "chat" else None),
            "kind": task.get("kind") or "task",
            "priority": task.get("priority") or "normal",
            "due_date": task.get("due_date"),
            "waited_hours": round((datetime.now().astimezone() - received).total_seconds() / 3600, 1) if received else None,
        }
        self._change(lambda d: d.__setitem__("taken", [*d["taken"], entry][-MAX_TAKEN:]))

    def unrecord_taken(self, task_id: str) -> None:
        def drop(d: dict[str, Any]) -> None:
            for i in range(len(d["taken"]) - 1, -1, -1):
                t = d["taken"][i]
                at = parse_time(t.get("at"))
                if t.get("task_id") == task_id and at and time.time() - at.timestamp() < UNDO_WINDOW:
                    del d["taken"][i]
                    return
        self._change(drop)

    def apply_update(self, update: dict[str, Any]) -> dict[str, Any]:
        def merge(d: dict[str, Any]) -> None:
            d["style"] = str(update.get("style") or "")[:1500]
            d["preferences"] = str(update.get("preferences") or "")[:1500]
            d["facts"] = [str(f)[:300] for f in update.get("facts") or []][:10]
            d["suggestions"] = [str(s)[:80] for s in update.get("suggestions") or [] if str(s).strip()][:4]
            d["updated_at"] = now_iso()
            d["updates"] = int(d.get("updates") or 0) + 1
        return self._change(merge)

    def reset(self) -> dict[str, Any]:
        def clear(d: dict[str, Any]) -> None:
            fresh = _empty()
            for key in ("style", "preferences", "facts", "suggestions", "taken", "updated_at", "updates"):
                d[key] = fresh[key]
        return self._change(clear)

    def prompt_block(self) -> str:
        d = self.load()
        p = d["profile"]
        name = " ".join(x for x in (p["first_name"], p["last_name"]) if x) or "не указано"
        lines = [f"Пользователь: {name}" + (f", роль: {p['role']}" if p["role"] else "") + "."]
        learned = []
        if d["style"]:
            learned.append(f"- Стиль общения: {d['style']}")
        if d["preferences"]:
            learned.append(f"- Какие задачи берёт в работу: {d['preferences']}")
        if d["facts"]:
            learned.append("- Факты: " + "; ".join(d["facts"]))
        if learned:
            lines.append("Что ты о нём знаешь (память обновляется автоматически по вашим диалогам и по тому, какие задачи он берёт в работу):")
            lines.extend(learned)
        return "\n".join(lines)


class MemoryKeeper:
    def __init__(self, store: MemoryStore, updater: Callable[[dict[str, Any], list[dict[str, str]]], dict[str, Any]],
                 idle: float = 180, every: int = 10):
        self.store = store
        self.updater = updater
        self.idle = idle
        self.every = every
        self._transcript: list[dict[str, str]] = []
        self._replies = 0
        self._dirty = False
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._running = threading.Lock()

    def observe_chat(self, user_text: str, assistant_text: str) -> None:
        with self._lock:
            self._transcript.append({"role": "user", "text": user_text[:1500]})
            if assistant_text:
                self._transcript.append({"role": "assistant", "text": assistant_text[:800]})
            del self._transcript[:-MAX_TRANSCRIPT]
            self._replies += 1
            self._dirty = True
            due = self._replies >= self.every
        if due:
            self.flush_async()
        else:
            self._arm()

    def observe_activity(self) -> None:
        with self._lock:
            self._dirty = True
        self._arm()

    def _arm(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.idle, self.flush)
            self._timer.daemon = True
            self._timer.start()

    def flush_async(self) -> None:
        threading.Thread(target=self.flush, name="memory-update", daemon=True).start()

    def flush(self) -> bool:
        if not self._running.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if not self._dirty:
                    return False
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                transcript = list(self._transcript)
                replies = self._replies
                self._dirty = False
            try:
                update = self.updater(copy.deepcopy(self.store.load()), transcript)
                self.store.apply_update(update)
            except Exception as e:
                log.warning("память не обновлена: %s: %s", type(e).__name__, e)
                with self._lock:
                    self._dirty = True
                self._arm()
                return False
            with self._lock:
                del self._transcript[:len(transcript)]
                self._replies = max(0, self._replies - replies)
            log.info("память ассистента обновлена (%d реплик)", len(transcript))
            return True
        finally:
            self._running.release()

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
