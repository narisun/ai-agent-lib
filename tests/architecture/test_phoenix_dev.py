"""The local Phoenix trial owns one environment and keeps data and ports local."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tools import phoenix_dev
from tools.local_dev import interpreter
from tools.phoenix_server import http_only


def test_setup_uses_pip_and_separate_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = Mock()
    monkeypatch.setattr(phoenix_dev, "run", commands)
    phoenix_dev.setup(tmp_path)
    calls = [call.args[0] for call in commands.call_args_list]
    assert calls[0][-3:] == ["-m", "venv", str(tmp_path / ".agentlib/phoenix/.venv")]
    assert calls[1][1:] == ["-m", "pip", "install", "arize-phoenix==20.22.0"]
    assert calls[2][1:] == ["-m", "pip", "check"]


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


def test_foreground_start_preserves_existing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = interpreter(tmp_path / ".agentlib/phoenix/.venv")
    python.parent.mkdir(parents=True)
    python.touch()
    database = tmp_path / ".agentlib/phoenix/data/phoenix.db"
    database.parent.mkdir()
    database.write_bytes(b"existing database")
    run = Mock()
    monkeypatch.setattr(phoenix_dev, "serve", run)
    phoenix_dev.start(tmp_path)
    server = run.call_args.args[0][0]
    assert server.port == 6006
    assert server.command[0] == str(python)
    assert server.command[-4:] == ("--host", "127.0.0.1", "--port", "6006")
    assert run.call_args.args[1].parent == tmp_path / ".agentlib/local-runs"
    assert database.read_bytes() == b"existing database"


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
