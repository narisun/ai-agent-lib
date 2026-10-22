"""Durable terminal outcomes for read-only reconciliation after a lost HTTP response."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol

from ai_agent_lib_core.contracts import IntegrityError, RequestContext

_MAX_ENVELOPE_BYTES = 8 * 1024 * 1024


class OutcomeJournal(Protocol):
    """Reserve once, save only confirmed outcomes, and read with the same authority."""

    async def begin(self, context: RequestContext) -> bool:
        """Return false if this scoped request ID was already admitted."""
        ...

    async def finish(self, context: RequestContext, envelope: dict[str, object]) -> None:
        """Persist a confirmed terminal envelope before it is sent to the caller."""
        ...

    async def read(self, context: RequestContext, request_id: str) -> dict[str, object] | None:
        """Return only the same owner's outcome; absent and pending both return None."""
        ...


def _owner(context: RequestContext) -> str:
    principal = context.principal
    authority = [
        context.application,
        principal.tenant,
        principal.subject,
        sorted(principal.roles),
        principal.authenticated_by,
        principal.delegation_chain,
        principal.kind,
        int(context.classification_ceiling),
    ]
    return hashlib.sha256(json.dumps(authority).encode()).hexdigest()


class SqliteOutcomeJournal:
    """Local single-host journal. Deployments can inject a shared durable implementation.

    A crash before finish leaves no recoverable outcome. Absence never grants
    permission to repeat a request. This file contains accepted answers and needs
    the same access controls and retention policy as the checkpoint database.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _execute(
        self,
        context: RequestContext,
        request_id: str,
        operation: str,
        envelope: dict[str, object] | None = None,
    ) -> object:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS outcomes (owner TEXT, thread TEXT, request TEXT, "
                "body TEXT, PRIMARY KEY(owner,thread,request))"
            )
            key = (_owner(context), context.thread_id, request_id)
            if operation == "begin":
                return (
                    db.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?,?,NULL)", key).rowcount
                    == 1
                )
            if operation == "finish":
                body = json.dumps(envelope)
                if len(body.encode()) > _MAX_ENVELOPE_BYTES:
                    # The answer was produced but cannot be recorded for recovery.
                    raise IntegrityError(
                        "the terminal outcome could not be journaled",
                        expected=f"an envelope of at most {_MAX_ENVELOPE_BYTES} bytes",
                        actual=f"an envelope of {len(body.encode())} bytes",
                        fix=(
                            "lower the agent's response size, or inject a journal "
                            "with a higher limit"
                        ),
                    )
                db.execute(
                    "UPDATE outcomes SET body=? WHERE owner=? AND thread=? "
                    "AND request=? AND body IS NULL",
                    (body, *key),
                )
                return None
            row = db.execute(
                "SELECT body FROM outcomes WHERE owner=? AND thread=? AND request=?", key
            ).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None

    async def begin(self, context: RequestContext) -> bool:
        """Reserve one scoped request off the event loop."""
        return bool(await asyncio.to_thread(self._execute, context, context.request_id, "begin"))

    async def finish(self, context: RequestContext, envelope: dict[str, object]) -> None:
        """Commit the confirmed envelope before the response is transmitted."""
        await asyncio.to_thread(self._execute, context, context.request_id, "finish", envelope)

    async def read(self, context: RequestContext, request_id: str) -> dict[str, object] | None:
        """Read only the outcome owned by this current authenticated authority."""
        result = await asyncio.to_thread(self._execute, context, request_id, "read")
        return result if isinstance(result, dict) else None
