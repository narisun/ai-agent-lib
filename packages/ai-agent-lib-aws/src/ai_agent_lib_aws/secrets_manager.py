"""A secrets provider backed by AWS Secrets Manager."""

from __future__ import annotations

import re
from typing import Any

from botocore import exceptions as aws
from pydantic import Field, SecretStr

from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import Clock, ConfigurationError, OptionsModel

__all__ = ["SecretsManagerOptions", "SecretsManagerProvider"]

_NAME = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,200}$")
_WHAT = "the secrets_manager provider"


class SecretsManagerOptions(OptionsModel):
    """Options of the ``secrets_manager`` secrets provider.

    Attributes:
        prefix: Put before a secret's name to form its ID in Secrets Manager,
            for example ``eap/accounts-agent/``. It keeps one service's
            secrets apart from another's, and an IAM policy can be scoped to it.
        cache_seconds: How long a secret is kept before it is read again, so
            that a rotated secret is picked up without a restart. ``0`` reads
            it every time.
    """

    prefix: str = Field(default="", pattern=r"^[A-Za-z0-9/_+=.@-]{0,200}$")
    cache_seconds: float = Field(default=300.0, ge=0, le=3600)


class SecretsManagerProvider:
    """Reads named secrets from AWS Secrets Manager.

    A secret is text. One that holds binary data is refused, since no adapter
    here could use it.

    Args:
        options: The ID prefix and the cache time.
        sessions: Builds the client and makes its calls.
        clock: Ages the cached secrets.
        client: A Secrets Manager client to use instead of building one.
    """

    def __init__(
        self,
        options: SecretsManagerOptions,
        sessions: AwsSessionFactory,
        clock: Clock,
        *,
        client: Any = None,
    ) -> None:
        self._options = options
        self._sessions = sessions
        self._clock = clock
        self._client = client if client is not None else sessions.client("secretsmanager")
        self._cache: dict[str, tuple[float, SecretStr]] = {}

    def __repr__(self) -> str:
        return f"SecretsManagerProvider(prefix={self._options.prefix!r})"

    async def get_secret(self, name: str) -> SecretStr:
        """Return the secret called ``name``.

        Raises:
            ConfigurationError: If the name is not usable, no such secret
                exists, it is not text, or it may not be read.
            CredentialsExpiredError: If the AWS sign-in has expired.
            TransientError: If Secrets Manager is throttling or unreachable.
        """
        if not _NAME.match(name):
            raise ConfigurationError(f"{_WHAT}: {name!r} cannot be the name of a secret")
        now = self._clock.monotonic()
        cached = self._cache.get(name)
        if cached is not None and now - cached[0] < self._options.cache_seconds:
            return cached[1]
        try:
            reply = await self._sessions.invoke(
                _WHAT, self._client.get_secret_value, SecretId=self._options.prefix + name
            )
        except aws.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                raise ConfigurationError(f"secret {name!r} is not configured") from None
            raise ConfigurationError(
                f"{_WHAT}: secret {name!r} could not be read ({code or 'error'})"
            ) from None
        text = reply.get("SecretString")
        if not isinstance(text, str):
            raise ConfigurationError(f"{_WHAT}: secret {name!r} is not text")
        secret = SecretStr(text)
        self._cache[name] = (now, secret)
        return secret
