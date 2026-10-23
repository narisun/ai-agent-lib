"""A generated MCP server tells a deprecated spelling in its log, as a generated agent does."""

from __future__ import annotations

import importlib
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from agentcraft2_cli.__main__ import main
from agentcraft2_cli.testing import toolbox_for_tests
from agentcraft2_core.config import deprecations, variable_for
from agentcraft2_core.config.bindings import legacy_spelling, provider_key
from agentcraft2_core.contracts import Section


def _forget_the_generated_package() -> None:
    """Each test generates its own workspace, so the package must be imported afresh."""
    for name in [n for n in sys.modules if n == "probe_mcp" or n.startswith("probe_mcp.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _own_logging_and_deprecation_log() -> Iterator[None]:
    """A service entry point installs the root handler; give back what it replaced."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    deprecations._LOGGED.clear()
    _forget_the_generated_package()
    yield
    deprecations._LOGGED.clear()
    _forget_the_generated_package()
    root.handlers[:] = handlers
    root.setLevel(level)


def _server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path]:
    toolbox = toolbox_for_tests()
    assert main(["init", "demo", "--dir", str(tmp_path)], toolbox) == 0
    workspace = tmp_path / "demo"
    assert main(["new", "mcp", "probe-mcp", "--workspace", str(workspace)], toolbox) == 0
    folder = workspace / "mcp-servers" / "probe-mcp"
    monkeypatch.syspath_prepend(str(folder / "src"))
    module = importlib.import_module("probe_mcp.__main__")

    async def serve_nothing(*_: object) -> None:
        return None

    monkeypatch.setattr(module, "_serve", serve_nothing)
    return module, folder / ".env"


def _log_lines(out: str) -> list[dict[str, str]]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_legacy_spelling_is_logged_as_a_warning_once_logging_is_set_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module, env = _server(tmp_path, monkeypatch)
    current = variable_for(provider_key(Section.CHECKPOINT))
    old = legacy_spelling(current)
    assert f"{current}=" in env.read_text(encoding="utf-8")
    env.write_text(env.read_text(encoding="utf-8").replace(f"{current}=", f"{old}="), "utf-8")
    capsys.readouterr()

    assert module.main([]) == 0

    # Read from standard output, where configure_logging writes its JSON lines: a record
    # logged before logging was set up would not arrive there as a structured line.
    told = [line for line in _log_lines(capsys.readouterr().out) if old in line["message"]]
    assert len(told) == 1
    assert told[0]["level"] == "WARNING"
    assert told[0]["service"] == module.APPLICATION
    assert told[0]["message"] == f"{old} is deprecated; use {current} instead"


def test_canonical_spellings_log_no_deprecation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module, _ = _server(tmp_path, monkeypatch)
    capsys.readouterr()

    assert module.main([]) == 0

    assert [
        line for line in _log_lines(capsys.readouterr().out) if "deprecated" in line["message"]
    ] == []
