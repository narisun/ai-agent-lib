"""Render the developer guide from its prose and from the code.

The prose lives in ``guide.src.html`` beside this script. Every table that
lists something the code defines (variables, defaults, adapters, adapter
options, command-line flags) is generated here from the code itself, so the
guide cannot drift from the library. Companion Anthropic walkthroughs, samples,
and the trace reader come from the CLI templates used by new workspaces.
Tests fail when these committed documents differ from their source.

Usage::

    python docs/guide/build_guide.py            # write the guide and companion files
    python docs/guide/build_guide.py --check    # exit 1 if any generated file is stale
    python docs/guide/build_guide.py --fragment out.html  # body only, for hosting
"""

from __future__ import annotations

import argparse
import html
import importlib
import inspect
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath

import click
from configuration_examples import configuration_examples

from agentcraft2_cli.app import cli
from agentcraft2_cli.deploy import Target, starter_settings
from agentcraft2_cli.deploy import agentcore as agentcore_target
from agentcraft2_cli.envfiles import variable_names
from agentcraft2_cli.render import TemplateRenderer
from agentcraft2_cli.workspace import LibrarySource
from agentcraft2_core.config import ConfigResolver, Key, MappingConfigSource, variable_for
from agentcraft2_core.config.bindings import (
    DEFAULT_BINDINGS,
    SECRET_VARIABLE_PREFIX,
    Binding,
    ValueKind,
    legacy_spelling,
    options_key,
    provider_key,
)
from agentcraft2_core.config.phoenix import PHOENIX_VARIABLES
from agentcraft2_core.config.profiles import PROFILE_DEFAULTS
from agentcraft2_core.config.reference import default_text
from agentcraft2_core.contracts import NoOptions, OptionsModel, Profile, Section
from agentcraft2_core.contracts.options_help import options_help, options_summary
from agentcraft2_core.di import DATA_PORT, MODEL_PORT, ServiceProviders, access_plan

__all__ = [
    "API_MODULES",
    "NO_OPTIONS_NOTES",
    "OTEL_VARIABLES",
    "PORTS",
    "configuration_variables",
    "guide_html",
    "main",
    "render",
    "section_of",
    "walkthrough_files",
]

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "guide.src.html"
TARGET = HERE.parent / "developer-guide.html"
_MARKER = re.compile(r"<!-- generated: (?P<name>[a-z-]+) -->")

PORTS: tuple[str, ...] = (MODEL_PORT, *(section.value for section in Section), DATA_PORT)
"""The ports, in the order the guide lists them."""

NO_OPTIONS_NOTES: Mapping[tuple[str, str], str] = {
    (MODEL_PORT, "bedrock"): (
        "No options. Uses the AWS session settings (<code>AWS_PROFILE</code>, "
        "<code>AWS_REGION</code>) and <code>AC2_TLS_CA_BUNDLE</code>."
    ),
    (MODEL_PORT, "fake"): (
        "No options. Echoes the question as <code>fake: &lt;question&gt;</code>; "
        "tests script its replies."
    ),
    (Section.SECRETS.value, "env"): (
        f"No options. Serves each secret from a <code>{SECRET_VARIABLE_PREFIX}&lt;NAME&gt;</code> "
        "variable."
    ),
    (Section.CHECKPOINT.value, "none"): (
        "No options. Keeps no graph state: for MCP servers and agents that compile no graph."
    ),
}

