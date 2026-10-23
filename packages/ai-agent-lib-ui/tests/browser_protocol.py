"""A real UI server process with a scripted HTTP agent, plus Socket.IO helpers for tests."""

import asyncio
import inspect
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
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
    """Pick a free loopback port by binding and releasing it.

    The UI process receives its port on the command line before it exists, so the
    port has to be chosen here and handed over; the bind-then-release leaves a small
    window in which another process could take it.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def poll(
    condition: Callable[[], bool], *, timeout: float = 8.0, interval: float = 0.05
) -> None:
    """Wait until ``condition()`` holds; fail with the condition's source at the deadline."""
    try:
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(interval)
    except TimeoutError:
        try:
            source = inspect.getsource(condition).strip()
        except (OSError, TypeError):
            source = getattr(condition, "__qualname__", repr(condition))
        pytest.fail(f"Condition not met within {timeout}s: {source}")


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
        self.config = path / ".agentcraft2/ui/config.json"
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

    def reset(self) -> None:
        """Restore the default steering so each test starts from the same fake agent."""
        self.delay = 0.0
        self.streaming = False
        self.stream_failure = False
        self.lose_response = False
        self.answer = None
        self.document = None
        self.trace = None

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
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(  # noqa: S603 - fixed module and owned test configuration
            python_command("-m", "ai_agent_lib_ui", "serve", "--config", str(self.config)),
            stdout=self.log,
            stderr=self.log,
            text=True,
            env=self.env,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + 25
        with httpx.Client(trust_env=False) as client:
            while time.monotonic() < deadline:
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


async def wait_thread(
    client: httpx.AsyncClient, excluded: set[str], *, timeout: float = 5.0
) -> str:
    """Return the first thread the server created that is not in ``excluded``."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                response = await client.get("/api/threads")
                assert response.status_code == 200, response.text
                for thread in response.json()["items"]:
                    if thread["thread_id"] not in excluded:
                        return str(thread["thread_id"])
                await asyncio.sleep(0.05)
    except TimeoutError:
        pytest.fail(f"No application-owned thread was created within {timeout}s")


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
    client: httpx.AsyncClient,
    thread: str,
    count: int,
    *,
    allow_failed: bool = False,
    timeout: float = 8.0,
) -> list[dict[str, Any]]:
    """Return the transcript once it holds exactly ``count`` terminal turns."""
    states = {"completed", "failed"} if allow_failed else {"completed"}
    data: dict[str, Any] = {}
    try:
        async with asyncio.timeout(timeout):
            while True:
                data = (await client.get(f"/api/threads/{thread}")).json()
                turns = data.get("turns", [])
                if len(turns) == count and all(t["run"]["state"] in states for t in turns):
                    return list(turns)
                await asyncio.sleep(0.05)
    except TimeoutError:
        pytest.fail("Expected terminal transcript: " + json.dumps(data))
