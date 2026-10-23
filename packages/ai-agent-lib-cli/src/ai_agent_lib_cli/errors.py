"""Errors the command line reports, and the exit codes it uses."""

from __future__ import annotations

from typing import Self

import click

from ai_agent_lib_core.contracts import AgentLibError
from ai_agent_lib_core.observability import explain

__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_USAGE", "CliError", "report_error"]

EXIT_OK = 0
EXIT_FAILED = 1
"""The command was understood and could not be done."""
EXIT_USAGE = 2
"""The command line itself was wrong: an unknown option, a missing argument."""


class CliError(AgentLibError):
    """A problem the developer can fix, reported in a few lines without a traceback.

    It is one of the library's errors, so it carries the same facts
    (``expected``, ``actual``, ``fix``) and is printed the same way. What sets
    it apart is its audience: its message is written for the person at the
    terminal and names what they typed or which file to change.

    Args:
        message: What went wrong.
        expected: What the command needed.
        actual: What it found instead.
        fix: What to do about it.
        detail: Text that may hold file content, shown only with the facts.
    """

    @classmethod
    def from_error(  # narrower than the base: adds `where`
        cls, error: AgentLibError, *, where: str | None = None
    ) -> Self:
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
            detail=error.detail,
        )


def report_error(problem: AgentLibError) -> int:
    """Print ``problem`` for the terminal and return the exit code to use.

    A :class:`CliError` is shown as written, because its message already
    speaks to the developer. Any other library error is explained with its
    type, facts, notes and causes, as ``explain`` does.
    """
    text = str(problem) if isinstance(problem, CliError) else explain(problem)
    click.echo(f"agentlib: error: {text}", err=True)
    return EXIT_FAILED
