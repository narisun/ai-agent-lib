"""The requests that travel through the pipelines.

These types are framework-neutral. The LangGraph and MCP bindings translate
their own objects into them, so the interceptors never import a framework.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ai_agent_lib_core.contracts import (
    AuditValue,
    Classification,
    Obligations,
    RequestContext,
    StructuredOutput,
)

__all__ = ["DataCall", "Evidence", "ModelCall", "ToolCall"]

_SHOWN_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_GEN_AI_PROVIDERS = {"bedrock": "aws.bedrock", "anthropic": "anthropic"}
"""Provider names as the OpenTelemetry GenAI conventions spell them."""


def _shown(name: str) -> str:
    """A name as a span may carry it: one a caller sent that is not shaped like a name is not."""
    return name if _SHOWN_NAME.match(name) else "unnamed"


class Evidence:
    """Facts the stages record about one call, for its audit record.

    A stage that learns something worth keeping, such as the policy decision
    ID or the registry revision, adds it here. The audit stage is outermost
    and reads the facts when the call ends, whether it succeeded or not.
    """

    __slots__ = ("_facts",)

    def __init__(self) -> None:
        self._facts: dict[str, AuditValue] = {}

    def add(self, **facts: AuditValue) -> None:
        """Record facts. A later value for the same name replaces the earlier one."""
        self._facts.update(facts)

    def facts(self) -> Mapping[str, AuditValue]:
        """Return a copy of everything recorded so far."""
        return MappingProxyType(dict(self._facts))


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One call to a chat model.

    Attributes:
        context: The request context, or ``None`` when none was supplied. The
            identity step refuses calls without one.
        alias: The logical model name the application asked for.
        provider: The provider that serves the alias.
        model_id: The provider's identifier for the model.
        payload: The messages, opaque to the pipeline.
        text: The text of the messages, for the input guardrails. It never
            reaches the audit record.
        output: The schema the reply must satisfy, if the caller asked for one.
        evidence: Facts the stages record for the audit record.
    """

    context: RequestContext | None
    alias: str
    provider: str
    model_id: str
    payload: object = field(default=None, repr=False)
    text: str = field(default="", repr=False)
    output: StructuredOutput | None = None
    evidence: Evidence = field(default_factory=Evidence, repr=False, compare=False)

    event = "model.call"

    @property
    def span_name(self) -> str:
        """The span's name, as the GenAI conventions name a chat call."""
        return f"chat {_shown(self.model_id)}"

    def span_attributes(self) -> Mapping[str, AuditValue]:
        """The span's attributes, with the GenAI conventions' names."""
        return {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": _GEN_AI_PROVIDERS.get(self.provider, self.provider),
            "gen_ai.request.model": self.model_id,
            "agentlib.model_alias": self.alias,
        }

    def signal_attributes(self) -> Mapping[str, AuditValue]:
        """Return the few, low-cardinality facts that label telemetry."""
        return {
            "model_alias": self.alias,
            "model_provider": self.provider,
            "model_id": self.model_id,
        }

    def audit_attributes(self) -> Mapping[str, AuditValue]:
        """Return metadata about the call. Never the payload."""
        return {**self.signal_attributes(), **self.evidence.facts()}


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One call to a tool.

    Attributes:
        context: The request context, or ``None`` when none was supplied.
        tool: The name of the tool.
        arguments: The arguments, opaque to the audit record.
        read_only: Whether the tool only reads. Only read-only tools are retried.
        payload: Anything else the binding needs to make the call, opaque to
            the pipeline.
        server: The MCP server the tool belongs to, or ``None`` for a tool
            that is a function in the application's own code.
        evidence: Facts the stages record for the audit record.
    """

    context: RequestContext | None
    tool: str
    arguments: Mapping[str, object] = field(default_factory=dict, repr=False)
    read_only: bool = False
    payload: object = field(default=None, repr=False)
    server: str | None = None
    evidence: Evidence = field(default_factory=Evidence, repr=False, compare=False)

    event = "tool.call"

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    @property
    def span_name(self) -> str:
        """The span's name, as the GenAI conventions name a tool call."""
        return f"execute_tool {_shown(self.tool)}"

    def span_attributes(self) -> Mapping[str, AuditValue]:
        """The span's attributes, with the GenAI conventions' names."""
        attributes: dict[str, AuditValue] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": _shown(self.tool),
            "agentlib.read_only": self.read_only,
        }
        if self.server is not None:
            attributes["agentlib.mcp_server"] = self.server
        return attributes

    def signal_attributes(self) -> Mapping[str, AuditValue]:
        """Return the few, low-cardinality facts that label telemetry."""
        signal: dict[str, AuditValue] = {"tool": self.tool, "read_only": self.read_only}
        if self.server is not None:
            signal["mcp_server"] = self.server
        return signal

    def audit_attributes(self) -> Mapping[str, AuditValue]:
        """Return metadata about the call. Never the arguments."""
        return {**self.signal_attributes(), **self.evidence.facts()}


@dataclass(frozen=True, slots=True)
class DataCall:
    """One named query against a data source.

    Attributes:
        context: The request context, or ``None`` when none was supplied.
        source: The name of the data source.
        query: The name of the query.
        parameters: The parameter values, opaque to the audit record.
        classification: How sensitive the query's data is.
        obligations: What the policy attached. Set by the policy stage, never
            by the caller.
        evidence: Facts the stages record for the audit record.
    """

    context: RequestContext | None
    source: str
    query: str
    parameters: Mapping[str, object] = field(default_factory=dict, repr=False)
    classification: Classification = Classification.INTERNAL
    obligations: Obligations = field(default_factory=Obligations)
    evidence: Evidence = field(default_factory=Evidence, repr=False, compare=False)

    event = "data.query"

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @property
    def span_name(self) -> str:
        """The span's name: the named query on its data source."""
        return f"query {self.source}.{self.query}"

    def span_attributes(self) -> Mapping[str, AuditValue]:
        """The span's attributes. Never the parameter values."""
        return {
            "db.operation.name": "query",
            "db.query.summary": f"{self.source}.{self.query}",
            "agentlib.data_source": self.source,
            "agentlib.query": self.query,
            "agentlib.classification": self.classification.name.lower(),
        }

    def signal_attributes(self) -> Mapping[str, AuditValue]:
        """Return the few, low-cardinality facts that label telemetry."""
        return {"data_source": self.source, "query": self.query}

    def audit_attributes(self) -> Mapping[str, AuditValue]:
        """Return metadata about the call. Never the parameter values."""
        return {
            **self.signal_attributes(),
            "classification": self.classification.name.lower(),
            **self.evidence.facts(),
        }
