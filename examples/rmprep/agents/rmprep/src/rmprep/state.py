"""The conversation shared by the router and both specialists."""

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from rmprep.memos import MemoVersion, merge_memos
from rmprep.selection import ContextSelection

Route = Literal["memoagent", "qnaagent"]


class State(TypedDict):
    """Keep messages in the checkpoint and replace the route on each request."""

    messages: Annotated[list[AnyMessage], add_messages]
    route: NotRequired[Route]
    memos: NotRequired[Annotated[list[MemoVersion], merge_memos]]
    active_memo_id: NotRequired[str]
    active_client: NotRequired[str]
    selection: NotRequired[ContextSelection]
    context_diagnostics: NotRequired[dict[str, object]]
    context_version: NotRequired[int]
