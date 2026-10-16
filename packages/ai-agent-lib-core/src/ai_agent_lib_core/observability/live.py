"""Request-scoped draft delivery and usage totals, independent of HTTP and models."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

from ai_agent_lib_core.contracts import RequestContext
from ai_agent_lib_core.contracts.diagnostics import TokenUsage


class DraftSink(Protocol):
    """Deliver provisional text after configured model checks; application checks may remain."""

    async def __call__(self, call: int, text: str, *, reset: bool = False) -> None:
        """Start a provider attempt or append its public text (never reasoning/tools)."""
        ...


@dataclass
class LiveRun:
    """Collect every provider attempt, including revisions and rejected replies."""

    context: RequestContext
    sink: DraftSink
    model_calls: int = 0
    usages: list[TokenUsage] = field(default_factory=list)

    async def begin(self) -> int:
        """Assign a request-local attempt number before asking the provider."""
        self.model_calls += 1
        call = self.model_calls
        await self.sink(call, "", reset=True)
        return call

    def usage(self) -> TokenUsage:
        """Sum complete reported fields; a missing attempt makes that total unknown."""
        if not self.model_calls or len(self.usages) != self.model_calls:
            return TokenUsage()
        counts: dict[str, int | None] = {}
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            values = [getattr(usage, name) for usage in self.usages]
            counts[name] = sum(values) if all(value is not None for value in values) else None
        kinds = {usage.kind for usage in self.usages}
        return TokenUsage(
            source="langchain",
            kind=self.usages[0].kind if len(kinds) == 1 else "unavailable",
            **counts,
        )


_LIVE: ContextVar[LiveRun | None] = ContextVar("live_model_run", default=None)


def live_run(context: RequestContext | None) -> LiveRun | None:
    """Never deliver a model call from a different authenticated request."""
    active = _LIVE.get()
    return active if active is not None and active.context == context else None


@contextmanager
def bind_live_run(run: LiveRun) -> Iterator[None]:
    """Let graph tasks inherit this invocation's draft sink and usage accumulator."""
    token = _LIVE.set(run)
    try:
        yield
    finally:
        _LIVE.reset(token)
