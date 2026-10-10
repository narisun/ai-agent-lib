"""The budget stage: what one request may use, so a looping graph stops.

The ledger counts per request, in this process, under the request's
``budget_key``: the caller's tenant and subject, the application, and the
invocation. An agent's entry point starts a new invocation for each request,
so a correlation ID a caller sends never carries spending from one request to
another, or from one caller to another. An MCP server keeps its own count for
the requests it serves, per caller and the request ID the agent sends.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar
from weakref import ReferenceType, ref

from ai_agent_lib_core.contracts import AuditValue, BudgetExceeded, BudgetLimits, RequestContext
from ai_agent_lib_core.pipeline.calls import Evidence
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["BudgetInterceptor", "BudgetLedger"]

_MAX_REQUESTS = 10_000


@dataclass
class _Spent:
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0


class BudgetLedger:
    """Request accounting retained for as long as a request context is alive.

    Only inactive entries may be evicted. Active contexts pin their accounting,
    including between calls, so traffic cannot reset a running graph's budget.

    Args:
        max_requests: Target cache size; live requests may exceed this limit.
    """

    def __init__(self, max_requests: int = _MAX_REQUESTS) -> None:
        self._spent: OrderedDict[str, _Spent] = OrderedDict()
        self._contexts: dict[int, ReferenceType[RequestContext]] = {}
        self._active: dict[str, set[int]] = {}
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        self._max = max_requests

    def for_request(self, context: RequestContext) -> _Spent:
        """Return accounting pinned until this context is released by its owner."""
        identity = id(context)
        key = context.budget_key
        if identity not in self._contexts:
            owner = ref(self)

            def release(_reference: ReferenceType[RequestContext]) -> None:
                ledger = owner()
                if ledger is not None:
                    ledger._contexts.pop(identity, None)
                    ledger._active[key].discard(identity)
                    if not ledger._active[key]:
                        del ledger._active[key]
                    ledger._trim()

            self._contexts[identity] = ref(context, release)
            self._active.setdefault(key, set()).add(identity)
        return self.spent(key)

    def _trim(self) -> None:
        if len(self._spent) <= self._max:
            return
        for key in tuple(self._spent):
            if len(self._spent) <= self._max:
                break
            if key not in self._active:
                del self._spent[key]

    def spent(self, key: str) -> _Spent:
        """Return mutable accounting for ``key`` and mark it recently used.

        This lookup alone does not pin the entry against eviction. Pipeline
        stages use ``for_request`` to retain spending while a context is alive.
        """
        spent = self._spent.get(key)
        if spent is None:
            spent = self._spent[key] = _Spent()
            self._trim()
        else:
            self._spent.move_to_end(key)
        return spent


class _Call(Protocol):
    @property
    def context(self) -> RequestContext | None: ...

    @property
    def evidence(self) -> Evidence: ...


CallT = TypeVar("CallT", bound=_Call)
ResponseT = TypeVar("ResponseT")


class BudgetInterceptor(Generic[CallT, ResponseT]):
    """Reject calls once a request has reached its call or reported-token limit.

    Admitted calls count before execution, including calls that later fail.
    Token usage is added after a response or recorded provider usage becomes
    available. A model call can therefore cross the token limit; subsequent
    model calls are refused. Missing usage counts as zero, not an estimate.
    This is per-process accounting, not a distributed quota or token reservation.

    Args:
        limits: The budget per request.
        ledger: What each request has used.
        kind: ``"model"`` or ``"tool"``: which count the call adds to.
        variable: The setting a developer changes to raise the budget, for the error's fix.
        usage: For model calls, returns the response's token counts.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        ledger: BudgetLedger,
        *,
        kind: str,
        variable: str = "the limits",
        usage: Callable[[ResponseT], Mapping[str, AuditValue]] | None = None,
    ) -> None:
        self._limits = limits
        self._ledger = ledger
        self._kind = kind
        self._variable = variable
        self._usage = usage

    async def __call__(self, request: CallT, call_next: Handler[CallT, ResponseT]) -> ResponseT:
        """Count the call, refuse it when the request is over budget, and add its tokens."""
        context = request.context
        if context is None:
            return await call_next(request)
        spent = self._ledger.for_request(context)
        self._check(spent)
        if self._kind == "model":
            spent.model_calls += 1
        else:
            spent.tool_calls += 1
        response: ResponseT | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            # Charged whether the call succeeded or a later stage rejected its reply.
            spent.tokens += self._tokens(request, response)
            request.evidence.add(
                budget_model_calls=spent.model_calls,
                budget_tool_calls=spent.tool_calls,
                budget_tokens=spent.tokens,
            )

    def _tokens(self, request: CallT, response: ResponseT | None) -> int:
        """The tokens the call spent: as the provider reported them, or from the reply."""
        facts = request.evidence.facts()
        recorded = [facts.get("input_tokens"), facts.get("output_tokens")]
        if any(value is not None for value in recorded):
            return sum(v for v in recorded if isinstance(v, int) and not isinstance(v, bool))
        if self._usage is None or response is None:
            return 0
        usage = self._usage(response)
        return sum(
            value
            for key in ("input_tokens", "output_tokens")
            if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
        )

    def _check(self, spent: _Spent) -> None:
        limits = self._limits
        checks = (
            ("model_calls", spent.model_calls, limits.model_calls, self._kind == "model"),
            ("tool_calls", spent.tool_calls, limits.tool_calls, self._kind == "tool"),
            ("tokens", spent.tokens, limits.tokens, self._kind == "model"),
        )
        over = next(
            (
                (name, used, limit)
                for name, used, limit, applies in checks
                if applies and limit is not None and used >= limit
            ),
            None,
        )
        if over is not None:
            name, used, limit = over
            raise BudgetExceeded(
                f"the request has used its budget of {name.replace('_', ' ')}",
                expected=f"at most {limit} {name.replace('_', ' ')} per request",
                actual=f"{used} used before this {self._kind} call",
                fix=(
                    f"if the graph is looping, stop it with a condition on its state; if the "
                    f"work needs more, raise budget.{name} in {self._variable}"
                ),
            )
