"""Ask this checkout's local launchers to stop their services and preserve saved data."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.local_control import active, status
from tools.local_processes import report

ROOT = Path(__file__).resolve().parents[1]


def stop(root: Path, *, timeout: float = 150) -> int:
    """Request every active launcher first, then wait for their cleanup in parallel."""
    runs = (root / ".agentlib/local-runs").resolve()
    requested: list[Path] = []
    for control in sorted(runs.glob("*/control.json")):
        directory = control.parent
        if directory.resolve().parent != runs or not active(directory):
            continue
        status(directory)  # Refuse unknown formats before writing a request.
        try:
            with (directory / "stop.request").open("xb"):
                pass
        except FileExistsError:
            pass  # Repeated or concurrent stop commands request the same shutdown.
        requested.append(directory)
        report(f"Shutdown requested. Service logs: {directory}")
    if not requested:
        report("No managed local launchers are running in this checkout.")
        report("For services started before this update or outside the scripts, use their Ctrl+C.")
        return 0
    deadline = time.monotonic() + timeout
    while any(active(directory) for directory in requested):
        if time.monotonic() >= deadline:
            report("Shutdown is still pending. Check the launcher windows and service logs.")
            return 1
        time.sleep(0.2)
    if any(status(directory).get("state") != "stopped" for directory in requested):
        report("A launcher did not confirm graceful shutdown. Check its service logs.")
        return 1
    report("Stopped gracefully. Conversations, traces and checkpoints are kept.")
    return 0


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT) -> int:
    """Stop local runs without loading .env, installing packages or opening databases."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        return stop(root)
    except (OSError, ValueError) as error:
        report(f"Local shutdown could not be confirmed: {error}")
        return 1
    except KeyboardInterrupt:
        report("Stopped waiting. Any shutdown already requested continues in the launcher.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
