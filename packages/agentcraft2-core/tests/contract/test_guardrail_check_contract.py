"""Every guardrail check passes the same contract suite."""

from __future__ import annotations

from agentcraft2_core.adapters import PatternGuardrails
from agentcraft2_core.contracts import GuardrailCheck
from agentcraft2_core.testing import FakeGuardrails
from agentcraft2_core.testing.contracts import GuardrailCheckContract


class TestPatternGuardrails(GuardrailCheckContract):
    def make_check(self) -> GuardrailCheck:
        return PatternGuardrails()


class TestFakeGuardrails(GuardrailCheckContract):
    def make_check(self) -> GuardrailCheck:
        return FakeGuardrails()
