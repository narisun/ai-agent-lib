"""A local audit sink: one JSON object per line, flushed to disk on every write."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ai_agent_lib_core.contracts import (
    AuditOptions,
    AuditRecord,
    ConfigurationError,
    IntegrityError,
)

__all__ = ["JsonlAuditOptions", "JsonlAuditSink"]


class JsonlAuditOptions(AuditOptions):
    """Options for the JSONL audit sink.

    Attributes:
        path: The file to append to. Parent directories are created.
        fsync: Force each record to disk before ``write`` returns.
    """

    path: Path = Path(".agentlib/audit.jsonl")
    fsync: bool = True


class JsonlAuditSink:
    """Appends audit records to a file, one JSON object per line.

    Each record is written with a single ``write`` call on a descriptor opened
    in append mode, so lines from concurrent writers do not interleave. With
    ``fsync`` on, ``write`` returns only after the record has reached the disk.

    This is the reference adapter for local development. It has no delivery
    guarantee beyond the local disk.
    """

    def __init__(self, options: JsonlAuditOptions) -> None:
        self._path = options.path
        self._fsync = options.fsync
        self._fd: int | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """The file records are appended to."""
        return self._path

    async def validate(self) -> None:
        """Check that the file can be opened for appending."""
        try:
            await asyncio.to_thread(self._open)
        except OSError as exc:
            raise ConfigurationError(f"the audit file cannot be opened: {self._path}") from exc

    async def write(self, record: AuditRecord) -> None:
        """Append ``record`` and flush it, or raise ``IntegrityError``."""
        line = json.dumps(record.to_dict(), separators=(",", ":"), ensure_ascii=False) + "\n"
        async with self._lock:
            try:
                await asyncio.to_thread(self._append, line.encode("utf-8"))
            except OSError as exc:
                raise IntegrityError(
                    f"the audit record could not be written to {self._path}"
                ) from exc

    async def aclose(self) -> None:
        """Close the file. Safe to call more than once."""
        async with self._lock:
            fd, self._fd = self._fd, None
            if fd is not None:
                await asyncio.to_thread(os.close, fd)

    def _open(self) -> int:
        if self._fd is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        return self._fd

    def _append(self, data: bytes) -> None:
        fd = self._open()
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short write: {written} of {len(data)} bytes")
        if self._fsync:
            os.fsync(fd)
