from __future__ import annotations

import csv
import importlib
import io
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from xml.etree import ElementTree

from .compose import reply_subject
from .memory import MemoryKeeper, MemoryStore
from .stats import EventLog
from .tasks import PRIORITIES, STATUSES, TASK_FILE, BacklogStore, parse_time, task_recipient

log = logging.getLogger("mail_harness.assistant")

STATUS_NAMES = {"new": "Новые", "in_progress": "В работе", "done": "Готово"}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
XLSX_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "A")
    n = 0
    for ch in letters.group(0) if letters else "A":
        n = n * 26 + (ord(ch) - 64)
    return n - 1


BUILTIN_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}


BUILTIN_PERCENT_FORMATS = {9, 10}


def _style_kinds(z: zipfile.ZipFile, names: set[str]) -> dict[int, str]:
    if "xl/styles.xml" not in names:
        return {}
    root = ElementTree.fromstring(z.read("xl/styles.xml"))
    custom: dict[int, str] = {}
    for fmt in root.findall("m:numFmts/m:numFmt", XLSX_NS):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", fmt.get("formatCode", "")).lower()
        if "%" in code:
            custom[int(fmt.get("numFmtId", "0"))] = "percent"
        elif re.search(r"[dy]|m{1,4}(?!.*[hs])", code) and "0.0" not in code:
            custom[int(fmt.get("numFmtId", "0"))] = "date"
    kinds = {}
    for i, xf in enumerate(root.findall("m:cellXfs/m:xf", XLSX_NS)):
        num = int(xf.get("numFmtId", "0"))
        kind = "date" if num in BUILTIN_DATE_FORMATS else "percent" if num in BUILTIN_PERCENT_FORMATS else custom.get(num)
        if kind:
            kinds[i] = kind
    return kinds


def _excel_value(raw: str, kind: str | None) -> str:
    try:
        number = float(raw)
    except ValueError:
        return raw
    if kind == "percent":
        return f"{number * 100:.4g}%"
    if kind == "date" and 0 < number < 2958466:
        moment = datetime(1899, 12, 30) + timedelta(days=number)
        return moment.date().isoformat() if number == int(number) else moment.strftime("%Y-%m-%d %H:%M")
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.6g}" if abs(number) >= 1e-4 else raw


def read_xlsx(path: Path, max_rows: int = 30, max_sheets: int = 3) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        style_kinds = _style_kinds(z, names)
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", XLSX_NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{XLSX_NS['m']}}}t")))
        workbook = ElementTree.fromstring(z.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", XLSX_NS)}
        sheets = []
        for sheet in workbook.findall("m:sheets/m:sheet", XLSX_NS)[:max_sheets]:
            target = targets.get(sheet.get(f"{{{XLSX_NS['r']}}}id"), "")
            target = target.lstrip("/")
            part = target if target.startswith("xl/") else f"xl/{target}"
            if part not in names:
                continue
            rows: list[list[str]] = []
            total = 0
            for row in ElementTree.fromstring(z.read(part)).iter(f"{{{XLSX_NS['m']}}}row"):
                total += 1
                if len(rows) >= max_rows:
                    continue
                values: dict[int, str] = {}
                for cell in row.findall("m:c", XLSX_NS):
                    kind = cell.get("t")
                    v = cell.find("m:v", XLSX_NS)
                    if kind == "s" and v is not None and v.text and v.text.isdigit():
                        text = shared[int(v.text)] if int(v.text) < len(shared) else ""
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(f"{{{XLSX_NS['m']}}}t"))
                    elif kind == "b" and v is not None:
                        text = "TRUE" if v.text == "1" else "FALSE"
                    elif kind in ("str", "e"):
                        text = v.text if v is not None and v.text is not None else ""
                    else:
                        raw = v.text if v is not None and v.text is not None else ""
                        text = _excel_value(raw, style_kinds.get(int(cell.get("s", "0")))) if raw else ""
                    values[_col_index(cell.get("r", ""))] = text
                if values:
                    width = max(values) + 1
                    rows.append([values.get(i, "") for i in range(width)])
            sheets.append({"sheet": sheet.get("name"), "rows_total": total, "rows": rows})
        return sheets


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_SHORT_ID = re.compile(r"_([0-9a-f]{8})$")
HIDDEN_TASK_KEYS = ("references", "message_id", "notify_message_id", "original_description", "reply_to", "has_update")


