"""The wiring of a service's or a command's entry point, in one place.

Every agent and MCP server starts the same way: load the configuration, set
up logging, start telemetry, run a coroutine, stop telemetry, and explain a
failure as an exit code. :func:`run_service` does that for a process that
serves until the platform stops it, :func:`run_command` for a one-shot command
that prints an answer. Part B's AgentCore entry point plugs in here too: it is
another ``serve`` coroutine handed to :func:`run_service`.

:func:`question_input` is the input check an agent's ``/invoke`` body goes
through before the graph runs.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from ai_agent_lib_core.config import load_service_config
from ai_agent_lib_core.contracts import ServiceConfig, ValidationFailed
from ai_agent_lib_core.observability import (
    configure_logging,
    report_error,
    shows_details,
    start_telemetry,
)

__all__ = ["EXIT_INTERRUPTED", "question_input", "run_command", "run_service"]

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
    Traces and metrics go to the collector when the settings turn them on, and
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
        config = load_service_config(env_file)
        configure_logging(application, details=shows_details(config))
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


def question_input(given: Any, *, example: str = "Say hello to Ada.") -> str:
    """Return the question in a request body, or refuse the body.

    Args:
        given: The decoded request body.
        example: A question shown in the explanation of a refused body.

    Raises:
        ValidationFailed: If ``given`` is not an object with a non-empty
            ``"question"`` string.
    """
    question = given.get("question") if isinstance(given, dict) else None
    if not isinstance(question, str) or not question.strip():
        raise ValidationFailed(
            "the input is not a question",
            expected=f'an object such as {{"question": "{example}"}}',
            actual="no non-empty 'question' in it",
        )
    return question
