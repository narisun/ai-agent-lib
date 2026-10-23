"""The wiring of a service's or a command's entry point, in one place.

Every agent and MCP server starts the same way: load the configuration, set
up logging, start telemetry, run a coroutine, stop telemetry, and explain a
failure as an exit code. :func:`run_service` does that for a process that
serves until the platform stops it, :func:`run_command` for a one-shot command
that prints an answer. An agent composed as an ``AgentService`` goes through
``run_agent`` instead, which is this wiring plus the agent's HTTP application.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from agentcraft2_core.config import (
    load_service_config,
    log_resolution_warnings,
    service_resolver,
)
from agentcraft2_core.contracts import ServiceConfig
from agentcraft2_core.observability import (
    configure_logging,
    report_error,
    shows_details,
    start_telemetry,
)

__all__ = ["EXIT_INTERRUPTED", "run_command", "run_service"]

EXIT_INTERRUPTED = 130
"""The exit code of a process stopped by an interrupt, as the shell reports it."""


def run_service(
    application: str,
    serve: Callable[[ServiceConfig], Coroutine[Any, Any, None]],
    *,
    env_file: Path | None = Path(".env"),
    program: str | None = None,
) -> int:
    """Run ``serve`` with logging and telemetry around it; return the exit code.

    The configuration is loaded from the process environment and ``env_file``.
    Logging is set up before anything else, so that nothing else sets up
    logging first: one line of JSON per event on standard output, and on a
    developer's machine a line about an error also says what the error said.
    A deprecated variable spelling the configuration was read from is told
    once in the log, after logging is set up, so that the people who run the
    service see it where they look. Traces and metrics go to the collector
    when the settings turn them on, and
    the last spans and metrics are flushed when ``serve`` returns, however it
    returns.

    Args:
        application: The service's name in the registry, in its log lines and
            in its traces.
        serve: Runs the service with the loaded configuration until it stops.
        env_file: The ``.env`` file to read; ``None`` reads the process
            environment only.
        program: The command's name put before an error, when it is not the
            application's name.

    Returns:
        ``0`` when ``serve`` returns, ``1`` when something failed (the failure
        is explained on standard error), :data:`EXIT_INTERRUPTED` when the
        process was interrupted.
    """
    config: ServiceConfig | None = None
    try:
        # explain() rather than resolve(): the same configuration, plus the deprecation
        # warnings, which the service's log shows once logging is set up.
        resolution = service_resolver(env_file).explain()
        config = resolution.config
        configure_logging(application, details=shows_details(config))
        log_resolution_warnings(resolution)
        stop_telemetry = start_telemetry(application, config)
        try:
            asyncio.run(serve(config))
        finally:
            stop_telemetry()  # the last spans and metrics are flushed
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as error:  # noqa: BLE001 - every failure is explained, then the exit code
        # What went wrong, what was expected, and the line of the code to look at.
        return report_error(program or application, error, details=shows_details(config))
    return 0


def run_command(
    program: str,
    run: Callable[[ServiceConfig], Coroutine[Any, Any, str | None]],
    *,
    env_file: Path | None = Path(".env"),
) -> int:
    """Run a one-shot command and print what it returns; return the exit code.

    The configuration is loaded as :func:`run_service` loads it, and a failure
    is explained the same way. Nothing else is set up: a command's answer owns
    standard output, so there is no logging to it and no telemetry.

    Args:
        program: The command's name, put before an error.
        run: Does the work with the loaded configuration. A string it returns
            is written to standard output with a newline after it.
        env_file: The ``.env`` file to read; ``None`` reads the process
            environment only.

    Returns:
        ``0`` when ``run`` returns, ``1`` when something failed,
        :data:`EXIT_INTERRUPTED` when the process was interrupted.
    """
    config: ServiceConfig | None = None
    try:
        config = load_service_config(env_file)
        answer = asyncio.run(run(config))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as error:  # noqa: BLE001 - every failure is explained, then the exit code
        return report_error(program, error, details=shows_details(config))
    if answer is not None:
        sys.stdout.write(answer + "\n")
    return 0