OTEL_VARIABLES: tuple[tuple[str, str, str], ...] = (
    (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://localhost:4318",
        "Base address of the collector. Traces go to /v1/traces and metrics to /v1/metrics.",
    ),
    (
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "not set",
        "Full traces address, when traces go somewhere else.",
    ),
    (
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "not set",
        "Full metrics address, when metrics go somewhere else.",
    ),
    (
        "OTEL_EXPORTER_OTLP_HEADERS",
        "not set",
        "Headers for every export, such as a vendor key. Keep it in the task's secrets.",
    ),
    (
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "not set",
        "CA file for a collector behind enterprise TLS.",
    ),
    (
        "OTEL_RESOURCE_ATTRIBUTES",
        "not set",
        "Extra resource attributes, e.g. deployment.environment=prod,service.version=1.4.0.",
    ),
    (
        "OTEL_METRIC_EXPORT_INTERVAL",
        "60000",
        "Milliseconds between metric exports.",
    ),
    (
        "OTEL_BSP_SCHEDULE_DELAY",
        "5000",
        "Milliseconds the batch span processor waits before sending.",
    ),
    (
        "OTEL_TRACES_SAMPLER",
        "parentbased_always_on",
        "Sampling. Use parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG for high volume.",
    ),
    (
        "OTEL_TRACES_SAMPLER_ARG",
        "not set",
        "Sampling ratio from 0 to 1 when using a ratio sampler, e.g. 0.1 for 10% of root traces.",
    ),
    ("OTEL_SDK_DISABLED", "false", "Turns the SDK off without a code change."),
)
"""Variables the OpenTelemetry SDK reads. The library never reads them itself."""


# ------------------------------------------------------------------- helpers


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _doc(text: str) -> str:
    """Escape docstring text and show its ``literal`` spans as code."""
    return re.sub(r"``(.+?)``", r"<code>\1</code>", _e(text))


def _code(text: object) -> str:
    return f"<code>{_e(text)}</code>"


def _table(headers: Iterable[str], rows: Iterable[Iterable[str]], css: str = "") -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "\n".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    cls = f' class="{css}"' if css else ""
    return (
        f'<div class="table-wrap"><table{cls}>\n<thead><tr>{head}</tr></thead>\n'
        f"<tbody>\n{body}\n</tbody></table></div>"
    )


def _anchor(port: str, name: str) -> str:
    return f"opt-{port}-{name}".replace("_", "-")


# ----------------------------------------------------------------- variables


def _markdown(text: str) -> str:
    """Escape text and show its `code` spans as code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", _e(text))


def _default_of(binding: Binding) -> str:
    text = default_text(binding)
    return '<span class="muted">not set</span>' if text == "not set" else _markdown(text)


def _notes(binding: Binding) -> str:
    notes = []
    if binding.kind is ValueKind.JSON:
        notes.append('<span class="tag">JSON</span>')
    if binding.sensitive:
        notes.append('<span class="tag warn">sensitive</span>')
    if binding.alternate_names:
        notes.append("also " + ", ".join(_code(n) for n in binding.alternate_names))
    # The legacy spelling of every owned variable is stated once, in the guide's prose;
    # a row names only a variable it replaces by another route.
    legacy = legacy_spelling(binding.name) if binding.owned else None
    replaced = [n for n in binding.deprecated_names if n != legacy]
    if replaced:
        notes.append("replaces " + ", ".join(_code(n) for n in replaced))
    return " ".join(notes)


def section_of(binding: Binding) -> Section | None:
    """Return the section a per-section binding belongs to, or ``None``."""
    for section in Section:
        if binding.key in (provider_key(section), options_key(section)):
            return section
    return None


def configuration_variables() -> str:
    """One table for library configuration, telemetry and the guide's desktop settings."""
    examples = configuration_examples()
    rows = []
    for b in DEFAULT_BINDINGS:
        example = examples[b.key]
        notes = _markdown(example.notes)
        section = section_of(b)
        if section is not None:
            names = ", ".join(
                f'<a href="#{_anchor(section.value, n)}">{_code(n)}</a>'
                for n in _registry().names(section.value)
            )
            notes += f'<div class="more">Adapters and options: {names}.</div>'
        rows.append(
            [
                f'<span class="var" id="var-{b.name.lower()}">{_e(b.name)}</span>',
                f'{_e(b.description)}<div class="more">Default: {_default_of(b)}. '
                f"{_notes(b)}</div>",
                _markdown(example.local),
                _markdown(example.aws),
                notes,
            ]
        )
    rows.append(
        [
            f'<span class="var">{_e(SECRET_VARIABLE_PREFIX)}&lt;NAME&gt;</span>',
            'A named secret for the env secrets provider. <span class="tag warn">sensitive</span>',
            f"Supply secrets as needed, e.g. {_code(SECRET_VARIABLE_PREFIX + 'RATES_TOKEN')}.",
            "Use Secrets Manager with the configured prefix.",
            f"The example supplies {_code('rates_token')}. Options such as {_code('auth_secret')} "
            "name the secret, never its value. Keep values out of source control and images.",
        ]
    )
    rows.extend(_otel_variable_rows())
    rows.extend(_phoenix_variable_rows())
    rows.extend(_desktop_variable_rows())
    rows.extend(_rmprep_variable_rows())
    return _table(("Variable name", "Purpose", "Local", "AWS", "Notes"), rows, "vars environments")


