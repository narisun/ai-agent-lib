"""A secrets adapter backed by the configuration snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from pydantic import SecretStr

from ai_agent_lib_core.contracts import ConfigurationError

__all__ = ["EnvSecretsProvider"]


class EnvSecretsProvider:
    """Serves the secrets that the configuration resolver captured at startup.

    The values come from the snapshot taken when configuration was resolved,
    not from the live environment, so they cannot change while the process runs.

    Args:
        secrets: The secrets, by name.
        how_to_set: Says how a developer supplies a missing secret, given its
            name. The configuration knows the variable; this adapter does not.
    """

    def __init__(
        self,
        secrets: Mapping[str, SecretStr],
        how_to_set: Callable[[str], str] | None = None,
    ) -> None:
        self._secrets: Mapping[str, SecretStr] = MappingProxyType(dict(secrets))
        self._how_to_set = how_to_set

    async def get_secret(self, name: str) -> SecretStr:
        """Return the secret called ``name``.

        Raises:
            ConfigurationError: If no such secret was configured.
        """
        try:
            return self._secrets[name]
        except KeyError:
            known = ", ".join(repr(known) for known in sorted(self._secrets)) or "none"
            raise ConfigurationError(
                f"secret {name!r} is not configured",
                expected=f"a value for the secret {name!r}",
                actual=f"configured secrets: {known}",
                fix=self._how_to_set(name) if self._how_to_set is not None else None,
            ) from None
