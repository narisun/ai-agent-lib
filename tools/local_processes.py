"""Supervise local development services without a shell or third-party packages."""

from __future__ import annotations

import http.client
import os
import signal
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tools.local_control import RunControl


@dataclass(frozen=True)
class Service:
    """One foreground Python service and its loopback readiness endpoint."""

    name: str
    command: tuple[str, ...]
    cwd: Path
    port: int
    health_path: str = "/readyz"
    # A full child environment; None keeps normal inheritance. Never log secrets.
    environment: Mapping[str, str] | None = field(default=None, repr=False)


@dataclass
class RunningService:
    """A child owned by this launcher, with its private run log."""

    service: Service
    process: subprocess.Popen[bytes]
    log: Path


def report(message: str) -> None:
    """Write immediate progress to the Command Prompt window."""
    print(message, flush=True)  # noqa: T201 - development CLI output


def check_ports(services: Sequence[Service]) -> None:
    """Fail before starting children if another application owns a required port."""
    ports = [service.port for service in services]
    if len(ports) != len(set(ports)):
        raise ValueError("The selected services need different ports; check the UI configuration.")
    for service in services:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", service.port))
            except OSError:
                raise ValueError(
                    f"Port {service.port} for {service.name} is in use. "
                    "Stop the existing service or choose the UI-only scenario."
                ) from None


def is_ready(service: Service) -> bool:
    """Check HTTP readiness directly on loopback, bypassing enterprise proxies."""
    connection = http.client.HTTPConnection("127.0.0.1", service.port, timeout=1)
    try:
        connection.request("GET", service.health_path)
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def check_children(children: Sequence[RunningService]) -> None:
    """Surface an unexpected exit instead of leaving a partially working stack."""
    for child in children:
        code = child.process.poll()
        if code is not None:
            raise RuntimeError(
                f"{child.service.name} exited with code {code}. Read its log: {child.log}"
            )


def stop_children(children: Sequence[RunningService]) -> bool:
    """Stop only our children, in reverse dependency order, with a bounded grace period."""
    graceful = True
    for child in reversed(children):
        if child.process.poll() is not None:
            continue
        report(f"Stopping {child.service.name}...")
        try:
            event = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
            child.process.send_signal(event)
            # Core services allow up to 20 seconds for requests in flight, then
            # close their adapters. Leave time for both before forcing a stop.
            child.process.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            graceful = False
            report(f"Forcing {child.service.name} to stop after graceful shutdown was unavailable.")
            child.process.kill()
            child.process.wait(timeout=5)
    return graceful


def serve(
    services: Sequence[Service],
    log_dir: Path,
    *,
    startup_timeout: float = 90,
    smoke_test: bool = False,
) -> None:
    """Start in dependency order; clean up on failure, interruption, or a smoke test.

    Every child runs its own interpreter. Logs are files so all services can
    share one console and Ctrl+C can stop the complete stack.
    """
    check_ports(services)
    log_dir.mkdir(parents=True, exist_ok=False)
    with RunControl(log_dir, [service.name for service in services]).own() as control:
        _serve(services, log_dir, control, startup_timeout=startup_timeout, smoke_test=smoke_test)


def _serve(
    services: Sequence[Service],
    log_dir: Path,
    control: RunControl,
    *,
    startup_timeout: float,
    smoke_test: bool,
) -> None:
    children: list[RunningService] = []
    try:
        for service in services:
            if control.stop_requested():
                return
            log = log_dir / f"{service.name}.log"
            report(f"Starting {service.name}; log: {log}")
            with log.open("wb") as output:
                process = subprocess.Popen(  # noqa: S603 - explicit local Python commands
                    service.command,
                    cwd=service.cwd,
                    env=service.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            children.append(RunningService(service, process, log))
            deadline = time.monotonic() + startup_timeout
            while True:
                if control.stop_requested():
                    return
                check_children(children)
                if is_ready(service):
                    report(f"Ready: {service.name} at http://127.0.0.1:{service.port}")
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{service.name} did not become ready. Read its log: {log}")
                time.sleep(0.2)
        control.update("running")
        report("All selected services are ready. Use stop-local.bat or press Ctrl+C here to stop.")
        if smoke_test:
            report("Startup check passed; stopping the services started by this check.")
            return
        while not control.stop_requested():
            check_children(children)
            time.sleep(0.5)
    finally:
        try:
            control.update("stopping")
        finally:
            # A status-file error must never prevent child cleanup.
            try:
                graceful = stop_children(children)
            except BaseException:
                control.update("shutdown_failed")
                raise
            control.update("stopped" if graceful else "forced")
