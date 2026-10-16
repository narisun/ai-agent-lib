# ai-agent-lib-core

Core package of the Enterprise Agentic Platform library: contracts (ports,
value types, errors), configuration, the service container, the request
pipelines, local adapters, the LangGraph, MCP and HTTP bindings,
observability, the testing kit and the evaluation package.

Local development uses Python 3.11+, pip, and the configured local providers.
The AWS adapters are in `ai-agent-lib-aws`; installing that package and the
selected provider's extra makes them selectable by configuration.

From the repository root, with your virtual environment active, install the
editable checkout:

```bat
python -m pip install -e "packages/ai-agent-lib-core[jwt,mcp,otel,serve]"
```

| Extra | Adds |
| --- | --- |
| `duckdb` | The DuckDB data source, for CSV files in local development |
| `jwt` | The jwt identity provider: OAuth 2.0 access tokens, for example from Microsoft Entra ID |
| `mcp` | The MCP bindings |
| `serve` | The HTTP entry point, health routes and server loop |
| `otel` | Exporting operational telemetry and optional Langfuse agent traces |
| `anthropic` | The anthropic model provider |
| `testing` | The contract test suites in `ai_agent_lib_core.testing.contracts` |

Start with the [repository README](../../README.md) or
[getting-started tutorial](../../docs/getting-started.md). The
[developer guide](../../docs/developer-guide.html) explains the design and lists
variables, adapter options, commands, and public modules. The
[code map](../../docs/README.md#find-the-code) identifies where changes belong.

The [Langfuse integration](../../docs/langfuse.md) supplies shared model/tool
tracing, usage and checkpoint references. Generated HTTP agents already include
it; configure a project to enable export to a separate backend.

HTTP services reject overlapping turns on the same thread before execution.
See [thread admission](../../docs/http-thread-admission.md) for the default
single-instance guard and shared coordination required by multiple workers.
