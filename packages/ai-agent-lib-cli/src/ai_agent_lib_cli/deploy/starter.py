"""A starting ``deploy.env``: the aws profile with example values, for a first run."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_cli.deploy.plan import depends_on_aws
from ai_agent_lib_cli.deploy.target import AWS_DISTRIBUTION, Target
from ai_agent_lib_cli.render import Report, write_files
from ai_agent_lib_core.config import EnvSetting, Key, options_key, provider_key, render_env_file
from ai_agent_lib_core.contracts import Section

__all__ = ["next_steps", "starter_settings", "write_starter"]


def starter_settings(target: Target) -> str:
    """Return a starting ``deploy.env`` for ``target``: the aws profile with example values."""
    settings = [
        EnvSetting(
            Key.PROFILE, "aws", comment="The AWS adapters: Bedrock, Firehose, OPA and the rest."
        ),
        EnvSetting(
            Key.DEPLOYMENT_ENV,
            "dev",
            comment="dev or prod. Development adapters are refused in both.",
        ),
        EnvSetting(
            Key.TELEMETRY,
            "opentelemetry",
            comment="Traces and metrics, through the collector sidecar, to X-Ray and CloudWatch.",
        ),
    ]
    if target.kind == "agent":
        settings.append(
            EnvSetting(
                Key.MODEL_ID,
                "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
                comment="The Bedrock model or inference profile; the permissions name only it.",
            )
        )
    else:
        settings.append(
            EnvSetting(
                provider_key(Section.CHECKPOINT),
                "none",
                comment="An MCP server keeps no conversations.",
            )
        )
    examples: list[tuple[Section, dict[str, object], str]] = [
        (
            Section.SECRETS,
            {"prefix": f"eap/{target.name}/"},
            "Secrets Manager: only the secrets under this prefix can be read.",
        ),
        (Section.AUDIT, {"stream": "eap-audit"}, "The Firehose delivery stream for audit records."),
        (
            Section.IDENTITY,
            {
                "preset": "entra",
                "tenant_id": "00000000-0000-0000-0000-000000000000",
                "audience": f"api://{target.name}",
            },
            "Who may call the service: tokens from this Entra tenant, for this audience.",
        ),
        (
            Section.REGISTRY,
            {"bucket": "my-eap-registry"},
            "The bucket that holds the agent and MCP tool registries.",
        ),
        (
            Section.GUARDRAILS,
            {"guardrail_id": "abc123def456", "guardrail_version": "1"},
            "The Bedrock guardrail, at a published version.",
        ),
    ]
    if target.kind == "agent":
        examples.insert(
            3,
            (
                Section.CHECKPOINT,
                {
                    "host": "eap-checkpoints.cluster-abc.eu-west-1.rds.amazonaws.com",
                    "database": "eap",
                    "user": target.name.replace("-", "_"),
                },
                "The Postgres database that keeps conversations; the service signs in with IAM.",
            ),
        )
    settings += [
        EnvSetting(options_key(section), value, comment=comment)
        for section, value, comment in examples
    ]
    header = (
        f"How {target.name} runs in AWS. 'agentlib deploy {target.name}' reads this file and\n"
        "its values become the task's environment, so never put a secret here.\n"
        "Replace the example values, then run 'agentlib deploy "
        f"{target.name} --plan'.\n"
        "A relative path is relative to this file, as it is inside the image."
    )
    return render_env_file(settings, header=header)


def write_starter(root: Path, target: Target) -> Report:
    """Write the starting ``deploy.env`` of ``target`` under ``root``."""
    return write_files(root, {target.settings: starter_settings(target)})


def next_steps(root: Path, target: Target) -> list[str]:
    """What to do after the starting settings were written, one step per line."""
    steps = []
    if not depends_on_aws(root, target):
        pyproject = f"{target.folder}/pyproject.toml"
        # The starting settings select Bedrock and Postgres for an agent.
        extras = "[bedrock,postgres]" if target.kind == "agent" else ""
        steps.append(
            f'add "{AWS_DISTRIBUTION}{extras}" to the dependencies in {pyproject}, '
            "then run 'python -m ai_agent_lib_cli install'"
        )
    steps.append(f"fill in {target.settings}: its values are examples")
    steps.append(f"run 'agentlib deploy {target.name} --plan'")
    return steps
