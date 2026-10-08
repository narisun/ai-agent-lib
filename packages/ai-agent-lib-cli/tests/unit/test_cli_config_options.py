"""'agentlib config options': every adapter, and each one's options, read from the code."""

from __future__ import annotations

import pytest

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.testing import toolbox_for_tests


def run(*arguments: str) -> int:
    return main(list(arguments), toolbox=toolbox_for_tests())


def test_without_arguments_every_port_lists_its_adapters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run("config", "options") == 0
    out = capsys.readouterr().out
    assert "audit\n  firehose         2 options\n  jsonl            local only, 3 options\n" in out
    assert "  env              no options\n" in out
    assert "agentlib config options PORT NAME" in out
    assert out.rstrip().endswith("agentlib config options limits")


def test_one_port_lists_only_its_adapters(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("config", "options", "checkpoint") == 0
    out = capsys.readouterr().out
    assert out.startswith("checkpoint\n  none             no options\n")
    assert "audit" not in out


def test_one_adapter_shows_each_option_with_its_default_and_meaning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run("config", "options", "audit", "jsonl") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "audit jsonl (local only): Options for the JSONL audit sink."
    assert lines[1] == "Set them as one JSON object in EAP_AUDIT_OPTIONS."
    assert '  path  (path; default ".agentlib/audit.jsonl")' in lines
    assert "      Force each record to disk before write returns." in lines


def test_data_source_options_say_where_they_go(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("config", "options", "data", "duckdb_csv") == 0
    out = capsys.readouterr().out
    assert 'Set them beside "kind" in each entry of EAP_DATA_SOURCES.' in out
    assert "  queries_dir  (path; required)" in out


def test_an_unknown_port_or_adapter_says_what_exists(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("config", "options", "moon") == 1
    err = capsys.readouterr().err
    assert "there is no port called 'moon'" in err
    assert "expected: one of: model, secrets, audit" in err
    assert run("config", "options", "audit", "kafka") == 1
    assert "registered providers: firehose, jsonl" in capsys.readouterr().err


def test_schema_prints_json_schema_for_an_editor(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert run("config", "options", "audit", "jsonl", "--schema") == 0
    schema = json.loads(capsys.readouterr().out)
    assert set(schema["properties"]) == {"tracing", "path", "fsync"}
    assert schema["additionalProperties"] is False
    assert run("config", "options", "--schema") == 1
    assert "--schema needs a PORT and a NAME" in capsys.readouterr().err


def test_limits_lists_the_timeouts_retries_and_budgets(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("config", "options", "limits") == 0
    out = capsys.readouterr().out
    assert "Set them as one JSON object in EAP_LIMITS" in out
    assert "  budget.model_calls  (integer; default 50)" in out
    assert "  model.timeout_seconds  (number; default 120.0)" in out
