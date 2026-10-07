"""The error taxonomy.

Every exception the library raises derives from :class:`AgentLibError`. Two
class attributes tell the resilience layer how an error may be handled, so no
caller has to keep its own list of which errors are safe to retry:

* ``retryable`` - the same call may be attempted again.
* ``fallback_allowed`` - a configured fallback may be used instead.

Only :class:`TransientError` sets either flag. In particular a policy denial is
never retried and never becomes a fallback.
"""

from __future__ import annotations

from typing import ClassVar

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
]


class AgentLibError(Exception):
    """Base class for every error raised by the library."""

    retryable: ClassVar[bool] = False
    fallback_allowed: ClassVar[bool] = False


class ConfigurationError(AgentLibError):
    """Configuration is invalid or missing. Raised at startup."""


class CredentialsExpiredError(AgentLibError):
    """A sign-in session or token has expired.

    Attributes:
        fix_command: The command that renews the credentials, when one is known.
    """

    def __init__(self, message: str, *, fix_command: str | None = None) -> None:
        if fix_command:
            message = f"{message} Run: {fix_command}"
        super().__init__(message)
        self.fix_command = fix_command


class PolicyDenied(AgentLibError):
    """A policy, entitlement or guardrail refused the request.

    Attributes:
        reason_code: A short, stable code that is safe to log and audit.
    """

    def __init__(self, message: str, *, reason_code: str = "denied") -> None:
        super().__init__(message)
        self.reason_code = reason_code


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
