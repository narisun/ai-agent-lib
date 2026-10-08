"""agentlib deploy: derive a service's deployment from the settings it runs with.

The settings a service runs with in AWS live in ``deploy.env`` in its folder.
From them this module works out which adapters run, what each needs from IAM
(each adapter says so itself), which sidecars the task needs, and anything a
person must check. It then writes a Terraform root module for the service
beside a reference module that runs it on ECS Fargate.

Nothing here calls AWS, and no secret is read or written: ``deploy.env`` may
not hold one.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.render import TemplateRenderer
from ai_agent_lib_cli.workspace import AGENTS_FOLDER, SERVERS_FOLDER, WorkspaceAnswers
from ai_agent_lib_core.config import (
    ConfigResolver,
    DotenvConfigSource,
    EnvSetting,
    Key,
    options_key,
    provider_key,
    render_env_file,
    secret_key,
    variable_for,
)
from ai_agent_lib_core.contracts import (
    Access,
    ConfigurationError,
    DeploymentEnv,
    Section,
    ServiceConfig,
    TelemetryMode,
)
from ai_agent_lib_core.contracts.access import ACCOUNT, REGION
from ai_agent_lib_core.di import AccessPlan, ServiceProviders, access_plan

__all__ = [
    "AWS_DISTRIBUTION",
    "DEPLOY_ENV",
    "MODULE_FOLDER",
    "OPA_FOLDER",
    "DeploymentPlan",
    "Target",
    "depends_on_aws",
    "deployment_files",
    "hcl_string",
    "maintained_files",
    "plan_deployment",
    "plan_lines",
    "starter_settings",
    "target_of",
]

DEPLOY_ENV = "deploy.env"
"""The file in a service's folder that holds the settings it runs with in AWS."""

MODULE_FOLDER = PurePosixPath("deploy", "modules", "agentlib-service")
"""Where the reference Terraform module is copied in a workspace."""

OPA_FOLDER = PurePosixPath("deploy", "opa")
"""Where the OPA sidecar's Dockerfile and the platform bundle are copied."""

_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the container's port is reached through the task's network
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
AWS_DISTRIBUTION = "ai-agent-lib-aws"
"""The distribution that holds the AWS adapters a deployed service selects."""
_HEADER = (
    "# Written by 'agentlib deploy {name}' from {source}.\n"
    "# Do not edit: change that file and run the command again.\n"
)


@dataclass(frozen=True, slots=True)
class Target:
    """The service being deployed.

    Attributes:
        name: The service's name.
        kind: ``agent`` or ``mcp``.
        folder: Its folder, relative to the workspace root.
        port: The port it listens on.
    """

    name: str
    kind: str
    folder: PurePosixPath
    port: int

    @property
    def command(self) -> tuple[str, ...]:
        """The command that serves it in the container."""
        program = f"{self.name}-serve" if self.kind == "agent" else self.name
        return (program, "--host", _ALL_INTERFACES, "--port", str(self.port))

    @property
    def settings(self) -> PurePosixPath:
        """Its ``deploy.env``, relative to the workspace root."""
        return self.folder / DEPLOY_ENV


def target_of(answers: WorkspaceAnswers, name: str) -> Target:
    """Return the service called ``name``.

    Raises:
        CliError: If the workspace has no such service.
    """
    agent = answers.agent(name)
    if agent is not None:
        return Target(agent.name, "agent", PurePosixPath(AGENTS_FOLDER, agent.name), agent.port)
    server = answers.mcp_server(name)
    if server is not None:
        return Target(server.name, "mcp", PurePosixPath(SERVERS_FOLDER, server.name), server.port)
    known = ", ".join(sorted(answers.service_names())) or "none"
    raise CliError(
        f"there is no service called {name!r} in this workspace",
        expected="the name of an agent or MCP server of this workspace",
        actual=f"{name!r}; the services are: {known}",
    )


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """What deploying one service takes.

    Attributes:
        target: The service.
        environment: The task's environment: each variable of ``deploy.env``
            and its value, in the file's order.
        access: What each selected adapter needs from AWS.
        opa: Whether the task needs the OPA sidecar.
        collector: Whether the task needs the telemetry collector sidecar.
        problems: What stops the deployment. Nothing is written while any remain.
        warnings: What a person should know or check.
    """

    target: Target
    environment: tuple[tuple[str, str], ...]
    access: AccessPlan
    opa: bool = False
    collector: bool = False
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())


