"""Langfuse and Phoenix settings share one origin rule and one layered resolution."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ai_agent_lib_core.config import MappingConfigSource
from ai_agent_lib_core.config._tracing_support import (
    Layer,
    resolve_variables,
    tracing_layers,
    validate_origin,
)
from ai_agent_lib_core.config.langfuse import LANGFUSE_VARIABLES, LangfuseSettings
from ai_agent_lib_core.config.phoenix import PHOENIX_VARIABLES, PhoenixSettings
from ai_agent_lib_core.config.tracing import resolve_tracing_settings
from ai_agent_lib_core.contracts import ConfigurationError

_LANGFUSE_ENABLED = {
    "EAP_LANGFUSE_ENABLED": "true",
    "LANGFUSE_BASE_URL": "http://localhost:3000",
    "LANGFUSE_PROJECT_ID": "trial",
    "LANGFUSE_PUBLIC_KEY": "public-test-key",
    "LANGFUSE_SECRET_KEY": "private-test-key",
}
_PHOENIX_ENABLED = {"EAP_PHOENIX_ENABLED": "true", "PHOENIX_API_KEY": "phoenix-test-key"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*LANGFUSE_VARIABLES.values(), *PHOENIX_VARIABLES):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- the origin rule

_SETTINGS: dict[str, tuple[str, Callable[[str], object]]] = {
    "langfuse": (
        "LANGFUSE_BASE_URL",
        lambda url: LangfuseSettings(url, "project", "public", "secret"),
    ),
    "phoenix": ("PHOENIX_COLLECTOR_ENDPOINT", lambda url: PhoenixSettings(base_url=url)),
}


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000",
        "http://127.0.0.1:6006",
        "http://[::1]:6006",
        "http://localhost",
        "https://traces.example.com",
        "https://traces.example.com:8443/",
    ],
)
@pytest.mark.parametrize("backend", sorted(_SETTINGS))
def test_loopback_http_and_remote_https_origins_are_accepted(backend: str, url: str) -> None:
    _, build = _SETTINGS[backend]
    build(url)
    validate_origin(url, variable="ANY")


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example.com",
        "https://user:secret@traces.example.com",
        "https://traces.example.com/v1/traces",
        "https://traces.example.com?token=x",
        "https://traces.example.com#fragment",
        "https://traces.example.com\x00",
        "https://traces.\x1fexample.com",
        "https://traces.example.com\x7f",
        "https://traces example.com",
        "https://traces.example.com\n",
        "https://<script>",
        "https://traces.example.com\\",
        "https://traces(example).com",
        "ftp://localhost",
        "",
    ],
)
@pytest.mark.parametrize("backend", sorted(_SETTINGS))
def test_non_origins_control_and_delimiter_characters_are_refused(backend: str, url: str) -> None:
    variable, build = _SETTINGS[backend]
    with pytest.raises(ConfigurationError, match=f"{variable} must be an HTTPS origin") as caught:
        build(url)
    assert caught.value.expected
    assert caught.value.fix
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("url", ["http://localhost:70000", "http://localhost:port"])
@pytest.mark.parametrize("backend", sorted(_SETTINGS))
def test_unparseable_ports_are_refused_as_invalid_origins(backend: str, url: str) -> None:
    variable, build = _SETTINGS[backend]
    with pytest.raises(ConfigurationError, match=f"{variable} must be a valid origin"):
        build(url)


@pytest.mark.parametrize("url", ["http://remote.example.com", "http://localhost:70000"])
def test_both_backends_report_the_same_text_for_the_same_bad_origin(url: str) -> None:
    messages = {}
    for backend, (variable, build) in _SETTINGS.items():
        with pytest.raises(ConfigurationError) as caught:
            build(url)
        messages[backend] = str(caught.value).replace(variable, "<VARIABLE>")
    assert messages["langfuse"] == messages["phoenix"]
    assert "<VARIABLE>" in messages["langfuse"]


# ------------------------------------------------------------ layered resolution


def test_layers_are_process_then_separate_file_then_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("X_ENV_FILE=extra.env\nNAME=dotenv\nONLY_DOTENV=d\n", encoding="utf-8")
    (tmp_path / "extra.env").write_text("NAME=extra\nONLY_EXTRA=e\n", encoding="utf-8")
    layers = tracing_layers(
        dotenv,
        source=None,
        environment=MappingConfigSource({"NAME": "process"}),
        working_dir=None,
        enabled="X_ENABLED",
        env_file="X_ENV_FILE",
        caller="test",
    )
    assert [layer.origin for layer in layers] == [
        "process environment",
        str((tmp_path / "extra.env").resolve()),
        str(dotenv.resolve()),
    ]
    resolved = resolve_variables(
        layers, ("NAME", "ONLY_EXTRA", "ONLY_DOTENV", "MISSING"), defaults={"MISSING": "fallback"}
    )
    assert dict(resolved.values) == {
        "NAME": "process",
        "ONLY_EXTRA": "e",
        "ONLY_DOTENV": "d",
        "MISSING": "fallback",
    }
    assert resolved.origins["NAME"] == "process environment: NAME"
    assert resolved.origins["ONLY_EXTRA"].endswith(": ONLY_EXTRA")
    assert resolved.origins["MISSING"] == "default"


def test_an_explicit_process_disable_skips_a_missing_separate_file(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("X_ENV_FILE=missing.env\n", encoding="utf-8")
    layers = tracing_layers(
        dotenv,
        source=None,
        environment=MappingConfigSource({"X_ENABLED": "false"}),
        working_dir=None,
        enabled="X_ENABLED",
        env_file="X_ENV_FILE",
        caller="test",
    )
    assert [layer.origin for layer in layers] == ["process environment", str(dotenv.resolve())]
    with pytest.raises(ValueError, match="either source or environment to test"):
        tracing_layers(
            None,
            source=MappingConfigSource({}),
            environment=MappingConfigSource({}),
            working_dir=None,
            enabled="X_ENABLED",
            env_file="X_ENV_FILE",
            caller="test",
        )


def test_flags_and_masking_never_show_the_offending_or_secret_value() -> None:
    resolved = resolve_variables(
        [Layer("test", MappingConfigSource({"FLAG": "private-value", "KEY": "private-key"}))],
        ("FLAG", "KEY", "EMPTY_KEY", "UNSET"),
        defaults={},
    )
    with pytest.raises(ConfigurationError, match="FLAG must be true or false") as caught:
        resolved.flag("FLAG")
    assert caught.value.expected == "true or false"
    assert caught.value.fix
    assert "private-value" not in str(caught.value)
    assert resolved.flag("UNSET") is False
    assert dict(resolved.shown(("KEY", "EMPTY_KEY"))) == {
        "FLAG": "private-value",
        "KEY": "********",
        "EMPTY_KEY": "",
        "UNSET": "",
    }


# ------------------------------------------------------- resolve_tracing_settings


def test_disabled_tracing_resolves_both_backends_to_none() -> None:
    resolved = resolve_tracing_settings(None, environment=MappingConfigSource({}))
    assert resolved.langfuse.settings is None
    assert resolved.phoenix.settings is None
    assert resolved.langfuse.shown["EAP_LANGFUSE_ENABLED"] == "false"
    assert resolved.phoenix.shown["EAP_PHOENIX_ENABLED"] == "false"
    assert set(resolved.langfuse.origins.values()) == {"default"}
    assert set(resolved.phoenix.origins.values()) == {"default"}


def test_langfuse_only_resolves_langfuse_and_masks_its_keys() -> None:
    resolved = resolve_tracing_settings(None, environment=MappingConfigSource(_LANGFUSE_ENABLED))
    assert resolved.phoenix.settings is None
    assert resolved.langfuse.settings is not None
    assert resolved.langfuse.settings.project_id == "trial"
    assert resolved.langfuse.settings.public_key == "public-test-key"
    assert resolved.langfuse.shown["LANGFUSE_PUBLIC_KEY"] == "********"
    assert resolved.langfuse.shown["LANGFUSE_SECRET_KEY"] == "********"
    assert resolved.langfuse.origins["LANGFUSE_PROJECT_ID"] == (
        "process environment: LANGFUSE_PROJECT_ID"
    )
    assert "private-test-key" not in str(dict(resolved.langfuse.shown))


def test_phoenix_only_resolves_phoenix_with_defaults_and_a_masked_key() -> None:
    resolved = resolve_tracing_settings(None, environment=MappingConfigSource(_PHOENIX_ENABLED))
    assert resolved.langfuse.settings is None
    assert resolved.phoenix.settings is not None
    assert resolved.phoenix.settings.base_url == "http://127.0.0.1:6006"
    assert resolved.phoenix.settings.project_name == "agent-lab"
    assert resolved.phoenix.settings.api_key == "phoenix-test-key"
    assert resolved.phoenix.shown["PHOENIX_API_KEY"] == "********"
    assert resolved.phoenix.origins["PHOENIX_PROJECT_NAME"] == "default"


def test_both_enabled_is_refused_before_any_backend_runs() -> None:
    both = MappingConfigSource({**_LANGFUSE_ENABLED, **_PHOENIX_ENABLED})
    with pytest.raises(ConfigurationError, match="Enable one agent trace destination"):
        resolve_tracing_settings(None, environment=both)


def test_a_planned_child_working_directory_locates_the_separate_file(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    (child / "traces.env").write_text(
        "EAP_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=child-project\n", encoding="utf-8"
    )
    resolved = resolve_tracing_settings(
        None,
        environment=MappingConfigSource({"EAP_PHOENIX_ENV_FILE": "traces.env"}),
        working_dir=child,
    )
    assert resolved.phoenix.settings is not None
    assert resolved.phoenix.settings.project_name == "child-project"
    assert resolved.langfuse.settings is None
