"""A starting ``deploy.env``: the aws profile with example values, for a first run."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_cli.deploy.agentcore import (
    V_ALLOWED_AUDIENCE,
    V_ALLOWED_CLIENTS,
    V_ALLOWED_SCOPES,
    V_ARTIFACT,
    V_AUTH,
    V_DISCOVERY_URL,
    V_GATEWAY,
    V_IDLE_TIMEOUT,
    V_MAX_LIFETIME,
    V_MEMORY,
    V_NETWORK,
    V_SECURITY_GROUPS,
    V_SUBNETS,
)
from ai_agent_lib_cli.deploy.plan import depends_on_aws
from ai_agent_lib_cli.deploy.target import AGENTCORE, AWS_DISTRIBUTION, Target
from ai_agent_lib_cli.render import Report, write_files
from ai_agent_lib_core.config import EnvSetting, Key, options_key, provider_key, render_env_file
from ai_agent_lib_core.contracts import Section

__all__ = ["next_steps", "starter_settings", "write_starter"]

_MEMORY_PLACEHOLDER = "created_by_terraform"
# The options a memory checkpoint takes when Terraform creates the memory: the
# ID is a placeholder until the first apply replaces it in the runtime's environment.
_MEMORY_CHECKPOINT_OPTIONS = {"memory_id": _MEMORY_PLACEHOLDER}


def _platform_settings(target: Target) -> list[EnvSetting]:
    """The profile, the environment and telemetry, for the platform the service goes to."""
    settings = [
        EnvSetting(
            Key.PROFILE, "aws", comment="The AWS adapters: Bedrock, Firehose, OPA and the rest."
        ),
        EnvSetting(
            Key.DEPLOYMENT_ENV,
            "dev",
            comment="dev or prod. Development adapters are refused in both.",
        ),
    ]
    if target.platform == AGENTCORE:
        settings.append(
            EnvSetting(
                Key.TELEMETRY,
                "ambient",
                comment=(
                    "ambient: the ADOT distribution the runtime starts the service under owns the\n"
                    "tracer and meter providers; the library installs none of its own."
                ),
            )
        )
        settings.append(
            EnvSetting(
                provider_key(Section.POLICY),
                "opa",
                comment=(
                    "No sidecar runs beside a container on AgentCore Runtime: decide policy\n"
                    "in-process with rules, or at an OPA the runtime can reach over the network."
                ),
            )
        )
        settings.append(
            EnvSetting(
                options_key(Section.POLICY),
                {"url": "https://opa.internal:8181"},
                comment="A remote OPA. For in-process rules: policy provider rules, no url.",
            )
        )
    else:
        settings.append(
            EnvSetting(
                Key.TELEMETRY,
                "opentelemetry",
                comment=(
                    "Traces and metrics, through the collector sidecar, to X-Ray and CloudWatch."
                ),
            )
        )
    return settings


def _checkpoint_example(target: Target) -> tuple[Section, dict[str, object], str]:
    if target.platform == AGENTCORE:
        return (
            Section.CHECKPOINT,
            dict(_MEMORY_CHECKPOINT_OPTIONS),
            "AgentCore Memory keeps the conversations. Terraform creates the memory (see\n"
            f"{V_MEMORY} below) and puts its ID here in the runtime's environment.",
        )
    return (
        Section.CHECKPOINT,
        {
            "host": "eap-checkpoints.cluster-abc.eu-west-1.rds.amazonaws.com",
            "database": "eap",
            "user": target.name.replace("-", "_"),
        },
        "The Postgres database that keeps conversations; the service signs in with IAM.",
    )


def _agentcore_block(target: Target) -> str:
    """The ``AGENTCORE_`` block: the deployment's own settings, kept out of the container."""
    memory = "create" if target.kind == "agent" else "none"
    lines = [
        "",
        "# ---- The deployment itself. These AGENTCORE_ variables never reach the container.",
        "",
        "# Who may invoke the runtime: iam (callers sign their requests and need",
        "# bedrock-agentcore:InvokeAgentRuntime) or jwt (bearer tokens from the issuer below).",
        f"{V_AUTH}=iam",
        f"# {V_DISCOVERY_URL}=https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration",
        f"# {V_ALLOWED_CLIENTS}=<client id>,<client id>",
        f"# {V_ALLOWED_AUDIENCE}=api://{target.name}",
        f"# {V_ALLOWED_SCOPES}=",
        "",
        "# PUBLIC, or VPC with the subnets and security groups the sessions run in.",
        f"{V_NETWORK}=PUBLIC",
        f"# {V_SUBNETS}=subnet-0123456789abcdef0,subnet-0fedcba9876543210",
        f"# {V_SECURITY_GROUPS}=sg-0123456789abcdef0",
        "",
        "# A session ends after this long idle, and after this long in any case (seconds).",
        f"{V_IDLE_TIMEOUT}=900",
        f"{V_MAX_LIFETIME}=28800",
        "",
        "# container: an ARM64 image pushed to the ECR repository Terraform creates.",
        "# code: a zip of the service and its wheels, built by build_zip.py, in S3.",
        f"{V_ARTIFACT}=container",
        "",
        "# none, create (Terraform creates one in front of the runtime, with the workspace's",
        "# MCP servers as targets) or the ARN of a gateway that exists.",
        f"{V_GATEWAY}=none",
        "",
        "# none, create (Terraform creates the memory the checkpoints go to) or the ID of a",
        "# memory that exists, which must then be the memory_id of the checkpoint options.",
        f"{V_MEMORY}={memory}",
    ]
    return "\n".join(lines) + "\n"


