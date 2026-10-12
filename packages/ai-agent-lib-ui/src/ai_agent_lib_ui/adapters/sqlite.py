"""SQLite stores UI history only; agent checkpoints stay behind the agent API."""

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
        with self.connection() as db:
            result = self._get(db, principal, thread_id)
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
        with self.connection() as db:
            rows = db.execute(
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

    @staticmethod
    def _touch(db: sqlite3.Connection, thread: ThreadRecord, stamp: datetime) -> None:
        thread = thread.model_copy(update={"updated_at": stamp})
        db.execute(
            "UPDATE threads SET updated=?,record=? WHERE id=?",
            (stamp.isoformat(), thread.model_dump_json(), thread.thread_id),
        )

    async def update(self, view: ViewContext, run: RunRecord, *, expected: RunRecord) -> bool:
        """Atomically save a legal outcome and accepted answer, with compare-and-set."""
        await self.authorizer.require(view, Permission.CONVERSATION_WRITE)
        identity = ("scope", "thread_id", "run_id", "idempotency_key", "accepted_at")
        if run.scope != view.scope or any(
            getattr(run, key) != getattr(expected, key) for key in identity
        ):
            raise ContractError(ErrorCode.NOT_FOUND)
        require_transition(expected.state, run.state)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            conversation = self._get(db, view.principal, run.thread_id)
            if conversation.thread.scope != view.scope:
                raise ContractError(ErrorCode.NOT_FOUND)
            updated = (
                db.execute(
                    "UPDATE runs SET state=?,record=? WHERE id=? AND record=?",
                    (
                        run.state,
                        run.model_dump_json(),
                        run.run_id,
                        expected.model_dump_json(),
                    ),
                ).rowcount
                == 1
            )
            if updated:
                self._touch(db, conversation.thread, run.updated_at)
        return updated

    async def history(self, view: ViewContext, thread_id: str) -> tuple[StoredTurn, ...]:
        """Restore admitted user messages and accepted answers, never framework drafts."""
        conversation = await self.get(view.principal, thread_id)
        if conversation.thread.scope != view.scope:
            raise ContractError(ErrorCode.NOT_FOUND)
        with self.connection() as db:
            rows = db.execute(
                "SELECT question,record FROM runs WHERE thread=? ORDER BY rowid", (thread_id,)
            ).fetchall()
        return tuple(
            StoredTurn(question=row[0], run=RunRecord.model_validate_json(row[1])) for row in rows
        )

    def recover(self) -> None:
        """Hold admission for interrupted runs; call only under the server process lock."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for run_id, record in db.execute(
                "SELECT id,record FROM runs WHERE state IN ('accepted','running')"
            ).fetchall():
                run = RunRecord.model_validate_json(record).model_copy(
                    update={"state": "unknown", "updated_at": now()}
                )
                db.execute(
                    "UPDATE runs SET state='unknown',record=? WHERE id=?",
                    (run.model_dump_json(), run_id),
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
