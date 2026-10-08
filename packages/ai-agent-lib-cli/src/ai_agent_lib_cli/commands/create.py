"""The commands that write: a workspace, its services, their links and pins, and updates."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

import click

from ai_agent_lib_cli.commands.common import (
    library_source,
    pass_toolbox,
    port_of,
    print_report,
    workspace_option,
    workspace_root,
)
from ai_agent_lib_cli.commands.data_options import print_proposal, proposal_for, redshift_options
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.names import check_name, server_id_for
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import (
    AGENTS_FOLDER,
    MODEL_PROVIDERS,
    SERVERS_FOLDER,
    AgentAnswers,
    McpAnswers,
    WorkspaceAnswers,
    load_answers,
)

__all__ = ["init", "link", "new", "registry", "update"]

_FIRST_AGENT_PORT = 8000
_FIRST_SERVER_PORT = 8100


@click.command()
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
@pass_toolbox
def init(
    tools: Toolbox,
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
                library=library_source(lib_path, lib_version),
                agents=answers.agents,
                mcp_servers=answers.mcp_servers,
            )
    else:
        if name is None:
            raise click.UsageError("give the workspace a NAME, or --answers <agentlib.toml>")
        answers = WorkspaceAnswers(
            name=check_name("workspace", name),
            owner=tools.ask(owner, "Which team owns this workspace?", "platform-team"),
            library=library_source(lib_path, lib_version),
        )
    root = parent / (name or answers.name)
    report = tools.scaffolder().init(root, answers, force=force)
    click.echo(f"Workspace {answers.name} at {root}")
    print_report(report, root)
    click.echo(
        "\nNext:\n"
        f"  cd {root}\n"
        "  uv sync --all-packages\n"
        "  uv run agentlib new mcp hello-mcp\n"
        "  uv run agentlib new agent hello-agent --mcp hello\n"
        "  uv sync --all-packages && uv run pytest"
    )


@click.group()
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
@workspace_option
@pass_toolbox
def new_agent(
    tools: Toolbox,
    name: str,
    description: str | None,
    model_provider: str | None,
    mcp_servers: tuple[str, ...],
    port: int | None,
    force: bool,
    workspace: Path | None,
) -> None:
    """Add a hello-world LangGraph agent called NAME, with its tests."""
    root = workspace_root(workspace)
    answers = load_answers(root)
    agent = AgentAnswers(
        name=check_name("agent", name),
        description=tools.ask(description, "What does the agent do?", "Greets people by name."),
        model_provider=tools.ask(
            model_provider, f"Model provider ({', '.join(MODEL_PROVIDERS)})?", "fake"
        ),
        port=port or port_of(answers.agent(name), answers, _FIRST_AGENT_PORT),
        mcp_servers=mcp_servers,
    )
    if agent.model_provider not in MODEL_PROVIDERS:
        raise CliError(f"the model provider must be one of: {', '.join(MODEL_PROVIDERS)}")
    report = tools.scaffolder().add_agent(root, agent, force=force)
    click.echo(f"Agent {agent.name} in {AGENTS_FOLDER}/{agent.name}")
    print_report(report, root)
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
@click.option(
    "--from-csv",
    "csv_folder",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Propose the tools from the CSV files in this folder, and copy the files in.",
)
@click.option(
    "--from-openapi",
    "openapi",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Propose the tools from the GET operations of this OpenAPI 3 document.",
)
@click.option("--base-url", default=None, help="Where the API is. Default: the document's.")
@click.option(
    "--from-redshift",
    "redshift_schema",
    metavar="SCHEMA",
    default=None,
    help="Propose the tools from the tables of this Redshift schema. Needs --database.",
)
@redshift_options
@click.option("--source", default=None, help="The name of the data source. Default: the ID.")
@click.option(
    "--per-table",
    type=click.IntRange(1, 10),
    default=3,
    show_default=True,
    help="The most tools to propose for one table.",
)
@click.option(
    "--query", "only", multiple=True, help="Keep only this proposed query. May be repeated."
)
@click.option("--propose", is_flag=True, help="Show what would be proposed and write nothing.")
@workspace_option
@pass_toolbox
def new_mcp(
    tools: Toolbox,
    /,
    name: str,
    server_id: str | None,
    description: str | None,
    port: int | None,
    no_pin: bool,
    force: bool,
    propose: bool,
    workspace: Path | None,
    **data: Any,
) -> None:
    """Add an MCP server called NAME, with its tests.

    Without a data option the server is a hello-world over sample data.

    With --from-csv its tools are proposed from your own files: a lookup by
    the column that identifies a row, and a filter for each column that sorts
    rows into a few groups.

    With --from-openapi there is one tool for each GET operation that answers
    with JSON records. The API is not called; the tests answer for it.

    With --from-redshift the tools are proposed from the tables of a schema.
    Only the catalogue is read. The server runs locally over stand-in CSV
    files with the tables' columns, and on Redshift when deployed.

    In each case, columns that look personal are masked for analysts.
    """
    root = workspace_root(workspace)
    answers = load_answers(root)
    check_name("MCP server", name)
    sid = server_id or server_id_for(name)
    proposal = proposal_for(tools, sid.replace("-", "_"), data)
    if proposal is not None:
        print_proposal(sid, proposal)
    elif propose:
        raise CliError("--propose needs a data option such as --from-csv")
    if propose:
        return
    server = McpAnswers(
        name=name,
        server_id=sid,
        description=tools.ask(
            description,
            "What does the server offer?",
            proposal.description
            if proposal is not None
            else "A greeting and a people directory, over sample data.",
        ),
        port=port or port_of(answers.mcp_server(name), answers, _FIRST_SERVER_PORT),
        data=proposal.plan if proposal is not None else None,
    )
    scaffolder = tools.scaffolder()
    report = scaffolder.add_mcp(
        root,
        server,
        pin=False,
        force=force,
        data_files=proposal.data_files if proposal is not None else None,
        extra_files=proposal.extra_files if proposal is not None else None,
    )
    click.echo(
        f"MCP server {server.name} (registry ID {server.server_id}) in "
        f"{SERVERS_FOLDER}/{server.name}"
    )
    print_report(report, root)
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


@click.command()
@click.argument("agent")
@click.argument("server")
@workspace_option
@pass_toolbox
def link(tools: Toolbox, agent: str, server: str, workspace: Path | None) -> None:
    """Let AGENT call the tools of the MCP server SERVER."""
    root = workspace_root(workspace)
    report = tools.scaffolder().link(root, agent, server)
    click.echo(f"{agent} may now call the tools of {server}")
    print_report(report, root)
    linked = load_answers(root).mcp_server(server)
    if linked is not None:
        click.echo(
            f"\nFrom now on {agent} needs {linked.name} running: uv run {linked.name}\n"
            "The tests need nothing running: uv run pytest tests"
        )


@click.group()
def registry() -> None:
    """Maintain the registries of the workspace."""


@registry.command("pin")
@click.argument("server")
@workspace_option
@pass_toolbox
def registry_pin(tools: Toolbox, server: str, workspace: Path | None) -> None:
    """Pin the input schema of every tool of the MCP server SERVER."""
    root = workspace_root(workspace)
    report = tools.scaffolder().pin(root, server)
    click.echo(f"Pins of {server}")
    print_report(report, root)


@click.command()
@click.option(
    "--diff", "show_diff", is_flag=True, help="Show what would change in files you changed."
)
@workspace_option
@pass_toolbox
def update(tools: Toolbox, show_diff: bool, workspace: Path | None) -> None:
    """Bring generated files up to what this version of agentlib writes.

    A file you have not changed since it was generated is replaced. A file you
    changed is left alone and listed. Registries, rules, samples and .env
    files are never touched.
    """
    root = workspace_root(workspace)
    report = tools.scaffolder().update(root)
    for label, paths in (("updated", report.updated), ("created", report.created)):
        for path in paths:
            click.echo(f"  {label}  {path.as_posix()}")
    for path in report.kept:
        click.echo(f"  kept     {path.as_posix()}  (you changed it)")
        if show_diff:
            current = root.joinpath(*path.parts).read_text(encoding="utf-8")
            lines = difflib.unified_diff(
                current.splitlines(),
                report.new_text[path].splitlines(),
                fromfile=f"{path.as_posix()} (yours)",
                tofile=f"{path.as_posix()} (generated now)",
                lineterm="",
            )
            click.echo("\n".join(f"    {line}" for line in lines))
    if not (report.updated or report.created or report.kept):
        click.echo("  nothing to do: every generated file is up to date")
    elif report.kept and not show_diff:
        click.echo(
            "\nSee what the templates would change in the files you changed: agentlib update --diff"
        )
