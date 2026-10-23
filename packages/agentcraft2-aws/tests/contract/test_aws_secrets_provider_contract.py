"""The Secrets Manager provider keeps the secrets contract."""

from __future__ import annotations

from collections.abc import Mapping

from agentcraft2_aws.secrets_manager import SecretsManagerOptions, SecretsManagerProvider
from agentcraft2_aws.testing import FakeSecretsManager, offline_sessions
from agentcraft2_core.contracts import SecretsProvider
from agentcraft2_core.testing import FrozenClock
from agentcraft2_core.testing.contracts import SecretsProviderContract


class TestSecretsManagerProvider(SecretsProviderContract):
    def make_provider(self, secrets: Mapping[str, str]) -> SecretsProvider:
        stored = {f"eap/agent/{name}": value for name, value in secrets.items()}
        return SecretsManagerProvider(
            SecretsManagerOptions(prefix="eap/agent/"),
            offline_sessions(),
            FrozenClock(),
            client=FakeSecretsManager(stored),
        )
