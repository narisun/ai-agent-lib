"""Loopback FastAPI parent mounts Chainlit behind one local session boundary."""

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_agent_lib_ui.application.chat import view_for
from ai_agent_lib_ui.contracts.queries import ContractError, ErrorCode, ErrorResponse, PageRequest
from ai_agent_lib_ui.integrations.documents import register_documents
from ai_agent_lib_ui.integrations.inspection import register_inspector
from ai_agent_lib_ui.integrations.logs import register_logs
from ai_agent_lib_ui.integrations.navigation import login_destination
from ai_agent_lib_ui.integrations.runtime import Runtime
from ai_agent_lib_ui.integrations.security import SessionBoundary, current_principal


def page(title: str, body: str) -> HTMLResponse:
    """Render a responsive local page; callers escape all dynamic text."""
    navigation = "".join(
        f"<a href='{path}'" + (" aria-current='page'" if title == name else "") + f">{name}</a>"
        for name, path in (("Chat", "/chat/"), ("Choose agent", "/catalog"), ("Logs", "/logs"))
    )
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} · Agent Lab</title>"
        "<link rel='icon' href='/favicon' type='image/svg+xml'>"
        "<link rel='stylesheet' href='/ui/theme.css'>"
        "<script src='/ui/theme.js'></script></head><body class='agent-lab-page'>"
        "<header class='lab-header'><a class='brand' href='/chat/'>"
        "<img src='/ui/agent-lab.svg' width='28' height='28' alt=''>Agent Lab</a>"
        f"<nav aria-label='Main navigation'>{navigation}"
        "<select class='theme-picker' aria-label='Theme' data-theme>"
        "<option value='light'>Light</option><option value='dark'>Dark</option>"
        "<option value='system'>System</option></select></nav></header>"
        f"<main><h1>{escape(title)}</h1>{body}"
        "<p role='status' data-form-status></p></main>"
        "<script src='/ui/forms.js' defer></script></body></html>"
    )


def _asset_routes(app: FastAPI) -> None:
    """Serve only the bundled, public presentation assets."""

    @app.get("/ui/forms.js")
    async def form_script() -> Response:
        return Response(
            files("ai_agent_lib_ui").joinpath("assets/forms.js").read_text(),
            media_type="text/javascript",
        )

    @app.get("/favicon")
    @app.get("/ui/agent-lab.svg")
    async def brand_icon() -> Response:
        # Parent pages, including sign-in, use the same icon as mounted chat.
        return Response(
            files("ai_agent_lib_ui").joinpath("assets/agent-lab.svg").read_bytes(),
            media_type="image/svg+xml",
        )

    @app.get("/ui/theme.css")
    async def theme_styles() -> Response:
        return Response(
            files("ai_agent_lib_ui").joinpath("assets/theme.css").read_text(),
            media_type="text/css",
        )

    @app.get("/ui/theme.js")
    async def theme_script() -> Response:
        return Response(
            files("ai_agent_lib_ui").joinpath("assets/theme.js").read_text(),
            media_type="text/javascript",
        )


class NewThread(BaseModel):
    """Browser-selectable agent ID, resolved through the authorized catalog."""

    model_config = ConfigDict(extra="forbid")
    agent_id: str


class NewTurn(BaseModel):
    """A bounded question with a client-generated idempotency key."""

    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, Field(min_length=1, max_length=65_536)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]


