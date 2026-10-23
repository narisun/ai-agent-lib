"""SqliteThreads ownership, cursors, history paging and lock waiting."""

import asyncio
from pathlib import Path

import pytest
from chat_support import AllowUsers, ScriptedAgent

from agentcraft2_core.contracts import Principal
from agentcraft2_ui.adapters.sqlite import SqliteThreads
from agentcraft2_ui.application.chat import ChatService, view_for
from agentcraft2_ui.config.settings import Settings
from agentcraft2_ui.contracts.conversations import TurnAdmission
from agentcraft2_ui.contracts.queries import ContractError, PageRequest


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


async def test_history_pages_are_bounded_complete_and_scope_bound(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
) -> None:
    service, _, store = chat
    principal = settings.identity.principal()
    view = view_for(principal, "memo")
    thread = (await service.create(principal, "memo")).thread.thread_id
    # Store representative long answers without making network/model calls.
    from agentcraft2_ui.adapters.sqlite import now
    from agentcraft2_ui.contracts.conversations import RunRecord

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
