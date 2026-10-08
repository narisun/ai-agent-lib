"""Errors the command line reports, and the exit codes it uses."""

from __future__ import annotations

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

    def __str__(self) -> str:
        facts = [("expected", self.expected), ("got", self.actual), ("fix", self.fix)]
        return "\n".join(
            [self.message, *(f"  {label}: {value}" for label, value in facts if value)]
        )
