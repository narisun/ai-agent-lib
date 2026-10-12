"""Regression checks for admission, local policy, dispatch and durable history."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess, pin_bindings, read_registries
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService, view_for
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings, agent_environment
from ai_agent_lib_ui.contracts.conversations import TurnAdmission
from ai_agent_lib_ui.contracts.queries import (
    ContractError,
    ErrorCode,
    PageRequest,
    Permission,
    ViewContext,
)


def settings_at(path: Path) -> Settings:
    agents = path / "agents.json"
    tools = path / "tools.json"
    agents.write_text(
        json.dumps(
            {
                "schema": "agentlib.registry/v1",
                "agents": [
                    {
                        "id": name,
                        "owner": "team",
                        "version": "1",
                        "url": f"http://127.0.0.1:8199/{name}",
                    }
                    for name in ("memo", "helper")
                ],
            }
        ),
        encoding="utf-8",
    )
    tools.write_text('{"schema":"agentlib.registry/v1","servers":[]}', encoding="utf-8")
    snapshot, _ = read_registries(agents, tools)
    urls = frozenset({"http://127.0.0.1:8199/memo", "http://127.0.0.1:8199/helper"})
    return Settings(
        agents_path=agents,
        tools_path=tools,
        identity=LocalIdentity(),
        allowed_urls=urls,
        bindings=pin_bindings(snapshot, ("memo", "helper"), urls),
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return settings_at(tmp_path)


class ScriptedAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.mode = "ok"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        self.entered.set()
        await self.release.wait()
        if self.mode == "timeout":
            raise httpx.ReadTimeout("may already have executed")
        if self.mode == "redirect":
            return httpx.Response(307, headers={"location": "http://127.0.0.1:8888/other"})
        if self.mode == "invalid":
            return httpx.Response(200, json={"output": {"draft": "not accepted"}})
        if self.mode == "large":
            return httpx.Response(200, content=b"x" * 1_048_577)
        if self.mode == "failed":
            return httpx.Response(403, json={"detail": "denied"})
        if self.mode == "gateway":
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={
                "thread_id": body["thread_id"],
                "request_id": request.headers["x-request-id"],
                "output": {"answer": f"answer {len(self.calls)}"},
            },
        )


@pytest.fixture
async def chat(
    settings: Settings, tmp_path: Path
) -> AsyncIterator[tuple[ChatService, ScriptedAgent, SqliteThreads]]:
    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "ui.sqlite", access, b"test-cursor-secret")
    agent = ScriptedAgent()
    async with httpx.AsyncClient(transport=httpx.MockTransport(agent), trust_env=False) as client:
        service = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 2))
        yield service, agent, store
        agent.release.set()
        await service.close()


async def test_two_questions_restart_resume_and_only_new_input(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread
    first = await service.submit(principal, thread.thread_id, "one", "Prepare a memo")
    second = await service.submit(principal, thread.thread_id, "two", "Explain the cash flow")
    assert first.state == second.state == "completed"
    assert agent.calls == [
        {"input": {"question": "Prepare a memo"}, "thread_id": thread.thread_id},
        {"input": {"question": "Explain the cash flow"}, "thread_id": thread.thread_id},
    ]
    restarted = SqliteThreads(store.path, LocalAccess(settings), b"test-cursor-secret")
    restarted.recover()
    assert (await restarted.get(principal, thread.thread_id)).binding == settings.bindings[0]
    history = await restarted.history(view_for(principal, "memo"), thread.thread_id)
    assert [turn.run.answer for turn in history] == ["answer 1", "answer 2"]
    await service.reattach(principal, thread.thread_id)
    assert len(agent.calls) == 2


async def test_duplicate_disconnect_and_same_thread_race(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.release.clear()
    browser = asyncio.create_task(service.submit(principal, thread, "same", "hello"))
    await agent.entered.wait()
    duplicate = asyncio.create_task(service.submit(principal, thread, "same", "hello"))
    browser.cancel()
    with pytest.raises(asyncio.CancelledError):
        await browser
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "other", "another question")
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "same", "changed question")
    agent.release.set()
    saved = await duplicate
    replay = await service.submit(principal, thread, "same", "hello")
    assert saved == replay
    assert saved.state == "completed"
    assert len(agent.calls) == 1
    assert len(await store.history(view_for(principal, "memo"), thread)) == 1


@pytest.mark.parametrize("mode", ["timeout", "invalid", "large", "redirect", "gateway"])
async def test_uncertain_outcome_holds_admission_without_retry(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings, mode: str
) -> None:
    service, agent, store = chat
    agent.mode = mode
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    result = await service.submit(principal, thread, "one", "hello")
    assert result.state == "unknown"
    assert result.answer is None
    store.recover()
    assert (await service.submit(principal, thread, "one", "hello")).state == "unknown"
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "two", "try again")
    assert len(agent.calls) == 1


async def test_definitive_http_failure_allows_new_turn_but_never_replays_old_key(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.mode = "failed"
    failed = await service.submit(principal, thread, "one", "hello")
    assert failed.state == "failed"
    agent.mode = "ok"
    assert await service.submit(principal, thread, "one", "hello") == failed
    assert (await service.submit(principal, thread, "two", "new question")).state == "completed"
    assert len(agent.calls) == 2


async def test_persistence_failure_before_dispatch_makes_no_call(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    with (
        patch.object(store, "admit", side_effect=sqlite3.OperationalError("disk full")),
        pytest.raises(sqlite3.OperationalError),
    ):
        await service.submit(principal, thread, "one", "hello")
    assert agent.calls == []


async def test_failed_result_commit_preserves_admission_and_restart_marks_unknown(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.release.clear()
    browser = asyncio.create_task(service.submit(principal, thread, "one", "hello"))
    await agent.entered.wait()
    with patch.object(store, "update", side_effect=sqlite3.OperationalError("disk full")):
        agent.release.set()
        with pytest.raises(sqlite3.OperationalError):
            await browser
    store.recover()
    history = await store.history(view_for(principal, "memo"), thread)
    assert history[0].run.state == "unknown"
    assert history[0].run.answer is None
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "two", "try again")
    assert len(agent.calls) == 1


async def test_registry_change_after_selection_fails_before_http(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    settings.agents_path.write_text(settings.agents_path.read_text().replace('"1"', '"2"'))
    assert (await service.submit(principal, thread, "one", "hello")).state == "failed"
    assert agent.calls == []
    with pytest.raises(ContractError, match="stale_binding"):
        await service.create(principal, "memo")


async def test_denied_agent_identity_and_missing_endpoint(settings: Settings) -> None:
    catalog = FileCatalog(settings, LocalAccess(settings))
    for principal, agent in [
        (settings.identity.principal(), "not-allowed"),
        (Principal(subject="other", tenant="dev-tenant"), "memo"),
    ]:
        with pytest.raises(ContractError, match="not_found"):
            await catalog.resolve(principal, agent)
    raw = json.loads(settings.agents_path.read_text())
    raw["agents"][0].pop("url")
    settings.agents_path.write_text(json.dumps(raw))
    agents, _ = read_registries(settings.agents_path, settings.tools_path)
    with pytest.raises(ValueError, match="loopback url"):
        pin_bindings(agents, ("memo",), settings.allowed_urls)


class AllowUsers:
    async def require(self, view: ViewContext, action: Permission) -> None:
        if view.principal.tenant not in {"one", "two"}:
            raise ContractError(ErrorCode.NOT_FOUND)


async def test_ownership_filters_cursors_rename_and_archive(
    settings: Settings, tmp_path: Path
) -> None:
    store = SqliteThreads(tmp_path / "owners.sqlite", AllowUsers(), b"cursor-key")
    alice = Principal(subject="alice", tenant="one")
    bob = Principal(subject="bob", tenant="one")
    other_tenant = Principal(subject="alice", tenant="two")
    for principal, name in [(alice, "a1"), (alice, "a2"), (bob, "b1"), (other_tenant, "t1")]:
        await store.create(view_for(principal, "memo"), settings.bindings[0], name, name)
    page = await store.list_owned(alice, PageRequest(limit=1))
    assert page.next_cursor
    next_page = await store.list_owned(alice, PageRequest(limit=1, cursor=page.next_cursor))
    assert {page.items[0].thread_id, next_page.items[0].thread_id} == {"a1", "a2"}
    for stranger in (bob, other_tenant):
        with pytest.raises(ContractError, match="not_found"):
            await store.get(stranger, "a1")
        with pytest.raises(ContractError, match="invalid_input"):
            await store.list_owned(stranger, PageRequest(cursor=page.next_cursor))
    with pytest.raises(ContractError, match="invalid_input"):
        await store.list_owned(alice, PageRequest(cursor=page.next_cursor), search="a")
    view = view_for(alice, "memo")
    await store.change(view, "a1", title="Meeting memo")
    assert len((await store.list_owned(alice, PageRequest(), search="meeting")).items) == 1
    await store.change(view, "a1", archived=True)
    assert len((await store.list_owned(alice, PageRequest())).items) == 1
    with pytest.raises(ContractError, match="conflict"):
        await store.admit(
            view,
            TurnAdmission(scope=view.scope, thread_id="a1", idempotency_key="x", question="hi"),
        )
    await store.change(view, "a1", archived=False)
    assert len((await store.list_owned(alice, PageRequest())).items) == 2


async def test_two_connections_cannot_admit_competing_turns(
    settings: Settings, tmp_path: Path
) -> None:
    principal = settings.identity.principal()
    view = view_for(principal, "memo")
    stores = [
        SqliteThreads(tmp_path / "race.sqlite", LocalAccess(settings), b"key") for _ in range(2)
    ]
    await stores[0].create(view, settings.bindings[0], "thread", "title")
    turns = [
        TurnAdmission(
            scope=view.scope, thread_id="thread", idempotency_key=f"key{i}", question="hi"
        )
        for i in range(2)
    ]
    # Run independent event loops in worker threads to exercise the database index,
    # rather than merely serializing two methods on this test's event loop.
    results = await asyncio.gather(
        *[
            asyncio.to_thread(asyncio.run, store.admit(view, turn))
            for store, turn in zip(stores, turns, strict=True)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ContractError) for result in results) == 1
    assert len(await stores[0].history(view, "thread")) == 1


def test_env_translation_is_explicit_and_does_not_export_model_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'EAP_REGISTRY_OPTIONS={"agents_path":"../agents.yaml","tools_path":"tools.yaml"}\n'
        'EAP_IDENTITY_OPTIONS={"subject":"ada","tenant":"bank","roles":["analyst"]}\n'
        "ANTHROPIC_API_KEY=not-a-real-key\n",
        encoding="utf-8",
    )
    agents, tools, identity = agent_environment(env)
    assert agents == (tmp_path.parent / "agents.yaml").resolve()
    assert tools == tmp_path / "tools.yaml"
    assert identity.subject == "ada"
    env.write_text("EAP_IDENTITY_PROVIDER=jwt\n")
    with pytest.raises(ValueError, match="static"):
        agent_environment(env)
    env.write_text('EAP_IDENTITY_OPTIONS={"audience":"memo","token_ttl_seconds":600}\n')
    _, _, defaults = agent_environment(env)
    assert defaults == LocalIdentity(subject="dev-user", tenant="dev-tenant", roles=())
    env.write_text("EAP_PROFILE=aws\n")
    with pytest.raises(ValueError, match="local"):
        agent_environment(env)


def test_shell_ignores_working_directory_dotenv(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("UI_IGNORED_SENTINEL=must-not-be-loaded\n")
    config = tmp_path / "config.json"
    config.write_text(settings.model_dump_json())
    script = (
        "import os; from pathlib import Path; "
        "from ai_agent_lib_ui.config.settings import Settings; "
        "from ai_agent_lib_ui.config.shell import prepare_shell; "
        "prepare_shell(Path('state'), Settings.read(Path('config.json'))); "
        "import chainlit; assert 'UI_IGNORED_SENTINEL' not in os.environ"
    )
    result = subprocess.run(  # noqa: S603 - fixed import probe in a temporary test folder
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "LITERAL_API_KEY"},
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


async def test_total_deadline_also_bounds_a_stalled_dispatch(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, _ = chat
    agent.release.clear()
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    async with asyncio.timeout(4):
        result = await service.submit(principal, thread, "one", "hello")
    assert result.state == "unknown"
    assert len(agent.calls) == 1
