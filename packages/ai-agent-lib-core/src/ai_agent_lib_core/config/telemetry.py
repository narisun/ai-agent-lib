"""Whether a service sends operational traces and metrics: one answer for everyone.

The container picks the telemetry adapter, a service's entry point starts the
SDK, and ``agentlib deploy`` adds the collector and checks the ``otel`` extra.
All of them ask this function, so they cannot disagree.
"""

from __future__ import annotations

from pydantic import ConfigDict, StrictBool

from ai_agent_lib_core.config.bindings import Key, options_key
from ai_agent_lib_core.config.reference import variable_for
from ai_agent_lib_core.contracts import (
    AuditOptions,
    ConfigurationError,
    Section,
    ServiceConfig,
    TelemetryMode,
    shown_value,
)

__all__ = ["telemetry_enabled"]


class _AuditTracing(AuditOptions):
    """The shared audit options only; a provider's own keys are not this module's business.

    ``tracing`` is strict here: a switch is true or false, never the word "yes".
    """

    model_config = ConfigDict(frozen=True, extra="ignore")
    tracing: StrictBool = False


def telemetry_enabled(config: ServiceConfig) -> bool:
    """Whether telemetry is on: ``EAP_TELEMETRY=opentelemetry``, or the older audit option.

    The audit option ``tracing: true`` predates the telemetry setting and still
    turns telemetry on.

    Raises:
        ConfigurationError: If the audit option ``tracing`` is not true or false.
    """
    selection = config.section(Section.AUDIT)
    try:
        tracing = selection.parse_options(_AuditTracing).tracing
    except ConfigurationError as error:
        raise ConfigurationError(
            "the audit option 'tracing' is not true or false",
            expected="true or false",
            actual=shown_value(selection.options.get("tracing")),
            fix=(
                'write "tracing": true or "tracing": false in '
                f"{variable_for(options_key(Section.AUDIT))}, or set "
                f"{variable_for(Key.TELEMETRY)}=opentelemetry instead"
            ),
        ) from error
    return tracing or config.telemetry is TelemetryMode.OPENTELEMETRY
