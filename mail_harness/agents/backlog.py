from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from ..agent import AgentContext
from ..models import Action, IncomingEmail, Move, Reply, SaveAttachments, Skip, WriteFile
from ..runner import sanitize_filename

DEFAULT_TEMPLATE = (
    "Здравствуйте!\n\n"
    "{owner} получил сообщение. Кинул Ваш запрос ему в бэклог.\n\n"
    "Сообщение создано автоматически."
)


class BacklogAgent:
    def __init__(
        self,
        owner: str = "Егор Иванович",
        extensions: list[str] | tuple[str, ...] = (".xlsx", ".xlsm", ".xls", ".csv"),
        template: str = DEFAULT_TEMPLATE,
        backlog_dir: str = "backlog",
        only_direct: bool = True,
        reply_all: bool = False,
        quote_original: bool = True,
        move_to: str | None = None,
    ):
        self.owner = owner
        self.extensions = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in extensions)
        self.template = template
        self.backlog_dir = backlog_dir
        self.only_direct = only_direct
        self.reply_all = reply_all
        self.quote_original = quote_original
        self.move_to = move_to

    def handle(self, email: IncomingEmail, ctx: AgentContext) -> list[Action]:
        files = [a for a in email.attachments if not a.inline and a.extension in self.extensions]
        if not files:
            return [Skip("нет подходящих вложений")]
        if self.only_direct and not ctx.is_addressed_to_me(email):
            return [Skip("я не в поле «Кому»")]

        received = email.date or datetime.now().astimezone()
        folder = f"{self.backlog_dir}/{received:%Y-%m-%d_%H%M}_{_slug(email.subject)}_{task_suffix(email)}"
        task = {
            "title": email.subject or "(без темы)",
            "description": email.text,
            "from": email.sender.email if email.sender else None,
            "from_name": email.sender.name if email.sender else None,
            "received": received.isoformat(),
            "message_id": email.message_id,
            "references": list(email.references),
            "reply_to": str(email.reply_to[0]) if email.reply_to else None,
            "files": [sanitize_filename(a.filename) for a in files],
            "status": "new",
        }
        actions: list[Action] = [
            SaveAttachments(folder, extensions=self.extensions),
            WriteFile(f"{folder}/task.json", json.dumps(task, ensure_ascii=False, indent=2)),
            Reply(
                body=self.template.format(owner=self.owner, subject=email.subject),
                reply_all=self.reply_all,
                quote_original=self.quote_original,
            ),
        ]
        if self.move_to:
            actions.append(Move(self.move_to))
        return actions


def task_suffix(email: IncomingEmail) -> str:
    key = email.message_id or f"{email.uid}:{email.subject}:{email.date}:{len(email.raw)}"
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:8]


def _slug(text: str, max_len: int = 40) -> str:
    text = re.sub(r"^\s*((re|fw|fwd|ответ|пересл)\s*:\s*)+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:max_len].rstrip("_") or "task"
