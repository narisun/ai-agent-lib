# ai-agent-lib-core

Core package of the Enterprise Agentic Platform library. It holds the
contracts (ports, value types, errors), configuration, the service container,
the request pipelines and the local adapters.

On top of those sit the LangGraph, MCP and HTTP bindings. In
`ai_agent_lib_core.integrations.http`, an `AgentService` declares what is
specific to one agent and `run_agent` serves it: configuration, logging,
telemetry, optional Phoenix tracing, the container's lifecycle, readiness and
shutdown. Observability, the testing kit and the evaluation package, which
scores an agent offline and online, complete the package.

Local development uses Python 3.11+, pip, and the configured local providers.
The AWS adapters are in `ai-agent-lib-aws`; installing that package and the
selected provider's extra makes them selectable by configuration.

From the repository root in **Command Prompt**, use your approved Python and
its pip to install the editable checkout. On macOS/Linux, use a bash or zsh
terminal and `python3` in place of `python`:

```bat
python -m pip install -e "packages/ai-agent-lib-core[jwt,mcp,otel,serve]"
```

| Extra | Adds |
| --- | --- |
| `duckdb` | The DuckDB data source, for CSV files in local development |
| `jwt` | The jwt identity provider: OAuth 2.0 access tokens, for example from Microsoft Entra ID |
| `mcp` | The MCP bindings |
| `serve` | The HTTP entry point, health routes and server loop |
| `otel` | Exporting operational telemetry and Phoenix agent traces |
| `anthropic` | The anthropic model provider |
| `testing` | The contract test suites in `ai_agent_lib_core.testing.contracts` |

Start with the [repository README](../../README.md) or
[getting-started tutorial](../../docs/getting-started.md). The
[developer guide](../../docs/developer-guide.html) explains the design and lists
variables, adapter options, commands, and public modules. The
[code map](../../docs/README.md#find-the-code) identifies where changes belong.
To score an agent's answers, offline against a dataset or online against saved
chats, read [Measure an agent with evals](../../docs/evals.md).

Phoenix is the only agent-tracing backend. It records model and tool calls,
usage and checkpoint references, and `run_agent` wires it: a service turns it
on with `AC2_PHOENIX_ENABLED=true` in its `.env`, with the `otel` extra
installed. [Trace agents with Phoenix](../../docs/phoenix-trial.md) covers the
settings, privacy and a team-hosted server.

HTTP services reject overlapping turns on the same thread before execution.
See [thread admission](../../docs/http-thread-admission.md) for the default
single-instance guard and shared coordination required by multiple workers.
