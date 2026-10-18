"""Context limits are typed, validated and injected at application startup."""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from ai_agent_lib_core import ConfigurationError, Principal
from ai_agent_lib_core.config import MappingConfigSource
from ai_agent_lib_core.integrations.http import AgentService, ServiceLifecycle
from ai_agent_lib_core.testing import FakeIdentityVerifier, Fakes

from rmprep import __main__ as cli
from rmprep import service
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


def test_command_line_entrypoint_loads_and_injects_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / ".env"
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=72000\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ENV_FILE", file)
    runner = AsyncMock(return_value="Scripted reply")
    monkeypatch.setattr(cli, "_run", runner)
    assert cli.main(["Hello"]) == 0
    runner.assert_awaited_once()
    assert runner.await_args is not None
    assert runner.await_args.args[-1].budget.recent_tokens == 72_000
    # A malformed setting fails before starting a graph or model.
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=0\n", encoding="utf-8")
    runner.reset_mock()
    assert cli.main(["Hello"]) != 0
    runner.assert_not_awaited()


def _graph_builds_with(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the graph with a recorder of the context policy it was built with."""
    built: dict[str, Any] = {}

    def fake_build_graph(
        services: Any, tools: Any = (), *, context_builder: ContextBuilder | None = None
    ) -> str:
        built["context_builder"] = context_builder
        return "graph"

    async def no_tools(services: Any) -> tuple[()]:
        return ()

    monkeypatch.setattr(service, "build_graph", fake_build_graph)
    monkeypatch.setattr(service, "registered_mcp_tools", no_tools)
    return built


async def test_serve_entrypoint_loads_the_budget_and_hands_the_composed_service_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / ".env"
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=72000\n", encoding="utf-8")
    monkeypatch.setattr(service, "ENV_FILE", file)
    runner = Mock(return_value=0)
    monkeypatch.setattr(service, "run_agent", runner)
    built = _graph_builds_with(monkeypatch)

    assert service.main(["--port", "9000"]) == 0

    runner.assert_called_once()
    composed, argv = runner.call_args.args
    assert isinstance(composed, AgentService)
    assert argv == ["--port", "9000"]
    # Only the graph builder carries the loaded budget; the rest is SERVICE as declared.
    assert dataclasses.replace(composed, build_graph=service.SERVICE.build_graph) == service.SERVICE
    assert composed.build_graph is not service.SERVICE.build_graph
    assert await composed.build_graph(object()) == "graph"  # type: ignore[arg-type]
    assert built["context_builder"].budget.recent_tokens == 72_000


def test_serve_entrypoint_refuses_a_malformed_budget_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / ".env"
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=0\n", encoding="utf-8")
    monkeypatch.setattr(service, "ENV_FILE", file)
    runner = Mock(return_value=0)
    monkeypatch.setattr(service, "run_agent", runner)

    assert service.main([]) != 0

    runner.assert_not_called()  # no listener, container, graph or model
    assert "RMPREP_CONTEXT_RECENT_TOKENS" in capsys.readouterr().err


async def test_the_service_alone_reads_the_budget_when_its_graph_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / ".env"
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=48000\n", encoding="utf-8")
    monkeypatch.setattr(service, "ENV_FILE", file)
    built = _graph_builds_with(monkeypatch)

    await service.SERVICE.build_graph(object())  # type: ignore[arg-type]
    assert built["context_builder"].budget.recent_tokens == 48_000

    # A malformed value still stops the unadorned service before it is ready.
    file.write_text("RMPREP_CONTEXT_RECENT_TOKENS=0\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="RMPREP_CONTEXT_RECENT_TOKENS"):
        await service.SERVICE.build_graph(object())  # type: ignore[arg-type]


def test_importing_the_service_reads_no_context_settings() -> None:
    environment = {**os.environ, "RMPREP_CONTEXT_RECENT_TOKENS": "0"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import rmprep.service"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
