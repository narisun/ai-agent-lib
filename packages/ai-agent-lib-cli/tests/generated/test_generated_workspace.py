"""What the commands generate works: its tests pass, and it is clean code.

A workspace is generated once, with every kind of service and a long name,
and its own tests, linter, formatter check and type check are run on it the
way a developer would run them. This is what keeps the templates and the
library from drifting apart: a change to the library that breaks generated
code fails here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.shared import read_servers

COMMANDS = (
    ("new", "mcp", "hello-mcp"),
    ("new", "agent", "hello-agent", "--mcp", "hello"),
    # A second pair, with long names, linked after the fact, and a real model provider.
    ("new", "agent", "customer-onboarding-agent", "--model", "anthropic"),
    ("new", "mcp", "claims-and-policy-lookup-mcp", "--server-id", "claims"),
    ("link", "customer-onboarding-agent", "claims"),
    ("link", "customer-onboarding-agent", "hello-mcp"),
)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    parent = tmp_path_factory.mktemp("generated")
    assert main(["init", "demo", "--dir", str(parent), "--owner", "demo-team"]) == 0
    root = parent / "demo"
    for command in COMMANDS:
        assert main([*command, "--workspace", str(root)]) == 0, command
    return root


def run_in(workspace: Path, *command: str) -> subprocess.CompletedProcess[str]:
    """Run a tool in the workspace, with its services importable as if installed."""
    sources = sorted(str(path) for path in workspace.glob("*/*/src"))
    environment = dict(os.environ)  # the tool needs the caller's environment to start at all
    environment["PYTHONPATH"] = os.pathsep.join(sources)
    environment.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(  # noqa: S603 - this interpreter and fixed arguments
        [sys.executable, "-m", *command],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def test_every_tool_of_every_server_was_pinned_from_the_running_code(workspace: Path) -> None:
    servers = read_servers(workspace)
    assert [server.id for server in servers] == ["claims", "hello"]
    for server in servers:
        assert all(tool.schema_sha256 for tool in server.tools), server.id
    # The same arguments give the same pin, whatever the server is called.
    assert [t.schema_sha256 for t in servers[0].tools] == [
        t.schema_sha256 for t in servers[1].tools
    ]


def test_the_generated_tests_pass(workspace: Path) -> None:
    done = run_in(workspace, "pytest", "-q", "-p", "no:cacheprovider")
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
    # 2 workspace tests, 2 servers x 6, 2 agents x 7, 3 links x 3.
    assert "37 passed" in done.stdout, done.stdout[-500:]


def test_the_generated_code_is_lint_clean_and_formatted(workspace: Path) -> None:
    lint = run_in(workspace, "ruff", "check", "--no-cache", ".")
    assert lint.returncode == 0, lint.stdout[-4000:]
    formatted = run_in(workspace, "ruff", "format", "--check", "--no-cache", ".")
    assert formatted.returncode == 0, formatted.stdout[-4000:]


@pytest.mark.integration
def test_the_generated_code_passes_a_strict_type_check(workspace: Path) -> None:
    done = run_in(workspace, "mypy", "--no-incremental", "--cache-dir", os.devnull)
    assert done.returncode == 0, done.stdout[-4000:]
    assert "no issues found" in done.stdout
