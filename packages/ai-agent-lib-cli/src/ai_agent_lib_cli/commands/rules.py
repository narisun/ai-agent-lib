"""The command that tries the rules: policy test."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from ai_agent_lib_cli.commands.common import pass_toolbox, workspace_option, workspace_root
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.policy import (
    SAMPLES_FILE,
    Outcome,
    decide_samples,
    load_samples,
    rules_engine,
)
from ai_agent_lib_cli.shared import RULES_FILE
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import load_answers

__all__ = ["policy"]


@click.group()
def policy() -> None:
    """Work with the rules of the workspace."""


def _print_outcomes(engine: str, outcomes: tuple[Outcome, ...]) -> int:
    click.echo(engine)
    for outcome in outcomes:
        decided = "allow" if outcome.allow else "deny"
        mark = "ok  " if outcome.passed else "FAIL"
        click.echo(f"  {mark}  {outcome.sample.name}: {decided} ({outcome.reason})")
        if not outcome.passed:
            wanted = outcome.sample.expect + (
                f" ({outcome.sample.reason})" if outcome.sample.reason else ""
            )
            click.echo(f"        expected: {wanted}")
    return sum(1 for outcome in outcomes if not outcome.passed)


@policy.command("test")
@click.option("--opa", "with_opa", is_flag=True, help="Also decide with OPA and compare.")
@click.option(
    "--bundle",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="The platform's Rego bundle. Default: the one in the library checkout.",
)
@workspace_option
@pass_toolbox
def policy_test(
    tools: Toolbox, with_opa: bool, bundle: Path | None, workspace: Path | None
) -> None:
    """Decide the sample requests in tests/policy-samples.yaml and compare with what they expect.

    The rules engine the services use locally decides them. With --opa, so
    does OPA with the platform's Rego bundle, and the two must agree.
    """
    root = workspace_root(workspace)
    answers = load_answers(root)
    samples = load_samples(root.joinpath(*SAMPLES_FILE.parts))
    if not samples:
        raise CliError(f"{SAMPLES_FILE} holds no samples")
    by_rules = asyncio.run(decide_samples(samples, rules_engine(root.joinpath(*RULES_FILE.parts))))
    failed = _print_outcomes("rules", by_rules)
    if with_opa:
        checkout = Path(answers.library.path) if answers.library.path else None
        chosen = bundle or (checkout / "policies" / "bundle" if checkout else None)
        if chosen is None:
            raise CliError("this workspace does not use a checkout of the library; give --bundle")

        async def decide_with_opa() -> tuple[Outcome, ...]:
            with tools.opa_server(chosen, root / "policies") as engine:
                try:
                    return await decide_samples(samples, engine)
                finally:
                    await engine.aclose()

        by_opa = asyncio.run(decide_with_opa())
        failed += _print_outcomes("opa", by_opa)
        differ = [
            ours.sample.name
            for ours, theirs in zip(by_rules, by_opa, strict=True)
            if (ours.allow, ours.reason) != (theirs.allow, theirs.reason)
        ]
        for name in differ:
            click.echo(f"  the two engines disagree on: {name}", err=True)
        failed += len(differ)
    if failed:
        raise CliError(f"{failed} sample{'s' if failed != 1 else ''} did not come out as expected")
    agreed = " The rules engine and OPA agree on every one." if with_opa else ""
    click.echo(f"{len(samples)} samples came out as expected.{agreed}")
