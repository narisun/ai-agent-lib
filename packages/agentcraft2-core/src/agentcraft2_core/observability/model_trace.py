"""A local JSONL destination for explicitly enabled model diagnostics."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Mapping
from pathlib import Path

__all__ = ["JsonlModelTraceSink"]


class JsonlModelTraceSink:
    """Append diagnostic records to a file, one JSON object per line.

    The caller enables content capture explicitly. Use a separate path per process
    and keep the file private. Each write opens and closes the file; this object
    owns no persistent file handle or background worker. Writes through this
    instance are serialized and run outside the event loop.

    Args:
        path: Destination file. Parent directories are created on the first write.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    async def write(self, record: Mapping[str, object]) -> None:
        """Append one record; propagate serialization and filesystem errors."""
        line = json.dumps(dict(record), ensure_ascii=False, allow_nan=False) + "\n"
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(line)
