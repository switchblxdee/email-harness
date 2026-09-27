from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from .models import Action, IncomingEmail


@dataclass(frozen=True)
class AgentContext:
    data_dir: Path
    dry_run: bool
    my_addresses: frozenset[str]
    log: logging.Logger

    def is_addressed_to_me(self, email: IncomingEmail) -> bool:
        return any(a.email in self.my_addresses for a in email.to)


@runtime_checkable
class Agent(Protocol):
    def handle(self, email: IncomingEmail, ctx: AgentContext) -> Iterable[Action] | None: ...


class FunctionAgent:
    def __init__(self, fn: Callable[[IncomingEmail, AgentContext], Iterable[Action] | None]):
        self.fn = fn

    def handle(self, email: IncomingEmail, ctx: AgentContext) -> Iterable[Action] | None:
        return self.fn(email, ctx)


def load_agent(spec: str, options: dict[str, Any] | None = None) -> Agent:
    module_name, sep, attr = spec.partition(":")
    if not sep or not attr:
        raise ValueError(f"агент задаётся как 'module:Object', получено {spec!r}")
    obj = getattr(importlib.import_module(module_name), attr)
    options = options or {}
    if inspect.isclass(obj):
        agent = obj(**options)
    elif hasattr(obj, "handle"):
        agent = obj
    elif callable(obj):
        agent = FunctionAgent(obj)
    else:
        raise TypeError(f"{spec} не класс, не агент и не функция")
    if options and not inspect.isclass(obj):
        raise ValueError(f"agent.options заданы, но {spec} не класс — их некуда передать")
    return agent
