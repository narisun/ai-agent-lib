# ai-agent-lib

Shared, governed plumbing for LangGraph agents and MCP servers on the
Enterprise Agentic Platform (EAP). Teams write graph and tool logic; the
library supplies configuration, identity, audit and the other cross-cutting
concerns behind small interfaces, so any of them can be swapped by
configuration and faked in a unit test.

## Start a workspace

```bash
uv run --project <path-to-this-repository> --package ai-agent-lib-cli agentlib init my-platform
cd my-platform && uv sync --all-packages
uv run agentlib new mcp hello-mcp
uv run agentlib new agent hello-agent --mcp hello
uv sync --all-packages && uv run pytest
```

That gives a working agent and MCP server with their tests, on the local
adapters, with no account and no network. [`docs/getting-started.md`](docs/getting-started.md)
goes on from there: changing them test first, and a server over your own CSV
files, REST API or Redshift tables. The rest of this page is what that
generated code is made of. The commands are described in
[`packages/ai-agent-lib-cli`](packages/ai-agent-lib-cli/README.md).

## What a graph author writes

```python
from langchain_core.messages import HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ai_agent_lib_core import RequestContext, ServiceContainer


def build_graph(services: ServiceContainer):
    """Built once per process, from one started container."""
    tools = services.tools([lookup_balance], read_only=["lookup_balance"])
    model = services.model("default").bind_tools(tools)

    async def agent(state: State) -> dict:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(**services.compile_kwargs())


async def answer(graph, services: ServiceContainer, context: RequestContext, question: str) -> str:
    """Run once per request, on behalf of the caller in ``context``."""
    result = await graph.ainvoke(
        {"messages": [HumanMessage(question)]}, **services.invocation(context)
    )
    return str(result["messages"][-1].content)
```

Everything returned by `services` is a native LangGraph or LangChain object.
Governed tools keep the native features: injected state, store, runtime and
tool call ID, `Command` results and artifacts.
A runnable version is in [`examples/accounts-agent`](examples/accounts-agent).

## What an MCP server author writes

```python
from mcp.server.mcpserver import MCPServer

from ai_agent_lib_core import ServiceContainer


def build_server(services: ServiceContainer) -> MCPServer:
    server = MCPServer("accounts-mcp", **services.mcp_server_kwargs("accounts"))
    ledger = services.data_source("ledger")

    @server.tool(name="accounts.by_region")
    async def by_region(region: str) -> dict:
        """List the accounts in one region."""
        return (await ledger.query("accounts_by_region", {"region": region})).to_payload()

    return server
```

Those keyword arguments are the library's whole footprint on the server. Over
HTTP, a request with no valid token is answered with 401 before it reaches
anything. Every tool call then runs through identity, the tool registry,
policy, guardrails and audit. The data source runs named queries only, asks
the policy before each one, and applies the row filters, masks and row caps
the policy returns. A runnable version, over CSV files, is in
[`examples/accounts-mcp`](examples/accounts-mcp); an agent reaches its tools
with `await services.mcp_tools("accounts")`. A server compiles no graph, so it
sets `EAP_CHECKPOINT_PROVIDER=none` and needs no store for graph state.

## Running as a service

`ai_agent_lib_core.integrations.http` (the `serve` extra) is what a deployed
agent or MCP server runs on. It is optional: an agent with its own web
framework calls `services.authenticate(token, ...)` itself.

```python
from ai_agent_lib_core import RequestContext, ServiceContainer
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app, serve


async def main() -> None:
    async with ServiceContainer.from_env() as services:
        graph = build_graph(services)  # built once

        async def run(context: RequestContext, given: dict) -> dict:
            return {"answer": await answer(graph, services, context, given["question"])}

        lifecycle = ServiceLifecycle(services.validate)
        app = agent_app(services, run, application="accounts-agent", lifecycle=lifecycle)
        await serve(app, lifecycle, host="0.0.0.0", port=8000)
```

| Route | Answers |
| --- | --- |
| `POST /invoke` | Takes `{"input": ..., "thread_id": "..."}` and a bearer token. The identity provider decides who the caller is; `run` gets their request context. A refused token is 401, a policy denial 403, and a failure is reported by a code, never by the error's text |
| `POST /invoke/stream` | Only when `agent_app(..., stream=steps)` is given. The same input and token; the answer is server-sent events: one `update` per item `steps` yields, then `end` with the thread and request IDs, or `error` with a code |
| `GET /healthz` | 200 while the process is up. No token |
| `GET /readyz` | 200 once startup validation has passed, 503 before that and while the service drains. No token |

`serve` listens first and validates second, so liveness answers while
readiness still says no. On SIGTERM the service reports not ready for
`drain_seconds` while it still takes requests, then stops listening and gives
the requests in flight `grace_seconds` to finish. It then returns, so the
container closes what it holds and the process exits with code 0. Keep the two
times together below the platform's stop timeout, which is 30 seconds on
Fargate by default.

