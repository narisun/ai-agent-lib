# accounts-agent

The reference agent for `ai-agent-lib`. It is an ordinary LangGraph
tool-calling agent: one model node, one tool node and the standard routing
between them. The library supplies the governed model, the governed tool, the
scoped checkpointer and the per-request run arguments.

## Run it offline

No account or key is needed. The `fake` model echoes the question.

```bash
cd examples/accounts-agent
EAP_MODEL_PROVIDER=fake EAP_MODEL_ID=fake-model accounts-agent "What is the balance of account 4411?"
```

Local state is written under `.agentlib/`: the audit log as JSON lines and the
conversation checkpoints in SQLite.

## Run it on the Anthropic API

```bash
python -m pip install -e "../../packages/ai-agent-lib-core[anthropic]"
ANTHROPIC_API_KEY=... EAP_MODEL_ID=<model id> accounts-agent "What is the balance of account 4411?"
```

## Serve it over HTTP

```bash
EAP_MODEL_PROVIDER=fake EAP_MODEL_ID=fake-model accounts-agent-serve --port 8000
curl -s localhost:8000/readyz
curl -s localhost:8000/invoke -H 'content-type: application/json' \
  -d '{"input": {"question": "What is the balance of account 4411?"}, "thread_id": "demo"}'
curl -sN localhost:8000/invoke/stream -H 'content-type: application/json' \
  -d '{"input": {"question": "What is the balance of account 4411?"}}'   # each step as it ends
```

On a developer's machine the static identity stands in, so no token is
needed. With an identity provider configured, `/invoke` needs a bearer token
and answers 401 without a valid one. Stop the service with Ctrl-C, or with
SIGTERM to see it drain: it reports not ready, finishes the requests it has
and exits with code 0.

## Use the tools of the reference MCP server

The agent also uses every MCP server the agent registry lists for it. See
`examples/accounts-mcp/README.md` for running the two together.

## What to read

| File | Shows |
| --- | --- |
| `src/accounts_agent/graph.py` | The five places the library appears in a graph |
| `src/accounts_agent/__main__.py` | Building the container and the request context |
| `src/accounts_agent/service.py` | Serving the agent over HTTP: one graph, a caller per request, streamed steps, an orderly stop |
| `tests/test_accounts_agent.py` | Testing an agent offline with the library's fakes |
| `tests/test_accounts_agent_service.py` | Testing the HTTP entry point offline, streaming, and a stop with a request in flight |
