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
@click.option(
    "--accept",
    is_flag=True,
    help="After the evals pass, save the report as the service's evals/baseline.json.",
)
@workspace_option
@pass_toolbox
def evals(tools: Toolbox, service: str | None, accept: bool, workspace: Path | None) -> None:
    """Run the evals of SERVICE, or of every service: its tests marked eval, with a real model.

    An eval skips itself until a real model is set in the service's .env. Each
    writes a report to the service's .agentlib/eval-report.json.
    --accept saves the passing report as evals/baseline.json; later runs fail
    on regressions against it. --accept still enforces the absolute bar: while
    the pytest run is in flight it leaves a marker beside the eval report, and
    the generated test skips only the regression gate against the existing
    baseline when that marker is there, moving the bar on purpose instead of
    deadlocking on it. The marker is removed once the run ends.
    """
    if accept and not service:
        raise CliError("--accept needs a service", fix="agentlib eval SERVICE --accept")
    root = workspace_root(workspace)
    target = service_folder(root, load_answers(root), service) if service else root
    arguments = ("-m", "eval", "-q", "-rs", target.relative_to(root).as_posix() if service else ".")
    click.echo(f"== evals: python -m pytest {' '.join(arguments)}")
    accept_marker = target / ".agentlib" / "eval-accept.flag"
    if accept:
        accept_marker.parent.mkdir(parents=True, exist_ok=True)
        accept_marker.touch()
    try:
        code = tools.run_tool("pytest", arguments, root)
    finally:
        if accept:
            accept_marker.unlink(missing_ok=True)
    # pytest says 5 when no test was collected: a service with no evals yet.
    if code == _NO_TESTS:
        raise CliError(
            f"there are no evals in {target.relative_to(root).as_posix() or 'this workspace'}",
            fix="add a test marked @pytest.mark.eval; 'agentlib new agent' writes one",
        )
    if code != 0:
        raise CliError("the evals did not meet their bar", fix="read the report above")
    if accept:
        report = target / ".agentlib" / "eval-report.json"
        if not report.is_file():
            raise CliError(
                f"there is no report at {report.relative_to(root).as_posix()}",
                fix="run the evals once without --accept, or check the service's eval test",
            )
        baseline = target / "evals" / "baseline.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(report.read_bytes())
        click.echo(f"baseline accepted: {baseline.relative_to(root).as_posix()}")


_NO_TESTS = 5
