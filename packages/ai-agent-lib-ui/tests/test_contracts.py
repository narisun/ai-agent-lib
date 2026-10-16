"""Serialized public contracts remain bounded, scoped and framework independent."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_agent_lib_core.contracts import AgentEntry, Principal, Scope
from ai_agent_lib_ui.application.catalog import bind_agent, read_manifest, validate_destination
from ai_agent_lib_ui.application.question_answer import QuestionAnswerAdapter
from ai_agent_lib_ui.contracts.artifacts import Artifact
from ai_agent_lib_ui.contracts.catalog import PresentationManifest
from ai_agent_lib_ui.contracts.conversations import TurnAdmission, require_transition
from ai_agent_lib_ui.contracts.queries import (
    Availability,
    ContentPreview,
    ContractError,
    ErrorCode,
    PageRequest,
    QueryBinding,
    ViewContext,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCOPE = Scope("tenant-1", "ada", "rmprep")
VIEW = ViewContext(principal=Principal(subject="ada", tenant="tenant-1"), scope=SCOPE)
TURN = TurnAdmission(
    scope=SCOPE, thread_id="thread-1", idempotency_key="send-1", question="A memo?"
)


def manifest() -> PresentationManifest:
    return read_manifest(json.loads((FIXTURES / "presentation.json").read_text()))


def envelope() -> dict[str, Any]:
    return json.loads((FIXTURES / "invoke.json").read_text())  # type: ignore[no-any-return]


def test_manifest_fixture_round_trip_and_unknown_optional_capability() -> None:
    value = manifest()
    assert json.loads(value.model_dump_json(by_alias=True)) == json.loads(
        (FIXTURES / "presentation.json").read_text()
    )
    assert value.capability("future_optional_feature") == Availability.UNSUPPORTED
    assert value.capability("artifacts") == Availability.AVAILABLE


def test_legacy_registry_and_invoke_need_no_new_fields() -> None:
    entry = AgentEntry(id="rmprep", owner="bank", version="0.1.0", url="http://127.0.0.1:8100")
    assert entry.url is not None
    bound = bind_agent(
        manifest(),
        entry,
        registry_revision="revision-1",
        allowed_urls=frozenset({entry.url}),
        local=True,
    )
    adapter = QuestionAnswerAdapter(bound.manifest)
    assert adapter.request(VIEW, TURN) == {
        "input": {"question": "A memo?"},
        "thread_id": "thread-1",
    }
    body = b'{"output":{"answer":"hello"},"thread_id":"thread-1","request_id":"request-1"}'
    assert adapter.response(VIEW, TURN, body).answer == "hello"
    followup = TURN.model_copy(update={"question": "More details?", "idempotency_key": "send-2"})
    assert adapter.request(VIEW, followup) == {
        "input": {"question": "More details?"},
        "thread_id": "thread-1",
    }


@pytest.mark.parametrize(
    "change", [{"agent_version": "2"}, {"registry_revision": "2"}, {"agent_id": "other"}]
)
def test_stale_bindings_fail_before_dispatch(change: dict[str, str]) -> None:
    with pytest.raises(ContractError) as caught:
        bind_agent(
            manifest().model_copy(update=change),
            AgentEntry(id="rmprep", owner="bank", version="0.1.0"),
            registry_revision="revision-1",
            allowed_urls=frozenset(),
        )
    assert caught.value.code == ErrorCode.STALE_BINDING


@pytest.mark.parametrize(
    "url",
    [
        "file:///secrets",
        "http://remote.example",
        "https://user:pass@agent.example",
        "https://agent.example?key=secret",
        "https://agent.example#fragment",
        "https://agent.example/../private",
        "https://agent.example:70000",
        "https://agent.example:0",
        "http://169.254.169.254",
        "http://localhost",
        "http://127.0.0.1%2f@remote.example",
        "https://agent.example\\evil",
        "https://agent.example\n",
        "\x00https://agent.example",
    ],
)
def test_invalid_destination_even_if_allowlisted(url: str) -> None:
    with pytest.raises(ContractError, match="destination_denied"):
        validate_destination(url, allowed_urls=frozenset({url}), local=True)


def test_destination_must_be_server_allowlisted_and_local_http_is_explicit() -> None:
    for allowed, local in [(frozenset(), True), (frozenset({"http://127.0.0.1:8100"}), False)]:
        with pytest.raises(ContractError, match="destination_denied"):
            validate_destination("http://127.0.0.1:8100", allowed_urls=allowed, local=local)
    assert (
        validate_destination(
            "https://agent.example", allowed_urls=frozenset({"https://agent.example"}), local=False
        )
        == "https://agent.example"
    )


def test_schema_and_adapter_versions_fail_explicitly() -> None:
    with pytest.raises(ContractError, match="unsupported_schema"):
        read_manifest({"schema": "agentlib.presentation/v2"})
    with pytest.raises(ContractError, match="unsupported_adapter"):
        QuestionAnswerAdapter(manifest().model_copy(update={"adapter": "arbitrary-json/v1"}))


@pytest.mark.parametrize("field", ["tenant", "subject", "application"])
def test_scope_changes_never_reuse_thread_or_cursor(field: str) -> None:
    other = replace(SCOPE, **{field: "other"})
    with pytest.raises(ContractError, match="not_found"):
        QuestionAnswerAdapter(manifest()).request(VIEW, TURN.model_copy(update={"scope": other}))
    query = QueryBinding(scope=SCOPE, resource="diagnostics", thread_id="thread-1")
    assert not query.matches(query.model_copy(update={"scope": other}))
    assert not query.matches(query.model_copy(update={"filters": (("event", "model.request"),)}))


def test_scope_requires_the_authenticated_owner() -> None:
    with pytest.raises(ValidationError, match="caller does not own"):
        ViewContext(principal=Principal(subject="bob", tenant="tenant-1"), scope=SCOPE)


def test_artifacts_are_bound_to_the_verified_invocation() -> None:
    answer = QuestionAnswerAdapter(manifest()).response(
        VIEW, TURN, (FIXTURES / "invoke.json").read_bytes()
    )
    assert answer.artifact_status == Availability.AVAILABLE
    assert answer.artifacts[0].scope == SCOPE
    assert answer.artifacts[0].artifact.version == 1


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "agentlib.artifact/v2"},
        {"request_id": "different"},
        {"thread_id": "different"},
        {"scope": {"tenant": "other"}},
        {"content": None, "content_ref": "../../secrets"},
        {"content": None, "content_ref": "https://private.example"},
        {"content": "x" * 262_145},
    ],
)
def test_bad_optional_artifact_preserves_chat(change: dict[str, object]) -> None:
    value = envelope()
    value["output"]["artifacts"][0].update(change)
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.answer
    assert answer.artifacts == ()
    assert answer.artifact_status == Availability.UNAVAILABLE


def test_unadvertised_artifacts_are_ignored() -> None:
    adapter = QuestionAnswerAdapter(manifest().model_copy(update={"capabilities": ()}))
    answer = adapter.response(VIEW, TURN, (FIXTURES / "invoke.json").read_bytes())
    assert answer.artifact_status == Availability.UNSUPPORTED
    assert not answer.artifacts


@pytest.mark.parametrize(
    "body", [b"{}", b'{"output":{"answer":42}}', b"not-json", b'{"event":"update"}']
)
def test_progress_and_invalid_envelopes_never_become_answers(body: bytes) -> None:
    with pytest.raises(ContractError, match="invalid_response"):
        QuestionAnswerAdapter(manifest()).response(VIEW, TURN, body)


def test_deeply_nested_json_is_a_safe_invalid_response() -> None:
    with pytest.raises(ContractError, match="invalid_response"):
        QuestionAnswerAdapter(manifest()).response(VIEW, TURN, b"[" * 2000 + b"]" * 2000)


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1},
        {"input_tokens": True},
        {"input_tokens": 2, "cache_read_tokens": 3},
        "invalid",
    ],
)
def test_invalid_optional_usage_preserves_the_accepted_answer(usage: object) -> None:
    body = envelope()
    body["usage"] = usage
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(body).encode())
    assert answer.answer == body["output"]["answer"]
    assert answer.usage.input_tokens is None


def test_bounds_and_explicit_unknown_state() -> None:
    assert PageRequest().limit == 50
    with pytest.raises(ValidationError):
        PageRequest(limit=201)
    with pytest.raises(ValidationError):
        ContentPreview(text="日" * 22_000, truncated=False)
    with pytest.raises(ContractError, match="conflict"):
        require_transition("unknown", "running")
    with pytest.raises(ContractError, match="conflict"):
        require_transition("unknown", "completed")
    require_transition("unknown", "completed", reconciled=True)
    raw = envelope()["output"]["artifacts"][0]
    raw["content_ref"] = "opaque-key"
    with pytest.raises(ValidationError, match="exactly one"):
        Artifact.model_validate(raw)
