from __future__ import annotations

import asyncio
import importlib
from typing import Any

from ..agent import AgentContext
from ..models import ACTION_TYPES, Action, IncomingEmail, Reply, Skip, action_from_dict

DEFAULT_PROMPT = """Тебе пришло рабочее письмо. Напиши ответ на него от моего имени.
Если отвечать не нужно, ответь одним словом {skip_token}.

От: {sender}
Кому: {to}
Копия: {cc}
Дата: {date}
Тема: {subject}
Вложения: {attachments}

{text}"""


class LangGraphAgent:
    def __init__(
        self,
        graph: str,
        mode: str = "messages",
        prompt: str = DEFAULT_PROMPT,
        system_prompt: str | None = None,
        skip_token: str = "SKIP",
        reply_all: bool = False,
        quote_original: bool = True,
        input_key: str = "email",
        output_key: str = "actions",
        include_attachments: bool = False,
        use_async: bool = False,
        recursion_limit: int = 25,
        use_thread_id: bool = True,
        config: dict[str, Any] | None = None,
    ):
        if mode not in ("messages", "state"):
            raise ValueError("mode должен быть 'messages' или 'state'")
        self.graph_spec = graph
        self.mode = mode
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.skip_token = skip_token
        self.reply_all = reply_all
        self.quote_original = quote_original
        self.input_key = input_key
        self.output_key = output_key
        self.include_attachments = include_attachments
        self.use_async = use_async
        self.recursion_limit = recursion_limit
        self.use_thread_id = use_thread_id
        self.extra_config = config or {}
        self.graph: Any = None

    def setup(self, ctx: AgentContext) -> None:
        if self.graph is None:
            self.graph = _load_graph(self.graph_spec)

    def handle(self, email: IncomingEmail, ctx: AgentContext) -> list[Action]:
        if self.graph is None:
            self.setup(ctx)
        config = self._config(email)
        if self.mode == "messages":
            state = self._invoke(self._messages_input(email), config)
            return self._actions_from_messages(state)
        payload = email.to_dict(include_attachment_data=self.include_attachments)
        state = self._invoke({self.input_key: payload}, config)
        return self._actions_from_state(state)


    def _config(self, email: IncomingEmail) -> dict[str, Any]:
        config: dict[str, Any] = {"recursion_limit": self.recursion_limit, **self.extra_config}
        if self.use_thread_id and email.thread_id:
            configurable = dict(config.get("configurable", {}))
            configurable.setdefault("thread_id", email.thread_id)
            config["configurable"] = configurable
        return config

    def _invoke(self, inputs: dict[str, Any], config: dict[str, Any]) -> Any:
        if self.use_async:
            return asyncio.run(self.graph.ainvoke(inputs, config=config))
        return self.graph.invoke(inputs, config=config)


    def _messages_input(self, email: IncomingEmail) -> dict[str, Any]:
        text = self.prompt.format(
            skip_token=self.skip_token,
            sender=email.sender or "-",
            to=", ".join(map(str, email.to)) or "-",
            cc=", ".join(map(str, email.cc)) or "-",
            date=f"{email.date:%d.%m.%Y %H:%M}" if email.date else "-",
            subject=email.subject or "(без темы)",
            attachments=", ".join(a.filename for a in email.attachments if not a.inline) or "нет",
            text=email.text,
        )
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": text})
        return {"messages": messages}

    def _actions_from_messages(self, state: Any) -> list[Action]:
        messages = _get(state, "messages") or []
        answer = ""
        for msg in reversed(messages):
            if _role(msg) in ("ai", "assistant"):
                answer = _text(_get(msg, "content")).strip()
                if answer:
                    break
        if not answer:
            return [Skip("граф не вернул ответа")]
        if answer.upper().startswith(self.skip_token.upper()):
            return [Skip(f"агент решил не отвечать: {answer[:200]}")]
        return [Reply(answer, reply_all=self.reply_all, quote_original=self.quote_original)]


    def _actions_from_state(self, state: Any) -> list[Action]:
        raw = _get(state, self.output_key)
        if raw is None:
            raise ValueError(f"в выходном состоянии графа нет ключа {self.output_key!r}")
        actions: list[Action] = []
        for item in raw:
            if isinstance(item, ACTION_TYPES):
                actions.append(item)
            elif isinstance(item, dict):
                actions.append(action_from_dict(item))
            elif hasattr(item, "model_dump"):
                actions.append(action_from_dict(item.model_dump()))
            else:
                raise TypeError(f"не понимаю действие из графа: {item!r}")
        return actions


def _load_graph(spec: str) -> Any:
    module_name, sep, attr = spec.partition(":")
    if not sep:
        raise ValueError(f"граф задаётся как 'module:attr', получено {spec!r}")
    obj = getattr(importlib.import_module(module_name), attr)
    if not hasattr(obj, "invoke") and callable(obj):
        obj = obj()
    if not hasattr(obj, "invoke"):
        raise TypeError(f"{spec} не граф LangGraph (нет .invoke)")
    return obj


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "")
    return str(getattr(msg, "type", ""))


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return "" if content is None else str(content)