def _phoenix_variable_rows() -> list[list[str]]:
    settings = (
        (
            "Enable Phoenix tracing; start-local.bat also starts the local server.",
            "false; true to trace locally",
            "false until a Phoenix server is available",
        ),
        (
            "Capture model and tool input/output.",
            "false; true for approved test data",
            "false unless approved",
        ),
        (
            "Optional private agent tracing settings file.",
            "Unset; use the root .env or an absolute file path",
            "Unset; inject task environment settings",
        ),
        (
            "Phoenix HTTP origin, without /v1/traces.",
            "http://127.0.0.1:6006",
            "Team-managed authenticated HTTPS origin",
        ),
        (
            "Project for this agent's traces.",
            "agent-lab; rmprep-trial in the walkthrough",
            "Team project",
        ),
        (
            "Optional API key; never included in trace links.",
            "Unset for the local server",
            "Process environment (on Fargate, an ECS task-definition secret); "
            "not the secrets provider",
        ),
    )
    return [
        [
            f'<span class="var" id="var-{name.lower()}">{_e(name)}</span>',
            _e(purpose),
            _e(local),
            _e(aws),
            "Process settings override the separate tracing file, then the service .env. "
            "Nothing is exported until tracing is enabled. A local Phoenix stores traces "
            "in SQLite and runs in its own Python environment. A team-hosted Phoenix is "
            "supported over HTTPS; your platform team runs it. "
            '<a href="phoenix-trial.md">Trace agents with Phoenix</a>, including the '
            '<a href="phoenix-trial.md#send-traces-to-a-team-hosted-phoenix">team-hosted '
            "setup</a>.",
        ]
        for name, (purpose, local, aws) in zip(PHOENIX_VARIABLES, settings, strict=True)
    ]


def _rmprep_variable_rows() -> list[list[str]]:
    settings = (
        ("INPUT", "128000", "Total estimated model input, including tool definitions."),
        (
            "RECENT",
            "96000",
            "Recent complete exchanges for the selected clients or memos, within the input limit.",
        ),
        ("QUESTION", "16000", "Maximum estimated size of the current question."),
        ("TOOL_SCHEMA", "4000", "Input space reserved for the bound tool definitions."),
    )
    return [
        [
            _code(f"RMPREP_CONTEXT_{name}_TOKENS"),
            _e(purpose),
            f"rmprep default: {_code(default)}. Set in its .env.",
            f"Set on the rmprep Fargate task; same default {_code(default)}.",
            "Specific to rmprep. Restart after changes. Process variables override .env. "
            "Keep input below the deployed model window with room for output; question and recent "
            'limits must each fit after the tool allowance. See <a href="#rmprep-context-budget">'
            "context budgets</a>.",
        ]
        for name, default, purpose in settings
    ]


