"""Running a generated service's own code, in a separate process.

A service of the workspace is run from its source folder with the interpreter
this command runs on, so it works whether or not the service is installed.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import FrameType

from ai_agent_lib_cli.errors import CliError

__all__ = ["GraphReader", "ServiceRunner", "read_graph", "run_code", "run_service"]

ServiceRunner = Callable[[Path, str, Sequence[str]], int]
"""Given a service folder, a module and arguments, runs the module and returns its exit code."""

GraphReader = Callable[[Path, str], str]
"""Given an agent's folder and its package name, returns its graph as a Mermaid diagram."""

_TIMEOUT_SECONDS = 120
_INTERRUPT_GRACE_SECONDS = 1.0
_MODULE = (
    "import runpy, sys; sys.path.insert(0, 'src'); sys.argv = sys.argv[1:]; "
    "runpy.run_module(sys.argv[0], run_name='__main__', alter_sys=True)"
)
# The graph is built from the committed settings with a scripted model, so
# printing it needs no account. Tools of MCP servers are not loaded: that would
# need the servers running.
_GRAPH = """
import asyncio, importlib, pathlib, sys, tempfile
sys.path.insert(0, 'src')
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.testing import load_test_config, scripted_providers

async def main():
    module = importlib.import_module(sys.argv[1] + '.graph')
    with tempfile.TemporaryDirectory() as state:
        config = load_test_config(pathlib.Path('.env.example'), state_dir=pathlib.Path(state))
        async with ServiceContainer(config, scripted_providers()) as services:
            print(module.build_graph(services).get_graph().draw_mermaid())

asyncio.run(main())
"""


def run_code(folder: Path, code: str, *arguments: str) -> str:
    """Run ``code`` in a service's folder and return what it printed.

    Raises:
        CliError: If it cannot be run, fails or takes too long. The message
            ends with the last line the code wrote to standard error.
    """
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter, code of this package
            [sys.executable, "-c", code, *arguments],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(
            f"the code of {folder.name} could not be run ({type(exc).__name__})"
        ) from None
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise CliError(f"the code of {folder.name} failed: {last}")
    return done.stdout


# Requests to end this process that the service must hear about too.
_STOP_SIGNALS = ("SIGTERM", "SIGHUP", "SIGBREAK")


def _interrupt(process: subprocess.Popen[bytes]) -> None:
    """Interrupt the service, if it is still running."""
    with contextlib.suppress(OSError, ValueError):
        if process.poll() is None:
            if sys.platform == "win32":
                process.terminate()
            else:
                process.send_signal(signal.SIGINT)


@contextlib.contextmanager
def _passing_requests_on() -> Iterator[list[subprocess.Popen[bytes]]]:
    """While inside, a request to stop this command stops the started service instead.

    Yields a list to put the service's process in once it is started. Without
    this the command would end and leave the service running, still holding
    its port.

    A request to end the command is passed on at once. An interrupt is
    different: a terminal sends it to this command and to the service alike,
    so the service is given a moment to stop by itself, and is interrupted from
    here only if it is still running after that. A request that arrives before
    the service is started ends the command, since there is nothing else to end.
    """
    started: list[subprocess.Popen[bytes]] = []

    def stop(signum: int, _frame: FrameType | None) -> None:
        if not started:
            raise SystemExit(128 + signum)
        for process in started:
            with contextlib.suppress(OSError):
                process.terminate()

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        if not started:
            raise KeyboardInterrupt
        for process in started:
            later = threading.Timer(_INTERRUPT_GRACE_SECONDS, _interrupt, [process])
            later.daemon = True  # it must not keep the command alive once the service has ended
            later.start()

    handlers = dict.fromkeys(_STOP_SIGNALS, stop) | {"SIGINT": interrupt}
    previous: dict[int, object] = {}
    for name, handler in handlers.items():
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, handler)
        except ValueError:  # not the main thread: there is nothing to pass on from here
            break
    try:
        yield started
    finally:
        for number, earlier in previous.items():
            signal.signal(number, earlier)  # type: ignore[arg-type]


def run_service(folder: Path, module: str, arguments: Sequence[str]) -> int:
    """Run a module of a service until it ends, with this terminal as its own.

    A request to stop this command is passed on to the service, which then
    stops in its own way; the command ends when the service has.

    Returns:
        The module's exit code.

    Raises:
        CliError: If the service's folder is not there.
    """
    if not (folder / "src").is_dir():
        raise CliError(f"{folder} has no src folder; is the service still in the workspace?")
    # The handlers are in place before the service exists, so no request to stop
    # can fall between starting it and being able to pass the request on.
    with _passing_requests_on() as started:
        process = subprocess.Popen(  # noqa: S603 - this interpreter, a module of the service
            [sys.executable, "-c", _MODULE, module, *arguments], cwd=folder
        )
        started.append(process)
        return process.wait()


def read_graph(folder: Path, package: str) -> str:
    """Return the graph of the agent in ``folder`` as a Mermaid diagram."""
    return run_code(folder, _GRAPH, package)
