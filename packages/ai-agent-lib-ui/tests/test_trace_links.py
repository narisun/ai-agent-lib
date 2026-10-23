"""Accept optional server-owned trace links without breaking legacy answers."""

import json
from pathlib import Path
from typing import Any

import pytest

from ai_agent_lib_core.contracts import Principal, Scope
from ai_agent_lib_ui.application.catalog import read_manifest
from ai_agent_lib_ui.application.question_answer import QuestionAnswerAdapter
from ai_agent_lib_ui.contracts.catalog import PresentationManifest
from ai_agent_lib_ui.contracts.conversations import TurnAdmission
from ai_agent_lib_ui.contracts.queries import ViewContext

FIXTURES = Path(__file__).parent / "fixtures"
SCOPE = Scope("tenant-1", "ada", "rmprep")
VIEW = ViewContext(principal=Principal(subject="ada", tenant="tenant-1"), scope=SCOPE)
TURN = TurnAdmission(
    scope=SCOPE, thread_id="thread-1", idempotency_key="send-1", question="A memo?"
)


def manifest() -> PresentationManifest:
    return read_manifest(json.loads((FIXTURES / "presentation.json").read_text()))


def envelope() -> dict[str, Any]:
    value: dict[str, Any] = json.loads((FIXTURES / "invoke.json").read_text())
    return value


TRACE_ID = "1234567890abcdef1234567890abcdef"
URL = f"https://traces.example/redirects/traces/{TRACE_ID}"
# The route a retired Langfuse integration emitted: no longer a valid trace link.
LEGACY_URL = f"https://traces.example/project/trial/traces/{TRACE_ID}"
LEGACY_TRACE = {"provider": "langfuse", "trace_id": TRACE_ID, "url": LEGACY_URL}


def phoenix(url: str) -> dict[str, str]:
    return {"provider": "phoenix", "trace_id": TRACE_ID, "url": url}


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"http://127.0.0.1:3000/redirects/traces/{TRACE_ID}",
        f"http://[::1]:3000/redirects/traces/{TRACE_ID}",
    ],
)
def test_optional_trace_links_are_correlated_with_accepted_envelope(url: str) -> None:
    value = envelope()
    value["output"]["trace"] = phoenix(url)
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is not None
    assert answer.trace.url == url
    assert answer.answer == value["output"]["answer"]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://remote.example",
        "https://user:secret@example.com",
        URL + "?secret=key",
        URL + "#x",
        URL + ")[bad](https://evil.example)",
        URL.replace(TRACE_ID, "0" * 32),
        "https://example.com\n/redirects/traces/" + TRACE_ID,
        URL.replace("traces.example", "traces.example:70000"),
        "\x00" + URL,
    ],
)
def test_malformed_trace_never_discards_answer(url: str) -> None:
    value = envelope()
    value["output"]["trace"] = phoenix(url)
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is None
    assert answer.answer == value["output"]["answer"]


def test_legacy_langfuse_trace_never_discards_answer() -> None:
    """An old agent still emitting a Langfuse link must not fail the answer."""
    value = envelope()
    plain = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    value["output"]["trace"] = LEGACY_TRACE
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is None
    assert answer.answer == value["output"]["answer"]
    assert answer == plain


@pytest.mark.parametrize("host", ["http://127.0.0.1:6006", "https://phoenix.example"])
def test_phoenix_trace_link_is_correlated_and_named(host: str) -> None:
    value = envelope()
    url = f"{host}/redirects/traces/{TRACE_ID}"
    value["output"]["trace"] = phoenix(url)
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is not None
    assert answer.trace.url == url
    assert answer.trace.label == "Agent Lab"
    assert answer.trace.model_dump()["provider"] == "phoenix"


@pytest.mark.parametrize(
    "url",
    [
        LEGACY_URL,
        f"http://remote.example/redirects/traces/{TRACE_ID}",
        f"http://127.0.0.1:6006/redirects/traces/{TRACE_ID}?token=private",
        f"http://user:secret@127.0.0.1:6006/redirects/traces/{TRACE_ID}",
        "http://127.0.0.1:6006/redirects/traces/" + "0" * 32,
    ],
)
def test_phoenix_rejects_wrong_routes_credentials_and_uncorrelated_ids(url: str) -> None:
    value = envelope()
    value["output"]["trace"] = phoenix(url)
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is None
    assert answer.answer == value["output"]["answer"]


