"""Bounded question-answer HTTP calls with no automatic retries or redirects."""

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from ai_agent_lib_ui.adapters.events import terminal_body
from ai_agent_lib_ui.application.question_answer import QuestionAnswerAdapter
from ai_agent_lib_ui.contracts.catalog import AgentBinding
from ai_agent_lib_ui.contracts.chat import StoredTurn
from ai_agent_lib_ui.contracts.conversations import RunRecord, TurnAdmission
from ai_agent_lib_ui.contracts.queries import ContractError, ErrorCode, ViewContext
from ai_agent_lib_ui.contracts.traces import TraceLink

# Operational records only: a status, a path and identifiers. Never a body.
logger = logging.getLogger(__name__)

_TERMINAL_FAILURES = frozenset({"invalid", "denied", "unauthorized", "budget_exceeded", "paused"})


class HttpDispatcher:
    """Use a dedicated client; no proxy, browser headers or browser URLs are accepted."""

    def __init__(self, client: httpx.AsyncClient, timeout_seconds: float) -> None:
        self.client, self.timeout = client, timeout_seconds

    async def outcome(
        self, view: ViewContext, binding: AgentBinding, stored: StoredTurn
    ) -> RunRecord | None:
        """Fetch only the terminal journal entry; never POST or execute agent work."""
        run = stored.run
        if run.scope != view.scope or run.state != "unknown":
            raise ContractError(ErrorCode.NOT_FOUND)
        async with asyncio.timeout(min(self.timeout, 10)):
            async with self.client.stream(
                "GET",
                binding.base_url + "/requests/" + quote(run.request_id, safe=""),
                params={"thread_id": run.thread_id},
                headers={"x-request-id": run.request_id},
                timeout=min(self.timeout, 10),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > binding.manifest.max_response_bytes:
                        raise ContractError(ErrorCode.INVALID_RESPONSE)
        envelope = json.loads(body)
        if envelope == {"state": "unknown"}:
            return None
        if (
            not isinstance(envelope, dict)
            or envelope.get("thread_id") != run.thread_id
            or envelope.get("request_id") != run.request_id
        ):
            raise ContractError(ErrorCode.INVALID_RESPONSE)
        if "output" in envelope and "error" not in envelope:
            turn = TurnAdmission(
                scope=view.scope,
                thread_id=run.thread_id,
                idempotency_key=run.idempotency_key,
                question=stored.question,
            )
            answer = QuestionAnswerAdapter(binding.manifest).response(
                view,
                turn,
                bytes(body),
                request_id=run.request_id,
            )
            result = run.model_copy(
                update={
                    "state": "completed",
                    "answer": answer.answer,
                    "artifacts": answer.artifacts,
                    "artifact_status": answer.artifact_status,
                    "artifact_warning": answer.artifact_warning,
                    "usage": answer.usage,
                    "model_calls": answer.model_calls,
                    "trace": answer.trace,
                    "updated_at": datetime.now(UTC),
                }
            )
        elif envelope.get("error") in _TERMINAL_FAILURES and "output" not in envelope:
            result = _failed_run(run, time.monotonic(), bytes(body))
        else:
            return None
        # Invalid optional timing cannot invalidate an authoritative outcome.
        elapsed = envelope.get("elapsed_ms")
        if type(elapsed) in (float, int):
            try:
                return RunRecord.model_validate({**result.model_dump(), "elapsed_ms": elapsed})
            except ValidationError:
                pass
        return result.model_copy(update={"elapsed_ms": run.elapsed_ms})

    async def dispatch(
        self, view: ViewContext, binding: AgentBinding, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        """Send only the new question and require its original request/thread correlation."""
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
            headers={
                "x-request-id": run.request_id,
                "accept": "text/event-stream, application/json",
            },
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
                _log_degraded("outcome unknown", response.status_code, binding, run)
                raise ContractError(ErrorCode.INVALID_RESPONSE)
            if response.status_code >= 400:
                body = await _bounded_body(response, binding.manifest.max_response_bytes)
                try:
                    envelope = json.loads(body)
                except ValueError:
                    envelope = None
                if (
                    isinstance(envelope, dict)
                    and envelope.get("thread_id") == run.thread_id
                    and envelope.get("request_id") == run.request_id
                    and envelope.get("error") in _TERMINAL_FAILURES
                    and "output" not in envelope
                ):
                    return _failed_run(run, started, body)
                # A refusal the agent did not describe in its envelope: the user sees
                # "failed"; the log keeps the status so an operator can find the cause.
                _log_degraded("failed without an envelope", response.status_code, binding, run)
                return run.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": datetime.now(UTC),
                        "elapsed_ms": (time.monotonic() - started) * 1000,
                    }
                )
            limit = binding.manifest.max_response_bytes
            if response.headers.get("content-type", "").split(";", 1)[0] == "text/event-stream":
                terminal = await terminal_body(
                    response, limit, thread_id=turn.thread_id, request_id=run.request_id
                )
                if terminal.failed:
                    return _failed_run(run, started, terminal.body)
                body = terminal.body
            else:
                body = await _bounded_body(response, limit)
            answer = adapter.response(
                view, turn, body, status_code=response.status_code, request_id=run.request_id
            )
        return run.model_copy(
            update={
                "state": "completed",
                "answer": answer.answer,
                "artifacts": answer.artifacts,
                "artifact_status": answer.artifact_status,
                "artifact_warning": answer.artifact_warning,
                "request_id": answer.request_id,
                "updated_at": datetime.now(UTC),
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "usage": answer.usage,
                "model_calls": answer.model_calls,
                "trace": answer.trace,
            }
        )


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise ContractError(ErrorCode.INVALID_RESPONSE)
    return bytes(body)


def _log_degraded(what: str, status: int, binding: AgentBinding, run: RunRecord) -> None:
    logger.warning(
        "agent dispatch %s",
        what,
        extra={
            "status": status,
            "path": "/invoke",
            "agent_id": binding.manifest.agent_id,
            "thread_id": run.thread_id,
            "request_id": run.request_id,
        },
    )


def _failed_run(run: RunRecord, started: float, body: bytes) -> RunRecord:
    """Keep optional diagnostics on a known failure without accepting its draft."""
    envelope = json.loads(body)
    failed = run.model_copy(
        update={
            "state": "failed",
            "failure_code": envelope["error"],
            "updated_at": datetime.now(UTC),
            "elapsed_ms": (time.monotonic() - started) * 1000,
        }
    )
    with suppress(ValidationError):
        failed = RunRecord.model_validate(
            {
                **failed.model_dump(),
                "usage": envelope.get("usage", {}),
                "model_calls": envelope.get("model_calls"),
            }
        )
    try:
        trace = TraceLink.model_validate(envelope.get("trace"))
    except ValidationError:
        return failed
    return failed.model_copy(update={"trace": trace})
