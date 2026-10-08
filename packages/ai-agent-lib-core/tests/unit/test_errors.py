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
    helpers = {"AgentLibError", "causes", "describe", "kind_of", "shown_value"}
    assert set(errors.__all__) == {cls.__name__ for cls in TAXONOMY} | helpers


# ------------------------------------------------- errors explain themselves


def test_an_error_says_what_was_expected_what_was_found_and_how_to_fix_it() -> None:
    error = ConfigurationError(
        "the audit option 'tracing' is not true or false",
        expected="true or false",
        actual="the text 'yes'",
        fix='write "tracing": true',
    )
    assert str(error) == (
        "the audit option 'tracing' is not true or false\n"
        "  expected: true or false\n"
        "  got: the text 'yes'\n"
        '  fix: write "tracing": true'
    )
    assert error.summary == (
        "the audit option 'tracing' is not true or false (expected true or false; "
        "got the text 'yes'; fix write \"tracing\": true)"
    )


def test_a_plain_error_reads_as_its_message() -> None:
    assert str(IntegrityError("the pin does not match")) == "the pin does not match"
    assert IntegrityError("the pin does not match").summary == "the pin does not match"


def test_the_detail_never_reaches_the_message() -> None:
    error = ValidationFailed("the reply does not match", detail="the model said: secret plan")
    assert "secret plan" not in str(error)
    assert "secret plan" not in error.summary
    assert error.detail == "the model said: secret plan"


def test_a_denial_shows_its_reason() -> None:
    error = PolicyDenied("no rule allows this", reason_code="no_rule", fix="add a rule")
    assert str(error) == "no rule allows this\n  reason: no_rule\n  fix: add a rule"


def test_an_error_survives_pickling_with_its_facts() -> None:
    import pickle

    error = PolicyDenied("no", reason_code="no_rule", expected="a rule", detail="d")
    copy = pickle.loads(pickle.dumps(error))  # noqa: S301 - our own object
    assert (copy.message, copy.reason_code, copy.expected, copy.detail) == (
        "no",
        "no_rule",
        "a rule",
        "d",
    )
    assert str(copy) == str(error)


def test_describe_names_the_type_and_removes_credentials() -> None:
    error = OSError("could not reach https://bob:hunter2@db.example/ with token=abc123")
    described = errors.describe(error)
    assert described.startswith("OSError: could not reach https://[redacted]@db.example/")
    assert "hunter2" not in described
    assert "abc123" not in described
    assert errors.describe(ValueError()) == "ValueError"
    assert errors.describe(ValidationFailed("bad", detail="private")) == "ValidationFailed: bad"


def test_causes_follow_the_chain_nearest_first() -> None:
    def fail() -> None:
        try:
            try:
                raise KeyError("inner")
            except KeyError as inner:
                raise ValueError("middle") from inner
        except ValueError as middle:
            raise ConfigurationError("outer") from middle

    with pytest.raises(ConfigurationError) as caught:
        fail()
    assert [type(c).__name__ for c in errors.causes(caught.value)] == ["ValueError", "KeyError"]


def test_kind_of_describes_a_value_without_showing_it() -> None:
    assert errors.kind_of("refund") == "text of 6 characters"
    assert errors.kind_of([1, 2]) == "a list of 2 items"
    assert errors.kind_of({"a": 1}) == "an object with 1 key"
    assert errors.kind_of(3.5) == "a number"
    assert errors.kind_of(True) == "true or false"
    assert errors.kind_of(None) == "null"


def test_shown_value_shows_short_values_and_describes_long_ones() -> None:
    assert errors.shown_value("yes") == "'yes'"
    assert errors.shown_value(3) == "3"
    assert errors.shown_value("x" * 41) == "text of 41 characters"
    assert errors.shown_value("token=abc123") == "'token=[redacted]'"