def short_id(task_id: str) -> str:
    m = _SHORT_ID.search(task_id)
    return m.group(1) if m else task_id


AUTO_FULL_ROWS = 60
PREVIEW_ROWS = 30


def _tsv(rows: list[list[Any]]) -> str:
    return "\n".join("\t".join(" ".join(str(c).split()) for c in row) for row in rows)


def _table(rows: list[list[Any]], total: int, max_rows: int | None) -> dict[str, Any]:
    shown = max_rows or (total if total <= AUTO_FULL_ROWS else PREVIEW_ROWS)
    out: dict[str, Any] = {"rows_total": total, "rows": _tsv(rows[:shown])}
    if total > shown:
        out["truncated"] = (f"показаны первые {shown} из {total} строк. Если для ответа нужны все строки (поиск, подсчёт, фильтр), "
                            f"сначала вызови read_attachment ещё раз с max_rows={min(total, 1000)}")
    return out


class BacklogTools:
    def __init__(self, store: BacklogStore, archive_root: Path, state_db: Path | None = None, owner: str = ""):
        self.store = store
        self.archive_root = archive_root
        self.state_db = state_db
        self.owner = owner
        self.applier: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.auto_apply = False
        self.memory: MemoryStore | None = None
        self.seen_path: Path | None = None
        self.events: EventLog | None = None
        self._seen: dict[str, int] | None = None
        self._seen_lock = threading.Lock()

    @staticmethod
    def today() -> date:
        return date.today()

    def context_line(self) -> str:
        d = self.today()
        return f"{d.isoformat()}, {WEEKDAYS[d.weekday()]}"

    def _due_bucket(self, task: dict[str, Any]) -> str | None:
        due = task.get("due_date")
        if not due or task.get("status") == "done":
            return None
        try:
            days = (date.fromisoformat(due) - self.today()).days
        except ValueError:
            return None
        if days < 0:
            return "overdue"
        if days == 0:
            return "today"
        if days <= 7:
            return "week"
        return "later"

    def list_tasks(self, status: str | None = None, query: str | None = None, due: str | None = None, limit: int = 20) -> dict[str, Any]:
        tasks = self.store.list()
        if status:
            if status not in STATUSES:
                return {"error": f"статус должен быть одним из {', '.join(STATUSES)}"}
            tasks = [t for t in tasks if t["status"] == status]
        if due:
            if due == "none":
                tasks = [t for t in tasks if not t.get("due_date")]
            elif due == "week":
                tasks = [t for t in tasks if self._due_bucket(t) in ("overdue", "today", "week")]
            else:
                tasks = [t for t in tasks if self._due_bucket(t) == due]
        if query:
            q = query.lower()
            tasks = [t for t in tasks if q in " ".join(str(t.get(k) or "") for k in
                     ("title", "summary", "description", "subject", "from", "from_name", "notes")).lower()
                     or any(q in f["name"].lower() for f in t.get("files") or [])]
        tasks.sort(key=lambda t: (t.get("due_date") or "9999-99-99", t.get("received") or ""))
        lines = [self._snapshot_line(t, summary=bool(query)) for t in tasks[:limit]]
        out: dict[str, Any] = {"count": len(tasks), "tasks": "\n".join(lines) or "ничего не найдено"}
        if len(tasks) > limit:
            out["note"] = f"показаны первые {limit} — уточни фильтр или увеличь limit"
        return out

    def get_task(self, task_id: str) -> dict[str, Any]:
        match = self._find(task_id)
        if match is None:
            return {"error": f"задача {task_id} не найдена"}
        full = {k: v for k, v in match.items() if k not in HIDDEN_TASK_KEYS and v not in (None, "", [], {})}
        full["id"] = short_id(match["id"])
        description = full.get("description") or ""
        if len(description) > 6000:
            full["description"] = description[:6000] + "\n[…обрезано…]"
        for key in ("updates", "replies"):
            if key in full:
                full[key] = [{k: v for k, v in item.items() if k not in ("message_id", "dry_run")} for item in full[key]]
        return full

    def stats(self) -> dict[str, Any]:
        tasks = self.store.list()
        by_status = Counter(t["status"] for t in tasks)
        buckets = Counter(self._due_bucket(t) for t in tasks)
        active = [t for t in tasks if t["status"] != "done"]
        senders = Counter(t.get("from_name") or t.get("from") or "неизвестно" for t in active)
        week_ago = datetime.now().astimezone() - timedelta(days=7)
        recent = [t for t in tasks if (parse_time(t.get("received")) or week_ago) > week_ago]
        return {
            "today": self.context_line(),
            "total": len(tasks),
            "by_status": {STATUS_NAMES[s]: by_status.get(s, 0) for s in STATUSES},
            "overdue": buckets.get("overdue", 0),
            "due_today": buckets.get("today", 0),
            "due_this_week": buckets.get("week", 0),
            "active_high_priority": sum(1 for t in active if t.get("priority") == "high"),
            "received_last_7_days": len(recent),
            "top_senders_active": senders.most_common(5),
        }

    def search_archive(self, query: str | None = None, days: int = 30, limit: int = 30) -> dict[str, Any]:
        if not self.archive_root.is_dir():
            return {"count": 0, "tasks": []}
        since = self.today() - timedelta(days=days)
        found = []
        for task_json in self.archive_root.glob(f"*/*/{TASK_FILE}"):
            try:
                day = date.fromisoformat(task_json.parent.parent.name)
                data = json.loads(task_json.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            if day < since:
                continue
            if query and query.lower() not in " ".join(str(data.get(k) or "") for k in ("title", "summary", "description", "from", "from_name", "notes")).lower():
                continue
            found.append({"archived_day": day.isoformat(), "title": data.get("title"), "from": data.get("from_name") or data.get("from"),
                          "done_at": (data.get("done_at") or "")[:16], "summary": (data.get("summary") or "")[:220]})
        found.sort(key=lambda t: t["archived_day"], reverse=True)
        return {"count": len(found), "tasks": found[:limit]}

    def mail_activity(self, hours: int = 24, limit: int = 40) -> dict[str, Any]:
        if not self.state_db or not Path(self.state_db).exists():
            return {"error": "база почты недоступна (почта не подключена)"}
        since = time.time() - hours * 3600
        try:
            with sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True, timeout=2) as db:
                processed = db.execute(
                    "SELECT uid, status, detail, updated_at FROM processed WHERE updated_at > ? ORDER BY updated_at DESC LIMIT ?",
                    (since, limit)).fetchall()
                replies = db.execute(
                    "SELECT recipient, subject, sent_at, dry_run FROM replies WHERE sent_at > ? ORDER BY sent_at DESC LIMIT ?",
                    (since, limit)).fetchall()
        except sqlite3.Error as e:
            return {"error": f"не удалось прочитать базу: {e}"}
        fmt = lambda ts: datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
        return {
            "hours": hours,
            "processed": [{"uid": u, "status": s, "detail": (d or "")[:200], "at": fmt(t)} for u, s, d, t in processed],
            "replies_sent": [{"to": r, "subject": subj, "at": fmt(t), "dry_run": bool(dr)} for r, subj, t, dr in replies],
            "counts": dict(Counter(s for _, s, _, _ in processed)),
        }

    def read_attachment(self, task_id: str, filename: str, max_rows: int | None = None) -> dict[str, Any]:
        task = self._find(task_id)
        try:
            path = self.store.file_path(task["id"] if task else task_id, filename)
        except (FileNotFoundError, ValueError):
            return {"error": f"файл {filename} в задаче {task_id} не найден"}
        ext = path.suffix.lower()
        limit = max(1, min(int(max_rows), 1000)) if max_rows else 1000
        try:
            if ext in (".xlsx", ".xlsm"):
                sheets = read_xlsx(path, max_rows=limit)
                return {"file": path.name, "format": "TSV", "sheets": [_table(sh["rows"], sh["rows_total"], max_rows) | {"sheet": sh["sheet"]}
                                                                        for sh in sheets]}
            if ext in (".csv", ".tsv"):
                text = _decode(path.read_bytes())
                dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
                rows = list(csv.reader(io.StringIO(text), dialect))
                return {"file": path.name, "format": "TSV", **_table(rows, len(rows), max_rows)}
            if ext in (".txt", ".md", ".json", ".xml", ".html", ".eml"):
                text = _decode(path.read_bytes())
                return {"file": path.name, "text": text[:8000], "truncated": len(text) > 8000}
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, csv.Error) as e:
            return {"error": f"не удалось прочитать {path.name}: {type(e).__name__}: {e}"}
        return {"file": path.name, "size": path.stat().st_size, "error": f"формат {ext or 'без расширения'} прочитать не могу — могу только сказать, что файл есть"}

    def _find(self, task_id: str) -> dict[str, Any] | None:
        ref = str(task_id or "").strip().strip("[]").strip()
        if not ref:
            return None
        tasks = self.store.list()
        exact = next((t for t in tasks if t["id"] == ref), None)
        if exact is not None:
            return exact
        matches = [t for t in tasks if t["id"].endswith("_" + ref)]
        return matches[0] if len(matches) == 1 else None

    def _offer(self, proposal: dict[str, Any]) -> dict[str, Any]:
        proposal["id"] = uuid.uuid4().hex[:12]
        if self.auto_apply and self.applier and proposal["action"] not in ("delete", "reply"):
            try:
                result = self.applier(proposal)
            except (ValueError, FileNotFoundError) as e:
                return {"error": str(e)}
            return {"applied": {**proposal, **result}, "message": "Изменение применено."}
        return {"proposal": proposal, "message": "Изменение показано пользователю. Оно применится, только если он нажмёт «Применить»."}

    def propose_create(self, title: str, description: str = "", summary: str | None = None, priority: str = "normal",
                       due_date: str | None = None, due: str | None = None, status: str = "new", notes: str | None = None) -> dict[str, Any]:
        fields = {"title": title, "description": description, "summary": summary, "priority": priority,
                  "due_date": due_date, "due": due, "status": status, "notes": notes}
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        try:
            clean = self.store.validate(fields)
        except ValueError as e:
            return {"error": str(e)}
        return self._offer({"action": "create", "title": clean["title"], "fields": clean})

    def propose_update(self, task_id: str, title: str | None = None, summary: str | None = None, description: str | None = None,
                       priority: str | None = None, due_date: str | None = None, due: str | None = None, status: str | None = None,
                       note: str | None = None, clear_due_date: bool = False, reason: str = "") -> dict[str, Any]:
        task = self._find(task_id)
        if task is None:
            return {"error": f"задача {task_id} не найдена"}
        wanted = {"title": title, "summary": summary, "description": description, "priority": priority,
                  "due_date": due_date, "due": due, "status": status}
        wanted = {k: v for k, v in wanted.items() if v is not None}
        if clear_due_date:
            wanted["due_date"] = None
            wanted["due"] = None
        try:
            clean = self.store.validate(wanted)
        except ValueError as e:
            return {"error": str(e)}
        changes = {k: {"from": task.get(k), "to": v} for k, v in clean.items() if task.get(k) != v}
        if not changes and not note:
            return {"error": "изменений нет: новые значения совпадают с текущими"}
        return self._offer({"action": "update", "task_id": task["id"], "title": task.get("title") or task.get("subject"),
                            "changes": changes, "note": note or None, "reason": reason})

    def propose_reply(self, task_id: str, body: str, reason: str = "") -> dict[str, Any]:
        task = self._find(task_id)
        if task is None:
            return {"error": f"задача {task_id} не найдена"}
        if task.get("source") == "chat":
            return {"error": "это задача, которую пользователь создал сам, — отвечать некому"}
        try:
            to = task_recipient(task, None)
        except ValueError as e:
            return {"error": str(e)}
        body = (body or "").strip()
        if not body:
            return {"error": "пустой текст ответа"}
        return self._offer({"action": "reply", "task_id": task["id"], "title": task.get("title") or task.get("subject"),
                            "to": str(to), "subject": reply_subject(task.get("subject") or task.get("title") or ""),
                            "body": body[:8000], "reason": reason})

    def _snapshot_line(self, t: dict[str, Any], summary: bool = False) -> str:
        parts = [f"[[{short_id(t['id'])}]] {t.get('title') or t.get('subject') or 'без названия'}", STATUS_NAMES.get(t["status"], t["status"])]
        if t.get("kind") == "meeting":
            parts.append(f"встреча {t.get('meeting_at') or t.get('due_date') or 'дата не указана'}")
        elif t.get("kind") == "question":
            parts.append("ждёт ответа" if not t.get("replies") else "вопрос, вы ответили")
        if t.get("priority") and t.get("priority") != "normal":
            parts.append(f"приоритет {t['priority']}")
        if t.get("due_date") and t.get("kind") != "meeting":
            parts.append(f"срок {t['due_date']}")
        if t.get("source") == "chat":
            parts.append("создана вами")
        elif t.get("from_name") or t.get("from"):
            parts.append(f"от {t.get('from_name') or t.get('from')}")
        if t.get("replies") and t.get("kind") != "question":
            parts.append(f"вы ответили {len(t['replies'])} раз")
        received = parse_time(t.get("received"))
        if received:
            parts.append(f"пришла {received.astimezone():%d.%m %H:%M}")
        updates = t.get("updates") or []
        if updates:
            last = updates[-1]
            text = " ".join((last.get("text") or "").split())
            parts.append(f"UPD ×{len(updates)}{' (не прочитано)' if t.get('has_update') else ''}: последний от "
                         f"{last.get('from_name') or last.get('from')} — «{text[:160]}{'…' if len(text) > 160 else ''}»")
        if summary and t.get("summary"):
            parts.append(f"суть: {' '.join(str(t['summary']).split())[:220]}")
        return "- " + " | ".join(parts)

    def _load_seen(self) -> dict[str, int] | None:
        if self._seen is None and self.seen_path is not None:
            try:
                data = json.loads(Path(self.seen_path).read_text(encoding="utf-8"))
                self._seen = {str(k): int(v) for k, v in data.get("tasks", {}).items()}
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError) as e:
                log.warning("не удалось прочитать %s: %s", self.seen_path, e)
        return self._seen

    def take_news(self) -> dict[str, Any]:
        tasks = self.store.list()
        marks = {t["id"]: len(t.get("updates") or []) for t in tasks}
        with self._seen_lock:
            seen = self._load_seen()
        if seen is None:
            return {"first": True, "fresh": [], "updated": [], "marks": marks}
        fresh = [t for t in tasks if t["id"] not in seen and t.get("source") != "chat"]
        updated = [{**t, "updates": t["updates"][seen[t["id"]]:]} for t in tasks
                   if t["id"] in seen and marks[t["id"]] > seen[t["id"]]]
        return {"first": False, "fresh": fresh, "updated": updated, "marks": marks}

    def commit_news(self, news: dict[str, Any]) -> None:
        with self._seen_lock:
            self._seen = dict(news["marks"])
            if self.seen_path is None:
                return
            path = Path(self.seen_path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"at": time.time(), "tasks": self._seen}, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)
            except OSError as e:
                log.warning("не удалось сохранить %s: %s", path, e)

    def snapshot(self, news: dict[str, Any] | None = None, limit: int = 60) -> str:
        tasks = self.store.list()
        active = [t for t in tasks if t["status"] != "done"]
        active.sort(key=lambda t: (t.get("due_date") or "9999-99-99", t.get("received") or ""))
        fresh = news["fresh"] if news else []
        updated = news["updated"] if news else []
        out = []
        if not news or news["first"]:
            out.append("НОВОЕ С ПРОШЛОГО СООБЩЕНИЯ: это первое сообщение — сравнивать не с чем, всё актуальное в списке ниже.")
        elif fresh:
            out.append(f"НОВОЕ С ПРОШЛОГО СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ ({len(fresh)}) — пришло из почты, пока вы не общались:")
            out.extend(self._snapshot_line(t) for t in fresh)
        elif not updated:
            out.append("НОВОЕ С ПРОШЛОГО СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ: ничего нового не пришло.")
        if updated:
            out.append(f"UPD — ОТВЕТЫ ПО СУЩЕСТВУЮЩИМ ЗАДАЧАМ С ПРОШЛОГО СООБЩЕНИЯ ({len(updated)}):")
            out.extend(self._snapshot_line(t) for t in updated)
        out.append("")
        out.append(f"ВСЕ АКТИВНЫЕ ЗАДАЧИ ({len(active)}; в «Готово» ещё {len(tasks) - len(active)}):")
        if active:
            out.extend(self._snapshot_line(t) for t in active[:limit])
        else:
            out.append("активных задач нет")
        if len(active) > limit:
            out.append(f"…и ещё {len(active) - limit} — используй list_tasks")
        return "\n".join(out)

    def propose_delete(self, task_id: str, reason: str = "") -> dict[str, Any]:
        task = self._find(task_id)
        if task is None:
            return {"error": f"задача {task_id} не найдена"}
        return self._offer({"action": "delete", "task_id": task["id"], "title": task.get("title") or task.get("subject"),
                            "status": task.get("status"), "reason": reason})


