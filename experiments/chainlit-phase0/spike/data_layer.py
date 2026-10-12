"""Supported Chainlit data-layer adapter over the single application store."""

from chainlit.data.base import BaseDataLayer
from chainlit.types import PageInfo, PaginatedResponse
from chainlit.user import PersistedUser
from fastapi import HTTPException

from spike.runtime import runtime
from spike.security import current_owner
from spike.store import now


class DataLayer(BaseDataLayer):
    """Scope every method, including routes not used by the main chat screen."""

    def owner(self) -> str:
        """Resolve the parent identity on each operation, including WebSocket events."""
        return current_owner(runtime().store)

    async def get_user(self, identifier):
        if identifier != self.owner():
            raise HTTPException(401, "Identity mismatch")
        return PersistedUser(
            id=identifier, identifier=identifier, createdAt=now(), metadata={"provider": "header"}
        )

    async def create_user(self, user):
        return await self.get_user(user.identifier)

    async def get_thread_author(self, thread_id):
        return runtime().store.thread(self.owner(), thread_id)["userIdentifier"]

    async def get_thread(self, thread_id):
        return runtime().store.thread(self.owner(), thread_id)

    async def list_threads(self, pagination, filters):
        owner = self.owner()
        if filters.userId and filters.userId != owner:
            raise HTTPException(404, "User not found")
        threads = runtime().store.threads(owner, filters.search or "")
        start = 0
        if pagination.cursor:
            ids = [thread["id"] for thread in threads]
            if pagination.cursor not in ids:
                raise HTTPException(400, "Unknown cursor")
            start = ids.index(pagination.cursor) + 1
        size = min(max(pagination.first, 1), 50)
        page = threads[start : start + size]
        return PaginatedResponse(
            PageInfo(
                start + size < len(threads),
                page[0]["id"] if page else None,
                page[-1]["id"] if page else None,
            ),
            page,
        )

    async def update_thread(self, thread_id, name=None, user_id=None, metadata=None, tags=None):
        owner = self.owner()
        if user_id is not None and user_id != owner:
            raise HTTPException(404, "User not found")
        # Session metadata contains only presentation fields, never authority.
        safe = {
            key: value
            for key, value in (metadata or {}).items()
            if key in ("chat_profile", "chat_settings")
        }
        runtime().store.update_thread(owner, thread_id, name=name, metadata=safe, tags=tags)

    async def create_step(self, step_dict):
        # Chainlit wraps lifecycle callbacks in run steps before on_chat_start.
        # The application transcript stores messages; diagnostics use owned runs.
        if step_dict["type"] not in ("user_message", "assistant_message", "system_message"):
            self.owner()
            return
        # The accepted transcript is flat; callback run parents are not messages.
        runtime().store.put_object(self.owner(), "step", {**step_dict, "parentId": None})

    async def update_step(self, step_dict):
        await self.create_step(step_dict)

    async def create_element(self, element):
        runtime().store.put_object(self.owner(), "element", element.to_dict())

    async def get_element(self, thread_id, element_id):
        return runtime().store.object(self.owner(), element_id, thread_id)

    async def delete_step(self, step_id):
        runtime().store.object(self.owner(), step_id)
        raise HTTPException(403, "Deletion is outside this experiment")

    async def delete_element(self, element_id, thread_id=None):
        runtime().store.object(self.owner(), element_id, thread_id)
        raise HTTPException(403, "Deletion is outside this experiment")

    async def delete_thread(self, thread_id):
        runtime().store.thread(self.owner(), thread_id)
        raise HTTPException(403, "Deletion is outside this experiment")

    async def upsert_feedback(self, feedback):
        runtime().store.object(self.owner(), feedback.forId)
        raise HTTPException(403, "Feedback is outside this experiment")

    async def delete_feedback(self, feedback_id):
        self.owner()
        raise HTTPException(403, "Feedback is outside this experiment")

    async def get_favorite_steps(self, user_id):
        if user_id != self.owner():
            raise HTTPException(404, "User not found")
        return []

    async def build_debug_url(self):
        self.owner()
        return "/catalog"

    async def close(self):
        return None
