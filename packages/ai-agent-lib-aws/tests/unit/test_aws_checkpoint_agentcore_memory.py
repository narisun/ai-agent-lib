"""The AgentCore Memory checkpoint store: scoped keys become an actor and a session."""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from botocore.config import Config
from botocore.stub import Stubber
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from ai_agent_lib_aws.checkpoint_agentcore_memory import (
    AgentCoreMemoryCheckpointBackend,
    AgentCoreMemoryCheckpointOptions,
    ScopedMemorySaver,
    encode_component,
    memory_ids,
)
from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_aws.testing import offline_session, offline_sessions
from ai_agent_lib_core.contracts import (
    CheckpointBackend,
    ConfigurationError,
    NotStartedError,
    PolicyDenied,
    ProviderSelection,
    Scope,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.testing import Fakes
from ai_agent_lib_core.testing.langgraph_contracts import CheckpointBackendContract

MEMORY_ID = "eap_checkpoints-0123456789"
# What AgentCore accepts, from the service model of bedrock-agentcore.
ACTOR_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*")
SESSION_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-_]*")
METADATA: Any = {"source": "input", "step": -1, "parents": {}}


def options(**changes: Any) -> AgentCoreMemoryCheckpointOptions:
    settings: dict[str, Any] = {"memory_id": MEMORY_ID}
    settings.update(changes)
    return AgentCoreMemoryCheckpointOptions.model_validate(settings)


def check_ids(actor: str, session: str) -> None:
    """Fail unless AgentCore would accept the actor and session IDs."""
    assert ACTOR_ID.fullmatch(actor), actor
    assert len(actor) <= 255, actor
    assert SESSION_ID.fullmatch(session), session
    assert len(session) <= 100, session


