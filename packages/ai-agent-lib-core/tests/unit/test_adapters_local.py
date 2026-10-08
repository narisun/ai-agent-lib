"""Behaviour of the local adapters beyond their port contracts."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from ai_agent_lib_core.adapters import (
    JsonlAuditOptions,
    JsonlAuditSink,
    NullTelemetry,
    OpenTelemetryTelemetry,
    StaticIdentityOptions,
    StaticIdentityVerifier,
    SystemClock,
    UuidGenerator,
)
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
    TokenExchanger,
)
from ai_agent_lib_core.testing import FrozenClock
from ai_agent_lib_core.testing.contracts import make_audit_record

# ---------------------------------------------------------------------- JSONL


async def test_jsonl_writes_one_compact_json_object_per_line(tmp_path: Path) -> None:
    sink = JsonlAuditSink(JsonlAuditOptions(path=tmp_path / "audit.jsonl"))
    await sink.write(make_audit_record(1))
    await sink.write(make_audit_record(2))
    await sink.aclose()
    raw = sink.path.read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 2
    assert all(": " not in line and json.loads(line) for line in lines)
    assert "ünïcode ✓" in lines[0]


async def test_jsonl_appends_to_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    first = JsonlAuditSink(JsonlAuditOptions(path=path))
    await first.write(make_audit_record(1))
    await first.aclose()
    second = JsonlAuditSink(JsonlAuditOptions(path=path, fsync=False))
    await second.write(make_audit_record(2))
    await second.aclose()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
async def test_jsonl_file_is_private_to_its_owner(tmp_path: Path) -> None:
    sink = JsonlAuditSink(JsonlAuditOptions(path=tmp_path / "audit.jsonl"))
    await sink.write(make_audit_record())
    await sink.aclose()
    assert stat.S_IMODE(sink.path.stat().st_mode) & 0o077 == 0


async def test_jsonl_validation_fails_for_a_path_that_cannot_be_opened(tmp_path: Path) -> None:
    sink = JsonlAuditSink(JsonlAuditOptions(path=tmp_path))  # a directory, not a file
    with pytest.raises(ConfigurationError, match="cannot be opened"):
        await sink.validate()


async def test_jsonl_close_is_idempotent_and_reopens_on_the_next_write(tmp_path: Path) -> None:
    sink = JsonlAuditSink(JsonlAuditOptions(path=tmp_path / "audit.jsonl"))
    await sink.aclose()
    await sink.write(make_audit_record(1))
    await sink.aclose()
    await sink.aclose()
    await sink.write(make_audit_record(2))
    await sink.aclose()
    assert len(sink.path.read_text(encoding="utf-8").splitlines()) == 2


def test_jsonl_options_reject_unknown_keys() -> None:
    with pytest.raises(ValueError, match="pth"):
        JsonlAuditOptions.model_validate({"pth": "typo.jsonl"})
    assert JsonlAuditOptions().tracing is False


# ------------------------------------------------------------------- identity


async def test_static_identity_uses_its_options_for_every_caller() -> None:
    verifier = StaticIdentityVerifier(
        StaticIdentityOptions(subject="dev", tenant="acme", roles=("analyst", "admin")),
        FrozenClock(),
    )
    principal = await verifier.verify("anything at all")
    assert (principal.subject, principal.tenant) == ("dev", "acme")
    assert principal.roles == frozenset({"analyst", "admin"})
    assert principal.authenticated_by == "static-dev-identity"
    assert await verifier.verify(None) == principal


# ------------------------------------------------------------------ telemetry


def test_null_telemetry_accepts_and_discards_signals() -> None:
    telemetry = NullTelemetry()
    telemetry.event("model.call", {"outcome": "success"})
    telemetry.duration("model.call", 0.2, {"outcome": "success"})


def test_opentelemetry_adapter_emits_a_span_event_a_count_and_a_duration() -> None:
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    telemetry = OpenTelemetryTelemetry(meter_provider)
    attributes = {"outcome": "success", "model_alias": "default", "tokens": None}

    with tracer_provider.get_tracer("test").start_as_current_span("request"):
        telemetry.event("model.call", attributes)
        telemetry.duration("model.call", 0.25, attributes)

    (span,) = spans.get_finished_spans()
    (event,) = span.events
    assert event.name == "model.call"
    assert dict(event.attributes or {}) == {"outcome": "success", "model_alias": "default"}

    data = reader.get_metrics_data()
    assert data is not None
    metrics = {
        metric.name: metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    (count,) = metrics["agentlib.events"].data.data_points
    (duration,) = metrics["agentlib.duration"].data.data_points
    assert isinstance(count, NumberDataPoint)
    assert isinstance(duration, HistogramDataPoint)
    assert count.value == 1
    assert dict(count.attributes or {}) == {
        "event": "model.call",
        "outcome": "success",
        "model_alias": "default",
    }
    assert duration.sum == 0.25


def test_opentelemetry_adapter_is_harmless_without_an_sdk() -> None:
    telemetry = OpenTelemetryTelemetry()
    telemetry.event("model.call", {"outcome": "success"})
    telemetry.duration("model.call", 0.1, {})


# --------------------------------------------------------------------- system


def test_system_clock_is_aware_and_monotonic() -> None:
    clock = SystemClock()
    assert clock.now().utcoffset() is not None
    assert clock.monotonic() <= clock.monotonic()


def test_uuid_generator_gives_unique_identifiers() -> None:
    ids = UuidGenerator()
    assert len({ids.new_id() for _ in range(100)}) == 100


def _caller(application: str = "accounts-agent") -> RequestContext:
    return RequestContext(
        principal=Principal(subject="u-7", tenant="t-9", roles=frozenset({"analyst"})),
        application=application,
        request_id="r-1",
        thread_id="th-1",
    )


async def test_a_development_token_carries_the_caller_to_one_audience() -> None:
    clock = FrozenClock()
    agent = StaticIdentityVerifier(StaticIdentityOptions(), clock)
    server = StaticIdentityVerifier(StaticIdentityOptions(audience="accounts-mcp"), clock)
    assert isinstance(agent, TokenExchanger)

    token = await agent.exchange(_caller(), "accounts-mcp")
    assert "u-7" not in repr(token)
    principal = await server.verify(token.get_secret_value())
    assert (principal.subject, principal.tenant) == ("u-7", "t-9")
    assert principal.roles == frozenset({"analyst"})
    # The agent that acted for the caller is on record.
    assert principal.delegation_chain == ("accounts-agent",)
    assert principal.authenticated_by == "static-dev-identity"

    other = StaticIdentityVerifier(StaticIdentityOptions(audience="payments-mcp"), clock)
    with pytest.raises(PolicyDenied) as wrong_audience:
        await other.verify(token.get_secret_value())
    assert wrong_audience.value.reason_code == "credential_audience"


async def test_a_development_token_expires_and_cannot_be_altered() -> None:
    clock = FrozenClock()
    identity = StaticIdentityVerifier(StaticIdentityOptions(token_ttl_seconds=60), clock)
    token = (await identity.exchange(_caller(), "accounts-mcp")).get_secret_value()
    prefix, body, signature = token.split(".")

    forged_body = body[:-2] + ("AA" if not body.endswith("AA") else "BB")
    for altered in (f"{prefix}.{forged_body}.{signature}", f"{prefix}.{body}.{signature[:-2]}xx"):
        with pytest.raises(PolicyDenied) as caught:
            await identity.verify(altered)
        assert caught.value.reason_code == "credential_invalid"

    other_key = StaticIdentityVerifier(StaticIdentityOptions(), clock, key=SecretStr("another key"))
    with pytest.raises(PolicyDenied):
        await other_key.verify(token)

    clock.advance(59)
    assert (await identity.verify(token)).subject == "u-7"
    clock.advance(1)
    with pytest.raises(PolicyDenied) as expired:
        await identity.verify(token)
    assert expired.value.reason_code == "credential_expired"


async def test_each_hop_is_added_to_the_delegation_chain() -> None:
    clock = FrozenClock()
    identity = StaticIdentityVerifier(StaticIdentityOptions(), clock)
    first = await identity.exchange(_caller("front-agent"), "accounts-agent")
    seen_by_agent = await identity.verify(first.get_secret_value())
    onward = RequestContext(
        principal=seen_by_agent, application="accounts-agent", request_id="r", thread_id="t"
    )
    second = await identity.exchange(onward, "accounts-mcp")
    seen_by_server = await identity.verify(second.get_secret_value())
    assert seen_by_server.delegation_chain == ("front-agent", "accounts-agent")


async def test_a_development_service_token_names_no_person() -> None:
    clock = FrozenClock()
    agent = StaticIdentityVerifier(StaticIdentityOptions(tenant="acme"), clock)
    server = StaticIdentityVerifier(StaticIdentityOptions(audience="accounts-mcp"), clock)

    token = await agent.service_token("accounts-mcp")
    principal = await server.verify(token.get_secret_value())

    assert principal.kind is PrincipalKind.SERVICE
    assert (principal.subject, principal.tenant) == ("dev-service", "acme")
    assert principal.roles == frozenset()
    assert principal.delegation_chain == ()
    # A caller's token still says it is a person's.
    for_a_user = await agent.exchange(_caller(), "accounts-mcp")
    assert (await server.verify(for_a_user.get_secret_value())).kind is PrincipalKind.USER
