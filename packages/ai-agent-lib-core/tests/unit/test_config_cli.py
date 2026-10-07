"""The module entry point that prints the generated configuration documents."""

from __future__ import annotations

import pytest

from ai_agent_lib_core.config import render_env_example, render_reference
from ai_agent_lib_core.config.__main__ import main


@pytest.mark.parametrize(
    ("argument", "expected"),
    [("reference", render_reference()), ("env-example", render_env_example())],
)
def test_it_prints_the_requested_document(
    argument: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([argument]) == 0
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("arguments", [[], ["unknown"], ["reference", "extra"]])
def test_it_explains_its_usage_on_bad_arguments(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err
