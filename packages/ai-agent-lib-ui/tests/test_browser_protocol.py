"""Exercise the installed shell over real HTTP and Socket.IO with a scripted agent."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import socketio  # type: ignore[import-untyped]
from python_runtime import python_command


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class LocalServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.port = free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.calls: list[dict[str, Any]] = []
        self.delay = 0.0
        self.streaming = False
        self.stream_failure = False
        self.lose_response = False
        self.answer: str | None = None
        self.document: dict[str, Any] | None = None
        self.trace: dict[str, str] | None = None
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.inspection_reads: list[tuple[str, str]] = []
        owner = self

        class AgentHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(body)
                output: dict[str, Any] = {
                    "answer": owner.answer or "Saved answer: " + body["input"]["question"]
                }
                if owner.document:
                    output["artifacts"] = [
                        {
                            "schema": "agentlib.artifact/v1",
                            "document_id": "meeting",
                            "media_type": "text/markdown",
                            "created_at": "2026-10-13T12:00:00Z",
                            **owner.document,
                            "thread_id": body["thread_id"],
                            "request_id": self.headers["x-request-id"],
                        }
                    ]
                if owner.trace:
                    output["trace"] = owner.trace
                if owner.streaming:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    identity = {
                        "thread_id": body["thread_id"],
                        "request_id": self.headers["x-request-id"],
                    }

                    def emit(name: str, **value: object) -> None:
                        self.wfile.write(
                            f"event: {name}\ndata: {json.dumps({**identity, **value})}\n\n".encode()
                        )
                        self.wfile.flush()

                    emit("draft", call=1, text="", reset=True)
                    for text in ("Draft ", "arrives ", "before final"):
                        emit("draft", call=1, text=text, reset=False)
                        time.sleep(0.15)
                    emit(
                        "error" if owner.stream_failure else "result",
                        **(
                            {"error": "invalid"}
                            if owner.stream_failure
                            else {
                                "output": {
                                    **output,
                                    "answer": owner.answer or "Saved streamed answer",
                                }
                            }
                        ),
                        model_calls=1,
                        usage={
                            "source": "langchain",
                            "kind": "reported",
                            "input_tokens": 123,
                            "output_tokens": 45,
                            "cache_read_tokens": 12,
                            "cache_write_tokens": 100,
                        },
                    )
                    return
                time.sleep(owner.delay)
                data = json.dumps(
                    {
                        "thread_id": body["thread_id"],
                        "request_id": self.headers["x-request-id"],
                        "output": output,
                    }
                ).encode()
                owner.outcomes[self.headers["x-request-id"]] = json.loads(data)
                if owner.lose_response:
                    return  # The agent completed, but its response did not reach the UI.
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = urlsplit(self.path)
                if path.path.endswith("/inspection"):
                    request_id = path.path.split("/")[-2]
                    thread_id = parse_qs(path.query).get("thread_id", [""])[0]
                    owner.inspection_reads.append((thread_id, request_id))
                    section = {
                        "availability": "available",
                        "record_count": 1,
                        "text": '<script>alert("diagnostic")</script> ' + request_id,
                    }
                    data = json.dumps(
                        {
                            "schema": "agentlib.turn-inspection/v1",
                            "scope": {
                                "tenant": "dev-tenant",
                                "subject": "dev-user",
                                "application": "memo",
                            },
                            "thread_id": thread_id,
                            "request_id": request_id,
                            "model_inputs": section,
                            "model_outputs": section,
                            "mcp": section,
                            "checkpoints": section,
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                envelope = owner.outcomes.get(path.path.rsplit("/", 1)[-1])
                if (
                    not envelope
                    or envelope["thread_id"] != parse_qs(path.query).get("thread_id", [""])[0]
                ):
                    envelope = {"state": "unknown"}
                data = json.dumps(envelope).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
                pass

        self.agent = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
        self.agent_thread = threading.Thread(target=self.agent.serve_forever, daemon=True)
        self.agent_thread.start()
        url = f"http://127.0.0.1:{self.agent.server_port}"
        (path / "agents.json").write_text(
            json.dumps(
                {
                    "schema": "agentlib.registry/v1",
                    "agents": [
                        {"id": name, "version": "1", "owner": "team", "url": url}
                        for name in ("memo", "helper")
                    ],
                }
            )
        )
        (path / "tools.json").write_text('{"schema":"agentlib.registry/v1","servers":[]}')
        (path / "agent.env").write_text(
            'AC2_REGISTRY_OPTIONS={"agents_path":"agents.json","tools_path":"tools.json"}\n'
        )
        self.config = path / ".agentlib/ui/config.json"
        self.env = {key: value for key, value in os.environ.items() if key != "LITERAL_API_KEY"}
        result = self.command(
            "init",
            "--env-file",
            str(path / "agent.env"),
            "--agent",
            "memo",
            "--agent",
            "helper",
            "--artifacts-agent",
            "memo",
            "--allow-url",
            url,
            "--port",
            str(self.port),
        )
        assert result.returncode == 0, result.stderr
        self.log = (path / "server.log").open("a", encoding="utf-8")
        self.process: subprocess.Popen[str] | None = None

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed module in this test's interpreter
            python_command("-m", "ai_agent_lib_ui", *args, "--config", str(self.config)),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=30,
        )

    def start(self) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - fixed module and owned test configuration
            python_command("-m", "ai_agent_lib_ui", "serve", "--config", str(self.config)),
            stdout=self.log,
            stderr=self.log,
            text=True,
            env=self.env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        with httpx.Client(trust_env=False) as client:
            for _ in range(250):
                if self.process.poll() is not None:
                    pytest.fail((self.path / "server.log").read_text(encoding="utf-8"))
                try:
                    if client.get(self.origin + "/healthz", timeout=0.2).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
        pytest.fail("Local UI did not start: " + (self.path / "server.log").read_text())

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def close(self) -> None:
        self.stop()
        self.log.close()
        self.agent.shutdown()
        self.agent.server_close()
        self.agent_thread.join(timeout=5)

    @asynccontextmanager
    async def login(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url=self.origin, trust_env=False, headers={"origin": self.origin}, timeout=15
        ) as client:
            code = self.config.with_name("access-code").read_text().strip()
            assert (await client.post("/login", data={"code": code})).status_code == 303
            response = await client.post("/chat/auth/header")
            assert response.status_code == 200, response.text
            yield client


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalServer]:
    value = LocalServer(tmp_path_factory.mktemp("local-ui"))
    try:
        value.start()
        yield value
    finally:
        value.close()
        assert "was never awaited" not in (value.path / "server.log").read_text(encoding="utf-8")


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


async def connect(
    server: LocalServer, client: httpx.AsyncClient, thread: str | None = None, profile: str = "memo"
) -> Any:
    sock = socketio.AsyncClient(reconnection=False)
    sock.events_seen = []

    async def event(name: str, data: object = None) -> None:
        sock.events_seen.append((name, data))

    sock.on("*", event)

    cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    await sock.connect(
        server.origin,
        socketio_path="chat/ws/socket.io",
        headers={"Cookie": cookie, "Origin": server.origin},
        transports=["websocket"],
        auth={
            "sessionId": str(uuid4()),
            "clientType": "webapp",
            "chatProfile": profile,
            **({"threadId": thread} if thread else {}),
        },
    )
    await sock.call("connection_successful", timeout=10)
    return sock


async def wait_thread(client: httpx.AsyncClient, excluded: set[str]) -> str:
    for _ in range(100):
        response = await client.get("/api/threads")
        assert response.status_code == 200, response.text
        for thread in response.json()["items"]:
            if thread["thread_id"] not in excluded:
                return str(thread["thread_id"])
        await asyncio.sleep(0.05)
    pytest.fail("No application-owned thread was created")


async def send(sock: Any, thread: str, question: str, key: str | None = None) -> str:
    key = key or str(uuid4())
    await sock.emit(
        "client_message",
        {
            "message": {
                "id": key,
                "threadId": thread,
                "name": "User",
                "type": "user_message",
                "output": question,
                "createdAt": "2026-10-11T00:00:00Z",
                "metadata": {},
            }
        },
    )
    return key


async def wait_turns(
    client: httpx.AsyncClient, thread: str, count: int, *, allow_failed: bool = False
) -> list[dict[str, Any]]:
    states = {"completed", "failed"} if allow_failed else {"completed"}
    for _ in range(160):
        data = (await client.get(f"/api/threads/{thread}")).json()
        turns = data.get("turns", [])
        if len(turns) == count and all(t["run"]["state"] in states for t in turns):
            return list(turns)
        await asyncio.sleep(0.05)
    pytest.fail("Expected terminal transcript: " + json.dumps(data))


async def test_real_chat_two_questions_refresh_and_restart(server: LocalServer) -> None:
    async with server.login() as client:
        before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
        sock = await connect(server, client)
        try:
            thread = await wait_thread(client, before)
            first = await send(sock, thread, "Prepare a memo")
            await wait_turns(client, thread, 1)
            await send(sock, thread, "Explain the memo")
            turns = await wait_turns(client, thread, 2)
        finally:
            await sock.disconnect()
        calls = len(server.calls)
        resumed = await connect(server, client, thread)
        try:
            restored = [data for name, data in resumed.events_seen if name == "resume_thread"]
            assert restored
            assert [
                step["output"].split("\n\n---\n\n")[0]
                for step in restored[-1]["steps"]
                if step["type"] in {"user_message", "assistant_message"}
            ] == [
                "Prepare a memo",
                "Saved answer: Prepare a memo",
                "Explain the memo",
                "Saved answer: Explain the memo",
            ]
        finally:
            await resumed.disconnect()
        server.stop()
        server.start()
        restored_json = (await client.get(f"/api/threads/{thread}")).json()
        assert restored_json["turns"] == turns
        assert len(server.calls) == calls
        # Even after restart a duplicate send returns the original accepted run.
        duplicate = await client.post(
            f"/api/threads/{thread}/messages",
            json={"question": "Prepare a memo", "idempotency_key": first},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["run_id"] == turns[0]["run"]["run_id"]
        assert len(server.calls) == calls
        assert all(set(call) == {"input", "thread_id"} for call in server.calls)


async def test_disconnect_reattaches_and_agent_selection_is_immutable(server: LocalServer) -> None:
    async with server.login() as client:
        before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
        sock = await connect(server, client, profile="helper")
        thread = await wait_thread(client, before)
        server.delay = 0.6
        calls = len(server.calls)
        try:
            await send(sock, thread, "Continue after disconnect")
            for _ in range(100):
                if len(server.calls) > calls:
                    break
                await asyncio.sleep(0.02)
            await sock.disconnect()
            resumed = await connect(server, client, thread, profile="memo")
            try:
                restored = [data for name, data in resumed.events_seen if name == "resume_thread"]
                assert restored[-1]["metadata"]["chat_profile"] == "helper"
                assert restored[-1]["steps"][-1]["output"].startswith(
                    "Saved answer: Continue after disconnect"
                )
                assert len(server.calls) == calls + 1
            finally:
                await resumed.disconnect()
        finally:
            server.delay = 0
            if sock.connected:
                await sock.disconnect()


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
                await anonymous.get(path, headers={"x-agentlib-user": "dev-user"})
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


async def test_agent_lab_branding_on_login_chat_and_parent_pages(server: LocalServer) -> None:
    """Installed wheels supply the same icon before sign-in and inside mounted chat."""
    async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as anonymous:
        login = await anonymous.get("/login")
        assert "<title>Sign in locally · Agent Lab</title>" in login.text
        assert "href='/favicon'" in login.text
        assert "src='/ui/agent-lab.svg'" in login.text
        icon = await anonymous.get("/ui/agent-lab.svg")
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg+xml")
        assert '<title id="title">Agent Lab</title>' in icon.text
        favicon = await anonymous.get("/favicon")
        assert favicon.status_code == 200
        assert favicon.content == icon.content
        from xml.etree import ElementTree

        artwork = ElementTree.fromstring(icon.content)  # noqa: S314 - bundled local SVG
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        assert artwork.findall("svg:rect", namespace) == []
        assert len(artwork.findall("svg:g/svg:circle", namespace)) == 3
        assert "currentColor" in icon.text
        assert "prefers-color-scheme: dark" in icon.text
        assert "#777" in icon.text
        assert "#ccc" in icon.text
        theme = await anonymous.get("/ui/theme.css")
        assert '.dark img[src*="/ui/agent-lab.svg"]' in theme.text
        assert "brightness(0) invert(1)" in theme.text
        assert (
            await anonymous.get("/favicon", headers={"host": "evil.example"})
        ).status_code == 403
        # Public branding must not expose neighboring shell assets or private settings.
        for path in ("/ui/private.svg", "/chat/public/chat.css", "/chat/project/settings"):
            assert (await anonymous.get(path)).status_code == 401
    async with server.login() as client:
        chat = await client.get("/chat/")
        assert "<title>Agent Lab</title>" in chat.text
        assert 'href="/chat/favicon"' in chat.text
        settings = (await client.get("/chat/project/settings")).json()
        assert settings["ui"]["name"] == "Agent Lab"
        assert settings["ui"]["default_avatar_file_url"] == "/ui/agent-lab.svg"
        assert any(link["display_name"] == "Agent Lab" for link in settings["ui"]["header_links"])
        for path in ("/chat/favicon", "/chat/logo?theme=light", "/chat/logo?theme=dark"):
            image = await client.get(path)
            assert image.status_code == 200
            assert image.content == icon.content
        for path, title in (("/catalog", "Choose agent"), ("/conversations", "Conversations")):
            page = await client.get(path)
            assert f"<title>{title} · Agent Lab</title>" in page.text
            assert "src='/ui/agent-lab.svg'" in page.text


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


async def test_welcome_guidance_stays_out_of_new_and_restored_transcripts(
    server: LocalServer,
) -> None:
    """Chainlit owns welcome placement; guidance must never become a chat message."""
    async with server.login() as client:
        settings = (await client.get("/chat/project/settings")).json()
        profiles = settings["chatProfiles"]
        assert {profile["name"] for profile in profiles} == {"memo", "helper"}
        for profile in profiles:
            assert profile["icon"] == "/ui/agent-lab.svg"
            note = profile["markdown_description"]
            assert "Welcome to Agent Lab" in note
            assert f"Selected agent: **{profile['name']}**" in note
            assert "[choose another agent](/catalog)" in note
        before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
        calls = len(server.calls)
        sock = await connect(server, client, profile="helper")
        try:
            thread = await wait_thread(client, before)
            assert not [
                value
                for name, value in sock.events_seen
                if name == "new_message" and value.get("output")
            ]
            assert (await client.get(f"/api/threads/{thread}")).json()["turns"] == []
        finally:
            await sock.disconnect()
        resumed = await connect(server, client, thread)
        try:
            restored = [v for name, v in resumed.events_seen if name == "resume_thread"][-1]
            assert restored["steps"] == []
            assert restored["metadata"]["chat_profile"] == "helper"
            assert len(server.calls) == calls
        finally:
            await resumed.disconnect()


async def test_shared_theme_assets_and_navigation_are_available_on_all_pages(
    server: LocalServer,
) -> None:
    """Packaged styles also work on sign-in without exposing protected app assets."""
    async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as anonymous:
        for path, media in (("/ui/theme.css", "text/css"), ("/ui/theme.js", "text/javascript")):
            asset = await anonymous.get(path)
            assert asset.status_code == 200
            assert asset.headers["content-type"].startswith(media)
            assert "unsafe-eval" not in asset.headers["content-security-policy"]
            assert (await anonymous.get(path, headers={"host": "evil.example"})).status_code == 403
        assert (await anonymous.get("/ui/private.css")).status_code == 401
    async with server.login() as client:
        for path in ("/login", "/catalog", "/logs", "/conversations"):
            response = await client.get(path)
            assert "href='/ui/theme.css'" in response.text
            assert "src='/ui/theme.js'" in response.text
            assert "aria-label='Theme'" in response.text
            assert "class='agent-lab-page'" in response.text
        assert "href='/catalog' aria-current='page'" in (await client.get("/catalog")).text
        assert "href='/logs' aria-current='page'" in (await client.get("/logs")).text
        css = await client.get("/chat/public/chat.css")
        assert '@import url("/ui/theme.css")' in css.text


async def test_versioned_chat_assets_are_served_after_shell_preparation(
    server: LocalServer,
) -> None:
    config_path = server.config.parent / "shell/.chainlit/config.toml"
    ui = tomllib.loads(config_path.read_text(encoding="utf-8"))["UI"]
    async with server.login() as client:
        for key, name in (("custom_css", "chat.css"), ("custom_js", "memos.js")):
            url = ui[key]
            assert url.startswith(f"/public/{name}?v=")
            response = await client.get("/chat" + url)
            assert response.status_code == 200
            assert response.content == (server.config.parent / "shell/public" / name).read_bytes()


async def test_choose_agent_memo_versions_and_logs_survive_restart(server: LocalServer) -> None:
    async with server.login() as client:
        page = await client.get("/catalog")
        assert "Start chat with memo" in page.text
        assert "Start chat with helper" in page.text
        response = await client.post("/catalog/start", data={"agent_id": "memo"})
        assert response.status_code == 303
        thread = response.headers["location"].split("/")[-1]
        assert (await client.get(f"/api/threads/{thread}")).json()["thread"]["scope"][
            "application"
        ] == "memo"
        other = (await client.post("/api/threads", json={"agent_id": "helper"})).json()["thread"][
            "thread_id"
        ]
        assert (
            await client.post("/catalog/start", data={"agent_id": "unapproved"})
        ).status_code == 404
        runs = []
        try:
            for version in (1, 2):
                server.streaming = version == 2
                server.document = {
                    "id": f"version-{version}",
                    "version": version,
                    "title": "Acme memo",
                    "content": f"# Memo version {version}\n\nCash is {version * 10}.",
                }
                runs.append(
                    (
                        await client.post(
                            f"/api/threads/{thread}/messages",
                            json={
                                "idempotency_key": str(uuid4()),
                                "question": f"Prepare memo {version}",
                            },
                        )
                    ).json()
                )
            server.document = None
            await client.post(
                f"/api/threads/{thread}/messages",
                json={"idempotency_key": str(uuid4()), "question": "Explain the memo"},
            )
        finally:
            server.streaming = False
            server.document = None
        calls = len(server.calls)
        for _ in range(2):
            listed = await client.get(f"/api/threads/{thread}/documents")
            assert [item["version"] for item in listed.json()["items"]] == [2, 1]
            assert "content" not in listed.json()["items"][0]
            assert (
                "<h1>Memo version 1</h1>"
                in (await client.get(f"/api/threads/{thread}/documents/version-1")).text
            )
            assert (
                "<h1>Memo version 2</h1>"
                in (await client.get(f"/api/threads/{thread}/documents/version-2")).text
            )
            assert (
                await client.get(f"/api/threads/{other}/documents/version-1")
            ).status_code == 404
            logs = await client.get("/logs", params={"thread_id": thread})
            assert "Prepare memo 1" in logs.text
            assert "Explain the memo" in logs.text
            assert runs[0]["request_id"] in logs.text
            assert len(server.calls) == calls
            if _ == 0:
                server.stop()
                server.start()
        resumed = await connect(server, client, thread)
        try:
            restored = [value for name, value in resumed.events_seen if name == "resume_thread"][-1]
            assert restored["metadata"]["chat_profile"] == "memo"
            assert restored["steps"][0]["output"] == "Prepare memo 1"
        finally:
            await resumed.disconnect()
    async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as anonymous:
        assert (await anonymous.get(f"/api/threads/{thread}/documents")).status_code == 401
        assert (await anonymous.get("/logs")).status_code == 303


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


async def test_rename_search_archive_restore_and_cli_lock(server: LocalServer) -> None:
    async with server.login() as client:
        created = (await client.post("/api/threads", json={"agent_id": "memo"})).json()
        thread = created["thread"]["thread_id"]
        renamed = await client.post(
            f"/conversations/{thread}", data={"title": "Unique meeting", "action": "rename"}
        )
        assert renamed.status_code == 303
        result = (await client.get("/api/threads", params={"search": "Unique"})).json()
        assert [t["thread_id"] for t in result["items"]] == [thread]
        await client.post(
            f"/conversations/{thread}", data={"title": "ignored", "action": "archive"}
        )
        assert (await client.get("/api/threads", params={"search": "Unique"})).json()["items"] == []
        await client.post(
            f"/conversations/{thread}", data={"title": "ignored", "action": "restore"}
        )
        assert (await client.get("/api/threads", params={"search": "Unique"})).json()["items"]
        assert server.command("check").returncode == 0
        locked = server.command("serve")
        assert locked.returncode == 2
        assert "another UI server" in locked.stderr


@pytest.mark.parametrize("failed", [False, True])
async def test_live_step_is_removed_and_final_metrics_survive_resume(
    server: LocalServer, failed: bool
) -> None:
    server.streaming = True
    server.stream_failure = failed
    try:
        async with server.login() as client:
            before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
            sock = await connect(server, client)
            try:
                thread = await wait_thread(client, before)
                await send(sock, thread, "Stream a response")
                turns = await wait_turns(client, thread, 1, allow_failed=failed)
                await asyncio.sleep(0.3)
                removed = [value for name, value in sock.events_seen if name == "delete_message"]
                progress = [
                    value
                    for name, value in sock.events_seen
                    if name in {"stream_start", "stream_token"}
                ]
                assert any(
                    "Draft arrives" in value.get("token", value.get("output", ""))
                    for value in progress
                )
                starts = [
                    value
                    for name, value in sock.events_seen
                    if name == "new_message" and value.get("name", "").startswith("Live draft")
                ]
                assert starts[-1]["defaultOpen"] is True
                assert starts[-1]["start"]
                assert not starts[-1].get("end")
                assert [value["id"] for value in removed] == [starts[-1]["id"]]
                assert not any(
                    value.get("name") == "Live draft finished"
                    for name, value in sock.events_seen
                    if name in {"new_message", "update_message"}
                )
                answers = [
                    value
                    for name, value in sock.events_seen
                    if name == "new_message" and value.get("type") == "assistant_message"
                ]
                assert "Input tokens: 123" in answers[-1]["output"]
                assert "Cache hit: 9.8%" in answers[-1]["output"]
                assert "Cache read: 12" in answers[-1]["output"]
                assert "Cache write: 100" in answers[-1]["output"]
                assert "Output tokens: 45" in answers[-1]["output"]
                assert "Time:" in answers[-1]["output"]
                assert turns[0]["run"]["state"] == ("failed" if failed else "completed")
                assert turns[0]["run"]["answer"] == (None if failed else "Saved streamed answer")
                if failed:
                    assert "passed its checks" in answers[-1]["output"]
                    assert "unknown" not in answers[-1]["output"]
            finally:
                await sock.disconnect()
            resumed = await connect(server, client, thread)
            try:
                restored = [
                    value for name, value in resumed.events_seen if name == "resume_thread"
                ][-1]
                assert len(restored["steps"]) == 2
                assert restored["steps"][-1]["output"] == answers[-1]["output"]
                assert all(step["type"] != "tool" for step in restored["steps"])
                if failed:
                    before_calls = len(server.calls)
                    server.stream_failure = False
                    await send(resumed, thread, "A new question after the known failure")
                    later = await wait_turns(client, thread, 2, allow_failed=True)
                    assert later[-1]["run"]["state"] == "completed"
                    assert len(server.calls) == before_calls + 1
            finally:
                await resumed.disconnect()
    finally:
        server.streaming = False
        server.stream_failure = False


async def test_resume_recovers_lost_response_without_another_agent_invocation(
    server: LocalServer,
) -> None:
    async with server.login() as client:
        thread = (await client.post("/api/threads", json={"agent_id": "memo"})).json()["thread"][
            "thread_id"
        ]
        before = len(server.calls)
        server.lose_response = True
        try:
            run = (
                await client.post(
                    f"/api/threads/{thread}/messages",
                    json={
                        "idempotency_key": str(uuid4()),
                        "question": "Recover this accepted answer",
                    },
                )
            ).json()
            assert run["state"] == "unknown"
        finally:
            server.lose_response = False
        resumed = await connect(server, client, thread)
        try:
            turns = await wait_turns(client, thread, 1)
            assert turns[0]["run"]["answer"] == "Saved answer: Recover this accepted answer"
            restored = [value for name, value in resumed.events_seen if name == "resume_thread"][-1]
            assert "Saved answer: Recover this accepted answer" in restored["steps"][-1]["output"]
            assert len(server.calls) == before + 1
        finally:
            await resumed.disconnect()


@pytest.mark.parametrize("mode", ["json", "stream", "reconcile"])
async def test_trace_link_and_metrics_survive_resume(server: LocalServer, mode: str) -> None:
    trace_id = uuid4().hex
    url = f"https://traces.example/redirects/traces/{trace_id}"
    server.trace = {"provider": "phoenix", "trace_id": trace_id, "url": url}
    server.streaming = mode == "stream"
    server.lose_response = mode == "reconcile"
    try:
        async with server.login() as client:
            thread = (await client.post("/api/threads", json={"agent_id": "memo"})).json()[
                "thread"
            ]["thread_id"]
            before = len(server.calls)
            result = (
                await client.post(
                    f"/api/threads/{thread}/messages",
                    json={
                        "idempotency_key": str(uuid4()),
                        "question": "A traced question",
                    },
                )
            ).json()
            assert result["state"] == ("unknown" if mode == "reconcile" else "completed")
            server.lose_response = False
            resumed = await connect(server, client, thread)
            try:
                turns = await wait_turns(client, thread, 1)
                assert turns[0]["run"]["trace"] == server.trace
                restored = [v for n, v in resumed.events_seen if n == "resume_thread"][-1]
                text = restored["steps"][-1]["output"]
                assert f"[Open trace in Agent Lab]({url})" in text
                assert "Open trace in Phoenix" not in text
                assert "Input tokens:" in text
                assert "Output tokens:" in text
                assert "Time:" in text
                assert len(server.calls) == before + 1
            finally:
                await resumed.disconnect()
    finally:
        server.trace = None
        server.streaming = False
        server.lose_response = False


@pytest.mark.parametrize("streaming", [False, True])
async def test_comparison_sources_render_live_and_on_resume_without_changing_saved_answer(
    server: LocalServer, streaming: bool
) -> None:
    reference = "financial_facts[uen=CANA-0010;concept=Revenues;period_end=2025-12-31]"
    raw = (
        "| Metric | Canada Goose |\n| --- | --- |\n"
        f"| Revenue | USD 919,811,320.76 <br> [{reference}] |\n\n"
        f"Revenue comparison [{reference}]."
    )
    server.answer = raw
    server.streaming = streaming
    try:
        async with server.login() as client:
            before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
            sock = await connect(server, client)
            try:
                thread = await wait_thread(client, before)
                await send(sock, thread, "Compare client financials")
                turns = await wait_turns(client, thread, 1)
                for _ in range(100):
                    answers = [
                        value
                        for name, value in sock.events_seen
                        if name == "new_message" and value.get("type") == "assistant_message"
                    ]
                    references = [
                        value
                        for name, value in sock.events_seen
                        if name == "new_message" and value.get("name") == "References"
                    ]
                    if answers:
                        break
                    await asyncio.sleep(0.02)
                display = answers[-1]["output"]
                assert "| Revenue | USD 919,811,320.76 |" in display
                assert "financial_facts[" not in display
                assert "References" not in display
                assert "<br>" not in display
                assert references == []
                assert not any(
                    value.get("name") == "Turn inspector"
                    for name, value in sock.events_seen
                    if name == "new_message"
                )
                assert "Input tokens:" in display
                assert "Time:" in display
                assert turns[0]["run"]["answer"] == raw
            finally:
                await sock.disconnect()
            calls = len(server.calls)
            resumed = await connect(server, client, thread)
            try:
                restored = [v for name, v in resumed.events_seen if name == "resume_thread"][-1]
                assert len(restored["steps"]) == 2
                assert restored["steps"][-1]["output"] == display
                history = await client.get(f"/conversations/{thread}/history")
                assert "<summary>References</summary>" not in history.text
                assert "Turn inspector" not in history.text
                assert "Input tokens:" in history.text
                assert "Time:" in history.text
                assert "&lt;br&gt;" not in history.text
                assert len(server.calls) == calls
            finally:
                await resumed.disconnect()
    finally:
        server.answer = None
        server.streaming = False


async def test_long_chat_resume_links_to_escaped_older_history(server: LocalServer) -> None:
    async with server.login() as client:
        thread = (await client.post("/api/threads", json={"agent_id": "memo"})).json()["thread"][
            "thread_id"
        ]
        for index in range(52):
            reply = await client.post(
                f"/api/threads/{thread}/messages",
                json={"idempotency_key": str(uuid4()), "question": f"<b>Question {index}</b>"},
            )
            assert reply.json()["state"] == "completed"
        recent = (await client.get(f"/api/threads/{thread}")).json()
        assert len(recent["turns"]) == 50
        assert recent["next_cursor"]
        older = await client.get(
            f"/conversations/{thread}/history", params={"cursor": recent["next_cursor"]}
        )
        assert older.status_code == 200
        assert "&lt;b&gt;Question 0&lt;/b&gt;" in older.text
        assert "<b>Question 0</b>" not in older.text
        resumed = await connect(server, client, thread)
        try:
            restored = [value for name, value in resumed.events_seen if name == "resume_thread"][-1]
            assert len(restored["steps"]) == 101
            assert "View older messages" in restored["steps"][0]["output"]
        finally:
            await resumed.disconnect()


async def test_inspector_remains_scoped_and_read_only_without_chat_panels(
    server: LocalServer,
) -> None:
    async with server.login() as client:
        before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
        sock = await connect(server, client, profile="memo")
        thread = await wait_thread(client, before)
        reads = len(server.inspection_reads)
        try:
            await send(sock, thread, "First inspected question")
            await wait_turns(client, thread, 1)
            await send(sock, thread, "Second inspected question")
            turns = await wait_turns(client, thread, 2)
            assert not any(
                value.get("name") == "Turn inspector"
                for name, value in sock.events_seen
                if name == "new_message"
            )
            assert len(server.inspection_reads) == reads
            calls = len(server.calls)
            for turn in turns:
                run = turn["run"]
                url = f"/conversations/{thread}/turns/{run['run_id']}/inspect"
                response = await client.get(url)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.text.count("<details>") == 4
                assert "<details open" not in response.text
                assert all(
                    title in response.text
                    for title in ("LLM input", "LLM output", "MCP calls", "Checkpoints")
                )
                assert "&lt;script&gt;" in response.text
                assert "<script>alert" not in response.text
                assert server.inspection_reads[-1] == (thread, run["request_id"])
            wrong = await client.get(
                f"/conversations/absent/turns/{turns[0]['run']['run_id']}/inspect"
            )
            assert wrong.status_code == 404
            wrong = await client.get(f"/conversations/{thread}/turns/absent/inspect")
            assert wrong.status_code == 404
            assert len(server.calls) == calls
            assert len(server.inspection_reads) == reads + 2
        finally:
            await sock.disconnect()
