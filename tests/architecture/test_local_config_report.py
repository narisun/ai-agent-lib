"""Startup output identifies winning settings without exposing credentials."""

import json
from pathlib import Path

import pytest
from tools.local_config_report import report_environment

from ai_agent_lib_core.config import ConfigResolver, LayeredConfigSource, MappingConfigSource


@pytest.mark.parametrize("from_launcher", [False, True])
def test_startup_uses_separate_tracing_file_and_child_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    from_launcher: bool,
) -> None:
    from tools.local_environment import agent_environment

    from ai_agent_lib_core.config.phoenix import resolve_phoenix_settings

    monkeypatch.setattr("os.environ", {})
    folder = tmp_path / "agent"
    folder.mkdir()
    service, launcher = folder / ".env", tmp_path / ".env"
    trace_file = (tmp_path if from_launcher else folder) / "tracing.env"
    service.write_text(
        "AC2_PHOENIX_ENABLED=true\nAC2_PHOENIX_CAPTURE_CONTENT=false\n"
        "PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006\n"
        + ("" if from_launcher else "AC2_PHOENIX_ENV_FILE=tracing.env\n"),
        encoding="utf-8",
    )
    launcher.write_text(
        "AC2_PHOENIX_ENV_FILE=tracing.env\n" if from_launcher else "", encoding="utf-8"
    )
    trace_file.write_text(
        "AC2_PHOENIX_CAPTURE_CONTENT=true\nPHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6007\n"
        "PHOENIX_API_KEY=phoenix-private\n",
        encoding="utf-8",
    )
    environment = agent_environment(service, launcher, required=True, working_dir=tmp_path)
    resolved = resolve_phoenix_settings(
        service, environment=MappingConfigSource(environment), working_dir=tmp_path
    )
    settings = resolved.settings
    assert settings is not None
    assert settings.capture_content
    assert settings.base_url == "http://127.0.0.1:6007"
    output = capsys.readouterr().out
    assert f'PHOENIX_COLLECTOR_ENDPOINT="http://127.0.0.1:6007"  [{trace_file}' in output
    assert f'AC2_PHOENIX_CAPTURE_CONTENT="true"  [{trace_file}' in output
    assert "http://127.0.0.1:6006" not in next(
        line for line in output.splitlines() if "PHOENIX_COLLECTOR_ENDPOINT=" in line
    )
    assert "PHOENIX_API_KEY=********" in output
    assert "phoenix-private" not in output


def show(shell: dict[str, str], launcher: dict[str, str], service: dict[str, str]) -> None:
    layers = tuple(
        (name, MappingConfigSource(values))
        for name, values in (
            ("shell", shell),
            ("launcher .env", launcher),
            ("service .env", service),
        )
    )
    resolution = ConfigResolver(LayeredConfigSource(*(source for _, source in layers))).explain()
    report_environment(resolution, layers)


def test_startup_summary_shows_effective_values_and_origins(
    capsys: pytest.CaptureFixture[str],
) -> None:
    show(
        {"AC2_MODEL_ID": "shell-model", "RMPREP_CONTEXT_INPUT_TOKENS": "64000"},
        {"AC2_MODEL_PROVIDER": "anthropic", "AC2_MODEL_ID": "file-model", "AC2_PROFILE": "local"},
        {"AC2_MODEL_PROVIDER": "fake", "AC2_MODEL_ID": "fake-model", "AC2_TELEMETRY": "off"},
    )
    output = capsys.readouterr().out
    assert 'AC2_PROFILE="local"  [launcher .env]' in output
    assert 'AC2_MODEL_ID="shell-model"  [shell]' in output
    assert 'AC2_MODEL_PROVIDER="anthropic"  [launcher .env]' in output
    assert 'AC2_TELEMETRY="off"  [service .env]' in output
    assert 'AC2_DEPLOYMENT_ENV="local"  [default]' in output
    assert 'RMPREP_CONTEXT_INPUT_TOKENS="64000"  [shell]' in output
    assert "file-model" not in output
    assert "fake-model" not in output


