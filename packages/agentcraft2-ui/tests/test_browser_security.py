"""Sign-in, origin and parent-session guards on the real UI server, and launcher probes."""

import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from browser_protocol import LocalServer
from python_runtime import python_command

pytestmark = pytest.mark.slow


def test_launcher_health_probes_do_not_reset_ui_connections(server: LocalServer) -> None:
    root = Path(__file__).resolve().parents[3]
    log = server.path / "server.log"
    before = log.stat().st_size
    probe = (
        f"import sys; sys.path.insert(0, {str(root)!r}); "
        "from pathlib import Path; "
        "from tools.local_processes import Service, is_ready; "
        f"service = Service('ui', (), Path.cwd(), {server.port}, '/healthz'); "
        "assert all(is_ready(service) for _ in range(40))"
    )
    result = subprocess.run(  # noqa: S603 - exercise our launcher with this test's server
        python_command("-c", probe), capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Let the same server handle another request before inspecting its callback errors.
    with httpx.Client(trust_env=False) as client:
        assert client.get(server.origin + "/healthz").json() == {"status": "ok"}
    output = log.read_bytes()[before:].decode("utf-8")
    assert "ConnectionResetError" not in output, output
    assert "_call_connection_lost" not in output, output


async def test_parent_session_guards_all_mounted_routes_and_origin(server: LocalServer) -> None:
    async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as anonymous:
        for path in (
            "/api/catalog",
            "/api/threads",
            "/chat/project/settings",
            "/chat/threads",
            "/chat/public/chat.css",
        ):
            assert (
                await anonymous.get(path, headers={"x-agentcraft2-user": "dev-user"})
            ).status_code == 401
        assert (await anonymous.post("/login", data={"code": "bad"})).status_code == 403
        assert (
            await anonymous.get("/healthz", headers={"host": "evil.example"})
        ).status_code == 403
    async with server.login() as client:
        assert (await client.post("/api/threads", json={"agent_id": "denied"})).status_code == 404
        assert (await client.get("/api/threads/not-owned")).status_code == 404
        cross_origin = await client.post(
            "/api/threads", json={"agent_id": "memo"}, headers={"origin": "http://evil.example"}
        )
        assert cross_origin.status_code == 403
        catalog = await client.get("/api/catalog")
        assert {item["agent_id"] for item in catalog.json()["agents"]} == {"memo", "helper"}
        assert "unsafe-eval" not in catalog.headers["content-security-policy"]
        assert (await client.post("/chat/mcp", json={})).status_code == 404
        assert (await client.post("/chat/project/file", content=b"file")).status_code == 404


async def test_saved_conversation_login_redirect_and_session_recovery(server: LocalServer) -> None:
    async with server.login() as client:
        created = (await client.post("/api/threads", json={"agent_id": "memo"})).json()
        thread = created["thread"]["thread_id"]
        path = f"/chat/thread/{thread}"
        assert (await client.post("/chat/logout")).status_code == 200
        response = await client.get(path)
        assert response.status_code == 303
        location = response.headers["location"]
        assert urlsplit(location).path == "/login"
        assert parse_qs(urlsplit(location).query) == {"next": [path]}
        form = await client.get(location)
        assert form.status_code == 200
        assert "Sign in locally" in form.text
        assert f"value='{path}'" in form.text
        code = server.config.with_name("access-code").read_text().strip()
        response = await client.post("/login", data={"code": code, "next": path})
        assert response.status_code == 303
        assert response.headers["location"] == path
        assert (await client.post("/chat/auth/header")).status_code == 200
        assert (await client.get(path)).status_code == 200
        saved = (await client.get(f"/api/threads/{thread}")).json()
        assert saved["thread"]["thread_id"] == thread


async def test_anonymous_pages_redirect_but_api_calls_keep_unauthorized_status(
    server: LocalServer,
) -> None:
    async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as client:
        for path in ("/catalog", "/conversations?search=Canada+Goose", "/chat/thread/saved-thread"):
            response = await client.get(path)
            assert response.status_code == 303
            assert parse_qs(urlsplit(response.headers["location"]).query) == {"next": [path]}
        response = await client.get("/api/threads", headers={"accept": "text/html"})
        assert response.status_code == 401
        assert response.json() == {"detail": "Sign in to the local UI"}
        assert "location" not in response.headers
        assert (
            await client.get("/chat/thread/saved-thread", headers={"host": "evil.example"})
        ).status_code == 403
        response = await client.post("/chat/thread/saved-thread", headers={"origin": server.origin})
        assert response.status_code == 401
        assert "location" not in response.headers


async def test_login_cannot_redirect_to_an_external_site(server: LocalServer) -> None:
    async with httpx.AsyncClient(
        base_url=server.origin, trust_env=False, headers={"origin": server.origin}
    ) as client:
        code = server.config.with_name("access-code").read_text().strip()
        form = await client.get("/login", params={"next": "https://example.com/"})
        assert "value='/chat/'" in form.text
        response = await client.post("/login", data={"code": code, "next": "//example.com/"})
        assert response.status_code == 303
        assert response.headers["location"] == "/chat/"