@pytest.mark.parametrize("transport", ["json", "sse", "journal"])
@pytest.mark.parametrize("invalid_optional", ["none", "metrics", "trace", "legacy_trace"])
async def test_failed_response_retains_independently_valid_diagnostics(
    transport: str, invalid_optional: str
) -> None:
    from datetime import UTC, datetime

    import httpx

    from ai_agent_lib_ui.adapters.http import HttpDispatcher
    from ai_agent_lib_ui.contracts.catalog import AgentBinding
    from ai_agent_lib_ui.contracts.chat import StoredTurn
    from ai_agent_lib_ui.contracts.conversations import RunRecord

    now = datetime.now(UTC)
    run = RunRecord(
        scope=SCOPE,
        thread_id=TURN.thread_id,
        request_id="request",
        run_id="run",
        idempotency_key=TURN.idempotency_key,
        state="unknown" if transport == "journal" else "running",
        accepted_at=now,
        updated_at=now,
    )
    binding = AgentBinding(manifest=manifest(), base_url="http://127.0.0.1:8000")
    payload: dict[str, Any] = {
        "error": "invalid",
        "thread_id": run.thread_id,
        "request_id": run.request_id,
        "trace": {
            "provider": "phoenix",
            "trace_id": TRACE_ID,
            "url": f"http://127.0.0.1:6006/redirects/traces/{TRACE_ID}",
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "model_calls": 3,
        "elapsed_ms": 1234,
    }
    if invalid_optional == "metrics":
        payload["model_calls"] = -1
    if invalid_optional == "trace":
        payload["trace"]["url"] = "javascript:alert(1)"
    if invalid_optional == "legacy_trace":
        payload["trace"] = LEGACY_TRACE

    def reply(request: httpx.Request) -> httpx.Response:
        if transport == "sse":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="event: error\ndata: " + json.dumps(payload) + "\n\n",
            )
        return httpx.Response(200 if transport == "journal" else 400, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        dispatcher = HttpDispatcher(client, 10)
        result = (
            await dispatcher.outcome(VIEW, binding, StoredTurn(run=run, question=TURN.question))
            if transport == "journal"
            else await dispatcher.dispatch(VIEW, binding, TURN, run)
        )
    assert result is not None
    assert result.state == "failed"
    assert result.answer is None
    assert result.failure_code == "invalid"
    assert (result.trace is not None) is (invalid_optional not in {"trace", "legacy_trace"})
    assert result.model_calls == (None if invalid_optional == "metrics" else 3)
    assert result.elapsed_ms is not None


def stored_run(trace: object) -> str:
    """A completed run as an older UI would have stored it, with this trace payload."""
    from datetime import UTC, datetime

    from ai_agent_lib_core.contracts.diagnostics import TokenUsage
    from ai_agent_lib_ui.contracts.conversations import RunRecord

    now = datetime(2026, 10, 13, 12, tzinfo=UTC)  # Fixed, so two calls compare equal.
    run = RunRecord(
        scope=SCOPE,
        thread_id=TURN.thread_id,
        request_id="request-1",
        run_id="run-1",
        idempotency_key=TURN.idempotency_key,
        state="completed",
        accepted_at=now,
        updated_at=now,
        answer="Saved answer",
        usage=TokenUsage(kind="reported", input_tokens=120, output_tokens=30),
        model_calls=2,
        elapsed_ms=1500.0,
    )
    return json.dumps({**json.loads(run.model_dump_json()), "trace": trace})


def test_stored_run_with_legacy_trace_reloads_without_the_link() -> None:
    from ai_agent_lib_ui.contracts.conversations import RunRecord

    baseline = RunRecord.model_validate_json(stored_run(None))
    restored = RunRecord.model_validate_json(stored_run(LEGACY_TRACE))
    assert restored.trace is None
    assert restored.answer == "Saved answer"
    assert restored.usage.input_tokens == 120
    assert restored.usage.output_tokens == 30
    assert restored.model_calls == 2
    assert restored.elapsed_ms == 1500.0
    assert restored == baseline


@pytest.mark.parametrize(
    "trace",
    [
        "not-a-link",
        42,
        ["phoenix"],
        {},
        {"provider": "phoenix"},
        {"provider": "langfuse", "trace_id": "short", "url": LEGACY_URL},
        phoenix("javascript:alert(1)"),
        phoenix(URL + "?token=private"),
        {**phoenix(URL), "unexpected": "field"},
    ],
)
def test_stored_run_with_unusable_trace_reloads_without_the_link(trace: object) -> None:
    from ai_agent_lib_ui.contracts.conversations import RunRecord

    restored = RunRecord.model_validate_json(stored_run(trace))
    assert restored.trace is None
    assert restored.answer == "Saved answer"


def test_stored_run_keeps_a_valid_phoenix_trace() -> None:
    from ai_agent_lib_ui.contracts.conversations import RunRecord
    from ai_agent_lib_ui.contracts.traces import TraceLink

    restored = RunRecord.model_validate_json(stored_run(phoenix(URL)))
    assert restored.trace == TraceLink(provider="phoenix", trace_id=TRACE_ID, url=URL)
    assert RunRecord.model_validate_json(restored.model_dump_json()) == restored


def test_stored_run_keeps_a_valid_cloudwatch_trace() -> None:
    """A row saved for an agent on AgentCore Runtime reloads with its console link."""
    from ai_agent_lib_ui.contracts.conversations import RunRecord
    from ai_agent_lib_ui.contracts.traces import console_link

    link = console_link("eu-west-1", "session-" + TRACE_ID)
    restored = RunRecord.model_validate_json(stored_run(link.model_dump()))
    assert restored.trace == link
    assert restored.trace.label == "CloudWatch"
    assert RunRecord.model_validate_json(restored.model_dump_json()) == restored


def test_tolerant_trace_degrades_what_no_longer_validates() -> None:
    from ai_agent_lib_ui.contracts.traces import TraceLink, tolerant_trace

    link = TraceLink(provider="phoenix", trace_id=TRACE_ID, url=URL)
    assert tolerant_trace(None) is None
    assert tolerant_trace(LEGACY_TRACE) is None
    assert tolerant_trace("not-a-link") is None
    assert tolerant_trace(phoenix(URL.replace("traces.example", "traces.example:70000"))) is None
    assert tolerant_trace(phoenix(URL)) == link
    assert tolerant_trace(link) is link
