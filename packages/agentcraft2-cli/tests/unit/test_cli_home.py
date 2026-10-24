"""A command works on the project home from any folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcraft2_cli.commands.common import workspace_root
from agentcraft2_cli.errors import CliError
from agentcraft2_core.contracts import ConfigurationError


def _workspace(folder: Path) -> Path:
    folder.mkdir()
    (folder / "agentcraft2.toml").write_text('name = "demo"\n', encoding="utf-8")
    return folder


def test_without_a_home_the_workspace_is_found_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AC2_HOME", raising=False)
    workspace = _workspace(tmp_path / "demo")
    (workspace / "agents").mkdir()
    monkeypatch.chdir(workspace / "agents")
    assert workspace_root(None) == workspace.resolve()


def test_the_home_is_the_workspace_whatever_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "demo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("AC2_HOME", str(workspace))
    monkeypatch.chdir(elsewhere)
    assert workspace_root(None) == workspace.resolve()


def test_an_option_that_names_a_workspace_beats_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _workspace(tmp_path / "home")
    other = _workspace(tmp_path / "other")
    monkeypatch.setenv("AC2_HOME", str(home))
    assert workspace_root(other) == other.resolve()


def test_a_home_that_is_not_a_workspace_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("AC2_HOME", str(plain))
    with pytest.raises(CliError, match="is not inside a workspace"):
        workspace_root(None)


def test_a_home_that_does_not_exist_is_refused_before_anything_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AC2_HOME", str(tmp_path / "typo"))
    with pytest.raises(ConfigurationError, match="does not name a folder"):
        workspace_root(None)
