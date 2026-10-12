"""The agent uses its configured policy and writes an audit event."""

from pathlib import Path

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.testing import audit_records, load_test_config, scripted_providers

from rmprep import APPLICATION, answer, build_graph

SERVICE = Path(__file__).resolve().parents[1]


async def test_the_rules_allow_the_model_and_record_the_caller(tmp_path: Path) -> None:
    config = load_test_config(SERVICE / ".env.example", state_dir=tmp_path)
    async with ServiceContainer(
        config, scripted_providers("Please supply a client_id.")
    ) as services:
        await services.validate()
        context = await services.authenticate(None, application=APPLICATION, thread_id="meeting-1")
        reply = await answer(build_graph(services), services, context, "memo: prepare a memo")
    assert reply == "Please supply a client_id."
    (record,) = audit_records(tmp_path)
    assert (record["event"], record["outcome"]) == ("model.call", "success")
    assert record["attributes"]["policy_reason_code"] == "rmprep-uses-its-models"
    assert record["subject"] == "dev-user"
