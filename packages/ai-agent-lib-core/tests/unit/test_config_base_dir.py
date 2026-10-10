"""Relative paths in configuration are relative to the folder of the .env file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_agent_lib_core.adapters import (
    DuckDbCsvOptions,
    FileRegistryOptions,
    JsonlAuditOptions,
    RulesPolicyOptions,
)
from ai_agent_lib_core.config import (
    ConfigResolver,
    EnvSetting,
    Key,
    MappingConfigSource,
    load_service_config,
    options_key,
    provider_key,
    render_env_file,
    secret_key,
    variable_for,
)
from ai_agent_lib_core.contracts import OptionsModel, ProviderSelection, Section

SETTINGS = [
    EnvSetting(Key.MODEL_PROVIDER, "fake", comment="The model.\nNo key is needed."),
    EnvSetting(Key.MODEL_ID, "fake-model"),
    EnvSetting(provider_key(Section.CHECKPOINT), "none"),
    EnvSetting(options_key(Section.POLICY), {"path": "../shared/rules.yaml"}),
    EnvSetting(options_key(Section.REGISTRY), {"agents_path": "registry/agents.yaml"}),
    EnvSetting(options_key(Section.AUDIT), {"path": "/var/log/audit.jsonl"}),
    EnvSetting(
        Key.DATA_SOURCES,
        {"people": {"kind": "duckdb_csv", "data_dir": "data", "queries_dir": "queries"}},
    ),
    EnvSetting(Key.TLS_CA_BUNDLE, "certs/ca.pem"),
    EnvSetting(Key.DEPLOYMENT_ENV, "dev", comment="Only when deployed.", enabled=False),
    EnvSetting(secret_key("rates_token"), "s3cret"),
]


def test_a_file_written_from_logical_keys_resolves_to_what_was_asked(tmp_path: Path) -> None:
    text = render_env_file(SETTINGS, header="A service.\nGenerated.")
    assert text.startswith("# A service.\n# Generated.\n\n# The model.\n# No key is needed.\n")
    assert f"# {variable_for(Key.DEPLOYMENT_ENV)}=dev\n" in text
    assert text.endswith("=s3cret\n")
    service = tmp_path / "services" / "hello"
    service.mkdir(parents=True)
    (service / ".env").write_text(text, encoding="utf-8")

    config = load_service_config(service / ".env")
    assert (config.model.provider, config.model.model_id) == ("fake", "fake-model")
    assert config.section(Section.CHECKPOINT).provider == "none"
    assert config.deployment_env.value == "local"
    assert config.secrets["rates_token"].get_secret_value() == "s3cret"

    # Wherever the service is started from, its files are found beside its .env.
    base = service.resolve()
    rules = config.section(Section.POLICY).parse_options(RulesPolicyOptions)
    registry = config.section(Section.REGISTRY).parse_options(FileRegistryOptions)
    audit = config.section(Section.AUDIT).parse_options(JsonlAuditOptions)
    people = config.data_sources["people"].parse_options(DuckDbCsvOptions)
    assert rules.path == base / "../shared/rules.yaml"
    assert registry.agents_path == base / "registry/agents.yaml"
    assert registry.tools_path == base / "registry/mcp-tools.yaml"  # a default is rebased too
    assert audit.path == Path("/var/log/audit.jsonl").absolute()  # an absolute path stays as it is
    assert (people.data_dir, people.queries_dir) == (base / "data", base / "queries")
    assert config.tls_ca_bundle == base / "certs/ca.pem"


def test_without_a_file_a_relative_path_stays_relative_to_the_working_directory(
    tmp_path: Path,
) -> None:
    source = MappingConfigSource(
        {variable_for(options_key(Section.POLICY)): json.dumps({"path": "rules.yaml"})}
    )
    config = ConfigResolver(source).resolve()
    assert config.section(Section.POLICY).parse_options(RulesPolicyOptions).path == Path(
        "rules.yaml"
    )
    missing = load_service_config(tmp_path / "no-such.env")
    assert missing.section(Section.AUDIT).base_dir is None


def test_paths_inside_nested_options_are_rebased_too(tmp_path: Path) -> None:
    class Inner(OptionsModel):
        ca_file: Path | None = None

    class Outer(OptionsModel):
        inner: Inner = Inner()
        name: str = "x"

    selection = ProviderSelection("thing", {"inner": {"ca_file": "ca.pem"}}, base_dir=tmp_path)
    assert selection.parse_options(Outer).inner.ca_file == tmp_path / "ca.pem"
    assert ProviderSelection("thing", {}, base_dir=tmp_path).parse_options(Outer) == Outer()


def test_a_setting_must_name_a_key_and_fit_on_one_line() -> None:
    with pytest.raises(KeyError, match="no variable feeds"):
        render_env_file([EnvSetting("model.temperature", "0")])
    with pytest.raises(ValueError, match="one line"):
        render_env_file([EnvSetting(Key.MODEL_ID, "two\nlines")])
    assert render_env_file([]) == "\n"
