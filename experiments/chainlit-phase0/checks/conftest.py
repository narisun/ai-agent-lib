"""Run protocol checks against an isolated, real loopback server."""

import asyncio
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import socketio
from spike.settings import ROOT, Settings
from spike.store import Store

ORIGIN = "http://127.0.0.1:8765"


class Server:
    def __init__(self, state):
        self.settings = Settings.local(state)
        self.store = Store(state / "sample.sqlite")
        self.log = (state / "server.log").open("a", encoding="utf-8")
        self.process = None

    def start(self):
        self.process = subprocess.Popen(  # noqa: S603 - fixed module, interpreter and test directory
            [sys.executable, "-m", "spike", "--state", str(self.settings.state)],
            cwd=ROOT,
            stdout=self.log,
            stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                if self.process.poll() is not None:
                    pytest.fail("Sample failed to start; see the check state directory/server.log")
                try:
                    if client.get(ORIGIN + "/healthz", timeout=0.3).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
        pytest.fail("Sample did not start within 10 seconds")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    @asynccontextmanager
    async def login(self, owner="demo/alice"):
        client = httpx.AsyncClient(
            base_url=ORIGIN, trust_env=False, headers={"origin": ORIGIN}, timeout=10
        )
        response = await client.post(
            "/login", data={"user": owner, "code": self.settings.access_code}
        )
        assert response.status_code == 303
        response = await client.post("/chat/auth/header")
        assert response.status_code == 200
        try:
            yield client
        finally:
            await client.aclose()


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    with httpx.Client(trust_env=False) as client:
        try:
            client.get(ORIGIN + "/healthz", timeout=0.3)
        except httpx.TransportError:
            pass
        else:
            pytest.fail("Stop the manual sample on port 8765 before running these checks")
    sample = Server(tmp_path_factory.mktemp("server"))
    sample.start()
    yield sample
    sample.stop()
    sample.log.close()


async def eventually(read, predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = await read()
        if predicate(value):
            return value
        await asyncio.sleep(0.05)
    pytest.fail("Expected state did not arrive before the deadline")


async def connect(client, *, thread=None, session=None, profile="rmprep-demo"):
    sock = socketio.AsyncClient(reconnection=False)
    sock.events_seen = []

    @sock.on("*")
    async def event(name, data=None):
        sock.events_seen.append((name, data))

    cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    await sock.connect(
        ORIGIN,
        socketio_path="chat/ws/socket.io",
        headers={"Cookie": cookie, "Origin": ORIGIN},
        transports=["websocket"],
        auth={
            "sessionId": session or str(uuid.uuid4()),
            "clientType": "webapp",
            "chatProfile": profile,
            **({"threadId": thread} if thread else {}),
        },
    )
    await sock.call("connection_successful", timeout=8)
    return sock


async def send_question(sock, thread, question, run_id=None):
    run_id = run_id or str(uuid.uuid4())
    await sock.emit(
        "client_message",
        {
            "message": {
                "id": run_id,
                "threadId": thread,
                "name": "User",
                "type": "user_message",
                "output": question,
                "createdAt": "2026-10-11T00:00:00Z",
                "metadata": {},
            }
        },
    )
    return run_id


async def new_thread(client, profile="rmprep-demo", session=None):
    before = {t["id"] for t in (await client.get("/api/threads")).json()}
    sock = await connect(client, profile=profile, session=session)

    async def read():
        return (await client.get("/api/threads")).json()

    threads = await eventually(read, lambda ts: any(t["id"] not in before for t in ts))
    return sock, next(t["id"] for t in threads if t["id"] not in before)


async def completed(client, run_id):
    async def read():
        response = await client.get(f"/api/runs/{run_id}")
        return response.json() if response.status_code == 200 else {}

    return await eventually(read, lambda run: run.get("status") == "completed")
