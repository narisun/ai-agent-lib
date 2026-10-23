"""Authorized document routes and inert Markdown for the optional memo panel."""

from html import escape
from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from markdown_it import MarkdownIt

from agentcraft2_ui.adapters.documents import SqliteDocuments
from agentcraft2_ui.application.chat import view_for
from agentcraft2_ui.contracts.artifacts import Artifact
from agentcraft2_ui.integrations.answer_format import present_answer
from agentcraft2_ui.integrations.runtime import Runtime
from agentcraft2_ui.integrations.security import current_principal


def document_html(artifact: Artifact) -> str:
    """Render text without raw HTML, executable links, images or external resources."""
    content = artifact.content or ""
    if artifact.media_type == "text/plain":
        return "<pre>" + escape(content) + "</pre>"
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    answer = present_answer(content)
    body = str(markdown.render(answer.body))
    if answer.references:
        body += (
            "<details><summary>References</summary>"
            + markdown.render(answer.references)
            + "</details>"
        )
    return body


def register_documents(app: FastAPI, resources: Runtime) -> None:
    """Select versions from saved outputs; opening a memo never executes an agent."""
    documents = SqliteDocuments(resources.store)

    @app.get("/api/threads/{thread_id}/documents")
    async def versions(
        thread_id: str, before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None
    ) -> object:
        principal = await current_principal(resources)
        conversation = await resources.store.get(principal, thread_id)
        return await documents.versions(
            view_for(principal, conversation.thread.scope.application), thread_id, before=before
        )

    @app.get("/api/threads/{thread_id}/documents/{artifact_id}")
    async def content(thread_id: str, artifact_id: str) -> HTMLResponse:
        principal = await current_principal(resources)
        conversation = await resources.store.get(principal, thread_id)
        item = await documents.get(
            view_for(principal, conversation.thread.scope.application), thread_id, artifact_id
        )
        return HTMLResponse(document_html(item.artifact))