class ChatService:
    def __init__(self, spec: str | None, options: dict[str, Any], tools: BacklogTools, memory: MemoryStore | None = None):
        self.spec = spec
        self.options = options
        self.tools = tools
        self.memory = memory
        self.keeper: MemoryKeeper | None = None
        self.agent: Any = None
        self.error: str | None = None if spec else "чат не настроен: добавь секцию [chat] в конфиг"
        self.ready = threading.Event()
        if not spec:
            self.ready.set()

    def start(self) -> None:
        if self.spec:
            threading.Thread(target=self._load, name="chat-init", daemon=True).start()

    def _load(self) -> None:
        try:
            module_name, _, attr = self.spec.partition(":")
            cls = getattr(importlib.import_module(module_name), attr)
            agent = cls(**self.options)
            agent.setup(self.tools)
            self.agent = agent
            if self.memory is not None and hasattr(agent, "update_memory"):
                self.keeper = MemoryKeeper(self.memory, agent.update_memory)
            log.info("чат с агентом готов (%s)", self.spec)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.warning("чат выключен: %s", self.error)
        finally:
            self.ready.set()

    def info(self) -> dict[str, Any]:
        return {"enabled": self.agent is not None, "loading": not self.ready.is_set(), "error": self.error}

    def stream(self, thread_id: str, message: str) -> Iterator[dict[str, Any]]:
        self.ready.wait(timeout=30)
        if self.agent is None:
            yield {"type": "error", "text": self.error or "чат не готов"}
            return
        parts: list[str] = []
        tools: list[str] = []
        proposals: list[str] = []
        error = False
        started = time.monotonic()
        events = self.tools.events
        if events is not None:
            events.add("chat_user", text=message[:2000], chars=len(message))
        try:
            for event in self.agent.stream(thread_id, message):
                kind = event.get("type")
                if kind == "token":
                    parts.append(event.get("text") or "")
                elif kind == "tool":
                    tools.append(event.get("name") or "")
                elif kind in ("proposal", "applied"):
                    proposals.append(event.get("action") or "")
                    if kind == "applied" and events is not None:
                        events.add("proposal_applied", action=event.get("action"), auto=True)
                elif kind == "error":
                    error = True
                yield event
        finally:
            if events is not None:
                events.add("chat_reply", chars=len("".join(parts)), tools=tools, proposals=proposals, error=error,
                           seconds=round(time.monotonic() - started, 1))
            if self.keeper is not None:
                self.keeper.observe_chat(message, "".join(parts))

    def observe_activity(self) -> None:
        if self.keeper is not None:
            self.keeper.observe_activity()

    def reset_memory(self) -> None:
        if self.keeper is not None:
            self.keeper.stop()
            self.keeper = MemoryKeeper(self.memory, self.agent.update_memory)

    def stop(self) -> None:
        if self.keeper is not None:
            self.keeper.stop()
