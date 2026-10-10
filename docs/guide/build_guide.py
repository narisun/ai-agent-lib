"""Render the developer guide from its prose and from the code.

The prose lives in ``guide.src.html`` beside this script. Every table that
lists something the code defines (variables, defaults, adapters, adapter
options, command-line flags) is generated here from the code itself, so the
guide cannot drift from the library. A test fails when the committed guide is
stale.

Usage::

    python docs/guide/build_guide.py            # write docs/developer-guide.html
    python docs/guide/build_guide.py --check    # exit 1 if it is stale
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

from ai_agent_lib_cli.app import cli
from ai_agent_lib_cli.deploy import Target, starter_settings
from ai_agent_lib_core.config import ConfigResolver, Key, MappingConfigSource, variable_for
from ai_agent_lib_core.config.bindings import (
    DEFAULT_BINDINGS,
    SECRET_VARIABLE_PREFIX,
    Binding,
    ValueKind,
    options_key,
    provider_key,
)
from ai_agent_lib_core.config.profiles import PROFILE_DEFAULTS
from ai_agent_lib_core.config.reference import default_text
from ai_agent_lib_core.contracts import NoOptions, OptionsModel, Profile, Section
from ai_agent_lib_core.contracts.options_help import options_help, options_summary
from ai_agent_lib_core.di import DATA_PORT, MODEL_PORT, ServiceProviders, access_plan

__all__ = [
    "API_MODULES",
    "NO_OPTIONS_NOTES",
    "OTEL_VARIABLES",
    "PORTS",
    "guide_html",
    "main",
    "render",
    "section_of",
]

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "guide.src.html"
TARGET = HERE.parent / "developer-guide.html"
_MARKER = re.compile(r"<!-- generated: (?P<name>[a-z-]+) -->")

PORTS: tuple[str, ...] = (MODEL_PORT, *(section.value for section in Section), DATA_PORT)
"""The ports, in the order the guide lists them."""

NO_OPTIONS_NOTES: Mapping[tuple[str, str], str] = {
    (MODEL_PORT, "anthropic"): (
        "No options. The key is the secret <code>anthropic_api_key</code>: "
        "<code>ANTHROPIC_API_KEY</code> with the env secrets adapter, Secrets Manager when "
        "deployed. Uses <code>HTTPS_PROXY</code>. Needs the <code>anthropic</code> extra."
    ),
    (MODEL_PORT, "bedrock"): (
        "No options. Uses the AWS session settings (<code>AWS_PROFILE</code>, "
        "<code>AWS_REGION</code>) and <code>EAP_TLS_CA_BUNDLE</code>."
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
    if binding.deprecated_names:
        notes.append("replaces " + ", ".join(_code(n) for n in binding.deprecated_names))
    return " ".join(notes)


def section_of(binding: Binding) -> Section | None:
    """Return the section a per-section binding belongs to, or ``None``."""
    for section in Section:
        if binding.key in (provider_key(section), options_key(section)):
            return section
    return None


def _explanation(binding: Binding) -> str:
    section = section_of(binding)
    text = _markdown(binding.details)
    if section is not None and binding.key == provider_key(section):
        names = ", ".join(_code(n) for n in _registry().names(section.value))
        return f"{text} Adapters: {names}."
    if section is not None:
        return f'{text} See <a href="#options">adapter options</a>.'
    return text


def _variable_rows(bindings: Iterable[Binding]) -> list[list[str]]:
    return [
        [
            f'<span class="var" id="var-{b.name.lower()}">{_e(b.name)}</span>',
            f'{_e(b.description)}<div class="more">{_explanation(b)}</div>',
            _default_of(b),
            _notes(b),
        ]
        for b in bindings
    ]


def variables_owned() -> str:
    """The variables the library owns."""
    rows = _variable_rows(b for b in DEFAULT_BINDINGS if b.owned)
    rows.append(
        [
            f'<span class="var">{_e(SECRET_VARIABLE_PREFIX)}&lt;NAME&gt;</span>',
            "One named secret for the env secrets adapter."
            f'<div class="more">{_code(SECRET_VARIABLE_PREFIX + "RATES_TOKEN")} supplies the '
            f"secret {_code('rates_token')}. Options such as {_code('auth_secret')} name the "
            "secret, never the value.</div>",
            '<span class="muted">not set</span>',
            '<span class="tag warn">sensitive</span>',
        ]
    )
    return _table(("Variable", "Purpose", "Default", "Notes"), rows, "vars")


def variables_external() -> str:
    """The variables of other tools that the library reads."""
    rows = _variable_rows(b for b in DEFAULT_BINDINGS if not b.owned)
    return _table(("Variable", "Purpose", "Default", "Notes"), rows, "vars")


def variables_otel() -> str:
    """The OpenTelemetry SDK's variables."""
    rows = [
        [f'<span class="var">{_e(n)}</span>', _e(why), _code(d)] for n, d, why in OTEL_VARIABLES
    ]
    return _table(("Variable", "Purpose", "SDK default"), rows, "vars")


