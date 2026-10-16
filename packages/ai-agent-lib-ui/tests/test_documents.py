"""Accepted documents are durable, scoped, bounded and safe to render."""

import json
from pathlib import Path

import pytest

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_ui.adapters.catalog import LocalAccess
from ai_agent_lib_ui.adapters.documents import SqliteDocuments
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads, now
from ai_agent_lib_ui.application.chat import view_for
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings
from ai_agent_lib_ui.contracts.artifacts import Artifact, ScopedArtifact
from ai_agent_lib_ui.contracts.catalog import AgentBinding, PresentationManifest
from ai_agent_lib_ui.contracts.conversations import RunRecord, TurnAdmission
from ai_agent_lib_ui.contracts.queries import ContractError
from ai_agent_lib_ui.integrations.documents import document_html


def settings_at(path: Path) -> Settings:
    url = "http://127.0.0.1:8199"
    return Settings(
        agents_path=path / "agents.json",
        tools_path=path / "tools.json",
        identity=LocalIdentity(),
        allowed_urls=frozenset({url}),
        bindings=(
            AgentBinding(
                base_url=url,
                manifest=PresentationManifest(
                    agent_id="memo",
                    agent_version="1",
                    registry_revision="rev-1",
                    title="Memo",
                    api_audience="memo",
                    capabilities=("artifacts",),
                ),
            ),
        ),
    )


def artifact(run: RunRecord, version: int, content: str = "# Saved memo") -> ScopedArtifact:
    return ScopedArtifact(
        scope=run.scope,
        artifact=Artifact(
            id=f"memo-{version}",
            document_id="meeting-memo",
            version=version,
            title="Acme memo",
            media_type="text/markdown",
            created_at=now(),
            request_id=run.request_id,
            thread_id=run.thread_id,
            content=content,
        ),
    )


async def complete(
    store: SqliteThreads, principal: Principal, key: str, version: int | None
) -> RunRecord:
    view = view_for(principal, "memo")
    admitted = await store.admit(
        view, TurnAdmission(scope=view.scope, thread_id="thread", idempotency_key=key, question=key)
    )
    running = admitted.model_copy(update={"state": "running"})
    assert await store.update(view, running, expected=admitted)
    done = running.model_copy(
        update={
            "state": "completed",
            "answer": "Accepted",
            "artifacts": (artifact(running, version),) if version else (),
        }
    )
    assert await store.update(view, done, expected=running)
    return done


async def test_versions_survive_qa_restart_and_cannot_be_overwritten(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    principal = settings.identity.principal()
    view = view_for(principal, "memo")
    store = SqliteThreads(tmp_path / "ui.sqlite", LocalAccess(settings), b"secret")
    await store.create(view, settings.bindings[0], "thread", "Meeting")
    await complete(store, principal, "v1", 1)
    await complete(store, principal, "question", None)
    await complete(store, principal, "v2", 2)
    duplicate = await complete(store, principal, "conflicting-v1", 1)
    # A second turn redeclares v1 with different request/timestamp; first version wins.
    restarted = SqliteThreads(store.path, LocalAccess(settings), b"secret")
    documents = SqliteDocuments(restarted)
    page = await documents.versions(view, "thread")
    assert [item["version"] for item in page["items"]] == [2, 1]
    first = await documents.get(view, "thread", "memo-1")
    assert first.artifact.request_id != duplicate.request_id
    assert first.artifact.content == "# Saved memo"
    assert all("content" not in item for item in page["items"])
    other = Principal(subject="other", tenant=principal.tenant)
    for target in (view_for(other, "memo"), view_for(principal, "helper")):
        with pytest.raises(ContractError, match="not_found"):
            await documents.get(target, "thread", "memo-1")
    with pytest.raises(ContractError, match="not_found"):
        await documents.get(view, "another-thread", "memo-1")


async def test_versions_are_paged_beyond_recent_chat_and_old_records_still_update(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    principal = settings.identity.principal()
    view = view_for(principal, "memo")
    store = SqliteThreads(tmp_path / "ui.sqlite", LocalAccess(settings), b"secret")
    await store.create(view, settings.bindings[0], "thread", "Meeting")
    for version in range(1, 52):
        await complete(store, principal, str(version), version)
    documents = SqliteDocuments(store)
    page = await documents.versions(view, "thread")
    assert len(page["items"]) == 50
    earlier = await documents.versions(view, "thread", before=page["next_before"])
    assert len(earlier["items"]) == 1
    assert earlier["items"][0]["version"] == 1
    assert (await documents.get(view, "thread", "memo-1")).artifact.version == 1
    admitted = await store.admit(
        view,
        TurnAdmission(
            scope=view.scope,
            thread_id="thread",
            idempotency_key="legacy",
            question="Older serialized record",
        ),
    )
    with store.connection() as db:
        value = admitted.model_dump(mode="json", exclude={"artifacts"})
        db.execute("UPDATE runs SET record=? WHERE id=?", (json.dumps(value), admitted.run_id))
    assert await store.update(
        view, admitted.model_copy(update={"state": "running"}), expected=admitted
    )


def test_memo_rendering_preserves_tables_and_keeps_untrusted_markup_inert() -> None:
    value = Artifact(
        id="a",
        document_id="d",
        version=1,
        title="Memo",
        media_type="text/markdown",
        created_at=now(),
        request_id="r",
        thread_id="t",
        content=(
            "# Meeting memo\n\n| Metric | Value |\n|---|---|\n| Cash | 10 |\n\n"
            '<script>alert(1)</script> <img src="https://bad.example/pixel">\n\n'
            "[Bad](javascript:alert(1)) ![Remote](https://bad.example/image)\n\n"
            "Cash [financial_facts[uen=ACME-1;concept=Cash]]."
        ),
    )
    rendered = document_html(value)
    assert "<table>" in rendered
    assert "<h1>Meeting memo</h1>" in rendered
    assert "<script" not in rendered
    assert "<img" not in rendered
    assert 'href="javascript:' not in rendered
    assert "<details><summary>References</summary>" in rendered
    assert "<details open" not in rendered
