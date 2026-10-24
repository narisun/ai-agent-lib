"""Agent Lab keeps its config in the project home when there is one."""

from pathlib import Path

import pytest

from agentcraft2_ui.config.home import config_file


def test_the_config_file_is_in_the_state_folder_of_the_working_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AC2_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config_file(None) == tmp_path.resolve() / ".agentcraft2" / "ui" / "config.json"


def test_the_config_file_is_in_the_project_home_when_there_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "project"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("AC2_HOME", str(home))
    monkeypatch.chdir(elsewhere)
    assert config_file(None) == home.resolve() / ".agentcraft2" / "ui" / "config.json"


def test_a_config_file_that_is_named_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC2_HOME", str(tmp_path))
    assert config_file(tmp_path / "mine.json") == tmp_path.resolve() / "mine.json"
