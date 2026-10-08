"""The ``agentlib`` command line."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ai_agent_lib_cli import __version__
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.names import check_name, server_id_for
from ai_agent_lib_cli.render import Report
from ai_agent_lib_cli.scaffold import AGENTS_FOLDER, SERVERS_FOLDER, Scaffolder
from ai_agent_lib_cli.workspace import (
    MODEL_PROVIDERS,
    AgentAnswers,
    LibrarySource,
    McpAnswers,
    WorkspaceAnswers,
    find_workspace,
    load_answers,
)

__all__ = ["cli"]

_FIRST_AGENT_PORT = 8000
_FIRST_SERVER_PORT = 8100
_CONTEXT = {"help_option_names": ["-h", "--help"]}

_workspace_option = click.option(
    "--workspace",
    "workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="A folder inside the workspace. Default: the current folder.",
)


def _ask(given: str | None, question: str, default: str) -> str:
    """Return an answer: the one given, or one typed at a terminal, or the default."""
    if given is not None:
        return given
    if sys.stdin.isatty():
        return str(click.prompt(question, default=default))
    return default


def _checkout() -> Path | None:
    """Return the library checkout this command runs from, if it runs from one."""
    here = Path(__file__).resolve()
    for folder in here.parents:
        if (folder / "packages" / "ai-agent-lib-core" / "pyproject.toml").is_file():
            return folder
    return None


def _library(path: Path | None, version: str | None) -> LibrarySource:
    if path is not None and version is not None:
        raise click.UsageError("give --lib-path or --lib-version, not both")
    if version is not None:
        return LibrarySource(kind="index", version=version)
    checkout = path or _checkout()
    if checkout is None:
        raise CliError(
            "this command is not running from a checkout of the library, so it cannot tell "
            "where a workspace should install the library from. Give --lib-path <checkout> "
            "or --lib-version '<specifier>'."
        )
    if not (checkout / "packages" / "ai-agent-lib-core" / "pyproject.toml").is_file():
        raise CliError(f"{checkout} is not a checkout of the library")
    return LibrarySource.from_path(checkout)


def _root(workspace: Path | None) -> Path:
    return find_workspace(workspace or Path.cwd())


def _port_of(
    existing: AgentAnswers | McpAnswers | None, answers: WorkspaceAnswers, first: int
) -> int:
    """Return the port a service keeps, or the first one no service uses."""
    if existing is not None:
        return existing.port
    port = first
    while port in answers.used_ports():
        port += 1
    return port


def _report(report: Report, root: Path) -> None:
    for label, paths in (("created", report.created), ("updated", report.updated)):
        for path in paths:
            click.echo(f"  {label}  {path.relative_to(root).as_posix()}")
    if not report.created and not report.updated:
        click.echo("  nothing to do: every file is already as it would be written")


@click.group(context_settings=_CONTEXT)
@click.version_option(__version__, "-V", "--version", prog_name="agentlib")
def cli() -> None:
    """Create and grow a local workspace of agents and MCP servers.

    Start with 'agentlib init <name>'. Every command writes working code with
    its tests; nothing it writes needs an account or a network connection.
    """


@cli.command()
@click.argument("name", required=False)
@click.option(
    "--dir",
    "parent",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    help="Where to create the workspace folder. Default: the current folder.",
)
@click.option("--owner", default=None, help="The team that owns the workspace.")
@click.option(
    "--lib-path",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=None,
    help="A local checkout of ai-agent-lib to install the library from.",
)
@click.option(
    "--lib-version", default=None, help="Install the library from a package index, e.g. '>=0.1'."
)
@click.option(
    "--answers",
    "answers_file",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="Generate the workspace an agentlib.toml describes, services included.",
)
@click.option("--force", is_flag=True, help="Overwrite files that exist and differ.")
def init(
    name: str | None,
    parent: Path,
    owner: str | None,
    lib_path: Path | None,
    lib_version: str | None,
    answers_file: Path | None,
    force: bool,
) -> None:
    """Create a workspace folder called NAME."""
    if answers_file is not None:
        answers = WorkspaceAnswers.from_toml(
            answers_file.read_text(encoding="utf-8"), what=str(answers_file)
        )
        if lib_path is not None or lib_version is not None:
            answers = WorkspaceAnswers(
                name=answers.name,
                owner=answers.owner,
                library=_library(lib_path, lib_version),
                agents=answers.agents,
                mcp_servers=answers.mcp_servers,
            )
    else:
        if name is None:
            raise click.UsageError("give the workspace a NAME, or --answers <agentlib.toml>")
        answers = WorkspaceAnswers(
            name=check_name("workspace", name),
            owner=_ask(owner, "Which team owns this workspace?", "platform-team"),
            library=_library(lib_path, lib_version),
        )
    root = parent / (name or answers.name)
    report = Scaffolder().init(root, answers, force=force)
    click.echo(f"Workspace {answers.name} at {root}")
    _report(report, root)
    click.echo(
        "\nNext:\n"
        f"  cd {root}\n"
        "  uv sync --all-packages\n"
        "  uv run agentlib new mcp hello-mcp\n"
        "  uv run agentlib new agent hello-agent --mcp hello\n"
        "  uv sync --all-packages && uv run pytest"
    )


@cli.group()
def new() -> None:
    """Add a service to the workspace."""


@new.command("agent")
@click.argument("name")
@click.option("--description", default=None, help="One line on what the agent does.")
@click.option(
    "--model",
    "model_provider",
    type=click.Choice(MODEL_PROVIDERS),
    default=None,
    help="The model provider. 'fake' needs no account. Default: fake.",
)
@click.option(
    "--mcp", "mcp_servers", multiple=True, help="An MCP server the agent may call. Repeatable."
)
@click.option("--port", type=click.IntRange(1024, 65535), default=None, help="Local HTTP port.")
@click.option("--force", is_flag=True, help="Overwrite files that exist and differ.")
@_workspace_option
def new_agent(
    name: str,
    description: str | None,
    model_provider: str | None,
    mcp_servers: tuple[str, ...],
    port: int | None,
    force: bool,
    workspace: Path | None,
) -> None:
    """Add a hello-world LangGraph agent called NAME, with its tests."""
    root = _root(workspace)
    answers = load_answers(root)
    agent = AgentAnswers(
        name=check_name("agent", name),
        description=_ask(description, "What does the agent do?", "Greets people by name."),
        model_provider=_ask(
            model_provider, f"Model provider ({', '.join(MODEL_PROVIDERS)})?", "fake"
        ),
        port=port or _port_of(answers.agent(name), answers, _FIRST_AGENT_PORT),
        mcp_servers=mcp_servers,
    )
    if agent.model_provider not in MODEL_PROVIDERS:
        raise CliError(f"the model provider must be one of: {', '.join(MODEL_PROVIDERS)}")
    report = Scaffolder().add_agent(root, agent, force=force)
    click.echo(f"Agent {agent.name} in {AGENTS_FOLDER}/{agent.name}")
    _report(report, root)
    linked = [load_answers(root).mcp_server(name) for name in mcp_servers]
    start = "".join(
        f"  uv run {server.name}        # in another terminal: the agent calls it\n"
        for server in linked
        if server is not None
    )
    click.echo(
        "\nNext:\n"
        "  uv sync --all-packages\n"
        f"  uv run pytest {AGENTS_FOLDER}/{agent.name}\n"
        f'{start}  uv run {agent.name} "Say hello to Ada."'
    )


@new.command("mcp")
@click.argument("name")
@click.option("--server-id", default=None, help="The server's ID in the tool registry.")
@click.option("--description", default=None, help="One line on what the server offers.")
@click.option("--port", type=click.IntRange(1024, 65535), default=None, help="Local HTTP port.")
@click.option("--no-pin", is_flag=True, help="Do not pin the tools' input schemas now.")
@click.option("--force", is_flag=True, help="Overwrite files that exist and differ.")
@_workspace_option
def new_mcp(
    name: str,
    server_id: str | None,
    description: str | None,
    port: int | None,
    no_pin: bool,
    force: bool,
    workspace: Path | None,
) -> None:
    """Add a hello-world MCP server called NAME, with sample data and its tests."""
    root = _root(workspace)
    answers = load_answers(root)
    server = McpAnswers(
        name=check_name("MCP server", name),
        server_id=server_id or server_id_for(name),
        description=_ask(
            description,
            "What does the server offer?",
            "A greeting and a people directory, over sample data.",
        ),
        port=port or _port_of(answers.mcp_server(name), answers, _FIRST_SERVER_PORT),
    )
    scaffolder = Scaffolder()
    report = scaffolder.add_mcp(root, server, pin=False, force=force)
    click.echo(
        f"MCP server {server.name} (registry ID {server.server_id}) in "
        f"{SERVERS_FOLDER}/{server.name}"
    )
    _report(report, root)
    if not no_pin:
        try:
            scaffolder.pin(root, server.name)
            click.echo("  pinned   the input schema of each tool in registry/mcp-tools.yaml")
        except CliError as problem:
            click.echo(
                f"  not pinned: {problem}\n"
                f"  Pin later with: agentlib registry pin {server.server_id}",
                err=True,
            )
    click.echo(
        "\nNext:\n"
        "  uv sync --all-packages\n"
        f"  uv run pytest {SERVERS_FOLDER}/{server.name}\n"
        f"  uv run {server.name}"
    )


@cli.command()
@click.argument("agent")
@click.argument("server")
@_workspace_option
def link(agent: str, server: str, workspace: Path | None) -> None:
    """Let AGENT call the tools of the MCP server SERVER."""
    root = _root(workspace)
    report = Scaffolder().link(root, agent, server)
    click.echo(f"{agent} may now call the tools of {server}")
    _report(report, root)
    linked = load_answers(root).mcp_server(server)
    if linked is not None:
        click.echo(
            f"\nFrom now on {agent} needs {linked.name} running: uv run {linked.name}\n"
            "The tests need nothing running: uv run pytest tests"
        )


@cli.group()
def registry() -> None:
    """Maintain the registries of the workspace."""


@registry.command("pin")
@click.argument("server")
@_workspace_option
def registry_pin(server: str, workspace: Path | None) -> None:
    """Pin the input schema of every tool of the MCP server SERVER."""
    root = _root(workspace)
    report = Scaffolder().pin(root, server)
    click.echo(f"Pins of {server}")
    _report(report, root)
