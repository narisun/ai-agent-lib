"""The Bedrock model provider: a configuration change away from the local agent."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from botocore import exceptions as aws
from botocore.stub import Stubber
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage

from ai_agent_lib_aws.model_bedrock import BedrockChatModelProvider
from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_aws.testing import offline_sessions
from ai_agent_lib_core import Principal, RequestContext, bind_request_context
from ai_agent_lib_core.config import ConfigResolver, MappingConfigSource
from ai_agent_lib_core.contracts import (
    AuditOutcome,
    ConfigurationError,
    CredentialsExpiredError,
    ProviderSelection,
    Section,
    TransientError,
)
from ai_agent_lib_core.di import ServiceContainer, ServiceProviders
from ai_agent_lib_core.testing import Fakes

MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
CONTEXT = RequestContext(
    principal=Principal(subject="u-1", tenant="t-9"),
    application="accounts-agent",
    request_id="r-1",
    thread_id="th-1",
)


def reply(text: str) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15},
        "metrics": {"latencyMs": 40},
    }


def container(sessions: AwsSessionFactory, fakes: Fakes) -> ServiceContainer:
    """A service configured for Bedrock by variables alone, with everything else faked."""
    resolved = ConfigResolver(
        MappingConfigSource(
            {
                "EAP_MODEL_PROVIDER": "bedrock",
                "EAP_MODEL_ID": MODEL,
                "AWS_REGION": "eu-west-1",
                # One attempt: these tests are about how an error is reported.
                "EAP_LIMITS": '{"model": {"retries": 0}}',
            }
        )
    ).resolve()
    config = resolved
    for section in Section:
        config = config.with_section(section, ProviderSelection("fake"))
    providers = register_aws_adapters(fakes.providers(), sessions=sessions)
    return ServiceContainer(config, providers, clock=fakes.clock, ids=fakes.ids)


def test_a_model_is_built_over_the_injected_clients() -> None:
    sessions = offline_sessions()
    model = BedrockChatModelProvider(sessions).create(MODEL)
    assert isinstance(model, ChatBedrockConverse)
    assert model.client is sessions.client("bedrock-runtime", retried_by_pipeline=True)
    assert model.bedrock_client is sessions.client("bedrock")
    assert model.model_id == MODEL


async def test_the_governed_model_answers_through_bedrock_and_is_audited() -> None:
    sessions, fakes = offline_sessions(), Fakes()
    with Stubber(sessions.client("bedrock-runtime", retried_by_pipeline=True)) as bedrock:
        bedrock.add_response("converse", reply("The balance is 1,250.00 USD."))
        async with container(sessions, fakes) as services:
            with bind_request_context(CONTEXT):
                answer = await services.model().ainvoke([HumanMessage("a confidential question")])
        bedrock.assert_no_pending_responses()

    assert isinstance(answer, AIMessage)
    assert answer.content == "The balance is 1,250.00 USD."
    (record,) = fakes.audit.records
    assert (record.event, record.outcome) == ("model.call", AuditOutcome.SUCCESS)
    assert record.attributes["model_provider"] == "bedrock"
    assert record.attributes["model_id"] == MODEL
    assert (record.attributes["input_tokens"], record.attributes["output_tokens"]) == (12, 3)
    assert "confidential" not in str(record.to_dict())


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("ThrottlingException", 429, TransientError),
        ("ExpiredTokenException", 403, CredentialsExpiredError),
        ("AccessDeniedException", 403, ConfigurationError),
    ],
)
async def test_a_failed_call_surfaces_as_the_librarys_own_error(
    code: str, status: int, expected: type[Exception]
) -> None:
    sessions, fakes = offline_sessions(profile=None, max_attempts=1), Fakes()
    with Stubber(sessions.client("bedrock-runtime", retried_by_pipeline=True)) as bedrock:
        bedrock.add_client_error(
            "converse", code, "the prompt was: a confidential question", http_status_code=status
        )
        async with container(sessions, fakes) as services:
            with bind_request_context(CONTEXT), pytest.raises(expected) as caught:
                await services.model().ainvoke([HumanMessage("a confidential question")])
    assert "confidential" not in str(caught.value)
    assert fakes.audit.records[0].outcome is AuditOutcome.FAILED
    assert fakes.audit.records[0].error_type == expected.__name__


async def test_an_expired_sso_sign_in_says_how_to_renew_it() -> None:
    provider = BedrockChatModelProvider(
        AwsSessionFactory(
            profile="dev-sso",
            region="eu-west-1",
            session_factory=lambda **_: offline_sessions()._session,
        )
    )
    mapped = provider.classify_error(aws.UnauthorizedSSOTokenError())
    assert isinstance(mapped, CredentialsExpiredError)
    assert mapped.fix_command == "aws sso login --profile dev-sso"


def test_the_pack_is_what_a_default_registry_loads() -> None:
    providers = ServiceProviders.default()
    assert "bedrock" in providers.names("model")
    assert providers.lookup("model", "bedrock").local_only is False


def test_a_missing_library_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "langchain_aws", None)
    with pytest.raises(ConfigurationError, match=r"ai-agent-lib-aws\[bedrock\]"):
        BedrockChatModelProvider(offline_sessions())


def test_r17_the_model_client_leaves_retries_to_the_pipeline() -> None:
    sessions = offline_sessions(profile=None)
    BedrockChatModelProvider(sessions)
    runtime = sessions.client("bedrock-runtime", retried_by_pipeline=True)
    assert runtime.meta.config.retries["total_max_attempts"] == 1
    # Clients outside the model pipeline keep the SDK's own retries.
    assert sessions.client("s3").meta.config.retries["total_max_attempts"] == 3
