"""Read immutable document versions saved with accepted answers in SQLite."""

import asyncio
from typing import TypedDict

from agentcraft2_ui.adapters.sqlite import SqliteThreads
from agentcraft2_ui.application.chat import view_for
from agentcraft2_ui.contracts.artifacts import ScopedArtifact
from agentcraft2_ui.contracts.queries import ContractError, ErrorCode, Permission, ViewContext


class DocumentVersion(TypedDict):
    """Metadata for a selector; content is fetched separately after authorization."""

    id: str
    document_id: str
    version: int
    title: str
    created_at: str
    request_id: str


class DocumentPage(TypedDict):
    """A bounded metadata page with a thread-scoped numeric continuation."""

    items: list[DocumentVersion]
    next_before: int | None


class SqliteDocuments:
    """Reuse thread ownership checks; content references are never treated as paths."""

    def __init__(self, threads: SqliteThreads) -> None:
        self.threads = threads

    async def _authorize(self, view: ViewContext, thread_id: str) -> None:
        conversation = await self.threads.get(view.principal, thread_id)
        if view != view_for(view.principal, conversation.thread.scope.application):
            raise ContractError(ErrorCode.NOT_FOUND)
        await self.threads.authorizer.require(view, Permission.ARTIFACT_READ)

    async def versions(
        self, view: ViewContext, thread_id: str, *, before: int | None = None
    ) -> DocumentPage:
        """List at most 50 metadata records, including versions outside recent chat history."""
        await self._authorize(view, thread_id)

        def read() -> DocumentPage:
            with self.threads.connection() as db:
                rows = db.execute(
                    "SELECT rowid,record FROM artifacts WHERE thread=? AND rowid<? "
                    "ORDER BY rowid DESC LIMIT 51",
                    (thread_id, before if before is not None else 2**63 - 1),
                ).fetchall()
            items: list[DocumentVersion] = []
            for _, record in rows[:50]:
                artifact = ScopedArtifact.model_validate_json(record).artifact
                items.append(
                    DocumentVersion(
                        id=artifact.id,
                        document_id=artifact.document_id,
                        version=artifact.version,
                        title=artifact.title,
                        created_at=artifact.created_at.isoformat(),
                        request_id=artifact.request_id,
                    )
                )
            return {"items": items, "next_before": rows[49][0] if len(rows) > 50 else None}

        return await asyncio.to_thread(read)

    async def get(self, view: ViewContext, thread_id: str, artifact_id: str) -> ScopedArtifact:
        """Read a version only inside its owned thread and agent scope."""
        await self._authorize(view, thread_id)

        def read() -> ScopedArtifact:
            with self.threads.connection() as db:
                row = db.execute(
                    "SELECT record FROM artifacts WHERE thread=? AND id=?",
                    (thread_id, artifact_id),
                ).fetchone()
            if row is None:
                raise ContractError(ErrorCode.NOT_FOUND)
            item = ScopedArtifact.model_validate_json(row[0])
            if item.scope != view.scope or item.artifact.thread_id != thread_id:
                raise ContractError(ErrorCode.NOT_FOUND)
            return item

        return await asyncio.to_thread(read)
