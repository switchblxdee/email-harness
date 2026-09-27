from __future__ import annotations

import html as html_lib
import re
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from .models import Address, Attachment, IncomingEmail

_MSGID_RE = re.compile(r"<[^<>\s]+>")


def parse_message(raw: bytes, uid: int = 0) -> IncomingEmail:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)

    headers: dict[str, list[str]] = {}
    for name, value in msg.items():
        headers.setdefault(name.lower(), []).append(_header_str(value))

    def first(name: str) -> str | None:
        values = headers.get(name.lower())
        return values[0] if values else None

    sender_list = _addresses(headers.get("from", []))
    references = tuple(_MSGID_RE.findall(first("references") or ""))
    in_reply_to_ids = _MSGID_RE.findall(first("in-reply-to") or "")
    message_ids = _MSGID_RE.findall(first("message-id") or "")

    text, html = _bodies(msg)
    if not text and html:
        text = html_to_text(html)

    return IncomingEmail(
        uid=uid,
        message_id=message_ids[0] if message_ids else None,
        subject=(first("subject") or "").strip(),
        sender=sender_list[0] if sender_list else None,
        reply_to=_addresses(headers.get("reply-to", [])),
        to=_addresses(headers.get("to", [])),
        cc=_addresses(headers.get("cc", [])),
        date=_date(first("date")),
        text=text.strip(),
        html=html,
        attachments=tuple(_attachments(msg)),
        in_reply_to=in_reply_to_ids[0] if in_reply_to_ids else None,
        references=references,
        headers={k: tuple(v) for k, v in headers.items()},
        raw=raw,
    )


def _header_str(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return repr(value)


def _addresses(values: list[str]) -> tuple[Address, ...]:
    result = []
    for name, addr in getaddresses(values):
        addr = addr.strip().lower()
        if "@" in addr:
            result.append(Address(name=name.strip(), email=addr))
    return tuple(result)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _bodies(msg: EmailMessage) -> tuple[str, str | None]:
    text = ""
    html = None
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        text = _part_text(plain_part)
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        html = _part_text(html_part)
    return text, html


def _part_text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except (LookupError, UnicodeError, KeyError):
        pass
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _attachments(msg: EmailMessage) -> list[Attachment]:
    body_parts = {id(p) for p in (msg.get_body(("plain",)), msg.get_body(("html",))) if p is not None}
    result: list[Attachment] = []

    def visit(part: EmailMessage) -> None:
        ctype = part.get_content_type()
        if part.is_multipart():
            for sub in part.iter_parts():
                visit(sub)
            return
        if id(part) in body_parts:
            return
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if ctype == "message/rfc822":
            inner = part.get_content()
            data = inner.as_bytes() if hasattr(inner, "as_bytes") else bytes(inner)
            result.append(Attachment(filename or "attached.eml", ctype, data))
            return
        if not filename and disposition != "attachment":
            return
        data = part.get_payload(decode=True) or b""
        result.append(
            Attachment(
                filename=filename or f"attachment{len(result) + 1}",
                content_type=ctype,
                data=data,
                inline=disposition == "inline" or bool(part.get("Content-ID") and disposition != "attachment"),
            )
        )

    if msg.is_multipart():
        visit(msg)
    return result


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
    _SKIP = {"style", "script", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html))
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


_QUOTE_HEADERS = re.compile(
    r"^\s*(-{2,}\s*(Исходное сообщение|Original Message|Пересылаемое сообщение|Forwarded message)\s*-{2,}"
    r"|(От|From)\s*:.*$"
    r"|.{3,160}\s(wrote|пишет|написала?|написал\(а\))\s*:\s*$"
    r"|.{3,160}<[^<>@\s]+@[^<>\s]+>\s*:\s*$"
    r"|_{10,})",
    re.IGNORECASE,
)


def split_quoted(text: str) -> tuple[str, str]:
    lines = (text or "").replace("\r", "").split("\n")
    out, quoted = [], []
    for i, line in enumerate(lines):
        if _QUOTE_HEADERS.match(line) and (i + 1 < len(lines) or out):
            quoted.extend(lines[i:])
            break
        if line.lstrip().startswith(">"):
            quoted.append(line)
            continue
        out.append(line)
    fresh = "\n".join(out).strip()
    if not fresh:
        return (text or "").strip(), ""
    return fresh, "\n".join(quoted).strip()


def strip_quoted(text: str) -> str:
    return split_quoted(text)[0]
