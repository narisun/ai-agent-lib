"""Every checkpoint backend passes the same contract suite."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core.contracts import CheckpointBackend
from ai_agent_lib_core.integrations.langgraph import (
    InMemoryCheckpointBackend,
    SqliteCheckpointBackend,
    SqliteCheckpointOptions,
)
from ai_agent_lib_core.testing.langgraph_contracts import CheckpointBackendContract


class TestSqliteCheckpointBackend(CheckpointBackendContract):
    async def make_backend(self, tmp_path: Path) -> CheckpointBackend:
        backend = SqliteCheckpointBackend(
            SqliteCheckpointOptions(path=tmp_path / "state" / "checkpoints.sqlite")
        )
        await backend.start()
        return backend


class TestInMemoryCheckpointBackend(CheckpointBackendContract):
    async def make_backend(self, tmp_path: Path) -> CheckpointBackend:
        return InMemoryCheckpointBackend()
