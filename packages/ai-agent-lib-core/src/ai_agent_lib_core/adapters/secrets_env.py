"""A secrets adapter backed by the configuration snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import SecretStr

from ai_agent_lib_core.contracts import ConfigurationError

__all__ = ["EnvSecretsProvider"]


class EnvSecretsProvider:
    """Serves the secrets that the configuration resolver captured at startup.

    The values come from the snapshot taken when configuration was resolved,
    not from the live environment, so they cannot change while the process runs.
    """

    def __init__(self, secrets: Mapping[str, SecretStr]) -> None:
        self._secrets: Mapping[str, SecretStr] = MappingProxyType(dict(secrets))

    async def get_secret(self, name: str) -> SecretStr:
        """Return the secret called ``name``.

        Raises:
            ConfigurationError: If no such secret was configured.
        """
        try:
            return self._secrets[name]
        except KeyError:
            raise ConfigurationError(f"secret {name!r} is not configured") from None
