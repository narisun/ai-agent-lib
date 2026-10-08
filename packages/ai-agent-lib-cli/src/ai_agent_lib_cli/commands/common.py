"""What the commands share: the toolbox, the workspace option and how results are printed."""

from __future__ import annotations

from pathlib import Path

import click

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.render import Report
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import (
    AgentAnswers,
    LibrarySource,
    McpAnswers,
    WorkspaceAnswers,
    find_workspace,
)

__all__ = [
    "library_source",
    "pass_toolbox",
    "port_of",
    "print_report",
    "workspace_option",
    "workspace_root",
]

pass_toolbox = click.make_pass_decorator(Toolbox, ensure=True)
"""Gives a command the toolbox as its first argument: the one the command line
was started with, or the real one."""

workspace_option = click.option(
    "--workspace",
    "workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="A folder inside the workspace. Default: the current folder.",
)


def _checkout() -> Path | None:
    """Return the library checkout this command runs from, if it runs from one."""
    here = Path(__file__).resolve()
    for folder in here.parents:
        if (folder / "packages" / "ai-agent-lib-core" / "pyproject.toml").is_file():
            return folder
    return None


def library_source(path: Path | None, version: str | None) -> LibrarySource:
    """Return where a new workspace installs the library from."""
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


def workspace_root(workspace: Path | None) -> Path:
    """Return the root of the workspace a command works on."""
    return find_workspace(workspace or Path.cwd())


def port_of(
    existing: AgentAnswers | McpAnswers | None, answers: WorkspaceAnswers, first: int
) -> int:
    """Return the port a service keeps, or the first one no service uses."""
    if existing is not None:
        return existing.port
    port = first
    while port in answers.used_ports():
        port += 1
    return port


def print_report(report: Report, root: Path) -> None:
    """Print which files a command created or updated."""
    for label, paths in (("created", report.created), ("updated", report.updated)):
        for path in paths:
            click.echo(f"  {label}  {path.relative_to(root).as_posix()}")
    if not report.created and not report.updated:
        click.echo("  nothing to do: every file is already as it would be written")
