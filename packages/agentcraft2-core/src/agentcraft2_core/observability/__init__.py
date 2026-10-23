"""What the people who run a service see: operational logs, traces and metrics."""

from agentcraft2_core.observability.diagnostics import Diagnostics, bind_diagnostic_links
from agentcraft2_core.observability.errors import (
    error_fields,
    explain,
    report_error,
    shows_details,
    your_code,
)
from agentcraft2_core.observability.logs import (
    LIBRARY_LOGGERS,
    JsonLogFormatter,
    configure_logging,
    redact,
)
from agentcraft2_core.observability.model_trace import JsonlModelTraceSink
from agentcraft2_core.observability.otel import (
    ambient_provider_installed,
    build_providers,
    configure_telemetry,
    start_telemetry,
)

__all__ = [
    "LIBRARY_LOGGERS",
    "Diagnostics",
    "JsonLogFormatter",
    "JsonlModelTraceSink",
    "ambient_provider_installed",
    "bind_diagnostic_links",
    "build_providers",
    "configure_logging",
    "configure_telemetry",
    "error_fields",
    "explain",
    "redact",
    "report_error",
    "shows_details",
    "start_telemetry",
    "your_code",
]
