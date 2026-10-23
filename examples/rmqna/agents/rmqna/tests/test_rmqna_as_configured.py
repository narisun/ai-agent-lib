"""The agent with its own configuration: the real rules, registry and audit log.

Only the model is scripted. These tests read `.env.example`, so they show
what the service does as it is set up, and they fail when a rule is missing.
The agent has no tools of its own; what it may ask of rmqna-data is checked
against the real server in the workspace's `tests/test_rmqna_with_rmqna_data.py`.
"""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.testing import (
    audit_records,
    load_test_config,
    scripted_model,
    scripted_providers,
)

from rmqna import APPLICATION, answer, build_graph
from rmqna.prompts import system_prompt

SERVICE = Path(__file__).resolve().parents[1]


async def test_the_rules_allow_the_agent_its_model(tmp_path: Path) -> None:
    config = load_test_config(SERVICE / ".env.example", state_dir=tmp_path)
    async with ServiceContainer(config, scripted_providers("Forty-six clients.")) as services:
        await services.validate()
        # No token: on a developer's machine the development identity stands in.
        context = await services.authenticate(None, application=APPLICATION, thread_id="th-1")
        reply = await answer(
            build_graph(services), services, context, "How many clients are there?"
        )
        prompt = scripted_model(services).models[0].calls[0]

    assert reply == "Forty-six clients."
    # The model was given the packaged prompt, whole, ahead of the question.
    content = prompt[0].content
    assert isinstance(content, list)
    assert isinstance(content[0], dict)
    assert content[0]["text"] == system_prompt()
    # The step was allowed by a rule in policies/agentcraft2/rules/data.yaml.
    decided = [
        (record["event"], record["outcome"], record["attributes"]["policy_reason_code"])
        for record in audit_records(tmp_path)
    ]
    assert decided == [("model.call", "success", "rmqna-uses-its-models")]
    assert {record["subject"] for record in audit_records(tmp_path)} == {"dev-user"}


async def test_the_registry_lists_the_one_server_the_agent_works_with(tmp_path: Path) -> None:
    config = load_test_config(SERVICE / ".env.example", state_dir=tmp_path)
    async with ServiceContainer(config, scripted_providers()) as services:
        entry = services.registry.agents.get(APPLICATION)

    assert entry is not None
    assert list(entry.mcp_servers) == ["rmqna-data"]
    assert list(entry.model_aliases) == ["default"]
