# 0004: AgentCore deployment target

## Context

The implementation plan of 22 October 2026 left six decisions open before an
agent or MCP server could be deployed to Amazon Bedrock AgentCore Runtime:
inbound identity, the checkpoint store, the artifact kind, gateway fronting,
the policy decision point and the Python version. Phases 3 and 4 settled them
in code.

## Decision

1. Inbound identity: JWT by default, verified by the service. SigV4 is
   supported through `identity=agentcore_runtime`, which yields a
   perimeter-trusted principal marked `authenticated_by: agentcore-runtime`.
2. Checkpoints: AgentCore Memory is implemented (`checkpoint=agentcore_memory`;
   actor and session are derived from the scoped thread key). Postgres stays.
3. Artifact: container first, an ARM64 image started under
   `opentelemetry-instrument`. A code zip is supported
   (`AGENTCORE_ARTIFACT=code`, Python 3.13).
4. Gateway: opt-in (`AGENTCORE_GATEWAY=create|<arn>`). Terraform receives the
   MCP targets; the runtime target is left to the CLI because the provider
   lacks it.
5. Policy: the plan recommended in-process `rules` with Cedar at the gateway,
   but `rules` is still `local_only`, so a deployed service uses a remote OPA
   URL (a loopback OPA is refused). Lifting `local_only` from `rules` is a
   separate decision still to take.
6. Python: generated AgentCore services are pinned to 3.13.

## Consequences

One image serves both contracts, `/invoke` and `/invocations`. Telemetry is
`ambient` on AgentCore: ADOT owns the providers. Operators grant by hand what
the plan cannot derive: `bedrock-agentcore:InvokeAgentRuntime` for `sigv4` MCP
servers in the registry, and `logs:PutResourcePolicy`. The Memory saver builds
its own boto3 client, so a named AWS profile is not forwarded to it.

## Owner

Library maintainers; included in the Phase 5 pilot review.
