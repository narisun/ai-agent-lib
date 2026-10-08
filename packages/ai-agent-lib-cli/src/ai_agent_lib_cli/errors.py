"""Errors the command line reports, and the exit codes it uses."""

from __future__ import annotations

__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_USAGE", "CliError"]

EXIT_OK = 0
EXIT_FAILED = 1
"""The command was understood and could not be done."""
EXIT_USAGE = 2
"""The command line itself was wrong: an unknown option, a missing argument."""


class CliError(Exception):
    """A problem the developer can fix, reported as one line without a traceback."""
