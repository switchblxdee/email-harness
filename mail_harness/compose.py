from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .models import Address, IncomingEmail, Reply

_RE_PREFIX = re.compile(r"^\s*(re|ответ)\s*:", re.IGNORECASE)


def reply_subject(subject: str) -> str:
    if not subject.strip():
        return "RE: (без темы)"
    return subject if _RE_PREFIX.match(subject) else f"RE: {subject}"


def reply_recipients(
    original: IncomingEmail, reply_all: bool, my_addresses: frozenset[str]
) -> tuple[tuple[Address, ...], tuple[Address, ...]]:
    primary = original.reply_to or ((original.sender,) if original.sender else ())
    to = tuple(a for a in primary if a.email not in my_addresses)
    if not reply_all:
        return to, ()
    seen = {a.email for a in to} | set(my_addresses)
    cc = []
    for addr in original.to + original.cc:
        if addr.email not in seen:
            seen.add(addr.email)
            cc.append(addr)
    return to, tuple(cc)


def build_reply(
    original: IncomingEmail,
    action: Reply,
    from_addr: Address,
    to: tuple[Address, ...],
    cc: tuple[Address, ...] = (),
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = str(from_addr)
    msg["To"] = ", ".join(str(a) for a in to)
    if cc:
        msg["Cc"] = ", ".join(str(a) for a in cc)
    msg["Subject"] = action.subject or reply_subject(original.subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.domain or None)
    if original.message_id:
        msg["In-Reply-To"] = original.message_id
        msg["References"] = " ".join((*original.references, original.message_id))
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"

    body = action.body
    if action.quote_original:
        body = body.rstrip() + "\n\n" + quote(original)
    msg.set_content(body)
    if action.html:
        msg.add_alternative(action.html, subtype="html")
    for att in action.attachments:
        maintype, _, subtype = att.content_type.partition("/")
        msg.add_attachment(
            att.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    return msg


def quote(original: IncomingEmail) -> str:
    lines = ["-----Исходное сообщение-----"]
    if original.sender:
        lines.append(f"От: {original.sender}")
    if original.date:
        lines.append(f"Отправлено: {original.date:%d.%m.%Y %H:%M}")
    if original.to:
        lines.append("Кому: " + "; ".join(str(a) for a in original.to))
    lines.append(f"Тема: {original.subject}")
    lines.append("")
    lines.extend("> " + line if line else ">" for line in original.text.splitlines())
    return "\n".join(lines)
