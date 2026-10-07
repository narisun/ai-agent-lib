"""The structured output stage: a reply is validated before anything uses it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from ai_agent_lib_core.contracts import ValidationFailed
from ai_agent_lib_core.pipeline.calls import ModelCall
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["StructuredOutputInterceptor"]

ResponseT = TypeVar("ResponseT")


class StructuredOutputInterceptor(Generic[ResponseT]):
    """Validates a model reply against the schema the call asked for.

    A call that asked for no schema passes through. A reply that does not
    match fails the call; it is never passed on for the caller to sort out.

    Args:
        payload_of: Returns what the model produced for a named schema: an
            already parsed value, or the reply text to parse strictly.
    """

    def __init__(self, payload_of: Callable[[ResponseT, str], object]) -> None:
        self._payload_of = payload_of

    async def __call__(
        self, request: ModelCall, call_next: Handler[ModelCall, ResponseT]
    ) -> ResponseT:
        """Return the reply only if it satisfies the schema."""
        response = await call_next(request)
        output = request.output
        if output is None:
            return response
        request.evidence.add(output_schema=output.name)
        try:
            payload = self._payload_of(response, output.name)
            if isinstance(payload, str):
                output.parse(payload)
            else:
                output.validate(payload)
        except ValidationFailed:
            request.evidence.add(output_valid=False)
            raise
        request.evidence.add(output_valid=True)
        return response
