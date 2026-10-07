"""A policy decision point that asks an Open Policy Agent server.

The server evaluates one rule, by default ``data.agentlib.authz.decision``,
which must produce an object such as::

    {"allow": true, "reason_code": "analysts-read-accounts",
     "obligations": {"mask_columns": ["holder"]}}

Everything else is a deny: a timeout, a transport error, a reply that is not
JSON, a missing or non-boolean ``allow`` and an obligation this library does
not recognise.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import httpx
from pydantic import Field, SecretStr

from ai_agent_lib_core.adapters.http_support import checked_base_url, tls_verification
from ai_agent_lib_core.adapters.policy_cache import PolicyCacheOptions
from ai_agent_lib_core.adapters.policy_documents import parse_obligations
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    Decision,
    IdGenerator,
    PolicyRequest,
)

__all__ = ["OpaPolicyDecisionPoint", "OpaPolicyOptions"]

_WHAT = "the opa policy provider"
_MAX_TEXT = 200


def _plain(value: object) -> str | None:
    """Return ``value`` if it is short, printable text with no padding, otherwise ``None``."""
    if (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_TEXT
        and value.isprintable()
        and value.strip() == value
    ):
        return value
    return None


def _short_name(bundle_name: object) -> str:
    return str(bundle_name).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


class OpaPolicyOptions(PolicyCacheOptions):
    """Options of the ``opa`` policy provider.

    Attributes:
        url: Where the OPA server is.
        package: The Rego package that holds the decision, with ``/`` between parts.
        rule: The name of the rule that produces the decision object.
        timeout_seconds: How long one decision may take.
        ca_file: A CA file for the server, when it is signed by a private authority.
        auth_secret: The name of the secret that holds a bearer token for the server.
        allow_http: Allow a ``url`` without TLS on a host other than this machine.
    """

    url: str = "http://localhost:8181"
    package: str = Field(
        default="agentlib/authz", pattern=r"^[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*$"
    )
    rule: str = Field(default="decision", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout_seconds: float = Field(default=2.0, gt=0)
    ca_file: Path | None = None
    auth_secret: str | None = None
    allow_http: bool = False


class OpaPolicyDecisionPoint:
    """Asks OPA for each decision and fails closed.

    Args:
        options: Where the server is and what to ask it.
        ids: Gives a decision an ID when the server does not supply one.
        token: A bearer token for the server, when it needs one.
        transport: Replaces the network, for tests.

    Raises:
        ConfigurationError: If the options are unsafe or inconsistent.
    """

    def __init__(
        self,
        options: OpaPolicyOptions,
        ids: IdGenerator,
        *,
        token: SecretStr | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = checked_base_url(_WHAT, options.url, allow_http=options.allow_http)
        if options.auth_secret is not None and token is None:
            raise ConfigurationError(f"{_WHAT}: the secret for its token is missing")
        self._decision_url = f"{base}/v1/data/{options.package}/{options.rule}"
        self._health_url = f"{base}/health"
        self._ids = ids
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        self._client = httpx.AsyncClient(
            verify=tls_verification(_WHAT, options.ca_file),
            timeout=options.timeout_seconds,
            follow_redirects=False,
            # The environment is read by the configuration layer only.
            trust_env=False,
            transport=transport,
            headers=headers,
        )

    def __repr__(self) -> str:
        return f"OpaPolicyDecisionPoint(decision_url={self._decision_url!r})"

    async def decide(self, request: PolicyRequest) -> Decision:
        """Return OPA's decision for ``request``, or a deny if there is no clear allow."""
        try:
            response = await self._client.post(
                self._decision_url,
                params={"provenance": "true"},
                json={"input": request.to_input()},
            )
        except httpx.HTTPError:
            return Decision.denied(self._ids.new_id(), "policy_unavailable")
        if response.status_code != httpx.codes.OK:
            return Decision.denied(self._ids.new_id(), "policy_unavailable")
        try:
            reply = response.json()
        except ValueError:
            return Decision.denied(self._ids.new_id(), "policy_malformed")
        if not isinstance(reply, Mapping):
            return Decision.denied(self._ids.new_id(), "policy_malformed")

        decision_id = _plain(reply.get("decision_id")) or self._ids.new_id()
        revision = self._revision(reply.get("provenance"))
        result = reply.get("result")
        # An undefined rule gives no result at all, which is not an allow.
        if not isinstance(result, Mapping) or not isinstance(result.get("allow"), bool):
            return Decision.denied(decision_id, "policy_malformed", bundle_revision=revision)
        reason = _plain(result.get("reason_code"))
        if reason is None and result.get("reason_code") is not None:
            return Decision.denied(decision_id, "policy_malformed", bundle_revision=revision)
        if not result["allow"]:
            return Decision.denied(decision_id, reason or "denied", bundle_revision=revision)
        try:
            obligations = parse_obligations(result.get("obligations"))
        except ValueError:
            return Decision.denied(decision_id, "obligation_unrecognised", bundle_revision=revision)
        return Decision.allowed(
            decision_id,
            reason_code=reason or "allowed",
            obligations=obligations,
            bundle_revision=revision,
        )

    @staticmethod
    def _revision(provenance: object) -> str | None:
        """Return the revision of the loaded bundle or bundles, when OPA reports one."""
        if not isinstance(provenance, Mapping):
            return None
        bundles = provenance.get("bundles")
        if isinstance(bundles, Mapping):
            # A bundle loaded from disk is named by its path; the last part is enough.
            revisions = sorted(
                f"{_short_name(name)}@{bundle['revision']}"
                for name, bundle in bundles.items()
                if isinstance(bundle, Mapping) and isinstance(bundle.get("revision"), str)
            )
            if revisions:
                return ",".join(revisions)
        revision = provenance.get("revision")
        return revision if isinstance(revision, str) and revision else None

    async def validate(self) -> None:
        """Check that the server answers its health endpoint.

        Raises:
            ConfigurationError: If it does not.
        """
        try:
            response = await self._client.get(self._health_url)
        except httpx.HTTPError as exc:
            raise ConfigurationError(
                f"{_WHAT}: the server could not be reached ({type(exc).__name__})"
            ) from None
        if response.status_code != httpx.codes.OK:
            raise ConfigurationError(
                f"{_WHAT}: the server's health check returned HTTP {response.status_code}"
            )

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
