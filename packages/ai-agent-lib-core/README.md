# ai-agent-lib-core

Core package of the Enterprise Agentic Platform library: contracts (ports,
value types, errors), configuration, the service container, the request
pipelines, local adapters, the LangGraph, MCP and HTTP bindings,
observability, the testing kit and the evaluation package.

Everything here runs on a developer's machine with no cloud account. The AWS
adapters are in `ai-agent-lib-aws`; installing it is enough to make them
selectable by configuration.

```bash
pip install "ai-agent-lib-core[jwt,mcp,otel,serve]"   # what a generated agent or MCP server uses
```

| Extra | Adds |
| --- | --- |
| `duckdb` | The DuckDB data source, for CSV files in local development |
| `jwt` | The jwt identity provider: OAuth 2.0 access tokens, for example from Microsoft Entra ID |
| `mcp` | The MCP bindings |
| `serve` | The HTTP entry point, health routes and server loop |
| `otel` | Exporting traces and metrics to an OpenTelemetry collector |
| `anthropic` | The anthropic model provider |
| `testing` | The contract test suites in `ai_agent_lib_core.testing.contracts` |

Start with the repository's `README.md` and `docs/getting-started.md`; the
developer guide, `docs/developer-guide.html`, lists every variable, adapter
option, command and public module.
