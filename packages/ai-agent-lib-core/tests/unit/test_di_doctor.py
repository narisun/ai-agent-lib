"""The doctor: every dependency of a service checked and reported, one by one."""

from __future__ import annotations

import json
from pathlib import Path

from ai_agent_lib_core.adapters import FakeChatModelProvider
from ai_agent_lib_core.config import (
    MASKED,
    ConfigResolver,
    Key,
    MappingConfigSource,
    options_key,
    provider_key,
    secret_key,
    service_resolver,
    variable_for,
)
from ai_agent_lib_core.contracts import (
    CheckResult,
    ConfigurationError,
    CredentialsExpiredError,
    ModelSection,
    ProviderSelection,
    Section,
    ServiceConfig,
    TransientError,
)
from ai_agent_lib_core.di import BuildContext, ServiceContainer, diagnose
from ai_agent_lib_core.testing import Fakes, fake_accounts_source


class Checked:
    """An adapter that reports whatever a test tells it to."""

    def __init__(self, problem: Exception | None = None) -> None:
        self.problem = problem
        self.closed = False

    async def validate(self) -> None:
        if self.problem is not None:
            raise self.problem

    async def aclose(self) -> None:
        self.closed = True


def by_name(results: tuple[CheckResult, ...]) -> dict[str, CheckResult]:
    return {result.name: result for result in results}


async def test_every_adapter_is_reported_usable_or_not_with_what_to_do() -> None:
    fakes = Fakes(data_sources={"ledger": fake_accounts_source()})
    audit = Checked(ConfigurationError("the audit stream 'eap-audit' is not active"))
    policy = Checked(TransientError("the policy server could not be reached"))
    secrets = Checked(CredentialsExpiredError("the sign-in expired; run: aws sso login"))
    providers = (
        fakes.providers()
        .register(Section.AUDIT, "fake", lambda context: audit, replace=True)
        .register(Section.POLICY, "fake", lambda context: policy, replace=True)
        .register(Section.SECRETS, "fake", lambda context: secrets, replace=True)
        .register(Section.GUARDRAILS, "fake", lambda context: Checked(), replace=True)
    )
    results = by_name(await diagnose(fakes.config(), providers))

    assert not results["audit (fake)"].ok
    assert results["audit (fake)"].detail == "the audit stream 'eap-audit' is not active"
    assert "Correct the setting" in results["audit (fake)"].fix
    assert "can be reached" in results["policy (fake)"].fix
    assert "Sign in again" in results["secrets (fake)"].fix
    assert (results["guardrails (fake)"].ok, results["guardrails (fake)"].detail) == (
        True,
        "checked",
    )
    assert (results["registry (fake)"].ok, results["registry (fake)"].detail) == (True, "built")
    assert results["data 'ledger' (fake)"].ok
    assert results["model"].detail == "fake serves 'fake-model'"
    # The service was closed again: a check leaves nothing running.
    assert audit.closed


async def test_an_adapter_that_cannot_be_built_is_the_one_result() -> None:
    def broken(context: BuildContext) -> object:
        raise ConfigurationError("invalid options for provider 'fake': path: Field required")

    providers = Fakes().providers().register(Section.AUDIT, "fake", broken, replace=True)
    (result,) = await diagnose(ServiceConfig.for_testing(), providers)
    assert (result.name, result.ok) == ("startup", False)
    assert "Field required" in result.detail
    assert result.fix


async def test_the_model_is_checked_without_calling_it_and_skipped_when_there_is_none() -> None:
    model = FakeChatModelProvider(["never used"])
    fakes = Fakes(model=model)
    results = by_name(await diagnose(fakes.config(), fakes.providers()))
    assert results["model"].ok
    assert all(not built.calls for built in model.models)

    no_alias = ServiceConfig.for_testing(model=ModelSection(provider="fake"))
    assert "model" not in by_name(await diagnose(no_alias, Fakes().providers()))

    other = ServiceConfig.for_testing(model=ModelSection(provider="fake", aliases={})).with_section(
        Section.AUDIT, ProviderSelection("fake")
    )
    assert "model" not in by_name(await diagnose(other, Fakes().providers()))


async def test_a_container_reports_its_own_checks() -> None:
    fakes = Fakes()
    async with fakes.container() as services:
        assert isinstance(services, ServiceContainer)
        results = await services.check()
    assert all(result.ok for result in results)
    assert [result.name for result in results][:2] == ["secrets (fake)", "audit (fake)"]


# ------------------------------------------------------------------- explaining


def test_a_resolution_says_where_each_setting_came_from_and_masks_what_is_sensitive(
    tmp_path: Path,
) -> None:
    source = MappingConfigSource(
        {
            variable_for(Key.MODEL_PROVIDER): "fake",
            variable_for(Key.MODEL_ID): "fake-model",
            variable_for(options_key(Section.AUDIT)): json.dumps({"path": "audit.jsonl"}),
            variable_for(secret_key("rates_token")): "s3cret-value",
            variable_for(Key.HTTPS_PROXY): "http://user:pw@proxy.example.test:8080",
        }
    )
    resolution = ConfigResolver(source).explain()
    shown, origins = resolution.shown, resolution.origins
    assert shown[Key.MODEL_PROVIDER] == "fake"
    assert origins[Key.MODEL_PROVIDER] == f"variable {variable_for(Key.MODEL_PROVIDER)}"
    assert shown[options_key(Section.AUDIT)] == '{"path": "audit.jsonl"}'
    # A default is shown too, with where it came from.
    assert (shown[provider_key(Section.AUDIT)], origins[provider_key(Section.AUDIT)]) == (
        "jsonl",
        "profile local",
    )
    assert (shown[Key.DEPLOYMENT_ENV], origins[Key.DEPLOYMENT_ENV]) == ("local", "default")
    assert shown[secret_key("rates_token")] == MASKED
    assert shown[Key.HTTPS_PROXY] == MASKED
    assert "s3cret-value" not in repr(resolution.shown)
    assert "pw@proxy" not in repr(resolution.shown)


def test_the_service_resolver_reads_the_file_and_rebases_its_paths(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{variable_for(Key.TLS_CA_BUNDLE)}=ca.pem\n", encoding="utf-8")
    resolution = service_resolver(dotenv).explain()
    assert resolution.config.tls_ca_bundle == tmp_path.resolve() / "ca.pem"
    assert resolution.origins[Key.TLS_CA_BUNDLE] == f"variable {variable_for(Key.TLS_CA_BUNDLE)}"
