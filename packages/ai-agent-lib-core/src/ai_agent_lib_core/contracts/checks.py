"""The outcome of checking one thing a service depends on."""

from __future__ import annotations

from dataclasses import dataclass

from ai_agent_lib_core.contracts._validation import require_identifier
from ai_agent_lib_core.contracts.errors import (
    AgentLibError,
    ConfigurationError,
    CredentialsExpiredError,
    TransientError,
    describe,
)

__all__ = ["CheckResult", "generic_fix"]


def generic_fix(problem: BaseException) -> str:
    """Return what a person does about a failure, by its kind, when it names no fix itself."""
    if isinstance(problem, AgentLibError) and problem.fix:
        return problem.fix
    if isinstance(problem, CredentialsExpiredError):
        return "Sign in again, with the command the message names, and run the check again."
    if isinstance(problem, TransientError):
        return (
            "Check that the service is running and can be reached from here "
            "(network, proxy, VPN), then run the check again."
        )
    if isinstance(problem, ConfigurationError):
        return "Correct the setting the message names, in the service's .env or its environment."
    return ""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Whether one dependency of a service is usable, and what to do if it is not.

    Attributes:
        name: What was checked, for example ``audit (jsonl)``.
        ok: Whether it is usable.
        detail: What was found. It never holds a secret or a caller's data.
        fix: What to do about a failure.
    """

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""

    def __post_init__(self) -> None:
        require_identifier("name", self.name)

    @classmethod
    def failed(cls, name: str, problem: BaseException) -> CheckResult:
        """Return a failed check that says what was expected, what was found and what to do."""
        if isinstance(problem, AgentLibError):
            parts = [problem.message]
            parts += [f"expected {problem.expected}"] if problem.expected else []
            parts += [f"got {problem.actual}"] if problem.actual else []
            parts += [str(note) for note in getattr(problem, "__notes__", ())]
            detail = "; ".join(parts)
        else:
            detail = describe(problem)
        return cls(name, ok=False, detail=detail, fix=generic_fix(problem))
