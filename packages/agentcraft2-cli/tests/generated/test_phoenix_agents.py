"""Generated HTTP agents use shared tracing without changing their response contract."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentcraft2_cli.__main__ import main
from agentcraft2_cli.testing import toolbox_for_tests
from agentcraft2_core import Principal, ServiceContainer
from agentcraft2_core.config.phoenix import PHOENIX_LABELS, PHOENIX_VARIABLES, PhoenixSettings
from agentcraft2_core.integrations.http import ServiceLifecycle
from agentcraft2_core.integrations.langgraph.phoenix import PhoenixTracing
from agentcraft2_core.testing import FakeChatModelProvider, FakeIdentityVerifier, Fakes, calls_tool


@pytest.mark.parametrize("live", [False, True])
async def test_generated_agent_traces_tool_and_same_thread_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    name = "trace-live-agent" if live else "trace-json-agent"
    package = name.replace("-", "_")
    toolbox = toolbox_for_tests()
    assert main(["init", "demo", "--dir", str(tmp_path)], toolbox) == 0
    workspace = tmp_path / "demo"
    assert main(["new", "agent", name, "--workspace", str(workspace)], toolbox) == 0
    folder = workspace / "agents" / name
    monkeypatch.syspath_prepend(str(folder / "src"))
    module = importlib.import_module(f"{package}.service")
    config_text = (folder / ".env.example").read_text()
    instructions = (folder / "docs/tracing.md").read_text()
    assert all(variable in config_text for variable in PHOENIX_VARIABLES)
    assert all(variable in instructions for variable in PHOENIX_VARIABLES)
    assert f"{PHOENIX_LABELS['project']}={name}" in instructions
    assert f'"%PYTHON%" -m {package}.service' in instructions
    assert "Python and pip" in instructions
    assert "PowerShell" not in instructions
    assert "{{" not in instructions
    exporter = InMemorySpanExporter()
    tracing = PhoenixTracing(
        PhoenixSettings(project_name="test", capture_content=True),
        application=name,
        exporter=exporter,
    )
    kit = Fakes(
        model=FakeChatModelProvider([calls_tool("greet", name="Ada"), "Hello Ada", "You are Ada"]),
        identity=FakeIdentityVerifier({"test-token": Principal(subject="ada", tenant="t-1")}),
    )
    try:
        async with ServiceContainer(kit.config(), kit.providers(), model_trace=tracing) as services:
            lifecycle = ServiceLifecycle(services.validate)
            app = await module.build_app(services, lifecycle, tracing=tracing)
            await lifecycle.start()
            transport = httpx.ASGITransport(app=app)
            headers = {"authorization": "Bearer test-token"}
            if live:
                headers["accept"] = "text/event-stream"
            answers = []
            async with httpx.AsyncClient(
                transport=transport, base_url="http://agent.test"
            ) as client:
                for question in ("Greet Ada", "What is my name?"):
                    reply = await client.post(
                        "/invoke",
                        headers=headers,
                        json={"input": {"question": question}, "thread_id": "one"},
                    )
                    assert reply.status_code == 200, reply.text
                    if live:
                        events = [
                            json.loads(line[6:])
                            for line in reply.text.splitlines()
                            if line.startswith("data: ")
                        ]
                        accepted = next(e for e in events if "output" in e)
                    else:
                        accepted = reply.json()
                    answers.append(accepted["output"])
            assert [a["answer"] for a in answers] == ["Hello Ada", "You are Ada"]
            assert len({a["trace"]["trace_id"] for a in answers}) == 2
        tracing.provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len([s for s in spans if s.name == "greet"]) == 1
        assert len([s for s in spans if s.name == "model"]) == 3
        roots = [s for s in spans if s.name == f"{name}.turn"]
        assert len(roots) == 2
        assert len({s.attributes["session.id"] for s in roots if s.attributes}) == 1
        assert all(s.attributes and "agentcraft2.checkpoints" in s.attributes for s in roots)
    finally:
        tracing.close()
