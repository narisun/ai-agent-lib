"""Cooperative shutdown requests for launchers in this checkout, using only files.

An OS lock proves that a supervisor is still alive. A stop request never signals
a stored PID or adopts a service found on a port. Each run owns a unique folder,
so a stale request cannot stop a later run.
"""

from __future__ import annotations

import errno
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def active(directory: Path) -> bool:
    """Check the held lock; stale metadata or recycled PIDs cannot claim ownership."""
    try:
        with (directory / "supervisor.lock").open("r+b") as handle:
            return not _try_lock(handle)
    except FileNotFoundError:
        return False


def status(directory: Path) -> dict[str, object]:
    """Read bounded control metadata; never read service environment or data files."""
    with (directory / "control.json").open(encoding="utf-8") as source:
        value = json.loads(source.read(8192))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError(f"Unrecognized launcher control file: {directory}")
    return value


class RunControl:
    """The supervisor owns status; a separate stop command may request shutdown."""

    def __init__(self, directory: Path, services: Sequence[str]) -> None:
        self.directory = directory
        self.services = list(services)

    def update(self, state: str) -> None:
        """Atomically publish status so readers never see a partially written file."""
        pending = self.directory / "control.tmp"
        pending.write_text(
            json.dumps({"version": 1, "services": self.services, "state": state}),
            encoding="utf-8",
        )
        pending.replace(self.directory / "control.json")

    def stop_requested(self) -> bool:
        """A marker is scoped to this run and contains no instructions or credentials."""
        return (self.directory / "stop.request").is_file()

    @contextmanager
    def own(self) -> Iterator[RunControl]:
        """Keep the lock through cleanup; the OS releases it even after a crash."""
        with (self.directory / "supervisor.lock").open("w+b") as handle:
            handle.write(b"0")
            handle.flush()
            if not _try_lock(handle):
                raise RuntimeError("This run already has a supervisor")
            self.update("starting")
            yield self