def _read_settings(root: Path, target: Target) -> tuple[ServiceConfig, list[tuple[str, str]]]:
    path = root.joinpath(*target.settings.parts)
    if not path.is_file():
        raise CliError(
            f"{target.settings} does not exist",
            expected=f"the settings {target.name} runs with in AWS, in {target.settings}",
            actual="no such file",
            fix=f"run 'agentlib deploy {target.name}' to write a starting one, then fill it in",
        )
    source = DotenvConfigSource(path, required=True)
    try:
        resolution = ConfigResolver(source, base_dir=path.parent).explain()
    except ConfigurationError as error:
        error.add_note(f"in {target.settings}")
        raise
    secret_variables = sorted(
        origin.removeprefix("variable ")
        for key, origin in resolution.origins.items()
        if key.startswith(secret_key(""))
    )
    if secret_variables:
        raise CliError(
            f"{target.settings} holds secrets, and its values become the task's environment",
            expected="no secret values: the service reads its secrets from Secrets Manager",
            actual="secret variables " + ", ".join(secret_variables),
            fix=f"remove them from {target.settings} and store each in Secrets Manager",
        )
    config = resolution.config
    if config.deployment_env is DeploymentEnv.LOCAL:
        variable = variable_for(Key.DEPLOYMENT_ENV)
        raise CliError(
            f"{target.settings} describes a developer's machine, not a deployment",
            expected=f"{variable}=dev or {variable}=prod",
            actual=f"{variable} is local",
            fix=f"set {variable}=dev in {target.settings}",
        )
    environment = [(name, value) for name in source.names() if (value := source.get(name))]
    return config, environment


def _uses_opa_beside_it(config: ServiceConfig) -> bool:
    selection = config.sections.get(Section.POLICY)
    if selection is None or selection.provider != "opa":
        return False
    url = selection.options.get("url", "http://localhost:8181")
    return isinstance(url, str) and (urlsplit(url).hostname or "") in _LOOPBACK


def _local_data_sources(root: Path, target: Target) -> set[str]:
    """The data sources the service's committed local settings name."""
    path = root.joinpath(*target.folder.parts, ".env.example")
    if not path.is_file():
        return set()
    text = DotenvConfigSource(path).get(variable_for(Key.DATA_SOURCES))
    try:
        found = json.loads(text) if text else {}
    except ValueError:
        return set()
    return set(found) if isinstance(found, dict) else set()


_REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_extras(root: Path, target: Target) -> dict[str, set[str]]:
    """The service's own dependencies: each distribution, with the extras it asks for."""
    path = root.joinpath(*target.folder.parts, "pyproject.toml")
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    found: dict[str, set[str]] = {}
    for requirement in project.get("dependencies", []):
        match = _REQUIREMENT.match(str(requirement))
        if match is None:
            continue
        extras = {item.strip() for item in (match["extras"] or "").split(",") if item.strip()}
        found.setdefault(_normalized(match["name"]), set()).update(extras)
    return found


def depends_on_aws(root: Path, target: Target) -> bool:
    """Whether the service's own dependencies include the AWS adapters."""
    return AWS_DISTRIBUTION in installed_extras(root, target)


