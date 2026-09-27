from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from email.headerregistry import Address as _HeaderAddress
from pathlib import PurePath
from typing import Any, Mapping, Union


@dataclass(frozen=True)
class Address:
    name: str
    email: str

    @property
    def domain(self) -> str:
        return self.email.rpartition("@")[2]

    def __str__(self) -> str:
        if not self.name:
            return self.email
        try:
            return str(_HeaderAddress(display_name=self.name, addr_spec=self.email))
        except (ValueError, IndexError):
            return f"{self.name} <{self.email}>"


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes = field(repr=False)
    inline: bool = False

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def extension(self) -> str:
        return PurePath(self.filename).suffix.lower()

    def to_dict(self, include_data: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "inline": self.inline,
        }
        if include_data:
            d["data_base64"] = base64.b64encode(self.data).decode("ascii")
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Attachment:
        return cls(
            filename=d["filename"],
            content_type=d.get("content_type", "application/octet-stream"),
            data=base64.b64decode(d["data_base64"]),
            inline=bool(d.get("inline", False)),
        )


@dataclass(frozen=True)
class IncomingEmail:
    uid: int
    message_id: str | None
    subject: str
    sender: Address | None
    reply_to: tuple[Address, ...]
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    date: datetime | None
    text: str
    html: str | None
    attachments: tuple[Attachment, ...]
    in_reply_to: str | None
    references: tuple[str, ...]
    headers: Mapping[str, tuple[str, ...]] = field(repr=False)
    raw: bytes = field(repr=False)

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None

    @property
    def thread_id(self) -> str | None:
        if self.references:
            return self.references[0]
        return self.in_reply_to or self.message_id

    def to_dict(self, include_attachment_data: bool = False) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "from": _addr_dict(self.sender) if self.sender else None,
            "reply_to": [_addr_dict(a) for a in self.reply_to],
            "to": [_addr_dict(a) for a in self.to],
            "cc": [_addr_dict(a) for a in self.cc],
            "date": self.date.isoformat() if self.date else None,
            "text": self.text,
            "html": self.html,
            "attachments": [a.to_dict(include_attachment_data) for a in self.attachments],
        }


def _addr_dict(a: Address) -> dict[str, str]:
    return {"name": a.name, "email": a.email}


@dataclass(frozen=True)
class Reply:
    body: str
    html: str | None = None
    reply_all: bool = False
    quote_original: bool = True
    subject: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class SaveAttachments:
    directory: str
    extensions: tuple[str, ...] = ()
    include_inline: bool = False


@dataclass(frozen=True)
class WriteFile:
    path: str
    content: str | bytes


@dataclass(frozen=True)
class Move:
    folder: str


@dataclass(frozen=True)
class Flag:
    flag: str = "\\Flagged"


@dataclass(frozen=True)
class Skip:
    reason: str


Action = Union[Reply, SaveAttachments, WriteFile, Move, Flag, Skip]
ACTION_TYPES = (Reply, SaveAttachments, WriteFile, Move, Flag, Skip)


def action_from_dict(d: Mapping[str, Any]) -> Action:
    kind = d.get("type")
    if kind == "reply":
        return Reply(
            body=d["body"],
            html=d.get("html"),
            reply_all=bool(d.get("reply_all", False)),
            quote_original=bool(d.get("quote_original", True)),
            subject=d.get("subject"),
            attachments=tuple(Attachment.from_dict(a) for a in d.get("attachments", ())),
        )
    if kind == "save_attachments":
        return SaveAttachments(
            directory=d["directory"],
            extensions=tuple(e.lower() for e in d.get("extensions", ())),
            include_inline=bool(d.get("include_inline", False)),
        )
    if kind == "write_file":
        return WriteFile(path=d["path"], content=d["content"])
    if kind == "move":
        return Move(folder=d["folder"])
    if kind == "flag":
        return Flag(flag=d.get("flag", "\\Flagged"))
    if kind == "skip":
        return Skip(reason=d.get("reason", ""))
    raise ValueError(f"неизвестный тип действия: {kind!r}")
