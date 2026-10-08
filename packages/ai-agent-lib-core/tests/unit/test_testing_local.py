"""Testing a service as configured: its own .env file, local adapters, a scripted model."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_core import Principal, RequestContext, ServiceContainer, bind_request_context
from ai_agent_lib_core.adapters import FakeChatModelProvider, JsonlAuditOptions
from ai_agent_lib_core.config import EnvSetting, Key, options_key, provider_key, render_env_file
from ai_agent_lib_core.contracts import ConfigurationError, Section
from ai_agent_lib_core.integrations.langgraph import SqliteCheckpointOptions
from ai_agent_lib_core.testing import audit_records, load_test_config, scripted_providers

RULES = """\
schema: agentlib.rules/v1
rules:
  - id: hello-uses-its-models
    actions: [model.route]
    applications: [hello-agent]
    resources: [default]
"""


def service(tmp_path: Path, *settings: EnvSetting) -> Path:
    folder = tmp_path / "hello-agent"
    (folder / "policies").mkdir(parents=True)
    (folder / "policies" / "rules.yaml").write_text(RULES, encoding="utf-8")
    text = render_env_file(
        [EnvSetting(options_key(Section.POLICY), {"path": "policies/rules.yaml"}), *settings]
    )
    (folder / ".env.example").write_text(text, encoding="utf-8")
    return folder / ".env.example"


async def test_a_service_runs_from_its_own_file_with_a_scripted_model(tmp_path: Path) -> None:
    # The file asks for a real model; a test never gets one.
    dotenv = service(
        tmp_path, EnvSetting(Key.MODEL_PROVIDER, "anthropic"), EnvSetting(Key.MODEL_ID, "a-model")
    )
    state = tmp_path / "state"
    config = load_test_config(dotenv, state_dir=state)
    assert (config.model.provider, config.model.model_id) == ("fake", "fake-model")
    audit = config.section(Section.AUDIT).parse_options(JsonlAuditOptions)
    checkpoint = config.section(Section.CHECKPOINT).parse_options(SqliteCheckpointOptions)
    assert (audit.path, checkpoint.path) == (state / "audit.jsonl", state / "checkpoints.sqlite")

    context = RequestContext(
        principal=Principal(subject="u-1", tenant="t-1"),
        application="hello-agent",
        request_id="r-1",
        thread_id="th-1",
    )
    async with ServiceContainer(config, scripted_providers("hello there")) as services:
        with bind_request_context(context):
            reply = await services.model().ainvoke("hi")
        model = services.model_provider("fake")
        assert isinstance(model, FakeChatModelProvider)
    assert reply.content == "hello there"
    (record,) = audit_records(state)
    assert (record["event"], record["attributes"]["policy_reason_code"]) == (
        "model.call",
        "hello-uses-its-models",
    )
    assert audit_records(tmp_path / "elsewhere") == []


def test_other_stores_and_the_real_model_can_be_kept(tmp_path: Path) -> None:
    dotenv = service(
        tmp_path,
        EnvSetting(provider_key(Section.CHECKPOINT), "none"),
        EnvSetting(Key.MODEL_PROVIDER, "anthropic"),
    )
    config = load_test_config(dotenv, state_dir=tmp_path, fake_model=False)
    assert config.model.provider == "anthropic"
    assert config.section(Section.CHECKPOINT).provider == "none"
    assert dict(config.section(Section.CHECKPOINT).options) == {}


def test_the_file_must_exist_and_the_process_environment_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_test_config(tmp_path / ".env.example", state_dir=tmp_path)
    from ai_agent_lib_core.config import variable_for

    monkeypatch.setenv(variable_for(provider_key(Section.CHECKPOINT)), "none")
    config = load_test_config(service(tmp_path), state_dir=tmp_path)
    assert config.section(Section.CHECKPOINT).provider == "sqlite"
