# Identity: who is calling, and through which agent

Authority in the platform comes from one place: a token signed by the identity
provider. The library verifies it at every hop and hands the result to policy.
Nothing in a prompt, a tool result or a request body can add a role.

The enterprise identity provider is Microsoft Entra ID. The library talks to it
through OAuth 2.0 standards and keeps everything Entra-specific in options and
one small adapter, so a change of provider is a change of configuration.

## The path of one request

```text
user ──signs in (SSO)──▶ chat UI ──token A──▶ agent ──token B──▶ MCP server ──▶ OPA
                                  aud = agent        aud = MCP server
                                                     azp = the agent's client ID
```

1. **Sign-in.** The chat UI signs the user in and obtains token A for the agent.
2. **The agent verifies token A.** `services.authenticate(token, ...)` checks the
   signature against the tenant's published keys, then the issuer, the audience
   and the validity period, and returns the request context.
3. **The agent exchanges it.** Before a call to an MCP server, the agent trades
   token A for token B, which only that server accepts. The agent never forwards
   token A. Token B still names the user and their roles, and it names the agent
   as the application it was issued to.
4. **The MCP server checks the token at its door.** Every HTTP request needs a
   valid token for this server: signature, issuer, audience, validity period and
   tenant. Outside local development the token must also have been issued to a
   registered, enabled agent that is listed for this server. Anything else is
   answered with HTTP 401 and the standard pointer to the server's protected
   resource metadata (RFC 9728), whatever the request asked for.
5. **The MCP server verifies token B for the tool call**, and reads from it the
   user (`oid`), the tenant (`tid`), the roles (`roles`, the app roles the user
   holds at this server) and the agent (`azp`). The registry check is repeated
   here, with the agent's classification ceiling.
6. **OPA decides.** The policy receives the whole authenticated context. Anything
   other than a well-formed allow is a deny.

An agent also talks to an MCP server before any user is involved: at startup it
asks which tools the server has. For that it presents a token of its own, from
the client credentials grant. The door accepts it, because it is a valid token
from a listed agent. A tool call with it is refused, because it names no user,
unless the server sets `accept_service_tokens`.

### Transport

Outside local development an agent uses an MCP server only if the tool registry
gives it an `https` address. A token is never sent over plain `http`, except to
this machine. The server gets these three keyword arguments from one call:

```python
server = MCPServer("accounts-mcp", **services.mcp_server_kwargs("accounts"))
```

## What each identity is

| Who | Entra object | Where it appears |
| --- | --- | --- |
| The user | A user in the tenant | `oid` in every token; `subject` in policy and audit |
| The user's role | An app role, assigned on each application | `roles` in the token for that application |
| An agent (non-human) | An app registration with a client secret | `azp` in the tokens it obtains; `client_id` in the agent registry |
| An MCP server | An app registration that exposes an API | `aud` of the tokens it accepts; `audience` in the tool registry |

## Configuration

An MCP server only verifies:

```bash
AC2_IDENTITY_PROVIDER=jwt
AC2_IDENTITY_OPTIONS={"preset": "entra", "tenant_id": "<tenant-guid>", "audience": ["<mcp-client-id>", "api://<mcp-client-id>"]}
```

An agent verifies and exchanges. `client_secret` is the name of a secret, read
through the secrets port:

```bash
AC2_IDENTITY_PROVIDER=jwt
AC2_IDENTITY_OPTIONS={"preset": "entra", "tenant_id": "<tenant-guid>", "audience": ["<agent-client-id>", "api://<agent-client-id>"], "exchange": {"client_id": "<agent-client-id>", "client_secret": "entra_client_secret"}}
```

The registries connect the two. The agent registry gives each agent its client
ID; the tool registry gives each server the audience a token for it must carry:

```yaml
# agents.yaml
- id: accounts-agent
  client_id: 22222222-2222-2222-2222-222222222222
  mcp_servers: [accounts]

# mcp-tools.yaml
- id: accounts
  audience: api://33333333-3333-3333-3333-333333333333
```

Further options of the `jwt` provider:

