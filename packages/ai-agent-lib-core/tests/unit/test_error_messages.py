"""What a developer reads when something is wrong: what was expected, what was found, the fix."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_core.config import ConfigResolver, MappingConfigSource
from ai_agent_lib_core.contracts import ConfigurationError
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.observability import explain


def resolve(values: dict[str, str]) -> ServiceContainer:
    return ServiceContainer(ConfigResolver(MappingConfigSource(values)).resolve())


def local(tmp_path: Path, **values: str) -> dict[str, str]:
    return {
        "EAP_AUDIT_OPTIONS": f'{{"path": "{(tmp_path / "audit.jsonl").as_posix()}"}}',
        "EAP_CHECKPOINT_PROVIDER": "none",
        "EAP_POLICY_PROVIDER": "rules",
        "EAP_POLICY_OPTIONS": f'{{"path": "{(tmp_path / "rules.yaml").as_posix()}"}}',
        **values,
    }


def rules(tmp_path: Path) -> None:
    (tmp_path / "rules.yaml").write_text("schema: agentlib.rules/v1\nrules: []\n", "utf-8")


async def test_an_adapter_that_cannot_be_built_names_the_variables_that_chose_it(
    tmp_path: Path,
) -> None:
    rules(tmp_path)
    services = resolve(local(tmp_path, EAP_IDENTITY_OPTIONS='{"tenantt": "acme"}'))
    with pytest.raises(ConfigurationError) as caught:
        await services.start()
    error = caught.value
    assert error.actual == "an unknown option 'tenantt' (did you mean 'tenant'?)"
    assert error.__notes__ == [
        "while building the identity adapter 'static', chosen by EAP_IDENTITY_PROVIDER "
        "with options from EAP_IDENTITY_OPTIONS"
    ]
    assert "  note: while building the identity adapter 'static'" in explain(error)


async def test_a_missing_secret_says_which_variable_supplies_it(tmp_path: Path) -> None:
    rules(tmp_path)
    services = resolve(local(tmp_path, EAP_MODEL_PROVIDER="anthropic", EAP_MODEL_ID="claude-test"))
    with pytest.raises(ConfigurationError) as caught:
        await services.start()
    error = caught.value
    assert error.message == "secret 'anthropic_api_key' is not configured"
    assert error.fix == "set ANTHROPIC_API_KEY in the service's .env or its environment"
    assert "while building the model provider 'anthropic'" in error.__notes__[0]


async def test_a_model_that_is_not_configured_says_which_variable_to_set(tmp_path: Path) -> None:
    rules(tmp_path)
    async with resolve(local(tmp_path, EAP_MODEL_PROVIDER="fake")) as services:
        with pytest.raises(ConfigurationError) as caught:
            services.model()
        assert caught.value.fix is not None
        assert caught.value.fix.startswith("set EAP_MODEL_ID to a model ID")
        with pytest.raises(ConfigurationError) as other:
            services.model("fast")
        assert other.value.fix is not None
        assert other.value.fix.startswith('add "fast" to EAP_MODEL_ALIASES')


async def test_a_local_adapter_in_production_says_what_to_choose_instead(tmp_path: Path) -> None:
    rules(tmp_path)
    services = resolve(local(tmp_path, EAP_DEPLOYMENT_ENV="prod"))
    with pytest.raises(ConfigurationError) as caught:
        await services.start()
    error = caught.value
    assert error.actual is not None
    assert error.actual.endswith("which is local only")
    assert error.fix is not None
    assert "EAP_DEPLOYMENT_ENV=local" in error.fix


async def test_a_data_source_that_is_not_configured_lists_the_ones_that_are(
    tmp_path: Path,
) -> None:
    rules(tmp_path)
    async with resolve(local(tmp_path)) as services:
        with pytest.raises(ConfigurationError) as caught:
            services.data_source("ledger")
    error = caught.value
    assert error.actual == "configured data sources: none"
    assert error.fix is not None
    assert "EAP_DATA_SOURCES" in error.fix


def test_a_value_that_is_not_json_says_where_and_how_to_write_it() -> None:
    with pytest.raises(ConfigurationError) as caught:
        ConfigResolver(MappingConfigSource({"EAP_AUDIT_OPTIONS": "{path: 'x'}"})).resolve()
    error = caught.value
    assert error.message == "EAP_AUDIT_OPTIONS is not valid JSON"
    assert error.actual is not None
    assert "line 1, column 2" in error.actual
    assert "'x'" not in str(error)


def test_telemetry_has_a_switch_of_its_own() -> None:
    from ai_agent_lib_core.adapters.telemetry import NullTelemetry, OpenTelemetryTelemetry
    from ai_agent_lib_core.contracts import TelemetryMode

    on = ConfigResolver(MappingConfigSource({"EAP_TELEMETRY": "opentelemetry"})).resolve()
    off = ConfigResolver(MappingConfigSource({})).resolve()
    assert (on.telemetry, off.telemetry) == (TelemetryMode.OPENTELEMETRY, TelemetryMode.OFF)
    assert isinstance(ServiceContainer(on).telemetry, OpenTelemetryTelemetry)
    assert isinstance(ServiceContainer(off).telemetry, NullTelemetry)
    with pytest.raises(ConfigurationError) as caught:
        ConfigResolver(MappingConfigSource({"EAP_TELEMETRY": "otel"})).resolve()
    assert caught.value.expected == "one of: off, opentelemetry"
