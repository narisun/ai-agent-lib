"""Exercise real HTTP and Socket.IO paths, including negative access checks."""

import asyncio
import uuid

import httpx
import pytest
import socketio
from conftest import ORIGIN, completed, connect, eventually, new_thread, send_question


@pytest.mark.parametrize(
    "path",
    [
        "/api/threads",
        "/api/catalog",
        "/chat/project/settings",
        "/chat/project/thread/missing",
        "/chat/project/share/missing",
        "/chat/project/file/missing",
        "/chat/public/elements/Metrics.jsx",
        "/artifacts/missing",
        "/inspector/missing",
        "/api/runs/missing/export",
    ],
)
async def test_anonymous_and_forged_headers_are_rejected(server, path):
    async with httpx.AsyncClient(base_url=ORIGIN, trust_env=False) as client:
        response = await client.get(path, headers={"x-phase0-user": "demo/alice"})
        assert response.status_code == 401


async def test_host_origin_and_chat_identity_are_bound(server):
    async with server.login() as alice, server.login("demo/bob") as bob:
        assert (
            await alice.get("/api/catalog", headers={"host": "evil.example"})
        ).status_code == 403
        assert (
            await alice.post("/logout", headers={"origin": "https://evil.example"})
        ).status_code == 403
        alice.cookies.set("phase0_chainlit", bob.cookies.get("phase0_chainlit"))
        assert (await alice.get("/api/threads")).status_code == 401
    async with httpx.AsyncClient(base_url=ORIGIN, trust_env=False) as anonymous:
        with pytest.raises(socketio.exceptions.ConnectionError):
            await connect(anonymous)


async def test_two_turns_history_artifacts_and_inspector_survive_restart(server):
    async with server.login() as client:
        sock, thread = await new_thread(client)
        try:
            first = await send_question(sock, thread, "Generate DEMO-001 memo")
            await completed(client, first)
            second = await send_question(sock, thread, "Explain gross margin")
            run = await completed(client, second)
            assert run["thread_id"] == thread
            assert "25%" in run["result"]["answer"]
            model_input = run["result"]["models"][1]["input"]
            assert model_input[1]["content"] == "Generate DEMO-001 memo"
            assert model_input[-1]["role"] == "tool"
            assert len(run["result"]["checkpoints"][0]["saved_messages"]) == 2
            assert run["result"]["metrics"]["cache_read_tokens"] == 6000
            assert run["result"]["elapsed_seconds"] >= 2
        finally:
            await sock.disconnect()
        server.stop()
        server.start()
        restored = await connect(client, thread=thread)
        try:

            async def read_events():
                return restored.events_seen

            events = await eventually(
                read_events, lambda es: any(e[0] == "resume_thread" for e in es)
            )
            snapshot = next(data for event, data in events if event == "resume_thread")
            assert len(snapshot["steps"]) == 4
            assert all(step.get("parentId") is None for step in snapshot["steps"])
            assert len(snapshot["elements"]) == 4
            memo_names = [e["name"] for e in snapshot["elements"] if e["type"] == "text"]
            assert len(set(memo_names)) == 2
            assert (await client.get(f"/artifacts/{first}")).text.startswith("# Client")
            assert (await client.get(f"/inspector/{second}")).status_code == 200
            assert (await client.get(f"/api/runs/{second}/export")).json()["id"] == second
            with server.store.connect() as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM invocations WHERE thread_id=?", (thread,)
                    ).fetchone()[0]
                    == 2
                )
        finally:
            await restored.disconnect()


