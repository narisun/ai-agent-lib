"""Real chat sessions over HTTP and Socket.IO: questions, reconnects, restarts, conversations."""

from uuid import uuid4

import httpx
import pytest
from browser_protocol import LocalServer, connect, poll, send, wait_thread, wait_turns

pytestmark = pytest.mark.slow


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
            await poll(lambda: len(server.calls) > calls)
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
            if sock.connected:
                await sock.disconnect()


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
