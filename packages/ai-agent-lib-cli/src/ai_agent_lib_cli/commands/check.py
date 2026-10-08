"""'agentlib check': the gate a change passes before it is shared."""

from __future__ import annotations

from pathlib import Path

import click

from ai_agent_lib_cli.commands.common import pass_toolbox, workspace_option, workspace_root
from ai_agent_lib_cli.commands.rules import policy_test
from ai_agent_lib_cli.diagnostics import service_folder
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import load_answers

__all__ = ["CHECKS", "check", "evals"]

CHECKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("lint", "ruff", ("check", ".")),
    ("format", "ruff", ("format", "--check", ".")),
    ("types", "mypy", ()),
    ("tests", "pytest", ("-q",)),
)
"""Each check: its name, the tool module, and the tool's arguments."""


@click.command()
@click.option("--opa", "with_opa", is_flag=True, help="Also check that OPA agrees with the rules.")
@click.option("--keep-going", is_flag=True, help="Run every check even after one fails.")
@workspace_option
@click.pass_context
@pass_toolbox
def check(
    tools: Toolbox, context: click.Context, with_opa: bool, keep_going: bool, workspace: Path | None
) -> None:
    """Run what CI runs: lint, format, types, tests and the policy samples.

    The same command runs on a developer's machine and in the workspace's CI
    workflow, so a change that passes here passes there.
    """
    root = workspace_root(workspace)
    results: list[tuple[str, bool]] = []
    for name, module, arguments in CHECKS:
        click.echo(f"== {name}: python -m {module} {' '.join(arguments)}".rstrip())
        passed = tools.run_tool(module, arguments, root) == 0
        results.append((name, passed))
        if not passed and not keep_going:
            break
    if all(passed for _, passed in results) or keep_going:
        click.echo("== policy samples")
        try:
            context.invoke(policy_test, with_opa=with_opa, bundle=None, workspace=root)
        except CliError as problem:
            click.echo(f"agentlib: error: {problem}", err=True)
            results.append(("policy samples", False))
        else:
            results.append(("policy samples", True))
    click.echo("")
    for name, passed in results:
        click.echo(f"  {'ok  ' if passed else 'FAIL'}  {name}")
    failed = [name for name, passed in results if not passed]
    if failed:
        raise CliError(
            f"{len(failed)} check{'s' if len(failed) != 1 else ''} failed: {', '.join(failed)}",
            fix="read the output of each failed check above; run it alone to iterate",
        )
    click.echo("Every check passed.")


@click.command("eval")
@click.argument("service", required=False)
@workspace_option
@pass_toolbox
def evals(tools: Toolbox, service: str | None, workspace: Path | None) -> None:
    """Run the evals of SERVICE, or of every service: its tests marked eval, with a real model.

    An eval skips itself until a real model is set in the service's .env. Each
    writes a report to the service's .agentlib/eval-report.json.
    """
    root = workspace_root(workspace)
    target = service_folder(root, load_answers(root), service) if service else root
    arguments = ("-m", "eval", "-q", "-rs", str(target.relative_to(root)) if service else ".")
    click.echo(f"== evals: python -m pytest {' '.join(arguments)}")
    code = tools.run_tool("pytest", arguments, root)
    # pytest says 5 when no test was collected: a service with no evals yet.
    if code == _NO_TESTS:
        raise CliError(
            f"there are no evals in {target.relative_to(root).as_posix() or 'this workspace'}",
            fix="add a test marked @pytest.mark.eval; 'agentlib new agent' writes one",
        )
    if code != 0:
        raise CliError("the evals did not meet their bar", fix="read the report above")


_NO_TESTS = 5
