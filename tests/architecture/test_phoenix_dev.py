"""The local Phoenix trial owns one environment and keeps data and ports local."""

import http.client
import json
import socket
import sqlite3
import sys
import time
import warnings
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from tools import local_processes, package_runtime, phoenix_dev, phoenix_server
from tools.phoenix_server import check_enabled_playground_dependencies, http_only


def test_setup_installs_packages_with_the_current_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = Mock()
    monkeypatch.setattr(package_runtime, "install", commands)
    phoenix_dev.setup(tmp_path)
    commands.assert_called_once_with(tmp_path, "phoenix", ["arize-phoenix==20.21.1"])


def test_server_environment_uses_local_sqlite_and_excludes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "ANTHROPIC_API_KEY",
        "LANGFUSE_SECRET_KEY",
        "PHOENIX_API_KEY",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "PHOENIX_SQL_DATABASE_URL",
    ):
        monkeypatch.setenv(key, "private-value")
    monkeypatch.setenv("PIP_CERT", "enterprise-cert.pem")
    values = phoenix_dev.server_environment(tmp_path)
    assert "private-value" not in str(values)
    assert values["PIP_CERT"] == "enterprise-cert.pem"
    assert (
        values["PHOENIX_SQL_DATABASE_URL"]
        == "sqlite:///" + (tmp_path / "data/phoenix.db").as_posix()
    )
    assert values["PHOENIX_HOST"] == "127.0.0.1"
    assert values["PHOENIX_TELEMETRY_ENABLED"] == "false"
    assert values["PHOENIX_DISABLE_AGENT_ASSISTANT"] == "true"


@pytest.mark.parametrize(
    "name",
    [
        "AC2_MODEL_PROVIDER",
        "AC2_PHOENIX_ENABLED",
        # The earlier prefix is still isolated while its spellings are accepted.
        "EAP_MODEL_PROVIDER",
        "EAP_PHOENIX_ENV_FILE",
        "RMPREP_CONTEXT_INPUT_TOKENS",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ],
)
def test_server_environment_isolates_agent_settings_in_both_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "private-value")
    monkeypatch.setenv("PIP_CERT", "enterprise-cert.pem")
    values = phoenix_dev.server_environment(tmp_path)
    assert name not in values
    assert "private-value" not in str(values)
    assert values["PIP_CERT"] == "enterprise-cert.pem"


def test_foreground_start_preserves_existing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_runtime.package_folder(tmp_path, "phoenix").mkdir(parents=True)
    database = tmp_path / ".agentlib/phoenix/data/phoenix.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"existing database")
    run = Mock()
    monkeypatch.setattr(phoenix_dev, "serve", run)
    phoenix_dev.start(tmp_path)
    server = run.call_args.args[0][0]
    assert server.port == 6006
    assert server.command[0] == sys.executable
    assert server.command[1:3] == ("-I", "-S")
    assert server.command[-4:] == ("--host", "127.0.0.1", "--port", "6006")
    assert run.call_args.args[1].parent == tmp_path / ".agentlib/local-runs"
    assert database.read_bytes() == b"existing database"


def test_local_trial_disables_code_sandboxes_without_installing_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An inherited playground setting must not launch optional binaries in the
    # local trace viewer. Standalone and stack startup share this environment.
    monkeypatch.setenv("PHOENIX_ALLOWED_SANDBOX_PROVIDERS", "MONTY,WASM")
    monkeypatch.setenv("PHOENIX_ENABLE_MCP_CODE_MODE", "true")
    environment = phoenix_dev.server_environment(tmp_path)
    assert environment["PHOENIX_ALLOWED_SANDBOX_PROVIDERS"] == "NONE"
    assert environment["PHOENIX_ENABLE_MCP_CODE_MODE"] == "false"
    assert environment["PHOENIX_DISABLE_AGENT_ASSISTANT"] == "true"
    assert environment["PHOENIX_PORT"] == "6006"


def test_trace_viewer_excludes_unused_model_generation_and_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHOENIX_ALLOWED_PROVIDERS", "AWS,ANTHROPIC")
    monkeypatch.setenv("PHOENIX_ENABLE_MCP_SERVER", "true")
    environment = phoenix_dev.server_environment(tmp_path)
    assert environment["PHOENIX_ALLOWED_PROVIDERS"] == "NONE"
    assert environment["PHOENIX_ENABLE_MCP_SERVER"] == "false"


@pytest.mark.parametrize(
    ("providers", "checked"),
    [
        (None, True),
        (frozenset({"AWS"}), True),
        (frozenset({"ANTHROPIC"}), False),
        (frozenset(), False),
    ],
)
def test_optional_bedrock_check_respects_enabled_providers(
    providers: frozenset[str] | None,
    checked: bool,
) -> None:
    check = Mock()
    app = SimpleNamespace(
        _warn_if_missing_aioboto3=check,
        get_env_allowed_providers=lambda: providers,
    )
    check_enabled_playground_dependencies(app)
    app._warn_if_missing_aioboto3()
    assert check.call_count == int(checked)


