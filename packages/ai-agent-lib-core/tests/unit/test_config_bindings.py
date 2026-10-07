"""The binding table is well formed and drives the generated documents."""

from __future__ import annotations

import pytest

from ai_agent_lib_core.config import (
    DEFAULT_BINDINGS,
    PREFIX,
    Binding,
    render_env_example,
    render_reference,
)
from ai_agent_lib_core.config.bindings import validate_bindings
from ai_agent_lib_core.contracts import Section


def test_prefix_is_eap() -> None:
    assert PREFIX == "EAP_"


def test_every_section_has_a_selector_and_an_options_variable() -> None:
    names = {binding.name for binding in DEFAULT_BINDINGS}
    for section in Section:
        assert f"EAP_{section.name}_PROVIDER" in names
        assert f"EAP_{section.name}_OPTIONS" in names


def test_third_party_variables_keep_their_own_names() -> None:
    external = {binding.name for binding in DEFAULT_BINDINGS if not binding.owned}
    assert external == {
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_CA_BUNDLE",
        "AWS_EXECUTION_ENV",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ANTHROPIC_API_KEY",
    }


def test_credentials_are_marked_sensitive() -> None:
    sensitive = {binding.name for binding in DEFAULT_BINDINGS if binding.sensitive}
    assert {"ANTHROPIC_API_KEY", "HTTPS_PROXY"} <= sensitive


@pytest.mark.parametrize(
    ("bindings", "problem"),
    [
        ([Binding("EAP_A", "k", "d"), Binding("EAP_B", "k", "d")], "key 'k'"),
        ([Binding("EAP_A", "k1", "d"), Binding("EAP_A", "k2", "d")], "variable 'EAP_A'"),
        (
            [Binding("EAP_A", "k1", "d"), Binding("EAP_B", "k2", "d", deprecated_names=("EAP_A",))],
            "variable 'EAP_A'",
        ),
        ([Binding("EAP_lower", "k", "d")], "malformed"),
        ([Binding("EAP__DOUBLE", "k", "d")], "malformed"),
        ([Binding("EAP_A", "k", "d", deprecated_names=("has space",))], "malformed"),
    ],
)
def test_malformed_tables_are_rejected(bindings: list[Binding], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        validate_bindings(bindings)


def test_reference_lists_every_variable_once() -> None:
    reference = render_reference()
    for binding in DEFAULT_BINDINGS:
        assert reference.count(f"| `{binding.name}` |") == 1
    assert "also read from `AWS_DEFAULT_REGION`" in reference
    assert reference.startswith("<!-- Generated")


def test_reference_shows_deprecated_names() -> None:
    table = [Binding("EAP_NEW", "k", "The setting.", deprecated_names=("EAP_OLD",))]
    assert "replaces `EAP_OLD`" in render_reference(table)


def test_env_example_lists_every_variable_commented_out() -> None:
    example = render_env_example()
    for binding in DEFAULT_BINDINGS:
        assert f"# {binding.name}=" in example
    assert all(line.startswith("#") or not line for line in example.splitlines())
