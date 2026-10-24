"""agentcraft2.settings.toml: what a developer says about a project, in TOML, under the .env."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcraft2_core.adapters import (
    DuckDbCsvOptions,
    FileRegistryOptions,
    RulesPolicyOptions,
    StaticIdentityOptions,
)
from agentcraft2_core.adapters.checkpoint_options import SqliteCheckpointOptions
from agentcraft2_core.config import (
    SETTINGS_FILE,
    ConfigResolver,
    SettingsFileSource,
    service_resolver,
    settings_files,
    settings_schema,
)
from agentcraft2_core.config.sources import LayeredConfigSource, MappingConfigSource
from agentcraft2_core.contracts import ConfigurationError, Section


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AC2_HOME", "AC2_PROFILE", "AC2_DEPLOYMENT_ENV", "AC2_MODEL_ID"):
        monkeypatch.delenv(name, raising=False)


def _write(folder: Path, text: str) -> Path:
    path = folder / SETTINGS_FILE
    path.write_text(text, encoding="utf-8")
    return path


FULL = """
[model]
provider = "bedrock"
id = "eu.anthropic.claude-sonnet-4-5"

[model.aliases]
fast = "eu.anthropic.claude-haiku-4-5"

[model.aliases.judge]
id = "judge-model"
provider = "anthropic"

[model.options.anthropic]
prompt_caching = true

[limits.budget]
model_calls = 20

[registry]
agents = "registry/agents.yaml"
tools = "registry/mcp-tools.yaml"

[policy]
rules = "policies/rules.yaml"

[persona]
subject = "ada"
tenant = "acme"
roles = ["analyst", "viewer"]