def starter_settings(target: Target) -> str:
    """Return a starting ``deploy.env`` for ``target``: the aws profile with example values."""
    settings = _platform_settings(target)
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
        examples.insert(3, _checkpoint_example(target))
        if target.platform == AGENTCORE:
            settings.append(
                EnvSetting(
                    provider_key(Section.CHECKPOINT),
                    "agentcore_memory",
                    comment="Where conversations are kept: agentcore_memory, postgres or none.",
                )
            )
    settings += [
        EnvSetting(options_key(section), value, comment=comment)
        for section, value, comment in examples
    ]
    command = f"agentlib deploy {target.name}"
    where, whose = "in AWS", "task's"
    if target.platform == AGENTCORE:
        command += " --target agentcore"
        where, whose = "on AgentCore Runtime", "container's"
    header = (
        f"How {target.name} runs {where}. '{command}' reads this file and\n"
        f"its values become the {whose} environment, so never put a secret here.\n"
        f"Replace the example values, then run '{command} --plan'.\n"
        "A relative path is relative to this file, as it is inside the image."
    )
    text = render_env_file(settings, header=header)
    if target.platform == AGENTCORE:
        text += _agentcore_block(target)
    return text


def write_starter(root: Path, target: Target) -> Report:
    """Write the starting ``deploy.env`` of ``target`` under ``root``."""
    return write_files(root, {target.settings: starter_settings(target)})


def next_steps(root: Path, target: Target) -> list[str]:
    """What to do after the starting settings were written, one step per line."""
    steps = []
    command = f"agentlib deploy {target.name}"
    if target.platform == AGENTCORE:
        command += " --target agentcore"
    if not depends_on_aws(root, target):
        pyproject = f"{target.folder}/pyproject.toml"
        # The starting settings select Bedrock, and for an agent the conversation store.
        if target.kind != "agent":
            extras = ""
        elif target.platform == AGENTCORE:
            extras = "[bedrock,agentcore]"
        else:
            extras = "[bedrock,postgres]"
        steps.append(
            f'add "{AWS_DISTRIBUTION}{extras}" to the dependencies in {pyproject}, '
            "then run 'python -m ai_agent_lib_cli install'"
        )
    steps.append(f"fill in {target.settings}: its values are examples")
    steps.append(f"run '{command} --plan'")
    return steps
