"""``agentcraft2 config schema`` prints the schema of the developer's settings file."""

from __future__ import annotations

import json

import pytest

from agentcraft2_cli.__main__ import main
from agentcraft2_cli.testing import toolbox_for_tests


def test_the_schema_command_prints_json_for_an_editor(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "schema"], toolbox_for_tests()) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "agentcraft2.settings.toml"
    assert "model" in schema["properties"]
    # What a deployment decides is not something the developer's file can say.
    assert not {"identity", "audit", "secrets", "checkpoint"} & set(schema["properties"])
