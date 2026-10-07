"""Principal and RequestContext are immutable and validated."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from ai_agent_lib_core.contracts import Classification, Principal, RequestContext, Scope


def _principal() -> Principal:
    return Principal(subject="u-1", tenant="t-9", roles=frozenset({"analyst"}))


def test_principal_is_immutable() -> None:
    principal = _principal()
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.tenant = "t-other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        principal.roles.add("admin")  # type: ignore[attr-defined]
    assert not hasattr(principal, "__dict__")


def test_principal_stores_immutable_copies_of_collections() -> None:
    roles = {"analyst"}
    chain = ["agent-a"]
    principal = Principal(
        subject="u-1",
        tenant="t-9",
        roles=roles,  # type: ignore[arg-type]
        delegation_chain=chain,  # type: ignore[arg-type]
    )
    roles.add("admin")
    chain.append("agent-b")
    assert principal.roles == frozenset({"analyst"})
    assert principal.delegation_chain == ("agent-a",)
    assert principal.has_role("analyst")
    assert not principal.has_role("admin")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"subject": "", "tenant": "t"},
        {"subject": "u", "tenant": " t"},
        {"subject": "u", "tenant": "t", "roles": frozenset({""})},
        {"subject": "u", "tenant": "t", "delegation_chain": ("ok", "bad\n")},
    ],
)
def test_principal_rejects_unusable_identifiers(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must not"):
        Principal(**kwargs)  # type: ignore[arg-type]


def test_request_context_is_immutable_and_derives_its_scope() -> None:
    context = RequestContext(
        principal=_principal(), application="accounts-agent", request_id="r-1", thread_id="th-1"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.application = "other"  # type: ignore[misc]
    assert context.scope == Scope(tenant="t-9", subject="u-1", application="accounts-agent")
    assert context.classification_ceiling is Classification.INTERNAL


def test_request_context_requires_a_principal() -> None:
    with pytest.raises(TypeError, match="Principal"):
        RequestContext(
            principal={"subject": "u-1"},  # type: ignore[arg-type]
            application="a",
            request_id="r",
            thread_id="t",
        )


def test_request_context_rejects_a_naive_deadline() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RequestContext(
            principal=_principal(),
            application="a",
            request_id="r",
            thread_id="t",
            deadline=datetime(2026, 1, 1),  # noqa: DTZ001 - the naive value is the point
        )
    aware = RequestContext(
        principal=_principal(),
        application="a",
        request_id="r",
        thread_id="t",
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert aware.deadline is not None


def test_classification_levels_are_ordered() -> None:
    assert (
        Classification.PUBLIC
        < Classification.INTERNAL
        < Classification.CONFIDENTIAL
        < Classification.RESTRICTED
    )
