"""Local adapters that implement the ports without any cloud service."""

from ai_agent_lib_core.adapters.audit_jsonl import JsonlAuditOptions, JsonlAuditSink
from ai_agent_lib_core.adapters.checkpoint_none import NoCheckpointBackend
from ai_agent_lib_core.adapters.data_duckdb import DuckDbCsvDataSource, DuckDbCsvOptions
from ai_agent_lib_core.adapters.data_rest import RestDataSource, RestOptions
from ai_agent_lib_core.adapters.guardrails_patterns import (
    GuardrailsOptions,
    PatternGuardrails,
    PatternGuardrailsOptions,
)
from ai_agent_lib_core.adapters.identity_jwt import (
    ExchangingJwtIdentity,
    JwtIdentityOptions,
    JwtIdentityVerifier,
    JwtSettings,
    TokenExchangeOptions,
    resolve_jwt_options,
)
from ai_agent_lib_core.adapters.identity_static import StaticIdentityOptions, StaticIdentityVerifier
from ai_agent_lib_core.adapters.model_anthropic import AnthropicChatModelProvider
from ai_agent_lib_core.adapters.model_fake import (
    FakeChatModel,
    FakeChatModelProvider,
    ScriptedResponse,
)
from ai_agent_lib_core.adapters.policy_cache import CachingPolicyDecisionPoint, PolicyCacheOptions
from ai_agent_lib_core.adapters.policy_opa import OpaPolicyDecisionPoint, OpaPolicyOptions
from ai_agent_lib_core.adapters.policy_rules import RulesPolicyDecisionPoint, RulesPolicyOptions
from ai_agent_lib_core.adapters.registry_file import FileRegistryOptions, FileRegistrySource
from ai_agent_lib_core.adapters.secrets_env import EnvSecretsProvider
from ai_agent_lib_core.adapters.system import SystemClock, UuidGenerator
from ai_agent_lib_core.adapters.telemetry import NullTelemetry, OpenTelemetryTelemetry
from ai_agent_lib_core.adapters.token_exchange import OAuthTokenExchanger

__all__ = [
    "AnthropicChatModelProvider",
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
