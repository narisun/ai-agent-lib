"""Map Chainlit history onto admitted turns; never persist a second transcript."""

from typing import Any
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from chainlit.data.base import BaseDataLayer
from chainlit.element import Element, ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User
from fastapi import HTTPException

from ai_agent_lib_ui.adapters.sqlite import now
from ai_agent_lib_ui.application.chat import view_for
from ai_agent_lib_ui.contracts.conversations import RunRecord
from ai_agent_lib_ui.contracts.feedback import FeedbackRecord
from ai_agent_lib_ui.contracts.queries import ContractError, ErrorCode, PageRequest
from ai_agent_lib_ui.integrations.answer_format import present_answer
from ai_agent_lib_ui.integrations.runtime import runtime
from ai_agent_lib_ui.integrations.security import current_principal


def answer_id(run: RunRecord) -> str:
    """Keep the accepted message ID stable across sends, refreshes and restarts."""
    return str(uuid5(NAMESPACE_URL, "agentlib:answer:" + run.run_id))


def outcome_text(run: RunRecord) -> str:
    """Show the answer, usage and trace link; keep evidence in the saved outcome."""
    usage = run.usage
    incoming = f"{usage.input_tokens:,}" if usage.input_tokens is not None else "unavailable"
    outgoing = f"{usage.output_tokens:,}" if usage.output_tokens is not None else "unavailable"
    elapsed = f"{run.elapsed_ms / 1000:.2f} s" if run.elapsed_ms is not None else "unavailable"
    metrics = f"Input tokens: {incoming} · Output tokens: {outgoing} · Time: {elapsed}"
    if usage.cache_read_tokens is not None and usage.input_tokens:
        metrics += f" · Cache hit: {usage.cache_read_tokens / usage.input_tokens:.1%}"
    if usage.cache_read_tokens is not None:
        metrics += f" · Cache read: {usage.cache_read_tokens:,}"
    if usage.cache_write_tokens is not None:
        metrics += f" · Cache write: {usage.cache_write_tokens:,}"
    if usage.kind in {"simulated", "estimated"}:
        metrics += f" · {usage.kind.capitalize()} usage"
    if run.trace is not None:
        label = "Agent Lab" if run.trace.provider == "phoenix" else run.trace.label
        metrics += f"\n\n[Open trace in {label}]({run.trace.url})"
    return _outcome(run) + "\n\n---\n\n" + metrics


def _outcome(run: RunRecord) -> str:
    """Explain incomplete outcomes without presenting a draft as an accepted answer."""
    if run.state == "completed":
        body = present_answer(run.answer or "").body
        if run.artifact_warning == "invalid_artifacts":
            body += (
                "\n\nThe answer was saved, but the agent returned an invalid document. "
                "Check the agent logs before requesting another memo."
            )
        return body
    if run.state == "failed":
        if run.failure_code == "invalid":
            return (
                "The agent could not accept this request or produce a response that passed "
                "its checks. You can send a new question in this thread."
            )
        return (
            "This request failed. Check the agent logs for the cause. "
            "You can send a new question in this thread."
        )
    if run.state == "unknown":
        return (
            "The request outcome is unknown. It may have reached the agent. "
            "This thread is held to prevent duplicate work. Reopen it to check the agent's "
            "saved outcome. Reopening never resends the question. If the agent has no "
            "confirmed outcome, the thread stays held; check the agent logs."
        )
    return "The request is still running. Reopen this thread to see its saved outcome."