def test_startup_summary_masks_credentials_but_shows_public_json_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    protected = {
        "ANTHROPIC_API_KEY": "anthropic-private-value",
        # A credential left over from an earlier setup is never shown, though never read.
        "LANGFUSE_SECRET_KEY": "stale-private-value",
        "PHOENIX_API_KEY": "phoenix-private-value",
        "AWS_ACCESS_KEY_ID": "aws-private-id",
        "AWS_SESSION_TOKEN": "aws-private-token",
        "OTEL_EXPORTER_OTLP_HEADERS": "x-custom-auth=otel-private-value",
        "AC2_SECRET_PAYMENTS": "payments-private-value",
        "MY_COMPANY_CREDENTIAL": "company-private-value",
    }
    options = {
        "AC2_MODEL_OPTIONS": json.dumps(
            {
                "anthropic": {
                    "prompt_caching": True,
                    "cache_ttl": "5m",
                    "custom-header": "model-private-value",
                }
            }
        ),
        "AC2_CHECKPOINT_OPTIONS": json.dumps(
            {"dsn": "postgres://user:database-private-value@host/db"}
        ),
        "AC2_IDENTITY_OPTIONS": json.dumps({"subject": "personal-identity", "roles": ["analyst"]}),
        "AC2_DATA_SOURCES": json.dumps(
            {
                "ledger": {
                    "kind": "rest",
                    "headers": {"X-Custom": "nested-private-value", "Accept": "application/json"},
                }
            }
        ),
    }
    show({}, protected, options)
    output = capsys.readouterr().out
    for name, value in protected.items():
        assert f"{name}=********  [launcher .env]" in output
        assert value not in output
    for name in options:
        line = next(line for line in output.splitlines() if line.startswith(f"  {name}="))
        assert line.endswith("  [service .env]")
        assert isinstance(json.loads(line.split("=", 1)[1].rsplit("  [", 1)[0]), dict)
    assert '"prompt_caching": true' in output
    assert '"cache_ttl": "5m"' in output
    assert '"roles": ["analyst"]' in output
    assert '"Accept": "application/json"' in output
    for value in (
        "model-private-value",
        "database-private-value",
        "personal-identity",
        "nested-private-value",
    ):
        assert value not in output


def test_public_json_summary_uses_the_winning_layer_without_mutating_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = {
        "anthropic": {"prompt_caching": True, "max_tokens": 4096, "apiKey": "private-test-key"}
    }
    original = json.dumps(value)
    launcher = {"AC2_MODEL_OPTIONS": original}
    show({}, launcher, {"AC2_MODEL_OPTIONS": '{"anthropic": {"prompt_caching": false}}'})
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if line.startswith("  AC2_MODEL_OPTIONS="))
    shown = json.loads(line.split("=", 1)[1].rsplit("  [", 1)[0])
    assert shown == {
        "anthropic": {"prompt_caching": True, "max_tokens": 4096, "apiKey": "********"}
    }
    assert line.endswith("  [launcher .env]")
    assert launcher["AC2_MODEL_OPTIONS"] == original


def test_startup_summary_handles_aliases_empty_values_and_unrelated_shell_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    show(
        {
            "AWS_DEFAULT_REGION": "us-east-1",
            "UNRELATED_SECRET": "not-for-this-process",
            "PATH": "private-path",
        },
        {"PHOENIX_API_KEY": "", "HTTPS_PROXY": "http://user:private-proxy@proxy:8080"},
        {},
    )
    output = capsys.readouterr().out
    assert 'AWS_DEFAULT_REGION="us-east-1"  [shell]' in output
    assert "PHOENIX_API_KEY=(empty)" in output
    assert "HTTPS_PROXY=********" in output
    assert "private-proxy" not in output
    assert "UNRELATED_SECRET" not in output
    assert "private-path" not in output
    assert "not-for-this-process" not in output


