"""Whether a service sends operational traces and metrics: one answer for everyone.

The container picks the telemetry adapter, a service's entry point starts the
SDK, and ``agentlib deploy`` adds the collector and checks the ``otel`` extra.
All of them ask this function, so they cannot disagree.
"""

from __future__ import annotations

from ai_agent_lib_core.config.bindings import Key, options_key
from ai_agent_lib_core.config.reference import variable_for
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    Section,
    ServiceConfig,
    TelemetryMode,
    shown_value,
)

__all__ = ["telemetry_enabled"]


def telemetry_enabled(config: ServiceConfig) -> bool:
    """Whether telemetry is on: ``EAP_TELEMETRY=opentelemetry``, or the older audit option.

    The audit option ``tracing: true`` predates the telemetry setting and still
    turns telemetry on.

    Raises:
        ConfigurationError: If the audit option ``tracing`` is not true or false.
    """
    tracing = config.section(Section.AUDIT).options.get("tracing", False)
    if not isinstance(tracing, bool):
        raise ConfigurationError(
            "the audit option 'tracing' is not true or false",
            expected="true or false",
            actual=shown_value(tracing),
            fix=(
                'write "tracing": true or "tracing": false in '
                f"{variable_for(options_key(Section.AUDIT))}, or set "
                f"{variable_for(Key.TELEMETRY)}=opentelemetry instead"
            ),
        )
    return tracing or config.telemetry is TelemetryMode.OPENTELEMETRY
