from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from .config import SafetyConfig
from .models import Address, IncomingEmail
from .state import StateStore

_AUTOMATED_LOCALPART = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|bounces?|notifications?|robot)\b",
    re.IGNORECASE,
)
_AUTOREPLY_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|автоматический ответ|автоответ"
    r"|undeliverable|не доставлено|delivery status notification)",
    re.IGNORECASE,
)


def automated_reason(email: IncomingEmail, my_addresses: frozenset[str]) -> str | None:
    if email.sender is None:
        return "нет отправителя"
    if email.sender.email in my_addresses:
        return "письмо от меня самого"

    auto_submitted = (email.header("Auto-Submitted") or "no").strip().lower()
    if auto_submitted != "no":
        return f"Auto-Submitted: {auto_submitted}"

    suppress = (email.header("X-Auto-Response-Suppress") or "").lower()
    if any(token in suppress for token in ("all", "autoreply", "oof")):
        return f"X-Auto-Response-Suppress: {suppress}"

    precedence = (email.header("Precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk", "auto_reply"):
        return f"Precedence: {precedence}"

    for header in ("List-Id", "List-Unsubscribe", "X-Autoreply", "X-Autorespond", "X-Auto-Reply"):
        if email.header(header):
            return f"есть заголовок {header}"

    if (email.header("Return-Path") or "").strip() == "<>":
        return "пустой Return-Path (уведомление о доставке)"
    if (email.header("Content-Type") or "").lower().startswith("multipart/report"):
        return "multipart/report (NDR)"
    if _AUTOMATED_LOCALPART.match(email.sender.email.partition("@")[0]):
        return f"служебный адрес {email.sender.email}"
    if _AUTOREPLY_SUBJECT.match(email.subject):
        return "тема похожа на автоответ"
    return None


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""


class ReplyGuard:
    def __init__(self, cfg: SafetyConfig, state: StateStore, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.state = state
        self.clock = clock

    def is_allowed_recipient(self, addr: Address) -> bool:
        if addr.email in self.cfg.my_addresses or addr.email in self.cfg.blocked_senders:
            return False
        if self.cfg.allowed_domains and addr.domain not in self.cfg.allowed_domains:
            return False
        return True

    def check_reply(self, email: IncomingEmail, primary: tuple[Address, ...]) -> Verdict:
        reason = automated_reason(email, self.cfg.my_addresses)
        if reason:
            return Verdict(False, f"автоматическое письмо: {reason}")
        if not primary:
            return Verdict(False, "некому отвечать")
        for addr in primary:
            if not self.is_allowed_recipient(addr):
                return Verdict(False, f"адресат {addr.email} запрещён настройками safety")

        if self.cfg.one_reply_per_thread and email.thread_id and self.state.replied_in_thread(email.thread_id):
            return Verdict(False, "в этой ветке уже был автоответ")

        hour_ago = self.clock() - 3600
        for addr in primary:
            n = self.state.replies_to_since(addr.email, hour_ago)
            if n >= self.cfg.max_replies_per_sender_per_hour:
                return Verdict(False, f"лимит ответов для {addr.email}: {n} за час")
        total = self.state.replies_since(hour_ago)
        if total >= self.cfg.max_replies_per_hour:
            return Verdict(False, f"общий лимит ответов: {total} за час")
        return Verdict(True)
