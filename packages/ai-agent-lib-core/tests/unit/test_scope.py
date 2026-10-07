"""Scoped keys never collide and always round-trip."""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ai_agent_lib_core.contracts import Scope

# Identifiers as the library accepts them: printable, non-empty, not padded.
# The alphabet deliberately includes the key separator and percent signs.
identifier = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "S"), include_characters="/% "),
    min_size=1,
    max_size=12,
).filter(lambda text: text == text.strip() and text.isprintable())

scope_parts = st.tuples(identifier, identifier, identifier, st.lists(identifier, max_size=3))


def _key(parts: tuple[str, str, str, list[str]]) -> str:
    tenant, subject, application, extra = parts
    return Scope(tenant, subject, application).key(*extra)


@given(scope_parts, scope_parts)
def test_two_different_scopes_never_share_a_key(
    left: tuple[str, str, str, list[str]], right: tuple[str, str, str, list[str]]
) -> None:
    assert (_key(left) == _key(right)) == (left == right)


@given(scope_parts)
def test_a_key_parses_back_to_its_scope_and_parts(parts: tuple[str, str, str, list[str]]) -> None:
    tenant, subject, application, extra = parts
    scope = Scope(tenant, subject, application)
    assert Scope.parse_key(scope.key(*extra)) == (scope, tuple(extra))


def test_separator_characters_in_identifiers_cannot_forge_another_scope() -> None:
    # Without encoding, both of these would produce the key "a/b/c/d".
    assert Scope("a/b", "c", "d").key() != Scope("a", "b/c", "d").key()
    assert Scope("a", "b", "c").key("d") != Scope("a", "b", "c/d").key()


def test_namespace_is_the_scope_followed_by_the_parts() -> None:
    assert Scope("t", "s", "app").namespace("memory", "notes") == (
        "t",
        "s",
        "app",
        "memory",
        "notes",
    )


def test_scope_is_immutable() -> None:
    scope = Scope("t", "s", "app")
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.tenant = "other"  # type: ignore[misc]
    assert not hasattr(scope, "__dict__")


@pytest.mark.parametrize("bad", ["", " padded", "padded ", "line\nbreak", "tab\tbed"])
def test_unusable_identifiers_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="tenant"):
        Scope(bad, "s", "app")
    with pytest.raises(ValueError, match="parts"):
        Scope("t", "s", "app").key(bad)


def test_non_string_identifiers_are_rejected() -> None:
    with pytest.raises(TypeError, match="subject"):
        Scope("t", 42, "app")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_key", ["", "only/two", "a/b/c/", "a/b//c"])
def test_parse_key_rejects_keys_it_did_not_build(bad_key: str) -> None:
    with pytest.raises(ValueError, match=r"scoped key|must not"):
        Scope.parse_key(bad_key)