def _otel_variable_rows() -> list[list[str]]:
    rows = []
    for name, default, purpose in OTEL_VARIABLES:
        aws = f"Use the SDK default: {_code(default)}."
        if name == "OTEL_EXPORTER_OTLP_ENDPOINT":
            aws = "<code>http://localhost:4318</code> for the collector sidecar."
        elif name == "OTEL_RESOURCE_ATTRIBUTES":
            aws = "Example: <code>deployment.environment.name=prod,service.version=1.0.0</code>."
        elif name == "OTEL_TRACES_SAMPLER":
            aws = "Default, or <code>parentbased_traceidratio</code> with the ratio below."
        elif name == "OTEL_TRACES_SAMPLER_ARG":
            aws = "Optional ratio, e.g. <code>0.1</code>; agree the coverage with the team."
        notes = "Set in the process environment; the SDK does not read the service's .env file."
        if name == "OTEL_RESOURCE_ATTRIBUTES":
            notes += " The library sets service.name from the service entry point."
        elif name == "OTEL_EXPORTER_OTLP_ENDPOINT":
            notes += " The library uses OTLP over HTTP, not gRPC. S3 is not an OTLP endpoint."
        elif name == "OTEL_SDK_DISABLED":
            notes += " Disables telemetry only; audit and model diagnostic files are separate."
        rows.append(
            [
                f'<span class="var" id="var-{name.lower()}">{_e(name)}</span>',
                f"OpenTelemetry SDK: {_e(purpose)}",
                f"Unset for the tutorial. SDK default: {_code(default)}.",
                aws,
                notes,
            ]
        )
    return rows


def _desktop_variable_rows() -> list[list[str]]:
    settings = (
        (
            "LIBRARY",
            "Command Prompt shortcut for the library checkout.",
            r"Example: `F:\work\ai-agent-lib`",
            "Not used by the service.",
            "Defined by the setup commands; it is not an ai-agent-lib setting.",
        ),
        (
            "PYTHON",
            "Command Prompt shortcut for the virtual environment's Python.",
            r"`%LIBRARY%\.venv\Scripts\python.exe`",
            "Not used by the service.",
            'Run `"%PYTHON%" -m pip` to install into this environment. '
            "This is a guide shortcut, not Python's PYTHONPATH setting.",
        ),
        (
            "PIP_CONFIG_FILE",
            "Pip: selects the team's approved configuration file.",
            r"Team-provided path, e.g. `C:\company\python\pip.ini`",
            "Build environment only, when the build team supplies it.",
            "The enterprise setup may already configure pip; keep it unless the team tells "
            "you to select another file. Pip variables belong in the process environment.",
        ),
        (
            "PIP_INDEX_URL",
            "Pip: package repository URL.",
            "Normally supplied by enterprise setup or pip.ini.",
            "Approved Artifactory endpoint in the image build environment.",
            "Use the team's repository and credential mechanism. Package installation "
            "happens during development/build, not service startup. "
            "Keep credentials out of images.",
        ),
        (
            "PIP_CERT",
            "Pip: trusted certificate bundle for the package repository.",
            "Team-provided CA file, if required.",
            "Build environment CA file, if required.",
            "Keep certificate verification enabled. This does not configure runtime AWS, "
            "HTTP or PostgreSQL clients.",
        ),
    )
    return [
        [f'<span class="var">{_e(name)}</span>', *(_markdown(c) for c in cells)]
        for name, *cells in settings
    ]


# ------------------------------------------------------------------ adapters


def _registry() -> ServiceProviders:
    return ServiceProviders.default()


def _package_of(factory: Callable[..., object]) -> str:
    module = getattr(factory, "__module__", "") or ""
    return "agentcraft2-aws" if module.startswith("agentcraft2_aws") else "agentcraft2-core"


def profiles() -> str:
    """The adapters each profile selects."""
    local, aws = PROFILE_DEFAULTS[Profile.LOCAL], PROFILE_DEFAULTS[Profile.AWS]
    rows = [["model", _code(local.model_provider), _code(aws.model_provider)]]
    rows += [
        [section.value, _code(local.sections[section]), _code(aws.sections[section])]
        for section in Section
    ]
    return _table(("Port", "local profile", "aws profile"), rows, "compact")


def introductory_profiles() -> str:
    """Explain each concern while deriving its provider defaults from configuration."""
    labels = {
        Section.IDENTITY: ("Caller identity", "IdentityVerifier"),
        Section.POLICY: ("Permission decisions", "PolicyDecisionPoint"),
        Section.AUDIT: ("Audit evidence", "AuditSink"),
        Section.SECRETS: ("Secrets", "SecretsProvider"),
        Section.CHECKPOINT: ("Conversation state", "CheckpointBackend"),
        Section.REGISTRY: ("Agent/tool registry", "RegistrySource"),
        Section.GUARDRAILS: ("Content checks", "GuardrailCheck"),
    }
    local, aws = PROFILE_DEFAULTS[Profile.LOCAL], PROFILE_DEFAULTS[Profile.AWS]
    rows = [
        [f"{label} â€” {_code(contract)}", _code(local.sections[port]), _code(aws.sections[port])]
        for port, (label, contract) in labels.items()
    ]
    rows.append(
        [
            f"Model creation â€” {_code('ChatModelProvider')}",
            _code(local.model_provider),
            _code(aws.model_provider),
        ]
    )
    return _table(("Concern and contract", "Local profile", "AWS profile"), rows, "compact")


