"""Scripted agents and an in-process ChatService for the chat test modules."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from agentcraft2_ui.adapters.catalog import FileCatalog, LocalAccess
from agentcraft2_ui.adapters.http import HttpDispatcher
from agentcraft2_ui.adapters.sqlite import SqliteThreads
from agentcraft2_ui.application.chat import ChatService
from agentcraft2_ui.config.settings import Settings
from agentcraft2_ui.contracts.queries import ContractError, ErrorCode, Permission, ViewContext


class ScriptedAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.mode = "ok"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        self.entered.set()
        await self.release.wait()
        if self.mode == "timeout":
            raise httpx.ReadTimeout("may already have executed")
        if self.mode == "redirect":
            return httpx.Response(307, headers={"location": "http://127.0.0.1:8888/other"})
        if self.mode == "invalid":
            return httpx.Response(200, json={"output": {"draft": "not accepted"}})
        if self.mode == "large":
            return httpx.Response(200, content=b"x" * 1_048_577)
        if self.mode == "failed":
            return httpx.Response(403, json={"detail": "denied"})
        if self.mode == "thread_busy":
            return httpx.Response(
                409, json={"error": "thread_busy", "request_id": request.headers["x-request-id"]}
            )
        if self.mode == "gateway":
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={
                "thread_id": body["thread_id"],
                "request_id": request.headers["x-request-id"],
                "output": {"answer": f"answer {len(self.calls)}"},
                **(
                    {"output": {"answer": "Saved answer", "artifacts": [{}]}}
                    if self.mode == "bad_artifact"
                    else {}
                ),
            },
        )


class AllowUsers:
    async def require(self, view: ViewContext, action: Permission) -> None:
        if view.principal.tenant not in {"one", "two"}:
            raise ContractError(ErrorCode.NOT_FOUND)


class EventStream(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request, mode: str, release: asyncio.Event) -> None:
        self.request, self.mode, self.release = request, mode, release

    async def __aiter__(self) -> AsyncIterator[bytes]:
        identity = {
            "thread_id": json.loads(self.request.content)["thread_id"],
            "request_id": self.request.headers["x-request-id"],
        }

        def frame(name: str, **value: object) -> bytes:
            return f"event: {name}\ndata: {json.dumps({**identity, **value})}\n\n".encode()

        yield frame("draft", call=1, text="", reset=True)
        yield frame("draft", call=1, text="Provisional £ text", reset=False)
        await self.release.wait()
        if self.mode == "missing":
            return
        if self.mode in {"cross_thread", "cross_thread_error"}:
            identity["thread_id"] = "someone-else"
        if self.mode == "cross_request_error":
            identity["request_id"] = "someone-else"
        if "error" in self.mode:
            terminal = frame(
                "error",
                error="internal" if self.mode == "internal_error" else "invalid",
                model_calls=3,
                usage={
                    "input_tokens": 90,
                    "output_tokens": 20,
                    "source": "langchain",
                    "kind": "reported",
                },
                **({"output": {"answer": "Do not accept"}} if self.mode == "output_error" else {}),
            )
            yield terminal
            if self.mode == "duplicate_error":
                yield terminal
            if self.mode == "interrupted_error":
                raise httpx.ReadError("connection lost after terminal")
            return
        result = frame(
            "result",
            output={"answer": "Accepted answer"},
            model_calls=2,
            usage={
                "source": "langchain",
                "kind": "reported",
                "input_tokens": 40,
                "output_tokens": 12,
                "cache_read_tokens": 10,
            },
        )
        # Split arbitrary bytes, including UTF-8 and SSE delimiters.
        for index in range(0, len(result), 7):
            yield result[index : index + 7]
        if self.mode == "duplicate":
            yield result


@asynccontextmanager
async def chat_at(
    settings: Settings, tmp_path: Path, *, commit_hook: Callable[[], None] | None = None
) -> AsyncIterator[tuple[ChatService, ScriptedAgent, SqliteThreads]]:
    """Serve a ChatService over a scripted agent and a fresh SQLite store in tmp_path."""
    access = LocalAccess(settings)
    store = SqliteThreads(
        tmp_path / "ui.sqlite", access, b"test-cursor-secret", commit_hook=commit_hook
    )
    agent = ScriptedAgent()
    async with httpx.AsyncClient(transport=httpx.MockTransport(agent), trust_env=False) as client:
        service = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 2))
        try:
            yield service, agent, store
        finally:
            agent.release.set()
            await service.close()
