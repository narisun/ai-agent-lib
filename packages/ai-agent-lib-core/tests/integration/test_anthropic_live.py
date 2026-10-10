"""Opt-in smoke test against the live Anthropic API.

Run it with a real key and model ID in the environment::

    python -m pytest -m integration packages/ai-agent-lib-core/tests/integration
"""

from __future__ import annotations

import os

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from ai_agent_lib_core.adapters import AnthropicChatModelProvider

pytestmark = [pytest.mark.integration, pytest.mark.enable_socket]


async def test_the_live_api_answers_a_one_line_prompt() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model_id = os.environ.get("EAP_MODEL_ID")
    if not api_key or not model_id:
        pytest.skip("set ANTHROPIC_API_KEY and EAP_MODEL_ID to run the live smoke test")
    model = AnthropicChatModelProvider(SecretStr(api_key)).create(model_id)
    assert isinstance(model, BaseChatModel)
    reply = await model.ainvoke([HumanMessage("Reply with the single word: ready")])
    assert "ready" in str(reply.content).lower()
