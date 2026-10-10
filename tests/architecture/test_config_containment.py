"""Variable names and environment access stay inside the config package."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from architecture.containment import find_violations

from ai_agent_lib_core.config import DEFAULT_BINDINGS
from ai_agent_lib_core.config.bindings import PREFIX, SECRET_VARIABLE_PREFIX, secret_name

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


# ---------------------------------------------------------- what is not Python
#
# Documents and templates are allowed to name variables: a person has to be
# told what to set. What they may not do is name a variable that does not
# exist, which is how a renamed variable lives on in a guide.

_VARIABLE = re.compile(r"\b" + re.escape(PREFIX) + r"[A-Z0-9_]*[A-Z0-9]")


def _documents() -> list[Path]:
    patterns = (
        "*.md",
        ".env.example",
        "docs/**/*.md",
        "policies/**/*.md",
        "packages/*/README.md",
        "examples/**/README.md",
        "examples/**/.env.example",
    )
    found = {path for pattern in patterns for path in REPO_ROOT.glob(pattern)}
    # The target specification includes proposed variables; current release docs
    # and templates must use the implemented vocabulary. See docs/release-scope.md.
    return sorted(
        path
        for path in found
        if ".venv" not in path.parts and path != REPO_ROOT / "docs/Specification.md"
    )


def _templates() -> list[Path]:
    return sorted(REPO_ROOT.glob("packages/*/src/**/templates/**/*.jinja"))


def test_every_variable_a_document_names_exists() -> None:
    known = {name for binding in DEFAULT_BINDINGS for name in binding.all_names}
    documents = _documents()
    assert any(path.name == "getting-started.md" for path in documents)
    unknown = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()}: {name}"
        for path in documents
        for name in set(_VARIABLE.findall(path.read_text(encoding="utf-8")))
        # "<prefix>SECRET_<NAME>" is how a document writes the pattern of a secret's variable.
        if name not in known and secret_name(name) is None and name + "_" != SECRET_VARIABLE_PREFIX
    )
    assert not unknown, "these documents name variables that do not exist:\n" + "\n".join(unknown)


def test_no_template_spells_out_a_variable_name() -> None:
    # A template gets a name from the configuration package when it is rendered,
    # so generated projects follow a rename without the templates being touched.
    templates = _templates()
    assert templates, "the scan found no templates"
    spelled_out = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()}: {name}"
        for path in templates
        for name in set(_VARIABLE.findall(path.read_text(encoding="utf-8")))
    )
    assert not spelled_out, "\n".join(spelled_out)
