"""What the commands do: create a workspace, add services to it, link them.

Every operation builds the complete set of files first and writes them in one
step, so a refused overwrite leaves the workspace exactly as it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from ai_agent_lib_cli.envfiles import DATA_SOURCE, DEV_ROLE, agent_env, mcp_env, variable_names
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.formatting import Formatter, format_python
from ai_agent_lib_cli.names import check_name, package_name
from ai_agent_lib_cli.pins import PinReader, read_pins
from ai_agent_lib_cli.render import Report, TemplateRenderer, write_files
from ai_agent_lib_cli.shared import (
    AGENTS_FILE,
    RULES_FILE,
    TOOLS_FILE,
    agents_text,
    pinned,
    read_agents,
    read_servers,
    rules_text,
    tools_text,
)
from ai_agent_lib_cli.workspace import (
    WORKSPACE_FILE,
    AgentAnswers,
    McpAnswers,
    WorkspaceAnswers,
    load_answers,
)
from ai_agent_lib_core.contracts import AgentEntry, Classification, ServerEntry, ToolEntry

__all__ = ["AGENTS_FOLDER", "SERVERS_FOLDER", "Scaffolder"]

AGENTS_FOLDER = "agents"
SERVERS_FOLDER = "mcp-servers"

_VERSION = "0.1.0"
_CEILING = Classification.CONFIDENTIAL
_CEILING_NAME = _CEILING.name.lower()
_AGENT_EXTRAS = "jwt,mcp,serve"
_SERVER_EXTRAS = "duckdb,jwt,mcp,serve"
_ENV, _ENV_EXAMPLE = ".env", ".env.example"

# Files the tool maintains. A command may rewrite them; everything else is the developer's.
_MAINTAINED = frozenset({AGENTS_FILE, TOOLS_FILE, RULES_FILE, PurePosixPath(WORKSPACE_FILE)})


def _agent_rules(agent: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{agent}-uses-its-models",
            "actions": ["model.route"],
            "applications": [agent],
            "resources": ["default"],
        },
        {
            "id": f"{agent}-calls-its-own-tools",
            "actions": ["tool.call"],
            "applications": [agent],
            "resources": ["greet"],
        },
    ]


def _link_rule(agent: str, server_id: str) -> dict[str, Any]:
    return {
        "id": f"{agent}-calls-{server_id}",
        "actions": ["tool.call"],
        "applications": [agent],
        "resources": [f"{server_id}/*"],
        "max_classification": _CEILING_NAME,
    }


def _server_rules(server: McpAnswers) -> list[dict[str, Any]]:
    queries = {
        "actions": ["data.query"],
        "applications": [server.name],
        "resources": [f"{DATA_SOURCE}.*"],
        "max_classification": _CEILING_NAME,
    }
    return [
        {
            "id": f"{server.server_id}-staff-call-the-tools",
            "actions": ["tool.call"],
            "applications": [server.name],
            "roles": [DEV_ROLE, "manager"],
            "resources": [f"{server.server_id}/*"],
            "max_classification": _CEILING_NAME,
        },
        {"id": f"{server.server_id}-managers-see-everything", "roles": ["manager"], **queries},
        {
            "id": f"{server.server_id}-analysts-see-no-email",
            "roles": [DEV_ROLE],
            **queries,
            "obligations": {"mask_columns": ["email"], "max_rows": 50},
        },
    ]


def _server_entry(server: McpAnswers, owner: str) -> ServerEntry:
    return ServerEntry(
        id=server.server_id,
        owner=owner,
        url=f"http://127.0.0.1:{server.port}/mcp",
        audience=server.name,
        tools=(
            ToolEntry(
                name=f"{server.server_id}.greet",
                version=_VERSION,
                classification=Classification.INTERNAL,
                read_only=True,
                description="A greeting for a person.",
            ),
            ToolEntry(
                name=f"{server.server_id}.people_by_team",
                version=_VERSION,
                classification=_CEILING,
                read_only=True,
                description="The people in one team.",
            ),
        ),
    )


def _under(folder: str, name: str, files: Mapping[PurePosixPath, str]) -> dict[PurePosixPath, str]:
    return {PurePosixPath(folder, name) / path: text for path, text in files.items()}


class Scaffolder:
    """Creates workspaces and adds services to them.

    Args:
        renderer: Renders the templates.
        pin_reader: Asks a generated MCP server for the pins of its tools.
        formatter: Formats the generated Python.
    """

    def __init__(
        self,
        renderer: TemplateRenderer | None = None,
        *,
        pin_reader: PinReader | None = None,
        formatter: Formatter | None = None,
    ) -> None:
        self._renderer = renderer or TemplateRenderer()
        self._pin_reader = pin_reader or read_pins
        self._formatter = formatter or format_python

    # ------------------------------------------------------------- workspace

    def init(self, root: Path, answers: WorkspaceAnswers, *, force: bool = False) -> Report:
        """Create the workspace at ``root`` with every service ``answers`` lists.

        Raises:
            CliError: If a name is not usable, or a file would be overwritten.
        """
        check_name("workspace", answers.name)
        empty = replace(answers, agents=(), mcp_servers=())
        files = self._renderer.render(
            "workspace", {"name": answers.name, "owner": answers.owner, "library": answers.library}
        )
        files[AGENTS_FILE] = agents_text(read_agents(root))
        files[TOOLS_FILE] = tools_text(read_servers(root))
        files[RULES_FILE] = rules_text(self._existing(root, RULES_FILE), [])
        if not (root / WORKSPACE_FILE).is_file():
            files[PurePosixPath(WORKSPACE_FILE)] = empty.to_toml()
        report = self._write(root, files, answers, force=force)
        for server in answers.mcp_servers:
            report = report.merged(self.add_mcp(root, server, force=force))
        for agent in answers.agents:
            report = report.merged(self.add_agent(root, agent, force=force))
        return report

    # ---------------------------------------------------------------- servers

    def add_mcp(
        self, root: Path, server: McpAnswers, *, pin: bool = True, force: bool = False
    ) -> Report:
        """Add an MCP server to the workspace at ``root``.

        Raises:
            CliError: If the name or the registry ID is taken, or a file would
                be overwritten.
        """
        answers = load_answers(root)
        check_name("MCP server", server.name)
        check_name("server ID", server.server_id)
        self._check_free(answers, server.name, replacing=answers.mcp_server(server.name))
        holder = answers.mcp_server(server.server_id)
        if holder is not None and holder.name != server.name:
            raise CliError(f"the server ID {server.server_id!r} belongs to {holder.name}")

        package = package_name(server.name)
        context = {
            "name": server.name,
            "package": package,
            "server_id": server.server_id,
            "description": server.description,
            "port": server.port,
            "source": DATA_SOURCE,
            "role": DEV_ROLE,
            "extras": _SERVER_EXTRAS,
            "library": answers.library,
            "var": variable_names(),
        }
        files = _under(SERVERS_FOLDER, server.name, self._renderer.render("mcp", context))
        files.update(self._env_files(root, SERVERS_FOLDER, server.name, mcp_env(server)))

        servers = {entry.id: entry for entry in read_servers(root)}
        servers.setdefault(server.server_id, _server_entry(server, answers.owner))
        files[TOOLS_FILE] = tools_text(list(servers.values()))
        files[RULES_FILE] = rules_text(
            self._existing(root, RULES_FILE),
            _server_rules(server),
            title=(
                f"{server.name}: who may call its tools, and what each role sees of the data.\n"
                "Analysts see people but not their email; managers see every column."
            ),
        )
        answers = answers.with_mcp_server(server)
        files[PurePosixPath(WORKSPACE_FILE)] = answers.to_toml()
        report = self._write(root, files, answers, force=force)
        return report.merged(self.pin(root, server.name)) if pin else report

    def pin(self, root: Path, name_or_id: str) -> Report:
        """Write the schema pin of every tool of one MCP server to the registry.

        Raises:
            CliError: If the server is not in the workspace or cannot be asked.
        """
        server = load_answers(root).mcp_server(name_or_id)
        if server is None:
            raise CliError(f"there is no MCP server called {name_or_id!r} in this workspace")
        pins = self._pin_reader(root / SERVERS_FOLDER / server.name, package_name(server.name))
        servers = [
            pinned(entry, pins) if entry.id == server.server_id else entry
            for entry in read_servers(root)
        ]
        return write_files(root, {TOOLS_FILE: tools_text(servers)}, replace=_MAINTAINED)

    # ----------------------------------------------------------------- agents

    def add_agent(self, root: Path, agent: AgentAnswers, *, force: bool = False) -> Report:
        """Add an agent to the workspace at ``root``, linked to its MCP servers.

        Raises:
            CliError: If the name is taken, an MCP server is not in the
                workspace, or a file would be overwritten.
        """
        answers = load_answers(root)
        check_name("agent", agent.name)
        self._check_free(answers, agent.name, replacing=answers.agent(agent.name))
        servers = [self._server(answers, name) for name in agent.mcp_servers]
        agent = replace(agent, mcp_servers=tuple(sorted({s.server_id for s in servers})))

        package = package_name(agent.name)
        context = {
            "name": agent.name,
            "package": package,
            "description": agent.description,
            "model_provider": agent.model_provider,
            "servers": [server.name for server in servers],
            "port": agent.port,
            "extras": _AGENT_EXTRAS,
            "library": answers.library,
            "var": variable_names(),
        }
        files = _under(AGENTS_FOLDER, agent.name, self._renderer.render("agent", context))
        files.update(self._env_files(root, AGENTS_FOLDER, agent.name, agent_env(agent)))
        rules = _agent_rules(agent.name)
        for server in servers:
            files.update(self._link_files(agent.name, server))
            rules.append(_link_rule(agent.name, server.server_id))

        agents = {entry.id: entry for entry in read_agents(root)}
        known = agents.get(agent.name)
        agents[agent.name] = (
            replace(known, mcp_servers=tuple(sorted({*known.mcp_servers, *agent.mcp_servers})))
            if known is not None
            else AgentEntry(
                id=agent.name,
                owner=answers.owner,
                version=_VERSION,
                description=agent.description,
                mcp_servers=agent.mcp_servers,
                model_aliases=("default",),
                classification_ceiling=_CEILING,
            )
        )
        files[AGENTS_FILE] = agents_text(list(agents.values()))
        files[RULES_FILE] = rules_text(
            self._existing(root, RULES_FILE),
            rules,
            title=(
                f"{agent.name}: the models and tools it may use. A tool of an MCP server is\n"
                "named <server>/<tool>; the server decides for itself what the caller may see."
            ),
        )
        answers = answers.with_agent(agent)
        files[PurePosixPath(WORKSPACE_FILE)] = answers.to_toml()
        return self._write(root, files, answers, force=force)

    def link(self, root: Path, agent_name: str, server_name: str) -> Report:
        """Let an agent call the tools of an MCP server of the same workspace.

        Raises:
            CliError: If either is not in the workspace.
        """
        answers = load_answers(root)
        agent = answers.agent(agent_name)
        if agent is None:
            raise CliError(f"there is no agent called {agent_name!r} in this workspace")
        server = self._server(answers, server_name)
        linked = replace(agent, mcp_servers=tuple(sorted({*agent.mcp_servers, server.server_id})))

        entries = read_agents(root)
        if all(entry.id != agent.name for entry in entries):
            raise CliError(f"{agent.name} is not in {AGENTS_FILE}; add it there first")
        agents = [
            replace(entry, mcp_servers=tuple(sorted({*entry.mcp_servers, server.server_id})))
            if entry.id == agent.name
            else entry
            for entry in entries
        ]
        files = self._link_files(agent.name, server)
        files[AGENTS_FILE] = agents_text(agents)
        files[RULES_FILE] = rules_text(
            self._existing(root, RULES_FILE),
            [_link_rule(agent.name, server.server_id)],
            title=f"{agent.name} may call the tools of the {server.server_id} server.",
        )
        answers = answers.with_agent(linked)
        files[PurePosixPath(WORKSPACE_FILE)] = answers.to_toml()
        return self._write(root, files, answers)

    # ---------------------------------------------------------------- helpers

    def _write(
        self,
        root: Path,
        files: Mapping[PurePosixPath, str],
        answers: WorkspaceAnswers,
        *,
        force: bool = False,
    ) -> Report:
        packages = sorted(package_name(name) for name in answers.service_names())
        formatted = self._formatter(files, packages)
        return write_files(root, formatted, replace=_MAINTAINED, force=force)

    def _link_files(self, agent: str, server: McpAnswers) -> dict[PurePosixPath, str]:
        return self._renderer.render(
            "link",
            {
                "agent": agent,
                "agent_package": package_name(agent),
                "server": server.name,
                "server_package": package_name(server.name),
                "server_id": server.server_id,
            },
        )

    @staticmethod
    def _server(answers: WorkspaceAnswers, name_or_id: str) -> McpAnswers:
        server = answers.mcp_server(name_or_id)
        if server is None:
            known = ", ".join(s.name for s in answers.mcp_servers) or "none"
            raise CliError(
                f"there is no MCP server called {name_or_id!r} in this workspace "
                f"(MCP servers: {known}). Add it first with 'agentlib new mcp'."
            )
        return server

    @staticmethod
    def _check_free(answers: WorkspaceAnswers, name: str, *, replacing: object) -> None:
        if replacing is None and name in answers.service_names():
            raise CliError(f"the name {name!r} is already used by another service")

    @staticmethod
    def _existing(root: Path, relative: PurePosixPath) -> str | None:
        path = root.joinpath(*relative.parts)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    @staticmethod
    def _env_files(root: Path, folder: str, name: str, text: str) -> dict[PurePosixPath, str]:
        """Return the service's settings files. An existing ``.env`` is never touched."""
        base = PurePosixPath(folder, name)
        files = {base / _ENV_EXAMPLE: text}
        if not (root / folder / name / _ENV).exists():
            files[base / _ENV] = text
        return files
