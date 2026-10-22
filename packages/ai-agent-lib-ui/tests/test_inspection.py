"""Reject misbound/oversized diagnostics and render only escaped, bounded content."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ai_agent_lib_core.contracts import Principal, Scope
from ai_agent_lib_core.contracts.inspection import InspectionSection, TurnInspection
from ai_agent_lib_ui.adapters.catalog import LocalAccess
from ai_agent_lib_ui.adapters.inspection import fetch_inspection
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings
from ai_agent_lib_ui.contracts.catalog import AgentBinding, PresentationManifest
from ai_agent_lib_ui.contracts.conversations import RunRecord
from ai_agent_lib_ui.contracts.queries import ContractError, Permission, ViewContext
from ai_agent_lib_ui.integrations.inspection import inspection_body
from ai_agent_lib_ui.integrations.navigation import login_destination

SCOPE = Scope("tenant", "user", "memo")
VIEW = ViewContext(principal=Principal(tenant="tenant", subject="user"), scope=SCOPE)
BINDING = AgentBinding(
    base_url="http://127.0.0.1:8000",
    manifest=PresentationManifest(
        agent_id="memo", agent_version="1", registry_revision="1", title="Memo", api_audience="memo"
    ),
)


def run() -> RunRecord:
    return RunRecord(
        scope=SCOPE,
        thread_id="thread",
        request_id="request",
        run_id="run",
        idempotency_key="send",
        state="completed",
        answer="answer",
        accepted_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    "mode",
    ["ok", "thread", "request", "subject", "schema", "large", "redirect", "404", "500", "timeout"],
)
async def test_inspection_transport_validates_identity_and_bounds(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/requests/request/inspection"
        assert request.url.params["thread_id"] == "thread"
        assert "authorization" not in request.headers
        if mode == "timeout":
            raise httpx.ReadTimeout("do not leak")
        if mode == "large":
            return httpx.Response(200, content=b"x" * 524_289)
        if mode in {"404", "500"}:
            return httpx.Response(int(mode))
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "http://elsewhere.test/"})
        data = TurnInspection(
            scope=SCOPE,
            thread_id="thread",
            request_id="request",
            model_inputs=InspectionSection(availability="available", text="input"),
        ).model_dump(mode="json", by_alias=True)
        if mode in {"thread", "request"}:
            data[mode + "_id"] = "other"
        if mode == "subject":
            data["scope"]["subject"] = "other"
        if mode == "schema":
            data["schema"] = "unknown/v9"
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        with caplog.at_level("WARNING"):
            result = await fetch_inspection(client, VIEW, BINDING, run())
    expected = "available" if mode == "ok" else "unsupported" if mode == "404" else "unavailable"
    assert result.model_inputs.availability == expected
    assert len(calls) == 1
    if mode == "timeout":
        # The panel says unavailable; the log names the agent, the turn and the error type.
        warned = [r for r in caplog.records if r.getMessage() == "inspection unavailable"]
        assert len(warned) == 1
        assert getattr(warned[0], "error_type", None) == "ReadTimeout"
        assert getattr(warned[0], "request_id", None) == "request"
        assert "do not leak" not in caplog.text


async def test_content_permission_can_be_disabled_without_disabling_chat(tmp_path: Path) -> None:
    settings = Settings(
        agents_path=tmp_path / "agents",
        tools_path=tmp_path / "tools",
        identity=LocalIdentity(tenant="tenant", subject="user"),
        bindings=(BINDING,),
        allowed_urls=frozenset({BINDING.base_url}),
        inspector_enabled=False,
    )
    access = LocalAccess(settings)
    await access.require(VIEW, Permission.CONVERSATION_READ)
    with pytest.raises(ContractError, match="not_found"):
        await access.require(VIEW, Permission.DIAGNOSTIC_CONTENT)


def test_rendered_sections_are_closed_escape_content_and_explain_truncation() -> None:
    result = TurnInspection(
        scope=SCOPE,
        thread_id="thread",
        request_id="request",
        model_inputs=InspectionSection(
            availability="available", text="<script>bad</script>", truncated=True
        ),
    )
    body = inspection_body(run(), "<b>Question</b>", result)
    assert body.count("<details>") == 4
    assert "<details open" not in body
    assert "&lt;script&gt;bad&lt;/script&gt;" in body
    assert "&lt;b&gt;Question&lt;/b&gt;" in body
    assert "Limited preview" in body
    assert "Older checkpoints without a request ID" in body
    path = "/conversations/thread/turns/run/inspect"
    assert login_destination(path) == path
