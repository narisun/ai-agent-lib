"""The signing keys an issuer publishes (a JWKS), read when needed and kept for a while.

The cache reads the keys outside its lock: the lock guards only the dictionary
of keys and the time it was read, never the network. Verifications that arrive
while a read is in flight share that one read instead of each starting their
own or queueing behind the lock.

PyJWT is an optional dependency; install the ``jwt`` extra of
``ai-agent-lib-core`` to use this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

import httpx

from ai_agent_lib_core.contracts import Clock, ConfigurationError, PolicyDenied, describe

if TYPE_CHECKING:
    from jwt import PyJWK

__all__ = ["SigningKeys"]

_WHAT = "the jwt identity provider"
_LOG = logging.getLogger(__name__)
_MIN_REFRESH_SECONDS = 60.0


class SigningKeys:
    """The issuer's published keys, read when needed and kept for a while.

    Args:
        url: Where the issuer publishes its keys.
        client: Reaches the issuer.
        clock: Ages the keys.
        keep_seconds: How long the keys are kept before they are read again.
    """

    def __init__(
        self, url: str, client: httpx.AsyncClient, clock: Clock, keep_seconds: int
    ) -> None:
        self._url = url
        self._client = client
        self._clock = clock
        self._keep_seconds = keep_seconds
        self._keys: dict[str, PyJWK] = {}
        self._read_at: float | None = None
        # Guards _keys, _read_at and _reading; it is never held across the network.
        self._lock = asyncio.Lock()
        self._reading: asyncio.Task[None] | None = None

    async def refresh(self) -> None:
        """Read the keys again.

        Raises:
            ConfigurationError: If they cannot be read or none of them is usable.
        """
        keys = await self._read()
        async with self._lock:
            self._keys = keys
            self._read_at = self._clock.monotonic()

    async def get(self, kid: str) -> PyJWK | None:
        """Return the key called ``kid``, reading the keys again if that may help.

        An unknown key ID triggers one more read, because issuers rotate keys,
        but not more often than once a minute, so a stream of made-up key IDs
        cannot be turned into a stream of requests to the issuer.

        Raises:
            PolicyDenied: If no keys are held and the issuer cannot be read.
        """
        async with self._lock:
            if not self._needs_read(kid):
                return self._keys.get(kid)
            reading = self._reading
            if reading is None:
                reading = self._reading = asyncio.create_task(self._read_and_keep())
        # One cancelled verification must not abandon the read the others wait for.
        await asyncio.shield(reading)
        async with self._lock:
            if not self._keys:
                raise PolicyDenied(
                    "the caller's token cannot be checked: the issuer's signing keys "
                    "are not available",
                    reason_code="identity_unavailable",
                )
            # Keys rotate rarely: the ones already held stay in use until a read works.
            return self._keys.get(kid)

    def _needs_read(self, kid: str) -> bool:
        """Decide, with the lock held, whether the keys are stale or ``kid`` is worth a read."""
        age = None if self._read_at is None else self._clock.monotonic() - self._read_at
        stale = age is None or age >= self._keep_seconds
        may_retry = age is None or age >= _MIN_REFRESH_SECONDS
        return stale or (kid not in self._keys and may_retry)

    async def _read_and_keep(self) -> None:
        """Read the keys and replace the held ones; a failure keeps what is held."""
        try:
            await self.refresh()
        except ConfigurationError as problem:
            # Whoever runs the service has to hear of this before the keys rotate.
            _LOG.warning(
                "the signing keys could not be refreshed",
                extra={"keys_held": len(self._keys), "reason": str(problem)},
            )
        finally:
            async with self._lock:
                self._reading = None

    async def _read(self) -> dict[str, PyJWK]:
        """Fetch and parse the published keys, outside the lock."""
        import jwt

        try:
            response = await self._client.get(self._url)
        except httpx.HTTPError as exc:
            raise ConfigurationError(
                f"{_WHAT}: the signing keys could not be read ({describe(exc)})"
            ) from exc
        if response.status_code != httpx.codes.OK:
            raise ConfigurationError(
                f"{_WHAT}: the signing keys could not be read",
                expected="HTTP 200 with a JWKS document",
                actual=f"HTTP {response.status_code} from {self._url}",
                fix="check jwks_url, and whether the issuer is reachable from this service",
            )
        try:
            document = response.json()
        except ValueError as exc:
            raise ConfigurationError(
                f"{_WHAT}: the signing keys could not be read ({describe(exc)})"
            ) from exc
        listed = document.get("keys") if isinstance(document, Mapping) else None
        keys: dict[str, PyJWK] = {}
        for entry in listed if isinstance(listed, list) else ():
            if not isinstance(entry, Mapping) or entry.get("use", "sig") != "sig":
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK.from_dict(dict(entry))
            except jwt.PyJWTError:
                continue  # a key type this library cannot use; tokens signed with it are refused
        if not keys:
            raise ConfigurationError(f"{_WHAT}: the issuer published no usable signing keys")
        return keys
