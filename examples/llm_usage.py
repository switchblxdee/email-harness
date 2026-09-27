from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from mail_harness.stats import EventLog


class UsageCallback(BaseCallbackHandler):
    def __init__(self, log: EventLog, source: str, model: str | None = None):
        self.log = log
        self.source = source
        self.model = model

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        meta = response.llm_output or {}
        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if usage:
                    details = usage.get("input_token_details") or {}
                    model = self.model or (getattr(message, "response_metadata", None) or {}).get("model_name") or meta.get("model_name")
                    self.log.usage(self.source, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0), details.get("cache_read") or 0)
                    return
        token_usage = meta.get("token_usage") or {}
        if token_usage:
            self.log.usage(self.source, self.model or meta.get("model_name"), token_usage.get("prompt_tokens", 0), token_usage.get("completion_tokens", 0),
                           (token_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
