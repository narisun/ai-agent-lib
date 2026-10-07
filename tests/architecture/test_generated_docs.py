"""The committed configuration documents match the binding table."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_core.config import render_env_example, render_reference

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("path", "expected", "command"),
    [
        ("docs/variables.md", render_reference(), "reference > docs/variables.md"),
        (".env.example", render_env_example(), "env-example > .env.example"),
    ],
)
def test_generated_document_is_current(path: str, expected: str, command: str) -> None:
    actual = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert actual == expected, (
        f"{path} is stale; regenerate it with: python -m ai_agent_lib_core.config {command}"
    )
