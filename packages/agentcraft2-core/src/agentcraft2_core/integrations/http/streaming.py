"""Keep generator cleanup in the same task and context as stream iteration."""

from anyio import CancelScope
from starlette.responses import StreamingResponse
from starlette.types import Send


class ClosingStreamingResponse(StreamingResponse):
    """Close a suspended iterator even when sending its current item fails.

    Earlier ASGI versions run iteration in a child task. Closing here preserves
    context-variable tokens and task-affine resources owned by the generator.
    """

    async def stream_response(self, send: Send) -> None:
        """Send the stream and finish generator cleanup before returning ownership."""
        try:
            await super().stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with CancelScope(shield=True):
                    await close()
