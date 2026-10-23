"""agentcraft2 deploy: write what runs a service on Fargate or AgentCore, from its settings."""

from __future__ import annotations

from pathlib import Path

import click

from ai_agent_lib_cli.commands.common import print_report, workspace_option, workspace_root
from ai_agent_lib_cli.deploy import (
    AGENTCORE,
    FARGATE,
    PLATFORMS,
    next_steps,
    plan_deployment,
    plan_lines,
    read_bundle,
    target_of,
    write_deployment,
    write_starter,
)
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.workspace import load_answers

__all__ = ["deploy"]


@click.command()
@click.argument("service")
@click.option(
    "--target",
    "platform",
    type=click.Choice(PLATFORMS),
    default=FARGATE,
    show_default=True,
    help="Where the service runs: an ECS service on Fargate, or an AgentCore Runtime.",
)
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
def deploy(
    service: str, platform: str, plan_only: bool, bundle: Path | None, workspace: Path | None
) -> None:
    """Write the Terraform and Dockerfiles that run SERVICE on ECS Fargate or AgentCore Runtime.

    The settings come from deploy.env in the service's folder; the first run
    writes one with example values. Every permission comes from the adapter
    that needs it, with the reason. Nothing is applied: you read the plan and
    run terraform yourself.
    """
    root = workspace_root(workspace)
    answers = load_answers(root)
    target = target_of(answers, service, platform)
    if not plan_only and not root.joinpath(*target.settings.parts).is_file():
        print_report(write_starter(root, target), root)
        click.echo("\nNext:")
        for step in next_steps(root, target):
            click.echo(f"  {step}")
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

    report = write_deployment(root, plan, read_bundle(answers, bundle) if plan.opa else {})
    click.echo("")
    print_report(report.written, root)
    for path in report.kept:
        click.echo(f"  kept     {path.as_posix()} (yours)")
    folder = target.deployment.as_posix()
    if target.platform == AGENTCORE:
        artifact = (
            "build the zip"
            if (plan.agentcore and plan.agentcore.artifact == "code")
            else ("build and push the ARM64 image")
        )
        click.echo(f"\nNext: read {folder}/README.md, {artifact}, then run terraform in {folder}.")
    else:
        click.echo(
            f"\nNext: read {folder}/README.md, build the images, then run terraform in {folder}."
        )
