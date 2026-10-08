"""Registration of the local adapters that ship with the core package."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, cast

from ai_agent_lib_core.adapters import (
    AnthropicChatModelProvider,
    CachingPolicyDecisionPoint,
    DuckDbCsvDataSource,
    DuckDbCsvOptions,
    EnvSecretsProvider,
    ExchangingJwtIdentity,
    FakeChatModelProvider,
    FileRegistryOptions,
    FileRegistrySource,
    JsonlAuditOptions,
    JsonlAuditSink,
    JwtIdentityOptions,
    JwtIdentityVerifier,
    NoCheckpointBackend,
    OAuthTokenExchanger,
    OpaPolicyDecisionPoint,
    OpaPolicyOptions,
    PatternGuardrails,
    PatternGuardrailsOptions,
    PolicyCacheOptions,
    RestDataSource,
    RestOptions,
    RulesPolicyDecisionPoint,
    RulesPolicyOptions,
    StaticIdentityOptions,
    StaticIdentityVerifier,
    resolve_jwt_options,
)
from ai_agent_lib_core.adapters.checkpoint_options import SqliteCheckpointOptions
from ai_agent_lib_core.adapters.http_support import tls_verification
from ai_agent_lib_core.config import secret_key, variable_for
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    NoOptions,
    PolicyDecisionPoint,
    SecretsProvider,
    Section,
    no_access,
)
from ai_agent_lib_core.di.providers import DATA_PORT, MODEL_PORT

if TYPE_CHECKING:
    from ai_agent_lib_core.contracts import CheckpointBackend
    from ai_agent_lib_core.di.providers import BuildContext, ServiceProviders

__all__ = ["register_local_adapters"]


def _jsonl_audit(context: BuildContext) -> JsonlAuditSink:
    return JsonlAuditSink(context.selection.parse_options(JsonlAuditOptions))


def _env_secrets(context: BuildContext) -> EnvSecretsProvider:
    def how_to_set(name: str) -> str:
        return f"set {variable_for(secret_key(name))} in the service's .env or its environment"

    return EnvSecretsProvider(context.secret_values, how_to_set)


def _static_identity(context: BuildContext) -> StaticIdentityVerifier:
    return StaticIdentityVerifier(
        context.selection.parse_options(StaticIdentityOptions), context.clock
    )


async def _jwt_identity(context: BuildContext) -> JwtIdentityVerifier:
    options = context.selection.parse_options(JwtIdentityOptions)
    settings = resolve_jwt_options(options)
    proxy = context.external.https_proxy if options.use_proxy else None
    if options.use_proxy and proxy is None:
        raise ConfigurationError(
            "the jwt identity provider: use_proxy is set but no outbound proxy is configured"
        )
    exchange = options.exchange
    if exchange is None or settings.exchange_kind is None or settings.token_url is None:
        return JwtIdentityVerifier(settings, context.clock, proxy=proxy)
    secrets = cast(SecretsProvider, context.get(Section.SECRETS))
    exchanger = OAuthTokenExchanger(
        kind=settings.exchange_kind,
        token_url=settings.token_url,
        client_id=exchange.client_id,
        client_secret=await secrets.get_secret(exchange.client_secret),
        scope=settings.exchange_scope,
        clock=context.clock,
        timeout_seconds=exchange.timeout_seconds,
        verify=tls_verification("the jwt identity provider", options.ca_file),
        proxy=proxy,
    )
    return ExchangingJwtIdentity(settings, context.clock, exchanger, proxy=proxy)


def _fake_model(context: BuildContext) -> FakeChatModelProvider:  # noqa: ARG001 - no options
    return FakeChatModelProvider()


async def _anthropic_model(context: BuildContext) -> AnthropicChatModelProvider:
    secrets = cast(SecretsProvider, context.get(Section.SECRETS))
    api_key = await secrets.get_secret("anthropic_api_key")
    # The model pipeline's resilience stage owns retries, so the vendor client makes
    # one attempt each: otherwise two pipeline retries become nine HTTP calls.
    return AnthropicChatModelProvider(api_key, proxy=context.external.https_proxy, max_retries=0)


async def _sqlite_checkpoint(context: BuildContext) -> CheckpointBackend:
    # Imported here so that code which never builds a graph does not load LangGraph.
    from ai_agent_lib_core.integrations.langgraph import (
        SqliteCheckpointBackend,
    )

    backend = SqliteCheckpointBackend(context.selection.parse_options(SqliteCheckpointOptions))
    await backend.start()
    return backend


def _no_checkpoint(context: BuildContext) -> NoCheckpointBackend:  # noqa: ARG001 - no options
    return NoCheckpointBackend()


def _cached(
    policy: PolicyDecisionPoint, options: PolicyCacheOptions, context: BuildContext
) -> PolicyDecisionPoint:
    if options.cache_ttl_seconds <= 0:
        return policy
    return CachingPolicyDecisionPoint(policy, context.clock, options)


def _rules_policy(context: BuildContext) -> PolicyDecisionPoint:
    options = context.selection.parse_options(RulesPolicyOptions)
    return _cached(RulesPolicyDecisionPoint(options, context.ids), options, context)


async def _opa_policy(context: BuildContext) -> PolicyDecisionPoint:
    options = context.selection.parse_options(OpaPolicyOptions)
    token = None
    if options.auth_secret is not None:
        secrets = cast(SecretsProvider, context.get(Section.SECRETS))
        token = await secrets.get_secret(options.auth_secret)
    return _cached(OpaPolicyDecisionPoint(options, context.ids, token=token), options, context)


def _pattern_guardrails(context: BuildContext) -> PatternGuardrails:
    return PatternGuardrails(context.selection.parse_options(PatternGuardrailsOptions))


def _file_registry(context: BuildContext) -> FileRegistrySource:
    return FileRegistrySource(context.selection.parse_options(FileRegistryOptions))


async def _duckdb_csv(context: BuildContext) -> DuckDbCsvDataSource:
    source = DuckDbCsvDataSource(
        context.instance, context.selection.parse_options(DuckDbCsvOptions), context.clock
    )
    await source.start()
    return source


async def _rest(context: BuildContext) -> RestDataSource:
    options = context.selection.parse_options(RestOptions)
    token = None
    if options.auth_secret is not None:
        secrets = cast(SecretsProvider, context.get(Section.SECRETS))
        token = await secrets.get_secret(options.auth_secret)
    source = RestDataSource(
        context.instance,
        options,
        context.clock,
        token=token,
        proxy=context.external.https_proxy,
    )
    await source.start()
    return source


def register_local_adapters(providers: ServiceProviders) -> None:
    """Register every local adapter that ships with core.

    None of them needs anything from a cloud account, so each says so.
    """
    register = functools.partial(providers.register, access=no_access)
    register(MODEL_PORT, "fake", _fake_model, local_only=True, options=NoOptions)
    register(MODEL_PORT, "anthropic", _anthropic_model, options=NoOptions, extra="anthropic")
    register(Section.SECRETS, "env", _env_secrets, options=NoOptions)
    register(Section.AUDIT, "jsonl", _jsonl_audit, local_only=True, options=JsonlAuditOptions)
    register(
        Section.IDENTITY, "static", _static_identity, local_only=True, options=StaticIdentityOptions
    )
    register(Section.IDENTITY, "jwt", _jwt_identity, options=JwtIdentityOptions, extra="jwt")
    register(
        Section.CHECKPOINT,
        "sqlite",
        _sqlite_checkpoint,
        local_only=True,
        options=SqliteCheckpointOptions,
    )
    register(Section.CHECKPOINT, "none", _no_checkpoint, options=NoOptions)
    register(Section.REGISTRY, "file", _file_registry, options=FileRegistryOptions)
    register(Section.POLICY, "rules", _rules_policy, local_only=True, options=RulesPolicyOptions)
    register(Section.POLICY, "opa", _opa_policy, options=OpaPolicyOptions)
    register(Section.GUARDRAILS, "patterns", _pattern_guardrails, options=PatternGuardrailsOptions)
    register(DATA_PORT, "duckdb_csv", _duckdb_csv, local_only=True, options=DuckDbCsvOptions)
    register(DATA_PORT, "rest", _rest, options=RestOptions)
