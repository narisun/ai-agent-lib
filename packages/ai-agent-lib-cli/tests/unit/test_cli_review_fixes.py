"""Regression coverage for pip workspaces and safe generation."""

import ast
import json
import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.commands.install import installation_arguments
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.read_openapi import read_openapi
from ai_agent_lib_cli.render import write_files
from ai_agent_lib_cli.testing import toolbox_for_tests


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    tools = toolbox_for_tests(pins={})
    assert main(["init", "demo", "--owner", 'Team "A"', "--dir", str(tmp_path)], tools) == 0
    return tmp_path / "demo"


@pytest.mark.parametrize("provider", ["fake", "anthropic", "bedrock"])
def test_generated_dependencies_follow_the_selected_model(workspace: Path, provider: str) -> None:
    tools = toolbox_for_tests(pins={})
    assert (
        main(
            [
                "new",
                "agent",
                "helper",
                "--model",
                provider,
                "--workspace",
                str(workspace),
            ],
            tools,
        )
        == 0
    )
    project = tomllib.loads((workspace / "agents/helper/pyproject.toml").read_text())
    requirements = project["project"]["dependencies"]
    if provider == "anthropic":
        assert any(
            "anthropic" in item and item.startswith("ai-agent-lib-core[") for item in requirements
        )
    elif provider == "bedrock":
        assert any(item.startswith("ai-agent-lib-aws[bedrock]") for item in requirements)
    else:
        assert not any("anthropic" in item or "bedrock" in item for item in requirements)


def test_install_uses_one_pip_resolution_for_local_projects(workspace: Path) -> None:
    recorded: list[tuple[str, tuple[str, ...], Path]] = []

    def run(module: str, arguments: Sequence[str], folder: Path) -> int:
        recorded.append((module, tuple(arguments), folder))
        return 0

    tools = toolbox_for_tests(pins={}, run_tool=run)
    assert main(["new", "agent", "helper", "--workspace", str(workspace)], tools) == 0
    assert main(["install", "--workspace", str(workspace)], tools) == 0
    module, arguments, folder = recorded[-1]
    assert (module, arguments[0], folder) == ("pip", "install", workspace)
    assert str(workspace / "agents/helper") in arguments
    assert sum(item == "--editable" for item in arguments) == 4
    assert any(item.startswith("pytest") for item in arguments)
    assert not any(
        item.startswith("pytest") for item in installation_arguments(workspace, dev=False)
    )


def test_descriptions_are_literals_in_python_and_toml(workspace: Path) -> None:
    description = 'Quotes """ and \\paths\nwith café 🚀 and \x7f'
    tools = toolbox_for_tests(pins={})
    assert (
        main(
            [
                "new",
                "agent",
                "helper",
                "--description",
                description,
                "--workspace",
                str(workspace),
            ],
            tools,
        )
        == 0
    )
    folder = workspace / "agents/helper"
    assert (
        tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "description"
        ]
        == description
    )
    module = ast.parse((folder / "src/helper/__init__.py").read_text(encoding="utf-8"))
    assert ast.get_docstring(module, clean=False) == description
    for path in workspace.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"))
    yaml.safe_load((workspace / ".github/workflows/check.yml").read_text())


def test_openapi_keywords_are_python_symbols_with_external_query_names(tmp_path: Path) -> None:
    path = tmp_path / "api.json"
    response = {"type": "object", "properties": {"value": {"type": "string"}}}
    operation: dict[str, Any] = {
        "operationId": "class",
        "summary": 'A """quoted""" summary',
        "parameters": [
            {"name": "from", "in": "query", "required": True, "schema": {"type": "string"}}
        ],
        "responses": {"200": {"content": {"application/json": {"schema": response}}}},
    }
    path.write_text(json.dumps({"openapi": "3.1.0", "paths": {"/things": {"get": operation}}}))
    reading = read_openapi(path)
    (query,) = reading.queries
    assert query.name == "class_"
    assert query.signature == "from_: str"
    assert yaml.safe_load(query.definition)["parameters"]["from_"]["name"] == "from"
    operation["parameters"].append(
        {"name": "from_", "in": "query", "required": True, "schema": {"type": "string"}}
    )
    path.write_text(json.dumps({"openapi": "3.1.0", "paths": {"/things": {"get": operation}}}))
    assert "same name" in read_openapi(path).notes[0]


@pytest.mark.parametrize("route", ["class", "server", "source"])
def test_openapi_fallback_names_are_safe(tmp_path: Path, route: str) -> None:
    path = tmp_path / "api.json"
    path.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {
                    f"/{route}": {
                        "get": {
                            "responses": {
                                "200": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {"value": {"type": "string"}},
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
            }
        )
    )
    assert read_openapi(path).queries[0].name == route + "_"


@pytest.mark.parametrize(
    ("filename", "invalid"),
    [("broken.py", "def class(): pass"), ("broken.toml", 'a = "'), ("broken.yaml", "a: [")],
)
def test_invalid_generated_code_is_rejected_before_any_write(
    tmp_path: Path, filename: str, invalid: str
) -> None:
    with pytest.raises(CliError, match="generated"):
        write_files(tmp_path, {PurePosixPath("good.txt"): "keep", PurePosixPath(filename): invalid})
    assert list(tmp_path.iterdir()) == []
