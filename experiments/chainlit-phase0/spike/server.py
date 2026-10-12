"""FastAPI parent application, local sign-in, and read-only inspector routes."""

import json
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from spike.fixture import fixture_routes
from spike.runtime import configure
from spike.security import SessionBoundary, current_owner, session_token
from spike.settings import ROOT, Settings, catalog


def create_app(settings: Settings) -> FastAPI:  # noqa: C901 - route composition in one sample factory
    """Compose public hooks under one parent HTTP/WebSocket session boundary."""
    settings.prepare_chainlit()
    resources = configure(settings)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        # A browser disconnect is not shutdown. Await remaining tasks during
        # teardown; failed HTTP calls and process crashes can remain unknown.
        import asyncio

        if resources.runs.tasks:
            await asyncio.gather(*list(resources.runs.tasks.values()), return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionBoundary, store=resources.store, settings=settings)
    app.include_router(fixture_routes(resources.store, settings))

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "sample": "phase0"}

    @app.get("/login")
    async def login_page():
        return HTMLResponse((ROOT / "views/login.html").read_text(encoding="utf-8"))

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        owner = str(form.get("user", ""))
        code = str(form.get("code", ""))
        if owner not in ("demo/alice", "demo/bob", "other/alice") or not secrets.compare_digest(
            code, settings.access_code
        ):
            raise HTTPException(401, "Invalid local fixture credentials")
        response = RedirectResponse("/chat", status_code=303)
        response.set_cookie(
            "phase0_session",
            resources.store.session(owner),
            httponly=True,
            samesite="strict",
            max_age=3600,
        )
        response.delete_cookie("phase0_chainlit")
        return response

    @app.post("/logout")
    async def logout():
        resources.store.revoke(session_token.get())
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("phase0_session")
        response.delete_cookie("phase0_chainlit")
        return response

    @app.get("/")
    async def index():
        return RedirectResponse("/chat")

    @app.get("/catalog")
    async def catalog_page():
        return HTMLResponse((ROOT / "views/catalog.html").read_text(encoding="utf-8"))

    @app.get("/api/catalog")
    async def catalog_api():
        return {"agents": catalog(), "identity": current_owner(resources.store)}

    @app.get("/api/threads")
    async def threads():
        return resources.store.threads(current_owner(resources.store))

    @app.get("/api/threads/{thread_id}/runs")
    async def runs(thread_id: str):
        return resources.store.runs(current_owner(resources.store), thread_id)

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str):
        return JSONResponse(
            resources.store.run(current_owner(resources.store), run_id),
            headers={"cache-control": "no-store"},
        )

    @app.get("/inspector/{run_id}")
    async def inspect_run(run_id: str):
        resources.store.run(current_owner(resources.store), run_id)
        return HTMLResponse(
            (ROOT / "views/inspector.html").read_text(encoding="utf-8"),
            headers={"cache-control": "no-store"},
        )

    @app.get("/artifacts/{run_id}")
    async def artifact(run_id: str, download: bool = False):
        result = resources.store.run(current_owner(resources.store), run_id)["result"]
        headers = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
        if download:
            headers["content-disposition"] = 'attachment; filename="preparation-memo.md"'
        return PlainTextResponse(result.get("memo", ""), headers=headers)

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: str):
        data = resources.store.run(current_owner(resources.store), run_id)
        return PlainTextResponse(
            json.dumps(data, indent=2),
            media_type="application/json",
            headers={
                "content-disposition": 'attachment; filename="synthetic-run.json"',
                "cache-control": "no-store",
            },
        )

    app.mount("/assets", StaticFiles(directory=ROOT / "public"), name="assets")
    from chainlit.utils import mount_chainlit

    mount_chainlit(app, target=str(ROOT / "spike/callbacks.py"), path="/chat")
    return app
