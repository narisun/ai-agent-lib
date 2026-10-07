"""Variable names and environment access stay inside the config package."""

from __future__ import annotations

from pathlib import Path

import pytest
from architecture.containment import find_violations

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PACKAGE = Path("packages/ai-agent-lib-core/src/ai_agent_lib_core/config")


def _source_files_outside_config() -> list[Path]:
    files = sorted(REPO_ROOT.glob("packages/*/src/**/*.py"))
    return [path for path in files if CONFIG_PACKAGE not in path.relative_to(REPO_ROOT).parents]


def test_the_scan_covers_the_library_but_not_config() -> None:
    files = _source_files_outside_config()
    relative = {path.relative_to(REPO_ROOT).as_posix() for path in files}
    assert any("/contracts/" in name for name in relative)
    assert any("ai_agent_lib_aws" in name for name in relative)
    assert not any("/config/" in name for name in relative)


def test_no_module_outside_config_knows_a_variable_name_or_reads_the_environment() -> None:
    problems = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {message}"
        for path in _source_files_outside_config()
        for line, message in find_violations(path.read_text(encoding="utf-8"))
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("planted", "expected"),
    [
        ('import os\nvalue = os.environ["HOME"]\n', "os.environ"),
        ('import os\nvalue = os.getenv("HOME")\n', "os.getenv"),
        ('import os as system\nvalue = system.environ.get("HOME")\n', "os.environ"),
        ("from os import environ\n", "imports os.environ"),
        ("from os import getenv\n", "imports os.getenv"),
        ('NAME = "EAP_MODEL_PROVIDER"\n', "literal"),
        ('def f(section: str) -> str:\n    return f"EAP_{section}_PROVIDER"\n', "literal"),
        ('"""Set EAP_PROFILE to choose a profile."""\n', "literal"),
        ("from dotenv import dotenv_values\n", "imports dotenv"),
        ("import dotenv\n", "imports dotenv"),
    ],
)
def test_a_planted_violation_is_caught(planted: str, expected: str) -> None:
    violations = find_violations(planted)
    assert violations, "the planted violation was not detected"
    assert any(expected in message for _, message in violations)


@pytest.mark.parametrize(
    "clean",
    [
        "import os\nseparator = os.sep\n",
        'from pathlib import Path\nhome = Path("~").expanduser()\n',
        'label = "enterprise agentic platform"\n',
    ],
)
def test_ordinary_code_is_not_flagged(clean: str) -> None:
    assert find_violations(clean) == []
