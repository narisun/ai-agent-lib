"""The Terraform that is derived from a plan: the task's policy and its settings."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ai_agent_lib_cli.deploy.plan import DeploymentPlan
from ai_agent_lib_core.contracts import Access
from ai_agent_lib_core.contracts.access import ACCOUNT, REGION

__all__ = [
    "aligned",
    "hcl_list",
    "hcl_string",
    "permissions_tf",
    "resource_expression",
    "settings_tf",
    "sid",
]


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
