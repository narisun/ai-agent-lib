"""Every guardrail check passes the same contract suite."""

from __future__ import annotations

from ai_agent_lib_core.adapters import PatternGuardrails
from ai_agent_lib_core.contracts import GuardrailCheck
from ai_agent_lib_core.testing import FakeGuardrails
from ai_agent_lib_core.testing.contracts import GuardrailCheckContract


class TestPatternGuardrails(GuardrailCheckContract):
    def make_check(self) -> GuardrailCheck:
        return PatternGuardrails()


class TestFakeGuardrails(GuardrailCheckContract):
    def make_check(self) -> GuardrailCheck:
        return FakeGuardrails()