class ActorBoundSaver(InMemorySaver):
    """An in-memory stand-in for the memory checkpointer.

    Like AgentCore Memory it refuses a call without an actor, checks that the
    IDs are ones the service would accept, and keeps each actor's sessions
    apart. It records how it was built and what it deleted.
    """

    def __init__(self, **arguments: Any) -> None:
        super().__init__()
        self.arguments = arguments
        self.actors: set[str] = set()
        self.deleted: list[tuple[str, str]] = []

    def _bound(self, config: RunnableConfig | None) -> RunnableConfig:
        assert config is not None
        configurable = dict(config["configurable"])
        actor, session = configurable.get("actor_id"), configurable.get("thread_id")
        assert isinstance(actor, str), "every call needs actor_id"
        assert isinstance(session, str)
        check_ids(actor, session)
        self.actors.add(actor)
        configurable["thread_id"] = f"{actor}|{session}"
        return {**config, "configurable": configurable}

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return super().get_tuple(self._bound(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - the base class names it
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        earlier = self._bound(before) if before is not None else None
        return super().list(self._bound(config), filter=filter, before=earlier, limit=limit)

    def put(self, config: RunnableConfig, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return super().put(self._bound(config), checkpoint, metadata, new_versions)

    def put_writes(
        self, config: RunnableConfig, writes: Any, task_id: str, task_path: str = ""
    ) -> None:
        super().put_writes(self._bound(config), writes, task_id, task_path)

    def delete_thread(self, thread_id: str, actor_id: str = "") -> None:
        assert actor_id, "deleting a thread needs its actor"
        self.deleted.append((thread_id, actor_id))
        super().delete_thread(f"{actor_id}|{thread_id}")

    async def adelete_thread(self, thread_id: str, actor_id: str = "") -> None:
        self.delete_thread(thread_id, actor_id)


async def started(**changes: Any) -> AgentCoreMemoryCheckpointBackend:
    backend = AgentCoreMemoryCheckpointBackend(
        options(**changes), offline_sessions(), saver_factory=ActorBoundSaver
    )
    await backend.start()
    return backend


class TestAgentCoreMemoryCheckpoints(CheckpointBackendContract):
    """The shipped contract, over the actor-bound stand-in."""

    async def make_backend(self, tmp_path: Path) -> CheckpointBackend:
        return await started()


# ---------------------------------------------------------------- encoding


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("abc123", "abc123"),
        ("_", "_5f"),
        ("-", "_2d"),
        ("a/b", "a_2fb"),
        ("a b", "a_20b"),
        ("é", "_c3_a9"),
        ("accounts-agent", "accounts_2dagent"),
    ],
)
def test_a_component_is_escaped_to_letters_digits_and_escapes(value: str, encoded: str) -> None:
    assert encode_component(value) == encoded
    assert re.fullmatch(r"[A-Za-z0-9]*(_[0-9a-f]{2}[A-Za-z0-9]*)*", encoded)


def test_the_ids_of_a_plain_scope_read_naturally() -> None:
    scope = Scope(tenant="acme", subject="user-42", application="accounts-agent")

    assert memory_ids(scope, "chat-1") == ("acme/user_2d42", "accounts_2dagent-chat_2d1")


@pytest.mark.parametrize(
    ("one", "other"),
    [
        # The separator inside a component, and its escape, cannot meet the joined form.
        (("a/b", "c", "app", "t"), ("a", "b/c", "app", "t")),
        (("a_2fb", "c", "app", "t"), ("a/b", "c", "app", "t")),
        (("a_5f", "c", "app", "t"), ("a_", "c", "app", "t")),
        (("t", "u", "a-b", "c"), ("t", "u", "a", "b-c")),
        (("t", "u", "a_2db", "c"), ("t", "u", "a-b", "c")),
        (("t", "u", "app", "x_"), ("t", "u", "app", "x_5f")),
        (("t-1/u-1", "x", "app-1", "t"), ("t-1", "u-1", "app-1", "t")),
    ],
)
def test_two_different_scopes_never_share_memory_ids(
    one: tuple[str, str, str, str], other: tuple[str, str, str, str]
) -> None:
    first = memory_ids(Scope(*one[:3]), one[3])
    second = memory_ids(Scope(*other[:3]), other[3])

    assert first != second


@pytest.mark.parametrize(
    ("tenant", "subject", "application", "name"),
    [
        ("acme", "user-42", "accounts-agent", "chat-1"),
        ("_underscored", "u", "a", "t"),
        ("-dashed", "u", "a", "t"),
        ("/slashed", "u", "a", "t"),
        ("t", "u", "_app", "t"),
        ("t", "u", "ünïcödé", "名前"),
        ("t", "x" * 300, "a", "t"),
        ("t", "u", "a", "n" * 200),
        ("t" * 128, "u" * 128, "a" * 60, "n" * 60),
    ],
)
def test_the_ids_are_ones_agentcore_accepts(
    tenant: str, subject: str, application: str, name: str
) -> None:
    actor, session = memory_ids(Scope(tenant, subject, application), name)

    check_ids(actor, session)


def test_a_value_that_is_too_long_becomes_a_prefix_and_its_digest() -> None:
    long_name = "n" * 200
    encoded = f"{encode_component('app')}-{encode_component(long_name)}"
    _, session = memory_ids(Scope("t", "u", "app"), long_name)
    _, other = memory_ids(Scope("t", "u", "app"), "n" * 199 + "m")

    assert session == f"happ-nnn-{hashlib.sha256(encoded.encode()).hexdigest()}"
    assert len(session) == 73
    assert session != other


# ----------------------------------------------------------------- the saver


def scoped_key(name: str = "chat", **scope: str) -> str:
    fields = {"tenant": "acme", "subject": "user-42", "application": "accounts-agent", **scope}
    return Scope(**fields).key("thread", name)


def run_config(thread_id: str, **more: Any) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", **more}}


@pytest.mark.parametrize(
    "thread_id",
    [
        "chat",
        "acme/user-42/accounts-agent/chat",
        "acme/user-42/accounts-agent/other/chat",
        "",
        None,
    ],
)
async def test_a_thread_id_that_is_not_a_scoped_key_is_refused(thread_id: object) -> None:
    saver = ScopedMemorySaver(ActorBoundSaver())
    config = run_config(str(thread_id)) if thread_id is not None else {"configurable": {}}

    with pytest.raises(PolicyDenied) as caught:
        saver.get_tuple(config)
    assert caught.value.reason_code == "unscoped_thread"
    with pytest.raises(PolicyDenied) as caught:
        await saver.aget_tuple(config)
    assert caught.value.reason_code == "unscoped_thread"
    with pytest.raises(PolicyDenied) as caught:
        saver.delete_thread(str(thread_id))
    assert caught.value.reason_code == "unscoped_thread"
    with pytest.raises(PolicyDenied) as caught:
        list(saver.list(None))
    assert caught.value.reason_code == "unscoped_listing"
    assert saver.inner.actors == set()  # type: ignore[attr-defined]


async def test_the_saver_rewrites_the_keys_for_the_memory_and_restores_them() -> None:
    inner = ActorBoundSaver()
    saver = ScopedMemorySaver(inner)
    key = scoped_key()
    config = run_config(key)

    stored = await saver.aput(config, empty_checkpoint(), METADATA, {})
    found = await saver.aget_tuple(config)
    listed = [item async for item in saver.alist(config)]
    sync_found = saver.get_tuple(config)

    assert inner.actors == {"acme/user_2d42"}
    assert stored["configurable"]["thread_id"] == key
    assert "actor_id" not in stored["configurable"]
    assert stored["configurable"]["checkpoint_id"]
    assert found is not None
    assert found.config["configurable"]["thread_id"] == key
    assert "actor_id" not in found.config["configurable"]
    assert found.parent_config is None
    assert [item.config for item in listed] == [found.config]
    assert sync_found is not None
    assert sync_found.config == found.config
    assert saver.get(config) is not None
    assert await saver.aget(config) is not None


async def test_the_sync_and_async_paths_write_to_the_same_place() -> None:
    inner = ActorBoundSaver()
    saver = ScopedMemorySaver(inner)
    config = run_config(scoped_key())

    stored = saver.put(config, empty_checkpoint(), METADATA, {})
    saver.put_writes(stored, [("channel", 1)], "task-1")
    await saver.aput_writes(stored, [("channel", 2)], "task-2")

    found = await saver.aget_tuple(config)
    assert found is not None
    assert len(found.pending_writes or ()) == 2
    assert len(list(saver.list(config))) == 1


async def test_a_thread_is_deleted_for_its_actor_only() -> None:
    inner = ActorBoundSaver()
    saver = ScopedMemorySaver(inner)
    mine, theirs = scoped_key(), scoped_key(subject="user-7")
    saver.put(run_config(mine), empty_checkpoint(), METADATA, {})
    saver.put(run_config(theirs), empty_checkpoint(), METADATA, {})

    saver.delete_thread(mine)
    await saver.adelete_thread(mine)

    assert inner.deleted == [("accounts_2dagent-chat", "acme/user_2d42")] * 2
    assert saver.get_tuple(run_config(mine)) is None
    assert saver.get_tuple(run_config(theirs)) is not None


def test_the_saver_exposes_what_the_memory_checkpointer_defines() -> None:
    inner = ActorBoundSaver()
    saver = ScopedMemorySaver(inner)

    assert saver.inner is inner
    assert saver.serde is inner.serde
    assert saver.config_specs == inner.config_specs
    # The in-memory checkpointer salts its versions, so only the ordinal is comparable.
    assert saver.get_next_version(None, None)[:32] == inner.get_next_version(None, None)[:32]


# --------------------------------------------------------------- the backend


async def test_starting_builds_the_checkpointer_from_the_sessions_settings() -> None:
    backend = AgentCoreMemoryCheckpointBackend(
        options(max_results=25, checkpoint_format="snapshot"),
        offline_sessions(),
        saver_factory=ActorBoundSaver,
    )
    with pytest.raises(NotStartedError):
        _ = backend.checkpointer
    with pytest.raises(ConfigurationError, match="not started"):
        await backend.validate()

    await backend.start()

    saver = backend.checkpointer
    assert isinstance(saver, ScopedMemorySaver)
    assert isinstance(saver.inner, ActorBoundSaver)
    arguments = dict(saver.inner.arguments)
    config = arguments.pop("config")
    assert isinstance(config, Config)
    assert arguments == {
        "memory_id": MEMORY_ID,
        "max_results": 25,
        "checkpoint_format": "snapshot",
        "region_name": "eu-west-1",
        "verify": None,
    }
    with pytest.raises(RuntimeError, match="already started"):
        await backend.start()
    assert MEMORY_ID in repr(backend)
    await backend.aclose()
    await backend.aclose()
    with pytest.raises(NotStartedError):
        _ = backend.checkpointer


async def test_starting_needs_a_region() -> None:
    class NoRegion(AwsSessionFactory):
        @property
        def region(self) -> str | None:
            return None

    backend = AgentCoreMemoryCheckpointBackend(
        options(), NoRegion(session_factory=offline_session), saver_factory=ActorBoundSaver
    )
    with pytest.raises(ConfigurationError, match="region"):
        await backend.start()


async def test_a_checkpointer_that_cannot_be_built_stops_startup() -> None:
    def refusing(**arguments: Any) -> Any:
        raise ValueError("the checkpointer's own message")

    backend = AgentCoreMemoryCheckpointBackend(
        options(), offline_sessions(), saver_factory=refusing
    )
    with pytest.raises(ConfigurationError, match="could not be set up"):
        await backend.start()


async def test_validating_reads_one_probe_session_and_writes_nothing() -> None:
    sessions = offline_sessions()
    backend = AgentCoreMemoryCheckpointBackend(options(), sessions, saver_factory=ActorBoundSaver)
    await backend.start()
    with Stubber(sessions.client("bedrock-agentcore")) as stub:
        stub.add_response(
            "list_events",
            {"events": []},
            {
                "memoryId": MEMORY_ID,
                "actorId": "eap-readiness",
                "sessionId": "eap-readiness",
                "maxResults": 1,
                "includePayloads": False,
            },
        )
        await backend.validate()
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("code", "status", "message"),
    [
        ("ResourceNotFoundException", 404, "could not be read"),
        ("AccessDeniedException", 403, "refused"),
        ("ThrottlingException", 429, "throttling"),
        ("ValidationException", 400, "could not be read"),
    ],
)
async def test_a_memory_that_cannot_be_read_fails_validation(
    code: str, status: int, message: str
) -> None:
    sessions = offline_sessions()
    backend = AgentCoreMemoryCheckpointBackend(options(), sessions, saver_factory=ActorBoundSaver)
    await backend.start()
    with Stubber(sessions.client("bedrock-agentcore")) as stub:
        stub.add_client_error(
            "list_events",
            service_error_code=code,
            service_message="the service's own message",
            http_status_code=status,
        )
        with pytest.raises(ConfigurationError, match=message) as caught:
            await backend.validate()
    assert "the service's own message" not in str(caught.value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"memory_id": ""}, "memory_id"),
        ({"memory_id": "1starts-with-a-digit"}, "memory_id"),
        ({"memory_id": "has space"}, "memory_id"),
        ({"max_results": 0}, "max_results"),
        ({"max_results": 101}, "max_results"),
        ({"checkpoint_format": "events"}, "checkpoint_format"),
        ({"region": "eu-west-1"}, "region"),
    ],
)
def test_options_are_checked(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        options(**changes)


# ------------------------------------------------------------------ the pack


def service_config() -> ServiceConfig:
    return ServiceConfig.for_testing(
        sections={
            Section.CHECKPOINT: ProviderSelection("agentcore_memory", {"memory_id": MEMORY_ID})
        }
    )


async def test_a_service_selects_the_store_by_name() -> None:
    fakes = Fakes()
    providers = register_aws_adapters(fakes.providers(), sessions=offline_sessions())
    spec = providers.lookup(Section.CHECKPOINT, "agentcore_memory")
    assert (spec.local_only, spec.extra) == (False, "agentcore")

    async with ServiceContainer(service_config(), providers, clock=fakes.clock) as services:
        checkpointer = services.checkpoint_backend.checkpointer
        assert isinstance(checkpointer, ScopedMemorySaver)
        assert type(checkpointer.inner).__name__ == "AgentCoreMemorySaver"
        assert checkpointer.inner.memory_id == MEMORY_ID  # type: ignore[attr-defined]


async def test_without_the_extra_the_store_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "ai_agent_lib_aws.checkpoint_agentcore_memory", None)
    fakes = Fakes()
    providers = register_aws_adapters(fakes.providers(), sessions=offline_sessions())

    with pytest.raises(ConfigurationError, match=r"ai-agent-lib-aws\[agentcore\]"):
        async with ServiceContainer(service_config(), providers, clock=fakes.clock):
            pass
