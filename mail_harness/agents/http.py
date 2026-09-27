from __future__ import annotations

import json
import os
import urllib.request

from ..agent import AgentContext
from ..models import Action, IncomingEmail, action_from_dict


class HttpAgent:
    def __init__(
        self,
        url: str,
        timeout: float = 120,
        include_attachments: bool = False,
        headers: dict[str, str] | None = None,
        token_env: str | None = None,
    ):
        self.url = url
        self.timeout = timeout
        self.include_attachments = include_attachments
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        if token_env:
            token = os.environ.get(token_env)
            if not token:
                raise ValueError(f"переменная окружения {token_env} не задана")
            self.headers["Authorization"] = f"Bearer {token}"

    def handle(self, email: IncomingEmail, ctx: AgentContext) -> list[Action]:
        payload = json.dumps(
            {"email": email.to_dict(include_attachment_data=self.include_attachments), "dry_run": ctx.dry_run},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(self.url, data=payload, headers=self.headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [action_from_dict(d) for d in body.get("actions", [])]
