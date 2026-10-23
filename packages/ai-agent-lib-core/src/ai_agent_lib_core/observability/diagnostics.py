"""Optional metadata capture with injectable storage and explicit operation scope."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from ai_agent_lib_core.contracts import Clock, IdGenerator, RequestContext
from ai_agent_lib_core.contracts.diagnostics import (
    AttemptRef,
    DiagnosticEvent,
    DiagnosticLinks,
    DiagnosticSink,
    ModelAttempt,
    OperationDecision,
    OperationSummary,
    TokenUsage,
)

_LOG = logging.getLogger(__name__)
_LINKS: ContextVar[DiagnosticLinks | None] = ContextVar("diagnostic_links", default=None)


@contextmanager
def bind_diagnostic_links(links: DiagnosticLinks) -> Iterator[None]:
    """Attach known node/checkpoint links in trusted agent composition code."""
    token = _LINKS.set(links)
    try:
        yield
    finally:
        _LINKS.reset(token)


@dataclass
class _Operation:
    owner: Diagnostics
    context: RequestContext
    attempts: list[AttemptRef] = field(default_factory=list)
    pending: set[AttemptRef] = field(default_factory=set)
    complete: bool = True
    closed: bool = False


_ACTIVE: ContextVar[_Operation | None] = ContextVar("diagnostic_operation", default=None)


class Diagnostics:
    """Write content-free usage and decisions independently of raw model tracing.

    The caller owns the sink. Inject the same recorder into ServiceContainer and
    agent_app to obtain a terminal summary. Await all work inside operation().
    Diagnostic failures preserve chat and are reported without payloads. This is
    not a durable request journal or an authorization layer.
    """

    def __init__(
        self,
        sink: DiagnosticSink,
        clock: Clock,
        ids: IdGenerator,
        *,
        write_timeout_seconds: float = 1.0,
    ) -> None:
        if not math.isfinite(write_timeout_seconds) or write_timeout_seconds <= 0:
            raise ValueError("diagnostic write timeout must be finite and positive")
        self._sink = sink
        self._write_timeout_seconds = write_timeout_seconds
        self.clock = clock
        self.ids = ids

    def _active(self, context: RequestContext) -> _Operation | None:
        active = _ACTIVE.get()
        if active is not None and active.owner is self and active.context == context:
            return active
        return None

    async def _write(self, event: DiagnosticEvent, context: RequestContext) -> None:
        try:
            async with asyncio.timeout(self._write_timeout_seconds):
                await self._sink.write(event)
        except Exception as error:  # noqa: BLE001 - diagnostics must not replace the business result
            active = self._active(context)
            if active is not None:
                active.complete = False
            _LOG.warning(
                "diagnostics.write_failed",
                extra={"error_type": type(error).__name__, "request_id": context.request_id},
            )

    def begin_attempt(self, context: RequestContext, ref: AttemptRef) -> None:
        """Register an expected attempt before dispatch, even if its record is lost."""
        active = self._active(context)
        if active is not None:
            if active.closed:
                raise RuntimeError("model work must finish before the diagnostic operation closes")
            active.attempts.append(ref)
            active.pending.add(ref)

    async def record_attempt(
        self,
        context: RequestContext,
        *,
        ref: AttemptRef,
        model_alias: str,
        provider: str,
        model_id: str,
        outcome: Literal["returned", "failed", "cancelled"],
        started: float,
        usage: TokenUsage,
        links: DiagnosticLinks | None = None,
        tool_call_ids: tuple[str, ...] = (),
    ) -> None:
        """Capture one SDK attempt before output validation can reject its reply."""
        active = self._active(context)
        if active is not None:
            active.pending.discard(ref)
        try:
            explicit = _LINKS.get() or DiagnosticLinks()
            framework = links or DiagnosticLinks()
            links = DiagnosticLinks(
                node=explicit.node or framework.node,
                checkpoint_id=explicit.checkpoint_id or framework.checkpoint_id,
                tool_call_ids=tuple(
                    dict.fromkeys((*explicit.tool_call_ids, *framework.tool_call_ids))
                ),
            )
            event = ModelAttempt(
                scope=context.scope,
                thread_id=context.thread_id,
                request_id=context.request_id,
                timestamp=self.clock.now(),
                ref=ref,
                model_alias=model_alias,
                provider=provider,
                model_id=model_id,
                outcome=outcome,
                duration_ms=max(0, (self.clock.monotonic() - started) * 1000),
                usage=usage,
                links=DiagnosticLinks(
                    node=links.node,
                    checkpoint_id=links.checkpoint_id,
                    tool_call_ids=tuple(dict.fromkeys((*links.tool_call_ids, *tool_call_ids))),
                ),
            )
            await self._write(event, context)
        except Exception as error:  # noqa: BLE001 - invalid provider metadata cannot break chat
            if active is not None:
                active.complete = False
            _LOG.warning(
                "diagnostics.invalid_metadata",
                extra={"error_type": type(error).__name__, "request_id": context.request_id},
            )

    async def decide(
        self,
        context: RequestContext,
        ref: AttemptRef,
        *,
        stage: Literal["model_pipeline", "application"],
        outcome: Literal["passed", "rejected"],
        reason_code: str,
    ) -> None:
        """Record an explicit decision; application rejection does not erase usage."""
        try:
            event = OperationDecision(
                scope=context.scope,
                thread_id=context.thread_id,
                request_id=context.request_id,
                timestamp=self.clock.now(),
                ref=ref,
                stage=stage,
                outcome=outcome,
                reason_code=reason_code,
            )
            await self._write(event, context)
        except Exception as error:  # noqa: BLE001 - invalid optional metadata cannot replace chat
            active = self._active(context)
            if active is not None:
                active.complete = False
            _LOG.warning(
                "diagnostics.invalid_metadata",
                extra={"error_type": type(error).__name__, "request_id": context.request_id},
            )

    @asynccontextmanager
    async def operation(self, context: RequestContext) -> AsyncIterator[None]:
        """Summarize an invocation; cancellation leaves the outcome unknown.

        Normal exit means the application produced its accepted output. Use this
        at the whole invocation boundary, never around a single model call. A lost
        process may emit no summary, which consumers must treat as unknown.
        """
        if _ACTIVE.get() is not None:
            raise RuntimeError("diagnostic operations cannot be nested")
        active = _Operation(self, context)
        token = _ACTIVE.set(active)
        outcome: Literal["completed", "failed", "unknown"] = "completed"
        try:
            yield
        except asyncio.CancelledError:
            outcome = "unknown"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            active.closed = True
            if active.pending:
                active.complete = False
                outcome = "unknown"
            try:
                await self._write(
                    OperationSummary(
                        scope=context.scope,
                        thread_id=context.thread_id,
                        request_id=context.request_id,
                        timestamp=self.clock.now(),
                        outcome=outcome,
                        expected_attempts=tuple(active.attempts),
                        capture_complete=active.complete,
                        accepted_output="invoke.output" if outcome == "completed" else None,
                    ),
                    context,
                )
            except Exception as error:  # noqa: BLE001 - invalid optional metadata cannot replace chat
                _LOG.warning(
                    "diagnostics.invalid_metadata",
                    extra={"error_type": type(error).__name__, "request_id": context.request_id},
                )
            finally:
                _ACTIVE.reset(token)