def create_app(resources: Runtime) -> FastAPI:
    """Build routes after writable shell configuration has been prepared."""
    from chainlit.utils import mount_chainlit

    from ai_agent_lib_ui.integrations.chainlit_compat import use_shared_profile_config

    use_shared_profile_config()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await resources.chat.close()
            await resources.client.aclose()

    app = FastAPI(title="Agent Lab", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(SessionBoundary, resources=resources)

    @app.exception_handler(ValidationError)
    async def invalid_input(_request: Request, _error: ValidationError) -> JSONResponse:
        error = ErrorResponse(code=ErrorCode.INVALID_INPUT, request_id=str(uuid4()))
        return JSONResponse(error.model_dump(mode="json"), status_code=400)

    @app.exception_handler(ContractError)
    async def contract_error(_request: Request, error: ContractError) -> JSONResponse:
        status = {ErrorCode.NOT_FOUND: 404, ErrorCode.INVALID_INPUT: 400}.get(error.code, 409)
        response = ErrorResponse(code=error.code, request_id=str(uuid4()))
        return JSONResponse(response.model_dump(mode="json"), status_code=status)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login")
    async def login_page(
        return_to: Annotated[str, Query(alias="next", max_length=2048)] = "/chat/",
    ) -> HTMLResponse:
        return page(
            "Sign in locally",
            "<p>Enter the access code from the UI state folder. "
            "This session uses the identity configured for your local agents.</p>"
            "<form method='post' action='/login'>"
            "<input name='next' type='hidden' "
            f"value='{escape(login_destination(return_to), quote=True)}'>"
            "<label>Access code "
            "<input name='code' type='password' required autocomplete='current-password'>"
            "</label><button class='primary' type='submit'>Sign in</button></form>",
        )

    _asset_routes(app)

    @app.post("/login")
    async def login(
        code: Annotated[str, Form(max_length=200)],
        return_to: Annotated[str, Form(alias="next", max_length=2048)] = "/chat/",
    ) -> RedirectResponse:
        if not hmac.compare_digest(code, resources.access_code):
            from fastapi import HTTPException

            raise HTTPException(401, "Access code did not match")
        token = resources.store.new_session(resources.identity)
        response = RedirectResponse(login_destination(return_to), status_code=303)
        response.set_cookie(
            "agentlib_session", token, httponly=True, samesite="strict", max_age=3600
        )
        response.delete_cookie("agentlib_chat")
        return response

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse("/catalog", status_code=303)

    _catalog_api(app, resources)

    _thread_api(app, resources)

    _catalog_page(app, resources)
    _conversation_pages(app, resources)
    register_inspector(app, resources, page)
    register_documents(app, resources)
    register_logs(app, resources, page)

    mount_chainlit(app, target=str(Path(__file__).with_name("callbacks.py")), path="/chat")
    return app


def _catalog_page(app: FastAPI, resources: Runtime) -> None:
    @app.get("/catalog")
    async def catalog_page() -> HTMLResponse:
        principal = current_principal()
        agents = await resources.catalog.agents(principal, PageRequest(limit=200))
        mcps = await resources.catalog.mcps(principal, PageRequest(limit=200))
        body = (
            "<p>Choose an agent below and select <strong>Start chat</strong>. "
            "Type a question in the message box, then press Enter. "
            "Continue in that chat for follow-ups; changing agents starts a new chat.</p>"
            "<div class='agent-grid'>"
        )
        for binding in agents.items:
            manifest = binding.manifest
            body += (
                f"<article><h2>{escape(manifest.title)}</h2><p>Version "
                f"{escape(manifest.agent_version)} · {escape(manifest.adapter)}</p>"
                "<form action='/catalog/start' method='post'>"
                "<input name='agent_id' type='hidden' "
                f"value='{escape(manifest.agent_id, quote=True)}'>"
                f"<button class='primary'>Start chat with {escape(manifest.title)}</button>"
                "</form></article>"
            )
        body += "</div><h2>Registered MCP services</h2>"
        if not mcps.items:
            body += "<p class='muted'>No MCP services are registered in this environment.</p>"
        for mcp in mcps.items:
            body += (
                f"<article><strong>{escape(mcp.id)}</strong>"
                f"<p>{escape(', '.join(mcp.tool_names))}</p></article>"
            )
        return page("Choose agent", body)

    @app.post("/catalog/start")
    async def start_chat(agent_id: Annotated[str, Form(max_length=200)]) -> RedirectResponse:
        conversation = await resources.chat.create(current_principal(), agent_id)
        return RedirectResponse(f"/chat/thread/{conversation.thread.thread_id}", status_code=303)


def _conversation_pages(app: FastAPI, resources: Runtime) -> None:
    @app.get("/conversations/{thread_id}/history")
    async def older_history(thread_id: str, cursor: str | None = None) -> HTMLResponse:
        from ai_agent_lib_ui.integrations.data_layer import outcome_text

        principal = current_principal()
        conversation = await resources.store.get(principal, thread_id)
        history = await resources.store.history_page(
            view_for(principal, conversation.thread.scope.application),
            thread_id,
            PageRequest(cursor=cursor),
        )
        body = f"<p><a href='/chat/thread/{escape(thread_id, quote=True)}'>Return to chat</a></p>"
        for stored in history.items:
            body += (
                "<article><strong>You</strong><div style='white-space:pre-wrap'>"
                + escape(stored.question)
                + "</div><hr><div style='white-space:pre-wrap'>"
                + escape(outcome_text(stored.run))
                + "</div>"
            )
            body += "</article>"
        if history.next_cursor:
            query = urlencode({"cursor": history.next_cursor})
            body += f"<a href='?{escape(query, quote=True)}'>Older messages</a>"
        return page("Conversation history", body)

    @app.get("/conversations")
    async def conversations(
        search: str = "", archived: bool = False, cursor: str | None = None
    ) -> HTMLResponse:
        result = await resources.store.list_owned(
            current_principal(), PageRequest(cursor=cursor), search=search, archived=archived
        )
        body = (
            "<p>Rename, archive or restore your conversations here. "
            "Archiving keeps the saved history.</p><form><label>Search titles "
            f"<input name='search' value='{escape(search, quote=True)}' "
            "maxlength='256'></label>"
            "<label>Show <select name='archived'>"
            f"<option value='false' {'selected' if not archived else ''}>Active</option>"
            f"<option value='true' {'selected' if archived else ''}>Archived</option>"
            "</select></label>"
            "<button>Search</button></form>"
        )
        for thread in result.items:
            thread_id = escape(thread.thread_id, quote=True)
            body += (
                f"<article><h2><a href='/chat/thread/{thread_id}'>{escape(thread.title)}</a></h2>"
                f"<small>{escape(thread.scope.application)} · "
                f"{thread.updated_at:%Y-%m-%d %H:%M} UTC</small>"
                f"<form action='/conversations/{thread_id}' method='post'>"
                f"<label>Title <input name='title' value='{escape(thread.title, quote=True)}' "
                "required maxlength='256'></label>"
                "<button name='action' value='rename'>Rename</button>"
                f"<button name='action' value='{'restore' if archived else 'archive'}'>"
                f"{'Restore' if archived else 'Archive'}</button></form></article>"
            )
        if not result.items:
            body += "<p>No conversations match this search.</p>"
        if result.next_cursor:
            query = urlencode(
                {"search": search, "archived": str(archived).lower(), "cursor": result.next_cursor}
            )
            body += f"<a href='/conversations?{escape(query, quote=True)}'>Next page</a>"
        return page("Conversations", body)

    @app.post("/conversations/{thread_id}")
    async def change_thread(
        thread_id: str, title: Annotated[str, Form(max_length=256)], action: Annotated[str, Form()]
    ) -> RedirectResponse:
        principal = current_principal()
        conversation = await resources.store.get(principal, thread_id)
        if action not in {"rename", "archive", "restore"}:
            raise ContractError(ErrorCode.INVALID_INPUT)
        await resources.store.change(
            view_for(principal, conversation.thread.scope.application),
            thread_id,
            title=title if action == "rename" else None,
            archived=(action == "archive") if action != "rename" else None,
        )
        return RedirectResponse("/conversations", status_code=303)


def _catalog_api(app: FastAPI, resources: Runtime) -> None:
    @app.get("/api/catalog")
    async def catalog_api() -> dict[str, object]:
        principal = current_principal()
        agents = await resources.catalog.agents(principal, PageRequest(limit=200))
        mcps = await resources.catalog.mcps(principal, PageRequest(limit=200))
        return {
            "agents": [b.manifest.model_dump(mode="json", by_alias=True) for b in agents.items],
            "mcps": [m.model_dump(mode="json") for m in mcps.items],
        }


def _thread_api(app: FastAPI, resources: Runtime) -> None:
    @app.get("/api/threads")
    async def threads_api(
        search: str = "", archived: bool = False, cursor: str | None = None
    ) -> object:
        return await resources.store.list_owned(
            current_principal(), PageRequest(cursor=cursor), search=search, archived=archived
        )

    @app.post("/api/threads")
    async def create_thread(body: NewThread) -> object:
        return await resources.chat.create(current_principal(), body.agent_id)

    @app.get("/api/threads/{thread_id}")
    async def thread_api(thread_id: str, cursor: str | None = None) -> object:
        principal = current_principal()
        conversation = await resources.store.get(principal, thread_id)
        view = view_for(principal, conversation.thread.scope.application)
        history = await resources.store.history_page(view, thread_id, PageRequest(cursor=cursor))
        return {
            "thread": conversation.thread,
            "turns": history.items,
            "next_cursor": history.next_cursor,
        }

    @app.post("/api/threads/{thread_id}/messages")
    async def message_api(thread_id: str, body: NewTurn) -> object:
        return await resources.chat.submit(
            current_principal(), thread_id, body.idempotency_key, body.question
        )
