"""A bounded index of owned conversations and their inspectable turns."""

from collections.abc import Callable
from html import escape
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from ai_agent_lib_ui.application.chat import view_for
from ai_agent_lib_ui.contracts.queries import PageRequest, Permission
from ai_agent_lib_ui.integrations.inspection import inspector_url
from ai_agent_lib_ui.integrations.runtime import Runtime
from ai_agent_lib_ui.integrations.security import current_principal


def register_logs(
    app: FastAPI, resources: Runtime, page: Callable[[str, str], HTMLResponse]
) -> None:
    """List metadata first; raw model content loads only when a turn is opened."""

    @app.get("/logs")
    async def logs(
        thread_id: str | None = None, cursor: str | None = None, search: str = ""
    ) -> HTMLResponse:
        principal = await current_principal(resources)
        body = (
            "<p>Choose a chat, then a turn to view <strong>LLM input and output</strong>, "
            "MCP calls, and checkpoints. Opening logs never reruns the agent.</p>"
            "<p><a href='/conversations'>Manage chat history (rename or archive)</a></p>"
        )
        if thread_id is None:
            result = await resources.store.list_owned(
                principal, PageRequest(cursor=cursor), search=search
            )
            body += (
                "<form><label>Search chat titles "
                f"<input name='search' value='{escape(search, quote=True)}' maxlength='256'>"
                "</label><button>Search</button></form>"
            )
            for thread in result.items:
                query = escape(urlencode({"thread_id": thread.thread_id}), quote=True)
                body += (
                    f"<article><h2><a href='/logs?{query}'>{escape(thread.title)}</a></h2>"
                    f"<p>Agent: {escape(thread.scope.application)} · "
                    f"{thread.updated_at:%Y-%m-%d %H:%M} UTC</p></article>"
                )
            has_items, next_cursor = bool(result.items), result.next_cursor
            more = {"search": search}
        else:
            conversation = await resources.store.get(principal, thread_id)
            view = view_for(principal, conversation.thread.scope.application)
            await resources.store.authorizer.require(view, Permission.DIAGNOSTIC_METADATA)
            history = await resources.store.history_page(
                view, thread_id, PageRequest(cursor=cursor)
            )
            body += (
                f"<h2>{escape(conversation.thread.title)}</h2>"
                "<p><a href='/logs'>All chat logs</a></p>"
            )
            for stored in reversed(history.items):
                run = stored.run
                body += (
                    f"<article><h3>{escape(stored.question[:256])}</h3>"
                    f"<p>{run.accepted_at:%Y-%m-%d %H:%M:%S} UTC · {escape(run.state)} · "
                    f"Request <code>{escape(run.request_id)}</code></p>"
                    f"<a href='{escape(inspector_url(run), quote=True)}'>"
                    "View LLM input/output, MCP calls and checkpoints</a>"
                    "</article>"
                )
            has_items, next_cursor = bool(history.items), history.next_cursor
            more = {"thread_id": thread_id}
        if not has_items:
            body += (
                "<p>No turns to inspect yet. Select Choose agent, start a chat, "
                "and send a question.</p>"
            )
        if next_cursor:
            query = escape(urlencode({**more, "cursor": next_cursor}), quote=True)
            body += f"<a href='/logs?{query}'>Older entries</a>"
        return page("Logs", body)
