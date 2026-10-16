# Trace agents with Langfuse

Agent Lab provides chat and memo viewing. Langfuse provides a timeline of the
model and tool work behind each answer. The library's `LangfuseTracing` adapter
connects any agent using its governed LangGraph bindings to a separate Langfuse
service. It records actual model attempts, local tools and MCP calls, usage, and
checkpoint references. Business graph nodes do not need tracing code.

## Start with a generated agent

New CLI-generated agents include the integration in `service.py`. Their README
links to `docs/langfuse.md` with commands using the generated agent's name.

1. Get a Langfuse project URL, project ID and API keys from your platform team,
   or start the [separate local backend](langfuse-trial.md#1-start-the-separate-backend).
2. Save these settings in a private file such as `F:\work\langfuse\agent.env`:

```dotenv
EAP_LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_PROJECT_ID=your-project-id
LANGFUSE_PUBLIC_KEY=your-project-public-key
LANGFUSE_SECRET_KEY=your-project-secret-key
EAP_LANGFUSE_CAPTURE_CONTENT=false
```

3. In Command Prompt, use the agent environment's Python and start its HTTP service:

```bat
python -m pip install "ai-agent-lib-core[otel]"
set "EAP_LANGFUSE_ENV_FILE=F:\work\langfuse\agent.env"
python -m your_agent.service
```

Replace the file path and module name with your values. Generated agents already
declare the `otel` extra; `python -m ai_agent_lib_cli install` installs it using
pip. Keep the enterprise pip index/certificate configuration supplied by your
team. Developers connecting to a shared backend need Python, pip and access to
that server. Agent Lab remains in its separate Python environment.

## Settings and privacy

| Variable | Purpose | Default |
| --- | --- | --- |
| `EAP_LANGFUSE_ENABLED` | Enable export for this service | `false` |
| `EAP_LANGFUSE_ENV_FILE` | Optional private settings file | Unset |
| `EAP_LANGFUSE_CAPTURE_CONTENT` | Capture model input/output, tool arguments/results, request and accepted output | `false` |
| `LANGFUSE_BASE_URL` | HTTPS origin; HTTP allowed for loopback | Required when enabled |
| `LANGFUSE_PROJECT_ID` | ID following `/project/` in the project URL | Required when enabled |
| `LANGFUSE_PUBLIC_KEY` | Project public API key | Required when enabled |
| `LANGFUSE_SECRET_KEY` | Project secret API key | Required when enabled |

Process variables override the separate file, which overrides the service's
`.env`. A file path declared in `.env` is relative to that file; a path declared
in Command Prompt is relative to the current directory. Restart after changes.
These settings are loaded at the tracing boundary; `ServiceConfig` and its
existing resolution API stay unchanged. The developer guide lists them in its
searchable configuration table.

From the workspace root, validate the agent's settings before starting it:

```bat
python -m ai_agent_lib_cli config explain your-agent
python -m ai_agent_lib_cli doctor your-agent
```

Replace `your-agent` with its registered name. `config explain` shows the source
file or process variable and masks both API keys. `doctor` reports Langfuse as
disabled, valid, or misconfigured using the same resolver as startup. This check
validates configuration; it does not contact Langfuse or export test content.
Explicitly setting `EAP_LANGFUSE_ENABLED=false` in Command Prompt skips the
separate file, including when that file is missing.

Older services can declare a migration alias in `pyproject.toml`, as rmprep does:

```toml
[tool.ai-agent-lib.langfuse]
legacy-prefix = "RMPREP"
```

Startup and CLI diagnostics then accept the old prefixed enable, content and file
variables. Canonical variables win within each source; process variables still
override files. New agents use the canonical variables in the table above.

Enable content only for approved data. Credential masking is best effort;
conversation and business content remain visible to users with Langfuse access.
The browser receives only a private trace URL. Project API keys remain in the
agent process. Metadata-only tracing still includes request/thread IDs, scoped
session hashes and checkpoint references. Plan access and retention for these.

## Add tracing to an existing service

Import the library integration in the service entry point:

```python
from ai_agent_lib_core.integrations.langgraph.langfuse import (
    LangfuseTracing,
    langfuse_tracing,
)
```

Wrap the service lifetime so the container closes before the exporter flushes:

```python
async with langfuse_tracing(APPLICATION, config, dotenv_path=ENV_FILE) as tracing:
    services = ServiceContainer(config, model_trace=tracing)
    try:

        async def start():
            await services.start()
            await services.validate()

        lifecycle = ServiceLifecycle(start)
        app = await build_app(services, lifecycle, tracing=tracing)
        await serve(app, lifecycle, host=host, port=port)
    finally:
        await services.aclose()
```

In `build_app`, add an optional keyword argument `tracing: LangfuseTracing | None
= None`. Route the response handler through it when enabled:

```python
async def run(context, given):
    question = parse_question(given)
    if tracing is not None:
        return await tracing.run(graph, services, context, question, respond=response)
    return await response(graph, services, context, question)
```

Your `response(graph, services, context, input)` function retains its current
behavior and returns the output dictionary. It must invoke the supplied graph
to retain its tool callbacks. The adapter copies that dictionary and adds
`trace` metadata; this field is reserved while tracing is enabled. It uses
`answer` as root output when available, otherwise the whole result. If the
output contains an `artifacts` list, artifact IDs are recorded without assuming
they are memos. Applications control their own graph state and output schema.

For isolated tests, construct `LangfuseTracing(settings, application=APPLICATION,
exporter=InMemorySpanExporter())` and inject it as the model trace sink and
response wrapper. The caller owns this adapter and calls `close()` after requests
finish. The `langfuse_tracing` context manager handles ownership in normal services.

## What developers should see

- One trace per request, with a session scoped to caller, tenant, agent and thread.
- One generation per actual model attempt, including retry/review attempts.
- Local-tool and MCP observations; model inputs after tools show their results.
- Provider usage split into uncached input, cache reads, cache writes and output.
- Request-scoped checkpoint IDs and parent IDs, bounded to 64 references and a
  one-second history-read deadline. Full checkpoint contents remain in the agent
  database. A timeout marks `checkpoints_unavailable` and `checkpoints_timed_out`
  in root metadata; the accepted answer still returns. Truncation is also marked.
- An **Open trace in Langfuse** link below each supported Agent Lab response,
  preserved through refresh and outcome recovery.

The adapter uses a private OpenTelemetry provider and the
[Langfuse OTLP/HTTP endpoint](https://langfuse.com/integrations/native/opentelemetry).
It does not replace the global telemetry provider or change operational metrics
destinations. The existing JSONL model trace remains available when configured.
Export is asynchronous and best effort; links identify traces, not ingestion
receipts. Export failure does not retry business operations or discard answers.

Custom services can set `checkpoint_timeout_seconds` when constructing
`LangfuseTracing`; it must be finite and positive. History reads use normal async
cancellation, so custom checkpoint providers must support cancellation. No
background history task remains after the request finishes.

Generated services trace `/invoke`, including live responses. Existing standalone
CLI commands and the legacy `/invoke/stream` graph-step endpoint are unchanged.
For a custom entry point, wrap its response function explicitly as shown above.
See the [verified rmprep walkthrough](langfuse-trial.md) for a memo, same-thread
follow-ups and evidence checks. rmprep continues accepting its original
`RMPREP_LANGFUSE_*` flags; new agents use the shared names listed above.

The shared adapter was also checked on 13 October 2026 using a freshly generated
`shared-trace-agent` and the local Langfuse OSS 4.58.0 backend. Two synthetic
questions on one thread produced two traces in one session, three scripted model
generations, one `greet` observation and eight checkpoint references. This
verified the generated service and real OTLP ingestion without a paid model call.
