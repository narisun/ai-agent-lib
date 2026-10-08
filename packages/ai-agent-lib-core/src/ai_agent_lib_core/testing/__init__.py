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
    last_shown_to_model,
    load_eval_config,
    load_test_config,
    scripted_model,
    scripted_providers,
)
from ai_agent_lib_core.testing.replies import calls_tool, calls_tools, structured_reply
from ai_agent_lib_core.testing.rest import rest_stub_providers, rest_stub_transport

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
    "calls_tool",
    "calls_tools",
    "fake_accounts_source",
    "fake_providers",
    "last_shown_to_model",
    "load_eval_config",
    "load_test_config",
    "rest_stub_providers",
    "rest_stub_transport",
    "scripted_model",
    "scripted_providers",
    "structured_reply",
    "write_accounts_endpoints",
    "write_accounts_files",
]
