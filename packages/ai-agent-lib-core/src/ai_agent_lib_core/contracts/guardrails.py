"""Guardrails: checks on what goes into and comes out of models and tools."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from ai_agent_lib_core.contracts._validation import require_identifier
from ai_agent_lib_core.contracts.identity import RequestContext

__all__ = ["GuardrailCheck", "GuardrailFinding", "GuardrailPoint", "GuardrailVerdict"]


class GuardrailPoint(enum.StrEnum):
    """Where in a call some text is checked."""

    MODEL_INPUT = "model.input"
    MODEL_OUTPUT = "model.output"
    TOOL_INPUT = "tool.input"
    TOOL_RESULT = "tool.result"


@dataclass(frozen=True, slots=True)
class GuardrailFinding:
    """Something a check noticed.

    A finding names what was found, never the text that was found.

    Attributes:
        code: What was found, for example ``pii.email`` or ``size``.
        count: How many times.
        blocked: Whether this finding stops the call.
    """

    code: str
    count: int = 1
    blocked: bool = False

    def __post_init__(self) -> None:
        require_identifier("code", self.code)
        if self.count < 1:
            raise ValueError("count must be at least 1")


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    """The outcome of checking one piece of text.

    Attributes:
        findings: Everything the checks noticed.
    """

    findings: tuple[GuardrailFinding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))

    @property
    def allow(self) -> bool:
        """Whether the call may continue: no finding blocks it."""
        return not any(finding.blocked for finding in self.findings)

    @property
    def blocking(self) -> GuardrailFinding | None:
        """The first finding that blocks the call, if any."""
        return next((finding for finding in self.findings if finding.blocked), None)

    def summary(self) -> str:
        """Return the findings as ``code:count`` pairs, for the audit record."""
        return ",".join(f"{f.code}:{f.count}" for f in sorted(self.findings, key=lambda f: f.code))


class GuardrailCheck(Protocol):
    """Checks text at one point of a model or tool call.

    A check that cannot reach a verdict raises. The pipeline then stops the
    call; it never continues without a verdict.
    """

    async def check(
        self, point: GuardrailPoint, text: str, context: RequestContext | None
    ) -> GuardrailVerdict:
        """Return what the checks found in ``text``."""
        ...
