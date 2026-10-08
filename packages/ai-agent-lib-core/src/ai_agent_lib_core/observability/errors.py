"""Explaining an error to the developer who has to fix it.

:func:`explain` turns an error into the text a command line prints when a
service cannot start or a command fails: what went wrong, what was expected
and what was found, how to fix it, what led to it, where it was raised and the
nearest line of the developer's own code. :func:`error_fields` gives the same
facts as fields for a JSON log line.

Neither shows an error's ``detail``, which may hold caller content, unless
``details`` is true: set it only where that content may be shown, such as on
a developer's machine.
"""

from __future__ import annotations

import sys
import sysconfig
import traceback
from pathlib import Path
from typing import IO, Any

from ai_agent_lib_core.contracts import (
    AgentLibError,
    DeploymentEnv,
    PolicyDenied,
    ServiceConfig,
    causes,
    describe,
)
from ai_agent_lib_core.contracts.redaction import redact, shorten

__all__ = ["error_fields", "explain", "raised_at", "report_error", "shows_details", "your_code"]

_MAX_TEXT = 2_000
_MAX_FRAMES = 8
_LIBRARY_PARTS = ("/ai_agent_lib_core/", "/ai_agent_lib_aws/", "/ai_agent_lib_cli/")
_INSTALLED_PARTS = ("/site-packages/", "/dist-packages/")


def _standard_library() -> tuple[str, ...]:
    found = {sysconfig.get_paths().get(name, "") for name in ("stdlib", "platstdlib")}
    return tuple(sorted(p.replace("\\", "/").rstrip("/") + "/" for p in found if p))


_STDLIB = _standard_library()


def _frames(error: BaseException) -> list[traceback.FrameSummary]:
    return list(traceback.extract_tb(error.__traceback__))


def _is_theirs(filename: str) -> bool:
    path = filename.replace("\\", "/")
    if path.startswith("<"):
        return True
    if any(part in path for part in (*_LIBRARY_PARTS, *_INSTALLED_PARTS)):
        return True
    return any(path.startswith(root) for root in _STDLIB)


def _place(frame: traceback.FrameSummary) -> str:
    path = Path(frame.filename)
    try:
        shown = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (OSError, ValueError):
        shown = path.as_posix()
    return f"{shown}:{frame.lineno} in {frame.name}"


def raised_at(error: BaseException) -> str | None:
    """Return where ``error`` was raised: the innermost frame of its traceback."""
    frames = _frames(error)
    return _place(frames[-1]) if frames else None


def your_code(error: BaseException) -> str | None:
    """Return the innermost frame that is neither the library, an installed package nor Python's.

    Looks at the error and then at what led to it, so the line of the
    developer's own code that set things off is found even when the library
    raised the error that was finally reported.
    """
    for candidate in (error, *causes(error)):
        mine = [frame for frame in _frames(candidate) if not _is_theirs(frame.filename)]
        if mine:
            return _place(mine[-1])
    return None


def _notes(error: BaseException) -> list[str]:
    return [redact(str(note)) for note in getattr(error, "__notes__", ()) or ()]


def _cause_text(cause: BaseException, *, details: bool) -> str:
    shown = isinstance(cause, AgentLibError) or details
    text = describe(cause) if shown else type(cause).__name__
    where = raised_at(cause)
    return f"{text} (at {where})" if where and not isinstance(cause, AgentLibError) else text


def explain(error: BaseException, *, details: bool = True) -> str:
    """Return a few lines that tell a developer what went wrong and where to look.

    Args:
        error: The error to explain.
        details: Also show the error's ``detail`` and the messages of other
            libraries' errors that led to it. True by default: this is for a
            developer's terminal. Pass false where the output is kept with the
            service's logs and may not hold caller content.
    """
    if isinstance(error, AgentLibError):
        lines = [f"{type(error).__name__}: {redact(error.message)}"]
        lines += [f"  {label}: {redact(value)}" for label, value in error.facts()]
        if details and error.detail:
            lines.append(f"  detail: {shorten(redact(error.detail), _MAX_TEXT)}")
    else:
        lines = [describe(error, limit=_MAX_TEXT) if details else type(error).__name__]
    lines += [f"  note: {note}" for note in _notes(error)]
    lines += [f"  caused by: {_cause_text(c, details=details)}" for c in causes(error)]
    where = raised_at(error)
    if where:
        lines.append(f"  raised at: {where}")
    mine = your_code(error)
    if mine and mine != where:
        lines.append(f"  your code: {mine}")
    return "\n".join(lines)


def error_fields(error: BaseException, *, details: bool = False) -> dict[str, Any]:
    """Return the facts about ``error`` as fields of a JSON log line.

    The library's own errors give their message and facts, which never hold
    caller content. Other errors give their type and where they were raised;
    their message only when ``details`` is true.
    """
    fields: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error_at": [_place(frame) for frame in _frames(error)[-_MAX_FRAMES:]],
    }
    if isinstance(error, AgentLibError):
        fields["error"] = shorten(redact(error.message), _MAX_TEXT)
        if isinstance(error, PolicyDenied):
            fields["error_reason"] = error.reason_code
        for label, value in (
            ("error_expected", error.expected),
            ("error_actual", error.actual),
            ("error_fix", error.fix),
        ):
            if value:
                fields[label] = shorten(redact(value), _MAX_TEXT)
        if details and error.detail:
            fields["error_detail"] = shorten(redact(error.detail), _MAX_TEXT)
    elif details:
        fields["error"] = describe(error, limit=_MAX_TEXT).split(": ", 1)[-1]
    notes = _notes(error)
    if notes and (isinstance(error, AgentLibError) or details):
        fields["error_notes"] = notes
    chain = [_cause_text(c, details=details) for c in causes(error)]
    if chain:
        fields["error_causes"] = chain
    mine = your_code(error)
    if mine:
        fields["error_in_your_code"] = mine
    return fields


def report_error(
    program: str,
    error: BaseException,
    *,
    details: bool = True,
    stream: IO[str] | None = None,
) -> int:
    """Tell the person who started ``program`` why it stopped, and return its exit code.

    The entry point of a service or a command calls this with the error that
    ended it. The library's own errors are explained in a few lines. Any other
    error is a bug, so with ``details`` on its full traceback comes first.

    Args:
        program: The command's name, put before the explanation.
        error: What ended it.
        details: Show what may hold caller content, and full tracebacks of bugs.
            On for a developer's machine; off where standard error is kept with
            the service's logs.
        stream: Where to write. Standard error by default.

    Returns:
        ``1``, the exit code of a program that failed.
    """
    out = stream if stream is not None else sys.stderr
    if details and not isinstance(error, AgentLibError):
        out.write("".join(traceback.format_exception(error)))
    out.write(f"{program}: {explain(error, details=details)}\n")
    return 1


def shows_details(config: ServiceConfig | None) -> bool:
    """Whether a service may show details: on a developer's machine.

    Before the configuration is known (it failed to load) there is nothing
    a caller sent to hide, so details are shown.
    """
    return config is None or config.deployment_env is DeploymentEnv.LOCAL
