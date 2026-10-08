"""Fakes and contract test suites, shared with adapters and application teams.

The contract suites live in :mod:`ai_agent_lib_core.testing.contracts`, which
needs ``pytest``; everything exported here works without it.
"""

from ai_agent_lib_core.adapters import FakeChatModel, FakeChatModelProvider
from ai_agent_lib_core.testing.datasets import (
    ACCOUNT_COLUMNS,
    ACCOUNT_ENDPOINTS,
    ACCOUNT_QUERIES,
    ACCOUNT_ROWS,
    accounts_api,
    fake_accounts_source,
    write_accounts_endpoints,
    write_accounts_files,
)
from ai_agent_lib_core.testing.fakes import (
    FakeDataSource,
    FakeGuardrails,
    FakeIdentityVerifier,
    FakePolicyDecisionPoint,
    FakeQuery,
    FakeRegistry,
    FakeSecretsProvider,
    FrozenClock,
    GuardrailAnswer,
    InMemoryAuditSink,
    PolicyAnswer,
    RecordingTelemetry,
    SequentialIds,
)
from ai_agent_lib_core.testing.harness import FAKE_PROVIDER, Fakes, fake_providers
from ai_agent_lib_core.testing.local import (
    TEST_AGENT,
    audit_records,
    load_test_config,
    scripted_providers,
)

__all__ = [
    "ACCOUNT_COLUMNS",
    "ACCOUNT_ENDPOINTS",
    "ACCOUNT_QUERIES",
    "ACCOUNT_ROWS",
    "FAKE_PROVIDER",
    "TEST_AGENT",
    "FakeChatModel",
    "FakeChatModelProvider",
    "FakeDataSource",
    "FakeGuardrails",
    "FakeIdentityVerifier",
    "FakePolicyDecisionPoint",
    "FakeQuery",
    "FakeRegistry",
    "FakeSecretsProvider",
    "Fakes",
    "FrozenClock",
    "GuardrailAnswer",
    "InMemoryAuditSink",
    "PolicyAnswer",
    "RecordingTelemetry",
    "SequentialIds",
    "accounts_api",
    "audit_records",
    "fake_accounts_source",
    "fake_providers",
    "load_test_config",
    "scripted_providers",
    "write_accounts_endpoints",
    "write_accounts_files",
]
