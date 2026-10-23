"""Replaceable IO ports behind the application and the SQLite/HTTP adapters.

Object readers authorize the action and verify view.scope against the object's
scope before reading storage or calling a service. Missing and unauthorized
object IDs both return not_found. No port exposes an unscoped checkpoint database.
"""

from typing import Protocol

from ai_agent_lib_ui.contracts.catalog import AgentBinding
from ai_agent_lib_ui.contracts.conversations import RunRecord, ThreadRecord, TurnAdmission
from ai_agent_lib_ui.contracts.queries import (
    Page,
    PageRequest,
    Permission,
    ViewContext,
)


class Authorizer(Protocol):
    """Authorize every operation, including catalog entries and content downloads."""

    async def require(self, view: ViewContext, action: Permission) -> None:
        """Raise not_found on denied object access; never trust a manifest for access."""
        ...


class AgentClient(Protocol):
    """Invoke an HTTP agent, revalidating registry revision and access at dispatch."""

    async def invoke(
        self, view: ViewContext, binding: AgentBinding, turn: TurnAdmission
    ) -> RunRecord:
        """Send only this question with the existing agent thread ID."""
        ...


class ConversationStore(Protocol):
    """Persist UI history separately from agent checkpoints, with atomic admission."""

    async def threads(self, view: ViewContext, page: PageRequest) -> Page[ThreadRecord]:
        """List this caller's scoped threads using verified cursors."""
        ...

    async def admit(self, view: ViewContext, turn: TurnAdmission) -> RunRecord:
        """Atomically return an idempotent run or reserve one active run per thread."""
        ...

    async def update(self, view: ViewContext, run: RunRecord, *, expected: RunRecord) -> bool:
        """Compare-and-set a legal state transition and persist accepted output."""
        ...
