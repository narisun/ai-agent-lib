"""Generated walkthroughs preserve tools and saved conversation across model calls."""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.scaffold import Scaffolder
from ai_agent_lib_cli.testing import as_written, toolbox_for_tests
from ai_agent_lib_core.contracts import ModelSection
from ai_agent_lib_core.testing import load_test_config

REPO = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("name", ["helper", "customer-support-agent"])
def test_trace_tutorial_shows_tools_then_checkpoint_history(tmp_path: Path, name: str) -> None:
    tools = toolbox_for_tests()
    assert main(["init", "tutorial", "--dir", str(tmp_path)], tools) == 0
    workspace = tmp_path / "tutorial"
    assert main(["new", "agent", name, "--workspace", str(workspace)], tools) == 0
    service = workspace / "agents" / name
    package = name.replace("-", "_")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(service / "src"),
                *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep)),
            ]
        ),
    }
    result = subprocess.run(  # noqa: S603 - fixed interpreter and checked-in tutorial
        [sys.executable, "-m", f"{package}.examples.trace_react"],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Messages sent on each model call: [2, 4, 6]" in result.stdout
    assert "Checkpoint message types: ['human', 'ai', 'tool', 'ai', 'human', 'ai']" in result.stdout
    captured = [
        json.loads(line)
        for line in (service / ".agentlib/react-demo.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["event"] for record in captured] == ["model.request", "model.response"] * 3
    first, after_tool, next_turn = captured[::2]
    assert first["request_id"] == after_tool["request_id"] == "demo-turn-1"
    assert next_turn["request_id"] == "demo-turn-2"
    assert len({record["call_id"] for record in captured}) == 3
    assert len({record["thread_id"] for record in captured}) == 1
    assert [message["type"] for message in after_tool["messages"]] == [
        "system",
        "human",
        "ai",
        "tool",
    ]
    assert "Hello, Ada!" in after_tool["messages"][-1]["content"]
    assert [message["type"] for message in next_turn["messages"]] == [
        "system",
        "human",
        "ai",
        "tool",
        "ai",
        "human",
    ]
    assert next_turn["messages"][1]["content"] == "My name is Ada. Please greet me."
    assert next_turn["messages"][-1]["content"] == "What is my name?"


def test_new_workspace_includes_the_captured_run_and_readable_samples(tmp_path: Path) -> None:
    assert main(["init", "tutorial", "--dir", str(tmp_path)], toolbox_for_tests()) == 0
    workspace = tmp_path / "tutorial"
    samples = workspace / "docs/samples/verified-anthropic"
    trace = samples / "model-trace.jsonl"
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    history = json.loads((samples / "checkpoint-history.json").read_text(encoding="utf-8"))
    assert [event["event"] for event in events] == ["model.request", "model.response"] * 3
    assert [len(event["messages"]) for event in events[::2]] == [2, 4, 6]
    assert [len(turn["messages"]) for turn in history] == [4, 6]
    assert events[4]["messages"][1:-1] == history[0]["messages"]
    assert history[1]["messages"][:4] == history[0]["messages"]
    assert events[1]["response"]["tool_calls"][0]["id"] == "tool-1"
    assert events[2]["messages"][-1]["tool_call_id"] == "tool-1"
    assert {event["thread_id"] for event in events} == {"conversation-1"}
    assert {event["request_id"] for event in events} == {"request-1", "request-2"}
    for sample in samples.iterdir():
        assert (
            sample.read_bytes()
            == (REPO / "docs/samples/verified-anthropic" / sample.name).read_bytes()
        )
    result = subprocess.run(  # noqa: S603 - the generated reader uses only the standard library
        [sys.executable, "examples/read_trace.py", str(trace), "--last", "2"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "model-call-3" in result.stdout
    assert "model-call-1" not in result.stdout


@pytest.mark.parametrize("provider", ["fake", "anthropic", "bedrock"])
def test_walkthrough_instructions_match_the_generated_agent(tmp_path: Path, provider: str) -> None:
    tools = toolbox_for_tests()
    assert main(["init", "tutorial", "--dir", str(tmp_path)], tools) == 0
    workspace = tmp_path / "tutorial"
    assert (
        main(
            ["new", "agent", "support-agent", "--model", provider, "--workspace", str(workspace)],
            tools,
        )
        == 0
    )
    instructions = (workspace / "agents/support-agent/docs/anthropic-example.md").read_text(
        encoding="utf-8"
    )
    assert '"%PYTHON%" -m support_agent.examples.trace_anthropic' in instructions
    assert '"%PYTHON%" -m ai_agent_lib_cli install' in instructions
    assert ("add `anthropic` to the extras" in instructions) == (provider != "anthropic")
    assert "PowerShell" not in instructions
    assert "$env:" not in instructions
    for document in workspace.rglob("*.md"):
        for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            if "://" in link:
                continue
            relative, _, anchor = link.partition("#")
            target = document.parent / relative if relative else document
            assert target.exists(), (document, link)
            if anchor and target.suffix == ".md":
                headings = re.findall(r"^#+ (.+)$", target.read_text(encoding="utf-8"), re.M)
                slugs = [
                    re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-") for title in headings
                ]
                assert anchor in slugs, (document, link)


def test_adding_an_agent_and_updating_preserve_custom_examples(tmp_path: Path) -> None:
    tools = toolbox_for_tests()
    assert main(["init", "tutorial", "--dir", str(tmp_path)], tools) == 0
    workspace = tmp_path / "tutorial"
    assert main(["new", "agent", "first-agent", "--workspace", str(workspace)], tools) == 0
    changed = {
        PurePosixPath(
            "agents/first-agent/src/first_agent/examples/trace_anthropic.py"
        ): "\n# My example.\n",
        PurePosixPath("docs/verified-anthropic-run.md"): "\nMy team's notes.\n",
    }
    for path, suffix in changed.items():
        file = workspace / path
        file.write_text(file.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    assert main(["new", "agent", "second-agent", "--workspace", str(workspace)], tools) == 0
    report = Scaffolder(formatter=as_written).update(workspace)
    for path, suffix in changed.items():
        assert path in report.kept
        assert (workspace / path).read_text(encoding="utf-8").endswith(suffix)
    assert (
        workspace / "agents/second-agent/src/second_agent/examples/trace_anthropic.py"
    ).is_file()


@pytest.mark.parametrize("resume", [False, True])
def test_anthropic_entrypoint_records_real_adapter_messages_and_persisted_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    anthropic_types = pytest.importorskip("anthropic.types")
    langchain_anthropic = pytest.importorskip("langchain_anthropic")
    tools = toolbox_for_tests()
    assert main(["init", "tutorial", "--dir", str(tmp_path)], tools) == 0
    workspace = tmp_path / "tutorial"
    name = "continue-agent" if resume else "fresh-agent"
    assert (
        main(["new", "agent", name, "--model", "anthropic", "--workspace", str(workspace)], tools)
        == 0
    )
    service = workspace / "agents" / name
    monkeypatch.syspath_prepend(str(service / "src"))
    example = importlib.import_module(f"{name.replace('-', '_')}.examples.trace_anthropic")
    config = replace(
        load_test_config(service / ".env.example", state_dir=tmp_path / "state"),
        model=ModelSection(provider="anthropic", model_id="claude-test-model"),
        secrets={"anthropic_api_key": SecretStr("synthetic-test-key")},
    )
    monkeypatch.setattr(example, "load_service_config", lambda path: config)
    calls: list[dict[str, Any]] = []

    async def create_reply(model: object, payload: dict[str, Any]) -> SimpleNamespace:
        # Intercept before the SDK performs any network request. Keep LangChain's
        # Anthropic response conversion and the library's adapter under test.
        calls.append(payload)
        first = len(calls) % 3 == 1
        content = (
            [
                {
                    "type": "tool_use",
                    "id": f"tool-{len(calls)}",
                    "name": "greet",
                    "input": {"name": "Ada"},
                }
            ]
            if first
            else [
                {
                    "type": "text",
                    "text": "Hello, Ada!" if len(calls) % 3 == 2 else "Your name is Ada.",
                }
            ]
        )
        response = anthropic_types.Message.model_validate(
            {
                "id": f"message-{len(calls)}",
                "type": "message",
                "role": "assistant",
                "model": "claude-test-model",
                "content": content,
                "stop_reason": "tool_use" if first else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 40, "output_tokens": 10},
            }
        )
        return SimpleNamespace(parse=lambda: response)

    monkeypatch.setattr(langchain_anthropic.ChatAnthropic, "_acreate", create_reply)
    for _ in range(2):
        assert example.main(["--thread", "continued-thread"] if resume else []) == 0
    runs = list((service / ".agentlib/anthropic-demo").iterdir())
    assert len(runs) == 2
    assert len(calls) == 6
    lengths = []
    threads = set()
    for run in runs:
        history = json.loads((run / "checkpoint-history.json").read_text(encoding="utf-8"))
        events = [
            json.loads(line)
            for line in (run / "model-trace.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(events) == 6
        assert events[4]["messages"][1:-1] == history[0]["messages"]
        assert history[1]["messages"][:-2] == history[0]["messages"]
        assert history[0]["request_id"] != history[1]["request_id"]
        assert history[0]["thread_id"] == history[1]["thread_id"]
        lengths.append(len(history[1]["messages"]))
        threads.add(history[0]["thread_id"])
    assert sorted(lengths) == ([6, 12] if resume else [6, 6])
    assert len(threads) == (1 if resume else 2)
