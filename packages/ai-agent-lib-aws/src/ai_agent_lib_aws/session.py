"""One AWS session for a service, and the rules every AWS client follows.

Nothing here reads the process environment. The profile, the region, the CA
files and the proxy all come from resolved configuration, so a value written
in a ``.env`` file works exactly like one that was exported.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

import boto3
from botocore import exceptions as aws
from botocore.config import Config

from ai_agent_lib_core.contracts import (
    AgentLibError,
    ConfigurationError,
    CredentialsExpiredError,
    ExternalSettings,
    TransientError,
)

__all__ = ["AwsSessionFactory", "classify_aws_error"]

T = TypeVar("T")

# The services that sit behind the enterprise CA file as well as the AWS-wide one.
_BEDROCK_SERVICES = frozenset({"bedrock", "bedrock-runtime"})

_EXPIRED = frozenset({"ExpiredToken", "ExpiredTokenException", "RequestExpired"})
_REJECTED = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "InvalidClientTokenId",
        "InvalidSignatureException",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
    }
)
_TRANSIENT = frozenset(
    {
        "InternalFailure",
        "InternalServerException",
        "InternalServiceError",
        "LimitExceededException",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "RequestLimitExceeded",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)
_SERVER_ERROR, _TOO_MANY = 500, 429


def _error_code(error: aws.ClientError) -> tuple[str, int]:
    response = error.response if isinstance(error.response, Mapping) else {}
    code = response.get("Error", {}).get("Code", "")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return (code if isinstance(code, str) else ""), (status if isinstance(status, int) else 0)


def classify_aws_error(
    error: BaseException, *, what: str, sign_in: str | None = None
) -> AgentLibError | None:
    """Map an AWS SDK error to the library's taxonomy.

    The SDK's own message is never copied into the new error, because it can
    repeat parts of the request. Only the error code and class name are kept.

    Args:
        error: The error the SDK raised.
        what: Names the adapter and the operation, for the message.
        sign_in: The command that renews an expired sign-in, when one is known.

    Returns:
        The error to raise in its place, or ``None`` if ``error`` is not one
        the SDK raises or needs no translation.
    """
    name = type(error).__name__
    if isinstance(error, aws.SSOError | aws.TokenRetrievalError):
        return CredentialsExpiredError(
            f"{what}: the AWS sign-in has expired or is missing ({name}).", fix_command=sign_in
        )
    if isinstance(error, aws.ClientError):
        code, status = _error_code(error)
        if code in _EXPIRED:
            return CredentialsExpiredError(
                f"{what}: the AWS credentials have expired ({code}).", fix_command=sign_in
            )
        if code in _REJECTED:
            return ConfigurationError(f"{what}: AWS refused the credentials or the access ({code})")
        if code in _TRANSIENT or status >= _SERVER_ERROR or status == _TOO_MANY:
            return TransientError(f"{what}: AWS is throttling or unavailable ({code or status})")
        return None
    if isinstance(
        error, aws.NoCredentialsError | aws.PartialCredentialsError | aws.CredentialRetrievalError
    ):
        return ConfigurationError(f"{what}: no usable AWS credentials were found ({name})")
    if isinstance(error, aws.ProfileNotFound | aws.NoRegionError | aws.UnknownRegionError):
        return ConfigurationError(f"{what}: the AWS profile or region is not valid ({name})")
    if isinstance(error, aws.SSLError):
        return ConfigurationError(
            f"{what}: the service's certificate could not be verified; check the CA file ({name})"
        )
    if isinstance(error, aws.ConnectionError | aws.HTTPClientError):
        return TransientError(f"{what}: AWS could not be reached ({name})")
    return None


def _bypasses_proxy(host: str, no_proxy: str | None) -> bool:
    """Return whether ``host`` is on the list of hosts that are reached directly."""
    for entry in (item.strip().lower() for item in (no_proxy or "").split(",")):
        if not entry:
            continue
        suffix = entry.lstrip("*").lstrip(".")
        if entry == "*" or host == suffix or host.endswith("." + suffix):
            return True
    return False


class AwsSessionFactory:
    """Builds the AWS clients of one service from resolved configuration.

    One client is kept per AWS service; clients are safe to share between
    threads. A call is made with :meth:`invoke`, which keeps it off the event
    loop and maps what the SDK raises to the library's errors.

    Args:
        profile: The named profile to use, for example one signed in through SSO.
            Without it the SDK's default chain applies, which on Fargate is the task role.
        region: The AWS region.
        ca_bundle: A CA file for every AWS client.
        bedrock_ca_bundle: The enterprise CA file. It is used for the Bedrock
            clients only, and for them it wins over ``ca_bundle``.
        proxy: The outbound proxy.
        no_proxy: Hosts that are reached without the proxy, comma-separated.
        connect_timeout: Seconds to wait for a connection.
        read_timeout: Seconds to wait for a response.
        max_attempts: Attempts the SDK makes for one call, the first included.
            Lower it to ``1`` once the pipeline's own resilience stage is in use.
        session_factory: Builds the SDK session. Replaced in tests.

    Raises:
        ConfigurationError: If the profile does not exist.
    """

    def __init__(
        self,
        *,
        profile: str | None = None,
        region: str | None = None,
        ca_bundle: Path | None = None,
        bedrock_ca_bundle: Path | None = None,
        proxy: str | None = None,
        no_proxy: str | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
        max_attempts: int = 3,
        session_factory: Callable[..., Any] = boto3.Session,
    ) -> None:
        self._profile = profile
        self._ca_bundle = ca_bundle
        self._bedrock_ca_bundle = bedrock_ca_bundle
        self._proxy = proxy
        self._no_proxy = no_proxy
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_attempts = max_attempts
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()
        try:
            self._session = session_factory(profile_name=profile, region_name=region)
        except aws.BotoCoreError as exc:
            mapped = classify_aws_error(exc, what="the AWS session", sign_in=self.sign_in)
            raise mapped or ConfigurationError(
                f"the AWS session could not be created ({type(exc).__name__})"
            ) from None

    @classmethod
    def from_settings(
        cls,
        external: ExternalSettings,
        *,
        tls_ca_bundle: Path | None,
        session_factory: Callable[..., Any] = boto3.Session,
    ) -> AwsSessionFactory:
        """Build the factory from the shared settings every adapter is given."""
        return cls(
            session_factory=session_factory,
            profile=external.aws_profile,
            region=external.aws_region,
            ca_bundle=external.aws_ca_bundle,
            bedrock_ca_bundle=tls_ca_bundle,
            proxy=external.https_proxy,
            no_proxy=external.no_proxy,
        )

    def __repr__(self) -> str:
        return f"AwsSessionFactory(profile={self._profile!r}, region={self.region!r})"

    @property
    def region(self) -> str | None:
        """The region the clients talk to."""
        region = self._session.region_name
        return region if isinstance(region, str) else None

    @property
    def sign_in(self) -> str | None:
        """The command that renews the sign-in of a named profile."""
        return f"aws sso login --profile {self._profile}" if self._profile else None

    def client(self, service: str) -> Any:
        """Return the client for an AWS service, building it on first use.

        Raises:
            ConfigurationError: If the client cannot be built, for example
                because no region is configured.
        """
        with self._lock:
            if service not in self._clients:
                self._clients[service] = self._build(service)
            return self._clients[service]

    def _verify(self, service: str) -> str | None:
        chosen = self._bedrock_ca_bundle if service in _BEDROCK_SERVICES else None
        chosen = chosen if chosen is not None else self._ca_bundle
        return str(chosen) if chosen is not None else None

    def _build(self, service: str) -> Any:
        what = f"the AWS {service} client"
        try:
            client = self._create(service, proxied=False)
            host = str(client.meta.endpoint_url).split("://", 1)[-1].split("/", 1)[0].lower()
            if self._proxy is not None and not _bypasses_proxy(host.split(":")[0], self._no_proxy):
                client = self._create(service, proxied=True)
        except (aws.BotoCoreError, aws.ClientError, ValueError) as exc:
            mapped = classify_aws_error(exc, what=what, sign_in=self.sign_in)
            raise mapped or ConfigurationError(
                f"{what} could not be created ({type(exc).__name__})"
            ) from None
        return client

    def _create(self, service: str, *, proxied: bool) -> Any:
        config = Config(
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            retries={"mode": "standard", "total_max_attempts": self._max_attempts},
            proxies={"https": self._proxy} if proxied and self._proxy else None,
        )
        return self._session.client(service, config=config, verify=self._verify(service))

    async def invoke(self, what: str, operation: Callable[..., T], /, **arguments: Any) -> T:
        """Call an SDK operation off the event loop and translate what it raises.

        Args:
            what: Names the adapter and the operation, for an error message.
            operation: A method of a client from :meth:`client`.
            **arguments: The operation's arguments.

        Raises:
            CredentialsExpiredError: If the sign-in or the credentials have expired.
            ConfigurationError: If AWS refused the credentials or the access.
            TransientError: If AWS throttled the call or could not be reached.
        """
        try:
            return await asyncio.to_thread(operation, **arguments)
        except (aws.BotoCoreError, aws.ClientError) as exc:
            mapped = classify_aws_error(exc, what=what, sign_in=self.sign_in)
            if mapped is None:
                raise
            raise mapped from None

    def classify(self, error: BaseException, *, what: str) -> AgentLibError | None:
        """Map an SDK error to the taxonomy, naming this session's sign-in command."""
        return classify_aws_error(error, what=what, sign_in=self.sign_in)
