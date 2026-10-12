"""Bounded question-answer HTTP calls with no automatic retries or redirects."""

import asyncio
import time
from datetime import UTC, datetime

import httpx

from ai_agent_lib_ui.application.question_answer import QuestionAnswerAdapter
from ai_agent_lib_ui.contracts.catalog import AgentBinding
from ai_agent_lib_ui.contracts.conversations import RunRecord, TurnAdmission
from ai_agent_lib_ui.contracts.queries import ContractError, ErrorCode, ViewContext


class HttpDispatcher:
    """Use a dedicated client; no proxy, browser headers or browser URLs are accepted."""

    def __init__(self, client: httpx.AsyncClient, timeout_seconds: float) -> None:
        self.client, self.timeout = client, timeout_seconds

    async def dispatch(
        self, view: ViewContext, binding: AgentBinding, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        """Send only the new question and capture the agent's correlated request ID."""
        # A wall-clock deadline also bounds a server that sends a slow trickle of
        # bytes; HTTPX's read timeout alone resets for each received chunk.
        async with asyncio.timeout(self.timeout):
            return await self._send(view, binding, turn, run)

    async def _send(
        self, view: ViewContext, binding: AgentBinding, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        adapter = QuestionAnswerAdapter(binding.manifest)
        started = time.monotonic()
        async with self.client.stream(
            "POST",
            binding.base_url + "/invoke",
            json=adapter.request(view, turn),
            headers={"x-request-id": run.request_id},
            timeout=self.timeout,
            follow_redirects=False,
        ) as response:
            # Redirects, timeouts, malformed envelopes and oversized responses
            # leave the outcome unknown. None are safe evidence for replay.
            if (
                300 <= response.status_code < 400
                or response.status_code == 408
                or response.status_code >= 500
            ):
                raise ContractError(ErrorCode.INVALID_RESPONSE)
            if response.status_code >= 400:
                return run.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": datetime.now(UTC),
                        "elapsed_ms": (time.monotonic() - started) * 1000,
                    }
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > binding.manifest.max_response_bytes:
                    raise ContractError(ErrorCode.INVALID_RESPONSE)
            answer = adapter.response(view, turn, bytes(body), status_code=response.status_code)
        return run.model_copy(
            update={
                "state": "completed",
                "answer": answer.answer,
                "request_id": answer.request_id,
                "updated_at": datetime.now(UTC),
                "elapsed_ms": (time.monotonic() - started) * 1000,
            }
        )
