"""Errors the command line reports, and the exit codes it uses."""

from __future__ import annotations

from typing import Self

from ai_agent_lib_core.contracts import AgentLibError

__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_USAGE", "CliError"]

EXIT_OK = 0
EXIT_FAILED = 1
"""The command was understood and could not be done."""
EXIT_USAGE = 2
"""The command line itself was wrong: an unknown option, a missing argument."""


class CliError(Exception):
    """A problem the developer can fix, reported in a few lines without a traceback.

    Args:
        message: What went wrong.
        expected: What the command needed.
        actual: What it found instead.
        fix: What to do about it.
    """

    def __init__(
        self,
        message: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
        fix: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.actual = actual
        self.fix = fix

    @classmethod
    def from_error(cls, error: AgentLibError, *, where: str | None = None) -> Self:
        """Return a command-line error that says what ``error`` says, its facts kept apart.

        Args:
            error: The library's error.
            where: What the error is about, put before its message, such as a file.
        """
        message = f"{where}: {error.message}" if where else error.message
        notes = "; ".join(getattr(error, "__notes__", ()))
        return cls(
            f"{message} ({notes})" if notes else message,
            expected=error.expected,
            actual=error.actual,
            fix=error.fix,
        )

    def __str__(self) -> str:
        facts = [("expected", self.expected), ("got", self.actual), ("fix", self.fix)]
        return "\n".join(
            [self.message, *(f"  {label}: {value}" for label, value in facts if value)]
        )
