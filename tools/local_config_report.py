"""Show startup settings with origins, without dumping the process environment."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from tools.local_config_redaction import display_json
from tools.local_processes import report

if TYPE_CHECKING:
    from agentcraft2_core.config import Resolution
    from agentcraft2_core.config.phoenix import PhoenixResolution
    from agentcraft2_core.contracts import ConfigSource

# Application and tracing controls outside the core binding table. Everything
# else is masked, including custom credentials with unfamiliar variable names.
_VISIBLE_EXTRAS = frozenset(
    {
        "RMPREP_CONTEXT_INPUT_TOKENS",
        "RMPREP_CONTEXT_QUESTION_TOKENS",
        "RMPREP_CONTEXT_RECENT_TOKENS",
        "RMPREP_CONTEXT_TOOL_SCHEMA_TOKENS",
        "AC2_PHOENIX_ENABLED",
        "AC2_PHOENIX_CAPTURE_CONTENT",
        "AC2_PHOENIX_ENV_FILE",
        # The earlier EAP_ spellings stay visible for the deprecation window.
        "EAP_PHOENIX_ENABLED",
        "EAP_PHOENIX_CAPTURE_CONTENT",
        "EAP_PHOENIX_ENV_FILE",
        "PHOENIX_COLLECTOR_ENDPOINT",
        "PHOENIX_PROJECT_NAME",
    }
)


def _display(value: str, *, visible: bool) -> str:
    """Mask first, then bound and escape a value for a single console line."""
    from agentcraft2_core.config import MASKED
    from agentcraft2_core.contracts.redaction import redact

    if not value:
        return "(empty)"
    if not visible or value == MASKED:
        return MASKED
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return MASKED
        if (
            parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            return MASKED
    clean = redact(value)
    if len(clean) > 180:
        clean = clean[:177] + "..."
    return json.dumps(clean, ensure_ascii=True)


def report_environment(
    resolution: Resolution,
    layers: Sequence[tuple[str, ConfigSource]],
    *,
    tracing: PhoenixResolution | None = None,
) -> None:
    """Print effective agent variables and their winning layer, with safe defaults.

    The first layer is the shell, followed by the launcher and service files.
    JSON option blocks keep their structure while sensitive fields are masked.
    Unrelated shell variables are never enumerated.
    """
    from agentcraft2_core.config import DEFAULT_BINDINGS, ValueKind

    bindings = {binding.key: binding for binding in DEFAULT_BINDINGS}
    rows: dict[str, tuple[str, str]] = {}
    for key, value in resolution.shown.items():
        binding = bindings.get(key)
        origin = resolution.origins.get(key, "default")
        if origin.startswith("variable "):
            name = origin.removeprefix("variable ")
            origin = next(label for label, source in layers if source.get(name) is not None)
        elif binding is not None:
            name = binding.name
        else:
            continue
        visible = binding is not None and not binding.sensitive
        shown = (
            display_json(value)
            if visible and binding is not None and binding.kind is ValueKind.JSON
            else _display(value, visible=visible)
        )
        rows[name] = (shown, origin)

    # File-defined variables can include SDK settings that core does not bind.
    # Also show relevant shell overrides, but never PATH or the entire OS env.
    names = {name for _, source in layers[1:] for name in source.names()}
    names.update(
        name
        for name in layers[0][1].names()
        # LANGFUSE_ is defense for stale environments: shown masked, never read.
        # EAP_ is the earlier prefix of the library's variables (deprecation window).
        if name.startswith(
            ("AC2_", "EAP_", "RMPREP_", "LANGFUSE_", "PHOENIX_", "OTEL_", "AWS_", "ANTHROPIC_")
        )
    )
    for name in sorted(names - rows.keys()):
        for label, source in layers:
            if (extra_value := source.get(name)) is not None:
                rows[name] = (_display(extra_value, visible=name in _VISIBLE_EXTRAS), label)
                break
    if tracing is not None:
        # Raw launcher/service layers cannot explain a separate tracing file.
        # Replace those rows with the same resolution used by agent startup.
        for name, value in tracing.shown.items():
            origin = tracing.origins[name]
            if origin.startswith("process environment: "):
                variable = origin.removeprefix("process environment: ")
                origin = next(
                    (label for label, source in layers[:2] if source.get(variable) is not None),
                    origin,
                )
            rows[name] = (_display(value, visible=name in _VISIBLE_EXTRAS), origin)
    report(
        "Agent configuration (credentials, sensitive fields and unrecognized variables are masked):"
    )
    for name, (value, origin) in sorted(rows.items()):
        report(f"  {name}={value}  [{origin}]")