def providers() -> str:
    """Every registered adapter, by port."""
    registry = _registry()
    rows = []
    for port in PORTS:
        for name in registry.names(port):
            spec = registry.lookup(port, name)
            model = spec.options
            where = (
                f'<a href="#{_anchor(port, name)}">{_e(model.__name__)}</a>'
                if model is not None and model is not NoOptions
                else '<span class="muted">none</span>'
            )
            badge = '<span class="tag warn">local only</span>' if spec.local_only else ""
            rows.append([_code(port), _code(name), badge, _e(_package_of(spec.factory)), where])
    return _table(("Port", "Adapter", "Guard", "Package", "Options"), rows, "compact")


# ------------------------------------------------------------------- options


def _shown_default(default: str | None) -> str:
    """A default as the guide shows it: text without its JSON quotes, unless empty."""
    if default is None:
        return '<span class="muted">not set</span>'
    value = json.loads(default)
    return _code(value) if isinstance(value, str) and value else _code(default)


def _option_rows(model: type[OptionsModel]) -> list[list[str]]:
    return [
        [
            f'<span class="var">{_e(option.name)}</span>',
            _e(option.type),
            '<span class="tag">required</span>'
            if option.required
            else (
                '<span class="muted">keys below</span>'
                if option.group and option.default is None
                else _shown_default(option.default)
            ),
            _doc(option.meaning),
        ]
        for option in options_help(model)
    ]


def options() -> str:
    """The options of every adapter, as each adapter declares them."""
    registry = _registry()
    parts = []
    for port in PORTS:
        for name in registry.names(port):
            spec = registry.lookup(port, name)
            guard = ' <span class="tag warn">local only</span>' if spec.local_only else ""
            title = (
                f'<h4 id="{_anchor(port, name)}"><code>{_e(port)}</code> Â· '
                f"<code>{_e(name)}</code>{guard}</h4>"
            )
            model = spec.options
            if model is None or model is NoOptions:
                parts.append(f'{title}\n<p class="small">{NO_OPTIONS_NOTES[(port, name)]}</p>')
                continue
            parts.append(
                f'{title}\n<p class="small">{_doc(options_summary(model))} '
                f'<span class="muted">({_e(model.__name__)})</span></p>\n'
                + _table(("Option", "Type", "Default", "Meaning"), _option_rows(model), "opts")
            )
    return "\n".join(parts)


# ----------------------------------------------------------------------- CLI


def _commands(group: click.Group, path: list[str]) -> list[tuple[list[str], click.Command]]:
    found: list[tuple[list[str], click.Command]] = []
    for name in sorted(group.commands):
        command = group.commands[name]
        if isinstance(command, click.Group):
            found += _commands(command, [*path, name])
        else:
            found.append(([*path, name], command))
    return found


def _flag_default(option: click.Option) -> str:
    default = option.default
    if option.is_flag:
        return _code("off")
    if default is None or repr(default).startswith("Sentinel") or default == ():
        return '<span class="muted">none</span>'
    if isinstance(default, Path):
        default = default.as_posix()
    return _code(default)


def _usage(path: list[str], command: click.Command) -> str:
    args = []
    for param in command.params:
        if isinstance(param, click.Argument):
            label = (param.name or "").upper()
            if param.nargs == -1:
                label = f"[{label}...]"
            elif not param.required:
                label = f"[{label}]"
            args.append(label)
    return " ".join(["agentcraft2", *path, "[OPTIONS]", *args])


