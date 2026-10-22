"""Diagnostic reads remain bounded, authorized and tied to one exact turn."""

import json
from dataclasses import replace
from pathlib import Path
from typing import TypedDict

import httpx
import pytest
from langgraph.graph import END, START, StateGraph

from ai_agent_lib_core.contracts import PolicyDenied, Principal, RequestContext
from ai_agent_lib_core.contracts.inspection import TurnInspection
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app
from ai_agent_lib_core.integrations.langgraph.inspection import GraphCheckpointPreview
from ai_agent_lib_core.observability.inspection import LocalTurnInspector, read_records
from ai_agent_lib_core.testing import FakeIdentityVerifier, Fakes


def owner() -> RequestContext:
    return RequestContext(
        principal=Principal(tenant="bank", subject="developer"),
        application="memo",
        thread_id="thread",
        request_id="request",
    )


def record(event: str = "model.request", **changes: object) -> dict[str, object]:
    return {
        "schema": "agentlib.audit/v1" if event == "tool.call" else "agentlib.model-trace/v1",
        "event": event,
        "tenant": "bank",
        "subject": "developer",
        "application": "memo",
        "thread_id": "thread",
        "request_id": "request",
        "content": "only this turn",
        **changes,
    }


def write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


@pytest.mark.parametrize("field", ["tenant", "subject", "application", "thread_id", "request_id"])
def test_reader_never_joins_by_request_id_alone(tmp_path: Path, field: str) -> None:
    path = tmp_path / "trace.jsonl"
    write(path, [record(), record(**{field: "other", "content": "hidden"}), record()])
    result = read_records((path,), owner(), "request", event="model.request")
    assert result.record_count == 1
    assert "only this turn" in result.text
    assert "hidden" not in result.text


async def test_inputs_outputs_tools_and_missing_capture_are_distinct(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    write(path, [record(), record("model.response"), record("model.error"), record("tool.call")])

    async def authorize(context: RequestContext) -> None:
        assert context == owner()

    result = await LocalTurnInspector(
        authorize=authorize, model_paths=(path,), audit_paths=(path,)
    ).read(owner(), "request")
    assert result.model_inputs.record_count == 1
    assert result.model_outputs.record_count == 2
    assert result.mcp.record_count == 1
    assert result.checkpoints.availability == "unsupported"
    missing = await LocalTurnInspector(authorize=authorize).read(owner(), "request")
    assert missing.model_inputs.availability == "disabled"
    assert missing.mcp.availability == "disabled"


def test_long_and_partial_records_are_bounded_and_labelled(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    write(path, [record(content="界" * 80_000), record("model.response", content="answer")])
    result = read_records((path,), owner(), "request", event="model.request")
    assert result.truncated
    assert len(result.text.encode("utf-8")) <= 65_536
    output = read_records((path,), owner(), "request", event="model.response")
    assert "answer" in output.text
    path.write_bytes(b'{"unfinished":')
    result = read_records((path,), owner(), "request", event="model.request")
    assert result.availability == "unavailable"
    assert result.truncated


class State(TypedDict):
    question: str


async def test_checkpoint_request_metadata_separates_same_thread_turns_and_owners() -> None:
    async with Fakes().container() as services:
        builder = StateGraph(State, context_schema=RequestContext)
        builder.add_node("answer", lambda state: {"question": state["question"]})
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        graph = builder.compile(**services.compile_kwargs())
        first, second = owner(), replace(owner(), request_id="second")
        await graph.ainvoke({"question": "first question"}, **services.invocation(first))
        await graph.ainvoke({"question": "second question"}, **services.invocation(second))
        reader = GraphCheckpointPreview(lambda: graph, services.invocation)
        one = await reader(first, "request")
        two = await reader(second, "second")
        assert one.record_count == two.record_count == 3
        assert "second question" not in one.text
        assert '"agentlib_request_id": "second"' in two.text
        other = replace(first, principal=replace(first.principal, subject="other"))
        assert (await reader(other, "request")).record_count == 0
        assert (await reader(first, "absent")).record_count == 0


async def test_endpoint_authorizes_before_read_and_never_executes_agent(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    write(path, [record()])
    principal = owner().principal
    kit = Fakes(
        identity=FakeIdentityVerifier(
            {"owner": principal, "denied": replace(principal, subject="other")}
        )
    )

    async def authorize(context: RequestContext) -> None:
        if context.principal.subject != "developer":
            raise PolicyDenied("denied", reason_code="diagnostic_denied")

    async def run(context: RequestContext, given: object) -> object:
        pytest.fail("inspection must not invoke the graph")

    async def ready() -> None:
        pass

    async with kit.container() as services:
        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(
            services,
            run,
            application="memo",
            lifecycle=lifecycle,
            inspector=LocalTurnInspector(authorize=authorize, model_paths=(path,)),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/requests/request/inspection?thread_id=thread",
                headers={"authorization": "Bearer owner"},
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["model_inputs"]["record_count"] == 1
            response = await client.get(
                "/requests/request/inspection?thread_id=thread",
                headers={"authorization": "Bearer denied"},
            )
            assert response.status_code == 404
            assert "only this turn" not in response.text


async def test_endpoint_logs_an_inspector_failure_and_answers_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    principal = owner().principal
    kit = Fakes(identity=FakeIdentityVerifier({"owner": principal}))

    class BrokenInspector:
        async def read(self, context: RequestContext, request_id: str) -> TurnInspection:
            raise OSError("the private diagnostics volume is gone")

    async def run(context: RequestContext, given: object) -> object:
        pytest.fail("inspection must not invoke the graph")

    async def ready() -> None:
        pass

    async with kit.container() as services:
        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(
            services, run, application="memo", lifecycle=lifecycle, inspector=BrokenInspector()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            with caplog.at_level("ERROR"):
                response = await client.get(
                    "/requests/request/inspection?thread_id=thread",
                    headers={"authorization": "Bearer owner"},
                )
    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"
    assert "private diagnostics" not in response.text
    failures = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
    assert failures[0].exc_info[0] is OSError
    assert getattr(failures[0], "request_id", None) == "request"
