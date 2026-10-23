"""OpenInference attributes understood by Phoenix, without an SDK dependency."""

import json
from collections.abc import Mapping
from typing import Any

from opentelemetry.trace import Span

from ai_agent_lib_core.contracts import RequestContext
from ai_agent_lib_core.contracts.diagnostics import TokenUsage


class PhoenixSchema:
    """Describe actual provider attempts and tools using OpenInference conventions."""

    def attributes(self, session: str, context: RequestContext) -> dict[str, str]:
        """Hash identity into the session key and retain request/checkpoint correlation."""
        return {
            "session.id": session,
            "agentcraft2.request_id": context.request_id,
            "agentcraft2.thread_id": context.thread_id,
        }

    def kind(self, span: Span, kind: str) -> None:
        """Classify spans for the Phoenix timeline."""
        span.set_attribute(
            "openinference.span.kind", {"agent": "AGENT", "generation": "LLM", "tool": "TOOL"}[kind]
        )

    def metadata(self, span: Span, name: str, value: str | int | bool) -> None:
        """Keep checkpoint IDs and MCP server names searchable as span attributes."""
        span.set_attribute(f"agentcraft2.{name}", value)

    def content(self, span: Span, kind: str, text: str) -> None:
        """Keep complete bounded JSON plus structured chat messages for LLM rendering."""
        span.set_attribute(f"{kind}.value", text)
        span.set_attribute(f"{kind}.mime_type", "application/json")
        value = json.loads(text)
        if not isinstance(value, dict) or value.get("truncated"):
            return
        if kind == "input" and isinstance(value.get("messages"), list):
            self._messages(span, kind, value["messages"])
        elif kind == "output" and value.get("type") == "ai":
            self._messages(span, kind, [value])

    @staticmethod
    def _messages(span: Span, kind: str, messages: list[Any]) -> None:
        # Raw input/output above remains authoritative if a long conversation is
        # too large for structured OTel attributes. Keep the most recent messages.
        attributes: dict[str, str] = {}
        truncated = len(messages) > 24
        for index, message in enumerate(messages[-24:]):
            if not isinstance(message, dict):
                continue
            if len(attributes) > 90:
                truncated = True
                break
            prefix = f"llm.{kind}_messages.{index}.message"
            role = str(message.get("role") or message.get("type", "user"))
            attributes[prefix + ".role"] = {"human": "user", "ai": "assistant"}.get(role, role)
            content = message.get("content", "")
            attributes[prefix + ".content"] = (
                content if isinstance(content, str) else json.dumps(content)
            )
            if call_id := message.get("tool_call_id"):
                attributes[prefix + ".tool_call_id"] = str(call_id)
            calls = message.get("tool_calls") or []
            for number, call in enumerate(calls):
                if len(attributes) > 90:
                    truncated = True
                    break
                if not isinstance(call, dict):
                    continue
                tool = f"{prefix}.tool_calls.{number}.tool_call"
                attributes[tool + ".id"] = str(call.get("id", ""))
                attributes[tool + ".function.name"] = str(call.get("name", ""))
                attributes[tool + ".function.arguments"] = json.dumps(call.get("args", {}))
        span.set_attributes(attributes)
        if truncated:
            span.set_attribute(f"agentcraft2.{kind}_messages_truncated", True)

    def model(self, span: Span, record: Mapping[str, object]) -> None:
        """Record the provider and model returned by the governed binding."""
        span.set_attribute("llm.model_name", str(record["model_id"]))
        span.set_attribute("llm.provider", str(record["model_provider"]))

    def usage(self, span: Span, usage: TokenUsage) -> None:
        """Prompt totals include cache reads/writes; missing usage stays unknown."""
        fields = {
            "prompt": usage.input_tokens,
            "completion": usage.output_tokens,
            "total": usage.input_tokens + usage.output_tokens
            if usage.input_tokens is not None and usage.output_tokens is not None
            else None,
            "prompt_details.cache_read": usage.cache_read_tokens,
            "prompt_details.cache_write": usage.cache_write_tokens,
        }
        for name, value in fields.items():
            if value is not None:
                span.set_attribute(f"llm.token_count.{name}", value)
