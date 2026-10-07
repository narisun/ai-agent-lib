"""Every secrets provider passes the same contract suite."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import SecretStr

from ai_agent_lib_core.adapters import EnvSecretsProvider
from ai_agent_lib_core.contracts import SecretsProvider
from ai_agent_lib_core.testing import FakeSecretsProvider
from ai_agent_lib_core.testing.contracts import SecretsProviderContract


class TestEnvSecretsProvider(SecretsProviderContract):
    def make_provider(self, secrets: Mapping[str, str]) -> SecretsProvider:
        return EnvSecretsProvider({name: SecretStr(value) for name, value in secrets.items()})


class TestFakeSecretsProvider(SecretsProviderContract):
    def make_provider(self, secrets: Mapping[str, str]) -> SecretsProvider:
        return FakeSecretsProvider(secrets)