| Option | Default | Purpose |
| --- | --- | --- |
| `token_version` | `2` | The access token version the application is registered to receive |
| `authority` | `https://login.microsoftonline.com` | The sign-in host, for national clouds |
| `required_scopes` | none | Delegated scopes a user's token must all carry, for example `access_as_user` |
| `accept_service_tokens` | `false` | Accept a token an application obtained for itself, with no user |
| `claims` | from the preset | Claim names that differ, by what they supply: `subject`, `tenant`, `roles`, `actor`, `scopes`, `kind` |
| `algorithms` | `["RS256"]` | Accepted signature algorithms. Symmetric and unsigned ones cannot be configured. |
| `leeway_seconds` | `60` | Clock difference tolerated when checking validity |
| `jwks_cache_seconds` | `86400` | How long signing keys are kept before they are read again |
| `ca_file`, `use_proxy` | none | For reaching the identity provider from inside the network |

## What to set up in Entra

For each **MCP server**: an app registration that exposes an API (an application
ID URI and one delegated scope such as `access_as_user`), with the app roles the
policy uses (`analyst`, `manager`, ...) assigned to users or groups, and
`requestedAccessTokenVersion` set to `2` in the manifest.

For each **agent**: an app registration that exposes an API in the same way (the
chat UI requests token A for it), a client secret, and a delegated permission to
each MCP server's scope with admin consent, so that the exchange needs no
prompt. The agent also requests a token of its own for each MCP server
(`<server application ID URI>/.default`, client credentials) to list its tools;
no application permission is needed for that unless the tenant requires
assignment.

Recommended: add the optional claim `idtyp` to access tokens. It marks a token
an application obtained for itself. Without it the library treats a token with
no delegated scope as an application's own.

## What OPA receives

```json
{
  "schema": "agentcraft2.decision/v1",
  "principal": {
    "subject": "<user object ID>",
    "tenant": "<tenant ID>",
    "roles": ["analyst"],
    "kind": "user",
    "actors": ["accounts-agent"]
  },
  "action": "tool.call",
  "resource": {"kind": "tool", "name": "accounts/accounts.by_region", "classification": "restricted", "read_only": true, "server": "accounts"},
  "context": {"application": "accounts-mcp", "environment": "prod"}
}
```

`actors` lists the applications acting for the user, the one that presented the
request last. A registered agent appears under its registry ID. A rule can
require one:

```yaml
- id: staff-call-account-tools
  actions: [tool.call]
  roles: [analyst, manager]
  agents: [accounts-agent]     # the agent that presented the request
  kinds: [user]                # a person, not an application acting for itself
```

A timeout, a transport error, a malformed reply or a missing `allow` from OPA is
a deny, and so is an obligation the library does not recognise.

## Local development without Entra

Nothing needs to be switched off. With no configuration the identity provider is
`static`: a fixed development principal, set in `AC2_IDENTITY_OPTIONS`:

```bash
AC2_IDENTITY_OPTIONS={"subject": "dev-user", "tenant": "dev-tenant", "roles": ["analyst"]}
```

With the `static` provider an MCP server has no check at the door, so a
developer can call a tool with no token at all; the tool call itself is still
governed. A local agent and a local MCP server still exercise the real path. The agent
exchanges the development principal for a short-lived signed development token,
and the server rebuilds the caller from it, so roles, the agent check and policy
behave as they do with Entra.

The `static` provider is local-only. A service whose `AC2_DEPLOYMENT_ENV` is
`dev` or `prod` refuses to start with it. A service that forgot to set
`AC2_DEPLOYMENT_ENV` would default to `local`, so a process on ECS, Fargate or
Lambda, which AWS marks with `AWS_EXECUTION_ENV`, refuses to start until it
names its environment as `dev` or `prod`. On other compute, the deployment
must set `AC2_DEPLOYMENT_ENV` itself.

To test against real tokens without Entra, `ai_agent_lib_core.testing.oauth`
has a stand-in identity provider that signs tokens, publishes its keys and
answers the token endpoint.

## Another identity provider

Leave out the preset and give the standard settings directly:

