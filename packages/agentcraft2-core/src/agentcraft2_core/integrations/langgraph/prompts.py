"""Portable prompt structure, with an explicit boundary after stable instructions."""

from typing import Any

from langchain_core.messages import SystemMessage


def static_system_prompt(instructions: str, request_context: str = "") -> SystemMessage:
    """Keep static instructions before changing context in separate text blocks.

    The metadata identifies a reusable prefix. A provider may translate it into
    its cache controls when caching is enabled. It does not cache model answers
    or change the instructions, authorization, or conversation selection.
    """
    blocks: list[str | dict[str, Any]] = [{"type": "text", "text": instructions}]
    if request_context:
        blocks.append({"type": "text", "text": request_context})
    return SystemMessage(content=blocks, additional_kwargs={"agentcraft2_static_prefix": True})
