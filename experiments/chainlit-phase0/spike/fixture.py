"""Deterministic HTTP agent: synthetic answers and explicitly simulated diagnostics."""

import asyncio
import json
import secrets

from fastapi import APIRouter, HTTPException, Request

from spike.settings import Settings, catalog
from spike.store import Store

MEMO = """# Client meeting preparation

**Synthetic client DEMO-001 · 3 August 2026 · Phase 0 fixture**

## Executive summary

The example client has revenue of $12 million and gross profit of $3 million.
Gross margin is 25%. These fixed figures validate the interface, not a real client.

## Topics for the meeting

1. Confirm the next quarter's working-capital needs.
2. Discuss collection timing with the finance team.
3. Review the planned equipment purchase.

## Evidence table

| Source reference | Metric | Value | Reporting period | Comment |
| --- | --- | --- | --- | --- |
| synthetic-financials-001 | Revenue | $12,000,000 | FY2025 | Scripted evidence for UI checks |
| synthetic-financials-002 | Gross profit | $3,000,000 | FY2025 | Gross margin: 25% |

## Questions to ask

Which customers explain the change in receivables? What financing dates matter?
The follow-up agent can reuse this saved conversation on the same thread.
"""


def fixture_routes(store: Store, settings: Settings) -> APIRouter:
    """Expose a real loopback HTTP boundary without any model or MCP dependency."""
    router = APIRouter()

    @router.post("/fixture/{agent_id}/invoke")
    async def invoke(agent_id: str, request: Request) -> dict:
        credential = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(credential, settings.agent_key):
            raise HTTPException(401, "Fixture service credential required")
        if agent_id not in {entry["id"] for entry in catalog()}:
            raise HTTPException(404, "Unknown fixture agent")
        payload = await request.json()
        owner, thread_id = payload["fixture_owner"], payload["thread_id"]
        question, run_id = payload["input"]["question"], request.headers["x-request-id"]
        store.thread(owner, thread_id)
        with store.connect() as db:
            previous = db.execute(
                "SELECT question,result FROM invocations "
                "WHERE owner=? AND thread_id=? ORDER BY rowid",
                (owner, thread_id),
            ).fetchall()
            # No endpoint-level retry: repeated calls are deliberately visible to checks.
            db.execute(
                "INSERT INTO invocations VALUES(?,?,?,?,?)",
                (run_id, thread_id, owner, question, "{}"),
            )
        await asyncio.sleep(settings.delay)
        messages = [{"role": "system", "content": "Synthetic memo assistant."}]
        for row in previous:
            old = json.loads(row["result"])
            messages.extend(
                [
                    {"role": "user", "content": row["question"]},
                    {"role": "assistant", "content": old.get("answer", "")},
                ]
            )
        messages.append({"role": "user", "content": question})
        answer = (
            "Gross margin is 25%: $3 million gross profit / $12 million revenue. "
            f"This is follow-up {len(previous)} on the same thread. [synthetic-financials-002]"
            if previous
            else "Your preparation memo is ready. Open **Preparation memo** beside the chat."
        )
        tool_id = f"tool-{run_id}"
        tool_result = {
            "revenue": 12000000,
            "gross_profit": 3000000,
            "source": "synthetic-financials-002",
        }
        tool_message = {"role": "tool", "tool_call_id": tool_id, "content": json.dumps(tool_result)}
        result = {
            "answer": answer,
            "memo": MEMO,
            "simulation": True,
            "metrics": {"input_tokens": 10000, "output_tokens": 2000, "cache_read_tokens": 6000},
            "models": [
                {
                    "call_id": f"model-1-{run_id}",
                    "attempt": 1,
                    "input": messages,
                    "output": {
                        "tool_calls": [
                            {"id": tool_id, "name": "financials", "args": {"client": "DEMO-001"}}
                        ]
                    },
                },
                {
                    "call_id": f"model-2-{run_id}",
                    "attempt": 1,
                    "input": [
                        *messages,
                        {
                            "role": "assistant",
                            "tool_calls": [{"id": tool_id, "name": "financials"}],
                        },
                        tool_message,
                    ],
                    "output": {"content": answer},
                },
            ],
            "tools": [
                {
                    "id": tool_id,
                    "server": "synthetic-data",
                    "name": "financials",
                    "result": tool_result,
                }
            ],
            "checkpoints": [
                {"id": f"before-{run_id}", "step": "restore", "saved_messages": messages[1:-1]},
                {
                    "id": f"after-{run_id}",
                    "step": "answer",
                    "saved_messages": [*messages[1:], {"role": "assistant", "content": answer}],
                },
            ],
            "logs": [
                {
                    "event": "fixture.invoke",
                    "request_id": run_id,
                    "thread_id": thread_id,
                    "previous_turns": len(previous),
                }
            ],
        }
        with store.connect() as db:
            db.execute("UPDATE invocations SET result=? WHERE id=?", (json.dumps(result), run_id))
        return {"output": result, "thread_id": thread_id, "request_id": run_id}

    return router