```json
{
  "issuer": "https://idp.example.com/realms/bank",
  "jwks_url": "https://idp.example.com/realms/bank/protocol/openid-connect/certs",
  "audience": "accounts-mcp",
  "tenant": "bank",
  "claims": {"roles": "realm_roles", "actor": "azp"},
  "service_tokens_are": "without_scopes",
  "exchange": {"kind": "token_exchange", "client_id": "accounts-agent", "token_url": "https://idp.example.com/realms/bank/protocol/openid-connect/token"}
}
```

`service_tokens_are` is required without a preset. It says how a token an
application obtained for itself (client credentials) is told from a user's,
which issuers do differently:

| Rule | An application's own token is one that | Choose it when |
| --- | --- | --- |
| `kind_claim` | carries a value of `service_kinds` (default `["app"]`) in the `kind` claim; a token without the claim is refused | the issuer marks the kind of token in a claim |
| `without_scopes` | carries no delegated scope claim | user tokens always carry `scope` and application tokens never do |
| `actor_is_subject` | has the same `actor` (`azp`) and subject; a token without both, as text, is refused | the issuer sets `sub` to the client ID for client credentials |

A wrong rule would let an application act as a user, so there is no default.
The Entra preset brings its own rule.

`token_exchange` is OAuth 2.0 Token Exchange, RFC 8693. Entra does not offer
that grant for this purpose; its delegation uses the JWT bearer grant of RFC
7523 with one Microsoft parameter, which the library calls `on_behalf_of`. That
difference is the whole of the provider-specific code.

## On AWS Bedrock AgentCore

An AgentCore runtime authenticates the caller before a request reaches the
container, in one of two modes. Which mode a runtime uses decides which
identity provider the service behind it selects.

### JWT mode, the recommended one

The runtime checks the bearer token against a JWT authorizer and, when told
to, passes the token on. The service then verifies it again with the `jwt`
provider, exactly as it would anywhere else. That second check is the point:
the runtime's authorizer is the perimeter, the library's verifier is what the
principal, the roles and the audit record are built from, and the two must
agree. The authorizer's settings map onto `JwtIdentityOptions` one to one:

| Runtime authorizer | `jwt` option | Note |
| --- | --- | --- |
| `discoveryUrl` | `issuer` | The issuer the discovery document names must be the `issuer` the preset or the options give |
| `allowedAudience`, `allowedClients` | `audience` | The same value or values |
| `requestHeaderAllowlist` | must include `Authorization` | Without it no token reaches the container, and every request is refused with `credential_missing` |

The AgentCore quick starts use Amazon Cognito, so the `jwt` provider has a
`cognito` preset beside `entra`:

```bash
AC2_IDENTITY_PROVIDER=jwt
AC2_IDENTITY_OPTIONS={"preset": "cognito", "user_pool_id": "<region>_<pool id>", "audience": "<app client id>"}
```

The preset fills in the user pool's issuer and key location from
`user_pool_id`; `audience` is the app client ID the runtime's `allowedClients`
names. Everything else, roles included, follows the same rules as any other
issuer.

### IAM (SigV4) mode

With IAM authentication the caller signs the request with AWS credentials and
no bearer token exists. The runtime passes the caller on as an opaque user ID
in the header `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`, and nothing else
about them. The AWS pack's `agentcore_runtime` provider trusts that perimeter:

```bash
AC2_IDENTITY_PROVIDER=agentcore_runtime
AC2_IDENTITY_OPTIONS={"tenant": "<tenant>", "roles": ["analyst"]}
```

The principal is the user ID as `subject`, the configured `tenant`, because
the runtime does not say which tenant a caller is in, and the configured
`roles`, because it passes none. Its `authenticated_by` is
`agentcore-runtime` rather than an issuer, so a policy can tell a
perimeter-trusted identity from a verified token and, for instance, keep the
most sensitive tools to the latter. A request without the header, and a bare
bearer token with no headers, is refused with `perimeter_identity_missing`; a
user ID that is not a usable identifier with `credential_invalid`. An MCP
server on such a runtime admits a request with no token at its door when its
identity trusts the perimeter and the runtime's headers are present.

