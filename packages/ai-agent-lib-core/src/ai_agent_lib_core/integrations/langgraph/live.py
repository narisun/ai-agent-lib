"""Assemble native provider chunks while forwarding only public draft text."""

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, message_chunk_to_message
from langchain_core.runnables import Runnable

from ai_agent_lib_core.observability.live import LiveRun
from ai_agent_lib_core.pipeline.guardrails import OutputPreviewCheck


def _text(content: str | list[str | dict[Any, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        block if isinstance(block, str) else str(block.get("text", ""))
        for block in content
        if isinstance(block, str) or block.get("type") == "text"
    )


async def stream_reply(
    target: Runnable[Any, Any],
    messages: list[BaseMessage],
    options: Mapping[str, Any],
    live: LiveRun,
    call: int,
    *,
    deliver: bool = True,
    check_prefix: OutputPreviewCheck | None = None,
) -> AIMessage:
    """Keep tool chunks and usage intact; draft delivery never accepts the reply."""
    combined: AIMessageChunk | None = None
    prefix = ""
    async for chunk in target.astream(messages, **options):
        if not isinstance(chunk, AIMessage):
            raise TypeError("the model stream must contain AI messages")
        text = _text(chunk.content)
        if deliver and text:
            if check_prefix is not None:
                prefix += text
                await check_prefix(prefix)
            await publish_reply(live, call, text)
        if not isinstance(chunk, AIMessageChunk):
            # Providers without native streaming fall back to one complete reply.
            if combined is not None:
                raise TypeError("the model stream mixed chunks with a complete reply")
            return chunk
        combined = chunk if combined is None else combined + chunk
    if combined is None:
        raise ValueError("the model stream returned no reply")
    reply = message_chunk_to_message(combined)
    if not isinstance(reply, AIMessage):
        raise TypeError("the model stream did not produce an AI reply")
    return reply


async def publish_reply(
    live: LiveRun, call: int, content: str | list[str | dict[str, Any]]
) -> None:
    """Publish public text only, after any required model output checks have passed."""
    text = _text(content)
    for offset in range(0, len(text), 1024):
        await live.sink(call, text[offset : offset + 1024])