def _missing_dependencies(
    root: Path, target: Target, access: AccessPlan, registry: ServiceProviders
) -> list[str]:
    """What the image would lack: a distribution, or an extra, a selected adapter needs."""
    have = installed_extras(root, target)
    needed: dict[str, dict[str, list[str]]] = {}
    for adapter in access.adapters:
        spec = registry.lookup(adapter.port, adapter.name)
        distribution = _normalized(spec.factory.__module__.split(".", 1)[0])
        extras = needed.setdefault(distribution, {})
        key = spec.extra or ""
        extras.setdefault(key, []).append(adapter.label)
    problems = []
    for distribution, extras in sorted(needed.items()):
        wanted = sorted(extra for extra in extras if extra)
        missing = [extra for extra in wanted if extra not in have.get(distribution, set())]
        if distribution in have and not missing:
            continue
        if distribution not in have and distribution == _normalized("ai-agent-lib-core"):
            continue  # core always comes with the service's own dependencies
        labels = sorted({label for group in extras.values() for label in group})
        requirement = f"{distribution}[{','.join(wanted)}]" if wanted else distribution
        what = (
            f"the extras {', '.join(missing)} of {distribution}"
            if distribution in have
            else distribution
        )
        problems.append(
            f"{', '.join(labels)} need {what}, which {target.name} does not install; "
            f'list "{requirement}" in the dependencies in {target.folder}/pyproject.toml '
            "and run 'uv sync --all-packages'"
        )
    return problems


