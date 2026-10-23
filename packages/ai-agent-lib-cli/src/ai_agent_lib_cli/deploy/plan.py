"""What deploying a service takes, worked out from the settings it runs with.

The settings a service runs with in AWS live in ``deploy.env`` in its folder.
From them this module works out which adapters run, what each needs from IAM
(each adapter says so itself), which sidecars the task needs, which
distributions the image must install, and anything a person must check.

Nothing here calls AWS, and no secret is read or written: ``deploy.env`` may
not hold one.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from warnings import catch_warnings, simplefilter

from ai_agent_lib_cli.deploy.agentcore import (
    AgentCoreSettings,
    agentcore_preflight,
    read_agentcore_settings,
    strip_agentcore,
)
from ai_agent_lib_cli.deploy.target import AGENTCORE, AWS_DISTRIBUTION, Target
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.workspace import WorkspaceAnswers
from ai_agent_lib_core.config import (
    LEGACY_PREFIX,
    PREFIX,
    ConfigResolver,
    DotenvConfigSource,
    Key,
    secret_key,
    telemetry_enabled,
    variable_for,
)
from ai_agent_lib_core.contracts import (
    ConfigSource,
    ConfigurationError,
    DeploymentEnv,
    Section,
    ServiceConfig,
    TelemetryMode,
    describe,
)
from ai_agent_lib_core.di import AccessPlan, ServiceProviders, access_plan

__all__ = [
    "DeploymentPlan",
    "DistributionResolver",
    "ModuleOrigin",
    "depends_on_aws",
    "installed_extras",
    "installed_origin",
    "missing_dependencies",
    "plan_deployment",
    "plan_lines",
]

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_CORE_DISTRIBUTION = "ai-agent-lib-core"
_REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?")


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """What deploying one service takes.

    Attributes:
        target: The service, and the platform it goes to.
        environment: The container's environment: each of the service's
            variables in ``deploy.env`` and its value, in the file's order. An
            empty value is kept; the deployment's own ``AGENTCORE_`` variables
            are not the service's and are left out.
        access: What each selected adapter needs from AWS.
        opa: Whether the task needs the OPA sidecar (Fargate only).
        collector: Whether the task needs the telemetry collector sidecar
            (Fargate only; AgentCore Runtime instruments the process itself).
        problems: What stops the deployment. Nothing is written while any remain.
        warnings: What a person should know or check.
        deprecated: Each retired spelling that ``deploy.env`` uses, with the
            current spelling that replaces it. The task's environment still
            carries the name as written.
        agentcore: The deployment's own settings, when the platform is AgentCore.
    """

    target: Target
    environment: tuple[tuple[str, str], ...]
    access: AccessPlan
    opa: bool = False
    collector: bool = False
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())
    deprecated: tuple[tuple[str, str], ...] = field(default=())
    agentcore: AgentCoreSettings | None = None


# ------------------------------------------------------------------ the settings


def _read_settings(
    root: Path, target: Target
) -> tuple[ServiceConfig, tuple[tuple[str, str], ...], ConfigSource]:
    path = root.joinpath(*target.settings.parts)
    if not path.is_file():
        raise CliError(
            f"{target.settings} does not exist",
            expected=f"the settings {target.name} runs with in AWS, in {target.settings}",
            actual="no such file",
            fix=f"run 'agentcraft2 deploy {target.name}' to write a starting one, then fill it in",
        )
    source = DotenvConfigSource(path, required=True)
    try:
        # The plan reports retired spellings itself, so the resolver's warning is not raised too.
        with catch_warnings():
            simplefilter("ignore", DeprecationWarning)
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
    # An empty value is a setting too: it is kept, as the file has it.
    environment = [(n, value) for n in source.names() if (value := source.get(n)) is not None]
    return config, strip_agentcore(environment), source


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


# -------------------------------------------------------------- the dependencies


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _project(root: Path, target: Target) -> dict[str, Any]:
    """Return the ``[project]`` table of the service's pyproject.toml.

    Raises:
        CliError: If the file cannot be read or is not valid TOML. A silently
            empty table would turn into a wrong list of missing dependencies.
    """
    path = root.joinpath(*target.folder.parts, "pyproject.toml")
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CliError(
            f"{target.folder}/pyproject.toml could not be read ({describe(exc)})",
            expected="the service's pyproject.toml with a [project] table",
            fix="repair the file, or regenerate the service with 'agentcraft2 new'",
        ) from exc
    return project if isinstance(project, dict) else {}


@dataclass(frozen=True, slots=True)
class ModuleOrigin:
    """Where a process gets a top-level import name from.

    Attributes:
        location: The file the module was loaded from, when it is loaded.
        distributions: The distributions that ship the import name, by their
            names as the index knows them. Empty when none is installed.
    """

    location: Path | None = None
    distributions: frozenset[str] = frozenset()


DistributionResolver = Callable[[str], ModuleOrigin]
"""Given a top-level import name, says where this process gets it from.

