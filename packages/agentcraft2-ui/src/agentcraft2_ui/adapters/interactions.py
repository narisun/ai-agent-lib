"""Export saved conversations as interaction records for online evals."""

from __future__ import annotations

from pathlib import Path

from agentcraft2_core.contracts import Principal
from agentcraft2_core.evaluation.feedback import load_feedback
from agentcraft2_core.evaluation.online import Interaction, write_interactions
from agentcraft2_ui.contracts.chat import StoredTurn, ThreadRepository
from agentcraft2_ui.contracts.conversations import RunRecord, ThreadRecord
from agentcraft2_ui.contracts.queries import PageRequest, ViewContext

__all__ = ["export_interactions"]


async def export_interactions(
    store: ThreadRepository,
    principal: Principal,
    out: Path,
    *,
    feedback_path: Path | None = None,
) -> int:
    """Write every completed exchange the principal owns as interaction JSONL.

    Active and archived conversations are both included. Feedback, when a
    file is given, is joined by request ID; the latest line for a request
    wins. Returns how many interactions were written. The output holds
    conversation content; keep it beside the UI's other private state.
    """
    verdicts: dict[str, str] = {}
    if feedback_path is not None and feedback_path.exists():
        for record in load_feedback(feedback_path):
            verdicts[record.request_id] = record.verdict
    interactions: list[Interaction] = []
    for thread in await _owned_threads(store, principal):
        for question, run in await _completed_turns(store, principal, thread):
            interactions.append(
                Interaction(
                    request_id=run.request_id,
                    thread_id=thread.thread_id,
                    question=question,
                    answer=run.answer or "",
                    application=thread.scope.application,
                    verdict=verdicts.get(run.request_id, ""),
                )
            )
    write_interactions(out, interactions)
    return len(interactions)


async def _owned_threads(store: ThreadRepository, principal: Principal) -> list[ThreadRecord]:
    """Page through every thread this principal owns, across all its scopes.

    Both active and archived threads are included, since archiving a
    conversation does not remove its completed runs from export scope.
    """
    threads: list[ThreadRecord] = []
    for archived in (False, True):
        page = await store.list_owned(principal, PageRequest(), archived=archived)
        threads.extend(page.items)
        while page.next_cursor:
            page = await store.list_owned(
                principal, PageRequest(cursor=page.next_cursor), archived=archived
            )
            threads.extend(page.items)
    return threads


async def _completed_turns(
    store: ThreadRepository, principal: Principal, thread: ThreadRecord
) -> list[tuple[str, RunRecord]]:
    """Page through this thread's full history and keep only completed runs."""
    view = ViewContext(principal=principal, scope=thread.scope)
    turns: list[StoredTurn] = []
    page = await store.history_page(view, thread.thread_id, PageRequest())
    turns.extend(page.items)
    while page.next_cursor:
        page = await store.history_page(
            view, thread.thread_id, PageRequest(cursor=page.next_cursor)
        )
        turns.extend(page.items)
    return [(turn.question, turn.run) for turn in turns if turn.run.state == "completed"]
