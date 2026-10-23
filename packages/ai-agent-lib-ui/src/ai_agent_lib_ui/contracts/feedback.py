"""Where a viewer's judgement of an answer is kept."""

from typing import Protocol

from ai_agent_lib_core.evaluation.feedback import FeedbackRecord

__all__ = ["FeedbackLog", "FeedbackRecord"]


class FeedbackLog(Protocol):
    """Appends judgements; never reads them back on the request path."""

    async def write(self, record: FeedbackRecord) -> None:
        """Append one record."""
        ...
