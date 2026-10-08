"""The budget stage: what one request may use, so a looping graph stops.

The ledger counts per request ID, in this process. An MCP server keeps its own
count for the requests it serves, under the same request ID the agent sends.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

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
    """What each recent request has used. Bounded: the oldest requests are forgotten.

    Args:
        max_requests: How many requests are remembered at once.
    """

    def __init__(self, max_requests: int = _MAX_REQUESTS) -> None:
        self._spent: OrderedDict[str, _Spent] = OrderedDict()
        self._max = max_requests

    def spent(self, request_id: str) -> _Spent:
        """Return what ``request_id`` has used so far, remembering it as recent."""
        spent = self._spent.get(request_id)
        if spent is None:
            spent = self._spent[request_id] = _Spent()
            while len(self._spent) > self._max:
                self._spent.popitem(last=False)
        else:
            self._spent.move_to_end(request_id)
        return spent


class _Call(Protocol):
    @property
    def context(self) -> RequestContext | None: ...

    @property
    def evidence(self) -> Evidence: ...


CallT = TypeVar("CallT", bound=_Call)
ResponseT = TypeVar("ResponseT")


class BudgetInterceptor(Generic[CallT, ResponseT]):
    """Stops a call that would take its request over budget.

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
        spent = self._ledger.spent(context.request_id)
        self._check(spent)
        if self._kind == "model":
            spent.model_calls += 1
        else:
            spent.tool_calls += 1
        response = await call_next(request)
        if self._usage is not None:
            usage = self._usage(response)
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    spent.tokens += value
        request.evidence.add(
            budget_model_calls=spent.model_calls,
            budget_tool_calls=spent.tool_calls,
            budget_tokens=spent.tokens,
        )
        return response

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
