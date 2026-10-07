"""The error taxonomy matches the table in the specification."""

from __future__ import annotations

import pytest

from ai_agent_lib_core.contracts import errors
from ai_agent_lib_core.contracts.errors import (
    AgentLibError,
    BudgetExceeded,
    ConfigurationError,
    CredentialsExpiredError,
    ExecutionPaused,
    IntegrityError,
    PolicyDenied,
    TransientError,
    ValidationFailed,
)

# exception -> (retryable, fallback_allowed), one row per exception in the spec.
TAXONOMY: dict[type[AgentLibError], tuple[bool, bool]] = {
    ConfigurationError: (False, False),
    CredentialsExpiredError: (False, False),
    PolicyDenied: (False, False),
    BudgetExceeded: (False, False),
    ExecutionPaused: (False, False),
    TransientError: (True, True),
    IntegrityError: (False, False),
    ValidationFailed: (False, False),
}


def _all_subclasses(cls: type[AgentLibError]) -> set[type[AgentLibError]]:
    found: set[type[AgentLibError]] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _all_subclasses(sub)
    return found


def test_every_library_error_has_exactly_one_taxonomy_row() -> None:
    defined = {cls for cls in _all_subclasses(AgentLibError) if cls.__module__ == errors.__name__}
    assert defined == set(TAXONOMY)


@pytest.mark.parametrize(("error", "expected"), TAXONOMY.items())
def test_handling_flags_match_the_taxonomy(
    error: type[AgentLibError], expected: tuple[bool, bool]
) -> None:
    assert (error.retryable, error.fallback_allowed) == expected


def test_a_policy_denial_is_never_retried_and_never_falls_back() -> None:
    assert PolicyDenied.retryable is False
    assert PolicyDenied.fallback_allowed is False


def test_policy_denied_carries_a_reason_code() -> None:
    assert PolicyDenied("no").reason_code == "denied"
    assert PolicyDenied("no", reason_code="role_missing").reason_code == "role_missing"


def test_credentials_expired_names_the_fix_command() -> None:
    error = CredentialsExpiredError("SSO session expired.", fix_command="aws sso login")
    assert error.fix_command == "aws sso login"
    assert "aws sso login" in str(error)


def test_module_exports_are_complete() -> None:
    assert set(errors.__all__) == {cls.__name__ for cls in TAXONOMY} | {"AgentLibError"}
