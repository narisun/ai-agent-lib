"""The Terraform that is derived from a plan: the task's policy and its settings."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ai_agent_lib_cli.deploy.plan import DeploymentPlan
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_core.config import options_key, provider_key, variable_for
from ai_agent_lib_core.contracts import Access, Section
from ai_agent_lib_core.contracts.access import ACCOUNT, REGION

__all__ = [
    "agentcore_settings_tf",
    "aligned",
    "hcl_list",
    "hcl_string",
    "permissions_tf",
    "resource_expression",
    "settings_tf",
    "sid",
]

_MEMORY_CHECKPOINT = "agentcore_memory"


def hcl_string(text: str) -> str:
    """Return ``text`` as a Terraform string literal that is never interpolated."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("${", "$${")
        .replace("%{", "%%{")
    )
    return f'"{escaped}"'


def hcl_list(items: Sequence[str]) -> str:
    """Return ``items`` as a Terraform list of string literals."""
    return "[" + ", ".join(hcl_string(item) for item in items) + "]"


def resource_expression(resource: str) -> str:
    """Return a resource with its region and account placeholders as Terraform expressions."""
    literal = hcl_string(resource)
    return literal.replace(REGION, "${var.region}").replace(
        ACCOUNT, "${data.aws_caller_identity.current.account_id}"
    )


def aligned(attributes: Sequence[tuple[str, str]], indent: str) -> list[str]:
    """Return ``name = value`` lines with the equals signs in one column, as terraform fmt does."""
    width = max((len(name) for name, _ in attributes), default=0)
    return [f"{indent}{name.ljust(width)} = {value}" for name, value in attributes]


def sid(label: str, number: int) -> str:
    """Return a statement ID: the label's words in CamelCase, then the number."""
    words = re.findall(r"[A-Za-z0-9]+", label)
    return "".join(word[:1].upper() + word[1:] for word in words) + str(number)


def permissions_tf(plan: DeploymentPlan, header: str) -> str:
    """Return ``permissions.tf``: one policy statement per access an adapter declared."""
    blocks: list[str] = []
    for adapter in plan.access.adapters:
        for number, access in enumerate(adapter.access, start=1):
            blocks.append(_statement(adapter.label, number, access))
    if not blocks:
        return (
            header
            + "\n# The adapters need nothing from AWS.\nlocals {\n  task_policy_json = null\n}\n"
        )
    identity = (
        'data "aws_caller_identity" "current" {}\n\n'
        if any(ACCOUNT in r for a in plan.access.access for r in a.resources)
        else ""
    )
    return (
        header
        + "# Each statement is what one adapter said it needs, and why.\n\n"
        + identity
        + 'data "aws_iam_policy_document" "task" {\n'
        + "\n".join(blocks)
        + "}\n\nlocals {\n  task_policy_json = data.aws_iam_policy_document.task.json\n}\n"
    )


def _statement(label: str, number: int, access: Access) -> str:
    lines = [f"  # {label}: {access.why}"]
    if access.note:
        lines.append(f"  # check: {access.note}")
    lines.append("  statement {")
    lines += aligned(
        [
            ("sid", hcl_string(sid(label, number))),
            ("actions", hcl_list(access.actions)),
            ("resources", "[" + ", ".join(resource_expression(r) for r in access.resources) + "]"),
        ],
        "    ",
    )
    lines.append("  }")
    return "\n".join(lines) + "\n"


def settings_tf(plan: DeploymentPlan, header: str) -> str:
    """Return ``settings.tf``: the command, port, sidecars and environment the module runs."""
    lines = [header.rstrip("\n"), "", "locals {"]
    lines += aligned(
        [
            ("command", hcl_list(plan.target.command)),
            ("port", str(plan.target.port)),
            ("uses_opa", "true" if plan.opa else "false"),
            ("collector", "true" if plan.collector else "false"),
        ],
        "  ",
    )
    lines.append("")
    if plan.environment:
        lines.append("  environment = {")
        lines += aligned([(n, hcl_string(v)) for n, v in plan.environment], "    ")
        lines.append("  }")
    else:
        lines.append("  environment = {}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _hcl_optional(text: str | None) -> str:
    return "null" if text is None else hcl_string(text)


def _environment_lines(plan: DeploymentPlan) -> list[str]:
    if not plan.environment:
        return ["  environment = {}"]
    lines = ["  environment = {"]
    lines += aligned([(n, hcl_string(v)) for n, v in plan.environment], "    ")
    lines.append("  }")
    return lines


def agentcore_settings_tf(plan: DeploymentPlan, header: str) -> str:
    """Return ``settings.tf`` of an AgentCore deployment: what the runtime is made of.

    The locals carry the protocol and port the service's kind fixes, the
    command, the ``AGENTCORE_`` block as Terraform values, and the container's
    environment. ``memory_option_variable`` names the checkpoint options
    variable Terraform rewrites with the ID of the memory it creates, or is
    ``null`` when the checkpoints do not go to a memory Terraform creates.

    Raises:
        CliError: If the plan is not an AgentCore deployment.
    """
    settings = plan.agentcore
    if settings is None:
        raise CliError(f"{plan.target.name} is planned for {plan.target.platform}, not agentcore")
    environment = dict(plan.environment)
    checkpoint = environment.get(provider_key_variable(Section.CHECKPOINT))
    memory_option_variable = (
        options_key_variable(Section.CHECKPOINT)
        if settings.create_memory and checkpoint == _MEMORY_CHECKPOINT
        else None
    )
    lines = [header.rstrip("\n"), "", "locals {"]
    lines += aligned(
        [
            ("protocol", hcl_string(plan.target.protocol)),
            ("port", str(plan.target.port)),
            ("command", hcl_list(plan.target.command)),
            ("auth", hcl_string(settings.auth)),
            ("discovery_url", _hcl_optional(settings.discovery_url)),
            ("allowed_clients", hcl_list(settings.allowed_clients)),
            ("allowed_audience", hcl_list(settings.allowed_audience)),
            ("allowed_scopes", hcl_list(settings.allowed_scopes)),
            ("network_mode", hcl_string(settings.network)),
            ("subnets", hcl_list(settings.subnets)),
            ("security_groups", hcl_list(settings.security_groups)),
            ("idle_timeout_seconds", str(settings.idle_timeout_seconds)),
            ("max_lifetime_seconds", str(settings.max_lifetime_seconds)),
            ("artifact", hcl_string(settings.artifact)),
            ("create_memory", "true" if settings.create_memory else "false"),
            ("memory_id", _hcl_optional(settings.memory_id)),
            ("gateway", hcl_string(settings.gateway)),
            ("memory_option_variable", _hcl_optional(memory_option_variable)),
        ],
        "  ",
    )
    lines.append("")
    lines += _environment_lines(plan)
    lines.append("}")
    return "\n".join(lines) + "\n"


def provider_key_variable(section: Section) -> str:
    """The variable that selects a section's adapter."""
    return variable_for(provider_key(section))


def options_key_variable(section: Section) -> str:
    """The variable that holds a section's adapter options."""
    return variable_for(options_key(section))
