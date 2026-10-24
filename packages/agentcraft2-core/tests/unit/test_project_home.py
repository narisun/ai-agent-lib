"""AC2_HOME: one folder for the files and the state of a project."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcraft2_core.adapters import JsonlAuditOptions
from agentcraft2_core.config import (
    ConfigResolver,
    MappingConfigSource,
    project_home,
    service_resolver,
    state_dir,
)
from agentcraft2_core.config.sources import LayeredConfigSource
from agentcraft2_core.contracts import ConfigurationError, Section


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    monkeypatch.setenv("AC2_HOME", str(folder))
    return folder


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AC2_HOME", "AC2_PROFILE", "AC2_DEPLOYMENT_ENV", "AC2_MODEL_TRACE_PATH"):
        monkeypatch.delenv(name, raising=False)


# ----------------------------------------------------------------------------- reading it


def test_without_the_variable_there_is_no_home() -> None:
    assert project_home() is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_home_is_no_home(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("AC2_HOME", blank)
    assert project_home() is None


def test_the_home_is_the_folder_the_variable_names(home: Path) -> None:
    assert project_home() == home.resolve()


def test_a_home_that_does_not_exist_is_refused_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "typo"
    monkeypatch.setenv("AC2_HOME", str(missing))
    with pytest.raises(ConfigurationError) as caught:
        project_home()
    assert caught.value.message == "AC2_HOME does not name a folder"
    assert "no such folder" in (caught.value.actual or "")
    assert not missing.exists()


def test_a_home_that_is_a_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "project.txt"
    file.write_text("", encoding="utf-8")
    monkeypatch.setenv("AC2_HOME", str(file))
    with pytest.raises(ConfigurationError) as caught:
        project_home()
    assert "not a folder" in (caught.value.actual or "")


def test_the_variable_is_read_from_the_source_it_is_given() -> None:
    assert project_home(MappingConfigSource({"AC2_HOME": "."})) == Path.cwd().resolve()


# ---------------------------------------------------------------------------- state folder


def test_state_goes_beside_the_service_without_a_home(tmp_path: Path) -> None:
    assert state_dir(tmp_path) == tmp_path / ".agentcraft2"


def test_state_goes_under_the_home_when_there_is_one(home: Path, tmp_path: Path) -> None:
    assert state_dir(tmp_path / "service") == home.resolve() / ".agentcraft2"


# ------------------------------------------------------------------------ the .env files


def test_the_homes_env_file_is_the_one_read_by_default(home: Path) -> None:
    (home / ".env").write_text("AC2_MODEL_ID=from-the-home\n", encoding="utf-8")
    assert service_resolver().resolve().model.model_id == "from-the-home"


def test_a_service_env_file_beats_the_homes(home: Path, tmp_path: Path) -> None:
    (home / ".env").write_text("AC2_MODEL_ID=from-the-home\nAC2_MODEL_PROVIDER=fake\n")
    service = tmp_path / "service"
    service.mkdir()
    (service / ".env").write_text("AC2_MODEL_ID=from-the-service\n", encoding="utf-8")
    config = service_resolver(service / ".env").resolve()
    assert config.model.model_id == "from-the-service"
    assert config.model.provider == "fake"  # what the service does not say, the home does


def test_the_process_environment_beats_both(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / ".env").write_text("AC2_MODEL_ID=from-the-home\n", encoding="utf-8")
    monkeypatch.setenv("AC2_MODEL_ID", "from-the-shell")
    assert service_resolver().resolve().model.model_id == "from-the-shell"


def test_the_home_cannot_be_set_in_the_file_that_it_locates(home: Path) -> None:
    (home / ".env").write_text(f"AC2_HOME={home}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        service_resolver()
    assert "it locates that file" in caught.value.message


def test_without_a_home_the_working_directory_env_file_is_read_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("AC2_MODEL_ID=from-here\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert service_resolver().resolve().model.model_id == "from-here"


# ----------------------------------------------------------------- where paths are relative


def test_state_defaults_are_found_under_the_home(home: Path) -> None:
    audit = service_resolver().resolve().section(Section.AUDIT).parse_options(JsonlAuditOptions)
    assert audit.path == home.resolve() / ".agentcraft2" / "audit.jsonl"


def test_a_path_in_the_shell_is_relative_to_the_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AC2_MODEL_TRACE_PATH", "traces/model.jsonl")
    assert service_resolver().resolve().model_trace_path == home.resolve() / "traces/model.jsonl"


def test_a_path_in_the_homes_env_file_is_relative_to_the_home(home: Path) -> None:
    (home / ".env").write_text("AC2_MODEL_TRACE_PATH=traces/model.jsonl\n", encoding="utf-8")
    assert service_resolver().resolve().model_trace_path == home.resolve() / "traces/model.jsonl"


def test_a_path_in_a_service_env_file_stays_relative_to_that_file(
    home: Path, tmp_path: Path
) -> None:
    service = tmp_path / "workspace" / "agents" / "demo"
    service.mkdir(parents=True)
    (service / ".env").write_text("AC2_MODEL_TRACE_PATH=../../shared/trace.jsonl\n")
    config = service_resolver(service / ".env").resolve()
    assert config.model_trace_path == service.resolve() / "../../shared/trace.jsonl"


def test_a_service_without_a_home_keeps_its_old_anchor(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AC2_MODEL_TRACE_PATH=trace.jsonl\n", encoding="utf-8")
    config = service_resolver(tmp_path / ".env").resolve()
    assert config.model_trace_path == tmp_path.resolve() / "trace.jsonl"
    audit = config.section(Section.AUDIT).parse_options(JsonlAuditOptions)
    assert audit.path == tmp_path.resolve() / ".agentcraft2" / "audit.jsonl"


def test_a_layer_says_where_its_values_were_written(tmp_path: Path) -> None:
    first = MappingConfigSource({"A": "1"}, base_dir=tmp_path / "first")
    second = MappingConfigSource({"A": "2", "B": "2"}, base_dir=tmp_path / "second")
    layers = LayeredConfigSource(first, second)
    assert layers.base_dir_for("A") == tmp_path / "first"
    assert layers.base_dir_for("B") == tmp_path / "second"
    assert layers.base_dir_for("C") is None


def test_the_resolver_uses_its_own_base_for_what_a_source_does_not_place(tmp_path: Path) -> None:
    config = ConfigResolver(
        MappingConfigSource({"AC2_MODEL_TRACE_PATH": "t.jsonl"}), base_dir=tmp_path
    ).resolve()
    assert config.model_trace_path == tmp_path / "t.jsonl"


# ------------------------------------------------------------------------- the old spelling


def test_the_variable_has_no_earlier_spelling() -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration variable"):
        ConfigResolver(MappingConfigSource({"EAP_HOME": "x"})).resolve()
