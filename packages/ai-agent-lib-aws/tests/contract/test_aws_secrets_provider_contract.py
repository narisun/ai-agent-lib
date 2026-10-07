"""The Secrets Manager provider keeps the secrets contract."""

from __future__ import annotations

from collections.abc import Mapping

from ai_agent_lib_aws.secrets_manager import SecretsManagerOptions, SecretsManagerProvider
from ai_agent_lib_aws.testing import FakeSecretsManager, offline_sessions
from ai_agent_lib_core.contracts import SecretsProvider
from ai_agent_lib_core.testing import FrozenClock
from ai_agent_lib_core.testing.contracts import SecretsProviderContract


class TestSecretsManagerProvider(SecretsProviderContract):
    def make_provider(self, secrets: Mapping[str, str]) -> SecretsProvider:
        stored = {f"eap/agent/{name}": value for name, value in secrets.items()}
        return SecretsManagerProvider(
            SecretsManagerOptions(prefix="eap/agent/"),
            offline_sessions(),
            FrozenClock(),
            client=FakeSecretsManager(stored),
        )
