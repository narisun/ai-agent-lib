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
import sys
from datetime import UTC, datetime
from typing import IO, Any

from ai_agent_lib_core.contracts.redaction import REDACTED, redact
from ai_agent_lib_core.observability.errors import error_fields
from ai_agent_lib_core.pipeline.context import bound_request_context

__all__ = ["LIBRARY_LOGGERS", "REDACTED", "JsonLogFormatter", "configure_logging", "redact"]

LIBRARY_LOGGERS = ("ai_agent_lib_core", "ai_agent_lib_aws", "uvicorn.error")
"""The loggers that speak at the service's level. Every other one starts at WARNING."""

_MAX_MESSAGE = 2_000
_MAX_FIELD = 300
_MAX_FRAMES = 8
_HANDLER_MARK = "_agentlib_handler"
_DETAIL = "detail"
"""An ``extra`` that may hold caller content: written only when details are on."""

# What a record carries by itself. Anything else on it was passed as ``extra``.
# ``color_message`` is the server's own message again, with terminal colours.
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


def _detail(value: object) -> str:
    return redact(str(value))[:_MAX_MESSAGE]


def _scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact(str(value))[:_MAX_FIELD]


class JsonLogFormatter(logging.Formatter):
    """Writes a record as one line of JSON.

    Every line has ``time``, ``level``, ``logger``, ``message`` and
    ``service``. Inside a request it also has ``request_id`` and
    ``application``. Values passed as ``extra`` are added when they are plain
    values. An error adds ``error_type``, ``error_at``, what led to it
    (``error_causes``) and the nearest line of the service's own code
    (``error_in_your_code``). The library's own errors also add their message,
    ``error_expected``, ``error_actual`` and ``error_fix``, which never hold
    caller content. An ``extra`` called ``detail`` may hold caller content and
    is written only when ``details`` is on.

    Args:
        service: The name of the service, written on every line.
        details: Also write what may hold caller content: an error's
            ``detail`` and the messages of other libraries' errors. For a
            developer's machine; leave it off where logs are kept.
    """

    def __init__(self, service: str, *, details: bool = False) -> None:
        super().__init__()
        self._service = service
        self._details = details

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
            if key in _STANDARD or key.startswith("_"):
                continue
            if key == _DETAIL and not self._details:
                continue
            line.setdefault(key, _scalar(value) if key != _DETAIL else _detail(value))
        error = record.exc_info[1] if record.exc_info else None
        if error is not None:
            line.update(error_fields(error, details=self._details))
        return json.dumps(line, ensure_ascii=False, default=str)


def configure_logging(
    service: str,
    *,
    level: int | str = logging.INFO,
    stream: IO[str] | None = None,
    details: bool = False,
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
        details: Also log what may hold caller content, such as the message
            of an unexpected error in a tool. Turn it on only on a developer's
            machine: ``details=config.deployment_env is DeploymentEnv.LOCAL``.

    Returns:
        The handler that was installed.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonLogFormatter(service, details=details))
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
