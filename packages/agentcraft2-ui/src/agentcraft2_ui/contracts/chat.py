"""Ports used by local chat, independent of its HTTP and browser frameworks."""

from typing import Protocol, runtime_checkable

from agentcraft2_core.contracts import Principal
from agentcraft2_core.contracts.diagnostics import DiagnosticValue
from agentcraft2_ui.contracts.catalog import AgentBinding
from agentcraft2_ui.contracts.conversations import RunRecord, ThreadRecord, TurnAdmission
from agentcraft2_ui.contracts.ports import ConversationStore
from agentcraft2_ui.contracts.queries import Page, PageRequest, ViewContext


class Conversation(DiagnosticValue):
    """A thread's immutable agent binding and accepted turn history."""

    thread: ThreadRecord
    binding: AgentBinding


class StoredTurn(DiagnosticValue):
    """One admitted question and its durable outcome."""

    question: str
    run: RunRecord


class ChatCatalog(Protocol):
    """Resolve authorized, pinned bindings against the current registry."""

    async def resolve(self, principal: Principal, agent_id: str) -> AgentBinding:
        """Reject denied, removed, disabled or changed registry entries."""
        ...


class ThreadRepository(ConversationStore, Protocol):
    """Application-owned lifecycle operations; every lookup checks ownership."""

    async def create(
        self, view: ViewContext, binding: AgentBinding, thread_id: str, title: str
    ) -> Conversation:
        """Create or return the same owned thread with the same agent binding."""
        ...

    async def get(self, principal: Principal, thread_id: str) -> Conversation:
        """Read the selected agent only after checking tenant and subject."""
        ...

    async def history(self, view: ViewContext, thread_id: str) -> tuple[StoredTurn, ...]:
        """Read this scoped thread's admitted questions and outcomes."""
        ...

    async def history_page(
        self, view: ViewContext, thread_id: str, page: PageRequest
    ) -> Page[StoredTurn]:
        """Read a bounded page, oldest first within the page, with a cursor for older turns."""
        ...

    async def reconcile(self, view: ViewContext, run: RunRecord, *, expected: RunRecord) -> bool:
        """Record an authoritative terminal outcome for an unknown run, with an audit entry."""
        ...

    async def list_owned(
        self, principal: Principal, page: PageRequest, *, search: str = "", archived: bool = False
    ) -> Page[ThreadRecord]:
        """List across permitted agent scopes for this tenant and subject."""
        ...

    async def change(
        self,
        view: ViewContext,
        thread_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ThreadRecord:
        """Rename or archive an owned thread without changing its agent."""
        ...


class TurnDispatcher(Protocol):
    """Send an already admitted turn; never retry an uncertain HTTP operation."""

    async def dispatch(
        self, view: ViewContext, binding: AgentBinding, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        """Return a terminal or unknown outcome; persist it through the repository."""
        ...


@runtime_checkable
class OutcomeReader(Protocol):
    """Optional read-only recovery; a missing outcome never permits a resend."""

    async def outcome(
        self, view: ViewContext, binding: AgentBinding, stored: StoredTurn
    ) -> RunRecord | None:
        """Return a correlated, confirmed outcome, or None when still unknown."""
        ...
