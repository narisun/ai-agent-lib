"""The resolver turns raw values into a ServiceConfig, strictly."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_core.config import Binding, ConfigResolver, MappingConfigSource, ValueKind
from ai_agent_lib_core.config.bindings import DEFAULT_BINDINGS, Key
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    ModelRef,
    Profile,
    ProviderSelection,
    Section,
    ServiceConfig,
)


def resolve(values: dict[str, str]) -> ServiceConfig:
    return ConfigResolver(MappingConfigSource(values)).resolve()


# ------------------------------------------------------------------ defaults


def test_nothing_set_gives_the_local_profile() -> None:
    config = resolve({})
    assert config.profile is Profile.LOCAL
    assert config.deployment_env is DeploymentEnv.LOCAL
    assert config.model.provider == "anthropic"
    assert config.section(Section.AUDIT).provider == "jsonl"
    assert config.section(Section.SECRETS).provider == "env"
    assert config.section(Section.IDENTITY).provider == "static"
    assert config.section(Section.CHECKPOINT).provider == "sqlite"
    assert config.secrets == {}


def test_aws_profile_selects_the_server_side_adapters() -> None:
    config = resolve({"EAP_PROFILE": "aws", "EAP_DEPLOYMENT_ENV": "prod"})
    assert config.profile is Profile.AWS
    assert config.deployment_env is DeploymentEnv.PROD
    assert config.model.provider == "bedrock"
    assert config.section(Section.AUDIT).provider == "firehose"
    assert config.section(Section.CHECKPOINT).provider == "postgres"


# ------------------------------------------------------- provider resolution


def test_a_selector_beats_the_profile() -> None:
    config = resolve({"EAP_PROFILE": "aws", "EAP_AUDIT_PROVIDER": "jsonl"})
    assert config.section(Section.AUDIT).provider == "jsonl"
    assert config.section(Section.SECRETS).provider == "secrets_manager"


def test_local_profile_can_mix_in_a_server_side_model_provider() -> None:
    config = resolve(
        {
            "EAP_PROFILE": "local",
            "EAP_MODEL_PROVIDER": "bedrock",
            "EAP_MODEL_ID": "model-a",
            "AWS_PROFILE": "dev-sso",
            "AWS_REGION": "us-east-1",
            "AWS_CA_BUNDLE": "/etc/ssl/enterprise-ca.pem",
        }
    )
    assert config.model.resolve() == ModelRef(provider="bedrock", model_id="model-a")
    assert config.section(Section.AUDIT).provider == "jsonl"
    assert config.external.aws_profile == "dev-sso"
    assert config.external.aws_region == "us-east-1"
    assert config.external.aws_ca_bundle == Path("/etc/ssl/enterprise-ca.pem")


def test_explain_reports_where_each_choice_came_from() -> None:
    resolution = ConfigResolver(
        MappingConfigSource({"EAP_MODEL_PROVIDER": "bedrock", "EAP_PROFILE": "local"})
    ).explain()
    assert resolution.origins[Key.MODEL_PROVIDER] == "variable EAP_MODEL_PROVIDER"
    assert resolution.origins["section.audit.provider"] == "profile local"
    assert resolution.warnings == ()


# ------------------------------------------------------------ strict parsing


def test_an_unknown_owned_variable_stops_startup_with_a_suggestion() -> None:
    with pytest.raises(ConfigurationError) as caught:
        resolve({"EAP_MODEL_PROVIDR": "bedrock"})
    assert "EAP_MODEL_PROVIDR" in str(caught.value)
    assert "did you mean EAP_MODEL_PROVIDER" in str(caught.value)


def test_variables_without_the_prefix_are_ignored() -> None:
    assert resolve({"PATH": "/usr/bin", "MODEL_PROVIDER": "x"}).model.provider == "anthropic"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_value_counts_as_not_set(blank: str) -> None:
    assert resolve({"EAP_MODEL_PROVIDER": blank}).model.provider == "anthropic"


def test_enum_values_are_validated_and_case_insensitive() -> None:
    assert resolve({"EAP_PROFILE": "AWS"}).profile is Profile.AWS
    with pytest.raises(ConfigurationError) as caught:
        resolve({"EAP_PROFILE": "moon"})
    error = caught.value
    assert error.message == "EAP_PROFILE is not one of the values it takes"
    assert (error.expected, error.actual) == ("one of: local, aws", "'moon'")
    assert error.fix == "set EAP_PROFILE=local"
    with pytest.raises(ConfigurationError) as near:
        resolve({"EAP_PROFILE": "aw"})
    assert near.value.fix == "set EAP_PROFILE=aws"
    with pytest.raises(ConfigurationError, match="EAP_DEPLOYMENT_ENV"):
        resolve({"EAP_DEPLOYMENT_ENV": "staging"})


def test_section_options_are_parsed_from_json() -> None:
    config = resolve({"EAP_AUDIT_OPTIONS": '{"path": "audit.jsonl", "fsync": true}'})
    assert dict(config.section(Section.AUDIT).options) == {"path": "audit.jsonl", "fsync": True}


def test_invalid_json_names_the_variable_and_never_repeats_the_value() -> None:
    with pytest.raises(ConfigurationError) as caught:
        resolve({"EAP_AUDIT_OPTIONS": '{"token": "s3cr3t-value"'})
    assert "EAP_AUDIT_OPTIONS" in str(caught.value)
    assert "s3cr3t-value" not in str(caught.value)


def test_options_must_be_a_json_object() -> None:
    with pytest.raises(ConfigurationError, match="EAP_AUDIT_OPTIONS is not a JSON object"):
        resolve({"EAP_AUDIT_OPTIONS": "[1, 2]"})


# -------------------------------------------------------------------- models


def test_model_aliases_accept_objects_and_plain_ids() -> None:
    config = resolve(
        {
            "EAP_MODEL_PROVIDER": "bedrock",
            "EAP_MODEL_ID": "model-a",
            "EAP_MODEL_ALIASES": (
                '{"fast": "model-f", "judge": {"provider": "anthropic", "model_id": "model-j"}}'
            ),
        }
    )
    assert config.model.resolve("fast") == ModelRef(provider="bedrock", model_id="model-f")
    assert config.model.resolve("judge") == ModelRef(provider="anthropic", model_id="model-j")


@pytest.mark.parametrize(
    "aliases",
    [
        '{"fast": 3}',
        '{"fast": {"provider": "bedrock"}}',
        '{"fast": {"model_id": "m", "region": "us-east-1"}}',
        '{"fast": {"model_id": "m", "provider": 7}}',
        '{"fast": ""}',
    ],
)
def test_malformed_model_aliases_are_rejected(aliases: str) -> None:
    with pytest.raises(ConfigurationError, match="EAP_MODEL_ALIASES"):
        resolve({"EAP_MODEL_ALIASES": aliases})


# ---------------------------------------------------- secrets and third-party


def test_secrets_are_captured_and_masked() -> None:
    config = resolve({"ANTHROPIC_API_KEY": "sk-very-secret"})
    assert config.secrets["anthropic_api_key"].get_secret_value() == "sk-very-secret"
    assert "sk-very-secret" not in repr(config)


def test_region_falls_back_to_the_alternate_name_without_a_warning() -> None:
    assert resolve({"AWS_DEFAULT_REGION": "eu-west-1"}).external.aws_region == "eu-west-1"
    both = resolve({"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "eu-west-1"})
    assert both.external.aws_region == "us-east-1"


def test_proxy_settings_are_read_in_either_case() -> None:
    config = resolve({"https_proxy": "http://proxy:8080", "NO_PROXY": "localhost"})
    assert config.external.https_proxy == "http://proxy:8080"
    assert config.external.no_proxy == "localhost"


def test_tls_ca_bundle_becomes_a_path() -> None:
    assert resolve({"EAP_TLS_CA_BUNDLE": "/etc/ssl/ca.pem"}).tls_ca_bundle == Path(
        "/etc/ssl/ca.pem"
    )


# ------------------------------------------------------- renames and aliases

RENAMED = (
    *(binding for binding in DEFAULT_BINDINGS if binding.key != Key.MODEL_PROVIDER),
    Binding(
        name="EAP_LLM_PROVIDER",
        key=Key.MODEL_PROVIDER,
        description="Provider of the default model alias.",
        deprecated_names=("EAP_MODEL_PROVIDER",),
    ),
)


def resolve_renamed(values: dict[str, str]) -> ServiceConfig:
    return ConfigResolver(MappingConfigSource(values), bindings=RENAMED).resolve()


def test_renaming_a_variable_only_needs_a_new_binding() -> None:
    assert resolve_renamed({"EAP_LLM_PROVIDER": "bedrock"}).model.provider == "bedrock"


def test_a_deprecated_name_still_works_and_warns() -> None:
    with pytest.warns(DeprecationWarning, match="use EAP_LLM_PROVIDER instead"):
        config = resolve_renamed({"EAP_MODEL_PROVIDER": "bedrock"})
    assert config.model.provider == "bedrock"


def test_deprecated_and_current_names_may_agree() -> None:
    with pytest.warns(DeprecationWarning, match="EAP_MODEL_PROVIDER is deprecated"):
        config = resolve_renamed({"EAP_MODEL_PROVIDER": "bedrock", "EAP_LLM_PROVIDER": "bedrock"})
    assert config.model.provider == "bedrock"


def test_deprecated_and_current_names_must_not_disagree() -> None:
    with pytest.raises(ConfigurationError, match="conflicts with EAP_LLM_PROVIDER"):
        resolve_renamed({"EAP_MODEL_PROVIDER": "anthropic", "EAP_LLM_PROVIDER": "bedrock"})


def test_errors_name_the_current_variable_from_the_table() -> None:
    table = (
        *(binding for binding in DEFAULT_BINDINGS if binding.key != Key.PROFILE),
        Binding(name="EAP_PRESET", key=Key.PROFILE, description="Default adapter set."),
    )
    with pytest.raises(ConfigurationError, match="EAP_PRESET is not one of the values it takes"):
        ConfigResolver(MappingConfigSource({"EAP_PRESET": "moon"}), bindings=table).resolve()


def test_a_malformed_table_is_rejected_by_the_resolver() -> None:
    table = [Binding("EAP_A", "k", "d", kind=ValueKind.TEXT), Binding("EAP_A", "k2", "d")]
    with pytest.raises(ValueError, match="more than once"):
        ConfigResolver(MappingConfigSource({}), bindings=table)


# -------------------------------------------------------------- data sources


def test_no_data_sources_are_configured_by_default() -> None:
    assert resolve({}).data_sources == {}


def test_named_data_sources_each_select_an_adapter_with_its_own_options() -> None:
    config = resolve(
        {
            "EAP_DATA_SOURCES": (
                '{"accounts": {"kind": "duckdb_csv", "data_dir": "data"},'
                ' "rates": {"kind": "rest"}}'
            )
        }
    )
    assert config.data_sources == {
        "accounts": ProviderSelection(provider="duckdb_csv", options={"data_dir": "data"}),
        "rates": ProviderSelection(provider="rest"),
    }


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("[]", "is not a JSON object"),
        ('{"accounts": "duckdb_csv"}', "does not say which adapter reads it"),
        ('{"accounts": {"data_dir": "data"}}', "does not say which adapter reads it"),
        ('{"accounts": {"kind": 7}}', "does not say which adapter reads it"),
        ('{"accounts": {"kind": " rest"}}', "whitespace"),
        ('{"": {"kind": "rest"}}', "a data source has a name code cannot ask for"),
        ('{"My Accounts": {"kind": "rest"}}', "a data source has a name code cannot ask for"),
    ],
)
def test_a_malformed_data_source_map_is_rejected(value: str, problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem) as caught:
        resolve({"EAP_DATA_SOURCES": value})
    assert "EAP_DATA_SOURCES" in str(caught.value)


# ------------------------------------------------------------- named secrets


def test_a_secret_variable_supplies_a_named_secret() -> None:
    resolution = ConfigResolver(
        MappingConfigSource({"EAP_SECRET_RATES_TOKEN": "t0ken", "EAP_SECRET_EMPTY": " "})
    ).explain()
    assert resolution.config.secrets["rates_token"].get_secret_value() == "t0ken"
    assert "empty" not in resolution.config.secrets
    assert resolution.origins["secret.rates_token"] == "variable EAP_SECRET_RATES_TOKEN"
    assert "t0ken" not in repr(resolution.config)


def test_a_secret_set_twice_with_different_values_is_a_conflict() -> None:
    same = resolve({"ANTHROPIC_API_KEY": "sk-1", "EAP_SECRET_ANTHROPIC_API_KEY": "sk-1"})
    assert same.secrets["anthropic_api_key"].get_secret_value() == "sk-1"
    with pytest.raises(ConfigurationError, match="both set the secret") as caught:
        resolve({"ANTHROPIC_API_KEY": "sk-1", "EAP_SECRET_ANTHROPIC_API_KEY": "sk-2"})
    assert "sk-" not in str(caught.value)


@pytest.mark.parametrize(
    "name", ["EAP_SECRET_", "EAP_SECRET_rates", "EAP_SECRET__X", "EAP_SECRET_1X"]
)
def test_a_malformed_secret_variable_is_an_unknown_variable(name: str) -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration variable"):
        resolve({name: "value"})


# ------------------------------------------------------- deployed means declared


@pytest.mark.parametrize("runtime", ["AWS_ECS_FARGATE", "AWS_ECS_EC2", "AWS_Lambda_python3.12"])
@pytest.mark.parametrize("declared", [{}, {"EAP_DEPLOYMENT_ENV": "local"}])
def test_a_service_on_managed_compute_cannot_run_as_a_developers_machine(
    runtime: str, declared: dict[str, str]
) -> None:
    with pytest.raises(
        ConfigurationError, match="EAP_DEPLOYMENT_ENV must be dev or prod"
    ) as caught:
        resolve({"AWS_EXECUTION_ENV": runtime, **declared})
    assert "AWS_EXECUTION_ENV" in str(caught.value)


def test_a_service_on_managed_compute_starts_once_it_names_its_environment() -> None:
    config = resolve({"AWS_EXECUTION_ENV": "AWS_ECS_FARGATE", "EAP_DEPLOYMENT_ENV": "dev"})
    assert config.deployment_env is DeploymentEnv.DEV


def test_a_developer_shell_that_aws_marks_is_still_a_developers_machine() -> None:
    # CloudShell and CodeBuild set the same variable; neither is a deployed service.
    assert resolve({"AWS_EXECUTION_ENV": "CloudShell"}).deployment_env is DeploymentEnv.LOCAL
