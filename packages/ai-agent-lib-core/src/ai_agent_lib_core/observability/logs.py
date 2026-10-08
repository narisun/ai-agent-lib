"""Operational logs: one JSON object per line on standard output, metadata only.

The audit log says who did what and whether it was allowed. These logs are for
the people who run the service: that it started, that a request took so long
and ended so, that something it depends on failed. Neither holds what a caller
asked or what a model answered.

The library keeps content out of its own log lines by never writing any. Other
libraries write what they like, so three more things apply to every line:
loggers outside the library are quiet below WARNING, anything shaped like a
credential is replaced, and an error is logged by its type and where it was
raised, not by its message.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import IO, Any

from ai_agent_lib_core.contracts import AgentLibError
from ai_agent_lib_core.pipeline.context import bound_request_context

__all__ = ["LIBRARY_LOGGERS", "JsonLogFormatter", "configure_logging", "redact"]

LIBRARY_LOGGERS = ("ai_agent_lib_core", "ai_agent_lib_aws", "uvicorn.error")
"""The loggers that speak at the service's level. Every other one starts at WARNING."""

REDACTED = "[redacted]"
_MAX_MESSAGE = 2_000
_MAX_FIELD = 300
_MAX_FRAMES = 8
_HANDLER_MARK = "_agentlib_handler"

# What a record carries by itself. Anything else on it was passed as ``extra``.
# ``color_message`` is the server's own message again, with terminal colours.
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}

_CREDENTIALS = (
    # An Authorization header value, whatever follows the scheme.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # A JSON web token.
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    # An AWS access key ID.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # key=value and "key": "value" where the key names a credential.
    re.compile(
        r"(?i)([\"']?[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|authorization)"
        r"[A-Za-z0-9_-]*[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
    ),
)


def redact(text: str) -> str:
    """Return ``text`` with anything shaped like a credential replaced."""
    for index, pattern in enumerate(_CREDENTIALS):
        text = pattern.sub(rf"\1{REDACTED}" if index in (0, 3) else REDACTED, text)
    return text


def _scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact(str(value))[:_MAX_FIELD]


def _where(error: BaseException) -> list[str]:
    """Return where an error was raised, innermost last, without any values."""
    frames = traceback.extract_tb(error.__traceback__)[-_MAX_FRAMES:]
    return [f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames]


class JsonLogFormatter(logging.Formatter):
    """Writes a record as one line of JSON.

    Every line has ``time``, ``level``, ``logger``, ``message`` and
    ``service``. Inside a request it also has ``request_id`` and
    ``application``. Values passed as ``extra`` are added when they are plain
    values. An error adds ``error_type`` and ``error_at``; its message is added
    only for the library's own errors, whose messages never hold content.

    Args:
        service: The name of the service, written on every line.
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as one line of JSON."""
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage())[:_MAX_MESSAGE],
            "service": self._service,
        }
        context = bound_request_context()
        if context is not None:
            line["request_id"] = context.request_id
            line["application"] = context.application
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                line.setdefault(key, _scalar(value))
        error = record.exc_info[1] if record.exc_info else None
        if error is not None:
            line["error_type"] = type(error).__name__
            line["error_at"] = _where(error)
            if isinstance(error, AgentLibError):
                line["error"] = redact(str(error))[:_MAX_MESSAGE]
        return json.dumps(line, ensure_ascii=False, default=str)


def configure_logging(
    service: str,
    *,
    level: int | str = logging.INFO,
    stream: IO[str] | None = None,
) -> logging.Handler:
    """Send every log record to ``stream`` as one line of JSON.

    Call it first thing in a service's entry point, before anything else sets
    up logging. Calling it again replaces what the earlier call installed.

    Args:
        service: The name of the service, written on every line.
        level: The level of the library's own loggers and of the server's.
            Loggers of other libraries start at WARNING, or at this level if
            it is higher.
        stream: Where the lines go. Standard output by default: on a container
            platform that is the log.

    Returns:
        The handler that was installed.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonLogFormatter(service))
    setattr(handler, _HANDLER_MARK, True)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    chosen = logging.getLevelName(level) if isinstance(level, str) else level
    if not isinstance(chosen, int):
        raise ValueError(f"{level!r} is not a log level")
    root.setLevel(max(chosen, logging.WARNING))
    for name in LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(chosen)
        logger.propagate = True
        for existing in list(logger.handlers):
            logger.removeHandler(existing)  # one handler, on the root, writes every line
    return handler
