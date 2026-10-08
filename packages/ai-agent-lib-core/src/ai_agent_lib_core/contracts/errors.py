"""The error taxonomy.

Every exception the library raises derives from :class:`AgentLibError`. Two
class attributes tell the resilience layer how an error may be handled, so no
caller has to keep its own list of which errors are safe to retry:

* ``retryable`` - the same call may be attempted again.
* ``fallback_allowed`` - a configured fallback may be used instead.

Only :class:`TransientError` sets either flag. In particular a policy denial is
never retried and never becomes a fallback.

Every error explains itself to the developer who reads it. Its message says
what went wrong; ``expected`` and ``actual`` say what the code needed and what
it found; ``fix`` says what to change. None of these may hold a secret or
anything a caller sent: they are written to logs. Text that may hold caller
content goes in ``detail``, which is shown only where the service is set up
to show it, such as on a developer's machine. Context added on the way out,
such as which adapter was being built, is added with ``add_note``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar, Self

from ai_agent_lib_core.contracts.redaction import redact, shorten

__all__ = [
    "AgentLibError",
    "BudgetExceeded",
    "ConfigurationError",
    "CredentialsExpiredError",
    "ExecutionPaused",
    "IntegrityError",
    "PolicyDenied",
    "TransientError",
    "ValidationFailed",
    "causes",
    "describe",
    "kind_of",
    "shown_value",
]

_MAX_DESCRIBED = 300
_SHOWN_VALUE_LENGTH = 40


class AgentLibError(Exception):
    """Base class for every error raised by the library.

    Args:
        message: What went wrong, in one sentence.
        expected: What the code needed, for example ``"true or false"``.
        actual: What it found instead, for example ``"the text 'yes'"``.
        fix: What the developer should change.
        detail: More about what happened that may hold caller content, such as
            the value a model returned. Never part of ``str(error)``.

    Attributes:
        message: As given.
        expected: As given.
        actual: As given.
        fix: As given.
        detail: As given.
    """

    retryable: ClassVar[bool] = False
    fallback_allowed: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
        fix: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.actual = actual
        self.fix = fix
        self.detail = detail

    def __str__(self) -> str:
        lines = [self.message]
        lines += [f"  {label}: {value}" for label, value in self.facts()]
        return "\n".join(lines)

    def facts(self) -> list[tuple[str, str]]:
        """Return the labelled facts after the message: expected, got and fix."""
        found = [("expected", self.expected), ("got", self.actual), ("fix", self.fix)]
        return [(label, value) for label, value in found if value]

    @property
    def summary(self) -> str:
        """The message and its facts on one line, for a reply or a short report."""
        facts = "; ".join(f"{label} {value}" for label, value in self.facts())
        return f"{self.message} ({facts})" if facts else self.message

    @classmethod
    def from_error(cls, error: AgentLibError) -> Self:
        """Return an error of this kind that says what ``error`` says, facts and detail included."""
        return cls(
            error.message,
            expected=error.expected,
            actual=error.actual,
            fix=error.fix,
            detail=error.detail,
        )

    def __reduce__(self) -> tuple[object, ...]:
        state = dict(self.__dict__)
        return (_rebuild, (type(self), state))


def _rebuild(kind: type[AgentLibError], state: dict[str, object]) -> AgentLibError:
    error = kind.__new__(kind)
    Exception.__init__(error, state.get("message", ""))
    error.__dict__.update(state)
    return error


class ConfigurationError(AgentLibError):
    """Configuration is invalid or missing. Raised at startup."""


class CredentialsExpiredError(AgentLibError):
    """A sign-in session or token has expired.

    Attributes:
        fix_command: The command that renews the credentials, when one is known.
    """

    def __init__(
        self,
        message: str,
        *,
        fix_command: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        fix: str | None = None,
        detail: str | None = None,
    ) -> None:
        if fix_command and not fix:
            fix = f"run: {fix_command}"
        super().__init__(message, expected=expected, actual=actual, fix=fix, detail=detail)
        self.fix_command = fix_command


class PolicyDenied(AgentLibError):
    """A policy, entitlement or guardrail refused the request.

    Attributes:
        reason_code: A short, stable code that is safe to log and audit.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "denied",
        expected: str | None = None,
        actual: str | None = None,
        fix: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, expected=expected, actual=actual, fix=fix, detail=detail)
        self.reason_code = reason_code

    def facts(self) -> list[tuple[str, str]]:
        """Return the reason code first, then expected, got and fix."""
        return [("reason", self.reason_code), *super().facts()]


class BudgetExceeded(AgentLibError):
    """A call or token limit has been reached."""


class ExecutionPaused(AgentLibError):
    """The execution pause is set, so no new work may start."""


class TransientError(AgentLibError):
    """Throttling, a timeout or a server error that may clear on its own."""

    retryable: ClassVar[bool] = True
    fallback_allowed: ClassVar[bool] = True


class IntegrityError(AgentLibError):
    """Evidence could not be recorded, or a hash or pin did not match."""


class ValidationFailed(AgentLibError):
    """Structured output or a schema check failed."""


def kind_of(value: object) -> str:
    """Describe a value by its kind and size, without showing it.

    For a caller's or a model's value, where the value itself may not be shown:
    ``"text of 6 characters"``, ``"a list of 3 items"``, ``"a number"``.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return f"text of {len(value)} character{'s' if len(value) != 1 else ''}"
    if isinstance(value, dict):
        return f"an object with {len(value)} key{'s' if len(value) != 1 else ''}"
    if isinstance(value, list | tuple):
        return f"a list of {len(value)} item{'s' if len(value) != 1 else ''}"
    return f"a {type(value).__name__}"


def describe(error: BaseException, *, limit: int = _MAX_DESCRIBED) -> str:
    """Return ``Type: message`` for an error, with credentials removed and cut short.

    For the library's own errors only the message is used, never ``detail``.
    """
    text = error.message if isinstance(error, AgentLibError) else str(error)
    text = shorten(" ".join(redact(text).split()), limit)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def causes(error: BaseException) -> Iterator[BaseException]:
    """Yield what led to ``error``, nearest first: its cause, that one's cause, and so on."""
    seen = {id(error)}
    current: BaseException | None = error
    while current is not None:
        nxt = current.__cause__
        if nxt is None and not current.__suppress_context__:
            nxt = current.__context__
        if nxt is None or id(nxt) in seen:
            return
        seen.add(id(nxt))
        yield nxt
        current = nxt


def shown_value(value: object) -> str:
    """Show a wrong configuration value when it is short and plain; describe it otherwise.

    Configuration names secrets rather than holding them, but a value pasted
    into the wrong place could still be one, so long text is described and
    anything shaped like a credential is removed.
    """
    if isinstance(value, bool | int | float) or value is None:
        return repr(value)
    if isinstance(value, str) and len(value) <= _SHOWN_VALUE_LENGTH:
        return repr(redact(value))
    return kind_of(value)
