"""Context limits are typed, validated and injected at application startup."""

from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from ai_agent_lib_core import ConfigurationError, Principal
from ai_agent_lib_core.config import MappingConfigSource
from ai_agent_lib_core.integrations.http import ServiceLifecycle
from ai_agent_lib_core.testing import FakeIdentityVerifier, Fakes

from rmprep.context import ContextBudget, ContextBuilder
from rmprep.service import build_app
from rmprep.settings import load_context_budget


def test_defaults_allow_large_comparisons_with_room_for_new_tools() -> None:
    budget = load_context_budget(source=MappingConfigSource({}))
    assert budget == ContextBudget(128_000, 16_000, 96_000, 4_000)
    assert budget.input_tokens - budget.recent_tokens - budget.tool_schema_tokens == 28_000


def test_file_process_precedence_is_a_snapshot_without_mutating_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / ".env"
    file.write_text(
        "RMPREP_CONTEXT_INPUT_TOKENS=64000\nRMPREP_CONTEXT_RECENT_TOKENS=32000\n"
        "RMPREP_CONTEXT_QUESTION_TOKENS=4000\nRMPREP_CONTEXT_TOOL_SCHEMA_TOKENS=2000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RMPREP_CONTEXT_RECENT_TOKENS", "48000")
    for name in ("INPUT", "QUESTION", "TOOL_SCHEMA"):
        monkeypatch.delenv(f"RMPREP_CONTEXT_{name}_TOKENS", raising=False)
    first = load_context_budget(file)
    assert first == ContextBudget(64_000, 4_000, 48_000, 2_000)
    monkeypatch.setenv("RMPREP_CONTEXT_RECENT_TOKENS", "16000")
    assert first.recent_tokens == 48_000
    assert load_context_budget(file).recent_tokens == 16_000
    monkeypatch.delenv("RMPREP_CONTEXT_RECENT_TOKENS")
    assert load_context_budget(tmp_path / "missing.env") == ContextBudget()
    assert load_context_budget(None) == ContextBudget()


@pytest.mark.parametrize(
    "value", ["", "0", "-1", "1.5", "true", "96k", "96000.0", "\uff19\uff16\uff10\uff10\uff10"]
)
def test_invalid_token_counts_fail_with_the_variable_name(value: str) -> None:
    with pytest.raises(ConfigurationError, match="RMPREP_CONTEXT_RECENT_TOKENS"):
        load_context_budget(source=MappingConfigSource({"RMPREP_CONTEXT_RECENT_TOKENS": value}))


def test_unknown_context_setting_and_inconsistent_limits_fail_at_startup() -> None:
    with pytest.raises(ConfigurationError, match="RMPREP_CONTEXT_RECENT_TOKEN"):
        load_context_budget(source=MappingConfigSource({"RMPREP_CONTEXT_RECENT_TOKEN": "96000"}))
    with pytest.raises(ConfigurationError, match="inside the input budget"):
        load_context_budget(source=MappingConfigSource({"RMPREP_CONTEXT_INPUT_TOKENS": "48000"}))
    with pytest.raises(ValueError, match="positive integers"):
        ContextBudget(recent_tokens=True)


@pytest.mark.parametrize("entrypoint", ["rmprep.__main__", "rmprep.service"])
def test_command_line_entrypoints_load_and_inject_settings(
    entrypoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / ".env"
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=72000\n", encoding="utf-8")
    module = import_module(entrypoint)
    monkeypatch.setattr(module, "ENV_FILE", file)
    http_service = entrypoint.endswith("service")
    runner = AsyncMock(return_value=None if http_service else "Scripted reply")
    monkeypatch.setattr(module, "_serve" if http_service else "_run", runner)
    if http_service:
        monkeypatch.setattr(module, "configure_logging", lambda *args, **kwargs: None)
        monkeypatch.setattr(module, "start_telemetry", lambda *args, **kwargs: lambda: None)
    assert module.main([] if http_service else ["Hello"]) == 0
    runner.assert_awaited_once()
    assert runner.await_args is not None
    assert runner.await_args.args[-1].budget.recent_tokens == 72_000
    # A malformed setting fails before starting a listener, graph or model.
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=0\n", encoding="utf-8")
    runner.reset_mock()
    assert module.main([] if http_service else ["Hello"]) != 0
    runner.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["/invoke", "/invoke/stream"])
async def test_http_and_streaming_use_the_injected_context_budget(endpoint: str) -> None:
    kit = Fakes(
        identity=FakeIdentityVerifier({"test-token": Principal(subject="developer", tenant="bank")})
    )
    builder = ContextBuilder(ContextBudget(10_000, 30, 1_000, 100))
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle, context_builder=builder)
        await lifecycle.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                endpoint,
                json={"input": {"question": "detail " * 100}, "thread_id": "budget"},
                headers={"authorization": "Bearer test-token"},
            )
    assert '"error":"invalid"' in response.text.replace(" ", "")
    assert all(not model.calls for model in kit.model.models)
