"""Binding a request context to the current task, outside any agent framework.

Inside a LangGraph run the request context travels in the graph's runtime
context. Code that runs elsewhere, such as an MCP tool handler or a script,
binds it explicitly::

    with bind_request_context(context):
        await model.ainvoke(messages)
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ai_agent_lib_core.contracts import RequestContext

__all__ = ["bind_request_context", "bound_request_context"]

_BOUND: ContextVar[RequestContext | None] = ContextVar("agentlib_request_context", default=None)


@contextmanager
def bind_request_context(context: RequestContext) -> Iterator[RequestContext]:
    """Make ``context`` the request context for the enclosed block."""
    if not isinstance(context, RequestContext):
        raise TypeError("context must be a RequestContext")
    token = _BOUND.set(context)
    try:
        yield context
    finally:
        _BOUND.reset(token)


def bound_request_context() -> RequestContext | None:
    """Return the explicitly bound request context, if there is one."""
    return _BOUND.get()
