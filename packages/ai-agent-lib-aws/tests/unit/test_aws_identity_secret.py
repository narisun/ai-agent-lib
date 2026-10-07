"""Identity on AWS: the service's OAuth client secret comes from Secrets Manager."""

from __future__ import annotations

import pytest

from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.secrets_manager import SecretsManagerOptions, SecretsManagerProvider
from ai_agent_lib_aws.testing import FakeSecretsManager, offline_sessions
from ai_agent_lib_core.adapters import ExchangingJwtIdentity
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    ProviderSelection,
    Section,
    ServiceConfig,
    TokenExchanger,
)
from ai_agent_lib_core.di import ServiceContainer, ServiceProviders
from ai_agent_lib_core.kit import BuildContext
from ai_agent_lib_core.testing import Fakes

TENANT = "11111111-1111-1111-1111-111111111111"
AGENT_CLIENT = "22222222-2222-2222-2222-222222222222"
CONFIG = ServiceConfig.for_testing(
    deployment_env=DeploymentEnv.PROD,
    sections={
        Section.SECRETS: ProviderSelection("secrets_manager", {"prefix": "eap/accounts-agent/"}),
        Section.IDENTITY: ProviderSelection(
            "jwt",
            {
                "preset": "entra",
                "tenant_id": TENANT,
                "audience": f"api://{AGENT_CLIENT}",
                "exchange": {"client_id": AGENT_CLIENT, "client_secret": "entra_client_secret"},
            },
        ),
    },
)


def providers(vault: FakeSecretsManager) -> ServiceProviders:
    """The real jwt identity provider over a stand-in for Secrets Manager."""
    fakes = Fakes()
    registry = register_aws_adapters(fakes.providers(), sessions=offline_sessions())
    jwt = ServiceProviders.default().lookup(Section.IDENTITY, "jwt")

    def secrets(context: BuildContext) -> SecretsManagerProvider:
        options = context.selection.parse_options(SecretsManagerOptions)
        return SecretsManagerProvider(options, offline_sessions(), context.clock, client=vault)

    registry.register(Section.IDENTITY, "jwt", jwt.factory, replace=True)
    registry.register(Section.SECRETS, "secrets_manager", secrets, replace=True)
    return registry


async def test_the_exchange_signs_in_with_the_secret_held_in_secrets_manager() -> None:
    vault = FakeSecretsManager({"eap/accounts-agent/entra_client_secret": "s3cret-value"})
    async with ServiceContainer(CONFIG, providers(vault)) as services:
        assert isinstance(services.identity, ExchangingJwtIdentity)
        assert isinstance(services.identity, TokenExchanger)
        assert "s3cret-value" not in repr(services.identity)
    assert vault.reads == ["eap/accounts-agent/entra_client_secret"]


async def test_a_service_without_its_client_secret_does_not_start() -> None:
    with pytest.raises(ConfigurationError, match="entra_client_secret") as caught:
        await ServiceContainer(CONFIG, providers(FakeSecretsManager({}))).start()
    assert "s3cret" not in str(caught.value)
