"""Coordinate durable admission and server-owned tasks independently of the UI shell."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from agentcraft2_core.contracts import Principal, Scope
from agentcraft2_ui.application.catalog import same_agent_binding
from agentcraft2_ui.application.progress import (
    ProgressCallback,
    ProgressFeed,
    bind_progress,
    wait_with_progress,
)
from agentcraft2_ui.application.question_answer import QuestionAnswerAdapter
from agentcraft2_ui.contracts.chat import (
    ChatCatalog,
    Conversation,
    OutcomeReader,
    ThreadRepository,
    TurnDispatcher,
)
from agentcraft2_ui.contracts.conversations import RunRecord, TurnAdmission
from agentcraft2_ui.contracts.queries import ContractError, ErrorCode, ViewContext

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
        self.progress: dict[str, ProgressFeed] = {}
        self.admissions: set[asyncio.Task[tuple[RunRecord, asyncio.Task[RunRecord] | None]]] = set()
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
        self,
        principal: Principal,
        thread_id: str,
        key: str,
        question: str,
        *,
        on_progress: ProgressCallback | None = None,
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
        if self.closing:
            raise ContractError(ErrorCode.UNAVAILABLE)
        # SQLite's worker can commit after the browser is cancelled. Own both
        # admission and the handoff to execution, without a cancellable gap.
        admission = asyncio.create_task(self._admit_and_start(view, conversation, turn))
        self.admissions.add(admission)
        admission.add_done_callback(self._forget_admission)
        run, task = await asyncio.shield(admission)
        if task is not None:
            # Closing a tab, pressing Stop or cancelling a browser handler cannot
            # cancel an HTTP request whose outcome the application must record.
            feed = self.progress.get(run.run_id)
            if on_progress is not None and feed is not None:
                try:
                    await wait_with_progress(task, feed, on_progress)
                except Exception:  # noqa: BLE001 - display failures never change dispatch
                    logger.warning("Live display stopped; the admitted request continues")
            return await asyncio.shield(task)
        return run

    async def _admit_and_start(
        self, view: ViewContext, conversation: Conversation, turn: TurnAdmission
    ) -> tuple[RunRecord, asyncio.Task[RunRecord] | None]:
        run = await self.store.admit(view, turn)
        task = self.tasks.get(run.run_id)
        if run.state == "accepted" and task is None:
            self.progress[run.run_id] = ProgressFeed()
            task = asyncio.create_task(self._execute(view, conversation, turn, run))
            self.tasks[run.run_id] = task
            task.add_done_callback(lambda finished: self._forget(run.run_id, finished))
        return run, task

    def _forget_admission(
        self, task: asyncio.Task[tuple[RunRecord, asyncio.Task[RunRecord] | None]]
    ) -> None:
        self.admissions.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("UI admission failed; no request was dispatched by this admission")

    def _forget(self, run_id: str, task: asyncio.Task[RunRecord]) -> None:
        self.tasks.pop(run_id, None)
        self.progress.pop(run_id, None)
        if not task.cancelled() and task.exception() is not None:
            # Never log prompts, answers, credentials or downstream exception text.
            logger.error("UI persistence/dispatch failed for run %s; inspect durable state", run_id)

    async def _execute(
        self, view: ViewContext, conversation: Conversation, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        started = time.monotonic()
        try:
            fresh = await self.catalog.resolve(view.principal, view.scope.application)
            if not same_agent_binding(conversation.binding, fresh):
                raise ContractError(ErrorCode.STALE_BINDING)
        except Exception as error:  # noqa: BLE001 - a provider failure before dispatch is terminal
            # No HTTP dispatch has happened. The admitted question has a known
            # failed outcome and will never be replayed by its idempotency key.
            # The error's type is logged, never its text: it may hold configuration.
            logger.warning(
                "catalog resolution failed before dispatch; the run is marked failed",
                extra={
                    "error_type": type(error).__name__,
                    "agent_id": view.scope.application,
                    "thread_id": run.thread_id,
                    "request_id": run.request_id,
                },
            )
            failed = run.model_copy(
                update={
                    "state": "failed",
                    "updated_at": datetime.now(UTC),
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                }
            )
            if not await self.store.update(view, failed, expected=run):
                raise ContractError(ErrorCode.CONFLICT) from None
            return failed
        running = run.model_copy(
            update={
                "state": "running",
                "updated_at": datetime.now(UTC),
                "registry_revision": fresh.manifest.registry_revision,
            }
        )
        if not await self.store.update(view, running, expected=run):
            raise ContractError(ErrorCode.CONFLICT)
        try:
            with bind_progress(self.progress[run.run_id]):
                outcome = await self.dispatcher.dispatch(view, fresh, turn, running)
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - uncertain outcome
            logger.warning(
                "Agent outcome could not be confirmed",
                extra={
                    "request_id": run.request_id,
                    "thread_id": run.thread_id,
                    "error_type": type(error).__name__,
                },
            )
            outcome = running.model_copy(
                update={
                    "state": "unknown",
                    "updated_at": datetime.now(UTC),
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                }
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
            elif stored.run.state == "unknown" and isinstance(self.dispatcher, OutcomeReader):
                try:
                    fresh = await self.catalog.resolve(principal, view.scope.application)
                    if not same_agent_binding(conversation.binding, fresh):
                        continue
                    outcome = await self.dispatcher.outcome(view, fresh, stored)
                    if outcome is not None:
                        await self.store.reconcile(view, outcome, expected=stored.run)
                except Exception:  # noqa: BLE001 - absence never permits replay
                    logger.warning(
                        "Outcome reconciliation unavailable for run %s", stored.run.run_id
                    )

    async def close(self) -> None:
        """Drain already admitted requests; dispatchers provide finite timeouts."""
        self.closing = True
        if self.admissions:
            await asyncio.gather(*tuple(self.admissions), return_exceptions=True)
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
