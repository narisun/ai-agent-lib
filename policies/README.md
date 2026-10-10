# Policies

`bundle/` is the platform's Rego bundle. It holds one small, fixed evaluator,
`data.agentlib.authz.decision`, and its tests. It does not say what is
allowed; it evaluates a rules document that each service supplies as data at
`data.agentlib.rules`.

## One rules document, two engines

A service keeps its rules in a folder laid out as an OPA bundle:

```text
policies/
  .manifest                  {"revision": "...", "roots": ["agentlib/rules"]}
  agentlib/rules/data.yaml   schema: agentlib.rules/v1
```

* In local development the in-process `rules` policy provider reads
  `policies/agentlib/rules/data.yaml`. No policy server is needed.
* Everywhere else the service asks OPA, which loads both bundles:

  ```bash
  opa run --server --addr 127.0.0.1:8181 -b policies/bundle path/to/service/policies
  ```

Both engines evaluate the same document the same way: the first rule that
covers a request allows it and supplies its obligations, and a request that no
rule covers is denied. The contract suite `PolicyDecisionPointContract` in
`ai_agent_lib_core.testing.contracts` runs against both.

Both also check the document as strictly. The in-process engine refuses to
start with a document that has an unknown key, a wrong type, an unknown action
or level, or two rules with one ID. OPA cannot refuse to start, so the bundle
answers every request with a deny, reason `invalid_rules`, until the document
is fixed. A misspelt key such as `role` for `roles` therefore never widens a
rule, and neither does a bad value in a rule no request uses.

`policies/testdata/rules_corpus.json` holds the cases both engines must judge
alike: each invalid rule is tried first and last, used and unused, beside a
valid grant. The unit tests run it against the in-process engine and the
integration tests against a real OPA, of the version the deployed sidecar runs.

## A rule

```yaml
- id: analysts-query-accounts        # the reason code of the decisions it allows
  actions: [data.query]              # required
  roles: [analyst]                   # the caller needs one of these; omit for any caller
  agents: [accounts-agent]           # the agent that presented the request, by registry ID; omit for any
  kinds: [user]                      # user or service; omit for either
  applications: [accounts-mcp]       # the asking service; omit for any
  resources: ["accounts.*"]          # `*` matches any text; omit for every resource
  max_classification: restricted     # omit for no limit
  obligations:
    row_filter: {region: [east, west]}
    mask_columns: [holder]
    max_rows: 50
```

`agents` is matched against the application the caller's token was issued to,
after the agent registry has turned its client ID into a registry ID.

Obligations only narrow what comes back. A row filter that names a column the
query does not return fails the call, so a condition is never dropped silently.
A mask applies to a returned column of that name: a query that returns the same
data under another name is not masked by it, so keep column names stable and
name every sensitive column in the rule.

## Checks

```bash
opa fmt --fail policies/bundle
opa check --strict policies/bundle
opa test policies/bundle
uv run pytest -m integration packages/ai-agent-lib-core/tests/integration/test_opa_live.py
```

The last command needs the `opa` binary on `PATH`. It starts a local OPA server
and runs the contract suite against it.
