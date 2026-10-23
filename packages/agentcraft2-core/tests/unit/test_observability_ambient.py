"""Ambient telemetry: the host installed the providers; the service installs nothing."""

from __future__ import annotations

import pytest
from opentelemetry import trace

from agentcraft2_core import ServiceContainer
from agentcraft2_core.adapters import OpenTelemetryTelemetry
from agentcraft2_core.config import telemetry_enabled
from agentcraft2_core.contracts import ConfigurationError, ServiceConfig, TelemetryMode
from agentcraft2_core.observability import ambient_provider_installed, otel, start_telemetry


@pytest.fixture
def no_global_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API's own placeholder, as in a process nobody instrumented."""
    monkeypatch.delenv("OTEL_PYTHON_TRACER_PROVIDER", raising=False)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)


def test_ambient_is_a_telemetry_mode_that_turns_telemetry_on() -> None:
    assert TelemetryMode("ambient") is TelemetryMode.AMBIENT
    config = ServiceConfig.for_testing(telemetry=TelemetryMode.AMBIENT)
    assert telemetry_enabled(config) is True
    assert isinstance(ServiceContainer._default_telemetry(config), OpenTelemetryTelemetry)
    assert telemetry_enabled(ServiceConfig.for_testing(telemetry=TelemetryMode.OFF)) is False


def test_ambient_telemetry_uses_the_providers_the_host_installed(
    monkeypatch: pytest.MonkeyPatch, no_global_provider: None
) -> None:
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr(
        otel, "configure_telemetry", lambda *a, **k: pytest.fail("ambient installs nothing")
    )
    # As opentelemetry-instrument does before the service's own code runs.
    installed = TracerProvider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", installed)
    assert ambient_provider_installed() is True
    flush = start_telemetry(
        "accounts-agent", ServiceConfig.for_testing(telemetry=TelemetryMode.AMBIENT)
    )
    flush()
    assert trace.get_tracer_provider() is installed
    installed.shutdown()


def test_ambient_telemetry_fails_fast_without_a_provider(no_global_provider: None) -> None:
    assert type(trace.get_tracer_provider()).__name__ == "ProxyTracerProvider"
    assert ambient_provider_installed() is False
    with pytest.raises(ConfigurationError) as caught:
        start_telemetry(
            "accounts-agent", ServiceConfig.for_testing(telemetry=TelemetryMode.AMBIENT)
        )
    assert caught.value.expected == (
        "a process started under opentelemetry-instrument, or AC2_TELEMETRY=opentelemetry"
    )
    assert "opentelemetry-instrument" in (caught.value.fix or "")
    # Off and opentelemetry are not affected by the missing provider.
    start_telemetry("accounts-agent", ServiceConfig.for_testing())()
