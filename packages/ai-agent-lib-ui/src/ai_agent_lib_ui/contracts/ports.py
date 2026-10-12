"""Replaceable IO ports for later chat and inspector implementations.

Object readers authorize the action and verify view.scope against the object's
scope before reading storage or calling a service. The catalog filters entries
for the verified principal before an agent is selected. Missing and unauthorized
object IDs both return not_found. No port exposes an unscoped checkpoint database.
"""

from typing import Protocol

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_core.contracts.diagnostics import (
    DiagnosticEvent,
    DiagnosticValue,
    Identifier,
    RequestRecord,
)
from ai_agent_lib_ui.contracts.artifacts import ScopedArtifact
from ai_agent_lib_ui.contracts.catalog import AgentBinding, McpSummary
from ai_agent_lib_ui.contracts.conversations import (
    RunRecord,
    ThreadRecord,
    ThreadRef,
    TurnAdmission,
)
from ai_agent_lib_ui.contracts.queries import (
    Availability,
    ContentPreview,
    Page,
    PageRequest,
    Permission,
    QueryBinding,
    ViewContext,
)


class CheckpointSummary(DiagnosticValue):
    """A decoded summary; fetching content never executes or unpickles state."""

    thread: ThreadRef
    checkpoint_id: Identifier
    parent_id: Identifier | None = None
    node_names: tuple[str, ...] = ()


class DiagnosticPage(DiagnosticValue):
    """A metadata page plus capture visibility, distinct from ingestion completeness."""

    availability: Availability
    records: Page[DiagnosticEvent]


class Authorizer(Protocol):
    """Authorize every operation, including catalog entries and content downloads."""

    async def require(self, view: ViewContext, action: Permission) -> None:
        """Raise not_found on denied object access; never trust a manifest for access."""
        ...


class CatalogReader(Protocol):
    """Resolve authorized registry metadata and server-owned presentation bindings."""

    async def agents(self, principal: Principal, page: PageRequest) -> Page[AgentBinding]:
        """List only agents this caller can use."""
        ...

    async def mcps(self, principal: Principal, page: PageRequest) -> Page[McpSummary]:
        """List permitted MCP metadata without granting direct tool execution."""
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


class CheckpointReader(Protocol):
    """Query the authenticated agent diagnostic API, never its database directly."""

    async def list(
        self, view: ViewContext, thread: ThreadRef, page: PageRequest
    ) -> Page[CheckpointSummary]:
        """Read summaries for one authorized agent thread."""
        ...

    async def preview(
        self, view: ViewContext, thread: ThreadRef, checkpoint_id: str
    ) -> ContentPreview:
        """Read a bounded decoded state with separate content permission."""
        ...


class DiagnosticReader(Protocol):
    """Bounded metadata and separately authorized content; no legacy reader changes."""

    async def query(
        self, view: ViewContext, query: QueryBinding, page: PageRequest
    ) -> DiagnosticPage:
        """Filter by the full scope and verify the cursor against all query filters."""
        ...

    async def preview(
        self, view: ViewContext, request: RequestRecord, content_ref: str
    ) -> ContentPreview:
        """Resolve a scoped opaque reference, never a browser-supplied URL or path."""
        ...


class ArtifactStore(Protocol):
    """Store immutable accepted versions with authorized opaque content references."""

    async def put(self, view: ViewContext, artifact: ScopedArtifact) -> None:
        """Persist a new accepted version; conflicting content for an ID is an error."""
        ...

    async def get(self, view: ViewContext, thread: ThreadRef, artifact_id: str) -> ScopedArtifact:
        """Read exactly this scoped thread's version, or return not_found."""
        ...