@pytest.mark.parametrize(
    "value",
    [
        "https://user:url-password@example.com",
        "https://example.com/?X-Amz-Signature=signed-secret",
        "https://example.com/#fragment-secret",
        "https://[invalid",
    ],
)
def test_startup_summary_hides_credentials_in_urls(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    show({}, {"PHOENIX_COLLECTOR_ENDPOINT": value}, {})
    output = capsys.readouterr().out
    assert "PHOENIX_COLLECTOR_ENDPOINT=********  [launcher .env]" in output
    assert value not in output


def test_startup_summary_escapes_control_characters(capsys: pytest.CaptureFixture[str]) -> None:
    show({}, {"PHOENIX_PROJECT_NAME": "project\n\x1b[31msecond-line"}, {})
    output = capsys.readouterr().out
    assert r'PHOENIX_PROJECT_NAME="project\n\u001b[31msecond-line"' in output
    assert "\x1b" not in output


def test_both_spellings_of_the_tracing_controls_are_shown_not_masked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    show(
        {"AC2_PHOENIX_ENV_FILE": "shell.env", "EAP_PHOENIX_CAPTURE_CONTENT": "true"},
        {"AC2_PHOENIX_ENABLED": "true"},
        {"EAP_PHOENIX_ENABLED": "true", "AC2_PHOENIX_CAPTURE_CONTENT": "false"},
    )
    output = capsys.readouterr().out
    assert 'AC2_PHOENIX_ENV_FILE="shell.env"  [shell]' in output
    assert 'EAP_PHOENIX_CAPTURE_CONTENT="true"  [shell]' in output
    assert 'AC2_PHOENIX_ENABLED="true"  [launcher .env]' in output
    assert 'EAP_PHOENIX_ENABLED="true"  [service .env]' in output
    assert 'AC2_PHOENIX_CAPTURE_CONTENT="false"  [service .env]' in output


# ------------------------------------------------- the EAP_ spellings, for one minor release
#
# Every test below is a dedicated legacy-alias test: it spells variables with the old
# ``EAP_`` prefix on purpose. The rest of this file speaks ``AC2_``.


def test_a_legacy_secret_variable_is_still_masked(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.warns(DeprecationWarning, match="EAP_SECRET_PAYMENTS is deprecated"):
        show({}, {"EAP_SECRET_PAYMENTS": "payments-private-value"}, {})
    output = capsys.readouterr().out
    assert "EAP_SECRET_PAYMENTS=********  [launcher .env]" in output
    assert "payments-private-value" not in output


def test_prefixed_shell_variables_of_either_prefix_are_listed_and_masked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Resolution does not list these: the report enumerates prefixed shell variables itself.
    layers = tuple(
        (name, MappingConfigSource(values))
        for name, values in (
            ("shell", {"EAP_UNRECOGNIZED": "legacy-private-value", "AC2_OTHER": "ac2-private"}),
            ("launcher .env", {}),
            ("service .env", {}),
        )
    )
    report_environment(ConfigResolver(MappingConfigSource({})).explain(), layers)
    output = capsys.readouterr().out
    assert "EAP_UNRECOGNIZED=********  [shell]" in output
    assert "AC2_OTHER=********  [shell]" in output
    assert "legacy-private-value" not in output
    assert "ac2-private" not in output


def test_legacy_tracing_names_resolve_and_report_under_the_canonical_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tools.local_environment import agent_environment

    from ai_agent_lib_core.config.phoenix import resolve_phoenix_settings

    monkeypatch.setattr("os.environ", {})
    folder = tmp_path / "agent"
    folder.mkdir()
    service, launcher = folder / ".env", tmp_path / ".env"
    service.write_text(
        "EAP_PHOENIX_ENABLED=true\nEAP_PHOENIX_ENV_FILE=tracing.env\n", encoding="utf-8"
    )
    launcher.write_text("", encoding="utf-8")
    trace_file = folder / "tracing.env"
    trace_file.write_text(
        "EAP_PHOENIX_CAPTURE_CONTENT=true\nPHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6007\n",
        encoding="utf-8",
    )
    environment = agent_environment(service, launcher, required=True, working_dir=tmp_path)
    resolved = resolve_phoenix_settings(
        service, environment=MappingConfigSource(environment), working_dir=tmp_path
    )
    assert resolved.settings is not None
    assert resolved.settings.capture_content
    output = capsys.readouterr().out
    assert f'AC2_PHOENIX_CAPTURE_CONTENT="true"  [{trace_file}' in output
    assert f'PHOENIX_COLLECTOR_ENDPOINT="http://127.0.0.1:6007"  [{trace_file}' in output
