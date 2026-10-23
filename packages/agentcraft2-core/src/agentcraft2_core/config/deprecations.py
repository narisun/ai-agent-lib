"""Deprecated variable names, told to the people who run the service.

The resolver raises a ``DeprecationWarning`` for each deprecated name it reads,
which is for programs that call it. A running service is read through its logs,
and Python hides ``DeprecationWarning`` unless asked, so the service entry point
also logs the same messages through this module.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentcraft2_core.config.resolver import Resolution

__all__ = ["log_deprecation", "log_resolution_warnings"]

_LOG = logging.getLogger("agentcraft2_core.config")
_LOGGED: set[str] = set()
_LOCK = threading.Lock()


def log_deprecation(message: str) -> None:
    """Log one deprecation message as a WARNING line, once per process.

    A message names the deprecated variable it is about, so it is logged the
    first time it is seen and not again, however often configuration is read.
    The Phoenix loader, which resolves outside the binding table, reports
    through this function; the resolver's own warnings arrive through
    :func:`log_resolution_warnings`.

    Args:
        message: The warning, such as ``"EAP_X is deprecated; use AC2_X instead"``.
    """
    with _LOCK:
        if message in _LOGGED:
            return
        _LOGGED.add(message)
    _LOG.warning("%s", message)


def log_resolution_warnings(resolution: Resolution) -> None:
    """Log each warning of a resolution as one WARNING line, once per process.

    A warning names the deprecated variable it is about, so each is logged the
    first time it is seen and not again, however often configuration is
    resolved (a service resolves it at start and a diagnostic may do so again).

    Args:
        resolution: The result of ``ConfigResolver.explain()``.
    """
    for message in resolution.warnings:
        log_deprecation(message)
