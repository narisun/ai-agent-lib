"""Checkpointing: the scoped wrapper and the local SQLite backend."""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_agent_lib_core.contracts import ConfigurationError, OptionsModel, PolicyDenied, Scope

__all__ = [
    "THREAD_NAMESPACE",
    "InMemoryCheckpointBackend",
    "ScopedCheckpointer",
    "SqliteCheckpointBackend",
    "SqliteCheckpointOptions",
    "scoped_thread_id",
]

THREAD_NAMESPACE = "thread"


def scoped_thread_id(scope: Scope, thread_id: str) -> str:
    """Return the storage key for ``thread_id`` inside ``scope``."""
    return scope.key(THREAD_NAMESPACE, thread_id)


def _require_scoped(thread_id: object) -> str:
    """Return ``thread_id`` if it was built by :func:`scoped_thread_id`, else deny."""
    if isinstance(thread_id, str):
        try:
            _, parts = Scope.parse_key(thread_id)
        except ValueError:
            parts = ()
        if len(parts) == 2 and parts[0] == THREAD_NAMESPACE:
            return thread_id
    raise PolicyDenied(
        "the thread ID is not scoped to a tenant, subject and application; "
        "build the run configuration with ServiceContainer.invocation()",
        reason_code="unscoped_thread",
    )


def _thread_of(config: RunnableConfig) -> str:
    return _require_scoped(config.get("configurable", {}).get("thread_id"))


class ScopedCheckpointer(BaseCheckpointSaver[Any]):
    """Wraps a checkpointer so that every thread ID must be a scoped key.

    A scoped key embeds the tenant, subject and application, so two callers
    can never reach each other's threads, even when they pick the same thread
    name. A raw, unscoped thread ID is refused instead of being stored where
    another tenant could address it.
    """

    def __init__(self, inner: BaseCheckpointSaver[Any]) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner

    @property
    def inner(self) -> BaseCheckpointSaver[Any]:
        """The wrapped checkpointer."""
        return self._inner

    @property
    def config_specs(self) -> list[Any]:
        """The wrapped checkpointer's configuration fields."""
        return self._inner.config_specs

    # ------------------------------------------------------------------- sync

    def get(self, config: RunnableConfig) -> Checkpoint | None:
        """Return the checkpoint for a scoped thread."""
        _thread_of(config)
        return self._inner.get(config)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Return the checkpoint tuple for a scoped thread."""
        _thread_of(config)
        return self._inner.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - the base class names it
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List the checkpoints of one scoped thread. Listing across threads is refused."""
        self._require_thread_for_listing(config)
        return self._inner.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store a checkpoint for a scoped thread."""
        _thread_of(config)
        return self._inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending writes for a scoped thread."""
        _thread_of(config)
        self._inner.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        """Delete a scoped thread."""
        self._inner.delete_thread(_require_scoped(thread_id))

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        """Delete the checkpoints of the given runs."""
        self._inner.delete_for_runs(run_ids)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Copy one scoped thread to another."""
        self._inner.copy_thread(
            _require_scoped(source_thread_id), _require_scoped(target_thread_id)
        )

    def prune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        """Prune the checkpoints of scoped threads."""
        self._inner.prune([_require_scoped(t) for t in thread_ids], strategy=strategy)

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, Any]:
        """Return channel history for a scoped thread."""
        _thread_of(config)
        return self._inner.get_delta_channel_history(config=config, channels=channels)

    def get_next_version(self, current: Any, channel: None) -> Any:
        """Return the next channel version, as the wrapped checkpointer defines it."""
        return self._inner.get_next_version(current, channel)

    def with_allowlist(
        self, extra_allowlist: Collection[tuple[str, ...]]
    ) -> BaseCheckpointSaver[Any]:
        """Return a scoped checkpointer over the wrapped one with a wider allowlist."""
        return ScopedCheckpointer(self._inner.with_allowlist(extra_allowlist))

    # ------------------------------------------------------------------ async

    async def aget(self, config: RunnableConfig) -> Checkpoint | None:
        """Return the checkpoint for a scoped thread."""
        _thread_of(config)
        return await self._inner.aget(config)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Return the checkpoint tuple for a scoped thread."""
        _thread_of(config)
        return await self._inner.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - the base class names it
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List the checkpoints of one scoped thread. Listing across threads is refused."""
        self._require_thread_for_listing(config)
        async for item in self._inner.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store a checkpoint for a scoped thread."""
        _thread_of(config)
        return await self._inner.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending writes for a scoped thread."""
        _thread_of(config)
        await self._inner.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete a scoped thread."""
        await self._inner.adelete_thread(_require_scoped(thread_id))

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        """Delete the checkpoints of the given runs."""
        await self._inner.adelete_for_runs(run_ids)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Copy one scoped thread to another."""
        await self._inner.acopy_thread(
            _require_scoped(source_thread_id), _require_scoped(target_thread_id)
        )

    async def aprune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        """Prune the checkpoints of scoped threads."""
        await self._inner.aprune([_require_scoped(t) for t in thread_ids], strategy=strategy)

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, Any]:
        """Return channel history for a scoped thread."""
        _thread_of(config)
        return await self._inner.aget_delta_channel_history(config=config, channels=channels)

    @staticmethod
    def _require_thread_for_listing(config: RunnableConfig | None) -> None:
        if config is None:
            raise PolicyDenied(
                "listing checkpoints across threads is not allowed",
                reason_code="unscoped_listing",
            )
        _thread_of(config)


class InMemoryCheckpointBackend:
    """A checkpoint backend that keeps state in memory. For tests."""

    def __init__(self) -> None:
        self._saver = InMemorySaver()

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any]:
        """The in-memory checkpointer."""
        return self._saver


class SqliteCheckpointOptions(OptionsModel):
    """Options for the SQLite checkpoint backend.

    Attributes:
        path: The database file. Parent directories are created.
    """

    path: Path = Path(".agentlib/checkpoints.sqlite")


class SqliteCheckpointBackend:
    """Stores graph thread state in a local SQLite file. For local development."""

    def __init__(self, options: SqliteCheckpointOptions) -> None:
        self._path = options.path
        self._connection: aiosqlite.Connection | None = None
        self._saver: AsyncSqliteSaver | None = None

    @property
    def path(self) -> Path:
        """The database file."""
        return self._path

    async def start(self) -> None:
        """Open the database and create its tables.

        Raises:
            ConfigurationError: If the file cannot be opened.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = await aiosqlite.connect(self._path)
            self._saver = AsyncSqliteSaver(self._connection)
            await self._saver.setup()
        except (OSError, aiosqlite.Error) as exc:
            await self.aclose()
            raise ConfigurationError(
                f"the checkpoint database cannot be opened: {self._path}"
            ) from exc

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any]:
        """The SQLite checkpointer.

        Raises:
            RuntimeError: If the backend has not been started.
        """
        if self._saver is None:
            raise RuntimeError("the SQLite checkpoint backend is not started")
        return self._saver

    async def aclose(self) -> None:
        """Close the database. Safe to call more than once."""
        connection, self._connection, self._saver = self._connection, None, None
        if connection is not None:
            await connection.close()
