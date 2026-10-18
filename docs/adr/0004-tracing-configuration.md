# 0004: Tracing configuration ownership

## Context

`config/langfuse.py` and `config/phoenix.py` resolve `LANGFUSE_*`, `PHOENIX_*`
and `EAP_LANGFUSE_ENABLED` through dedicated loaders, outside the binding
table in `config/bindings.py`. The October `doctor` blind spot came from
this parallel path; the fix shared a loader for that one check, but the
second path itself remains, and the binding table's "unknown `EAP_` names
fail startup" guarantee does not cover these variables. Tracing code spans
`config/`, `observability/` and `integrations/langgraph/`.

## Decision

Keep the vendors' documented names (`LANGFUSE_BASE_URL`, `PHOENIX_API_KEY`
and the rest) and register them in the binding table as listed third-party
reads, the way `AWS_PROFILE`, `AWS_REGION`, `AWS_CA_BUNDLE` and
`AWS_EXECUTION_ENV` already are; `OTEL_*` variables are read directly by the
OpenTelemetry SDK through `configure_telemetry()` and stay outside the table.
Keep `EAP_LANGFUSE_ENABLED`, `EAP_LANGFUSE_CAPTURE_CONTENT`,
`EAP_LANGFUSE_ENV_FILE`, `EAP_PHOENIX_ENABLED`, `EAP_PHOENIX_CAPTURE_CONTENT`
and `EAP_PHOENIX_ENV_FILE` as bound, `EAP_`-prefixed variables. Every
optional integration must resolve its settings through one loader shared by
startup, `doctor` and `config explain` — the Langfuse fix generalized as a
standing rule, not a one-off.

## Consequences

Langfuse and Phoenix need their variables registered and their loaders
adapted to read through the binding table; no behavioral change to current
deployments. Future optional integrations must register their variables and
share a loader across startup, doctor and config explain before shipping, or
they inherit the same blind spot. Consolidating the tracing modules across
`config/`, `observability/` and `integrations/langgraph/` is possible later
but is not decided here.

## Owner

Library maintainers.
