"""The commands that look and run without writing: doctor, config explain, run and graph."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from ai_agent_lib_cli.commands.common import pass_toolbox, workspace_option, workspace_root
from ai_agent_lib_cli.diagnostics import (
    check_service,
    check_workspace,
    explain_service,
    service_folder,
)
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.names import package_name
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import AGENTS_FOLDER, load_answers
from ai_agent_lib_core.contracts import CheckResult

__all__ = ["config", "doctor", "graph", "run"]


def _print_checks(title: str, results: tuple[CheckResult, ...]) -> int:
    """Print one group of results and return how many failed."""
    click.echo(title)
    for result in results:
        mark = "ok  " if result.ok else "FAIL"
        click.echo(f"  {mark}  {result.name}" + (f": {result.detail}" if result.detail else ""))
        if not result.ok and result.fix:
            click.echo(f"        fix: {result.fix}")
    return sum(1 for result in results if not result.ok)


@click.command()
@click.argument("service", required=False)
@click.option("--no-pins", is_flag=True, help="Do not start each MCP server to compare its pins.")
@workspace_option
@pass_toolbox
def doctor(tools: Toolbox, service: str | None, no_pins: bool, workspace: Path | None) -> None:
    """Check what the workspace, or one SERVICE, needs in order to run.

    Each service is built from its .env file and everything it is configured
    to use is checked. Nothing is changed. The exit code is 1 if a check fails.
    """
    root = workspace_root(workspace)
    answers = load_answers(root)
    names = [service] if service is not None else sorted(answers.service_names())
    failed = 0
    if service is None:
        shared = check_workspace(root, answers, pin_reader=None if no_pins else tools.read_pins)
        failed += _print_checks("workspace", shared)
    for name in names:
        folder = service_folder(root, answers, name)
        failed += _print_checks(folder.name, asyncio.run(check_service(folder)))
    if failed:
        raise CliError(f"{failed} check{'s' if failed != 1 else ''} failed")
    click.echo("Everything checked is usable.")


@click.group()
def config() -> None:
    """Look at how a service is configured."""


@config.command("explain")
@click.argument("service")
@workspace_option
def config_explain(service: str, workspace: Path | None) -> None:
    """Show every setting of SERVICE and where it came from. Secrets are masked."""
    root = workspace_root(workspace)
    folder = service_folder(root, load_answers(root), service)
    settings, warnings = explain_service(folder)
    width = max(len(setting.variable) for setting in settings)
    for setting in settings:
        click.echo(f"{setting.variable.ljust(width)}  {setting.value}")
        click.echo(f"{' ' * width}    from {setting.origin}")
    for warning in warnings:
        click.echo(f"warning: {warning}", err=True)


@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("service")
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
@workspace_option
@click.pass_context
@pass_toolbox
def run(
    tools: Toolbox,
    ctx: click.Context,
    service: str,
    arguments: tuple[str, ...],
    workspace: Path | None,
) -> None:
    """Start SERVICE and keep it running: an agent's HTTP entry point, or an MCP server.

    Anything after the name is passed to the service, for example --port 9000.
    Stop it with Ctrl-C; requests in flight finish first.
    """
    root = workspace_root(workspace)
    answers = load_answers(root)
    folder = service_folder(root, answers, service)
    package = package_name(folder.name)
    module = f"{package}.service" if answers.agent(service) is not None else f"{package}.__main__"
    ctx.exit(tools.run_service(folder, module, arguments))


@click.command()
@click.argument("agent")
@workspace_option
@pass_toolbox
def graph(tools: Toolbox, agent: str, workspace: Path | None) -> None:
    """Print the graph of AGENT as a Mermaid diagram.

    The graph is built with the agent's own tools. Tools of MCP servers are
    not loaded, so no server has to be running.
    """
    root = workspace_root(workspace)
    answers = load_answers(root)
    if answers.agent(agent) is None:
        raise CliError(f"there is no agent called {agent!r} in this workspace")
    click.echo(tools.read_graph(root / AGENTS_FOLDER / agent, package_name(agent)).rstrip())