def cli_reference() -> str:
    """Every command and flag of the agentcraft2 CLI."""
    parts = []
    for path, command in _commands(cli, []):
        help_text = inspect.cleandoc(command.help or "").split("\n\n", 1)[0].replace("\n", " ")
        rows = [
            [
                f'<span class="var">{_e(", ".join(p.opts))}</span>',
                _flag_default(p),
                _e(p.help or ""),
            ]
            for p in command.params
            if isinstance(p, click.Option) and not p.hidden
        ]
        parts.append(
            f'<h4 id="cli-{"-".join(path)}"><code>agentcraft2 {_e(" ".join(path))}</code></h4>\n'
            f'<p class="small">{_e(help_text)}</p>\n'
            f'<pre class="usage">{_e(_usage(path, command))}</pre>\n'
            + (
                _table(("Flag", "Default", "Meaning"), rows, "opts")
                if rows
                else '<p class="small muted">No flags.</p>'
            )
        )
    return "\n".join(parts)


# -------------------------------------------------------------------- extras

_PACKAGES = HERE.parents[1] / "packages"


def _extra_comments(text: str) -> dict[str, str]:
    comments: dict[str, str] = {}
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            pending.append(stripped.lstrip("# "))
            continue
        match = re.match(r"^([a-z][a-z0-9-]*) = \[", stripped)
        if match:
            comments[match.group(1)] = " ".join(pending)
        pending = []
    return comments


def extras() -> str:
    """The optional extras of each distribution, from its pyproject.toml."""
    rows = []
    for package in ("agentcraft2-core", "agentcraft2-aws", "agentcraft2-cli"):
        text = (_PACKAGES / package / "pyproject.toml").read_text(encoding="utf-8")
        found = tomllib.loads(text)["project"].get("optional-dependencies", {})
        comments = _extra_comments(text)
        for name, requirements in found.items():
            rows.append(
                [
                    _code(f"{package}[{name}]"),
                    _e(comments.get(name, "")),
                    ", ".join(_code(r) for r in requirements),
                ]
            )
    return _table(("Install", "What it adds", "Requirements"), rows, "compact")


# ------------------------------------------------------------- permissions

_EXAMPLE_SOURCE = {
    "ledger": {
        "kind": "redshift_data",
        "queries_dir": "queries",
        "database": "sales",
        "workgroup": "analytics",
    }
}


def permissions() -> str:
    """What each AWS adapter needs, asked of its own access rule, for example settings.

    The settings are the starting ``deploy.env`` that ``agentcraft2 deploy`` writes
    for an agent, plus one Redshift data source.
    """
    target = Target("helper", "agent", PurePosixPath("agents", "helper"), 8000)
    values = dict(
        line.split("=", 1)
        for line in starter_settings(target).splitlines()
        if line and not line.startswith("#")
    )
    values[variable_for(Key.DATA_SOURCES)] = json.dumps(_EXAMPLE_SOURCE)
    plan = access_plan(ConfigResolver(MappingConfigSource(values)).resolve(), _registry())
    rows = []
    for adapter in plan.adapters:
        for access in adapter.access:
            why = _e(access.why) + (f"<br><em>check:</em> {_e(access.note)}" if access.note else "")
            rows.append(
                [
                    _e(adapter.label),
                    "<br>".join(_code(action) for action in access.actions),
                    "<br>".join(_code(resource) for resource in access.resources),
                    why,
                ]
            )
    return _table(("Adapter", "Actions", "Resources", "Why"), rows, "compact")


# ---------------------------------------------------------------- AgentCore


