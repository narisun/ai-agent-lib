# ai-agent-lib

Shared, governed plumbing for LangGraph agents and MCP servers on the
Enterprise Agentic Platform (EAP). Teams write graph and tool logic; the
library supplies configuration, identity, audit and the other cross-cutting
concerns behind small interfaces, so any of them can be swapped by
configuration and faked in a unit test.

## What a graph author writes

```python
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ai_agent_lib_core import RequestContext, ServiceContainer


async def answer(context: RequestContext, inputs: dict) -> dict:
    async with ServiceContainer.from_env() as services:
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

        graph = builder.compile(**services.compile_kwargs())
        return await graph.ainvoke(inputs, **services.invocation(context))
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
            result = await graph.ainvoke(given, **services.invocation(context))
            return {"answer": result["messages"][-1].content}

        lifecycle = ServiceLifecycle(services.validate)
        app = agent_app(services, run, application="accounts-agent", lifecycle=lifecycle)
        await serve(app, lifecycle, host="0.0.0.0", port=8000)
```

| Route | Answers |
| --- | --- |
| `POST /invoke` | Takes `{"input": ..., "thread_id": "..."}` and a bearer token. The identity provider decides who the caller is; `run` gets their request context. A refused token is 401, a policy denial 403, and a failure is reported by a code, never by the error's text |
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

The entry point answers with one JSON document. It does not stream.

## Layout

| Path | What it is |
| --- | --- |
| `packages/ai-agent-lib-core` | Contracts, configuration, container, pipelines, local adapters, LangGraph bindings and the testing kit |
| `packages/ai-agent-lib-aws` | AWS adapters, loaded automatically when the package is installed: Bedrock, Secrets Manager, Firehose, Redshift, S3, PostgreSQL and Bedrock guardrails. See its [README](packages/ai-agent-lib-aws/README.md) |
| `packages/ai-agent-lib-cli` | The `agentlib` developer tool. Empty for now |
| `examples/accounts-agent` | The reference agent |
| `examples/accounts-mcp` | The reference MCP server, with its queries, rules and registries |
| `policies/bundle` | The platform's Rego bundle for OPA, and its tests |
| `tests/architecture` | Rules that keep the module boundaries in place |
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
| `testing` | Fakes and the contract test suites | Everything |

## Working on the library

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --all-packages --all-extras   # create the environment and install everything
uv run ruff check .                   # lint
uv run ruff format --check .          # formatting
uv run mypy                           # strict type check
uv run lint-imports                   # module boundary rules
uv run pytest                         # all tests; network sockets are blocked
```

A change is ready when all five checks pass. Tests that need something outside
the process are opt-in: `uv run pytest -m integration`. With the `opa` binary
on `PATH`, that run starts a local OPA server and checks that it and the
in-process `rules` provider make the same decisions, then runs the reference
agent against the reference MCP server over HTTP. The Rego bundle has its own
checks, listed in [`policies/README.md`](policies/README.md).

When a configuration variable is added or renamed, change the binding table in
`ai_agent_lib_core/config/bindings.py` and regenerate the two documents:

```bash
uv run python -m ai_agent_lib_core.config reference > docs/variables.md
uv run python -m ai_agent_lib_core.config env-example > .env.example
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
