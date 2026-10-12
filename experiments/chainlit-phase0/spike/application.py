"""Own HTTP tasks and durable results independently of the browser connection."""

import asyncio
import time
import uuid

import httpx
from fastapi import HTTPException

from spike.settings import Settings, catalog
from spike.store import Store, now


class Runs:
    """Keep admitted tasks alive, and persist each accepted answer only once."""

    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.tasks: dict[str, asyncio.Task] = {}

    async def submit(self, owner: str, thread_id: str, run_id: str, question: str) -> dict:
        """Run or reattach to a turn; HTTP identity and endpoints are server-owned."""
        if not question.strip() or len(question) > 8000:
            raise HTTPException(400, "Use a question between 1 and 8000 characters")
        if self.store.admit(owner, thread_id, run_id, question):
            task = asyncio.create_task(self._execute(owner, thread_id, run_id, question))
            self.tasks[run_id] = task
            task.add_done_callback(lambda _: self.tasks.pop(run_id, None))
        if task := self.tasks.get(run_id):
            await asyncio.shield(task)
        return self.store.run(owner, run_id)

    async def wait(self, owner: str, thread_id: str) -> None:
        """Reattach a resumed thread to work already running, without resubmission."""
        for run in self.store.runs(owner, thread_id):
            if task := self.tasks.get(run["id"]):
                await asyncio.shield(task)

    async def _execute(self, owner: str, thread_id: str, run_id: str, question: str) -> None:
        started = time.perf_counter()
        try:
            thread = self.store.thread(owner, thread_id)
            agent = thread["metadata"]["chat_profile"]
            entry = next(entry for entry in catalog() if entry["id"] == agent)
            async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
                response = await client.post(
                    entry["url"] + "/invoke",
                    headers={
                        "authorization": f"Bearer {self.settings.agent_key}",
                        "x-request-id": run_id,
                    },
                    json={
                        "input": {"question": question},
                        "thread_id": thread_id,
                        "fixture_owner": owner,
                    },
                )
                response.raise_for_status()
            result = response.json()["output"]
            result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            message_id = str(uuid.uuid4())
            result["message_id"] = message_id
            result["memo_id"] = str(uuid.uuid4())
            result["metrics_id"] = str(uuid.uuid4())
            # Chainlit groups side elements by name when restoring history.
            result["memo_name"] = f"Preparation memo · {run_id[:8]}"
            result["answer"] = result["answer"].replace(
                "**Preparation memo**", f"**{result['memo_name']}**"
            )
            if result["memo_name"] not in result["answer"]:
                result["answer"] += f"\n\nOpen **{result['memo_name']}**."
            self.store.put_object(
                owner,
                "step",
                {
                    "id": message_id,
                    "threadId": thread_id,
                    "name": agent,
                    "type": "assistant_message",
                    "output": result["answer"],
                    "createdAt": now(),
                    "metadata": {"run_id": run_id},
                    "streaming": False,
                    "isError": False,
                },
            )
            common = {"threadId": thread_id, "forId": message_id}
            self.store.put_object(
                owner,
                "element",
                {
                    **common,
                    "id": result["memo_id"],
                    "type": "text",
                    "name": result["memo_name"],
                    "display": "side",
                    "url": f"/artifacts/{run_id}",
                    "mime": "text/plain",
                },
            )
            self.store.put_object(
                owner,
                "element",
                {
                    **common,
                    "id": result["metrics_id"],
                    "type": "custom",
                    "name": "Metrics",
                    "display": "inline",
                    "props": {
                        **result["metrics"],
                        "elapsed_seconds": result["elapsed_seconds"],
                        "run_id": run_id,
                    },
                },
            )
            self.store.complete(owner, run_id, result)
        except Exception as error:  # noqa: BLE001 - preserve unknown outcomes without replay
            self.store.complete(owner, run_id, {"error_type": type(error).__name__}, "unknown")
