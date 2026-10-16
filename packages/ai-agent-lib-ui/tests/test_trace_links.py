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
URL = f"https://traces.example/project/trial/traces/{TRACE_ID}"


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"http://127.0.0.1:3000/project/trial/traces/{TRACE_ID}",
        f"http://[::1]:3000/project/trial/traces/{TRACE_ID}",
    ],
)
def test_optional_trace_links_are_correlated_with_accepted_envelope(url: str) -> None:
    value = envelope()
    value["output"]["trace"] = {"provider": "langfuse", "trace_id": TRACE_ID, "url": url}
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
        "https://example.com\n/project/trial/traces/" + TRACE_ID,
        URL.replace("traces.example", "traces.example:70000"),
        "\x00" + URL,
    ],
)
def test_malformed_trace_never_discards_answer(url: str) -> None:
    value = envelope()
    value["output"]["trace"] = {"provider": "langfuse", "trace_id": TRACE_ID, "url": url}
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is None
    assert answer.answer == value["output"]["answer"]


@pytest.mark.parametrize("host", ["http://127.0.0.1:6006", "https://phoenix.example"])
def test_phoenix_trace_link_is_correlated_and_named(host: str) -> None:
    value = envelope()
    url = f"{host}/redirects/traces/{TRACE_ID}"
    value["output"]["trace"] = {"provider": "phoenix", "trace_id": TRACE_ID, "url": url}
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is not None
    assert answer.trace.url == url
    assert answer.trace.label == "Phoenix"
    assert answer.trace.model_dump()["provider"] == "phoenix"


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"http://remote.example/redirects/traces/{TRACE_ID}",
        f"http://127.0.0.1:6006/redirects/traces/{TRACE_ID}?token=private",
        f"http://user:secret@127.0.0.1:6006/redirects/traces/{TRACE_ID}",
        "http://127.0.0.1:6006/redirects/traces/" + "0" * 32,
    ],
)
def test_phoenix_rejects_wrong_routes_credentials_and_uncorrelated_ids(url: str) -> None:
    value = envelope()
    value["output"]["trace"] = {"provider": "phoenix", "trace_id": TRACE_ID, "url": url}
    answer = QuestionAnswerAdapter(manifest()).response(VIEW, TURN, json.dumps(value).encode())
    assert answer.trace is None
    assert answer.answer == value["output"]["answer"]


@pytest.mark.parametrize("transport", ["json", "sse", "journal"])
@pytest.mark.parametrize("invalid_optional", ["none", "metrics", "trace"])
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
    assert (result.trace is not None) is (invalid_optional != "trace")
    assert result.model_calls == (None if invalid_optional == "metrics" else 3)
    assert result.elapsed_ms is not None
