"""Parent FastAPI routes run in process over the injected Runtime, without Chainlit."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from local_runtime import settings_at

from ai_agent_lib_ui.contracts.queries import ErrorCode, ErrorResponse
from ai_agent_lib_ui.integrations import runtime as composition
from ai_agent_lib_ui.integrations.runtime import Runtime, compose, install_runtime, reset_runtime
from ai_agent_lib_ui.integrations.server import parent_app, status_for


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    origin = runtime.settings.origin
    transport = httpx.ASGITransport(app=parent_app(runtime))
    async with httpx.AsyncClient(
        transport=transport, base_url=origin, headers={"origin": origin}
    ) as value:
        yield value


async def login(client: httpx.AsyncClient, runtime: Runtime) -> None:
    response = await client.post("/login", data={"code": runtime.access_code})
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/chat/"
    assert "agentcraft2_session" in response.cookies


async def test_login_checks_the_access_code_and_opens_a_session(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    refused = await client.post("/login", data={"code": "not the code"})
    assert refused.status_code == 401
    assert refused.json() == {"detail": "Access code did not match"}
    assert "agentcraft2_session" not in refused.cookies
    await login(client, runtime)
    listing = await client.get("/api/threads")
    assert listing.status_code == 200
    assert listing.json()["items"] == []


async def test_unauthenticated_requests_get_json_or_a_login_redirect(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    api = await client.get("/api/threads")
    assert (api.status_code, api.json()) == (401, {"detail": "Sign in to the local UI"})
    page = await client.get("/catalog")
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=%2Fcatalog"
    assert page.headers["cache-control"] == "no-store"


async def test_contract_errors_become_stable_json_with_a_request_id(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await login(client, runtime)
    missing = await client.get("/api/threads/missing")
    assert missing.status_code == 404
    error = ErrorResponse.model_validate(missing.json())
    assert error.code == ErrorCode.NOT_FOUND
    UUID(error.request_id)
    assert set(missing.json()) == {"code", "request_id"}
    created = await client.post("/api/threads", json={"agent_id": "memo"})
    assert created.status_code == 200
    thread = created.json()["thread"]["thread_id"]
    bogus = await client.post(
        f"/conversations/{thread}", data={"title": "Renamed", "action": "bogus"}
    )
    assert bogus.status_code == 400
    assert ErrorResponse.model_validate(bogus.json()).code == ErrorCode.INVALID_INPUT
    unknown_agent = await client.post("/api/threads", json={"agent_id": "nobody"})
    assert unknown_agent.status_code == 404
    assert all(isinstance(status_for(code), int) for code in ErrorCode)


async def test_compose_is_reentrant_and_the_callback_slot_serves_one_runtime(
    tmp_path: Path, runtime: Runtime
) -> None:
    (tmp_path / "other").mkdir()
    other = compose(settings_at(tmp_path / "other"), tmp_path / "other" / "state")
    try:
        assert other.identity == runtime.identity
        assert other.store.path != runtime.store.path
        with pytest.raises(RuntimeError, match="not been composed"):
            composition.runtime()
        install_runtime(runtime)
        install_runtime(runtime)
        assert composition.runtime() is runtime
        with pytest.raises(RuntimeError, match="serve one UI per process"):
            install_runtime(other)
        reset_runtime()
        install_runtime(other)
        assert composition.runtime() is other
    finally:
        await other.chat.close()
        await other.client.aclose()
