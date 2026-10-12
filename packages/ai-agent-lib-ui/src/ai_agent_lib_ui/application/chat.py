"""Coordinate durable admission and server-owned tasks independently of the UI shell."""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from ai_agent_lib_core.contracts import Principal, Scope
from ai_agent_lib_ui.application.question_answer import QuestionAnswerAdapter
from ai_agent_lib_ui.contracts.chat import (
    ChatCatalog,
    Conversation,
    ThreadRepository,
    TurnDispatcher,
)
from ai_agent_lib_ui.contracts.conversations import RunRecord, TurnAdmission
from ai_agent_lib_ui.contracts.queries import ContractError, ErrorCode, ViewContext

logger = logging.getLogger(__name__)


def view_for(principal: Principal, agent_id: str) -> ViewContext:
    """Construct an owner-qualified target after the caller has been authenticated."""
    return ViewContext(
        principal=principal,
        scope=Scope(tenant=principal.tenant, subject=principal.subject, application=agent_id),
    )


class ChatService:
    """Admission survives refreshes; browser tasks only wait on owned request tasks."""

    def __init__(
        self, store: ThreadRepository, catalog: ChatCatalog, dispatcher: TurnDispatcher
    ) -> None:
        self.store, self.catalog, self.dispatcher = store, catalog, dispatcher
        self.tasks: dict[str, asyncio.Task[RunRecord]] = {}
        self.closing = False

    async def create(
        self, principal: Principal, agent_id: str, *, thread_id: str | None = None
    ) -> Conversation:
        """Start a thread only with a currently authorized, valid agent binding."""
        binding = await self.catalog.resolve(principal, agent_id)
        return await self.store.create(
            view_for(principal, agent_id),
            binding,
            thread_id or str(uuid4()),
            f"New chat · {agent_id}",
        )

    async def submit(
        self, principal: Principal, thread_id: str, key: str, question: str
    ) -> RunRecord:
        """Admit once; duplicate sends with the same content reattach to the same run."""
        if self.closing:
            raise ContractError(ErrorCode.UNAVAILABLE)
        conversation = await self.store.get(principal, thread_id)
        view = view_for(principal, conversation.thread.scope.application)
        turn = TurnAdmission(
            scope=view.scope, thread_id=thread_id, idempotency_key=key, question=question
        )
        QuestionAnswerAdapter(conversation.binding.manifest).request(view, turn)
        run = await self.store.admit(view, turn)
        task = self.tasks.get(run.run_id)
        if run.state == "accepted" and task is None:
            task = asyncio.create_task(self._execute(view, conversation, turn, run))
            self.tasks[run.run_id] = task
            task.add_done_callback(lambda finished: self._forget(run.run_id, finished))
        if task is not None:
            # Closing a tab, pressing Stop or cancelling a browser handler cannot
            # cancel an HTTP request whose outcome the application must record.
            return await asyncio.shield(task)
        return run

    def _forget(self, run_id: str, task: asyncio.Task[RunRecord]) -> None:
        self.tasks.pop(run_id, None)
        if not task.cancelled() and task.exception() is not None:
            # Never log prompts, answers, credentials or downstream exception text.
            logger.error("UI persistence/dispatch failed for run %s; inspect durable state", run_id)

    async def _execute(
        self, view: ViewContext, conversation: Conversation, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        try:
            fresh = await self.catalog.resolve(view.principal, view.scope.application)
            if fresh != conversation.binding:
                raise ContractError(ErrorCode.STALE_BINDING)
        except Exception:  # noqa: BLE001 - a provider failure before dispatch is terminal
            # No HTTP dispatch has happened. The admitted question has a known
            # failed outcome and will never be replayed by its idempotency key.
            failed = run.model_copy(update={"state": "failed", "updated_at": datetime.now(UTC)})
            if not await self.store.update(view, failed, expected=run):
                raise ContractError(ErrorCode.CONFLICT) from None
            return failed
        running = run.model_copy(update={"state": "running", "updated_at": datetime.now(UTC)})
        if not await self.store.update(view, running, expected=run):
            raise ContractError(ErrorCode.CONFLICT)
        try:
            outcome = await self.dispatcher.dispatch(view, fresh, turn, running)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - preserve uncertain outcomes
            outcome = running.model_copy(
                update={"state": "unknown", "updated_at": datetime.now(UTC)}
            )
        # A failed commit leaves running/unknown admission held. Startup recovery
        # handles process crashes without exposing a response that was not saved.
        if not await self.store.update(view, outcome, expected=running):
            raise ContractError(ErrorCode.CONFLICT)
        return outcome

    async def reattach(self, principal: Principal, thread_id: str) -> None:
        """Wait for this thread's existing task; never create work while resuming."""
        conversation = await self.store.get(principal, thread_id)
        view = view_for(principal, conversation.thread.scope.application)
        for stored in await self.store.history(view, thread_id):
            task = self.tasks.get(stored.run.run_id)
            if task is not None:
                await asyncio.shield(task)

    async def close(self) -> None:
        """Drain already admitted requests; dispatchers provide finite timeouts."""
        self.closing = True
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
