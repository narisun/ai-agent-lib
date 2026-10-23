"""The AgentCore Runtime deployment's own settings, and what the platform refuses.

``deploy.env`` holds two kinds of variable. The service's own (the library's
prefix) become the container's environment. The ``AGENTCORE_`` block, read
here, describes the deployment itself: how callers are authenticated, the
network, the artifact, the session lifecycle, and whether a Memory or a
Gateway is created beside the runtime. Nothing of it reaches the container.

Nothing here calls AWS.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from ai_agent_lib_cli.deploy.target import Target
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_core.config import (
    Key,
    options_key,
    provider_key,
    telemetry_enabled,
    variable_for,
)
from ai_agent_lib_core.contracts import ConfigSource, Section, ServiceConfig, TelemetryMode

__all__ = [
    "AGENTCORE_PREFIX",
    "AGENTCORE_PYTHON",
    "AgentCoreSettings",
    "agentcore_preflight",
    "is_agentcore_variable",
    "read_agentcore_settings",
    "strip_agentcore",
]

AGENTCORE_PREFIX = "AGENTCORE_"
"""Every variable of ``deploy.env`` with this prefix is the deployment's, not the service's."""

AGENTCORE_PYTHON = "3.13"
"""The Python the runtime offers to a code artifact, and the version the image is built with."""

AUTH_MODES = ("iam", "jwt")
NETWORK_MODES = ("PUBLIC", "VPC")
ARTIFACTS = ("container", "code")
NONE = "none"
CREATE = "create"
DISCOVERY_SUFFIX = ".well-known/openid-configuration"
_SESSION_SECONDS = (60, 28800)  # the runtime's bounds for both lifecycle timeouts

# The names, once, so that a message and the reader agree.
V_AUTH = AGENTCORE_PREFIX + "AUTH"
V_DISCOVERY_URL = AGENTCORE_PREFIX + "DISCOVERY_URL"
V_ALLOWED_CLIENTS = AGENTCORE_PREFIX + "ALLOWED_CLIENTS"
V_ALLOWED_AUDIENCE = AGENTCORE_PREFIX + "ALLOWED_AUDIENCE"
V_ALLOWED_SCOPES = AGENTCORE_PREFIX + "ALLOWED_SCOPES"
V_NETWORK = AGENTCORE_PREFIX + "NETWORK"
V_SUBNETS = AGENTCORE_PREFIX + "SUBNETS"
V_SECURITY_GROUPS = AGENTCORE_PREFIX + "SECURITY_GROUPS"
V_IDLE_TIMEOUT = AGENTCORE_PREFIX + "IDLE_TIMEOUT_SECONDS"
V_MAX_LIFETIME = AGENTCORE_PREFIX + "MAX_LIFETIME_SECONDS"
V_ARTIFACT = AGENTCORE_PREFIX + "ARTIFACT"
V_GATEWAY = AGENTCORE_PREFIX + "GATEWAY"
V_MEMORY = AGENTCORE_PREFIX + "MEMORY"

_GATEWAY_ARN = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:gateway/[A-Za-z0-9_-]+$"
)
_MEMORY_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,110}$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_CHECKPOINTS_OFFERED = ("postgres", "agentcore_memory", "none")
_MEMORY_CHECKPOINT = "agentcore_memory"
_OPTION_MEMORY_ID = "memory_id"


@dataclass(frozen=True, slots=True)
class AgentCoreSettings:
    """How a service is deployed to AgentCore Runtime: the ``AGENTCORE_`` block of ``deploy.env``.

    Attributes:
        auth: Who may invoke the runtime: ``iam`` (SigV4-signed callers) or
            ``jwt`` (bearer tokens from the discovery URL's issuer).
        discovery_url: The issuer's OpenID discovery document, for ``jwt``.
        allowed_clients: Client IDs a token may carry, for ``jwt``.
        allowed_audience: Audiences a token may carry, for ``jwt``.
        allowed_scopes: Scopes a token must carry, for ``jwt``.
        network: ``PUBLIC`` or ``VPC``.
        subnets: The subnets of a ``VPC`` deployment.
        security_groups: The security groups of a ``VPC`` deployment.
        idle_timeout_seconds: How long a session may be idle before it ends.
        max_lifetime_seconds: How long a session may live at most.
        artifact: ``container`` (an image in ECR) or ``code`` (a zip in S3).
        gateway: ``none``, ``create`` or the ARN of a gateway that fronts the runtime.
        memory: ``none``, ``create`` or the ID of the Memory the checkpoints go to.
    """

    auth: str = "iam"
    discovery_url: str | None = None
    allowed_clients: tuple[str, ...] = ()
    allowed_audience: tuple[str, ...] = ()
    allowed_scopes: tuple[str, ...] = ()
    network: str = "PUBLIC"
    subnets: tuple[str, ...] = ()
    security_groups: tuple[str, ...] = ()
    idle_timeout_seconds: int = 900
    max_lifetime_seconds: int = 28800
    artifact: str = "container"
    gateway: str = NONE
    memory: str = NONE

    @property
    def jwt(self) -> bool:
        """Whether callers present bearer tokens rather than signing requests."""
        return self.auth == "jwt"

    @property
    def create_memory(self) -> bool:
        """Whether Terraform creates the Memory."""
        return self.memory == CREATE

    @property
    def memory_id(self) -> str | None:
        """The ID of an existing Memory, when one is named."""
        return None if self.memory in (NONE, CREATE) else self.memory

    @property
    def create_gateway(self) -> bool:
        """Whether Terraform creates the Gateway."""
        return self.gateway == CREATE

    @property
    def gateway_arn(self) -> str | None:
        """The ARN of an existing Gateway, when one is named."""
        return None if self.gateway in (NONE, CREATE) else self.gateway


def is_agentcore_variable(name: str) -> bool:
    """Whether ``name`` is one of the deployment's own variables, kept out of the container."""
    return name.startswith(AGENTCORE_PREFIX)


def strip_agentcore(environment: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Return ``environment`` without the deployment's own variables."""
    return tuple((name, value) for name, value in environment if not is_agentcore_variable(name))


# ------------------------------------------------------------------ reading


def _bad(name: str, where: str, expected: str, actual: str) -> CliError:
    return CliError(
        f"{name} in {where} is not valid",
        expected=expected,
        actual=actual,
        fix=f"change {name} in {where}",
    )


def _choice(
    source: ConfigSource, name: str, where: str, choices: Sequence[str], default: str
) -> str:
    value = (source.get(name) or "").strip() or default
    if value not in choices:
        raise _bad(name, where, " or ".join(choices), repr(value))
    return value


def _names(source: ConfigSource, name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in (source.get(name) or "").split(",") if item.strip())


def _seconds(source: ConfigSource, name: str, where: str, default: int) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    low, high = _SESSION_SECONDS
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise _bad(name, where, f"a whole number of seconds from {low} to {high}", repr(raw))
    return int(raw)


def _one_of(
    source: ConfigSource, name: str, where: str, pattern: re.Pattern[str], what: str
) -> str:
    value = (source.get(name) or "").strip() or NONE
    if value in (NONE, CREATE) or pattern.match(value):
        return value
    raise _bad(name, where, f"{NONE}, {CREATE} or {what}", repr(value))


def _jwt_settings(
    source: ConfigSource, where: str, auth: str
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    discovery_url = (source.get(V_DISCOVERY_URL) or "").strip() or None
    clients = _names(source, V_ALLOWED_CLIENTS)
    audience = _names(source, V_ALLOWED_AUDIENCE)
    if auth != "jwt":
        given = [
            name
            for name, value in (
                (V_DISCOVERY_URL, discovery_url),
                (V_ALLOWED_CLIENTS, clients),
                (V_ALLOWED_AUDIENCE, audience),
            )
            if value
        ]
        if given:
            raise _bad(
                V_AUTH, where, f"{V_AUTH}=jwt when {', '.join(given)} are set", f"{V_AUTH}={auth}"
            )
        return None, (), ()
    if discovery_url is None:
        raise _bad(
            V_DISCOVERY_URL,
            where,
            f"the issuer's discovery URL, ending in {DISCOVERY_SUFFIX}",
            "not set",
        )
    split = urlsplit(discovery_url)
    if (
        split.scheme != "https"
        or not split.hostname
        or not discovery_url.endswith(DISCOVERY_SUFFIX)
    ):
        raise _bad(
            V_DISCOVERY_URL,
            where,
            f"an https URL ending in {DISCOVERY_SUFFIX}",
            repr(discovery_url),
        )
    if not clients and not audience:
        raise _bad(
            V_ALLOWED_CLIENTS,
            where,
            f"at least one client ID in {V_ALLOWED_CLIENTS} or one audience in "
            f"{V_ALLOWED_AUDIENCE}",
            "neither is set",
        )
    return discovery_url, clients, audience


def _network_settings(
    source: ConfigSource, where: str
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    network = _choice(source, V_NETWORK, where, NETWORK_MODES, "PUBLIC")
    subnets = _names(source, V_SUBNETS)
    groups = _names(source, V_SECURITY_GROUPS)
    if network == "VPC":
        for name, values in ((V_SUBNETS, subnets), (V_SECURITY_GROUPS, groups)):
            if not values:
                raise _bad(name, where, f"a comma-separated list, since {V_NETWORK}=VPC", "not set")
    elif subnets or groups:
        raise _bad(
            V_NETWORK,
            where,
            f"{V_NETWORK}=VPC when {V_SUBNETS} or {V_SECURITY_GROUPS} are set",
            "PUBLIC",
        )
    return network, subnets, groups


def read_agentcore_settings(source: ConfigSource, where: str) -> AgentCoreSettings:
    """Return the ``AGENTCORE_`` block of ``source``, each value checked.

    Args:
        source: The settings file.
        where: How the file is named in a message.

    Raises:
        CliError: If a value is not valid; the message names the variable.
    """
    auth = _choice(source, V_AUTH, where, AUTH_MODES, "iam")
    discovery_url, clients, audience = _jwt_settings(source, where, auth)
    network, subnets, groups = _network_settings(source, where)
    idle = _seconds(source, V_IDLE_TIMEOUT, where, 900)
    lifetime = _seconds(source, V_MAX_LIFETIME, where, 28800)
    if idle > lifetime:
        raise _bad(V_IDLE_TIMEOUT, where, f"no more than {V_MAX_LIFETIME} ({lifetime})", str(idle))
    return AgentCoreSettings(
        auth=auth,
        discovery_url=discovery_url,
        allowed_clients=clients,
        allowed_audience=audience,
        allowed_scopes=_names(source, V_ALLOWED_SCOPES) if auth == "jwt" else (),
        network=network,
        subnets=subnets,
        security_groups=groups,
        idle_timeout_seconds=idle,
        max_lifetime_seconds=lifetime,
        artifact=_choice(source, V_ARTIFACT, where, ARTIFACTS, "container"),
        gateway=_one_of(source, V_GATEWAY, where, _GATEWAY_ARN, "the ARN of a gateway"),
        memory=_one_of(source, V_MEMORY, where, _MEMORY_ID, "the ID of a memory"),
    )


# ---------------------------------------------------------------- preflight


_CLAUSE = re.compile(r"^(?P<op>~=|==|!=|<=|>=|<|>)\s*(?P<version>\d+(?:\.\d+)*)(?P<rest>\.\*)?$")


def _python_excluded(specifier: str, version: str) -> bool:
    """Whether ``requires-python`` leaves ``version`` out.

    The clauses are read here rather than with ``packaging``, which the CLI
    does not depend on. A clause the reader does not understand excludes
    nothing, so an unusual specifier is never mistaken for a refusal.
    """
    wanted = tuple(int(part) for part in version.split("."))
    for clause in specifier.split(","):
        match = _CLAUSE.match(clause.strip())
        if match is None:
            continue
        bound = tuple(int(part) for part in match["version"].split("."))
        at = wanted[: len(bound)]
        operator = match["op"]
        excluded = (
            (operator == ">=" and at < bound)
            or (operator == ">" and at <= bound)
            or (operator == "<=" and at > bound)
            or (operator == "<" and at >= bound)
            or (operator == "==" and at != bound)
            or (operator == "!=" and at == bound)
            or (operator == "~=" and (at < bound or at[: len(bound) - 1] != bound[:-1]))
        )
        if excluded:
            return True
    return False


def _telemetry_problems(config: ServiceConfig, where: str) -> list[str]:
    variable = variable_for(Key.TELEMETRY)
    if config.telemetry is TelemetryMode.OPENTELEMETRY:
        return [
            f"{variable}=opentelemetry installs the service's own tracer and meter providers, "
            "which collide with the ones ADOT installs on AgentCore Runtime; set "
            f"{variable}=ambient (or off) in {where}"
        ]
    if config.telemetry is TelemetryMode.OFF and telemetry_enabled(config):
        return [
            f"the audit option tracing turns telemetry on while {variable} is off, so the "
            "service would install its own providers beside ADOT's; set "
            f"{variable}=ambient in {where}"
        ]
    return []


def _policy_problems(config: ServiceConfig, where: str) -> list[str]:
    selection = config.sections.get(Section.POLICY)
    if selection is None or selection.provider != "opa":
        return []
    url = selection.options.get("url", "http://localhost:8181")
    if not isinstance(url, str) or (urlsplit(url).hostname or "") not in _LOOPBACK:
        return []
    return [
        "policy opa decides on the loopback address, where its sidecar would run; nothing runs "
        "beside a container on AgentCore Runtime. Point url in "
        f"{variable_for(options_key(Section.POLICY))} at an OPA the runtime can reach, or set "
        f"{variable_for(provider_key(Section.POLICY))}=rules in {where}"
    ]


def _checkpoint_findings(
    config: ServiceConfig, settings: AgentCoreSettings, where: str
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    selection = config.sections.get(Section.CHECKPOINT)
    provider = selection.provider if selection is not None else NONE
    provider_variable = variable_for(provider_key(Section.CHECKPOINT))
    if provider not in _CHECKPOINTS_OFFERED:
        problems.append(
            f"checkpoint {provider} has nowhere durable to write on AgentCore Runtime, whose "
            f"disk ends with the session; set {provider_variable} to one of "
            f"{', '.join(_CHECKPOINTS_OFFERED)} in {where}"
        )
    configured = selection.options.get(_OPTION_MEMORY_ID) if selection is not None else None
    if provider == _MEMORY_CHECKPOINT:
        if settings.memory_id is not None and configured != settings.memory_id:
            problems.append(
                f"{V_MEMORY} names the memory {settings.memory_id}, but the checkpoint option "
                f"{_OPTION_MEMORY_ID} is {configured!r}; make them agree in {where} "
                f"({V_MEMORY} and {variable_for(options_key(Section.CHECKPOINT))})"
            )
        elif settings.create_memory:
            warnings.append(
                f"{V_MEMORY}=create: Terraform creates the memory, puts its ID into the "
                f"checkpoint option {_OPTION_MEMORY_ID} of the runtime's environment and grants "
                f"the role access to it; {_OPTION_MEMORY_ID} in {where} ({configured!r}) is a "
                "placeholder, and the statement in permissions.tf names that placeholder"
            )
    elif settings.memory != NONE:
        warnings.append(
            f"{V_MEMORY}={settings.memory} while checkpoint {provider} does not use AgentCore "
            f"Memory; set {provider_variable}={_MEMORY_CHECKPOINT} in {where} to keep "
            f"conversations there, or {V_MEMORY}=none"
        )
    return problems, warnings


def agentcore_preflight(
    target: Target,
    config: ServiceConfig,
    settings: AgentCoreSettings,
    requires_python: str | None,
) -> tuple[list[str], list[str]]:
    """Return what AgentCore Runtime refuses in these settings, and what to know.

    The platform runs one session in one microVM, with no sidecar and a disk
    that ends with the session, under ADOT's instrumentation, on ARM64 and
    Python 3.13 for a code artifact. Each refusal names the variable to change.
    The ``local_only`` adapters are the plan's own problem already.

    Args:
        target: The service.
        config: Its settings, resolved.
        settings: The ``AGENTCORE_`` block.
        requires_python: The service's ``requires-python``, when it has one.

    Returns:
        The problems, then the warnings.
    """
    where = str(target.settings)
    problems = _policy_problems(config, where) + _telemetry_problems(config, where)
    # The SQLite outcome journal needs no check: the generated service opens it
    # only when the deployment environment is local, which the plan refuses.
    checkpoint_problems, warnings = _checkpoint_findings(config, settings, where)
    problems += checkpoint_problems
    if requires_python and _python_excluded(requires_python, AGENTCORE_PYTHON):
        problems.append(
            f"the runtime offers Python {AGENTCORE_PYTHON} and the image is built with it, but "
            f'requires-python = "{requires_python}" in {target.folder}/pyproject.toml excludes '
            "it; widen the specifier"
        )
    if settings.artifact == "code":
        warnings.append(
            f"{V_ARTIFACT}=code: every dependency needs a wheel for Python {AGENTCORE_PYTHON} on "
            "aarch64 manylinux2014. build_zip.py enforces that (pip --only-binary=:all: fails "
            "on a dependency without one); the plan does not check it"
        )
    return problems, warnings
