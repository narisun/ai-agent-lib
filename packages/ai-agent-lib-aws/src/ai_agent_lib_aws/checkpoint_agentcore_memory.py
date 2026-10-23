"""A checkpoint store in Amazon Bedrock AgentCore Memory.

AgentCore Memory keys everything by an actor and a session. The library keys
every thread by a scope: tenant, subject, application and the thread's name,
all in one storage key. This store maps one onto the other on every call: the
tenant and subject become the actor, the application and thread name become
the session, and each is encoded so that two different scopes can never meet
at one memory key. A thread ID that is not a scoped key is refused.

The memory client is the one ``langgraph-checkpoint-aws`` ships. It builds its
own boto3 client, so the service's region, CA file, proxy and timeouts are
passed to it; a named AWS profile cannot be, which only matters on a
developer's machine. With the PostgreSQL store, this is one of the two
modules of the package that import LangGraph.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import string
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from typing import Any, cast

from botocore import exceptions as aws
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph_checkpoint_aws import AgentCoreMemorySaver

from ai_agent_lib_aws.checkpoint_options import AgentCoreMemoryCheckpointOptions
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import (
    AgentCraft2Error,
    ConfigurationError,
    NotStartedError,
    PolicyDenied,
    Scope,
    describe,
)

__all__ = [
    "AgentCoreMemoryCheckpointBackend",
    "AgentCoreMemoryCheckpointOptions",
    "ScopedMemorySaver",
    "encode_component",
    "memory_ids",
]

_WHAT = "the agentcore memory checkpoint store"
# The library's scoped thread key is tenant/subject/application/thread/<name>.
_THREAD_NAMESPACE = "thread"
# AgentCore's limits: an actor ID and a session ID this long at most.
_ACTOR_LIMIT = 255
_SESSION_LIMIT = 100
_PREFIX_KEPT = 7
# The characters kept as they are. "_" starts an escape, "-" and "/" join components.
_SAFE = frozenset(string.ascii_letters + string.digits)
# One actor and one session ID that exist only to ask the memory whether it is there.
_PROBE = "eap-readiness"

SaverFactory = Callable[..., BaseCheckpointSaver[Any]]
"""Builds the memory checkpointer from its keyword arguments."""


def encode_component(value: str) -> str:
    """Return ``value`` with every character outside ``[A-Za-z0-9]`` escaped.

    A character outside the safe set becomes ``_`` and two hex digits for
    each byte of its UTF-8 form; ``_`` itself becomes ``_5f`` and ``-``
    becomes ``_2d``. An escape is always ``_`` followed by two hex digits, so
    the mapping is one-to-one and ``-`` and ``/`` can join components without
    two different pairs of components meeting at one string.
    """
    return "".join(
        ch if ch in _SAFE else "".join(f"_{byte:02x}" for byte in ch.encode("utf-8"))
        for ch in value
    )


def _fit(encoded: str, limit: int) -> str:
    """Return ``encoded``, or a digest of it when it is too long or starts badly.

    AgentCore needs an ID to start with a letter or a digit and to be no
    longer than ``limit``. Otherwise the ID becomes ``h``, the first seven
    characters of the encoding and the SHA-256 digest of the whole of it.
    """
    if len(encoded) <= limit and encoded[0].isalnum():
        return encoded
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return f"h{encoded[:_PREFIX_KEPT]}-{digest}"


def memory_ids(scope: Scope, thread_name: str) -> tuple[str, str]:
    """Return the actor ID and the session ID of a scoped thread in the memory."""
    actor = _fit(
        f"{encode_component(scope.tenant)}/{encode_component(scope.subject)}", _ACTOR_LIMIT
    )
    session = _fit(
        f"{encode_component(scope.application)}-{encode_component(thread_name)}", _SESSION_LIMIT
    )
    return actor, session


def _unscoped(reason: str = "unscoped_thread") -> PolicyDenied:
    return PolicyDenied(
        "the thread ID is not scoped to a tenant, subject and application; "
        "build the run configuration with ServiceContainer.invocation()",
        reason_code=reason,
    )


def _ids_of(thread_id: object) -> tuple[str, str]:
    """Return the memory IDs of a scoped thread key, or refuse."""
    if isinstance(thread_id, str):
        try:
            scope, parts = Scope.parse_key(thread_id)
        except ValueError:
            parts = ()
        else:
            if len(parts) == 2 and parts[0] == _THREAD_NAMESPACE:
                return memory_ids(scope, parts[1])
    raise _unscoped()


class ScopedMemorySaver(BaseCheckpointSaver[Any]):
    """Translates the library's scoped thread keys into the memory's actor and session.

    Every call rewrites ``configurable["thread_id"]`` into the session ID and
    adds ``configurable["actor_id"]``, then hands the call to the memory
    checkpointer. What comes back carries the scoped key again, so the graph
    runtime and the library's own scoped wrapper see the key they gave.

    Args:
        inner: The memory checkpointer that stores the checkpoints.
    """

    def __init__(self, inner: BaseCheckpointSaver[Any]) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner
        # The memory checkpointer deletes a thread of one actor; a plain one knows no actor.
        self._delete_takes_actor = "actor_id" in inspect.signature(inner.delete_thread).parameters

    @property
    def inner(self) -> BaseCheckpointSaver[Any]:
        """The memory checkpointer."""
        return self._inner

    @property
    def config_specs(self) -> list[Any]:
        """The memory checkpointer's configuration fields."""
        return self._inner.config_specs

    # ------------------------------------------------------------ translation

    @staticmethod
    def _outbound(config: RunnableConfig) -> tuple[RunnableConfig, str]:
        """Return the configuration for the memory, and the scoped key it came with."""
        configurable: Mapping[str, Any] = config.get("configurable", {})
        scoped = configurable.get("thread_id")
        actor, session = _ids_of(scoped)
        rewritten = {
            **config,
            "configurable": {**configurable, "thread_id": session, "actor_id": actor},
        }
        return cast(RunnableConfig, rewritten), str(scoped)

    @staticmethod
    def _inbound(config: RunnableConfig | None, scoped: str) -> RunnableConfig | None:
        """Return a configuration from the memory with the scoped key put back."""
        if config is None:
            return None
        configurable = {
            key: value for key, value in config.get("configurable", {}).items() if key != "actor_id"
        }
        configurable["thread_id"] = scoped
        return cast(RunnableConfig, {**config, "configurable": configurable})

    @classmethod
    def _restore(cls, found: CheckpointTuple | None, scoped: str) -> CheckpointTuple | None:
        if found is None:
            return None
        return found._replace(
            config=cls._inbound(found.config, scoped) or found.config,
            parent_config=cls._inbound(found.parent_config, scoped),
        )

    @classmethod
    def _listing(
        cls, config: RunnableConfig | None, before: RunnableConfig | None
    ) -> tuple[RunnableConfig, RunnableConfig | None, str]:
        if config is None:
            raise _unscoped("unscoped_listing")
        rewritten, scoped = cls._outbound(config)
        earlier = cls._outbound(before)[0] if before is not None else None
        return rewritten, earlier, scoped

    # ------------------------------------------------------------------- sync

    def get(self, config: RunnableConfig) -> Checkpoint | None:
        """Return the checkpoint of a scoped thread."""
        found = self.get_tuple(config)
        return found.checkpoint if found is not None else None

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Return the checkpoint tuple of a scoped thread."""
        rewritten, scoped = self._outbound(config)
        return self._restore(self._inner.get_tuple(rewritten), scoped)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - the base class names it
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List the checkpoints of one scoped thread. Listing across threads is refused."""
        rewritten, earlier, scoped = self._listing(config, before)
        for item in self._inner.list(rewritten, filter=filter, before=earlier, limit=limit):
            restored = self._restore(item, scoped)
            if restored is not None:
                yield restored

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store a checkpoint of a scoped thread."""
        rewritten, scoped = self._outbound(config)
        stored = self._inner.put(rewritten, checkpoint, metadata, new_versions)
        return self._inbound(stored, scoped) or stored

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending writes of a scoped thread."""
        rewritten, _ = self._outbound(config)
        self._inner.put_writes(rewritten, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        """Delete a scoped thread from the memory."""
        actor, session = _ids_of(thread_id)
        if self._delete_takes_actor:
            self._inner.delete_thread(session, actor_id=actor)  # type: ignore[call-arg]
        else:
            self._inner.delete_thread(session)

    def get_next_version(self, current: Any, channel: None) -> Any:
        """Return the next channel version, as the memory checkpointer defines it."""
        return self._inner.get_next_version(current, channel)

    # ------------------------------------------------------------------ async

    async def aget(self, config: RunnableConfig) -> Checkpoint | None:
        """Return the checkpoint of a scoped thread."""
        found = await self.aget_tuple(config)
        return found.checkpoint if found is not None else None

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Return the checkpoint tuple of a scoped thread."""
        rewritten, scoped = self._outbound(config)
        return self._restore(await self._inner.aget_tuple(rewritten), scoped)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - the base class names it
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List the checkpoints of one scoped thread. Listing across threads is refused."""
        rewritten, earlier, scoped = self._listing(config, before)
        async for item in self._inner.alist(rewritten, filter=filter, before=earlier, limit=limit):
            restored = self._restore(item, scoped)
            if restored is not None:
                yield restored

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store a checkpoint of a scoped thread."""
        rewritten, scoped = self._outbound(config)
        stored = await self._inner.aput(rewritten, checkpoint, metadata, new_versions)
        return self._inbound(stored, scoped) or stored

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending writes of a scoped thread."""
        rewritten, _ = self._outbound(config)
        await self._inner.aput_writes(rewritten, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete a scoped thread from the memory."""
        actor, session = _ids_of(thread_id)
        if self._delete_takes_actor:
            await self._inner.adelete_thread(session, actor_id=actor)  # type: ignore[call-arg]
        else:
            await self._inner.adelete_thread(session)


class AgentCoreMemoryCheckpointBackend:
    """Stores graph thread state in an AgentCore Memory resource.

    Args:
        options: Which memory, and how it is read and written.
        sessions: Builds the clients and makes the readiness call.
        saver_factory: Builds the memory checkpointer from its keyword
            arguments (``memory_id``, ``max_results``, ``checkpoint_format``
            and the boto3 client arguments). A test replaces it.
    """

    def __init__(
        self,
        options: AgentCoreMemoryCheckpointOptions,
        sessions: AwsSessionFactory,
        *,
        saver_factory: SaverFactory | None = None,
    ) -> None:
        self._options = options
        self._sessions = sessions
        self._saver_factory: SaverFactory = (
            saver_factory if saver_factory is not None else AgentCoreMemorySaver
        )
        self._client: Any = None
        self._saver: ScopedMemorySaver | None = None

    def __repr__(self) -> str:
        return f"AgentCoreMemoryCheckpointBackend(memory_id={self._options.memory_id!r})"

    async def start(self) -> None:
        """Build the memory checkpointer. No call to AWS is made yet.

        Raises:
            ConfigurationError: If no region is configured or the client
                cannot be built.
        """
        if self._saver is not None:
            raise RuntimeError("the checkpoint store is already started")
        if self._sessions.region is None:
            raise ConfigurationError(f"{_WHAT} needs an AWS region to reach the memory")
        options = self._options
        try:
            inner = await asyncio.to_thread(
                self._saver_factory,
                memory_id=options.memory_id,
                max_results=options.max_results,
                checkpoint_format=options.checkpoint_format,
                **self._sessions.client_arguments("bedrock-agentcore"),
            )
        except (aws.BotoCoreError, aws.ClientError, ValueError) as exc:
            mapped = self._sessions.classify(exc, what=_WHAT)
            raise (
                mapped
                if isinstance(mapped, ConfigurationError)
                else ConfigurationError(f"{_WHAT} could not be set up ({describe(exc)})")
            ) from exc
        self._saver = ScopedMemorySaver(inner)

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any]:
        """The memory checkpointer, scoped.

        Raises:
            NotStartedError: If the store has not been started.
        """
        if self._saver is None:
            raise NotStartedError(_WHAT)
        return self._saver

    async def validate(self) -> None:
        """Check that the memory exists and the service may read it.

        One empty read of a probe session asks for the same permission every
        checkpoint read needs and writes nothing.

        Raises:
            ConfigurationError: If the store is not started, the memory does
                not exist, or the service may not use it.
        """
        if self._saver is None:
            raise ConfigurationError(f"{_WHAT} is not started")
        if self._client is None:
            self._client = self._sessions.client("bedrock-agentcore")
        try:
            await self._sessions.invoke(
                _WHAT,
                self._client.list_events,
                memoryId=self._options.memory_id,
                actorId=_PROBE,
                sessionId=_PROBE,
                maxResults=1,
                includePayloads=False,
            )
        except ConfigurationError:
            raise
        except AgentCraft2Error as exc:
            raise ConfigurationError.from_error(exc) from exc
        except (aws.BotoCoreError, aws.ClientError) as exc:
            code = (
                exc.response.get("Error", {}).get("Code")
                if isinstance(exc, aws.ClientError)
                else None
            )
            raise ConfigurationError(
                f"{_WHAT}: memory {self._options.memory_id!r} could not be read "
                f"({code or describe(exc)})",
                fix="check the memory ID and that it is in the configured region",
            ) from exc

    async def aclose(self) -> None:
        """Forget the checkpointer. Safe to call more than once."""
        self._saver, self._client = None, None
