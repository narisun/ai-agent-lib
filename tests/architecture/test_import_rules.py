"""The module boundaries hold, and a planted violation of each rule is caught."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("ai_agent_lib_core", "ai_agent_lib_aws", "ai_agent_lib_cli")
_RUN_LINTER = (
    "import sys; from importlinter.cli import lint_imports; "
    "sys.exit(lint_imports(config_filename=sys.argv[1], no_cache=True))"
)


def _lint(search_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run import-linter with the repository's rules, optionally on a copy of the code."""
    env = dict(os.environ)  # the linter subprocess needs the caller's environment
    if search_path is not None:
        env["PYTHONPATH"] = str(search_path)
    return subprocess.run(  # noqa: S603 - fixed command, no untrusted input
        [sys.executable, "-c", _RUN_LINTER, str(REPO_ROOT / "pyproject.toml")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def code_copy(tmp_path: Path) -> Path:
    """A private copy of the three packages, safe to plant violations in."""
    for package in PACKAGES:
        distribution = package.replace("_", "-")
        shutil.copytree(REPO_ROOT / "packages" / distribution / "src" / package, tmp_path / package)
    return tmp_path


def test_the_real_code_keeps_every_import_rule() -> None:
    result = _lint()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout


@pytest.mark.parametrize(
    ("module", "planted_import", "broken_rule"),
    [
        (
            "ai_agent_lib_core/contracts/planted.py",
            "import ai_agent_lib_core.config",
            "contracts imports nothing else from the library",
        ),
        (
            "ai_agent_lib_core/pipeline/planted.py",
            "import ai_agent_lib_core.adapters",
            "config, pipeline and adapters import contracts only",
        ),
        (
            "ai_agent_lib_core/config/planted.py",
            "import ai_agent_lib_core.di",
            "config, pipeline and adapters stay below di and integrations",
        ),
        (
            "ai_agent_lib_core/adapters/planted.py",
            "import langgraph.graph",
            "only integrations.langgraph imports langgraph",
        ),
        (
            "ai_agent_lib_core/di/planted.py",
            "import boto3",
            "core never imports the AWS SDK, the AWS package or the CLI",
        ),
        (
            "ai_agent_lib_aws/planted.py",
            "import ai_agent_lib_cli",
            "no runtime package imports the CLI",
        ),
        (
            "ai_agent_lib_aws/planted.py",
            "import ai_agent_lib_core.adapters.data_duckdb",
            "a provider pack builds on contracts and the kit only",
        ),
        (
            "ai_agent_lib_aws/planted.py",
            "import ai_agent_lib_core.di.container",
            "a provider pack builds on contracts and the kit only",
        ),
        (
            "ai_agent_lib_core/di/planted.py",
            "import starlette.applications",
            "only integrations.http imports the HTTP server framework",
        ),
        (
            "ai_agent_lib_aws/planted.py",
            "import langgraph.graph",
            "in the AWS package only the checkpoint store imports langgraph",
        ),
        (
            "ai_agent_lib_core/adapters/planted.py",
            "import ai_agent_lib_core.kit",
            "config, pipeline and adapters stay below di and integrations",
        ),
    ],
)
def test_a_planted_bad_import_breaks_its_rule(
    code_copy: Path, module: str, planted_import: str, broken_rule: str
) -> None:
    (code_copy / module).write_text(f'"""Planted."""\n\n{planted_import}\n', encoding="utf-8")
    result = _lint(search_path=code_copy)
    assert result.returncode != 0, "the planted import was not detected"
    assert f"{broken_rule} BROKEN" in result.stdout
