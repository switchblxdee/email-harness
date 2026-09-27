from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .tasks import TASK_FILE, parse_time

log = logging.getLogger("mail_harness.stats")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

SOURCES = ("chat", "memory", "mail")
KIND_NAMES = {"task": "Задачи", "meeting": "Встречи", "question": "Вопросы", "chat": "Созданы в чате"}
PRIORITY_NAMES = {"high": "Высокий", "normal": "Обычный", "low": "Низкий"}
TOOL_NAMES = {
    "list_tasks": "Искал задачи", "get_task": "Открывал задачу", "stats": "Считал сводку", "search_archive": "Искал в архиве",
    "mail_activity": "Смотрел почту", "read_attachment": "Читал файлы", "propose_create": "Создавал задачи",
    "propose_update": "Менял задачи", "propose_reply": "Писал ответы", "propose_delete": "Удалял задачи",
}
ACTION_NAMES = {"create": "Создать", "update": "Изменить", "reply": "Ответить", "delete": "Удалить"}
STOPWORDS = set("""
а без более бы был была были было быть в вам вас весь во вот все всё всего всех вы где да даже для до его ее её ей ему если есть еще ещё же за
здесь и из или им их к как какая какие какой ко когда кто ли либо мне меня мной мы на над надо наш не него нее неё нет ни них но ну о об
однако он она они оно от очень по под после при про с со так также такой там те тем то того тоже той только том ты у уже хотя чего чей
чем что чтобы чье чьё эта эти это этот я пожалуйста давай можно нужно сделай покажи скажи какие сколько теперь сейчас просто вообще
тебя тебе твои твой мои мой моя моё мое есть был будет будут могу можешь хочу хорошо спасибо окей привет
the and for you that this with are what from have
""".split())
WORD_RE = re.compile(r"[a-zа-яё][a-zа-яё\-]{3,}", re.IGNORECASE)


class EventLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.path.resolve()), threading.Lock())

    def add(self, event_type: str, /, **fields: Any) -> None:
        row = {"at": round(time.time(), 3), "type": event_type, **{k: v for k, v in fields.items() if v is not None}}
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except OSError as e:
            log.warning("статистика: не удалось записать событие: %s", e)

    def read(self, since: float | None = None) -> list[dict[str, Any]]:
        try:
            with self._lock:
                lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            log.warning("статистика: не удалось прочитать журнал: %s", e)
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (since is None or float(row.get("at") or 0) >= since):
                rows.append(row)
        return rows

    def usage(self, source: str, model: str | None, input_tokens: int, output_tokens: int, cached: int = 0) -> None:
        self.add("usage", source=source, model=model, input=int(input_tokens or 0), output=int(output_tokens or 0), cached=int(cached or 0))


def _read_tasks(data_dir: Path) -> list[dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for pattern, archived in ((f"backlog/*/{TASK_FILE}", False), (f"archive/*/*/{TASK_FILE}", True)):
        for path in data_dir.glob(pattern):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            data["id"] = path.parent.name
            data["_archived"] = archived
            tasks.setdefault(data["id"], data)
    return list(tasks.values())


def _kind(task: dict[str, Any]) -> str:
    return "chat" if task.get("source") == "chat" else (task.get("kind") or "task")


def _sender(task: dict[str, Any]) -> str:
    if task.get("source") == "chat":
        return "Вы (через чат)"
    return task.get("from_name") or task.get("from") or "неизвестно"


def _arrived(task: dict[str, Any]) -> datetime | None:
    return parse_time(task.get("created_at") if task.get("source") == "chat" else task.get("received"))


def _hours(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "mean": None, "n": 0}
    return {"median": round(statistics.median(values), 1), "mean": round(statistics.fmean(values), 1), "n": len(values)}


def _day(ts: float | datetime) -> str:
    d = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts).astimezone()
    return d.astimezone().date().isoformat()


def _top(counter: Counter, n: int) -> list[dict[str, Any]]:
    return [{"name": k, "value": v} for k, v in counter.most_common(n)]


def _skip_reason(detail: str) -> str:
    detail = (detail or "").strip()
    if detail.startswith("служебный адрес"):
        return "служебный адрес (noreply и т.п.)"
    if detail.startswith("есть заголовок List-"):
        return "рассылка"
    if detail.startswith("Precedence"):
        return "рассылка" if any(w in detail for w in ("bulk", "list", "junk")) else "автоответ"
    if detail.startswith(("Auto-Submitted", "X-Auto-Response-Suppress", "тема похожа на автоответ", "есть заголовок X-Auto")):
        return "автоответ"
    if detail.startswith(("пустой Return-Path", "multipart/report")):
        return "уведомление о доставке"
    return detail.split(":")[0][:60] or "без причины"