An MCP server gets the same two health routes and the same stop behaviour:

```python
lifecycle = ServiceLifecycle(services.validate)
add_health_routes(server, lifecycle)
app = server.streamable_http_app(stateless_http=True, json_response=True, host=host)
await serve(app, lifecycle, host=host, port=port)
```

To stream, pass a function that yields what each step produced, for example
the items of `graph.astream(..., stream_mode="updates", **services.invocation(context))`.
Every step still runs through the pipelines. A generated agent does this already.

### Logs and telemetry

```python
from ai_agent_lib_core.observability import configure_logging, configure_telemetry

configure_logging("accounts-agent")  # first line of the entry point
shutdown = configure_telemetry("accounts-agent")  # optional; needs the otel extra
```

`configure_logging` writes one JSON object per line to standard output, which
is what the Fargate log driver collects. These are operational logs: started,
listening, ready, one `request` line per agent call with route, status,
duration and request ID, one `tool_call` line per MCP tool call with the tool,
how the call ended and its duration, draining, stopped, and failures of what
the service depends on.
They never hold what a caller asked or a model answered; that is not in the
audit log either. Loggers outside the library are quiet below WARNING, and
anything shaped like a credential is replaced. An error is logged with its
facts as fields: `error_type`, `error_at`, `error_expected`, `error_actual`,
`error_fix`, `error_notes` and the line of your code that led to it.

`configure_telemetry` installs OpenTelemetry providers that send traces and
metrics to a collector over OTLP. The OpenTelemetry SDK reads where the
collector is from its own standard variables. Each agent request is one
`agent.invoke` span, with a child span for each model, tool and query call
named and labelled by the OpenTelemetry GenAI conventions (`chat {model}`,
`execute_tool {tool}`). The trace continues into the MCP servers the agent
calls. Spans per call, events and metrics are on when
`EAP_TELEMETRY=opentelemetry`. Call the function `configure_telemetry` returns
when the service stops.

### When it fails

Every error the library raises says what the code expected, what it got, and
how to fix it, with notes on which adapter and which variable were involved:

```text
helper: ConfigurationError: the options of provider 'jsonl' are not valid (2 problems)
  expected: path: a path, written as text; only known options: fsync, path, tracing
  got: path = 5; an unknown option 'fsynk' (did you mean 'fsync'?)
  fix: correct the options; the developer guide lists every option with its default
  note: while building the audit adapter 'jsonl', chosen by EAP_AUDIT_PROVIDER with options from EAP_AUDIT_OPTIONS
  raised at: .../ai_agent_lib_core/contracts/options.py:279 in parse_options
  your code: agents/helper/src/helper/__main__.py:21 in _run
```

Generated entry points print it with `report_error(program, error,
details=shows_details(config))`. Secrets, tokens and what a caller sent never
appear in a message; a value that helps to debug goes in `error.detail`, shown
only on a developer's machine.

### Is it configured correctly?

`ServiceContainer.check()` builds every adapter the configuration selects,
runs each one's startup check and returns one result per adapter, with what is
wrong and how to fix it. `diagnose(config)` from `ai_agent_lib_core.di` does
the same from a configuration that may not start at all. Neither calls a
model. `agentlib doctor` prints the results for every service of a workspace.

## Deploying

`agentlib deploy SERVICE` reads the settings the service will run with in AWS
from `deploy.env` in its folder, asks each selected adapter what it needs from
IAM, and writes a Terraform root module for ECS Fargate (with the OPA and
OpenTelemetry collector sidecars when the settings use them), the task's
environment, a least-privilege task policy with a reason for every statement,
and a Dockerfile. `--plan` shows all of it and writes nothing. Nothing is
applied: you run `terraform`.

## Layout

| Path | What it is |
| --- | --- |
| `packages/ai-agent-lib-core` | Contracts, configuration, container, pipelines, local adapters, the LangGraph, MCP and HTTP bindings, observability, the testing kit and the evaluation package |
| `packages/ai-agent-lib-aws` | AWS adapters, loaded automatically when the package is installed: Bedrock, Secrets Manager, Firehose, Redshift, S3, PostgreSQL and Bedrock guardrails. See its [README](packages/ai-agent-lib-aws/README.md) |
| `packages/ai-agent-lib-cli` | The `agentlib` command: creates a workspace, adds agents and MCP servers with their tests, checks, evaluates and deploys them. See its [README](packages/ai-agent-lib-cli/README.md) |
| `examples/accounts-agent` | The reference agent |
| `examples/accounts-mcp` | The reference MCP server, with its queries, rules and registries |
| `policies/bundle` | The platform's Rego bundle for OPA, and its tests |
| `tests/architecture` | Rules that keep the module boundaries in place |
| `docs/developer-guide.html` | The developer guide: concepts, practices and every reference table, generated from `docs/guide/` |
| `docs/getting-started.md` | From nothing to a tested agent and MCP server over your own data, and on to AWS |
| `docs/variables.md` | Every configuration variable, generated from the binding table |
| `docs/identity.md` | How a signed-in user and the agent acting for them reach an MCP server, with Microsoft Entra ID and without |
| `docs/adr` | Recorded decisions |

