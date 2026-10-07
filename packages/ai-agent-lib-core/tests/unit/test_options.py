"""Configuration values are immutable, validated and hide secrets."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    ModelRef,
    ModelSection,
    OptionsModel,
    Profile,
    ProviderSelection,
    Section,
    ServiceConfig,
)


class _SinkOptions(OptionsModel):
    path: str
    fsync: bool = True


def test_provider_selection_options_are_deeply_immutable() -> None:
    raw: dict[str, Any] = {"path": "audit.jsonl", "tags": ["a", "b"], "nested": {"k": 1}}
    selection = ProviderSelection(provider="jsonl", options=raw)
    raw["path"] = "changed"
    raw["tags"].append("c")
    assert selection.options["path"] == "audit.jsonl"
    assert selection.options["tags"] == ("a", "b")
    with pytest.raises(TypeError):
        selection.options["path"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        selection.options["nested"]["k"] = 2  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        selection.provider = "other"  # type: ignore[misc]


def test_provider_selection_rejects_non_json_options() -> None:
    with pytest.raises(TypeError, match="JSON-like"):
        ProviderSelection(provider="jsonl", options={"when": object()})


def test_parse_options_returns_the_adapters_typed_model() -> None:
    options = ProviderSelection("jsonl", {"path": "a.jsonl"}).parse_options(_SinkOptions)
    assert options == _SinkOptions(path="a.jsonl", fsync=True)


def test_parse_options_rejects_unknown_keys_without_echoing_values() -> None:
    selection = ProviderSelection("jsonl", {"path": "a.jsonl", "pth": "hunter2-secret"})
    with pytest.raises(ConfigurationError) as caught:
        selection.parse_options(_SinkOptions)
    message = str(caught.value)
    assert "pth" in message
    assert "jsonl" in message
    assert "hunter2-secret" not in message


def test_parse_options_reports_missing_and_mistyped_fields() -> None:
    with pytest.raises(ConfigurationError, match="path"):
        ProviderSelection("jsonl").parse_options(_SinkOptions)
    with pytest.raises(ConfigurationError, match="fsync"):
        ProviderSelection("jsonl", {"path": "a", "fsync": "maybe"}).parse_options(_SinkOptions)


def test_default_alias_resolves_to_the_default_provider_and_model() -> None:
    section = ModelSection(provider="bedrock", model_id="model-a")
    assert section.resolve() == ModelRef(provider="bedrock", model_id="model-a")
    assert section.resolve("default") == section.resolve()


def test_named_aliases_can_use_a_different_provider() -> None:
    section = ModelSection(
        provider="bedrock",
        model_id="model-a",
        aliases={"judge": ModelRef(provider="anthropic", model_id="model-j")},
    )
    assert section.resolve("judge") == ModelRef(provider="anthropic", model_id="model-j")
    with pytest.raises(TypeError):
        section.aliases["fast"] = ModelRef("bedrock", "m")  # type: ignore[index]


def test_unknown_alias_is_a_configuration_error_that_lists_known_aliases() -> None:
    section = ModelSection(provider="bedrock", model_id="model-a", aliases={})
    with pytest.raises(ConfigurationError, match=r"'fast'.*default"):
        section.resolve("fast")
    with pytest.raises(ConfigurationError, match="none"):
        ModelSection(provider="bedrock").resolve()


def test_service_config_is_immutable() -> None:
    config = ServiceConfig.for_testing()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.profile = Profile.AWS  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.sections[Section.AUDIT] = ProviderSelection("x")  # type: ignore[index]
    with pytest.raises(TypeError):
        config.secrets["k"] = SecretStr("v")  # type: ignore[index]


def test_for_testing_selects_the_fake_adapter_everywhere() -> None:
    config = ServiceConfig.for_testing()
    assert config.profile is Profile.LOCAL
    assert config.deployment_env is DeploymentEnv.LOCAL
    assert config.model.provider == "fake"
    assert {config.section(section).provider for section in Section} == {"fake"}


def test_with_section_returns_a_changed_copy() -> None:
    config = ServiceConfig.for_testing()
    changed = config.with_section(Section.AUDIT, ProviderSelection("jsonl", {"path": "a"}))
    assert changed.section(Section.AUDIT).provider == "jsonl"
    assert config.section(Section.AUDIT).provider == "fake"


def test_enum_fields_accept_their_string_values() -> None:
    config = ServiceConfig(profile="aws", deployment_env="prod")  # type: ignore[arg-type]
    assert config.profile is Profile.AWS
    assert config.deployment_env is DeploymentEnv.PROD
    with pytest.raises(ValueError, match="moon"):
        ServiceConfig(profile="moon")  # type: ignore[arg-type]


def test_secrets_never_appear_in_repr() -> None:
    config = ServiceConfig.for_testing(secrets={"anthropic_api_key": SecretStr("sk-very-secret")})
    assert "sk-very-secret" not in repr(config)
    assert "sk-very-secret" not in str(config)
    assert config.secrets["anthropic_api_key"].get_secret_value() == "sk-very-secret"


def test_secrets_must_be_secret_strings() -> None:
    with pytest.raises(TypeError, match="SecretStr"):
        ServiceConfig.for_testing(secrets={"k": "plain"})


def test_proxy_url_is_hidden_from_repr_because_it_can_hold_credentials() -> None:
    from ai_agent_lib_core.contracts import ExternalSettings

    external = ExternalSettings(https_proxy="http://user:pw@proxy:8080")
    assert "pw" not in repr(external)


def test_missing_section_is_a_configuration_error() -> None:
    config = ServiceConfig(sections={})
    with pytest.raises(ConfigurationError, match="audit"):
        config.section(Section.AUDIT)