The default, :func:`installed_origin`, asks the running interpreter. A test
passes its own, so that the answer does not depend on what the test process
happens to have imported or installed.
"""


def installed_origin(top: str) -> ModuleOrigin:
    """Return where this process gets the import name ``top`` from.

    An import name and a distribution name are different things, so the
    installed distributions' metadata answers; a loaded module's file says
    whether it is the service's own code.
    """
    location = getattr(sys.modules.get(top), "__file__", None)
    shipped_by = metadata.packages_distributions().get(top, [])
    return ModuleOrigin(
        location=Path(location) if location is not None else None,
        distributions=frozenset(shipped_by),
    )


def _owning_distribution(
    module: str, root: Path, target: Target, resolve: DistributionResolver
) -> str | None:
    """The distribution that ships ``module``, or ``None`` when it is the service's own code.

    The import name is the last resort: a module no installed distribution
    ships is taken to come from a distribution of the same name.
    """
    top = module.split(".", 1)[0]
    origin = resolve(top)
    service = root.joinpath(*target.folder.parts).resolve()
    if origin.location is not None and origin.location.resolve().is_relative_to(service):
        return None
    own = _normalized(str(_project(root, target).get("name", target.name)))
    shipped_by = {_normalized(name) for name in origin.distributions}
    if own in shipped_by or (not shipped_by and _normalized(top) == own):
        return None
    return min(shipped_by) if shipped_by else _normalized(top)


def installed_extras(root: Path, target: Target) -> dict[str, set[str]]:
    """The service's own dependencies: each distribution, with the extras it asks for."""
    project = _project(root, target)
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


def missing_dependencies(
    root: Path,
    target: Target,
    access: AccessPlan,
    registry: ServiceProviders,
    resolve: DistributionResolver = installed_origin,
) -> list[str]:
    """What the image would lack: a distribution, or an extra, a selected adapter needs.

    Args:
        root: The workspace.
        target: The service.
        access: The adapters its settings select.
        registry: Where the adapters are defined.
        resolve: Says which distribution ships an adapter's module.
    """
    have = installed_extras(root, target)
    needed: dict[str, dict[str, list[str]]] = {}
    for adapter in access.adapters:
        spec = registry.lookup(adapter.port, adapter.name)
        distribution = _owning_distribution(spec.factory.__module__, root, target, resolve)
        if distribution is None:
            continue  # the service's own adapter is in its own image
        extras = needed.setdefault(distribution, {})
        key = spec.extra or ""
        extras.setdefault(key, []).append(adapter.label)
    problems = []
    for distribution, extras in sorted(needed.items()):
        wanted = sorted(extra for extra in extras if extra)
        missing = [extra for extra in wanted if extra not in have.get(distribution, set())]
        if distribution in have and not missing:
            continue
        if distribution not in have and distribution == _CORE_DISTRIBUTION:
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
            "and run 'python -m ai_agent_lib_cli install'"
        )
    return problems


# --------------------------------------------------------------------- the plan


