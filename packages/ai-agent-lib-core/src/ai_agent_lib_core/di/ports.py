"""Typed descriptors for built-in provider ports."""

from ai_agent_lib_core.contracts import (
    AuditSink,
    ChatModelProvider,
    CheckpointBackend,
    DataSource,
    GuardrailCheck,
    IdentityVerifier,
    PolicyDecisionPoint,
    RegistrySource,
    SecretsProvider,
)
from ai_agent_lib_core.di.providers import ServicePort

__all__ = [
    "AUDIT",
    "CHECKPOINT",
    "DATA",
    "GUARDRAILS",
    "IDENTITY",
    "MODEL",
    "POLICY",
    "REGISTRY",
    "SECRETS",
]

SECRETS = ServicePort[SecretsProvider]("secrets", ("get_secret",), ("get_secret",))
AUDIT = ServicePort[AuditSink]("audit", ("write",), ("write",))
IDENTITY = ServicePort[IdentityVerifier]("identity", ("verify",), ("verify",))
CHECKPOINT = ServicePort[CheckpointBackend]("checkpoint", ("checkpointer",))
REGISTRY = ServicePort[RegistrySource]("registry", ("agents", "tools"))
POLICY = ServicePort[PolicyDecisionPoint]("policy", ("decide",), ("decide",))
GUARDRAILS = ServicePort[GuardrailCheck]("guardrails", ("check",), ("check",))
MODEL = ServicePort[ChatModelProvider](
    "model", ("create", "capabilities", "classify_error"), ("create", "classify_error")
)
DATA = ServicePort[DataSource]("data", ("describe", "query"), ("describe", "query"))
