"""Exercise the installed shell over real HTTP and Socket.IO with a scripted agent."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import socketio  # type: ignore[import-untyped]


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
        owner = self

        class AgentHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(body)
                time.sleep(owner.delay)
                data = json.dumps(
                    {
                        "thread_id": body["thread_id"],
                        "request_id": self.headers["x-request-id"],
                        "output": {"answer": "Saved answer: " + body["input"]["question"]},
                    }
                ).encode()
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
            'EAP_REGISTRY_OPTIONS={"agents_path":"agents.json","tools_path":"tools.json"}\n'
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
            [sys.executable, "-m", "ai_agent_lib_ui", *args, "--config", str(self.config)],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=30,
        )

    def start(self) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - fixed module and owned test configuration
            [sys.executable, "-m", "ai_agent_lib_ui", "serve", "--config", str(self.config)],
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


async def wait_turns(client: httpx.AsyncClient, thread: str, count: int) -> list[dict[str, Any]]:
    for _ in range(160):
        data = (await client.get(f"/api/threads/{thread}")).json()
        turns = data.get("turns", [])
        if len(turns) == count and all(t["run"]["state"] == "completed" for t in turns):
            return list(turns)
        await asyncio.sleep(0.05)
    pytest.fail("Expected completed transcript: " + json.dumps(data))


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
            assert [step["output"] for step in restored[-1]["steps"]] == [
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
                assert (
                    restored[-1]["steps"][-1]["output"] == "Saved answer: Continue after disconnect"
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
