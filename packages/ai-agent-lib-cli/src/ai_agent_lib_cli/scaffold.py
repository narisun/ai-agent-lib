"""What the commands do: create a workspace, add services to it, link them.

Every operation builds the complete set of files first and writes them in one
step, so a refused overwrite leaves the workspace exactly as it was.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from ai_agent_lib_cli.dataplan import API_RESPONSES_FILE
from ai_agent_lib_cli.envfiles import DEV_ROLE, agent_env, mcp_env, variable_names
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.formatting import Formatter, format_python
from ai_agent_lib_cli.governance import (
    ANALYST_ROWS,
    agent_entry,
    agent_rules,
    agent_samples,
    analyst_rule_id,
    link_rule,
    link_sample,
    rules_title,
    server_entry,
    server_offers,
    server_rules,
    server_samples,
)
from ai_agent_lib_cli.names import check_name, package_name
from ai_agent_lib_cli.pins import PinReader, read_pins
from ai_agent_lib_cli.policy import SAMPLES_FILE, samples_text
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
    AGENTS_FOLDER,
    SERVERS_FOLDER,
    WORKSPACE_FILE,
    AgentAnswers,
    McpAnswers,
    WorkspaceAnswers,
    load_answers,
)

__all__ = ["LOCK_FILE", "Scaffolder", "UpdateReport"]

# otel: a deployed service sends traces and metrics when its settings turn them on.
_AGENT_EXTRAS = "jwt,mcp,otel,serve"
_SERVER_EXTRAS = {"duckdb_csv": "duckdb,jwt,mcp,otel,serve", "rest": "jwt,mcp,otel,serve"}
_ENV, _ENV_EXAMPLE = ".env", ".env.example"

LOCK_FILE = "agentlib.lock"
"""Records what each generated file held when it was written, so that an update
can tell a file the developer changed from one they did not."""

_LOCK_SCHEMA = "agentlib.lock/v1"

# Files the tool maintains. A command may rewrite them; everything else is the developer's.
_MAINTAINED = frozenset(
    {
        AGENTS_FILE,
        TOOLS_FILE,
        RULES_FILE,
        SAMPLES_FILE,
        PurePosixPath(WORKSPACE_FILE),
        PurePosixPath(LOCK_FILE),
    }
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UpdateReport:
    """What an update did.

    Attributes:
        updated: Files the developer had not changed, now as the templates write them.
        created: Files the templates produce that were not there before.
        kept: Files the developer changed. They were left as they are.
        new_text: For each kept file, what the templates would write now.
    """

    updated: tuple[PurePosixPath, ...] = ()
    created: tuple[PurePosixPath, ...] = ()
    kept: tuple[PurePosixPath, ...] = ()
    new_text: Mapping[PurePosixPath, str] = field(default_factory=dict)


def _under(folder: str, name: str, files: Mapping[PurePosixPath, str]) -> dict[PurePosixPath, str]:
    return {PurePosixPath(folder, name) / path: text for path, text in files.items()}


class Scaffolder:
    """Creates workspaces and adds services to them.

    Coordinates answer models, pure rendering, schema pin discovery, formatting,
    and file writes. Templates and maintained-file lists define what can be
    regenerated; edited application files belong to the developer. These steps
    are not a filesystem transaction, so an I/O failure may leave partial output.

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
        files = self._workspace_files(answers)
        files[AGENTS_FILE] = agents_text(read_agents(root))
        files[TOOLS_FILE] = tools_text(read_servers(root))
        files[RULES_FILE] = rules_text(self._existing(root, RULES_FILE), [])
        files[SAMPLES_FILE] = samples_text(self._existing(root, SAMPLES_FILE), [])
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
        self,
        root: Path,
        server: McpAnswers,
        *,
        pin: bool = True,
        force: bool = False,
        data_files: Mapping[str, Path] | None = None,
        extra_files: Mapping[PurePosixPath, str] | None = None,
    ) -> Report:
        """Add an MCP server to the workspace at ``root``.

        Args:
            root: The workspace.
            server: The answers the server is generated from.
            pin: Pin the tools' input schemas straight away.
            force: Overwrite files that exist and differ.
            data_files: Files to copy into the server's ``data`` folder, by
                the name each gets there. They are the developer's data.
            extra_files: More text files for the server's folder.

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

        data_dir = root / SERVERS_FOLDER / server.name / "data"
        copies = {data_dir / name: path for name, path in (data_files or {}).items()}
        differing = sorted(
            target.name
            for target, path in copies.items()
            if target.exists() and not filecmp.cmp(target, path, shallow=False)
        )
        if differing and not force:
            raise CliError(
                f"these files exist in {data_dir.relative_to(root).as_posix()} and differ from "
                f"what would be copied: {', '.join(differing)}. Nothing was changed. "
                "Use --force to overwrite them."
            )

        files = self._server_files(answers, server)
        files.update(_under(SERVERS_FOLDER, server.name, extra_files or {}))
        files.update(self._own_settings(root, SERVERS_FOLDER, server.name, mcp_env(server)))
        servers = {entry.id: entry for entry in read_servers(root)}
        servers.setdefault(server.server_id, server_entry(server, answers.owner))
        files[TOOLS_FILE] = tools_text(list(servers.values()))
        files[RULES_FILE] = rules_text(
            self._existing(root, RULES_FILE),
            server_rules(server),
            title=rules_title(server),
        )
        files[SAMPLES_FILE] = samples_text(
            self._existing(root, SAMPLES_FILE), server_samples(server)
        )
        answers = answers.with_mcp_server(server)
        files[PurePosixPath(WORKSPACE_FILE)] = answers.to_toml()
        report = self._write(root, files, answers, force=force)
        report = report.merged(self._copy(copies))
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

        files = self._agent_files(answers, agent)
        files.update(self._own_settings(root, AGENTS_FOLDER, agent.name, agent_env(agent)))
        rules = agent_rules(agent.name)
        samples = agent_samples(agent.name)
        for server in servers:
            rules.append(link_rule(agent.name, server.server_id))
            samples.append(link_sample(agent.name, server))

        agents = {entry.id: entry for entry in read_agents(root)}
        known = agents.get(agent.name)
        agents[agent.name] = (
            replace(known, mcp_servers=tuple(sorted({*known.mcp_servers, *agent.mcp_servers})))
            if known is not None
            else agent_entry(agent, answers.owner)
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
        files[SAMPLES_FILE] = samples_text(self._existing(root, SAMPLES_FILE), samples)
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
        answers = answers.with_agent(linked)
        files = self._link_files(agent.name, server)
        # What the agent's files say about its servers follows the link, unless
        # the developer has made the file their own.
        described = self._untouched(root, self._agent_files(answers, linked), answers)
        files.update(described)
        files[AGENTS_FILE] = agents_text(agents)
        files[RULES_FILE] = rules_text(
            self._existing(root, RULES_FILE),
            [link_rule(agent.name, server.server_id)],
            title=f"{agent.name} may call the tools of the {server.server_id} server.",
        )
        files[SAMPLES_FILE] = samples_text(
            self._existing(root, SAMPLES_FILE), [link_sample(agent.name, server)]
        )
        files[PurePosixPath(WORKSPACE_FILE)] = answers.to_toml()
        return self._write(root, files, answers, refresh=frozenset(described))

    # ----------------------------------------------------------------- update

    def update(self, root: Path) -> UpdateReport:
        """Bring the generated files up to what the templates write now.

        A file the developer has not changed since it was generated is
        replaced. A file they changed, or deleted, is left alone. Registries,
        rules, samples and ``.env`` files are never touched.
        """
        answers = load_answers(root)
        files = self._workspace_files(answers)
        for server in answers.mcp_servers:
            files.update(self._server_files(answers, server))
            files[PurePosixPath(SERVERS_FOLDER, server.name, _ENV_EXAMPLE)] = mcp_env(server)
        for agent in answers.agents:
            files.update(self._agent_files(answers, agent))
            files[PurePosixPath(AGENTS_FOLDER, agent.name, _ENV_EXAMPLE)] = agent_env(agent)
        files = self._formatted(files, answers)

        lock = self._read_lock(root)
        write: dict[PurePosixPath, str] = {}
        updated: list[PurePosixPath] = []
        created: list[PurePosixPath] = []
        kept: dict[PurePosixPath, str] = {}
        for path, text in sorted(files.items()):
            current = self._existing(root, path)
            recorded = lock.get(path.as_posix())
            if current is None:
                if recorded is None:
                    created.append(path)
                    write[path] = text
                # Otherwise it was generated before and the developer removed it.
            elif current == text:
                lock[path.as_posix()] = _digest(text)
            elif recorded == _digest(current):
                updated.append(path)
                write[path] = text
            else:
                kept[path] = text
        write_files(root, write, force=True)
        lock.update({path.as_posix(): _digest(text) for path, text in write.items()})
        self._write_lock(root, lock)
        return UpdateReport(tuple(updated), tuple(created), tuple(sorted(kept)), kept)

    # ---------------------------------------------------------------- helpers

    def _workspace_files(self, answers: WorkspaceAnswers) -> dict[PurePosixPath, str]:
        return self._renderer.render(
            "workspace", {"name": answers.name, "owner": answers.owner, "library": answers.library}
        )

    def _server_files(
        self, answers: WorkspaceAnswers, server: McpAnswers
    ) -> dict[PurePosixPath, str]:
        plan = server.plan
        context = {
            "name": server.name,
            "package": package_name(server.name),
            "server_id": server.server_id,
            "description": server.description,
            "port": server.port,
            "plan": plan,
            "sample": plan.is_sample,
            "rest": plan.kind == "rest",
            "responses_file": API_RESPONSES_FILE.as_posix(),
            "offers": server_offers(plan),
            "analyst_rule": analyst_rule_id(server.server_id, plan),
            "analyst_rows": ANALYST_ROWS,
            "role": DEV_ROLE,
            "extras": _SERVER_EXTRAS[plan.kind],
            "library": answers.library,
            "var": variable_names(),
        }
        files = self._renderer.render("mcp", context)
        if plan.is_sample:
            files.update(self._renderer.render("mcp-sample", context))
        for query in plan.queries:
            files[PurePosixPath("queries", query.name + plan.suffix)] = query.definition
        return _under(SERVERS_FOLDER, server.name, files)

    def _agent_files(
        self, answers: WorkspaceAnswers, agent: AgentAnswers
    ) -> dict[PurePosixPath, str]:
        """Return the agent's own files and its tests with each server it is linked to."""
        servers = [self._server(answers, server_id) for server_id in agent.mcp_servers]
        context = {
            "name": agent.name,
            "package": package_name(agent.name),
            "description": agent.description,
            "model_provider": agent.model_provider,
            "servers": [server.name for server in servers],
            "port": agent.port,
            "extras": (
                _AGENT_EXTRAS + ",anthropic"
                if agent.model_provider == "anthropic"
                else _AGENT_EXTRAS
            ),
            "library": answers.library,
            "var": variable_names(),
        }
        files = _under(AGENTS_FOLDER, agent.name, self._renderer.render("agent", context))
        for server in servers:
            files.update(self._link_files(agent.name, server))
        return files

    def _link_files(self, agent: str, server: McpAnswers) -> dict[PurePosixPath, str]:
        return self._renderer.render(
            "link",
            {
                "agent": agent,
                "agent_package": package_name(agent),
                "server": server.name,
                "server_package": package_name(server.name),
                "server_id": server.server_id,
                "query": server.plan.queries[0],
                "sample": server.plan.is_sample,
                "rest": server.plan.kind == "rest",
                "responses_file": API_RESPONSES_FILE.as_posix(),
            },
        )

    def _formatted(
        self, files: Mapping[PurePosixPath, str], answers: WorkspaceAnswers
    ) -> dict[PurePosixPath, str]:
        packages = sorted(package_name(name) for name in answers.service_names())
        return self._formatter(files, packages)

    def _write(
        self,
        root: Path,
        files: Mapping[PurePosixPath, str],
        answers: WorkspaceAnswers,
        *,
        force: bool = False,
        refresh: frozenset[PurePosixPath] = frozenset(),
    ) -> Report:
        formatted = self._formatted(files, answers)
        report = write_files(root, formatted, replace=_MAINTAINED | refresh, force=force)
        lock = self._read_lock(root)
        lock.update(
            {
                path.as_posix(): _digest(text)
                for path, text in formatted.items()
                if path not in _MAINTAINED and path.name != _ENV
            }
        )
        self._write_lock(root, lock)
        return report

    @staticmethod
    def _copy(copies: Mapping[Path, Path]) -> Report:
        """Copy the developer's data files into place; report the ones that changed."""
        created, updated, unchanged = [], [], []
        for target, path in copies.items():
            if not target.exists():
                created.append(target)
            elif filecmp.cmp(target, path, shallow=False):
                unchanged.append(target)
                continue
            else:
                updated.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        return Report(tuple(created), tuple(updated), tuple(unchanged))

    def _untouched(
        self, root: Path, files: Mapping[PurePosixPath, str], answers: WorkspaceAnswers
    ) -> dict[PurePosixPath, str]:
        """Return the files of ``files`` that differ on disk and the developer has not changed."""
        lock = self._read_lock(root)
        stale: dict[PurePosixPath, str] = {}
        for path, text in self._formatted(files, answers).items():
            current = self._existing(root, path)
            if current is None or current == text:
                continue
            if lock.get(path.as_posix()) == _digest(current):
                stale[path] = text
        return stale

    @staticmethod
    def _read_lock(root: Path) -> dict[str, str]:
        path = root / LOCK_FILE
        if not path.is_file():
            return {}
        try:
            recorded = json.loads(path.read_text(encoding="utf-8")).get("files", {})
        except (OSError, ValueError, AttributeError):
            raise CliError(f"{path} cannot be read; delete it to start a new record") from None
        return {str(name): str(digest) for name, digest in dict(recorded).items()}

    @staticmethod
    def _write_lock(root: Path, lock: Mapping[str, str]) -> None:
        document = {"schema": _LOCK_SCHEMA, "files": dict(sorted(lock.items()))}
        text = json.dumps(document, indent=2) + "\n"
        write_files(root, {PurePosixPath(LOCK_FILE): text}, replace=_MAINTAINED)

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
    def _own_settings(root: Path, folder: str, name: str, text: str) -> dict[PurePosixPath, str]:
        """Return the service's settings files. An existing ``.env`` is never touched."""
        base = PurePosixPath(folder, name)
        files = {base / _ENV_EXAMPLE: text}
        if not (root / folder / name / _ENV).exists():
            files[base / _ENV] = text
        return files
