"""Bounded SSE parsing for the negotiated /invoke response; never retry a stream."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from agentcraft2_ui.application.progress import publish_draft
from agentcraft2_ui.contracts.queries import ContractError, ErrorCode


@dataclass(frozen=True)
class TerminalEvent:
    """One correlated terminal envelope, received through the end of the stream."""

    body: bytes
    failed: bool = False


async def events(response: httpx.Response, limit: int) -> AsyncIterator[tuple[str, bytes]]:
    """Bound both individual frames and the whole stream before decoding JSON."""
    buffer = bytearray()
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit * 8:
            raise ContractError(ErrorCode.INVALID_RESPONSE)
        buffer.extend(chunk)
        buffer = buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            frame, _, buffer = buffer.partition(b"\n\n")
            if len(frame) > limit:
                raise ContractError(ErrorCode.INVALID_RESPONSE)
            name = "message"
            data: list[bytes] = []
            for line in frame.split(b"\n"):
                if line.startswith(b"event:"):
                    name = line[6:].strip().decode("utf-8")
                elif line.startswith(b"data:"):
                    data.append(bytes(line[5:].lstrip(b" ")))
            if data:
                yield name, b"\n".join(data)
        if len(buffer) > limit:
            raise ContractError(ErrorCode.INVALID_RESPONSE)
    if buffer.strip():
        raise ContractError(ErrorCode.INVALID_RESPONSE)


async def terminal_body(
    response: httpx.Response, limit: int, *, thread_id: str, request_id: str
) -> TerminalEvent:
    """Distinguish an explicit refusal from an interrupted or ambiguous stream.

    These failure codes match terminal HTTP 4xx outcomes. Internal errors and
    unavailable services remain uncertain, as do missing or duplicate terminals.
    """
    result: TerminalEvent | None = None
    async for name, data in events(response, limit):
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or value.get("thread_id") != thread_id
            or value.get("request_id") != request_id
            or result is not None
        ):
            raise ContractError(ErrorCode.INVALID_RESPONSE)
        if name == "draft":
            _draft(value)
        elif name == "result":
            result = TerminalEvent(data)
        elif (
            name == "error"
            and value.get("error")
            in ("invalid", "denied", "unauthorized", "budget_exceeded", "paused")
            and "output" not in value
        ):
            result = TerminalEvent(data, failed=True)
        else:
            # Do not turn uncertain execution into permission to dispatch again.
            raise ContractError(ErrorCode.INVALID_RESPONSE)
    if result is None:
        raise ContractError(ErrorCode.INVALID_RESPONSE)
    return result


def _draft(value: dict[str, object]) -> None:
    call, text, reset = value.get("call"), value.get("text"), value.get("reset")
    if (
        type(call) is not int
        or call < 1
        or not isinstance(text, str)
        or len(text) > 65_536
        or not isinstance(reset, bool)
    ):
        raise ContractError(ErrorCode.INVALID_RESPONSE)
    publish_draft(call, text, reset=reset)
