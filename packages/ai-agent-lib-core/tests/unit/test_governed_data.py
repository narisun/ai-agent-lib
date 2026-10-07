"""Governed data access: decided by policy, narrowed by obligations, always recorded."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from ai_agent_lib_core import bind_request_context
from ai_agent_lib_core.contracts import (
    MASK,
    AuditOutcome,
    Classification,
    IntegrityError,
    Obligations,
    PolicyDenied,
    Principal,
    ProviderSelection,
    RequestContext,
    RowFilter,
    Section,
    ServiceConfig,
    ValidationFailed,
)
from ai_agent_lib_core.di import DATA_PORT, ServiceContainer, ServiceProviders
from ai_agent_lib_core.testing import (
    FakeDataSource,
    FakePolicyDecisionPoint,
    Fakes,
    PolicyAnswer,
    fake_accounts_source,
    write_accounts_files,
)


def caller(**overrides: object) -> RequestContext:
    values: dict[str, object] = {
        "principal": Principal(subject="u-7", tenant="t-9", roles=frozenset({"analyst"})),
        "application": "accounts-mcp",
        "request_id": "r-1",
        "thread_id": "th-1",
        "classification_ceiling": Classification.RESTRICTED,
        **overrides,
    }
    return RequestContext(**values)  # type: ignore[arg-type]


def governed(answer: PolicyAnswer | None = None) -> tuple[Fakes, FakeDataSource]:
    source = fake_accounts_source()
    policy = FakePolicyDecisionPoint((lambda request: answer) if answer is not None else None)
    return Fakes(data_sources={"accounts": source}, policy=policy), source


async def test_an_allowed_query_runs_and_leaves_one_metadata_only_record() -> None:
    fakes, _ = governed()
    async with fakes.container() as services:
        result = await services.data_source("accounts").query(
            "accounts_by_region", {"region": "west", "min_balance": "1.5"}, context=caller()
        )
    assert [str(row[0]) for row in result.rows] == ["5520"]

    (asked,) = fakes.policy.requests
    assert asked.to_input() == {
        "schema": "agentlib.decision/v1",
        "principal": {
            "subject": "u-7",
            "tenant": "t-9",
            "roles": ["analyst"],
            "kind": "user",
            "actors": [],
        },
        "action": "data.query",
        "resource": {
            "kind": "query",
            "name": "accounts.accounts_by_region",
            "classification": "restricted",
            "source": "accounts",
            "query": "accounts_by_region",
        },
        "context": {"application": "accounts-mcp", "environment": "local"},
    }
    (record,) = fakes.audit.records
    assert record.event == "data.query"
    assert record.outcome is AuditOutcome.SUCCESS
    assert (record.tenant, record.subject, record.application) == ("t-9", "u-7", "accounts-mcp")
    assert dict(record.attributes) == {
        "data_source": "accounts",
        "query": "accounts_by_region",
        "classification": "restricted",
        "policy_decision_id": "decision-1",
        "policy_reason_code": "allowed",
        "policy_bundle_revision": "fake",
        "policy_cached": False,
        "row_filter_columns": "",
        "policy_max_rows": None,
        "rows": 1,
        "truncated": False,
        "masked_columns": "",
        "duration_ms": 0.0,
        "principal_kind": "user",
    }
    recorded = json.dumps(record.to_dict())
    assert "west" not in recorded
    assert "Eve" not in recorded
    assert "1.5" not in recorded
    assert fakes.telemetry.events[0] == (
        "data.query",
        {"outcome": "success", "data_source": "accounts", "query": "accounts_by_region"},
    )


async def test_the_policys_obligations_are_applied_inside_the_data_layer() -> None:
    obligations = Obligations(
        row_filters=(RowFilter("region", ("east",)),),
        mask_columns=frozenset({"holder"}),
        max_rows=2,
    )
    fakes, source = governed(obligations)
    async with fakes.container() as services:
        result = await services.data_source("accounts").query("all_accounts", context=caller())
    assert [str(row[0]) for row in result.rows] == ["4411", "4412"]
    assert {row[1] for row in result.rows} == {MASK}
    assert result.truncated
    assert source.calls[0][2] == obligations
    attributes = fakes.audit.records[0].attributes
    assert attributes["row_filter_columns"] == "region"
    assert attributes["policy_max_rows"] == 2
    assert attributes["masked_columns"] == "holder"
    assert (attributes["rows"], attributes["truncated"]) == (2, True)


async def test_a_denied_query_never_reaches_the_source_and_is_recorded() -> None:
    fakes, source = governed("no_matching_rule")
    async with fakes.container() as services:
        with pytest.raises(PolicyDenied) as caught:
            await services.data_source("accounts").query("all_accounts", context=caller())
    assert caught.value.reason_code == "no_matching_rule"
    assert source.calls == []
    (record,) = fakes.audit.records
    assert record.outcome is AuditOutcome.DENIED
    assert record.attributes["reason_code"] == "no_matching_rule"
    assert record.attributes["policy_decision_id"] == "decision-1"
    assert "rows" not in record.attributes


async def test_the_caller_cannot_supply_or_relax_obligations() -> None:
    fakes, _ = governed(Obligations(mask_columns=frozenset({"holder"})))
    async with fakes.container() as services:
        source = services.data_source("accounts")
        with pytest.raises(TypeError):
            await source.query("all_accounts", context=caller(), obligations=Obligations())  # type: ignore[call-arg]


async def test_the_request_context_can_be_bound_instead_of_passed() -> None:
    fakes, _ = governed()
    async with fakes.container() as services:
        with bind_request_context(caller()):
            result = await services.data_source("accounts").query("all_accounts")
    assert len(result.rows) == 6
    assert fakes.audit.records[0].subject == "u-7"


async def test_without_a_caller_nothing_is_asked_and_nothing_runs() -> None:
    fakes, source = governed()
    async with fakes.container() as services:
        with pytest.raises(PolicyDenied) as caught:
            await services.data_source("accounts").query("all_accounts")
        late = caller(deadline=fakes.clock.now() - timedelta(seconds=1))
        with pytest.raises(PolicyDenied) as expired:
            await services.data_source("accounts").query("all_accounts", context=late)
    assert caught.value.reason_code == "identity_missing"
    assert expired.value.reason_code == "deadline_exceeded"
    assert fakes.policy.requests == []
    assert source.calls == []
    assert [record.outcome for record in fakes.audit.records] == [AuditOutcome.DENIED] * 2


async def test_data_above_the_requests_ceiling_is_refused_before_the_policy_is_asked() -> None:
    fakes, source = governed()
    async with fakes.container() as services:
        with pytest.raises(PolicyDenied) as caught:
            await services.data_source("accounts").query(
                "accounts_by_region",
                {"region": "east"},
                context=caller(classification_ceiling=Classification.CONFIDENTIAL),
            )
    assert caught.value.reason_code == "classification_exceeded"
    assert fakes.policy.requests == []
    assert source.calls == []


async def test_an_approval_obligation_is_a_deny_until_approvals_exist() -> None:
    fakes, source = governed(Obligations(require_approval=True))
    async with fakes.container() as services:
        with pytest.raises(PolicyDenied) as caught:
            await services.data_source("accounts").query("all_accounts", context=caller())
    assert caught.value.reason_code == "approval_unavailable"
    assert source.calls == []


async def test_an_obligation_the_query_cannot_honour_is_a_deny() -> None:
    fakes, _ = governed(Obligations(row_filters=(RowFilter("branch", ("north",)),)))
    async with fakes.container() as services:
        with pytest.raises(PolicyDenied) as caught:
            await services.data_source("accounts").query("all_accounts", context=caller())
    assert caught.value.reason_code == "obligation_unenforceable"
    assert fakes.audit.records[0].outcome is AuditOutcome.DENIED


async def test_an_unknown_query_is_refused_and_recorded_without_its_name() -> None:
    fakes, source = governed()
    assert source.calls == []
    async with fakes.container() as services:
        with pytest.raises(ValidationFailed):
            await services.data_source("accounts").query(
                "accounts; DROP TABLE accounts", context=caller()
            )
    assert fakes.policy.requests == []
    (record,) = fakes.audit.records
    assert record.outcome is AuditOutcome.FAILED
    assert record.attributes["query"] == "unknown"
    assert "DROP" not in json.dumps(record.to_dict())


async def test_bad_parameters_are_refused_and_recorded_without_their_values() -> None:
    fakes, _ = governed()
    async with fakes.container() as services:
        with pytest.raises(ValidationFailed):
            await services.data_source("accounts").query(
                "accounts_by_region", {"region": "east", "min_balance": "s3cret"}, context=caller()
            )
    (record,) = fakes.audit.records
    assert record.outcome is AuditOutcome.FAILED
    assert record.error_type == "ValidationFailed"
    assert "s3cret" not in json.dumps(record.to_dict())


async def test_no_rows_are_returned_when_the_audit_record_cannot_be_written() -> None:
    fakes, source = governed()
    fakes.audit.fail_with = OSError("disk full")
    async with fakes.container() as services:
        with pytest.raises(IntegrityError):
            await services.data_source("accounts").query("all_accounts", context=caller())
    assert len(source.calls) == 1


async def test_rules_csv_and_jsonl_work_together_with_no_server(tmp_path: Path) -> None:
    """Real local adapters end to end: rules policy, DuckDB over CSV, JSONL audit."""
    data_dir, queries_dir = write_accounts_files(tmp_path)
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "schema: agentlib.rules/v1\n"
        "rules:\n"
        "  - id: analysts-see-the-east\n"
        "    actions: [data.query]\n"
        "    roles: [analyst]\n"
        "    resources: ['ledger.*']\n"
        "    max_classification: restricted\n"
        "    obligations:\n"
        "      row_filter: {region: [east]}\n"
        "      mask_columns: [holder]\n",
        encoding="utf-8",
    )
    audit_path = tmp_path / "audit.jsonl"
    defaults = ServiceProviders.default()
    registry = Fakes().providers()
    for port, name in (
        (Section.POLICY, "rules"),
        (Section.AUDIT, "jsonl"),
        (DATA_PORT, "duckdb_csv"),
    ):
        spec = defaults.lookup(port, name)
        registry.register(port, name, spec.factory, local_only=spec.local_only)
    config = ServiceConfig.for_testing(
        sections={
            Section.POLICY: ProviderSelection("rules", {"path": str(rules)}),
            Section.AUDIT: ProviderSelection("jsonl", {"path": str(audit_path)}),
        },
        data_sources={
            "ledger": ProviderSelection(
                "duckdb_csv", {"data_dir": str(data_dir), "queries_dir": str(queries_dir)}
            )
        },
    )
    async with ServiceContainer(config, registry) as services:
        await services.validate()
        ledger = services.data_source("ledger")
        analyst = await ledger.query("all_accounts", context=caller())
        with pytest.raises(PolicyDenied):
            await ledger.query(
                "all_accounts",
                context=caller(
                    principal=Principal(subject="u-8", tenant="t-9", roles=frozenset({"teller"}))
                ),
            )

    assert [str(row[0]) for row in analyst.rows] == ["4411", "4412", "4413", "4414"]
    assert {row[1] for row in analyst.rows} == {MASK}
    allowed, denied = (json.loads(line) for line in audit_path.read_text("utf-8").splitlines())
    assert allowed["outcome"] == "success"
    assert allowed["attributes"]["policy_reason_code"] == "analysts-see-the-east"
    assert allowed["attributes"]["policy_bundle_revision"].startswith("sha256:")
    assert allowed["attributes"]["policy_decision_id"]
    assert denied["outcome"] == "denied"
    assert denied["attributes"]["reason_code"] == "no_matching_rule"
    assert denied["attributes"]["policy_decision_id"] != allowed["attributes"]["policy_decision_id"]
