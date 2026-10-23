"""Every secrets provider passes the same contract suite."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import SecretStr

from agentcraft2_core.adapters import EnvSecretsProvider
from agentcraft2_core.contracts import SecretsProvider
from agentcraft2_core.testing import FakeSecretsProvider
from agentcraft2_core.testing.contracts import SecretsProviderContract


class TestEnvSecretsProvider(SecretsProviderContract):
    def make_provider(self, secrets: Mapping[str, str]) -> SecretsProvider:
        return EnvSecretsProvider({name: SecretStr(value) for name, value in secrets.items()})


class TestFakeSecretsProvider(SecretsProviderContract):
    def make_provider(self, secrets: Mapping[str, str]) -> SecretsProvider:
        return FakeSecretsProvider(secrets)