def plan_deployment(
    root: Path,
    answers: WorkspaceAnswers,
    target: Target,
    providers: ServiceProviders | None = None,
) -> DeploymentPlan:
    """Work out what deploying ``target`` takes, from its ``deploy.env``.

    Raises:
        CliError: If ``deploy.env`` is missing, holds secrets or describes a
            developer's machine.
        ConfigurationError: If a setting is not valid, or selects an adapter
            that is not installed.
    """
    config, environment = _read_settings(root, target)
    registry = providers if providers is not None else ServiceProviders.default()
    access = access_plan(config, registry)
    problems = [
        f"{adapter.label} runs only on a developer's machine; select another in {target.settings}"
        for adapter in access.local_only
    ]
    warnings = [
        f"{adapter.label} does not say what it needs from AWS, so permissions.tf has nothing "
        "for it; add what it needs by hand"
        for adapter in access.undeclared
    ]
    problems += _missing_dependencies(root, target, access, registry)
    collector = config.telemetry is TelemetryMode.OPENTELEMETRY
    core = installed_extras(root, target).get("ai-agent-lib-core", set())
    if collector and "otel" not in core:
        problems.append(
            f"telemetry is on, but {target.name} does not install the OpenTelemetry SDK; "
            f'add "otel" to the extras of ai-agent-lib-core in {target.folder}/pyproject.toml'
        )
    missing = sorted(_local_data_sources(root, target) - set(config.data_sources))
    if missing:
        warnings.append(
            f"the service reads the data sources {', '.join(missing)} locally, but "
            f"{target.settings} does not configure them, so the deployed service cannot "
            f"find them; set {variable_for(Key.DATA_SOURCES)} there"
        )
    if config.external.aws_profile is not None:
        warnings.append(
            f"{variable_for(Key.AWS_PROFILE)} is set; a task signs in as its role, so remove it "
            f"from {target.settings}"
        )
    if answers.library.kind == "path":
        warnings.append(
            "this workspace installs the library from a local checkout, which an image build "
            "cannot reach; publish the library to a package index before building the image"
        )
    return DeploymentPlan(
        target=target,
        environment=tuple(environment),
        access=access,
        opa=_uses_opa_beside_it(config),
        collector=collector,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------- what is shown


def plan_lines(plan: DeploymentPlan) -> list[str]:
    """Return the plan as a person reads it."""
    target = plan.target
    lines = [f"Deploying {target.name} with the settings in {target.settings}.", ""]
    lines.append("Adapters and what each needs from AWS:")
    for adapter in plan.access.adapters:
        if not adapter.declared:
            lines.append(f"  {adapter.label}: does not say")
        elif not adapter.access:
            lines.append(f"  {adapter.label}: nothing")
        else:
            lines.append(f"  {adapter.label}:")
            for access in adapter.access:
                lines.append(f"    {', '.join(access.actions)}  ({access.why})")
                lines += [f"      on {resource}" for resource in access.resources]
                if access.note:
                    lines.append(f"      check: {access.note}")
    lines += ["", "Containers in the task:", f"  service: {' '.join(target.command)}"]
    if plan.opa:
        lines.append("  opa: decides policy on the task's loopback address (deploy/opa/Dockerfile)")
    if plan.collector:
        lines.append("  collector: sends traces to X-Ray and metrics to CloudWatch")
    lines += ["", f"Environment: {len(plan.environment)} variables from {target.settings}."]
    lines += [f"  {name}" for name, _ in plan.environment]
    if plan.warnings:
        lines += ["", "Warnings:", *(f"  - {warning}" for warning in plan.warnings)]
    if plan.problems:
        lines += ["", "Problems (nothing is written until they are fixed):"]
        lines += [f"  - {problem}" for problem in plan.problems]
    return lines


# --------------------------------------------------------------- what is written


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


def _hcl_list(items: Sequence[str]) -> str:
    return "[" + ", ".join(hcl_string(item) for item in items) + "]"


def _resource(resource: str) -> str:
    """A resource with its placeholders as Terraform expressions."""
    literal = hcl_string(resource)
    return literal.replace(REGION, "${var.region}").replace(
        ACCOUNT, "${data.aws_caller_identity.current.account_id}"
    )


def _aligned(attributes: Sequence[tuple[str, str]], indent: str) -> list[str]:
    width = max((len(name) for name, _ in attributes), default=0)
    return [f"{indent}{name.ljust(width)} = {value}" for name, value in attributes]


def _sid(label: str, number: int) -> str:
    words = re.findall(r"[A-Za-z0-9]+", label)
    return "".join(word[:1].upper() + word[1:] for word in words) + str(number)


def _permissions_tf(plan: DeploymentPlan, header: str) -> str:
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
    lines += _aligned(
        [
            ("sid", hcl_string(_sid(label, number))),
            ("actions", _hcl_list(access.actions)),
            ("resources", "[" + ", ".join(_resource(r) for r in access.resources) + "]"),
        ],
        "    ",
    )
    lines.append("  }")
    return "\n".join(lines) + "\n"


def _settings_tf(plan: DeploymentPlan, header: str) -> str:
    lines = [header.rstrip("\n"), "", "locals {"]
    lines += _aligned(
        [
            ("command", _hcl_list(plan.target.command)),
            ("port", str(plan.target.port)),
            ("uses_opa", "true" if plan.opa else "false"),
            ("collector", "true" if plan.collector else "false"),
        ],
        "  ",
    )
    lines.append("")
    if plan.environment:
        lines.append("  environment = {")
        lines += _aligned([(n, hcl_string(v)) for n, v in plan.environment], "    ")
        lines.append("  }")
    else:
        lines.append("  environment = {}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def maintained_files(target: Target) -> frozenset[PurePosixPath]:
    """The files ``agentlib deploy`` rewrites every time; the rest are written once."""
    folder = PurePosixPath("deploy", target.name)
    return frozenset({folder / "settings.tf", folder / "permissions.tf"})


def deployment_files(
    plan: DeploymentPlan,
    bundle: Mapping[PurePosixPath, str],
    renderer: TemplateRenderer | None = None,
) -> dict[PurePosixPath, str]:
    """Return every file a deployment of ``plan`` needs, by path from the workspace root.

    Args:
        plan: The plan, with no problems left.
        bundle: The platform's Rego bundle, by path inside it. Copied only when
            the task needs the OPA sidecar.
        renderer: Renders the templates.
    """
    render = (renderer or TemplateRenderer()).render
    target = plan.target
    context: dict[str, object] = {
        "name": target.name,
        "folder": target.folder.as_posix(),
        "port": target.port,
        "command_json": json.dumps(list(target.command)),
    }
    files = {MODULE_FOLDER / path: text for path, text in render("deploy-module", {}).items()}
    folder = PurePosixPath("deploy", target.name)
    files.update({folder / path: text for path, text in render("deploy-service", context).items()})
    header = _HEADER.format(name=target.name, source=target.settings)
    files[folder / "settings.tf"] = _settings_tf(plan, header)
    files[folder / "permissions.tf"] = _permissions_tf(plan, header)
    if plan.opa:
        files.update({OPA_FOLDER / path: text for path, text in render("deploy-opa", {}).items()})
        files.update({OPA_FOLDER / "bundle" / path: text for path, text in bundle.items()})
    return files


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
