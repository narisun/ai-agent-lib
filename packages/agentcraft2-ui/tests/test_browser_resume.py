"""Live drafts, lost responses, trace links, comparison sources and long histories on resume."""

from uuid import uuid4

import pytest
from browser_protocol import LocalServer, connect, poll, send, wait_thread, wait_turns

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("failed", [False, True])
async def test_live_step_is_removed_and_final_metrics_survive_resume(
    server: LocalServer, failed: bool
) -> None:
    server.streaming = True
    server.stream_failure = failed
    async with server.login() as client:
        before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
        sock = await connect(server, client)
        try:
            thread = await wait_thread(client, before)
            await send(sock, thread, "Stream a response")
            turns = await wait_turns(client, thread, 1, allow_failed=failed)
            await poll(
                lambda: (
                    any(name == "delete_message" for name, _ in sock.events_seen)
                    and any(
                        name == "new_message" and value.get("type") == "assistant_message"
                        for name, value in sock.events_seen
                    )
                )
            )
            removed = [value for name, value in sock.events_seen if name == "delete_message"]
            progress = [
                value
                for name, value in sock.events_seen
                if name in {"stream_start", "stream_token"}
            ]
            assert any(
                "Draft arrives" in value.get("token", value.get("output", "")) for value in progress
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
            restored = [value for name, value in resumed.events_seen if name == "resume_thread"][-1]
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


async def test_resume_recovers_lost_response_without_another_agent_invocation(
    server: LocalServer,
) -> None:
    async with server.login() as client:
        thread = (await client.post("/api/threads", json={"agent_id": "memo"})).json()["thread"][
            "thread_id"
        ]
        before = len(server.calls)
        server.lose_response = True
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
    async with server.login() as client:
        thread = (await client.post("/api/threads", json={"agent_id": "memo"})).json()["thread"][
            "thread_id"
        ]
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
    async with server.login() as client:
        before = {t["thread_id"] for t in (await client.get("/api/threads")).json()["items"]}
        sock = await connect(server, client)
        try:
            thread = await wait_thread(client, before)
            await send(sock, thread, "Compare client financials")
            turns = await wait_turns(client, thread, 1)
            await poll(
                lambda: any(
                    name == "new_message" and value.get("type") == "assistant_message"
                    for name, value in sock.events_seen
                )
            )
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