[data.accounts]
kind = "duckdb_csv"
data_dir = "data"
queries_dir = "queries"
"""


# ------------------------------------------------------------------------ what it writes


def test_every_table_writes_the_setting_it_stands_for(tmp_path: Path) -> None:
    config = ConfigResolver(SettingsFileSource(_write(tmp_path, FULL))).resolve()
    assert config.model.provider == "bedrock"
    assert config.model.model_id == "eu.anthropic.claude-sonnet-4-5"
    assert config.model.resolve("fast").model_id == "eu.anthropic.claude-haiku-4-5"
    judge = config.model.resolve("judge")
    assert (judge.provider, judge.model_id) == ("anthropic", "judge-model")
    assert config.model.options["anthropic"] == {"prompt_caching": True}
    assert config.limits.budget.model_calls == 20
    # Parsed, the paths are relative to the file that holds them.
    config = service_resolver(tmp_path / ".env").resolve()
    registry = config.section(Section.REGISTRY).parse_options(FileRegistryOptions)
    assert registry.agents_path == tmp_path.resolve() / "registry/agents.yaml"
    policy = config.section(Section.POLICY).parse_options(RulesPolicyOptions)
    assert policy.path == tmp_path.resolve() / "policies/rules.yaml"
    persona = config.section(Section.IDENTITY).parse_options(StaticIdentityOptions)
    assert (persona.subject, persona.tenant, persona.roles) == (
        "ada",
        "acme",
        ("analyst", "viewer"),
    )
    source = config.data_sources["accounts"]
    assert source.provider == "duckdb_csv"
    assert source.parse_options(DuckDbCsvOptions).data_dir == tmp_path.resolve() / "data"


def test_an_empty_file_writes_nothing(tmp_path: Path) -> None:
    assert list(SettingsFileSource(_write(tmp_path, "")).names()) == []


def test_a_byte_order_mark_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / SETTINGS_FILE
    path.write_bytes(b"\xef\xbb\xbf" + b'[model]\nid = "m"\n')
    assert SettingsFileSource(path).get("AC2_MODEL_ID") == "m"


def test_the_file_says_where_each_value_was_written(tmp_path: Path) -> None:
    source = SettingsFileSource(_write(tmp_path, FULL))
    resolution = ConfigResolver(source).explain()
    assert resolution.origins["model.model_id"] == f"{SETTINGS_FILE} [model]"
    assert resolution.origins["data.sources"] == f"{SETTINGS_FILE} [data]"


# --------------------------------------------------------------------------- precedence


def test_a_variable_beats_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, '[model]\nid = "from-the-file"\nprovider = "fake"\n')
    (tmp_path / ".env").write_text("AC2_MODEL_ID=from-the-env-file\n", encoding="utf-8")
    config = service_resolver(tmp_path / ".env").resolve()
    assert config.model.model_id == "from-the-env-file"
    assert config.model.provider == "fake"  # what the .env does not say, the file does

    monkeypatch.setenv("AC2_MODEL_ID", "from-the-shell")
    assert service_resolver(tmp_path / ".env").resolve().model.model_id == "from-the-shell"


def test_the_settings_file_of_a_service_beats_the_one_in_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    service = home / "agents" / "demo"
    service.mkdir(parents=True)
    _write(home, '[model]\nid = "from-the-home"\nprovider = "fake"\n')
    _write(service, '[model]\nid = "from-the-service"\n')
    monkeypatch.setenv("AC2_HOME", str(home))
    config = service_resolver(service / ".env").resolve()
    assert config.model.model_id == "from-the-service"
    assert config.model.provider == "fake"


def test_the_home_settings_file_is_read_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, '[model]\nid = "from-the-home"\n')
    monkeypatch.setenv("AC2_HOME", str(tmp_path))
    assert service_resolver().resolve().model.model_id == "from-the-home"


def test_settings_files_lists_those_that_exist_without_repeats(tmp_path: Path) -> None:
    here = _write(tmp_path, "")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert settings_files(tmp_path, empty, tmp_path) == [here]


# --------------------------------------------------------------------- relative paths


def test_a_relative_path_is_relative_to_the_file_that_holds_it(tmp_path: Path) -> None:
    service = tmp_path / "workspace" / "agents" / "demo"
    service.mkdir(parents=True)
    _write(service, '[registry]\nagents = "../../registry/agents.yaml"\n')
    config = service_resolver(service / ".env").resolve()
    registry = config.section(Section.REGISTRY).parse_options(FileRegistryOptions)
    assert registry.agents_path == service.resolve() / "../../registry/agents.yaml"


def test_state_defaults_are_unaffected(tmp_path: Path) -> None:
    _write(tmp_path, '[model]\nid = "m"\n')
    config = service_resolver(tmp_path / ".env").resolve()
    checkpoint = config.section(Section.CHECKPOINT).parse_options(SqliteCheckpointOptions)
    assert checkpoint.path == tmp_path.resolve() / ".agentcraft2" / "checkpoints.sqlite"


# ----------------------------------------------------------------------------- errors


def test_a_misspelt_key_is_refused_with_a_suggestion(tmp_path: Path) -> None:
    path = _write(tmp_path, '[model]\nprovder = "bedrock"\n')
    with pytest.raises(ConfigurationError) as caught:
        SettingsFileSource(path)
    assert "model.provder: not a key of the file; did you mean provider?" in (
        caught.value.actual or ""
    )


def test_an_unknown_table_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, "[modle]\nid = 1\n")
    with pytest.raises(ConfigurationError) as caught:
        SettingsFileSource(path)
    assert "modle: not a key of the file; did you mean model?" in (caught.value.actual or "")


def test_the_infrastructure_is_not_something_the_file_can_say(tmp_path: Path) -> None:
    path = _write(tmp_path, '[identity]\nprovider = "jwt"\n[audit]\nprovider = "jsonl"\n')
    with pytest.raises(ConfigurationError) as caught:
        SettingsFileSource(path)
    assert "identity: not a key of the file" in (caught.value.actual or "")
    assert "audit: not a key of the file" in (caught.value.actual or "")


def test_a_wrong_type_names_the_key_and_never_repeats_the_value(tmp_path: Path) -> None:
    path = _write(tmp_path, '[model]\nid = ["sk-a-secret-by-mistake"]\n')
    with pytest.raises(ConfigurationError) as caught:
        SettingsFileSource(path)
    assert "model.id: string_type" in (caught.value.actual or "")
    assert "sk-a-secret" not in str(caught.value)


def test_text_that_is_not_toml_says_where(tmp_path: Path) -> None:
    path = _write(tmp_path, '[model]\nid = "unfinished\n')
    with pytest.raises(ConfigurationError, match=r"is not valid TOML \(.*line 2"):
        SettingsFileSource(path)


def test_a_file_that_cannot_be_read_says_why(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not be read"):
        SettingsFileSource(tmp_path / "missing.toml")


def test_a_bad_value_for_a_setting_is_reported_by_the_resolver_as_for_a_variable(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "[limits.budget]\nmodel_calls = -1\n")
    with pytest.raises(ConfigurationError, match="AC2_LIMITS"):
        ConfigResolver(SettingsFileSource(path)).resolve()


# --------------------------------------------------------------------------------- schema


def test_the_schema_describes_the_tables_and_nothing_of_the_infrastructure() -> None:
    schema = json.loads(settings_schema())
    assert schema["title"] == SETTINGS_FILE
    assert set(schema["properties"]) == {"model", "limits", "registry", "policy", "persona", "data"}
    assert schema["additionalProperties"] is False


def test_layered_sources_pass_the_origin_through() -> None:
    plain = MappingConfigSource({"B": "1"})
    layers = LayeredConfigSource(plain)
    assert layers.origin_of("B") is None
