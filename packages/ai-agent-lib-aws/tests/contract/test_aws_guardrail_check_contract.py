"""The Bedrock guardrails pass the contract suite every guardrail check passes."""

from __future__ import annotations

from ai_agent_lib_aws.guardrails_bedrock import BedrockGuardrails, BedrockGuardrailsOptions
from ai_agent_lib_aws.testing import FakeBedrockGuardrail, offline_sessions
from ai_agent_lib_core.contracts import GuardrailCheck
from ai_agent_lib_core.testing.contracts import GuardrailCheckContract


class TestBedrockGuardrails(GuardrailCheckContract):
    def make_check(self) -> GuardrailCheck:
        return BedrockGuardrails(
            BedrockGuardrailsOptions(guardrail_id="gr1abc", guardrail_version="3"),
            offline_sessions(),
            client=FakeBedrockGuardrail(),
        )