def plan_deployment(
    root: Path,
    answers: WorkspaceAnswers,
    target: Target,
    providers: ServiceProviders | None = None,
    *,
    resolve: DistributionResolver = installed_origin,
) -> DeploymentPlan:
    """Work out what deploying ``target`` takes, from its ``deploy.env``.

    Args:
        root: The workspace.
        answers: The workspace's answers.
        target: The service.
        providers: Where the adapters are defined. Default: the installed ones.
        resolve: Says which distribution ships an adapter's module, so that
            the image's dependencies can be checked.

    Raises:
        CliError: If ``deploy.env`` is missing, holds secrets or describes a
            developer's machine.
        ConfigurationError: If a setting is not valid, or selects an adapter
            that is not installed.
    """
    config, environment, source = _read_settings(root, target)
    registry = providers if providers is not None else ServiceProviders.default()
    access = access_plan(config, registry)
    agentcore = (
        read_agentcore_settings(source, str(target.settings))
        if target.platform == AGENTCORE
        else None
    )
    problems = [
        f"{adapter.label} runs only on a developer's machine; select another in {target.settings}"
        for adapter in access.local_only
    ]
    warnings = [
        f"{adapter.label} does not say what it needs from AWS, so permissions.tf has nothing "
        "for it; add what it needs by hand"
        for adapter in access.undeclared
    ]
    problems += missing_dependencies(root, target, access, registry, resolve)
    if agentcore is not None:
        # The runtime instruments the process itself (ADOT): no sidecar, and the
        # image, not the service, brings the distribution that owns the providers.
        found, noted = agentcore_preflight(
            target, config, agentcore, _project(root, target).get("requires-python")
        )
        problems += found
        warnings += noted
        collector = False
    else:
        collector = telemetry_enabled(config)
    core = installed_extras(root, target).get(_CORE_DISTRIBUTION, set())
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
        deprecated=tuple(
            (name, PREFIX + name.removeprefix(LEGACY_PREFIX))
            for name, _ in environment
            if name.startswith(LEGACY_PREFIX)
        ),
        environment=environment,
        access=access,
        opa=agentcore is None and _uses_opa_beside_it(config),
        collector=collector,
        problems=tuple(problems),
        warnings=tuple(warnings),
        agentcore=agentcore,
    )


# --------------------------------------------------------------- what is shown


def _agentcore_lines(plan: DeploymentPlan, settings: AgentCoreSettings) -> list[str]:
    target = plan.target
    inbound = (
        f"jwt (tokens from {settings.discovery_url})" if settings.jwt else "iam (signed requests)"
    )
    network = settings.network
    if settings.network == "VPC":
        network += f" ({', '.join(settings.subnets)}; {', '.join(settings.security_groups)})"
    memory = {"none": "none", "create": "created by Terraform"}.get(
        settings.memory, f"existing {settings.memory}"
    )
    gateway = {"none": "none", "create": "created by Terraform, one target per MCP server"}.get(
        settings.gateway, f"existing {settings.gateway}"
    )
    telemetry = next(
        (value for name, value in plan.environment if name == variable_for(Key.TELEMETRY)),
        TelemetryMode.OFF.value,
    )
    session = f"idle {settings.idle_timeout_seconds}s, at most {settings.max_lifetime_seconds}s"
    return [
        f"Deploying {target.name} to AgentCore Runtime with the settings in {target.settings}.",
        "",
        "Runtime:",
        *aligned_lines(
            [
                ("protocol", f"{target.protocol} on port {target.port}"),
                ("inbound auth", inbound),
                ("network", network),
                ("artifact", settings.artifact),
                ("session", session),
                ("memory", memory),
                ("gateway", gateway),
                ("telemetry", f"{telemetry} (ADOT, installed by the image, owns the providers)"),
            ]
        ),
        "",
    ]


def aligned_lines(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """Return ``name: value`` lines with the values in one column."""
    width = max((len(name) for name, _ in pairs), default=0)
    return [f"  {name.ljust(width)}  {value}" for name, value in pairs]


def plan_lines(plan: DeploymentPlan) -> list[str]:
    """Return the plan as a person reads it."""
    target = plan.target
    if plan.agentcore is not None:
        lines = _agentcore_lines(plan, plan.agentcore)
    else:
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
    what = "Container:" if plan.agentcore is not None else "Containers in the task:"
    lines += ["", what, f"  service: {' '.join(target.command)}"]
    if plan.opa:
        lines.append("  opa: decides policy on the task's loopback address (deploy/opa/Dockerfile)")
    if plan.collector:
        lines.append("  collector: sends traces to X-Ray and metrics to CloudWatch")
    lines += ["", f"Environment: {len(plan.environment)} variables from {target.settings}."]
    lines += [f"  {name}" for name, _ in plan.environment]
    if plan.deprecated:
        lines += [
            "",
            f"Deprecated spellings in {target.settings} (the task gets each name as written, "
            "and the service accepts it for now):",
            *(f"  {old} -> {new}" for old, new in plan.deprecated),
            "Rename them now: each old spelling stops working after one minor release.",
        ]
    if plan.warnings:
        lines += ["", "Warnings:", *(f"  - {warning}" for warning in plan.warnings)]
    if plan.problems:
        lines += ["", "Problems (nothing is written until they are fixed):"]
        lines += [f"  - {problem}" for problem in plan.problems]
    return lines
