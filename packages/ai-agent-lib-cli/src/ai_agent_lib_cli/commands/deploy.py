"""agentlib deploy: write what runs a service on ECS Fargate, from its settings."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import click

from ai_agent_lib_cli.commands.common import print_report, workspace_option, workspace_root
from ai_agent_lib_cli.deploy import (
    AWS_DISTRIBUTION,
    MODULE_FOLDER,
    OPA_FOLDER,
    depends_on_aws,
    deployment_files,
    maintained_files,
    plan_deployment,
    plan_lines,
    starter_settings,
    target_of,
)
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.render import write_files
from ai_agent_lib_cli.workspace import WorkspaceAnswers, load_answers

__all__ = ["deploy"]


def _bundle(answers: WorkspaceAnswers, given: Path | None) -> dict[PurePosixPath, str]:
    """Return the platform's Rego bundle, without its tests, by path inside it."""
    checkout = Path(answers.library.path) if answers.library.path else None
    folder = given or (checkout / "policies" / "bundle" if checkout else None)
    if folder is None or not folder.is_dir():
        raise CliError(
            "the platform's Rego bundle was not found",
            expected="the bundle the OPA sidecar decides with",
            actual=f"no folder at {folder}" if folder else "a workspace with no library checkout",
            fix="give its folder with --bundle",
        )
    return {
        PurePosixPath(path.relative_to(folder).as_posix()): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob("*"))
        if path.is_file() and not path.name.endswith("_test.rego")
    }


@click.command()
@click.argument("service")
@click.option(
    "--plan", "plan_only", is_flag=True, help="Show what would be deployed and why; write nothing."
)
@click.option(
    "--bundle",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="The platform's Rego bundle, for the OPA sidecar. Default: the library checkout's.",
)
@workspace_option
def deploy(service: str, plan_only: bool, bundle: Path | None, workspace: Path | None) -> None:
    """Write the Terraform and Dockerfiles that run SERVICE on ECS Fargate.

    The settings come from deploy.env in the service's folder; the first run
    writes one with example values. Every permission comes from the adapter
    that needs it, with the reason. Nothing is applied: you read the plan and
    run terraform yourself.
    """
    root = workspace_root(workspace)
    answers = load_answers(root)
    target = target_of(answers, service)
    if not plan_only and not root.joinpath(*target.settings.parts).is_file():
        report = write_files(root, {target.settings: starter_settings(target)})
        print_report(report, root)
        click.echo("\nNext:")
        if not depends_on_aws(root, target):
            pyproject = f"{target.folder}/pyproject.toml"
            # The starting settings select Bedrock and Postgres for an agent.
            extras = "[bedrock,postgres]" if target.kind == "agent" else ""
            click.echo(
                f'  add "{AWS_DISTRIBUTION}{extras}" to the dependencies in {pyproject}, '
                "then run 'uv sync --all-packages'"
            )
        click.echo(f"  fill in {target.settings}: its values are examples")
        click.echo(f"  run 'agentlib deploy {service} --plan'")
        return

    plan = plan_deployment(root, answers, target)
    for line in plan_lines(plan):
        click.echo(line)
    if plan.problems:
        count = len(plan.problems)
        raise CliError(
            f"{count} problem{'s' if count != 1 else ''} stop the deployment",
            fix=f"fix each problem above in {target.settings}, then run the command again",
        )
    if plan_only:
        return

    files = deployment_files(plan, _bundle(answers, bundle) if plan.opa else {})
    ours = maintained_files(target) | {
        path
        for path in files
        if path.is_relative_to(MODULE_FOLDER) or path.is_relative_to(OPA_FOLDER)
    }
    # A file written once is the developer's from then on: it is never overwritten.
    kept = sorted(
        path for path in files if path not in ours and root.joinpath(*path.parts).is_file()
    )
    report = write_files(
        root, {path: text for path, text in files.items() if path not in kept}, replace=ours
    )
    click.echo("")
    print_report(report, root)
    for path in kept:
        click.echo(f"  kept     {path.as_posix()} (yours)")
    folder = f"deploy/{target.name}"
    click.echo(
        f"\nNext: read {folder}/README.md, build the images, then run terraform in {folder}."
    )
