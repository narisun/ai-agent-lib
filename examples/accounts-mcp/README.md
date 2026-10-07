# accounts-mcp

The reference MCP server for `ai-agent-lib`. It is an ordinary MCP server: two
tools written as plain functions. The library supplies the keyword arguments
that govern the server (the check of the caller's token at the door and the
middleware around every tool call) and the governed data source the tools read
from.

In local development the data is `data/accounts.csv`, queried by DuckDB with
the same SQL files a production deployment runs against Amazon Redshift.

## Run it

```bash
cd examples/accounts-mcp
cp .env.example .env
uv run accounts-mcp            # serves http://127.0.0.1:8001/mcp, with /healthz and /readyz
```

No model, key or policy server is needed. Local state is written under
`.agentlib/`: the audit log as JSON lines. The server compiles no graph, so its
configuration selects no checkpoint store (`EAP_CHECKPOINT_PROVIDER=none`).

## Call it from the reference agent

In a second terminal, point the agent at the same registry and give the local
caller a role:

```bash
cd examples/accounts-agent
export EAP_REGISTRY_OPTIONS='{"agents_path": "../accounts-mcp/registry/agents.yaml", "tools_path": "../accounts-mcp/registry/mcp-tools.yaml"}'
export EAP_IDENTITY_OPTIONS='{"roles": ["analyst"]}'
ANTHROPIC_API_KEY=... EAP_MODEL_ID=<model id> uv run accounts-agent "Which accounts are in the west region?"
```

The agent exchanges the caller's identity for a short-lived development token
bound to `accounts-mcp`; the server rebuilds the caller from it. What each role
gets is decided by `policies/agentlib/rules/data.yaml`:

| Role | Tool call | Data |
| --- | --- | --- |
| `manager` | allowed | every column |
| `analyst` | allowed | `holder` is masked, at most 50 rows |
| anything else | denied | none |

The tool-call rule also names the agent: a call that does not come through
`accounts-agent` is denied, whatever the caller's role.

Each side writes its own audit log, joined by the request ID. Every record of a
decision carries the policy decision ID and the revision of the rules.

## Use Microsoft Entra ID instead of the development identity

`.env.entra.example` here and in `examples/accounts-agent` show the settings.
No code changes: the agent verifies the signed-in user's token and exchanges it
for one bound to this server, and this server verifies that. The whole path is
described in [`docs/identity.md`](../../docs/identity.md).

Locally, the development identity is the default and needs nothing. It cannot
be used when `EAP_DEPLOYMENT_ENV` is `dev` or `prod`.

## Decide with OPA instead of the in-process rules

```bash
opa run --server --addr 127.0.0.1:8181 -b ../../policies/bundle policies
EAP_POLICY_PROVIDER=opa uv run accounts-mcp
```

OPA evaluates the same `policies/agentlib/rules/data.yaml`, so the outcome is
the same.

## What to read

| File | Shows |
| --- | --- |
| `src/accounts_mcp/server.py` | The two places the library appears in a server |
| `src/accounts_mcp/__main__.py` | Building the container and checking the registry at startup |
| `queries/*.sql` | Named queries: declared parameters, a row cap and a classification |
| `policies/agentlib/rules/data.yaml` | The rules, one document for the `rules` provider and for OPA |
| `registry/*.yaml` | The agent and tool registries, with schema pins |
| `tests/` | Testing a server and an agent together, in process |

The pins in `registry/mcp-tools.yaml` are fingerprints of each tool's input
schema. Changing a tool's parameters changes its pin, and so can an upgrade of
the MCP SDK that changes how schemas are generated; `tests/` fails when the
pins and the code disagree.
