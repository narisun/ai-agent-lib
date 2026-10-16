"""Regression checks for admission, local policy, dispatch and durable history."""

import asyncio
import json
import os
import sqlite3
import subprocess
import threading
import tomllib
from collections.abc import AsyncIterator
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import pytest
from python_runtime import python_command

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess, pin_bindings, read_registries
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService, view_for
from ai_agent_lib_ui.application.progress import Draft
from ai_agent_lib_ui.config import shell
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings, agent_environment
from ai_agent_lib_ui.contracts.conversations import RunRecord, ThreadRecord, TurnAdmission
from ai_agent_lib_ui.contracts.queries import (
    Availability,
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
        if self.mode == "thread_busy":
            return httpx.Response(
                409, json={"error": "thread_busy", "request_id": request.headers["x-request-id"]}
            )
        if self.mode == "gateway":
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={
                "thread_id": body["thread_id"],
                "request_id": request.headers["x-request-id"],
                "output": {"answer": f"answer {len(self.calls)}"},
                **(
                    {"output": {"answer": "Saved answer", "artifacts": [{}]}}
                    if self.mode == "bad_artifact"
                    else {}
                ),
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


@pytest.mark.parametrize(
    "body", [b"data: " + b"x" * 65 + b"\n\n", b"data: " + b"x" * 65, b": heartbeat\n\n" * 50]
)
async def test_stream_parser_bounds_frames_and_total_bytes(body: bytes) -> None:
    from ai_agent_lib_ui.adapters.events import events

    with pytest.raises(ContractError, match="invalid_response"):
        async for _ in events(httpx.Response(200, content=body), 64):
            pass


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


async def test_enabling_optional_artifacts_keeps_existing_chats_usable(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    bindings = tuple(
        binding.model_copy(
            update={
                "manifest": binding.manifest.model_copy(update={"capabilities": ("artifacts",)})
            }
        )
        for binding in settings.bindings
    )
    updated = settings.model_copy(update={"bindings": bindings})
    service.catalog = FileCatalog(updated, LocalAccess(updated))
    result = await service.submit(principal, thread, "after-upgrade", "Continue this conversation")
    assert result.state == "completed"
    assert len(agent.calls) == 1


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # Chainlit's transitive imports.
async def test_invalid_document_warning_survives_dispatch_and_restart(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    from ai_agent_lib_ui.integrations.data_layer import outcome_text

    service, agent, store = chat
    bindings = tuple(
        binding.model_copy(
            update={
                "manifest": binding.manifest.model_copy(update={"capabilities": ("artifacts",)})
            }
        )
        for binding in settings.bindings
    )
    settings = settings.model_copy(update={"bindings": bindings})
    service.catalog = FileCatalog(settings, LocalAccess(settings))
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.mode = "bad_artifact"
    saved = await service.submit(principal, thread, "one", "Generate a memo")
    restarted = SqliteThreads(store.path, LocalAccess(settings), b"test-cursor-secret")
    restored = (await restarted.history(view_for(principal, "memo"), thread))[0].run
    assert restored == saved
    assert restored.answer == "Saved answer"
    assert restored.state == "completed"
    assert restored.artifact_status == Availability.UNAVAILABLE
    assert restored.artifact_warning == "invalid_artifacts"
    assert "invalid document" in outcome_text(restored)
    # Pre-upgrade records still load without migration or transcript changes.
    legacy = RunRecord.model_validate(
        saved.model_dump(exclude={"artifact_status", "artifact_warning"})
    )
    assert legacy.answer == saved.answer
    assert legacy.artifact_warning is None


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


async def test_disconnect_during_commit_still_dispatches_and_shutdown_drains(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    entered, release = threading.Event(), threading.Event()
    touch = store._touch

    def hold_commit(db: sqlite3.Connection, thread_record: ThreadRecord, stamp: datetime) -> None:
        touch(db, thread_record, stamp)
        entered.set()
        assert release.wait(5)

    with patch.object(store, "_touch", side_effect=hold_commit):
        browser = asyncio.create_task(service.submit(principal, thread, "one", "hello"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            browser.cancel()
            with pytest.raises(asyncio.CancelledError):
                await browser
            closing = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            assert not closing.done()
        finally:
            release.set()
        await asyncio.wait_for(closing, 5)
    history = await store.history(view_for(principal, "memo"), thread)
    assert history[0].run.state == "completed"
    assert len(agent.calls) == 1
    assert not service.admissions
    assert not service.tasks


async def test_restart_releases_accepted_run_without_replaying_it(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    view = view_for(principal, "memo")
    await store.admit(
        view,
        TurnAdmission(scope=view.scope, thread_id=thread, idempotency_key="old", question="hi"),
    )
    store.recover()
    assert (await service.submit(principal, thread, "old", "hi")).state == "failed"
    assert not agent.calls
    assert (await service.submit(principal, thread, "new", "hello")).state == "completed"
    assert len(agent.calls) == 1


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


@pytest.mark.parametrize("mode", ["failed", "thread_busy"])
async def test_definitive_http_failure_allows_new_turn_but_never_replays_old_key(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings, mode: str
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.mode = mode
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
        python_command("-c", script),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "LITERAL_API_KEY"},
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_shell_versions_assets_by_content_without_changing_saved_secrets(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A package copy lets the test simulate an upgrade across two local starts.
    package = tmp_path / "package"
    assets = package / "assets"
    assets.mkdir(parents=True)
    for name in ("chainlit.toml", "chat.css", "conversations.svg", "memos.js", "agent-lab.svg"):
        (assets / name).write_bytes(files("ai_agent_lib_ui").joinpath("assets", name).read_bytes())
    monkeypatch.setattr(shell, "files", lambda _: package)
    monkeypatch.setattr("ai_agent_lib_ui.config.shell.os.environ", {})
    state = tmp_path / "state"

    def prepare() -> tuple[tuple[str, str], dict[str, object]]:
        secrets = shell.prepare_shell(state, settings)
        config = tomllib.loads((state / "shell/.chainlit/config.toml").read_text())
        return secrets, config["UI"]

    first_secrets, first = prepare()
    second_secrets, second = prepare()
    assert second == first
    assert second_secrets == first_secrets
    assert urlsplit(str(first["custom_css"])).path == "/public/chat.css"
    assert urlsplit(str(first["custom_css"])).query.startswith("v=")
    assert urlsplit(str(first["custom_js"])).query.startswith("v=")
    css = (assets / "chat.css").read_bytes() + b"\n/* changed style */\n"
    (assets / "chat.css").write_bytes(css)
    third_secrets, third = prepare()
    assert third_secrets == first_secrets
    assert third["custom_css"] != first["custom_css"]
    assert third["custom_js"] == first["custom_js"]
    assert (state / "shell/public/chat.css").read_bytes() == css


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


class EventStream(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request, mode: str, release: asyncio.Event) -> None:
        self.request, self.mode, self.release = request, mode, release

    async def __aiter__(self) -> AsyncIterator[bytes]:
        identity = {
            "thread_id": json.loads(self.request.content)["thread_id"],
            "request_id": self.request.headers["x-request-id"],
        }

        def frame(name: str, **value: object) -> bytes:
            return f"event: {name}\ndata: {json.dumps({**identity, **value})}\n\n".encode()

        yield frame("draft", call=1, text="", reset=True)
        yield frame("draft", call=1, text="Provisional £ text", reset=False)
        await self.release.wait()
        if self.mode == "missing":
            return
        if self.mode in {"cross_thread", "cross_thread_error"}:
            identity["thread_id"] = "someone-else"
        if self.mode == "cross_request_error":
            identity["request_id"] = "someone-else"
        if "error" in self.mode:
            terminal = frame(
                "error",
                error="internal" if self.mode == "internal_error" else "invalid",
                model_calls=3,
                usage={
                    "input_tokens": 90,
                    "output_tokens": 20,
                    "source": "langchain",
                    "kind": "reported",
                },
                **({"output": {"answer": "Do not accept"}} if self.mode == "output_error" else {}),
            )
            yield terminal
            if self.mode == "duplicate_error":
                yield terminal
            if self.mode == "interrupted_error":
                raise httpx.ReadError("connection lost after terminal")
            return
        result = frame(
            "result",
            output={"answer": "Accepted answer"},
            model_calls=2,
            usage={
                "source": "langchain",
                "kind": "reported",
                "input_tokens": 40,
                "output_tokens": 12,
                "cache_read_tokens": 10,
            },
        )
        # Split arbitrary bytes, including UTF-8 and SSE delimiters.
        for index in range(0, len(result), 7):
            yield result[index : index + 7]
        if self.mode == "duplicate":
            yield result


@pytest.mark.parametrize(
    "mode",
    [
        "ok",
        "missing",
        "cross_thread",
        "error",
        "duplicate",
        "internal_error",
        "cross_thread_error",
        "cross_request_error",
        "output_error",
        "duplicate_error",
        "interrupted_error",
    ],
)
async def test_live_drafts_are_not_saved_and_only_one_terminal_result_is_accepted(
    settings: Settings,
    tmp_path: Path,
    mode: str,
) -> None:
    release = asyncio.Event()
    observed = asyncio.Event()
    calls = 0

    async def agent(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(404)  # Older servers do not have a status journal.
        calls += 1
        assert request.headers["accept"] == "text/event-stream, application/json"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=EventStream(request, mode, release),
        )

    async def display(draft: Draft) -> None:
        if "Provisional" in draft.text:
            observed.set()

    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "stream.sqlite", access, b"key")
    principal = settings.identity.principal()
    async with httpx.AsyncClient(transport=httpx.MockTransport(agent)) as client:
        service = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 3))
        thread = (await service.create(principal, "memo")).thread.thread_id
        browser = asyncio.create_task(
            service.submit(principal, thread, "send", "question", on_progress=display)
        )
        await asyncio.wait_for(observed.wait(), timeout=2)
        assert not browser.done()
        history = await store.history(view_for(principal, "memo"), thread)
        assert history[0].run.answer is None
        # Disconnect the browser during delivery. The service must still save the result.
        browser.cancel()
        with pytest.raises(asyncio.CancelledError):
            await browser
        release.set()
        await service.reattach(principal, thread)
        result = await service.submit(principal, thread, "send", "question")
        assert result.state == {"ok": "completed", "error": "failed"}.get(mode, "unknown")
        assert result.elapsed_ms is not None
        if mode == "ok":
            assert result.answer == "Accepted answer"
            assert result.usage.input_tokens == 40
            assert result.usage.output_tokens == 12
            assert result.model_calls == 2
            reopened = SqliteThreads(store.path, access, b"key")
            assert (await reopened.history(view_for(principal, "memo"), thread))[0].run == result
        else:
            assert result.answer is None
        assert calls == 1
        if mode == "error":
            assert result.failure_code == "invalid"
            assert result.usage.input_tokens == 90
            assert result.usage.output_tokens == 20
            assert result.model_calls == 3
            reopened = SqliteThreads(store.path, access, b"key")
            assert (await reopened.history(view_for(principal, "memo"), thread))[0].run == result
            # The original key never dispatches again; a different question is allowed.
            assert await service.submit(principal, thread, "send", "question") == result
            assert calls == 1
            assert (
                await service.submit(principal, thread, "next", "new question")
            ).state == "failed"
            assert calls == 2
        elif mode != "ok":
            with pytest.raises(ContractError):
                await service.submit(principal, thread, "next", "new question")
            assert calls == 1
        await service.close()


async def test_json_wrong_request_id_holds_admission(settings: Settings, tmp_path: Path) -> None:
    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "correlation.sqlite", access, b"key")

    async def wrong(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "thread_id": json.loads(request.content)["thread_id"],
                "request_id": "another-request",
                "output": {"answer": "wrong invocation"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong)) as client:
        chat = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 2))
        principal = settings.identity.principal()
        thread = (await chat.create(principal, "memo")).thread.thread_id
        result = await chat.submit(principal, thread, "one", "Question")
        assert result.state == "unknown"
        assert result.answer is None
        assert result.request_id != "another-request"
        with pytest.raises(ContractError, match="conflict"):
            await chat.submit(principal, thread, "two", "Do not resend")


async def test_unrelated_registry_change_preserves_existing_thread(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    old_revision = settings.bindings[0].manifest.registry_revision
    raw = json.loads(settings.agents_path.read_text())
    raw["agents"][1]["version"] = "2"
    settings.agents_path.write_text(json.dumps(raw))
    result = await service.submit(principal, thread, "one", "Continue")
    assert result.state == "completed"
    assert len(agent.calls) == 1
    assert result.registry_revision != old_revision


async def test_history_pages_are_bounded_complete_and_scope_bound(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
) -> None:
    service, _, store = chat
    principal = settings.identity.principal()
    view = view_for(principal, "memo")
    thread = (await service.create(principal, "memo")).thread.thread_id
    # Store representative long answers without making network/model calls.
    from ai_agent_lib_ui.adapters.sqlite import now
    from ai_agent_lib_ui.contracts.conversations import RunRecord

    stamp = now()
    with store.connection() as db:
        for i in range(110):
            run = RunRecord(
                scope=view.scope,
                thread_id=thread,
                request_id=f"r-{i}",
                run_id=f"run-{i}",
                idempotency_key=f"key-{i}",
                state="completed",
                accepted_at=stamp,
                updated_at=stamp,
                answer="x" * 50_000,
            )
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                (run.run_id, thread, run.idempotency_key, str(i), run.state, run.model_dump_json()),
            )
    recent = await store.history(view, thread)
    assert len(recent) < 50
    assert recent[-1].question == "109"
    page = await store.history_page(view, thread, PageRequest())
    first_cursor = page.next_cursor
    collected = list(page.items)
    while page.next_cursor:
        page = await store.history_page(view, thread, PageRequest(cursor=page.next_cursor))
        collected = [*page.items, *collected]
    assert [item.question for item in collected] == [str(i) for i in range(110)]
    other = (await service.create(principal, "memo")).thread.thread_id
    with pytest.raises(ContractError, match="invalid_input"):
        await store.history_page(view, other, PageRequest(cursor=first_cursor))
    assert first_cursor
    with pytest.raises(ContractError, match="invalid_input"):
        await store.history_page(view, thread, PageRequest(cursor=first_cursor + "x"))


@pytest.mark.parametrize("outcome", ["completed", "failed", "pending", "wrong_id", "unavailable"])
async def test_reopen_reconciles_only_confirmed_correlated_outcomes_without_resending(
    settings: Settings,
    tmp_path: Path,
    outcome: str,
) -> None:
    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "recovery.sqlite", access, b"key")
    saved: dict[str, object] = {}
    methods: list[str] = []

    async def agent(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            saved.update(
                thread_id=json.loads(request.content)["thread_id"],
                request_id=request.headers["x-request-id"],
                elapsed_ms=1250,
                usage={"input_tokens": 20, "output_tokens": 5},
                model_calls=1,
            )
            if outcome == "failed":
                saved["error"] = "invalid"
            else:
                saved["output"] = {"answer": "The already accepted answer"}
            raise httpx.ReadTimeout("response was lost")
        if outcome == "pending":
            return httpx.Response(200, json={"state": "unknown"})
        if outcome == "unavailable":
            return httpx.Response(404)
        return httpx.Response(
            200, json={**saved, **({"request_id": "wrong"} if outcome == "wrong_id" else {})}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(agent)) as client:
        service = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 2))
        principal = settings.identity.principal()
        view = view_for(principal, "memo")
        thread = (await service.create(principal, "memo")).thread.thread_id
        unknown = await service.submit(principal, thread, "one", "Question")
        assert unknown.state == "unknown"
        await service.reattach(principal, thread)
        latest = (await store.history(view, thread))[-1].run
        assert methods == ["POST", "GET"]
        if outcome in {"completed", "failed"}:
            assert latest.state == outcome
            assert latest.usage.input_tokens == 20
            assert latest.elapsed_ms == 1250
            with store.connection() as db:
                assert db.execute("SELECT state FROM reconciliations").fetchall() == [(outcome,)]
            await service.reattach(principal, thread)
            assert methods == ["POST", "GET"]
            assert await service.submit(principal, thread, "one", "Question") == latest
        else:
            assert latest == unknown
            with pytest.raises(ContractError, match="conflict"):
                await service.submit(principal, thread, "two", "Not safe to retry")


async def test_waiting_for_database_write_does_not_block_the_ui_loop(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
) -> None:
    service, _, store = chat
    principal = settings.identity.principal()
    view = view_for(principal, "memo")
    thread = (await service.create(principal, "memo")).thread.thread_id
    with store.connection() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        waiting = asyncio.create_task(
            store.admit(
                view,
                TurnAdmission(
                    scope=view.scope, thread_id=thread, idempotency_key="wait", question="Question"
                ),
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert not waiting.done()  # The event loop can release the competing lock.
        finally:
            blocker.commit()
            await waiting