def test_enabled_dependency_check_keeps_warnings_and_errors_visible() -> None:
    def check() -> None:
        warnings.warn("Missing optional Bedrock client", UserWarning, stacklevel=2)
        raise RuntimeError("dependency check failed")

    app = SimpleNamespace(
        _warn_if_missing_aioboto3=check,
        get_env_allowed_providers=lambda: None,
    )
    filters = list(warnings.filters)
    showwarning = warnings.showwarning
    check_enabled_playground_dependencies(app)
    with (
        pytest.warns(UserWarning, match="Missing optional Bedrock client"),
        pytest.raises(RuntimeError, match="dependency check failed"),
    ):
        app._warn_if_missing_aioboto3()
    assert warnings.filters == filters
    assert warnings.showwarning is showwarning


def test_unverified_phoenix_version_fails_before_app_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phoenix_server, "version", lambda name: "99.0.0")
    imported = Mock()
    monkeypatch.setattr(phoenix_server, "import_module", imported)
    with pytest.raises(RuntimeError, match="verified Phoenix version"):
        phoenix_server.main()
    imported.assert_not_called()


@pytest.mark.integration
def test_installed_phoenix_collects_traces_without_unused_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    if not package_runtime.package_folder(root, "phoenix").is_dir():
        pytest.skip("Run start-phoenix.bat setup for the optional Phoenix startup check")
    protobuf = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    request = protobuf.ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(
        key="openinference.project.name"
    ).value.string_value = "trace-viewer-test"
    now = time.time_ns()
    span = resource.scope_spans.add().spans.add(
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        name="model",
        start_time_unix_nano=now,
        end_time_unix_nano=now + 1_000_000,
    )
    for key, value in {
        "openinference.span.kind": "LLM",
        "llm.provider": "anthropic",
        "input.value": "Synthetic input",
        "output.value": "Synthetic output",
        "agentlib.checkpoints": '[{"checkpoint_id":"synthetic-checkpoint"}]',
    }.items():
        span.attributes.add(key=key).value.string_value = value
    span.attributes.add(key="llm.token_count.prompt").value.int_value = 10
    span.attributes.add(key="llm.token_count.completion").value.int_value = 5
    (tmp_path / "data").mkdir()
    environment = phoenix_dev.server_environment(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment["PHOENIX_PORT"] = str(port)
    configured = phoenix_dev.service(root)
    service = replace(
        configured,
        port=port,
        command=(*configured.command[:-1], str(port)),
        environment=environment,
    )
    ready = local_processes.is_ready

    def check_trace_viewer(candidate: local_processes.Service) -> bool:
        if not ready(candidate):
            return False
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", "/v1/model_providers")
            with connection.getresponse() as response:
                assert response.status == 200
                assert json.loads(response.read())["data"] == []
            # GET routes fall back to Phoenix's web app. POST must not reach
            # an MCP handler when that optional endpoint is disabled.
            connection.request("POST", "/mcp", b"{}", {"Content-Type": "application/json"})
            with connection.getresponse() as response:
                response.read()
                assert response.status == 405
            # A malformed span must remain an error; then accept a valid trace
            # from an agent even though Playground providers are disabled.
            for payload, status in [(b"\xff", 422), (request.SerializeToString(), 200)]:
                connection.request(
                    "POST",
                    "/v1/traces",
                    payload,
                    {"Content-Type": "application/x-protobuf"},
                )
                with connection.getresponse() as response:
                    assert response.status == status
                    response.read()
        finally:
            connection.close()
        return True

    monkeypatch.setattr(local_processes, "is_ready", check_trace_viewer)
    logs = tmp_path / "logs"
    local_processes.serve([service], logs, smoke_test=True)
    output = (logs / "phoenix.log").read_text(encoding="utf-8")
    assert "Code sandboxes      Disabled:" in output
    assert "Application startup complete." in output
    assert "Application shutdown complete." in output
    assert "Monty worker pool" not in output
    assert "MCP server          Disabled" in output
    assert "PydanticJsonSchemaWarning" not in output
    assert "boto3 is installed but aioboto3 is not" not in output
    assert "ConnectionResetError" not in output
    assert "Traceback" not in output
    database = tmp_path / "data/phoenix.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT attributes, llm_token_count_prompt, llm_token_count_completion "
            "FROM spans WHERE span_id=?",
            ("02" * 8,),
        ).fetchone()
    assert row is not None
    attributes = json.loads(row[0])
    assert attributes["llm"]["provider"] == "anthropic"
    assert attributes["input"]["value"] == "Synthetic input"
    assert attributes["output"]["value"] == "Synthetic output"
    assert "synthetic-checkpoint" in attributes["agentlib"]["checkpoints"]
    assert row[1:] == (10, 5)


async def test_http_only_compatibility_shim_skips_wildcard_grpc_listener() -> None:
    class Collector:
        _disabled = False
        listening = False

        async def __aenter__(self) -> None:
            if not self._disabled:
                self.listening = True

        async def __aexit__(self, *args: Any) -> None:
            self.listening = False

    http_only(Collector)
    collector = Collector()
    async with collector:
        assert collector._disabled
        assert not collector.listening
