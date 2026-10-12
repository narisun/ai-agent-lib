"""What the people who run a service see: operational logs, traces and metrics."""

from ai_agent_lib_core.observability.diagnostics import Diagnostics, bind_diagnostic_links
from ai_agent_lib_core.observability.errors import (
    error_fields,
    explain,
    report_error,
    shows_details,
    your_code,
)
from ai_agent_lib_core.observability.logs import (
    LIBRARY_LOGGERS,
    JsonLogFormatter,
    configure_logging,
    redact,
)
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink
from ai_agent_lib_core.observability.otel import (
    build_providers,
    configure_telemetry,
    start_telemetry,
)

__all__ = [
    "LIBRARY_LOGGERS",
    "Diagnostics",
    "JsonLogFormatter",
    "JsonlModelTraceSink",
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
