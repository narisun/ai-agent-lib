"""The committed configuration documents match the binding table."""

from __future__ import annotations

from pathlib import Path

import build_guide
import pytest

from ai_agent_lib_core.config import DEFAULT_BINDINGS, render_env_example, render_reference
from ai_agent_lib_core.contracts import NoOptions
from ai_agent_lib_core.di import ServiceProviders

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


def test_the_developer_guide_is_current() -> None:
    actual = (REPO_ROOT / "docs" / "developer-guide.html").read_text(encoding="utf-8")
    assert actual == build_guide.guide_html(), (
        "docs/developer-guide.html is stale; regenerate it with: "
        "uv run python docs/guide/build_guide.py"
    )


def test_the_guide_has_a_note_for_every_adapter_without_options() -> None:
    providers = ServiceProviders.default()
    without = {
        (port, name)
        for port in build_guide.PORTS
        for name in providers.names(port)
        if providers.lookup(port, name).options in (None, NoOptions)
    }
    assert without == set(build_guide.NO_OPTIONS_NOTES)


def test_every_variable_is_explained_in_the_binding_table() -> None:
    assert [binding.name for binding in DEFAULT_BINDINGS if not binding.details] == []