The repository root is a `uv` workspace and is not itself a distribution.

Inside `ai_agent_lib_core`:

| Package | Role | May import |
| --- | --- | --- |
| `contracts` | Ports, value types, options models, errors | Nothing else in the library |
| `config` | Sources, the variable binding table, the resolver | `contracts` |
| `pipeline` | The interceptor contract and the fixed stage order | `contracts` |
| `adapters` | Local implementations of the ports | `contracts` |
| `di` | The provider registry and the service container | All of the above |
| `integrations.langgraph` | Governed model, tools and checkpointer | The only place that imports `langgraph` |
| `integrations.mcp` | The server middleware and the agent-side MCP tools | The only place that imports the MCP SDK |
| `integrations.http` | An agent's HTTP entry point, health routes and the server loop | The only place that imports Starlette and uvicorn |
| `observability` | JSON operational logs, error reports and OpenTelemetry export for a service's entry point | `contracts`, `pipeline` |
| `kit` | What a provider pack builds on: the registry and shared adapter machinery | Below `di` |
| `testing` | Fakes, scripted replies and the contract test suites | Everything |
| `evaluation` | Eval cases, scorers and reports, off the request path | Nothing on the request path imports it |

## Working on the library

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --all-packages --all-extras   # create the environment and install everything
uv run ruff check .                   # lint
uv run ruff format --check .          # formatting
uv run mypy                           # strict type check
uv run lint-imports                   # module boundary rules
uv run pytest                         # all tests; network sockets are blocked
uv run python docs/guide/build_guide.py --check   # the developer guide matches the code
```

A change is ready when all six checks pass; CI runs them on Linux and Windows. Tests that need something outside
the process are opt-in: `uv run pytest -m integration`. With the `opa` binary
on `PATH`, that run starts a local OPA server and checks that it and the
in-process `rules` provider make the same decisions, then runs the reference
agent against the reference MCP server over HTTP. The Rego bundle has its own
checks, listed in [`policies/README.md`](policies/README.md).

When a configuration variable is added or renamed, change the binding table in
`ai_agent_lib_core/config/bindings.py` and regenerate the documents made from it:

```bash
uv run python -m ai_agent_lib_core.config reference > docs/variables.md
uv run python -m ai_agent_lib_core.config env-example > .env.example
uv run python docs/guide/build_guide.py
```

## Testing an adapter or an agent

Every port has a contract test suite in `ai_agent_lib_core.testing.contracts`.
An adapter is tested by subclassing the suite for its port; the fakes pass the
same suites. An agent is tested offline with the fakes:

```python
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes

fakes = Fakes(model=FakeChatModelProvider(["scripted answer"]))
async with fakes.container() as services:
    ...
assert fakes.audit.records[0].event == "model.call"
```

A service is also tested as it is configured: its own `.env.example`, the real
rules file, registries and CSV data, with only the model scripted and local
state kept in a temporary folder.

```python
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.testing import (
    audit_records,
    calls_tool,
    last_shown_to_model,
    load_test_config,
    scripted_providers,
)

config = load_test_config(SERVICE / ".env.example", state_dir=tmp_path, roles=["analyst"])
replies = scripted_providers(calls_tool("accounts.by_region", region="emea"), "answer")
async with ServiceContainer(config, replies) as services:
    ...
    assert "masked" in last_shown_to_model(services)  # the tool result, as the model saw it
assert audit_records(tmp_path)[0]["attributes"]["policy_reason_code"] == "agent-uses-its-models"
```

`roles` says who the caller without a token is, on the development identity.
`scripted_model(services)` gives the scripted model, with every prompt it was
sent.

`ai_agent_lib_core.testing.mcp` has `call_tool_as`, which calls a tool of an
in-process MCP server for a caller with given roles, and
`InProcessMcpConnector`, which lets an agent reach a server with no network.

A service that reads a REST API is tested without the API.
`rest_stub_providers` keeps the real `rest` data source, with its endpoint
definitions, parameter checks, row cap and masks, and answers its requests
from a table in the test:

```python
from ai_agent_lib_core.testing import rest_stub_providers

responses = {"GET /v1/accounts/7": {"id": 7, "owner": {"name": "Ann"}}}
async with ServiceContainer(config, rest_stub_providers(responses)) as services:
    ...
```

A relative path in a `.env` file is relative to that file, so a service finds
its files whatever folder it is started from.