class DataLayer(BaseDataLayer):
    """Authorize framework endpoints and derive their messages from the UI store."""

    async def get_user(self, identifier: str) -> PersistedUser | None:
        """Return only the user authenticated by the parent session."""
        current_principal()
        if identifier != runtime().identity:
            raise HTTPException(404, "User not found")
        return PersistedUser(
            id=identifier,
            identifier=identifier,
            createdAt=now().isoformat(),
            display_name=runtime().settings.identity.subject,
            metadata={"provider": "header"},
        )

    async def create_user(self, user: User) -> PersistedUser | None:
        """Reuse the configured identity; the browser cannot create identities."""
        return await self.get_user(user.identifier)

    async def get_thread_author(self, thread_id: str) -> str:
        """Check ownership before exposing the framework author identifier."""
        await runtime().store.get(current_principal(), thread_id)
        return runtime().identity

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        """Restore messages exclusively from durable admitted turns."""
        principal = current_principal()
        conversation = await runtime().store.get(principal, thread_id)
        thread = conversation.thread
        view = view_for(principal, thread.scope.application)
        steps: list[StepDict] = []
        history = await runtime().store.history_page(view, thread_id, PageRequest())
        if history.next_cursor:
            query = urlencode({"cursor": history.next_cursor})
            steps.append(
                {
                    "id": str(uuid5(NAMESPACE_URL, "agentlib:older:" + thread_id)),
                    "threadId": thread_id,
                    "name": "History",
                    "type": "system_message",
                    "output": (
                        "Showing recent messages. [View older messages]"
                        f"(/conversations/{thread_id}/history?{query})."
                    ),
                    "createdAt": thread.created_at.isoformat(),
                    "parentId": None,
                }
            )
        for stored in history.items:
            run = stored.run
            steps.append(
                {
                    "id": run.idempotency_key,
                    "threadId": thread_id,
                    "name": "You",
                    "type": "user_message",
                    "output": stored.question,
                    "createdAt": run.accepted_at.isoformat(),
                    "parentId": None,
                }
            )
            steps.append(
                {
                    "id": answer_id(run),
                    "threadId": thread_id,
                    "name": thread.scope.application,
                    "type": "assistant_message" if run.state == "completed" else "system_message",
                    "output": outcome_text(run),
                    "createdAt": run.updated_at.isoformat(),
                    "parentId": None,
                    "metadata": {"run_id": run.run_id, "state": run.state},
                }
            )
        return {
            "id": thread_id,
            "createdAt": thread.created_at.isoformat(),
            "name": thread.title,
            "userId": runtime().identity,
            "userIdentifier": runtime().identity,
            "tags": [],
            "metadata": {"chat_profile": thread.scope.application},
            "steps": steps,
            "elements": [],
        }

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        """Return bounded, owner-filtered summaries with a signed cursor."""
        principal = current_principal()
        if filters.userId and filters.userId != runtime().identity:
            raise HTTPException(404, "User not found")
        page = await runtime().store.list_owned(
            principal,
            PageRequest(limit=min(max(pagination.first, 1), 100), cursor=pagination.cursor),
            search=filters.search or "",
        )
        threads: list[ThreadDict] = [
            {
                "id": thread.thread_id,
                "createdAt": thread.created_at.isoformat(),
                "name": thread.title,
                "userId": runtime().identity,
                "userIdentifier": runtime().identity,
                "tags": [],
                "metadata": {"chat_profile": thread.scope.application},
                "steps": [],
                "elements": [],
            }
            for thread in page.items
        ]
        return PaginatedResponse(PageInfo(bool(page.next_cursor), None, page.next_cursor), threads)

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Accept title changes; framework metadata cannot change ownership or agent."""
        principal = current_principal()
        if user_id is not None and user_id != runtime().identity:
            raise HTTPException(404, "User not found")
        try:
            conversation = await runtime().store.get(principal, thread_id)
        except ContractError as error:
            # Chainlit writes lifecycle metadata before on_chat_start. Only the
            # application create operation may establish agent binding/ownership.
            if error.code == ErrorCode.NOT_FOUND and name is None:
                return
            raise
        if name is not None:
            await runtime().store.change(
                view_for(principal, conversation.thread.scope.application), thread_id, title=name
            )
        # Framework session metadata and tags cannot change a stored agent binding.

    async def create_step(self, step_dict: StepDict) -> None:
        """Authenticate lifecycle callbacks without persisting framework drafts."""
        current_principal()
        # Chainlit emits drafts and lifecycle steps. Admission/completion, not this
        # callback, owns the transcript transaction in SqliteThreads.

    async def update_step(self, step_dict: StepDict) -> None:
        """Ignore draft changes in the framework-owned message cache."""
        await self.create_step(step_dict)

    async def delete_thread(self, thread_id: str) -> None:
        """Keep destructive framework deletion disabled; provide explicit archiving."""
        await runtime().store.get(current_principal(), thread_id)
        raise HTTPException(409, "Use Logs to archive this thread")

    async def delete_step(self, step_id: str) -> None:
        """Keep the accepted transcript immutable."""
        current_principal()
        raise HTTPException(404, "Message operation unavailable")

    async def create_element(self, element: Element) -> None:
        """Keep documents in scoped artifacts, outside Chainlit's element storage."""
        current_principal()
        raise HTTPException(404, "Document elements are not enabled")

    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        """Check the thread owner before reporting unsupported elements."""
        await runtime().store.get(current_principal(), thread_id)
        return None

    async def delete_element(self, element_id: str, thread_id: str | None = None) -> None:
        """Keep saved document versions immutable; Chainlit elements are unsupported."""
        current_principal()
        raise HTTPException(404, "Document elements are not enabled")

    async def upsert_feedback(self, feedback: Feedback) -> str:
        """Record a judgement of an accepted answer as append-only evidence."""
        principal = current_principal()
        if not feedback.threadId:
            raise HTTPException(404, "Feedback needs a thread")
        conversation = await runtime().store.get(principal, feedback.threadId)
        application = conversation.thread.scope.application
        view = view_for(principal, application)
        history = await runtime().store.history_page(view, feedback.threadId, PageRequest())
        run = next(
            (
                stored.run
                for stored in history.items
                if stored.run.state == "completed" and answer_id(stored.run) == feedback.forId
            ),
            None,
        )
        if run is None:
            raise HTTPException(404, "Feedback target not found")
        record = FeedbackRecord(
            request_id=run.request_id,
            thread_id=feedback.threadId,
            application=application,
            subject=principal.subject,
            verdict="up" if feedback.value else "down",
            comment=feedback.comment or "",
            created_at=now().isoformat(),
        )
        await runtime().feedback.write(record)
        return answer_id(run)

    async def delete_feedback(self, feedback_id: str) -> bool:
        """Keep judgements append-only; recorded evidence is never unrecorded."""
        current_principal()
        raise HTTPException(404, "Feedback cannot be deleted")

    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        """Check identity even when the favorites feature is disabled."""
        await self.get_user(user_id)
        return []

    async def build_debug_url(self) -> str:
        """Link to Logs for saved turns and their diagnostics."""
        current_principal()
        return "/logs"

    async def close(self) -> None:
        # Connections are short-lived; server resources close in the parent lifespan.
        """Connections are released by each store operation."""
        return None