def compute(data_dir: Path, state_db: Path | None, days: int | None, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now()).astimezone()
    since_dt = now - timedelta(days=days) if days else None
    since = since_dt.timestamp() if since_dt else None
    in_period = lambda d: d is not None and (since_dt is None or d >= since_dt)
    series_days = min(days or 90, 90)
    day_keys = [(now.date() - timedelta(days=i)).isoformat() for i in range(series_days - 1, -1, -1)]
    today = now.date()

    events = EventLog(data_dir / "stats" / "events.jsonl").read(since)
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_type[e.get("type", "")].append(e)

    tasks = _read_tasks(data_dir)
    incoming = [t for t in tasks if in_period(_arrived(t))]
    done = [t for t in tasks if t.get("status") == "done" and in_period(parse_time(t.get("done_at")))]
    active = [t for t in tasks if not t["_archived"] and t.get("status") != "done"]
    overdue = [t for t in active if t.get("due_date") and t["due_date"] < today.isoformat()]
    with_due = [t for t in done if t.get("due_date")]
    on_time = [t for t in with_due if parse_time(t.get("done_at")).date().isoformat() <= t["due_date"]]
    to_done = [(parse_time(t["done_at"]) - _arrived(t)).total_seconds() / 3600 for t in done if _arrived(t)]
    taken = by_type["taken"]
    to_take = [float(e["waited_hours"]) for e in taken if e.get("waited_hours") is not None]

    series_in = Counter(_day(_arrived(t)) for t in incoming)
    series_done = Counter(_day(parse_time(t["done_at"])) for t in done)
    senders_in = Counter(_sender(t) for t in incoming)
    senders_done = Counter(_sender(t) for t in done)
    weekday_done = Counter(parse_time(t["done_at"]).astimezone().weekday() for t in done)

    deleted = 0
    trash = data_dir / "trash"
    if trash.is_dir():
        for day_dir in trash.iterdir():
            try:
                d = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if since_dt is None or d >= since_dt.date():
                deleted += sum(1 for p in day_dir.iterdir() if p.is_dir())

    user_msgs = by_type["chat_user"]
    replies = by_type["chat_reply"]
    words = Counter()
    phrases = Counter()
    for m in user_msgs:
        text = str(m.get("text") or "").lower()
        words.update(w for w in WORD_RE.findall(text) if w not in STOPWORDS)
        norm = " ".join(re.sub(r"[^\w\s]", " ", text).split())
        if norm:
            phrases[norm] += 1
    tools = Counter(TOOL_NAMES.get(n, n) for r in replies for n in set(r.get("tools") or []))
    proposed = Counter(a for r in replies for a in r.get("proposals") or [])
    applied = Counter(e.get("action") for e in by_type["proposal_applied"])

    usage = by_type["usage"]
    tok_day: dict[str, Counter] = defaultdict(Counter)
    tok_source: dict[str, Counter] = defaultdict(Counter)
    tok_model: Counter = Counter()
    for u in usage:
        src = u.get("source") if u.get("source") in SOURCES else "mail"
        total = int(u.get("input") or 0) + int(u.get("output") or 0)
        tok_day[_day(u["at"])][src] += total
        for key in ("input", "output", "cached"):
            tok_source[src][key] += int(u.get(key) or 0)
        tok_source[src]["calls"] += 1
        tok_model[u.get("model") or "неизвестно"] += total
    chat_replies_ok = [r for r in replies if not r.get("error")]

    mail: dict[str, Any] = {"available": False}
    if state_db and Path(state_db).exists():
        try:
            with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2) as db:
                rows = db.execute("SELECT status, detail FROM processed WHERE ? IS NULL OR updated_at >= ?", (since, since)).fetchall()
                sent = db.execute("SELECT dry_run, COUNT(DISTINCT source_key) FROM replies WHERE ? IS NULL OR sent_at >= ? GROUP BY dry_run",
                                  (since, since)).fetchall()
            statuses = Counter(s for s, _ in rows)
            upd = sum(1 for s, d in rows if s == "done" and (d or "").startswith("UPD"))
            sent_map = {bool(dr): n for dr, n in sent}
            mail = {
                "available": True,
                "processed": len(rows),
                "to_backlog": statuses.get("done", 0) - upd,
                "upd": upd,
                "skipped": statuses.get("skipped", 0),
                "errors": statuses.get("error", 0),
                "auto_replies": sent_map.get(False, 0),
                "auto_replies_dry": sent_map.get(True, 0),
                "skip_reasons": _top(Counter(_skip_reason(d) for s, d in rows if s == "skipped"), 6),
            }
        except sqlite3.Error as e:
            mail = {"available": False, "error": str(e)}

    return {
        "period_days": days,
        "generated_at": now.isoformat(timespec="seconds"),
        "since": since_dt.date().isoformat() if since_dt else None,
        "tasks": {
            "incoming": len(incoming),
            "incoming_mail": sum(1 for t in incoming if t.get("source") != "chat"),
            "incoming_chat": sum(1 for t in incoming if t.get("source") == "chat"),
            "done": len(done),
            "active_now": len(active),
            "in_progress_now": sum(1 for t in active if t.get("status") == "in_progress"),
            "overdue_now": len(overdue),
            "taken": len(taken),
            "deleted": deleted,
            "on_time": {"done": len(on_time), "with_due": len(with_due)},
            "hours_to_take": _hours(to_take),
            "hours_to_done": _hours(to_done),
            "daily": [{"day": d, "incoming": series_in.get(d, 0), "done": series_done.get(d, 0)} for d in day_keys],
            "senders": [{"name": n, "incoming": v, "done": senders_done.get(n, 0)} for n, v in senders_in.most_common(8)],
            "kinds": [{"name": KIND_NAMES.get(k, k), "incoming": sum(1 for t in incoming if _kind(t) == k),
                       "done": sum(1 for t in done if _kind(t) == k)} for k in KIND_NAMES],
            "priorities": [{"name": PRIORITY_NAMES[p], "incoming": sum(1 for t in incoming if (t.get("priority") or "normal") == p),
                            "done": sum(1 for t in done if (t.get("priority") or "normal") == p)} for p in PRIORITY_NAMES],
            "taken_by_kind": _top(Counter(KIND_NAMES.get(e.get("kind") or "task", e.get("kind")) for e in taken), 4),
            "taken_by_sender": _top(Counter(e.get("from") or "неизвестно" for e in taken), 6),
            "done_by_weekday": [weekday_done.get(i, 0) for i in range(7)],
        },
        "chat": {
            "user_messages": len(user_msgs),
            "replies": len(replies),
            "errors": sum(1 for r in replies if r.get("error")),
            "avg_user_chars": round(statistics.fmean(m.get("chars") or 0 for m in user_msgs)) if user_msgs else None,
            "avg_reply_seconds": round(statistics.fmean(r.get("seconds") or 0 for r in chat_replies_ok), 1) if chat_replies_ok else None,
            "daily": [{"day": d, "messages": v} for d, v in ((d, sum(1 for m in user_msgs if _day(m["at"]) == d)) for d in day_keys)],
            "top_words": _top(words, 16),
            "repeated": [x for x in _top(phrases, 6) if x["value"] > 1],
            "tools": _top(tools, 10),
            "proposals": [{"name": ACTION_NAMES.get(a, a), "proposed": proposed.get(a, 0), "applied": applied.get(a, 0)}
                          for a in ACTION_NAMES if proposed.get(a) or applied.get(a)],
            "memory_updates": len([u for u in usage if u.get("source") == "memory"]),
        },
        "tokens": {
            "total": sum(int(u.get("input") or 0) + int(u.get("output") or 0) for u in usage),
            "input": sum(int(u.get("input") or 0) for u in usage),
            "output": sum(int(u.get("output") or 0) for u in usage),
            "cached": sum(int(u.get("cached") or 0) for u in usage),
            "calls": len(usage),
            "per_chat_reply": round((tok_source["chat"]["input"] + tok_source["chat"]["output"]) / len(chat_replies_ok))
            if chat_replies_ok and tok_source["chat"]["calls"] else None,
            "by_source": {s: dict(tok_source[s]) for s in SOURCES},
            "daily": [{"day": d, **{s: tok_day[d].get(s, 0) for s in SOURCES}} for d in day_keys],
            "by_model": _top(tok_model, 5),
        },
        "mail": mail,
    }
