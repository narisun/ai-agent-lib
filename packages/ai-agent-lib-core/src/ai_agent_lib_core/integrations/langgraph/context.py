"""Finding the request context for the call in progress."""

from __future__ import annotations

from langgraph.config import get_config
from langgraph.runtime import get_runtime
from pydantic import ValidationError

from ai_agent_lib_core.contracts import RequestContext
from ai_agent_lib_core.contracts.diagnostics import DiagnosticLinks
from ai_agent_lib_core.pipeline import bound_request_context

__all__ = ["current_request_context"]


def current_diagnostic_links() -> DiagnosticLinks:
    """Read known framework links, without inspecting messages or checkpoint data.

    checkpoint_id, when supplied in configuration, names the input checkpoint. It
    does not claim to be the checkpoint produced after this model call finishes.
    """
    try:
        config = get_config()
    except RuntimeError:
        return DiagnosticLinks()
    node = config.get("metadata", {}).get("langgraph_node")
    checkpoint = config.get("configurable", {}).get("checkpoint_id")
    try:
        return DiagnosticLinks(node=node, checkpoint_id=checkpoint)
    except ValidationError:
        return DiagnosticLinks()


def current_request_context() -> RequestContext | None:
    """Return the request context of the current call, if there is one.

    An explicitly bound context wins. Otherwise the context comes from the
    runtime of the LangGraph run in progress. Graph state and message content
    are never consulted.
    """
    bound = bound_request_context()
    if bound is not None:
        return bound
    try:
        runtime: object = get_runtime()
    except RuntimeError:  # not inside any runnable
        return None
    # Inside a runnable that is not part of a graph run there is no runtime at all.
    context: object = getattr(runtime, "context", None)
    return context if isinstance(context, RequestContext) else None
