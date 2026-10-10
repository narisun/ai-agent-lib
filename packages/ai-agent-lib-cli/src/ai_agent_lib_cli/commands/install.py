"""Install a workspace with the current interpreter's pip."""

from __future__ import annotations

import tomllib
from pathlib import Path

import click

from ai_agent_lib_cli.commands.common import pass_toolbox, workspace_option, workspace_root
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import AGENTS_FOLDER, SERVERS_FOLDER, load_answers

__all__ = ["install", "installation_arguments"]


def installation_arguments(root: Path, *, dev: bool = True) -> tuple[str, ...]:
    """Return pip arguments using local projects and their declared dependencies.

    All projects are resolved together, so pip checks their dependency constraints
    and uses editable library checkouts without needing workspace-aware tooling.
    """
    answers = load_answers(root)
    arguments = ["install"]
    if answers.library.kind == "path":
        checkout = Path(answers.library.path or "")
        for package in ("ai-agent-lib-core", "ai-agent-lib-aws", "ai-agent-lib-cli"):
            arguments.extend(("--editable", str(checkout / "packages" / package)))
    if dev:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        requirements = project.get("dependency-groups", {}).get("dev", [])
        if not all(isinstance(item, str) for item in requirements):
            raise CliError("the dev dependency group must contain requirement strings")
        arguments.extend(requirements)
    for folder, services in (
        (AGENTS_FOLDER, answers.agents),
        (SERVERS_FOLDER, answers.mcp_servers),
    ):
        for service in services:
            arguments.extend(("--editable", str(root / folder / service.name)))
    if len(arguments) == 1:
        raise CliError("there are no projects or dependencies to install")
    return tuple(arguments)


@click.command()
@click.option("--no-dev", is_flag=True, help="Omit the workspace's development tools.")
@workspace_option
@pass_toolbox
def install(tools: Toolbox, no_dev: bool, workspace: Path | None) -> None:
    """Install every workspace service and its dependencies with Python's pip.

    Run inside a virtual environment. Run again after adding a service or changing
    its pyproject.toml. No Docker, uv, or administrator privileges are needed.
    """
    root = workspace_root(workspace)
    if tools.run_tool("pip", installation_arguments(root, dev=not no_dev), root) != 0:
        raise CliError("pip could not install the workspace", fix="read pip's output above")