def agentcore_settings() -> str:
    """Every ``AGENTCORE_`` variable the deploy target reads, from the target's own table.

    The table lives beside the reader in ``deploy/agentcore.py``; this checks
    that it names exactly the variables the module reads, so the guide cannot
    list a variable the target ignores or miss one it reads.
    """
    listed = [variable.name for variable in agentcore_target.AGENTCORE_VARIABLES]
    read = {
        value
        for name, value in vars(agentcore_target).items()
        if name.startswith("V_") and isinstance(value, str)
    }
    if set(listed) != read or len(listed) != len(set(listed)):
        raise ValueError(
            "AGENTCORE_VARIABLES disagrees with the V_ names of deploy/agentcore.py: "
            f"listed {sorted(listed)}, read {sorted(read)}"
        )
    rows = [
        [
            f'<span class="var" id="var-{variable.name.lower()}">{_e(variable.name)}</span>',
            _markdown(variable.values),
            (
                '<span class="muted">not set</span>'
                if variable.default == "not set"
                else _code(variable.default)
            ),
            _markdown(variable.meaning),
        ]
        for variable in agentcore_target.AGENTCORE_VARIABLES
    ]
    prefix = agentcore_target.AGENTCORE_PREFIX
    note = (
        f"<p>The protocol is not a variable (there is no {_code(prefix + 'PROTOCOL')}): it "
        "follows the service's kind, HTTP for an agent and MCP for an MCP server, as does the "
        f"port. Every {_code(prefix)} variable stays out of the container's environment.</p>"
    )
    return _table(("Variable", "Values", "Default", "Meaning"), rows, "compact") + "\n" + note


# --------------------------------------------------------------- public API

API_MODULES: tuple[str, ...] = (
    "agentcraft2_core",
    "agentcraft2_core.contracts",
    "agentcraft2_core.config",
    "agentcraft2_core.di",
    "agentcraft2_core.observability",
    "agentcraft2_core.integrations.langgraph",
    "agentcraft2_core.integrations.mcp",
    "agentcraft2_core.integrations.http",
    "agentcraft2_core.testing",
    "agentcraft2_core.evaluation",
    "agentcraft2_core.adapters",
    "agentcraft2_core.pipeline",
    "agentcraft2_core.kit",
    "agentcraft2_aws",
)
"""The modules whose ``__all__`` is the library's public API, in reading order."""


def public_api() -> str:
    """Every public module, what it is for, and the names it exports."""
    rows = []
    for name in API_MODULES:
        module = importlib.import_module(name)
        summary = (module.__doc__ or "").strip().splitlines()[0]
        exported = sorted(getattr(module, "__all__", ()), key=str.lower)
        rows.append([_code(name), _doc(summary), ", ".join(_code(item) for item in exported)])
    return _table(("Module", "What it is for", "Names"), rows, "compact")


def walkthrough_files() -> dict[Path, str]:
    """Render companion docs and samples from the templates shipped to developers.

    The library's quickstart names its generated agent ``helper``. Links here
    target the library docs; generated workspaces use the templates' local links.
    """
    renderer = TemplateRenderer()
    shared = renderer.render(
        "workspace/docs",
        {
            "library_docs": True,
            "run_instructions": "anthropic-example.md",
            "select_python": "getting-started.md#using-a-new-terminal",
            "reader_path": r"docs\examples\read_trace.py",
            "tracing_questions": "developer-guide.html#faq",
        },
    )
    agent = renderer.render(
        "agent/docs",
        {
            "name": "helper",
            "package": "helper",
            "port": 8000,
            "model_provider": "fake",
            "library": LibrarySource.from_path(HERE.parents[1]),
            "var": variable_names(),
            "setup_link": "getting-started.md#using-a-new-terminal",
            "verified_link": "verified-anthropic-run.md",
            "questions_link": "developer-guide.html#faq",
        },
    )
    reader = renderer.render("workspace/examples", {})
    return {
        **{HERE.parent / path: content for path, content in shared.items()},
        # The repository has a general integration guide; generated agents get
        # their own Phoenix tracing instructions with service-specific paths and commands.
        **{
            HERE.parent / path: content
            for path, content in agent.items()
            if path != PurePosixPath("tracing.md")
        },
        **{HERE.parent / "examples" / path: content for path, content in reader.items()},
    }


