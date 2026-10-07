"""Opt-in smoke test against live Amazon Bedrock.

Sign in and point the configuration at Bedrock, then run it::

    aws sso login --profile dev-sso
    export AWS_PROFILE=dev-sso AWS_REGION=us-east-1
    export EAP_MODEL_PROVIDER=bedrock EAP_MODEL_ID=<model or inference-profile ID>
    export EAP_TLS_CA_BUNDLE=/path/to/enterprise-ca.pem   # only behind a TLS-inspecting proxy
    uv run pytest -m integration packages/ai-agent-lib-aws/tests/integration
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from ai_agent_lib_aws.model_bedrock import BedrockChatModelProvider
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.config import load_service_config

pytestmark = [pytest.mark.integration, pytest.mark.enable_socket]


async def test_live_bedrock_answers_a_one_line_prompt() -> None:
    config = load_service_config(None)
    model_id = config.model.model_id
    if config.model.provider != "bedrock" or model_id is None:
        pytest.skip("select the bedrock model provider and a model ID to run the live smoke test")
    sessions = AwsSessionFactory.from_settings(config.external, tls_ca_bundle=config.tls_ca_bundle)
    provider = BedrockChatModelProvider(sessions)
    model = provider.create(model_id)
    assert isinstance(model, BaseChatModel)
    try:
        reply = await model.ainvoke([HumanMessage("Reply with the single word: ready")])
    except Exception as error:
        mapped = provider.classify_error(error)
        raise mapped or error from error
    assert "ready" in str(reply.content).lower()
