"""Contract test suites: one per port, run against every adapter of that port.

To test an adapter, subclass the suite for its port in a test module, give the
subclass a name that starts with ``Test`` and implement the hooks::

    class TestJsonlAuditSink(AuditSinkContract):
        def make_sink(self, tmp_path): ...
        async def read_records(self, sink): ...

The same suites run against the fakes in this package, the local adapters and
the cloud adapters, which is what makes one adapter replaceable by another.

This module needs ``pytest`` and ``pytest-asyncio``; install the ``testing``
extra of ``ai-agent-lib-core`` to get them.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    MASK,
    AgentEntry,
    AgentLibError,
    AuditOutcome,
    AuditRecord,
    AuditSink,
    AuditValue,
    ChatModelProvider,
    Classification,
    Clock,
    ConfigSource,
    ConfigurationError,
    DataSource,
    Decision,
    GuardrailCheck,
    GuardrailPoint,
    GuardrailVerdict,
    IdentityVerifier,
    IdGenerator,
    IntegrityError,
    ModelCapabilities,
    Obligations,
    ParameterType,
    PolicyAction,
    PolicyDecisionPoint,
    PolicyDenied,
    PolicyRequest,
    PolicyResource,
    Principal,
    PrincipalKind,
    QueryResult,
    RegistrySource,
    RowFilter,
    SecretsProvider,
    ServerEntry,
    SupportsAsyncClose,
    SupportsValidation,
    Telemetry,
    ToolEntry,
    ValidationFailed,
)

__all__ = [
    "AuditSinkContract",
    "ChatModelProviderContract",
    "ClockContract",
    "ConfigSourceContract",
    "DataSourceContract",
    "GuardrailCheckContract",
    "IdGeneratorContract",
    "IdentityVerifierContract",
    "PolicyDecisionPointContract",
    "RegistrySourceContract",
    "SecretsProviderContract",
    "TelemetryContract",
    "make_audit_record",
]


def make_audit_record(number: int = 1) -> AuditRecord:
    """Return a distinct, valid audit record for use in tests."""
    return AuditRecord(
        record_id=f"rec-{number}",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        event="model.call",
        outcome=AuditOutcome.SUCCESS,
        request_id=f"req-{number}",
        thread_id="thread-1",
        tenant="tenant-1",
        subject="subject-1",
        application="contract-suite",
        attributes={"number": number, "label": "ünïcode ✓", "flag": True, "missing": None},
    )


@pytest.mark.asyncio
class AuditSinkContract(abc.ABC):
    """What every :class:`AuditSink` must do."""

    @abc.abstractmethod
    def make_sink(self, tmp_path: Path) -> AuditSink:
        """Return a new, empty sink. ``tmp_path`` is a private directory."""

    @abc.abstractmethod
    async def read_records(self, sink: AuditSink) -> list[dict[str, object]]:
        """Return everything the sink has stored, oldest first, as dictionaries."""

    async def break_sink(self, sink: AuditSink) -> bool:  # noqa: ARG002 - a hook
        """Make the sink unable to store records.

        Return ``False`` if this cannot be simulated; the failure test is then skipped.
        """
        return False

    @pytest.fixture
    async def sink(self, tmp_path: Path) -> AsyncIterator[AuditSink]:
        """A fresh sink, closed after the test if it can be closed."""
        sink = self.make_sink(tmp_path)
        yield sink
        if isinstance(sink, SupportsAsyncClose):
            await sink.aclose()

    async def test_a_written_record_can_be_read_back_unchanged(self, sink: AuditSink) -> None:
        record = make_audit_record()
        await sink.write(record)
        assert await self.read_records(sink) == [record.to_dict()]

    async def test_records_are_stored_in_the_order_written(self, sink: AuditSink) -> None:
        for number in range(1, 6):
            await sink.write(make_audit_record(number))
        stored = await self.read_records(sink)
        assert [item["record_id"] for item in stored] == [f"rec-{n}" for n in range(1, 6)]

    async def test_concurrent_writes_are_all_stored_intact(self, sink: AuditSink) -> None:
        records = [make_audit_record(number) for number in range(1, 26)]
        await asyncio.gather(*(sink.write(record) for record in records))
        stored = await self.read_records(sink)
        assert sorted(stored, key=lambda item: str(item["request_id"])) == sorted(
            (record.to_dict() for record in records), key=lambda item: str(item["request_id"])
        )

    async def test_a_sink_that_cannot_store_raises_integrity_error(self, sink: AuditSink) -> None:
        if not await self.break_sink(sink):
            pytest.skip("this sink cannot be broken in a test")
        with pytest.raises(IntegrityError):
            await sink.write(make_audit_record())

    async def test_a_working_sink_passes_its_own_validation(self, sink: AuditSink) -> None:
        if not isinstance(sink, SupportsValidation):
            pytest.skip("this sink has no startup validation")
        await sink.validate()


@pytest.mark.asyncio
class SecretsProviderContract(abc.ABC):
    """What every :class:`SecretsProvider` must do."""

    @abc.abstractmethod
    def make_provider(self, secrets: Mapping[str, str]) -> SecretsProvider:
        """Return a provider that holds exactly ``secrets``."""

    async def test_a_known_secret_is_returned_masked(self) -> None:
        provider = self.make_provider({"api_key": "s3cr3t-value"})
        secret = await provider.get_secret("api_key")
        assert isinstance(secret, SecretStr)
        assert secret.get_secret_value() == "s3cr3t-value"
        assert "s3cr3t-value" not in repr(secret)
        assert "s3cr3t-value" not in str(secret)

    async def test_an_unknown_secret_is_a_configuration_error(self) -> None:
        provider = self.make_provider({"api_key": "s3cr3t-value"})
        with pytest.raises(ConfigurationError) as caught:
            await provider.get_secret("missing")
        assert "missing" in str(caught.value)
        assert "s3cr3t-value" not in str(caught.value)

    async def test_the_provider_does_not_reveal_secrets_in_its_repr(self) -> None:
        provider = self.make_provider({"api_key": "s3cr3t-value"})
        assert "s3cr3t-value" not in repr(provider)
        assert "s3cr3t-value" not in str(provider)


@pytest.mark.asyncio
class IdentityVerifierContract(abc.ABC):
    """What every :class:`IdentityVerifier` must do."""

    @abc.abstractmethod
    def make_verifier(self) -> IdentityVerifier:
        """Return a verifier."""

    @abc.abstractmethod
    def trusted_credential(self) -> str | None:
        """Return a credential the verifier accepts."""

    def untrusted_credentials(self) -> list[str | None]:
        """Return credentials the verifier must refuse. Empty if it refuses none."""
        return []

    async def test_a_trusted_credential_gives_an_immutable_principal(self) -> None:
        principal = await self.make_verifier().verify(self.trusted_credential())
        assert isinstance(principal, Principal)
        with pytest.raises(dataclasses.FrozenInstanceError):
            principal.tenant = "another-tenant"  # type: ignore[misc]

    async def test_the_same_credential_gives_the_same_principal(self) -> None:
        verifier = self.make_verifier()
        first = await verifier.verify(self.trusted_credential())
        second = await verifier.verify(self.trusted_credential())
        assert first == second

    async def test_an_untrusted_credential_is_denied(self) -> None:
        refused = self.untrusted_credentials()
        if not refused:
            pytest.skip("this verifier accepts every caller by design")
        verifier = self.make_verifier()
        for credential in refused:
            with pytest.raises(PolicyDenied):
                await verifier.verify(credential)


class ConfigSourceContract(abc.ABC):
    """What every :class:`ConfigSource` must do."""

    @abc.abstractmethod
    def make_source(self, values: Mapping[str, str], tmp_path: Path) -> ConfigSource:
        """Return a source that holds exactly ``values``."""

    def test_a_held_value_is_returned_as_given(self, tmp_path: Path) -> None:
        source = self.make_source({"FIRST": "1", "JSON": '{"a": [1, 2]}'}, tmp_path)
        assert source.get("FIRST") == "1"
        assert source.get("JSON") == '{"a": [1, 2]}'

    def test_a_missing_name_gives_none(self, tmp_path: Path) -> None:
        assert self.make_source({"FIRST": "1"}, tmp_path).get("SECOND") is None

    def test_names_lists_exactly_what_is_held(self, tmp_path: Path) -> None:
        source = self.make_source({"FIRST": "1", "SECOND": "2"}, tmp_path)
        assert sorted(source.names()) == ["FIRST", "SECOND"]

    def test_names_are_case_sensitive(self, tmp_path: Path) -> None:
        assert self.make_source({"FIRST": "1"}, tmp_path).get("first") is None


class ClockContract(abc.ABC):
    """What every :class:`Clock` must do."""

    @abc.abstractmethod
    def make_clock(self) -> Clock:
        """Return a clock."""

    def test_now_is_timezone_aware_utc(self) -> None:
        now = self.make_clock().now()
        assert now.utcoffset() is not None
        assert now.utcoffset() == UTC.utcoffset(now)

    def test_monotonic_never_goes_backwards(self) -> None:
        clock = self.make_clock()
        readings = [clock.monotonic() for _ in range(5)]
        assert readings == sorted(readings)


class IdGeneratorContract(abc.ABC):
    """What every :class:`IdGenerator` must do."""

    @abc.abstractmethod
    def make_generator(self) -> IdGenerator:
        """Return an identifier generator."""

    def test_identifiers_are_unique_non_empty_strings(self) -> None:
        generator = self.make_generator()
        identifiers = [generator.new_id() for _ in range(200)]
        assert all(identifiers), "an identifier was empty"
        assert len(set(identifiers)) == len(identifiers)


class TelemetryContract(abc.ABC):
    """What every :class:`Telemetry` must do."""

    @abc.abstractmethod
    def make_telemetry(self) -> Telemetry:
        """Return a telemetry adapter."""

    def test_signals_with_every_scalar_type_are_accepted(self) -> None:
        telemetry = self.make_telemetry()
        attributes: dict[str, AuditValue] = {
            "text": "value",
            "count": 3,
            "ratio": 0.5,
            "flag": True,
            "missing": None,
        }
        telemetry.event("model.call", attributes)
        telemetry.duration("model.call", 0.25, attributes)

    def test_signals_without_attributes_are_accepted(self) -> None:
        telemetry = self.make_telemetry()
        telemetry.event("tool.call", {})
        telemetry.duration("tool.call", 0.0, {})

    def test_a_span_opens_takes_facts_as_it_ends_and_never_raises(self) -> None:
        telemetry = self.make_telemetry()
        with telemetry.span("chat model-x", {"gen_ai.operation.name": "chat"}) as note:
            note({"agentlib.outcome": "success", "gen_ai.usage.input_tokens": 3, "none": None})
        with telemetry.span("execute_tool lookup", {}) as note:
            note({"agentlib.outcome": "failed"}, ValueError("the private text"))

    def test_an_error_inside_a_span_passes_through_it(self) -> None:
        telemetry = self.make_telemetry()
        with pytest.raises(KeyError), telemetry.span("query a.b", {}):
            raise KeyError("x")


class ChatModelProviderContract(abc.ABC):
    """What every :class:`ChatModelProvider` must do, without calling a model."""

    @abc.abstractmethod
    def make_provider(self) -> ChatModelProvider:
        """Return a provider. It must not need the network to be constructed."""

    def vendor_errors(self) -> list[tuple[BaseException, type[AgentLibError]]]:
        """Return vendor errors and the taxonomy class each must map to."""
        return []

    def test_capabilities_are_declared(self) -> None:
        assert isinstance(self.make_provider().capabilities, ModelCapabilities)

    def test_creating_a_model_needs_no_network(self) -> None:
        assert self.make_provider().create("any-model-id") is not None

    def test_errors_from_elsewhere_are_not_claimed(self) -> None:
        provider = self.make_provider()
        assert provider.classify_error(ValueError("not a vendor error")) is None
        assert provider.classify_error(KeyError("nor this")) is None

    def test_vendor_errors_map_to_the_taxonomy(self) -> None:
        cases = self.vendor_errors()
        if not cases:
            pytest.skip("this provider has no vendor errors of its own")
        provider = self.make_provider()
        for error, expected in cases:
            mapped = provider.classify_error(error)
            assert isinstance(mapped, expected), f"{type(error).__name__} -> {mapped!r}"


def _account_ids(result: QueryResult) -> list[str]:
    # Engines differ on whether an identifier column is text or a number.
    return [str(row[0]) for row in result.rows]


@pytest.mark.asyncio
class DataSourceContract(abc.ABC):
    """What every :class:`DataSource` must do.

    The suite runs against the standard accounts dataset; see
    :mod:`ai_agent_lib_core.testing.datasets`.
    """

    @abc.abstractmethod
    async def make_source(self, tmp_path: Path) -> DataSource:
        """Return a started source that holds the standard accounts dataset."""

    @pytest.fixture
    async def source(self, tmp_path: Path) -> AsyncIterator[DataSource]:
        """A fresh source, closed after the test if it can be closed."""
        source = await self.make_source(tmp_path)
        yield source
        if isinstance(source, SupportsAsyncClose):
            await source.aclose()

    async def test_the_queries_and_their_interfaces_are_described(self, source: DataSource) -> None:
        described = source.describe()
        assert sorted(described) == ["accounts_by_region", "all_accounts"]
        by_region = described["accounts_by_region"]
        assert by_region.max_rows == 3
        assert [(p.name, p.type, p.required) for p in by_region.parameters] == [
            ("region", ParameterType.STRING, True),
            ("min_balance", ParameterType.NUMBER, False),
        ]

    async def test_a_query_returns_named_columns_and_matching_rows(
        self, source: DataSource
    ) -> None:
        result = await source.query("accounts_by_region", {"region": "west"})
        assert result.columns == ("account_id", "holder", "region", "balance")
        assert _account_ids(result) == ["5520", "5521"]
        assert not result.truncated
        assert result.as_dicts()[0]["holder"] == "Eve"

    async def test_an_optional_parameter_falls_back_to_its_default(
        self, source: DataSource
    ) -> None:
        everything = await source.query("accounts_by_region", {"region": "west"})
        explicit_none = await source.query(
            "accounts_by_region", {"region": "west", "min_balance": None}
        )
        large_only = await source.query(
            "accounts_by_region", {"region": "west", "min_balance": "1000"}
        )
        assert _account_ids(everything) == _account_ids(explicit_none) == ["5520", "5521"]
        assert _account_ids(large_only) == ["5520"]

    async def test_the_row_cap_is_enforced_and_reported(self, source: DataSource) -> None:
        result = await source.query("accounts_by_region", {"region": "east"})
        assert _account_ids(result) == ["4411", "4412", "4413"]
        assert result.truncated

    async def test_an_unknown_query_is_refused(self, source: DataSource) -> None:
        with pytest.raises(ValidationFailed):
            await source.query("drop_everything")
        with pytest.raises(ValidationFailed):
            await source.query("SELECT * FROM accounts")

    @pytest.mark.parametrize(
        "parameters",
        [
            {},
            {"region": "east", "surprise": 1},
            {"region": 7},
            {"region": "east", "min_balance": "plenty"},
        ],
        ids=["missing", "unknown", "wrong-type", "not-a-number"],
    )
    async def test_invalid_parameters_are_refused(
        self, source: DataSource, parameters: dict[str, object]
    ) -> None:
        with pytest.raises(ValidationFailed):
            await source.query("accounts_by_region", parameters)

    async def test_a_hostile_value_is_data_and_never_part_of_the_query(
        self, source: DataSource
    ) -> None:
        result = await source.query("accounts_by_region", {"region": "east' OR '1'='1"})
        assert result.rows == ()

    async def test_row_filters_apply_before_the_row_cap(self, source: DataSource) -> None:
        # Di is the fourth account in the east; a cap applied first would drop the row.
        result = await source.query(
            "accounts_by_region",
            {"region": "east"},
            obligations=Obligations(row_filters=(RowFilter("holder", ("Di",)),)),
        )
        assert _account_ids(result) == ["4414"]
        assert not result.truncated

    async def test_every_row_filter_must_hold(self, source: DataSource) -> None:
        obligations = Obligations(
            row_filters=(RowFilter("region", ("west",)), RowFilter("holder", ("Ann", "Fay")))
        )
        result = await source.query("all_accounts", obligations=obligations)
        assert _account_ids(result) == ["5521"]

    async def test_a_row_filter_with_no_values_admits_no_rows(self, source: DataSource) -> None:
        result = await source.query(
            "all_accounts", obligations=Obligations(row_filters=(RowFilter("region", ()),))
        )
        assert result.rows == ()

    async def test_a_row_filter_that_cannot_be_applied_fails_closed(
        self, source: DataSource
    ) -> None:
        obligations = Obligations(row_filters=(RowFilter("branch", ("north",)),))
        with pytest.raises(PolicyDenied) as caught:
            await source.query("all_accounts", obligations=obligations)
        assert caught.value.reason_code == "obligation_unenforceable"

    async def test_masked_columns_are_replaced_and_reported(self, source: DataSource) -> None:
        result = await source.query(
            "all_accounts", obligations=Obligations(mask_columns=frozenset({"Holder", "branch"}))
        )
        assert {row[1] for row in result.rows} == {MASK}
        assert result.masked_columns == frozenset({"holder"})
        assert _account_ids(result)[0] == "4411"

    async def test_a_policy_can_lower_the_row_cap_but_not_raise_it(
        self, source: DataSource
    ) -> None:
        lowered = await source.query(
            "accounts_by_region", {"region": "east"}, obligations=Obligations(max_rows=1)
        )
        raised = await source.query(
            "accounts_by_region", {"region": "east"}, obligations=Obligations(max_rows=50)
        )
        assert (len(lowered.rows), lowered.truncated) == (1, True)
        assert (len(raised.rows), raised.truncated) == (3, True)

    async def test_a_result_says_where_it_came_from(self, source: DataSource) -> None:
        result = await source.query("all_accounts")
        assert result.source is not None
        assert result.source.source
        assert result.source.retrieved_at.tzinfo is not None

    async def test_calls_can_overlap(self, source: DataSource) -> None:
        results = await asyncio.gather(
            *(source.query("accounts_by_region", {"region": "west"}) for _ in range(12))
        )
        assert all(_account_ids(result) == ["5520", "5521"] for result in results)


class RegistrySourceContract(abc.ABC):
    """What every :class:`RegistrySource` must do."""

    servers: tuple[ServerEntry, ...] = (
        ServerEntry(
            id="accounts",
            owner="treasury-data",
            url="http://localhost:8001/mcp",
            audience="accounts-mcp",
            tools=(
                ToolEntry(
                    name="accounts.lookup",
                    version="1.2.0",
                    classification=Classification.RESTRICTED,
                    read_only=True,
                    schema_sha256="ab" * 32,
                ),
                ToolEntry(name="accounts.close", version="0.1.0"),
            ),
        ),
        ServerEntry(id="rates", owner="markets", url="https://rates.example.test/mcp"),
    )
    agents: tuple[AgentEntry, ...] = (
        AgentEntry(
            id="accounts-agent",
            owner="treasury-data",
            version="0.3.0",
            description="Answers account balance questions",
            url="http://localhost:8000",
            mcp_servers=("accounts",),
            model_aliases=("default",),
            classification_ceiling=Classification.RESTRICTED,
        ),
    )

    @abc.abstractmethod
    def make_source(
        self, tmp_path: Path, agents: tuple[AgentEntry, ...], servers: tuple[ServerEntry, ...]
    ) -> RegistrySource:
        """Return a source that holds exactly ``agents`` and ``servers``."""

    @pytest.fixture
    def source(self, tmp_path: Path) -> RegistrySource:
        """A source that holds the standard entries."""
        return self.make_source(tmp_path, self.agents, self.servers)

    def test_registered_entries_are_returned_unchanged(self, source: RegistrySource) -> None:
        assert source.tools.get("accounts") == self.servers[0]
        assert source.agents.get("accounts-agent") == self.agents[0]
        assert source.tools.entries() == self.servers
        assert source.agents.entries() == self.agents

    def test_a_tool_is_found_through_its_server(self, source: RegistrySource) -> None:
        server = source.tools.get("accounts")
        assert server is not None
        lookup = server.tool("accounts.lookup")
        assert lookup is not None
        assert (lookup.read_only, lookup.classification) == (True, Classification.RESTRICTED)
        assert server.tool("accounts.delete") is None

    def test_what_is_not_registered_is_absent(self, source: RegistrySource) -> None:
        assert source.tools.get("payments") is None
        assert source.agents.get("payments-agent") is None
        assert source.tools.get("Accounts") is None

    def test_each_registry_reports_a_revision(self, source: RegistrySource) -> None:
        assert source.tools.revision
        assert source.agents.revision

    def test_an_empty_source_registers_nothing(self, tmp_path: Path) -> None:
        empty = self.make_source(tmp_path, (), ())
        assert empty.tools.entries() == ()
        assert empty.agents.entries() == ()
        assert empty.tools.get("accounts") is None


@pytest.mark.asyncio
class PolicyDecisionPointContract(abc.ABC):
    """What every :class:`PolicyDecisionPoint` that evaluates a rules document must do.

    The in-process ``rules`` provider and the Rego bundle that OPA runs are
    both tested against the document below, which is what keeps them agreeing.
    """

    rules_document: Mapping[str, object] = {
        "schema": "agentlib.rules/v1",
        "rules": [
            {
                "id": "analysts-query-accounts",
                "actions": ["data.query"],
                "roles": ["analyst"],
                "applications": ["accounts-mcp"],
                "resources": ["accounts.*"],
                "max_classification": "restricted",
                "obligations": {
                    "row_filter": {"region": ["east", "west"]},
                    "mask_columns": ["holder"],
                    "max_rows": 50,
                },
            },
            {
                "id": "auditors-query-anything",
                "actions": ["data.query"],
                "roles": ["auditor"],
                "max_classification": "confidential",
            },
            {
                "id": "anyone-uses-the-default-model",
                "actions": ["model.route"],
                "resources": ["default"],
            },
            {
                "id": "analysts-query-accounts-elsewhere",
                "actions": ["data.query"],
                "roles": ["analyst"],
                "resources": ["accounts.*"],
            },
            {
                "id": "tellers-through-the-branch-agent",
                "actions": ["memory.read"],
                "roles": ["teller"],
                "agents": ["branch-agent"],
            },
            {"id": "services-write-memory", "actions": ["memory.write"], "kinds": ["service"]},
        ],
    }

    @abc.abstractmethod
    async def make_policy(
        self, tmp_path: Path, document: Mapping[str, object]
    ) -> PolicyDecisionPoint:
        """Return a decision point that evaluates ``document``."""

    @pytest.fixture
    async def policy(self, tmp_path: Path) -> AsyncIterator[PolicyDecisionPoint]:
        """A decision point over the standard rules, closed after the test."""
        policy = await self.make_policy(tmp_path, self.rules_document)
        yield policy
        if isinstance(policy, SupportsAsyncClose):
            await policy.aclose()

    @staticmethod
    def question(
        *,
        roles: tuple[str, ...] = ("analyst",),
        action: PolicyAction = PolicyAction.DATA_QUERY,
        name: str = "accounts.by_region",
        classification: Classification | None = Classification.RESTRICTED,
        application: str = "accounts-mcp",
        actors: tuple[str, ...] = (),
        kind: PrincipalKind = PrincipalKind.USER,
    ) -> PolicyRequest:
        """Return a request; by default the one the first standard rule grants."""
        return PolicyRequest(
            principal=Principal(
                subject="u-1",
                tenant="t-9",
                roles=frozenset(roles),
                delegation_chain=actors,
                kind=kind,
            ),
            action=action,
            resource=PolicyResource(kind="query", name=name, classification=classification),
            application=application,
            environment="local",
        )

    async def test_a_granted_request_is_allowed_with_the_rules_obligations(
        self, policy: PolicyDecisionPoint
    ) -> None:
        decision = await policy.decide(self.question())
        assert decision.allow
        assert decision.reason_code == "analysts-query-accounts"
        assert decision.obligations == Obligations(
            row_filters=(RowFilter("region", ("east", "west")),),
            mask_columns=frozenset({"holder"}),
            max_rows=50,
        )

    async def test_the_first_matching_rule_decides(self, policy: PolicyDecisionPoint) -> None:
        elsewhere = await policy.decide(self.question(application="reporting-mcp"))
        assert elsewhere.allow
        assert elsewhere.reason_code == "analysts-query-accounts-elsewhere"
        assert elsewhere.obligations.empty

    async def test_a_request_no_rule_grants_is_denied_without_obligations(
        self, policy: PolicyDecisionPoint
    ) -> None:
        for request in (
            self.question(roles=("viewer",)),
            self.question(roles=()),
            self.question(action=PolicyAction.TOOL_CALL),
            self.question(name="payments.by_region"),
            self.question(name="accountsXby_region"),
        ):
            decision = await policy.decide(request)
            assert not decision.allow
            assert decision.reason_code == "no_matching_rule"
            assert decision.obligations.empty

    async def test_a_rule_covers_data_up_to_its_classification_limit(
        self, policy: PolicyDecisionPoint
    ) -> None:
        def as_auditor(classification: Classification | None) -> PolicyRequest:
            return self.question(
                roles=("auditor",), name="ledger.all", classification=classification
            )

        assert (await policy.decide(as_auditor(Classification.CONFIDENTIAL))).allow
        assert (await policy.decide(as_auditor(Classification.PUBLIC))).allow
        assert not (await policy.decide(as_auditor(Classification.RESTRICTED))).allow
        # A limit cannot be checked against data of unknown sensitivity.
        assert not (await policy.decide(as_auditor(None))).allow

    async def test_a_rule_without_roles_covers_any_caller(
        self, policy: PolicyDecisionPoint
    ) -> None:
        def route(alias: str) -> PolicyRequest:
            return self.question(
                roles=(), action=PolicyAction.MODEL_ROUTE, name=alias, classification=None
            )

        assert (await policy.decide(route("default"))).allow
        assert not (await policy.decide(route("judge"))).allow

    async def test_a_rule_that_names_agents_needs_that_agent_to_present_the_request(
        self, policy: PolicyDecisionPoint
    ) -> None:
        def as_teller(*actors: str) -> PolicyRequest:
            return self.question(
                roles=("teller",),
                action=PolicyAction.MEMORY_READ,
                name="notes",
                classification=None,
                actors=actors,
            )

        through_agent = await policy.decide(as_teller("branch-agent"))
        assert through_agent.allow
        assert through_agent.reason_code == "tellers-through-the-branch-agent"
        assert (await policy.decide(as_teller("front-agent", "branch-agent"))).allow
        assert not (await policy.decide(as_teller())).allow
        assert not (await policy.decide(as_teller("other-agent"))).allow
        # Only the agent that presented the request counts, not one further up the chain.
        assert not (await policy.decide(as_teller("branch-agent", "other-agent"))).allow

    async def test_a_rule_can_be_limited_to_a_kind_of_caller(
        self, policy: PolicyDecisionPoint
    ) -> None:
        def write(kind: PrincipalKind) -> PolicyRequest:
            return self.question(
                roles=(),
                action=PolicyAction.MEMORY_WRITE,
                name="notes",
                classification=None,
                kind=kind,
            )

        assert (await policy.decide(write(PrincipalKind.SERVICE))).allow
        assert not (await policy.decide(write(PrincipalKind.USER))).allow

    async def test_every_decision_is_identified(self, policy: PolicyDecisionPoint) -> None:
        first = await policy.decide(self.question())
        second = await policy.decide(self.question(roles=("viewer",)))
        assert isinstance(first, Decision)
        assert first.decision_id
        assert second.decision_id
        assert first.decision_id != second.decision_id
        assert first.bundle_revision
        assert second.bundle_revision == first.bundle_revision

    async def test_an_empty_rule_set_denies_everything(self, tmp_path: Path) -> None:
        policy = await self.make_policy(tmp_path, {"schema": "agentlib.rules/v1", "rules": []})
        try:
            assert not (await policy.decide(self.question())).allow
        finally:
            if isinstance(policy, SupportsAsyncClose):
                await policy.aclose()


@pytest.mark.asyncio
class GuardrailCheckContract(abc.ABC):
    """What every :class:`GuardrailCheck` must do."""

    @abc.abstractmethod
    def make_check(self) -> GuardrailCheck:
        """Return a guardrail check with its default settings."""

    @pytest.mark.parametrize("point", list(GuardrailPoint))
    async def test_harmless_text_is_allowed_at_every_point(self, point: GuardrailPoint) -> None:
        verdict = await self.make_check().check(point, "The meeting is at ten.", None)
        assert isinstance(verdict, GuardrailVerdict)
        assert verdict.allow
        assert verdict.blocking is None

    @pytest.mark.parametrize("point", list(GuardrailPoint))
    async def test_empty_text_is_allowed(self, point: GuardrailPoint) -> None:
        assert (await self.make_check().check(point, "", None)).allow

    async def test_a_finding_names_what_was_found_and_never_repeats_it(self) -> None:
        text = "Write to jo.bloggs@example.test or pay card 4111 1111 1111 1111."
        verdict = await self.make_check().check(GuardrailPoint.MODEL_INPUT, text, None)
        described = verdict.summary() + repr(verdict)
        assert "jo.bloggs" not in described
        assert "4111" not in described
