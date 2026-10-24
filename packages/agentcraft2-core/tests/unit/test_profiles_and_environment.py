"""A server profile says where it runs, and what is for a developer's machine stays there."""

from __future__ import annotations

import warnings

import pytest

from agentcraft2_core.config import ConfigResolver, MappingConfigSource
from agentcraft2_core.config.phoenix import PhoenixSettings
from agentcraft2_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    NoOptions,
    Profile,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from agentcraft2_core.di.providers import ProviderSpec, ServiceProviders
from agentcraft2_core.di.selection import (
    check_allowed,
    check_infrastructure_options,
    selected_adapters,
)
from agentcraft2_core.integrations.http import require_serving_profile
from agentcraft2_core.integrations.langgraph.phoenix import phoenix_tracing


def _resolve(values: dict[str, str]) -> ServiceConfig:
    return ConfigResolver(MappingConfigSource(values)).resolve()


# ------------------------------------------------------ the environment is named on a server


@pytest.mark.parametrize("profile", ["aws", "agentcore"])
def test_a_server_profile_without_an_environment_is_refused(profile: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _resolve({"AC2_PROFILE": profile})
    assert "AC2_DEPLOYMENT_ENV is not set" in caught.value.message
    assert "AC2_DEPLOYMENT_ENV=local" in (caught.value.fix or "")


@pytest.mark.parametrize("environment", ["dev", "prod", "local"])
def test_a_server_profile_that_names_its_environment_resolves(environment: str) -> None:
    # Naming local is deliberate: a developer runs the server profile on their own machine.
    config = _resolve({"AC2_PROFILE": "aws", "AC2_DEPLOYMENT_ENV": environment})
    assert config.deployment_env is DeploymentEnv(environment)


def test_the_local_profile_still_defaults_to_a_developers_machine() -> None:
    assert _resolve({}).deployment_env is DeploymentEnv.LOCAL


def test_agentcore_starts_from_the_aws_adapters() -> None:
    aws = _resolve({"AC2_PROFILE": "aws", "AC2_DEPLOYMENT_ENV": "dev"})
    agentcore = _resolve({"AC2_PROFILE": "agentcore", "AC2_DEPLOYMENT_ENV": "dev"})
    assert agentcore.profile is Profile.AGENTCORE
    assert {s: p.provider for s, p in agentcore.sections.items()} == {
        s: p.provider for s, p in aws.sections.items()
    }
    assert agentcore.model.provider == aws.model.provider


def test_a_blank_environment_in_the_process_does_not_hide_the_one_in_the_file() -> None:
    from agentcraft2_core.config import LayeredConfigSource

    source = LayeredConfigSource(
        MappingConfigSource({"AC2_DEPLOYMENT_ENV": ""}),
        MappingConfigSource({"AC2_PROFILE": "aws", "AC2_DEPLOYMENT_ENV": "prod"}),
    )
    assert ConfigResolver(source).resolve().deployment_env is DeploymentEnv.PROD


# ------------------------------------------------------------------- conversation content


def test_a_model_trace_file_is_refused_in_production() -> None:
    with pytest.raises(ConfigurationError, match="a production service does not"):
        _resolve(
            {
                "AC2_PROFILE": "aws",
                "AC2_DEPLOYMENT_ENV": "prod",
                "AC2_MODEL_TRACE_PATH": "trace.jsonl",
            }
        )


def test_a_model_trace_file_in_dev_is_allowed_with_a_warning() -> None:
    values = {
        "AC2_PROFILE": "aws",
        "AC2_DEPLOYMENT_ENV": "dev",
        "AC2_MODEL_TRACE_PATH": "trace.jsonl",
    }
    resolution = ConfigResolver(MappingConfigSource(values)).explain()
    assert resolution.config.model_trace_path is not None
    assert any("complete conversations" in note for note in resolution.warnings)


def test_a_model_trace_file_on_a_developers_machine_needs_no_warning() -> None:
    values = {"AC2_MODEL_TRACE_PATH": ".agentcraft2/model-trace.jsonl"}
    assert ConfigResolver(MappingConfigSource(values)).explain().warnings == ()


async def test_phoenix_content_capture_is_refused_in_production() -> None:
    config = ServiceConfig(deployment_env=DeploymentEnv.PROD)
    with pytest.raises(ConfigurationError, match="a production service does not send it"):
        async with phoenix_tracing("agent", config, settings=PhoenixSettings(capture_content=True)):
            pass


# ------------------------------------------------------- the secret that has a standard name


def test_the_secret_twin_of_a_standard_variable_still_works_but_names_the_standard_one() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolution = ConfigResolver(
            MappingConfigSource({"AC2_SECRET_ANTHROPIC_API_KEY": "sk-1"})
        ).explain()
    assert resolution.config.secrets["anthropic_api_key"].get_secret_value() == "sk-1"
    assert resolution.warnings == (
        "AC2_SECRET_ANTHROPIC_API_KEY is deprecated; use ANTHROPIC_API_KEY instead",
    )
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_the_standard_variable_alone_warns_of_nothing() -> None:
    resolution = ConfigResolver(MappingConfigSource({"ANTHROPIC_API_KEY": "sk-1"})).explain()
    assert resolution.warnings == ()


def test_a_secret_with_no_standard_name_is_not_deprecated() -> None:
    resolution = ConfigResolver(MappingConfigSource({"AC2_SECRET_RATES_TOKEN": "t"})).explain()
    assert resolution.warnings == ()


# ----------------------------------------------------------- adapters that name their profile


def _spec(profiles: frozenset[Profile] | None) -> ProviderSpec:
    return ProviderSpec(
        port="identity",
        name="trusting",
        factory=lambda context: object(),
        options=NoOptions,
        profiles=profiles,
    )


def test_an_adapter_that_names_a_profile_is_refused_under_another() -> None:
    spec = _spec(frozenset({Profile.AGENTCORE}))
    with pytest.raises(ConfigurationError) as caught:
        check_allowed(spec, DeploymentEnv.PROD, Profile.AWS)
    assert "runs only under the agentcore profile" in caught.value.message
    assert "'aws'" in caught.value.message
    assert "AC2_PROFILE=agentcore" in (caught.value.fix or "")


def test_an_adapter_that_names_a_profile_runs_under_it() -> None:
    check_allowed(_spec(frozenset({Profile.AGENTCORE})), DeploymentEnv.PROD, Profile.AGENTCORE)


def test_an_adapter_that_names_no_profile_runs_under_any() -> None:
    for profile in Profile:
        check_allowed(_spec(None), DeploymentEnv.DEV, profile)


def test_register_takes_the_profiles_an_adapter_runs_under() -> None:
    providers = ServiceProviders()
    providers.register(
        "identity", "trusting", lambda context: object(), profiles=[Profile.AGENTCORE]
    )
    assert providers.lookup("identity", "trusting").profiles == frozenset({Profile.AGENTCORE})


# ------------------------------------------------------ options that are for a developer's machine


def _with_identity_options(environment: DeploymentEnv, options: dict[str, object]) -> ServiceConfig:
    return ServiceConfig(deployment_env=environment).with_section(
        Section.IDENTITY, ProviderSelection("jwt", options)
    )


@pytest.mark.parametrize("environment", [DeploymentEnv.DEV, DeploymentEnv.PROD])
def test_allow_http_is_refused_on_a_deployed_service(environment: DeploymentEnv) -> None:
    config = _with_identity_options(environment, {"allow_http": True})
    with pytest.raises(ConfigurationError) as caught:
        check_infrastructure_options(config, selected_adapters(config))
    assert "identity (jwt): allow_http" in (caught.value.actual or "")


def test_allow_http_is_refused_wherever_it_is_nested() -> None:
    config = _with_identity_options(DeploymentEnv.PROD, {"exchange": {"allow_http": True}})
    with pytest.raises(ConfigurationError, match="developer's machine"):
        check_infrastructure_options(config, selected_adapters(config))


def test_allow_http_set_to_false_is_not_an_error() -> None:
    config = _with_identity_options(DeploymentEnv.PROD, {"allow_http": False})
    check_infrastructure_options(config, selected_adapters(config))


def test_allow_http_is_a_developers_choice_on_a_developers_machine() -> None:
    config = _with_identity_options(DeploymentEnv.LOCAL, {"allow_http": True})
    check_infrastructure_options(config, selected_adapters(config))


# ----------------------------------------------------------------- listening for AgentCore


def test_listening_for_agentcore_needs_the_agentcore_profile() -> None:
    config = ServiceConfig(profile=Profile.AWS, deployment_env=DeploymentEnv.PROD)
    with pytest.raises(ConfigurationError) as caught:
        require_serving_profile("agentcore", config)
    assert "AC2_PROFILE=agentcore" in (caught.value.fix or "")


def test_listening_for_agentcore_under_the_agentcore_profile_is_fine() -> None:
    config = ServiceConfig(profile=Profile.AGENTCORE, deployment_env=DeploymentEnv.PROD)
    require_serving_profile("agentcore", config)


def test_the_standard_serving_profile_fits_any_service_profile() -> None:
    for profile in Profile:
        require_serving_profile("standard", ServiceConfig(profile=profile))
