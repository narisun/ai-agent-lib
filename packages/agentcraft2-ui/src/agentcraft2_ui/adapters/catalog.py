"""Reuse the public registry parser and authorize server-pinned local bindings."""

import asyncio
from collections.abc import Mapping
from pathlib import Path

from agentcraft2_core.contracts import AgentSnapshot, Principal, ToolSnapshot
from agentcraft2_core.kit import RegistryDocument, load_registries
from agentcraft2_ui.application.catalog import bind_agent, same_agent_binding
from agentcraft2_ui.config.settings import Settings
from agentcraft2_ui.contracts.catalog import (
    AgentBinding,
    AgentCoreRuntime,
    McpSummary,
    PresentationManifest,
)
from agentcraft2_ui.contracts.queries import (
    ContractError,
    ErrorCode,
    Page,
    PageRequest,
    Permission,
    ViewContext,
)


def read_registries(agents_path: Path, tools_path: Path) -> tuple[AgentSnapshot, ToolSnapshot]:
    """Read a fresh, bounded snapshot; missing files are configuration errors."""

    def document(path: Path) -> RegistryDocument:
        with path.open("rb") as source:
            content = source.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("registry files must be at most 2 MiB")
        return RegistryDocument(content=content, suffix=path.suffix, what="UI registry")

    return load_registries(agents=document(agents_path), tools=document(tools_path))


def pin_bindings(
    agents: AgentSnapshot,
    agent_ids: tuple[str, ...],
    allowed_urls: frozenset[str],
    *,
    artifact_agent_ids: frozenset[str] = frozenset(),
    runtimes: Mapping[str, AgentCoreRuntime] | None = None,
) -> tuple[AgentBinding, ...]:
    """Explicit operator selection establishes supported question-answer bindings.

    ``runtimes`` names the selected agents hosted on AgentCore Runtime; their
    binding URL is derived from the ARN, so the registry entry may omit ``url``.
    """
    result: list[AgentBinding] = []
    runtimes = runtimes or {}
    if set(runtimes) - set(agent_ids):
        raise ValueError("every --agentcore-runtime agent must also be selected with --agent")
    for agent_id in agent_ids:
        entry = agents.get(agent_id)
        if entry is None:
            raise ValueError(f"agent {agent_id!r} is absent from the registry")
        if not entry.url and agent_id not in runtimes:
            raise ValueError(f"add a loopback url to registry agent {agent_id!r}")
        manifest = PresentationManifest(
            agent_id=entry.id,
            agent_version=entry.version,
            registry_revision=agents.revision,
            title=entry.id,
            api_audience=entry.id,
            capabilities=("artifacts",) if agent_id in artifact_agent_ids else (),
        )
        result.append(
            bind_agent(
                manifest,
                entry,
                registry_revision=agents.revision,
                allowed_urls=allowed_urls,
                local=True,
                runtime=runtimes.get(agent_id),
            )
        )
    return tuple(result)


class LocalAccess:
    """A configured local identity can use only explicitly selected agent IDs.

    This UI permission check supplements the agent's own policy enforcement. It
    grants scoped diagnostics only when enabled in server settings, never by headers.
    """

    def __init__(self, settings: Settings) -> None:
        self.principal = settings.identity.principal()
        self.agent_ids = frozenset(binding.manifest.agent_id for binding in settings.bindings)
        self.inspector_enabled = settings.inspector_enabled

    def owns(self, principal: Principal) -> None:
        """Require the configured tenant, subject and roles."""
        if (principal.tenant, principal.subject, principal.roles) != (
            self.principal.tenant,
            self.principal.subject,
            self.principal.roles,
        ):
            raise ContractError(ErrorCode.NOT_FOUND)

    async def require(self, view: ViewContext, action: Permission) -> None:
        """Fail closed for other identities, agents and unsupported permissions."""
        self.owns(view.principal)
        allowed = {
            Permission.CATALOG_READ,
            Permission.AGENT_INVOKE,
            Permission.CONVERSATION_READ,
            Permission.CONVERSATION_WRITE,
            Permission.ARTIFACT_READ,
        }
        if self.inspector_enabled:
            allowed.update({Permission.DIAGNOSTIC_METADATA, Permission.DIAGNOSTIC_CONTENT})
        if view.scope.application not in self.agent_ids or action not in allowed:
            raise ContractError(ErrorCode.NOT_FOUND)


class FileCatalog:
    """Refresh registry metadata at selection and dispatch; never follow redirects."""

    def __init__(self, settings: Settings, access: LocalAccess) -> None:
        self.settings, self.access = settings, access

    async def resolve(self, principal: Principal, agent_id: str) -> AgentBinding:
        """Allow unrelated registry edits while retaining the selected agent's pin."""
        self.access.owns(principal)
        pinned = next((b for b in self.settings.bindings if b.manifest.agent_id == agent_id), None)
        if pinned is None:
            raise ContractError(ErrorCode.NOT_FOUND)
        agents, _ = await asyncio.to_thread(
            read_registries, self.settings.agents_path, self.settings.tools_path
        )
        entry = agents.get(agent_id)
        if entry is None:
            raise ContractError(ErrorCode.NOT_FOUND)
        current = bind_agent(
            pinned.manifest.model_copy(update={"registry_revision": agents.revision}),
            entry,
            registry_revision=agents.revision,
            allowed_urls=self.settings.allowed_urls,
            local=True,
            runtime=pinned.runtime,
        )
        if not same_agent_binding(pinned, current):
            raise ContractError(ErrorCode.STALE_BINDING)
        return current

    async def agents(self, principal: Principal, page: PageRequest) -> Page[AgentBinding]:
        """List authorized agents; an unusable pin fails check instead of disappearing."""
        self.access.owns(principal)
        if page.cursor:
            raise ContractError(ErrorCode.INVALID_INPUT)
        if len(self.settings.bindings) > page.limit:
            raise ContractError(ErrorCode.INVALID_INPUT)
        bindings = [
            await self.resolve(principal, b.manifest.agent_id) for b in self.settings.bindings
        ]
        return Page(items=tuple(bindings))

    async def mcps(self, principal: Principal, page: PageRequest) -> Page[McpSummary]:
        """Expose only MCP metadata referenced by authorized agents, without endpoints."""
        await self.agents(principal, PageRequest(limit=200))
        agents, tools = await asyncio.to_thread(
            read_registries, self.settings.agents_path, self.settings.tools_path
        )
        visible = {
            server
            for agent in agents.entries()
            if agent.id in self.access.agent_ids
            for server in agent.mcp_servers
        }
        entries = tuple(
            McpSummary(
                id=server.id,
                owner=server.owner,
                tool_names=tuple(tool.name for tool in server.tools),
            )
            for server in tools.entries()
            if server.id in visible
        )
        if page.cursor or len(entries) > page.limit:
            raise ContractError(ErrorCode.INVALID_INPUT)
        return Page(items=entries)
