"""Lazy, read-only turn inspector; diagnostic content is escaped, never executed."""

from collections.abc import Callable
from html import escape
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from ai_agent_lib_core.contracts.inspection import TurnInspection
from ai_agent_lib_ui.adapters.inspection import fetch_inspection
from ai_agent_lib_ui.application.chat import view_for
from ai_agent_lib_ui.contracts.conversations import RunRecord
from ai_agent_lib_ui.contracts.queries import Permission
from ai_agent_lib_ui.integrations.runtime import Runtime
from ai_agent_lib_ui.integrations.security import current_principal


def inspector_url(run: RunRecord) -> str:
    """Link a display response to its immutable UI turn, not a browser-supplied request."""
    return (
        f"/conversations/{quote(run.thread_id, safe='')}/turns/{quote(run.run_id, safe='')}/inspect"
    )


def inspection_body(run: RunRecord, question: str, result: TurnInspection) -> str:
    """Render independently closed sections with honest availability and size limits."""
    body = (
        f"<p><a href='/chat/thread/{escape(quote(run.thread_id, safe=''), quote=True)}'>"
        "Return to chat</a></p>"
        f"<p><strong>Question:</strong> {escape(question)}</p>"
        f"<p>Agent: <code>{escape(run.scope.application)}</code> · "
        f"Request: <code>{escape(run.request_id)}</code> · State: {escape(run.state)}</p>"
        "<p>Read-only diagnostics for this turn. Model records show the messages and tool "
        "schemas supplied to the model SDK and the returned response. MCP records show "
        "governed calls and their outcomes; tool arguments and results may also appear in "
        "model messages. Checkpoints are parent graph states saved during this turn, "
        "including any saved conversation carried forward.</p>"
    )
    for title, section in (
        ("LLM input", result.model_inputs),
        ("LLM output", result.model_outputs),
        ("MCP calls", result.mcp),
        ("Checkpoints", result.checkpoints),
    ):
        body += f"<article><details><summary>{title} · {section.availability}"
        if section.record_count:
            body += f" · {section.record_count} captured record(s) in this preview"
        body += "</summary>"
        if section.truncated:
            body += (
                "<p><strong>Limited preview:</strong> content, record or file scan limit reached; "
                "this is not the complete capture. Consult the agent's configured local files "
                "for the full capture.</p>"
            )
        if section.text:
            body += (
                "<pre style='max-height:65vh;overflow:auto;white-space:pre'>"
                + escape(section.text)
                + "</pre>"
            )
        else:
            body += (
                "<p>No captured records are available for this turn. The agent may not "
                "support inspection, capture may be disabled, or the data may no longer "
                "be retained. Older checkpoints without a request ID cannot be safely "
                "assigned to this turn.</p>"
            )
        body += "</details></article>"
    return body


def register_inspector(
    app: FastAPI, resources: Runtime, page: Callable[[str, str], HTMLResponse]
) -> None:
    """Authorize one stored turn before contacting its currently approved agent."""

    @app.get("/conversations/{thread_id}/turns/{run_id}/inspect")
    async def inspect_turn(thread_id: str, run_id: str) -> HTMLResponse:
        principal = await current_principal(resources)
        conversation = await resources.store.get(principal, thread_id)
        view = view_for(principal, conversation.thread.scope.application)
        for permission in (Permission.DIAGNOSTIC_METADATA, Permission.DIAGNOSTIC_CONTENT):
            await resources.store.authorizer.require(view, permission)
        stored = await resources.store.turn(view, thread_id, run_id)
        binding = await resources.catalog.resolve(principal, view.scope.application)
        result = await fetch_inspection(resources.client, view, binding, stored.run)
        # The parent session boundary applies no-store to every authenticated page.
        return page("Turn inspector", inspection_body(stored.run, stored.question, result))