The provider cannot be selected in local development: there is no runtime to
trust on a laptop, so a service whose deployment environment is `local`
refuses to start with it. It is the one adapter with the opposite rule to
`static`.

The header is only as trustworthy as the callers allowed to set it. A caller
with `bedrock-agentcore:InvokeAgentRuntimeForUser` names the user ID itself.
Follow the AWS guidance: deny that action on every runtime that does not need
user delegation, and allow it on the rest only to the applications that act
for signed-in users, such as the chat UI's own role.

### Outbound: tokens for the services an agent calls

On AgentCore the runtime, not the agent, holds the client secrets of the
downstream identity providers, in AgentCore Identity *credential providers*.
The runtime hands the agent a workload access token in a header
(`X-Amz-Bedrock-AgentCore-Identity-WAT`); the agent exchanges it through
AgentCore Identity for a token the MCP server accepts. The `agentcore_runtime`
provider does this when it has `exchange` options:

```bash
AC2_IDENTITY_OPTIONS={"tenant": "<tenant>", "exchange": {"credential_provider": "entra-provider", "scopes": ["{audience}/.default"], "workload_name": "accounts-agent"}}
```

`credential_provider` is the name of the credential provider in AgentCore
Identity; `scopes` are asked for with `{audience}` replaced by the audience the
tool registry gives the server; `workload_name` is the agent's own workload
identity, used to mint a workload token when a request brought none and for
the agent's own token. The flow follows the caller: `USER_FEDERATION` for a
person, so the downstream token names them, and `M2M` for the service acting
in its own name, for example when it lists a server's tools at startup. As
everywhere in the library, the caller's own credential is used only to obtain
the downstream token and is never forwarded. Tokens are reused for
`cache_seconds` per caller and audience.

User federation needs the person to have authorised the agent for the
downstream service once, through the credential provider's consent flow. Until
they have, AgentCore answers with a URL for their browser instead of a token,
and the call is refused with `user_consent_required`. The URL is for the
person, not for a log or an error message; the application decides how to show
it. `return_url` says where the browser goes afterwards.

The permissions this needs, `bedrock-agentcore:GetResourceOauth2Token`,
`GetWorkloadAccessToken` and `GetWorkloadAccessTokenForUserId` on the
workload identity directory and the agent's workload identity, are declared by
the adapter like every other AWS permission and appear in `agentcraft2 deploy
SERVICE --plan`. A JWT-mode runtime that exchanges tokens directly with the
identity provider keeps using the `jwt` provider's `exchange` options and
needs none of them.

## Why a call was refused

The reason code is in the audit record and, for a tool call, in the refusal the
caller receives. A token an agent refuses at the door, in
`services.authenticate`, leaves a `request.authenticate` record that names no
caller. Every other record says what kind of caller it was (`principal_kind`)
and which applications presented the request (`actors`).

| Reason code | Meaning |
| --- | --- |
| `credential_missing` | No token was presented |
| `credential_invalid` | Malformed, not signed by the issuer, not yet valid, or missing a needed claim |
| `credential_expired` | Past its validity period |
| `credential_audience` | Issued for another service, for example a forwarded sign-in token |
| `credential_issuer`, `credential_tenant` | From another issuer or tenant |
| `scope_missing` | A required delegated scope is absent |
| `service_token_refused` | An application's own token, where those are not accepted |
| `identity_unavailable` | The signing keys could not be read and none were held |
| `token_exchange_refused` | The identity provider refused to issue the downstream token |
| `perimeter_identity_missing` | On an AgentCore runtime in IAM mode: the request carries no caller identity from the runtime, or only a bearer token |
| `user_consent_required` | The user has not yet authorised this service for the downstream one through AgentCore Identity |
| `server_refused_token` | An MCP server answered 401 or 403 at its door: the token is not valid there, or the agent is not listed for it |
| `agent_unidentified` | The token names no agent, on a deployed server |
| `agent_unregistered`, `agent_disabled`, `server_not_allowed` | The agent fails the registry check |
| `no_matching_rule` | Policy: no rule covers the request |
| `policy_unavailable`, `policy_malformed` | Policy could not give a clear answer |