# ------------------------------------------------------------------ adapters


def _registry() -> ServiceProviders:
    return ServiceProviders.default()


def _package_of(factory: Callable[..., object]) -> str:
    module = getattr(factory, "__module__", "") or ""
    return "ai-agent-lib-aws" if module.startswith("ai_agent_lib_aws") else "ai-agent-lib-core"


def profiles() -> str:
    """The adapters each profile selects."""
    local, aws = PROFILE_DEFAULTS[Profile.LOCAL], PROFILE_DEFAULTS[Profile.AWS]
    rows = [["model", _code(local.model_provider), _code(aws.model_provider)]]
    rows += [
        [section.value, _code(local.sections[section]), _code(aws.sections[section])]
        for section in Section
    ]
    return _table(("Port", "local profile", "aws profile"), rows, "compact")


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
                f'<h4 id="{_anchor(port, name)}"><code>{_e(port)}</code> · '
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
    return " ".join(["agentlib", *path, "[OPTIONS]", *args])


def cli_reference() -> str:
    """Every command and flag of the agentlib CLI."""
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
            f'<h4 id="cli-{"-".join(path)}"><code>agentlib {_e(" ".join(path))}</code></h4>\n'
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
    for package in ("ai-agent-lib-core", "ai-agent-lib-aws", "ai-agent-lib-cli"):
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

    The settings are the starting ``deploy.env`` that ``agentlib deploy`` writes
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


# --------------------------------------------------------------- public API

API_MODULES: tuple[str, ...] = (
    "ai_agent_lib_core",
    "ai_agent_lib_core.contracts",
    "ai_agent_lib_core.config",
    "ai_agent_lib_core.di",
    "ai_agent_lib_core.observability",
    "ai_agent_lib_core.integrations.langgraph",
    "ai_agent_lib_core.integrations.mcp",
    "ai_agent_lib_core.integrations.http",
    "ai_agent_lib_core.testing",
    "ai_agent_lib_core.evaluation",
    "ai_agent_lib_core.adapters",
    "ai_agent_lib_core.pipeline",
    "ai_agent_lib_core.kit",
    "ai_agent_lib_aws",
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


# -------------------------------------------------------------------- render

_GENERATORS: Mapping[str, Callable[[], str]] = {
    "variables-owned": variables_owned,
    "variables-external": variables_external,
    "variables-otel": variables_otel,
    "profiles": profiles,
    "providers": providers,
    "options": options,
    "cli": cli_reference,
    "extras": extras,
    "permissions": permissions,
    "public-api": public_api,
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
    """Write, check or export the guide."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="exit 1 if the guide is stale")
    parser.add_argument("--fragment", type=Path, help="also write the body alone to this file")
    arguments = parser.parse_args(argv)
    expected = guide_html()
    if arguments.check:
        actual = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if actual != expected:
            sys.stderr.write(f"{TARGET.name} is stale; run: python docs/guide/build_guide.py\n")
            return 1
        return 0
    TARGET.write_text(expected, encoding="utf-8", newline="\n")
    if arguments.fragment is not None:
        arguments.fragment.write_text(render(SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
