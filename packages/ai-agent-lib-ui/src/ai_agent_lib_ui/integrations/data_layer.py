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
from ai_agent_lib_ui.contracts.queries import ContractError, ErrorCode, PageRequest
from ai_agent_lib_ui.integrations.answer_format import answer_id, outcome_text
from ai_agent_lib_ui.integrations.runtime import Runtime
from ai_agent_lib_ui.integrations.security import current_principal


class DataLayer(BaseDataLayer):
    """Authorize framework endpoints and derive their messages from the UI store."""

    def __init__(self, resources: Runtime) -> None:
        self.resources = resources

    async def get_user(self, identifier: str) -> PersistedUser | None:
        """Return only the user authenticated by the parent session."""
        await current_principal(self.resources)
        if identifier != self.resources.identity:
            raise HTTPException(404, "User not found")
        return PersistedUser(
            id=identifier,
            identifier=identifier,
            createdAt=now().isoformat(),
            display_name=self.resources.settings.identity.subject,
            metadata={"provider": "header"},
        )

    async def create_user(self, user: User) -> PersistedUser | None:
        """Reuse the configured identity; the browser cannot create identities."""
        return await self.get_user(user.identifier)

    async def get_thread_author(self, thread_id: str) -> str:
        """Check ownership before exposing the framework author identifier."""
        await self.resources.store.get(await current_principal(self.resources), thread_id)
        return self.resources.identity

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        """Restore messages exclusively from durable admitted turns."""
        principal = await current_principal(self.resources)
        conversation = await self.resources.store.get(principal, thread_id)
        thread = conversation.thread
        view = view_for(principal, thread.scope.application)
        steps: list[StepDict] = []
        history = await self.resources.store.history_page(view, thread_id, PageRequest())
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
            "userId": self.resources.identity,
            "userIdentifier": self.resources.identity,
            "tags": [],
            "metadata": {"chat_profile": thread.scope.application},
            "steps": steps,
            "elements": [],
        }

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        """Return bounded, owner-filtered summaries with a signed cursor."""
        principal = await current_principal(self.resources)
        if filters.userId and filters.userId != self.resources.identity:
            raise HTTPException(404, "User not found")
        page = await self.resources.store.list_owned(
            principal,
            PageRequest(limit=min(max(pagination.first, 1), 100), cursor=pagination.cursor),
            search=filters.search or "",
        )
        threads: list[ThreadDict] = [
            {
                "id": thread.thread_id,
                "createdAt": thread.created_at.isoformat(),
                "name": thread.title,
                "userId": self.resources.identity,
                "userIdentifier": self.resources.identity,
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
        principal = await current_principal(self.resources)
        if user_id is not None and user_id != self.resources.identity:
            raise HTTPException(404, "User not found")
        try:
            conversation = await self.resources.store.get(principal, thread_id)
        except ContractError as error:
            # Chainlit writes lifecycle metadata before on_chat_start. Only the
            # application create operation may establish agent binding/ownership.
            if error.code == ErrorCode.NOT_FOUND and name is None:
                return
            raise
        if name is not None:
            await self.resources.store.change(
                view_for(principal, conversation.thread.scope.application), thread_id, title=name
            )
        # Framework session metadata and tags cannot change a stored agent binding.

    async def create_step(self, step_dict: StepDict) -> None:
        """Authenticate lifecycle callbacks without persisting framework drafts."""
        await current_principal(self.resources)
        # Chainlit emits drafts and lifecycle steps. Admission/completion, not this
        # callback, owns the transcript transaction in SqliteThreads.

    async def update_step(self, step_dict: StepDict) -> None:
        """Ignore draft changes in the framework-owned message cache."""
        await self.create_step(step_dict)

    async def delete_thread(self, thread_id: str) -> None:
        """Keep destructive framework deletion disabled; provide explicit archiving."""
        await self.resources.store.get(await current_principal(self.resources), thread_id)
        raise HTTPException(409, "Archive this thread from the Conversations page instead")

    async def delete_step(self, step_id: str) -> None:
        """Keep the accepted transcript immutable."""
        await current_principal(self.resources)
        raise HTTPException(404, "Message operation unavailable")

    async def create_element(self, element: Element) -> None:
        """Keep documents in scoped artifacts, outside Chainlit's element storage."""
        await current_principal(self.resources)
        raise HTTPException(404, "Document elements are not enabled")

    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        """Check the thread owner before reporting unsupported elements."""
        await self.resources.store.get(await current_principal(self.resources), thread_id)
        return None

    async def delete_element(self, element_id: str, thread_id: str | None = None) -> None:
        """Keep saved document versions immutable; Chainlit elements are unsupported."""
        await current_principal(self.resources)
        raise HTTPException(404, "Document elements are not enabled")

    async def upsert_feedback(self, feedback: Feedback) -> str:
        """Reject unsupported feedback storage."""
        await current_principal(self.resources)
        raise HTTPException(404, "Feedback is not enabled")

    async def delete_feedback(self, feedback_id: str) -> bool:
        """Reject unsupported feedback deletion."""
        await current_principal(self.resources)
        raise HTTPException(404, "Feedback is not enabled")

    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        """Check identity even when the favorites feature is disabled."""
        await self.get_user(user_id)
        return []

    async def build_debug_url(self) -> str:
        """Link to Logs for saved turns and their diagnostics."""
        await current_principal(self.resources)
        return "/logs"

    async def close(self) -> None:
        """Release nothing: each store operation closes its own short-lived connection.

        The server's shared resources close in the parent application lifespan.
        """
        return None
