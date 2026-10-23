"""Local adapters that implement the ports without any cloud service."""

from agentcraft2_core.adapters.audit_jsonl import JsonlAuditOptions, JsonlAuditSink
from agentcraft2_core.adapters.checkpoint_none import NoCheckpointBackend
from agentcraft2_core.adapters.data_duckdb import DuckDbCsvDataSource, DuckDbCsvOptions
from agentcraft2_core.adapters.data_rest import RestDataSource, RestOptions
from agentcraft2_core.adapters.guardrails_patterns import (
    GuardrailsOptions,
    PatternGuardrails,
    PatternGuardrailsOptions,
)
from agentcraft2_core.adapters.identity_jwt import (
    ExchangingJwtIdentity,
    JwtIdentityOptions,
    JwtIdentityVerifier,
    JwtSettings,
    TokenExchangeOptions,
    resolve_jwt_options,
)
from agentcraft2_core.adapters.identity_static import StaticIdentityOptions, StaticIdentityVerifier
from agentcraft2_core.adapters.model_anthropic import (
    AnthropicChatModelProvider,
    AnthropicModelOptions,
)
from agentcraft2_core.adapters.model_fake import (
    FakeChatModel,
    FakeChatModelProvider,
    ScriptedResponse,
)
from agentcraft2_core.adapters.policy_cache import CachingPolicyDecisionPoint, PolicyCacheOptions
from agentcraft2_core.adapters.policy_opa import OpaPolicyDecisionPoint, OpaPolicyOptions
from agentcraft2_core.adapters.policy_rules import RulesPolicyDecisionPoint, RulesPolicyOptions
from agentcraft2_core.adapters.registry_file import FileRegistryOptions, FileRegistrySource
from agentcraft2_core.adapters.secrets_env import EnvSecretsProvider
from agentcraft2_core.adapters.system import SystemClock, UuidGenerator
from agentcraft2_core.adapters.telemetry import NullTelemetry, OpenTelemetryTelemetry
from agentcraft2_core.adapters.token_exchange import OAuthTokenExchanger

__all__ = [
    "AnthropicChatModelProvider",
    "AnthropicModelOptions",
    "CachingPolicyDecisionPoint",
    "DuckDbCsvDataSource",
    "DuckDbCsvOptions",
    "EnvSecretsProvider",
    "ExchangingJwtIdentity",
    "FakeChatModel",
    "FakeChatModelProvider",
    "FileRegistryOptions",
    "FileRegistrySource",
    "GuardrailsOptions",
    "JsonlAuditOptions",
    "JsonlAuditSink",
    "JwtIdentityOptions",
    "JwtIdentityVerifier",
    "JwtSettings",
    "NoCheckpointBackend",
    "NullTelemetry",
    "OAuthTokenExchanger",
    "OpaPolicyDecisionPoint",
    "OpaPolicyOptions",
    "OpenTelemetryTelemetry",
    "PatternGuardrails",
    "PatternGuardrailsOptions",
    "PolicyCacheOptions",
    "RestDataSource",
    "RestOptions",
    "RulesPolicyDecisionPoint",
    "RulesPolicyOptions",
    "ScriptedResponse",
    "StaticIdentityOptions",
    "StaticIdentityVerifier",
    "SystemClock",
    "TokenExchangeOptions",
    "UuidGenerator",
    "resolve_jwt_options",
]
