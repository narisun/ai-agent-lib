"""Startup output identifies winning settings without exposing credentials."""

import json
from pathlib import Path

import pytest
from tools.local_config_report import report_environment

from ai_agent_lib_core.config import ConfigResolver, LayeredConfigSource, MappingConfigSource


@pytest.mark.parametrize("backend", ["PHOENIX", "LANGFUSE"])
@pytest.mark.parametrize("from_launcher", [False, True])
def test_startup_uses_separate_tracing_file_and_child_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: str,
    from_launcher: bool,
) -> None:
    from tools.local_environment import agent_environment

    from ai_agent_lib_core.config.tracing import resolve_tracing_settings

    monkeypatch.setattr("os.environ", {})
    folder = tmp_path / "agent"
    folder.mkdir()
    service, launcher = folder / ".env", tmp_path / ".env"
    trace_file = (tmp_path if from_launcher else folder) / "tracing.env"
    endpoint = "PHOENIX_COLLECTOR_ENDPOINT" if backend == "PHOENIX" else "LANGFUSE_BASE_URL"
    service.write_text(
        f"EAP_{backend}_ENABLED=true\nEAP_{backend}_CAPTURE_CONTENT=false\n"
        f"{endpoint}=http://127.0.0.1:6006\n"
        + ("" if from_launcher else f"EAP_{backend}_ENV_FILE=tracing.env\n"),
        encoding="utf-8",
    )
    launcher.write_text(
        f"EAP_{backend}_ENV_FILE=tracing.env\n" if from_launcher else "", encoding="utf-8"
    )
    trace_file.write_text(
        f"EAP_{backend}_CAPTURE_CONTENT=true\n{endpoint}=http://127.0.0.1:6007\n"
        "PHOENIX_API_KEY=phoenix-private\nLANGFUSE_PROJECT_ID=trial\n"
        "LANGFUSE_PUBLIC_KEY=langfuse-public-private\nLANGFUSE_SECRET_KEY=langfuse-private\n",
        encoding="utf-8",
    )
    environment = agent_environment(service, launcher, required=True, working_dir=tmp_path)
    resolved = resolve_tracing_settings(
        service, environment=MappingConfigSource(environment), working_dir=tmp_path
    )
    settings = getattr(resolved, backend.lower()).settings
    assert settings.capture_content
    assert settings.base_url == "http://127.0.0.1:6007"
    output = capsys.readouterr().out
    assert f'{endpoint}="http://127.0.0.1:6007"  [{trace_file}' in output
    assert f'EAP_{backend}_CAPTURE_CONTENT="true"  [{trace_file}' in output
    assert "http://127.0.0.1:6006" not in next(
        line for line in output.splitlines() if f"{endpoint}=" in line
    )
    assert "phoenix-private" not in output
    assert "langfuse-private" not in output
    assert "langfuse-public-private" not in output


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
        {"EAP_MODEL_ID": "shell-model", "RMPREP_CONTEXT_INPUT_TOKENS": "64000"},
        {"EAP_MODEL_PROVIDER": "anthropic", "EAP_MODEL_ID": "file-model", "EAP_PROFILE": "local"},
        {"EAP_MODEL_PROVIDER": "fake", "EAP_MODEL_ID": "fake-model", "EAP_TELEMETRY": "off"},
    )
    output = capsys.readouterr().out
    assert 'EAP_PROFILE="local"  [launcher .env]' in output
    assert 'EAP_MODEL_ID="shell-model"  [shell]' in output
    assert 'EAP_MODEL_PROVIDER="anthropic"  [launcher .env]' in output
    assert 'EAP_TELEMETRY="off"  [service .env]' in output
    assert 'EAP_DEPLOYMENT_ENV="local"  [default]' in output
    assert 'RMPREP_CONTEXT_INPUT_TOKENS="64000"  [shell]' in output
    assert "file-model" not in output
    assert "fake-model" not in output


def test_startup_summary_masks_credentials_but_shows_public_json_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    protected = {
        "ANTHROPIC_API_KEY": "anthropic-private-value",
        "LANGFUSE_PUBLIC_KEY": "public-private-value",
        "LANGFUSE_SECRET_KEY": "langfuse-private-value",
        "PHOENIX_API_KEY": "phoenix-private-value",
        "AWS_ACCESS_KEY_ID": "aws-private-id",
        "AWS_SESSION_TOKEN": "aws-private-token",
        "OTEL_EXPORTER_OTLP_HEADERS": "x-custom-auth=otel-private-value",
        "EAP_SECRET_PAYMENTS": "payments-private-value",
        "MY_COMPANY_CREDENTIAL": "company-private-value",
    }
    options = {
        "EAP_MODEL_OPTIONS": json.dumps(
            {
                "anthropic": {
                    "prompt_caching": True,
                    "cache_ttl": "5m",
                    "custom-header": "model-private-value",
                }
            }
        ),
        "EAP_CHECKPOINT_OPTIONS": json.dumps(
            {"dsn": "postgres://user:database-private-value@host/db"}
        ),
        "EAP_IDENTITY_OPTIONS": json.dumps({"subject": "personal-identity", "roles": ["analyst"]}),
        "EAP_DATA_SOURCES": json.dumps(
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
    launcher = {"EAP_MODEL_OPTIONS": original}
    show({}, launcher, {"EAP_MODEL_OPTIONS": '{"anthropic": {"prompt_caching": false}}'})
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if line.startswith("  EAP_MODEL_OPTIONS="))
    shown = json.loads(line.split("=", 1)[1].rsplit("  [", 1)[0])
    assert shown == {
        "anthropic": {"prompt_caching": True, "max_tokens": 4096, "apiKey": "********"}
    }
    assert line.endswith("  [launcher .env]")
    assert launcher["EAP_MODEL_OPTIONS"] == original


def test_startup_summary_handles_aliases_empty_values_and_unrelated_shell_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    show(
        {
            "AWS_DEFAULT_REGION": "us-east-1",
            "UNRELATED_SECRET": "not-for-this-process",
            "PATH": "private-path",
        },
        {"LANGFUSE_SECRET_KEY": "", "HTTPS_PROXY": "http://user:private-proxy@proxy:8080"},
        {},
    )
    output = capsys.readouterr().out
    assert 'AWS_DEFAULT_REGION="us-east-1"  [shell]' in output
    assert "LANGFUSE_SECRET_KEY=(empty)" in output
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
    show({}, {"LANGFUSE_BASE_URL": value}, {})
    output = capsys.readouterr().out
    assert "LANGFUSE_BASE_URL=********  [launcher .env]" in output
    assert value not in output


def test_startup_summary_escapes_control_characters(capsys: pytest.CaptureFixture[str]) -> None:
    show({}, {"LANGFUSE_PROJECT_ID": "project\n\x1b[31msecond-line"}, {})
    output = capsys.readouterr().out
    assert r'LANGFUSE_PROJECT_ID="project\n\u001b[31msecond-line"' in output
    assert "\x1b" not in output
