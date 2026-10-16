"""SQLite stores UI history only; agent checkpoints stay behind the agent API."""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_ui.contracts.catalog import AgentBinding
from ai_agent_lib_ui.contracts.chat import Conversation, StoredTurn
from ai_agent_lib_ui.contracts.conversations import (
    RunRecord,
    ThreadRecord,
    TurnAdmission,
    require_transition,
)
from ai_agent_lib_ui.contracts.ports import Authorizer
from ai_agent_lib_ui.contracts.queries import (
    ContractError,
    ErrorCode,
    Page,
    PageRequest,
    Permission,
    ViewContext,
)


def now() -> datetime:
    """Use aware UTC timestamps in persisted records."""
    return datetime.now(UTC)


class SqliteThreads:
    """Use short transactions for admission, compare-and-set and accepted output.

    Each method opens its own connection. The unique active-run index coordinates
    competing callers even when they use different connections. A server holds a
    separate process lock before recovering unfinished work after a restart.
    """

    def __init__(self, path: Path, authorizer: Authorizer, cursor_secret: bytes) -> None:
        self.path, self.authorizer, self.cursor_secret = path, authorizer, cursor_secret
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(
                files("ai_agent_lib_ui").joinpath("migrations/001_local.sql").read_text()
            )
            if db.execute("SELECT version FROM ui_schema").fetchall() != [(1,)]:
                raise ValueError("unsupported UI database schema")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Commit each operation as a unit and always release the file handle."""
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _get(db: sqlite3.Connection, principal: Principal, thread_id: str) -> Conversation:
        row = db.execute(
            "SELECT record, binding FROM threads WHERE id=? AND tenant=? AND subject=?",
            (thread_id, principal.tenant, principal.subject),
        ).fetchone()
        if row is None:
            raise ContractError(ErrorCode.NOT_FOUND)
        return Conversation(
            thread=ThreadRecord.model_validate_json(row[0]),
            binding=AgentBinding.model_validate_json(row[1]),
        )

    async def get(self, principal: Principal, thread_id: str) -> Conversation:
        """Check both ownership and current permission before exposing a thread."""

        def read() -> Conversation:
            with self.connection() as db:
                return self._get(db, principal, thread_id)

        result = await asyncio.to_thread(read)
        await self.authorizer.require(
            ViewContext(principal=principal, scope=result.thread.scope),
            Permission.CONVERSATION_READ,
        )
        return result

    async def create(
        self, view: ViewContext, binding: AgentBinding, thread_id: str, title: str
    ) -> Conversation:
        """Bind a new thread to an agent permanently; repeated creation is harmless."""
        await self.authorizer.require(view, Permission.CONVERSATION_WRITE)
        if binding.manifest.agent_id != view.scope.application:
            raise ContractError(ErrorCode.NOT_FOUND)
        stamp = now()
        thread = ThreadRecord(
            scope=view.scope, thread_id=thread_id, title=title, created_at=stamp, updated_at=stamp
        )

        def write() -> Conversation:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO threads VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        thread_id,
                        view.scope.tenant,
                        view.scope.subject,
                        view.scope.application,
                        title,
                        stamp.isoformat(),
                        0,
                        thread.model_dump_json(),
                        binding.model_dump_json(),
                    ),
                )
                result = self._get(db, view.principal, thread_id)
                if result.thread.scope != view.scope or result.binding != binding:
                    raise ContractError(ErrorCode.CONFLICT)
            return result

        return await asyncio.to_thread(write)

    async def change(
        self,
        view: ViewContext,
        thread_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ThreadRecord:
        """Rename or archive; existing transcript and pending work remain intact."""
        await self.authorizer.require(view, Permission.CONVERSATION_WRITE)

        def write() -> ThreadRecord:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                conversation = self._get(db, view.principal, thread_id)
                if conversation.thread.scope != view.scope:
                    raise ContractError(ErrorCode.NOT_FOUND)
                values = conversation.thread.model_dump()
                values.update(updated_at=now())
                if title is not None:
                    if not title.strip():
                        raise ContractError(ErrorCode.INVALID_INPUT)
                    values["title"] = title.strip()
                if archived is not None:
                    values["archived"] = archived
                thread = ThreadRecord.model_validate(values)
                db.execute(
                    "UPDATE threads SET title=?,updated=?,archived=?,record=? WHERE id=?",
                    (
                        thread.title,
                        thread.updated_at.isoformat(),
                        thread.archived,
                        thread.model_dump_json(),
                        thread_id,
                    ),
                )
            return thread

        return await asyncio.to_thread(write)

    async def threads(self, view: ViewContext, page: PageRequest) -> Page[ThreadRecord]:
        """List within exactly one authorized agent scope."""
        await self.authorizer.require(view, Permission.CONVERSATION_READ)
        return await self._list(view.principal, page, agent=view.scope.application)

    async def list_owned(
        self, principal: Principal, page: PageRequest, *, search: str = "", archived: bool = False
    ) -> Page[ThreadRecord]:
        """List this owner's threads across agents, rechecking each entry's access."""
        return await self._list(principal, page, search=search, archived=archived)

    async def _list(
        self,
        principal: Principal,
        page: PageRequest,
        *,
        agent: str = "",
        search: str = "",
        archived: bool = False,
    ) -> Page[ThreadRecord]:
        if len(search) > 256:
            raise ContractError(ErrorCode.INVALID_INPUT)
        binding = [principal.tenant, principal.subject, agent, search, archived]
        after = ["9999", ""]
        if page.cursor:
            try:
                payload, signature = page.cursor.split(".")
                if not hmac.compare_digest(
                    signature, hmac.digest(self.cursor_secret, payload.encode(), "sha256").hex()
                ):
                    raise ValueError
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                if decoded["binding"] != binding:
                    raise ValueError
                after = decoded["after"]
            except (ValueError, KeyError, TypeError):
                raise ContractError(ErrorCode.INVALID_INPUT) from None
        query = """SELECT record,updated,id FROM threads
            WHERE tenant=? AND subject=? AND archived=? AND (?='' OR agent=?)
            AND instr(lower(title), lower(?))>0
            AND (updated < ? OR (updated = ? AND id > ?))
            ORDER BY updated DESC,id LIMIT ?"""

        def read() -> list[tuple[str, str, str]]:
            with self.connection() as db:
                return db.execute(
                    query,
                    (
                        principal.tenant,
                        principal.subject,
                        archived,
                        agent,
                        agent,
                        search,
                        after[0],
                        after[0],
                        after[1],
                        page.limit + 1,
                    ),
                ).fetchall()

        rows = await asyncio.to_thread(read)
        items: list[ThreadRecord] = []
        for row in rows[: page.limit]:
            thread = ThreadRecord.model_validate_json(row[0])
            try:
                await self.authorizer.require(
                    ViewContext(principal=principal, scope=thread.scope),
                    Permission.CONVERSATION_READ,
                )
            except ContractError:
                continue
            items.append(thread)
        cursor = None
        if len(rows) > page.limit:
            row = rows[page.limit - 1]
            payload = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "binding": binding,
                        "after": [row[1], row[2]],
                    }
                ).encode()
            ).decode()
            cursor = (
                payload + "." + hmac.digest(self.cursor_secret, payload.encode(), "sha256").hex()
            )
        return Page(items=tuple(items), next_cursor=cursor)

    async def admit(self, view: ViewContext, turn: TurnAdmission) -> RunRecord:
        """Persist the question and request ID before a network request is permitted."""
        await self.authorizer.require(view, Permission.AGENT_INVOKE)
        if turn.scope != view.scope:
            raise ContractError(ErrorCode.NOT_FOUND)
        stamp = now()
        run = RunRecord(
            scope=view.scope,
            thread_id=turn.thread_id,
            request_id=str(uuid4()),
            run_id=str(uuid4()),
            idempotency_key=turn.idempotency_key,
            state="accepted",
            accepted_at=stamp,
            updated_at=stamp,
        )

        def write() -> RunRecord:
            try:
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    conversation = self._get(db, view.principal, turn.thread_id)
                    if conversation.thread.scope != view.scope:
                        raise ContractError(ErrorCode.NOT_FOUND)
                    row = db.execute(
                        "SELECT question,record FROM runs WHERE thread=? AND key=?",
                        (turn.thread_id, turn.idempotency_key),
                    ).fetchone()
                    if row:
                        if row[0] != turn.question:
                            raise ContractError(ErrorCode.CONFLICT)
                        return RunRecord.model_validate_json(row[1])
                    if conversation.thread.archived:
                        raise ContractError(ErrorCode.CONFLICT)
                    db.execute(
                        "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                        (
                            run.run_id,
                            turn.thread_id,
                            turn.idempotency_key,
                            turn.question,
                            run.state,
                            run.model_dump_json(),
                        ),
                    )
                    self._touch(db, conversation.thread, stamp)
            except sqlite3.IntegrityError:
                raise ContractError(ErrorCode.CONFLICT) from None
            return run

        return await asyncio.to_thread(write)

    @staticmethod
    def _touch(db: sqlite3.Connection, thread: ThreadRecord, stamp: datetime) -> None:
        thread = thread.model_copy(update={"updated_at": stamp})
        db.execute(
            "UPDATE threads SET updated=?,record=? WHERE id=?",
            (stamp.isoformat(), thread.model_dump_json(), thread.thread_id),
        )

    async def update(self, view: ViewContext, run: RunRecord, *, expected: RunRecord) -> bool:
        """Save an ordinary state transition; unknown runs require reconciliation."""
        return await self._update(view, run, expected=expected)

    async def reconcile(self, view: ViewContext, run: RunRecord, *, expected: RunRecord) -> bool:
        """Audit a server-verified terminal outcome; never unlock by elapsed time alone."""
        if expected.state != "unknown":
            raise ContractError(ErrorCode.CONFLICT)
        return await self._update(view, run, expected=expected, reconciled=True)

    async def _update(
        self, view: ViewContext, run: RunRecord, *, expected: RunRecord, reconciled: bool = False
    ) -> bool:
        """Atomically save a legal outcome and accepted answer, with compare-and-set."""
        await self.authorizer.require(view, Permission.CONVERSATION_WRITE)
        identity = ("scope", "thread_id", "request_id", "run_id", "idempotency_key", "accepted_at")
        if run.scope != view.scope or any(
            getattr(run, key) != getattr(expected, key) for key in identity
        ):
            raise ContractError(ErrorCode.NOT_FOUND)
        require_transition(expected.state, run.state, reconciled=reconciled)

        def write() -> bool:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                conversation = self._get(db, view.principal, run.thread_id)
                if conversation.thread.scope != view.scope:
                    raise ContractError(ErrorCode.NOT_FOUND)
                row = db.execute("SELECT record FROM runs WHERE id=?", (run.run_id,)).fetchone()
                # Older JSON records omit newly optional fields. Compare their parsed
                # values, then use the exact stored bytes for compare-and-set.
                if row is None or RunRecord.model_validate_json(row[0]) != expected:
                    return False
                updated = (
                    db.execute(
                        "UPDATE runs SET state=?,record=? WHERE id=? AND record=?",
                        (
                            run.state,
                            run.model_dump_json(),
                            run.run_id,
                            row[0],
                        ),
                    ).rowcount
                    == 1
                )
                if updated:
                    for item in run.artifacts:
                        artifact = item.artifact
                        if artifact.content is not None:
                            # Repeated or conflicting declarations cannot overwrite an
                            # accepted version. Optional documents never reject the answer.
                            db.execute(
                                "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?,?)",
                                (
                                    run.thread_id,
                                    artifact.id,
                                    artifact.document_id,
                                    artifact.version,
                                    run.run_id,
                                    item.model_dump_json(),
                                ),
                            )
                    self._touch(db, conversation.thread, run.updated_at)
                    if reconciled:
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS reconciliations "
                            "(run TEXT, request TEXT, subject TEXT, state TEXT, stamp TEXT)"
                        )
                        db.execute(
                            "INSERT INTO reconciliations VALUES (?,?,?,?,?)",
                            (
                                run.run_id,
                                run.request_id,
                                view.principal.subject,
                                run.state,
                                run.updated_at.isoformat(),
                            ),
                        )
            return updated

        return await asyncio.to_thread(write)

    async def history(self, view: ViewContext, thread_id: str) -> tuple[StoredTurn, ...]:
        """Read the latest bounded page; use history_page to retrieve older exchanges."""
        return (await self.history_page(view, thread_id, PageRequest())).items

    async def turn(self, view: ViewContext, thread_id: str, run_id: str) -> StoredTurn:
        """Read one owned turn, including older turns outside the recent history page."""
        conversation = await self.get(view.principal, thread_id)
        if conversation.thread.scope != view.scope:
            raise ContractError(ErrorCode.NOT_FOUND)

        def read() -> StoredTurn:
            with self.connection() as db:
                self._get(db, view.principal, thread_id)
                row = db.execute(
                    "SELECT question,record FROM runs WHERE thread=? AND id=?", (thread_id, run_id)
                ).fetchone()
            if row is None:
                raise ContractError(ErrorCode.NOT_FOUND)
            run = RunRecord.model_validate_json(row[1])
            if run.scope != view.scope or run.thread_id != thread_id:
                raise ContractError(ErrorCode.NOT_FOUND)
            return StoredTurn(question=row[0], run=run)

        return await asyncio.to_thread(read)

    async def history_page(
        self, view: ViewContext, thread_id: str, page: PageRequest
    ) -> Page[StoredTurn]:
        """Load a bounded page off the event loop, with a cursor bound to its owner/thread."""
        conversation = await self.get(view.principal, thread_id)
        if conversation.thread.scope != view.scope:
            raise ContractError(ErrorCode.NOT_FOUND)
        binding = [
            "history",
            [view.scope.tenant, view.scope.subject, view.scope.application],
            thread_id,
        ]
        before = 2**63 - 1
        if page.cursor:
            try:
                payload, signature = page.cursor.split(".")
                expected = hmac.digest(self.cursor_secret, payload.encode(), "sha256").hex()
                if not hmac.compare_digest(signature, expected):
                    raise ValueError
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                if decoded["binding"] != binding or type(decoded["before"]) is not int:
                    raise ValueError
                before = decoded["before"]
            except (ValueError, KeyError, TypeError):
                raise ContractError(ErrorCode.INVALID_INPUT) from None

        def read() -> Page[StoredTurn]:
            items: list[StoredTurn] = []
            size, last, more = 0, before, False
            with self.connection() as db:
                self._get(db, view.principal, thread_id)
                rows = db.execute(
                    "SELECT rowid,question,record FROM runs WHERE thread=? AND rowid<? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (thread_id, before, page.limit + 1),
                )
                for position, question, record in rows:
                    amount = len(record.encode("utf-8")) + len(question.encode("utf-8"))
                    # Always include one turn so even a large answer remains accessible.
                    if len(items) >= page.limit or (items and size + amount > 2 * 1024 * 1024):
                        more = True
                        break
                    items.append(
                        StoredTurn(question=question, run=RunRecord.model_validate_json(record))
                    )
                    size += amount
                    last = position
            cursor = None
            if more:
                payload = base64.urlsafe_b64encode(
                    json.dumps({"binding": binding, "before": last}).encode()
                ).decode()
                cursor = (
                    payload
                    + "."
                    + hmac.digest(self.cursor_secret, payload.encode(), "sha256").hex()
                )
            return Page(items=tuple(reversed(items)), next_cursor=cursor)

        return await asyncio.to_thread(read)

    def recover(self) -> None:
        """Recover interrupted runs under the process lock without replaying requests."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for run_id, record in db.execute(
                "SELECT id,record FROM runs WHERE state IN ('accepted','running')"
            ).fetchall():
                previous = RunRecord.model_validate_json(record)
                # Dispatch starts only after the durable transition to running.
                # An accepted run is therefore safe to fail and release.
                state = "failed" if previous.state == "accepted" else "unknown"
                run = previous.model_copy(update={"state": state, "updated_at": now()})
                db.execute(
                    "UPDATE runs SET state=?,record=? WHERE id=?",
                    (state, run.model_dump_json(), run_id),
                )

    def new_session(self, identity: str) -> str:
        """Persist only a digest of a random, expiring local login cookie."""
        token = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    identity,
                    time.time() + 3600,
                ),
            )
        return token

    def session(self, token: str, identity: str) -> None:
        """Revalidate expiry and current configured identity on every operation."""
        with self.connection() as db:
            row = db.execute(
                "SELECT identity,expires FROM sessions WHERE digest=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if row is None or row[0] != identity or row[1] <= time.time():
            raise ContractError(ErrorCode.NOT_FOUND)

    def revoke(self, token: str) -> None:
        """Signing out invalidates future operations on existing browser sockets too."""
        with self.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),)
            )
