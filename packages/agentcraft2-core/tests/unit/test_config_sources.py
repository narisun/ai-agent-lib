"""Configuration sources take snapshots and layer predictably."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcraft2_core.config import (
    DotenvConfigSource,
    EnvironConfigSource,
    LayeredConfigSource,
    MappingConfigSource,
)
from agentcraft2_core.contracts import ConfigurationError


def test_mapping_source_keeps_a_copy() -> None:
    values = {"AC2_PROFILE": "local"}
    source = MappingConfigSource(values)
    values["AC2_PROFILE"] = "aws"
    assert source.get("AC2_PROFILE") == "local"
    assert source.get("MISSING") is None
    assert tuple(source.names()) == ("AC2_PROFILE",)


def test_environment_source_is_a_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC2_PROFILE", "local")
    source = EnvironConfigSource()
    monkeypatch.setenv("AC2_PROFILE", "aws")
    monkeypatch.setenv("AC2_ADDED_LATER", "x")
    assert source.get("AC2_PROFILE") == "local"
    assert "AC2_ADDED_LATER" not in source.names()


def test_dotenv_source_parses_comments_quotes_and_json(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment line",
                "AC2_PROFILE=local                  # trailing comment",
                'AC2_MODEL_ID="model with spaces"',
                'AC2_AUDIT_OPTIONS={"path": "audit.jsonl", "fsync": true}',
                "export AWS_PROFILE=dev-sso",
                "AC2_EMPTY=",
            ]
        ),
        encoding="utf-8",
    )
    source = DotenvConfigSource(env_file)
    assert source.get("AC2_PROFILE") == "local"
    assert source.get("AC2_MODEL_ID") == "model with spaces"
    assert source.get("AC2_AUDIT_OPTIONS") == '{"path": "audit.jsonl", "fsync": true}'
    assert source.get("AWS_PROFILE") == "dev-sso"
    assert source.get("AC2_EMPTY") == ""


def test_dotenv_source_does_not_touch_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AC2_FROM_FILE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("AC2_FROM_FILE=1\n", encoding="utf-8")
    DotenvConfigSource(env_file)
    assert EnvironConfigSource().get("AC2_FROM_FILE") is None


def test_missing_dotenv_file_is_empty_unless_required(tmp_path: Path) -> None:
    missing = tmp_path / "nope.env"
    assert tuple(DotenvConfigSource(missing).names()) == ()
    with pytest.raises(ConfigurationError, match="not found"):
        DotenvConfigSource(missing, required=True)


def test_layered_source_prefers_earlier_layers() -> None:
    source = LayeredConfigSource(
        MappingConfigSource({"A": "from-env"}),
        MappingConfigSource({"A": "from-file", "B": "from-file"}),
    )
    assert source.get("A") == "from-env"
    assert source.get("B") == "from-file"
    assert source.get("C") is None
    assert tuple(source.names()) == ("A", "B")


def test_layered_source_needs_at_least_one_layer() -> None:
    with pytest.raises(ValueError, match="at least one"):
        LayeredConfigSource()