def verified_anthropic() -> str:
    """Explain the real captured exchanges using the same samples as new workspaces."""
    files = TemplateRenderer().render("workspace/docs", {})
    events = [
        json.loads(line)
        for line in files[
            PurePosixPath("samples/verified-anthropic/model-trace.jsonl")
        ].splitlines()
    ]
    history = json.loads(files[PurePosixPath("samples/verified-anthropic/checkpoint-history.json")])
    rows = [
        [
            _code(event["call_id"]),
            _code(event["request_id"]),
            str(len(event["messages"])),
            explanation,
        ]
        for event, explanation in zip(
            events[::2],
            (
                'System prompt + first question. The model requests greet(name="Ada").',
                "System prompt + question + AI tool call + tool result. The model answers.",
                "System prompt + four saved messages + follow-up question. The model recalls Ada.",
            ),
            strict=True,
        )
    ]
    tool = events[2]["messages"][-1]
    tool_json = _e(json.dumps(tool, indent=2, ensure_ascii=False))
    restored = events[4]["messages"]
    message_rows = [
        [str(index + 1), _code(message["type"]), origin]
        for index, (message, origin) in enumerate(
            zip(
                restored,
                ("Model node", *["First checkpoint"] * 4, "Second question"),
                strict=True,
            )
        )
    ]
    return (
        _table(("Model call", "Question / request", "Messages", "What reaches the model"), rows)
        + "<p>The fourth message in <code>model-call-2</code> is the tool result. Its "
        "<code>tool_call_id</code> matches the model's requested action, <code>tool-1</code>:</p>"
        f'<pre><code class="language-json" id="verified-tool-result">{tool_json}</code></pre>'
        + "<p>After the first answer, the checkpoint holds four messages: human question, "
        "AI tool call, tool result, and AI answer. The next question restores those messages. "
        "<code>model-call-3</code> receives these six entries, in order:</p>"
        + _table(("Position", "Message type", "Source"), message_rows)
        + "<p>Positions 2 through 5 match the first checkpoint. The model receives this history "
        "from the graph; it does not read SQLite itself. The system prompt is added by "
        "the model node and is not stored in this graph's checkpoint.</p>"
        + f"<p>The actual second answer was:</p><blockquote>{_e(history[1]['answer'])}</blockquote>"
        + "<p>The second snapshot preserves the first four messages and appends the new "
        "human question and AI answer. Reopening SQLite after the process exited returned "
        "the same six messages.</p>"
    )


# -------------------------------------------------------------------- render

_GENERATORS: Mapping[str, Callable[[], str]] = {
    "configuration-variables": configuration_variables,
    "profiles": profiles,
    "intro-profiles": introductory_profiles,
    "providers": providers,
    "options": options,
    "cli": cli_reference,
    "extras": extras,
    "permissions": permissions,
    "agentcore-settings": agentcore_settings,
    "public-api": public_api,
    "verified-anthropic": verified_anthropic,
}

_HEAD_END = "<!-- head-end -->"

_DOCUMENT = """<!doctype html>
<!-- Generated by docs/guide/build_guide.py from docs/guide/guide.src.html. Do not edit by hand. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{head}
</head>
<body>
{body}
</body>
</html>
"""


def render(source: str) -> str:
    """Return the guide's body: the prose with every generated block filled in."""

    def fill(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in _GENERATORS:
            raise KeyError(f"guide.src.html asks for an unknown block: {name}")
        return _GENERATORS[name]()

    return _MARKER.sub(fill, source)


def guide_html() -> str:
    """Return the complete guide as the repository keeps it."""
    head, _, body = render(SOURCE.read_text(encoding="utf-8")).partition(_HEAD_END)
    return _DOCUMENT.format(head=head.strip(), body=body.strip())


def main(argv: list[str] | None = None) -> int:
    """Write or check the guide and companion walkthroughs, or export the guide body."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="exit 1 if generated docs are stale")
    parser.add_argument("--fragment", type=Path, help="also write the body alone to this file")
    arguments = parser.parse_args(argv)
    expected_files = {TARGET: guide_html(), **walkthrough_files()}
    if arguments.check:
        stale = [
            path
            for path, expected in expected_files.items()
            if not path.exists() or path.read_text(encoding="utf-8") != expected
        ]
        if stale:
            for path in stale:
                sys.stderr.write(f"{path.name} is stale; run: python docs/guide/build_guide.py\n")
            return 1
        return 0
    for path, expected in expected_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8", newline="\n")
    if arguments.fragment is not None:
        arguments.fragment.write_text(render(SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
