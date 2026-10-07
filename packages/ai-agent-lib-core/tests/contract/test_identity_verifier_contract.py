"""Every identity verifier passes the same contract suite."""

from __future__ import annotations

from ai_agent_lib_core.adapters import (
    JwtIdentityOptions,
    JwtIdentityVerifier,
    StaticIdentityOptions,
    StaticIdentityVerifier,
    resolve_jwt_options,
)
from ai_agent_lib_core.contracts import IdentityVerifier, Principal
from ai_agent_lib_core.testing import FakeIdentityVerifier, FrozenClock
from ai_agent_lib_core.testing.contracts import IdentityVerifierContract
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

IDP = FakeIdentityProvider()


class TestStaticIdentityVerifier(IdentityVerifierContract):
    def make_verifier(self) -> IdentityVerifier:
        return StaticIdentityVerifier(StaticIdentityOptions(roles=("analyst",)), FrozenClock())

    def trusted_credential(self) -> str | None:
        return None

    def untrusted_credentials(self) -> list[str | None]:
        # Anything that claims to be a development token must be a valid one.
        return ["agentlib-dev.e30.AAAA", "agentlib-dev.only-two-parts", "agentlib-dev.."]


class TestFakeIdentityVerifier(IdentityVerifierContract):
    def make_verifier(self) -> IdentityVerifier:
        return FakeIdentityVerifier({"good-token": Principal(subject="u-1", tenant="t-1")})

    def trusted_credential(self) -> str | None:
        return "good-token"

    def untrusted_credentials(self) -> list[str | None]:
        return [None, "", "bad-token"]


class TestJwtIdentityVerifier(IdentityVerifierContract):
    def make_verifier(self) -> IdentityVerifier:
        options = JwtIdentityOptions.model_validate(IDP.options("api://accounts-mcp"))
        return JwtIdentityVerifier(
            resolve_jwt_options(options), FrozenClock(), transport=IDP.transport
        )

    def trusted_credential(self) -> str | None:
        return IDP.user_token(audience="api://accounts-mcp", subject="u-1", roles=["analyst"])

    def untrusted_credentials(self) -> list[str | None]:
        return [
            None,
            "",
            "not-a-token",
            IDP.user_token(audience="api://another-service", subject="u-1"),
            FakeIdentityProvider().user_token(audience="api://accounts-mcp", subject="u-1"),
        ]