async def test_disconnect_completes_once_and_restores_elements(server):
    async with server.login() as client:
        sock, thread = await new_thread(client)
        run_id = await send_question(sock, thread, "Disconnect during memo")

        async def read_run():
            return (await client.get(f"/api/runs/{run_id}")).status_code

        await eventually(read_run, lambda code: code == 200)
        await sock.disconnect()
        await completed(client, run_id)
        resumed = await connect(client, thread=thread)
        try:
            await send_question(resumed, thread, "Disconnect during memo", run_id)
            await completed(client, run_id)
            await asyncio.sleep(0.15)
            with server.store.connect() as db:
                assert (
                    db.execute("SELECT count(*) FROM invocations WHERE id=?", (run_id,)).fetchone()[
                        0
                    ]
                    == 1
                )
            snapshot = (await client.get(f"/chat/project/thread/{thread}")).json()
            assert len(snapshot["steps"]) == 2
            assert len(snapshot["elements"]) == 2
        finally:
            await resumed.disconnect()


@pytest.mark.parametrize("intruder", ["demo/bob", "other/alice"])
async def test_foreign_history_elements_downloads_and_socket_sessions_are_denied(server, intruder):
    async with server.login() as alice, server.login(intruder) as other:
        session_id = str(uuid.uuid4())
        sock, thread = await new_thread(alice, session=session_id)
        try:
            run_id = await send_question(sock, thread, "Private fixture memo")
            run = await completed(alice, run_id)
            paths = [
                f"/chat/project/thread/{thread}",
                f"/chat/project/share/{thread}",
                f"/chat/project/thread/{thread}/element/{run['result']['memo_id']}",
                f"/api/threads/{thread}/runs",
                f"/api/runs/{run_id}",
                f"/api/runs/{run_id}/export",
                f"/inspector/{run_id}",
                f"/artifacts/{run_id}",
                f"/artifacts/{run_id}?download=true",
            ]
            for path in paths:
                assert (await other.get(path)).status_code == 404, path
            history = await other.post(
                "/chat/project/threads",
                json={"pagination": {"first": 20}, "filter": {"userId": "demo/alice"}},
            )
            assert history.status_code == 200
            assert thread not in {t["id"] for t in history.json()["data"]}
            for kwargs in ({"thread": thread}, {"session": session_id}):
                with pytest.raises(socketio.exceptions.ConnectionError):
                    await connect(other, **kwargs)
        finally:
            await sock.disconnect()


async def test_revoked_live_socket_cannot_invoke_and_assets_are_local(server):
    async with server.login() as client:
        sock, thread = await new_thread(client, profile="help-demo")
        try:
            response = await client.get("/chat/")
            policy = response.headers["content-security-policy"]
            assert "style-src 'self' 'unsafe-inline'" in policy
            assert "https:" not in policy
            assert (await client.get("/chat/public/spike.css")).status_code == 200
            await client.post("/logout")
            run_id = await send_question(sock, thread, "Must not invoke")
            await asyncio.sleep(0.3)
            with server.store.connect() as db:
                assert (
                    db.execute("SELECT count(*) FROM invocations WHERE id=?", (run_id,)).fetchone()[
                        0
                    ]
                    == 0
                )
            assert (await client.get("/api/threads")).status_code == 401
        finally:
            await sock.disconnect()


async def test_chat_menu_logout_revokes_parent_session(server):
    async with server.login() as client:
        token = client.cookies.get("phase0_session")
        assert (await client.post("/chat/logout")).status_code == 200
        client.cookies.set("phase0_session", token)
        assert (await client.get("/api/threads")).status_code == 401


async def test_crash_after_dispatch_is_unknown_and_never_replayed(server):
    async with server.login() as client:
        sock, thread = await new_thread(client)
        run_id = await send_question(sock, thread, "Crash during memo")

        async def invocation_count():
            with server.store.connect() as db:
                return db.execute(
                    "SELECT count(*) FROM invocations WHERE id=?", (run_id,)
                ).fetchone()[0]

        await eventually(invocation_count, lambda count: count == 1)
        server.stop()
        await sock.disconnect()
        server.start()
        assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "unknown"
        resumed = await connect(client, thread=thread)
        try:
            await send_question(resumed, thread, "Crash during memo", run_id)
            await asyncio.sleep(0.2)
            assert await invocation_count() == 1
            assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "unknown"
        finally:
            await resumed.disconnect()
