"""One SQLite store for the sample's threads, messages, runs, and identities.

Short synchronous transactions keep the experiment easy to inspect. The later
package needs an asynchronous storage adapter and versioned migrations.
"""

import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException


def now() -> str:
    """Return a sortable UTC timestamp."""
    return datetime.now(UTC).isoformat()


class Store:
    """Persist application-owned records; every content lookup checks its owner."""

    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    token TEXT PRIMARY KEY, owner TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, owner TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS objects(
                    id TEXT PRIMARY KEY, thread_id TEXT, kind TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, thread_id TEXT, owner TEXT, question TEXT,
                    status TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS invocations(
                    id TEXT PRIMARY KEY, thread_id TEXT, owner TEXT, question TEXT, result TEXT);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived database connection."""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, owner: str) -> str:
        """Create an opaque session for an already authenticated fixture user."""
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (token, owner, time.time() + 3600))
        return token

    def owner(self, token: str) -> str:
        """Resolve an unexpired parent session, independently of Chainlit JWTs."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        if row is None or row["expires"] <= time.time():
            raise HTTPException(401, "Local session required")
        return row["owner"]

    def revoke(self, token: str) -> None:
        """End a parent session, including its future WebSocket operations."""
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (token,))

    def thread(self, owner: str, thread_id: str) -> dict:
        """Read one owned thread and its bounded content."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM threads WHERE id=? AND owner=?", (thread_id, owner)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Thread not found")
            data = json.loads(row["data"])
            objects = db.execute(
                "SELECT kind,data FROM objects WHERE thread_id=? ORDER BY rowid LIMIT 400",
                (thread_id,),
            ).fetchall()
        data["steps"] = [json.loads(o["data"]) for o in objects if o["kind"] == "step"]
        data["elements"] = [json.loads(o["data"]) for o in objects if o["kind"] == "element"]
        return data

    def update_thread(self, owner: str, thread_id: str, **changes) -> None:
        """Create or update metadata without allowing ownership transfer."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if row is not None and row["owner"] != owner:
                raise HTTPException(404, "Thread not found")
            data = (
                json.loads(row["data"])
                if row
                else {
                    "id": thread_id,
                    "userId": owner,
                    "userIdentifier": owner,
                    "createdAt": now(),
                    "name": "New conversation",
                    "metadata": {},
                    "tags": [],
                }
            )
            for key, value in changes.items():
                if value is not None:
                    if key == "metadata" and data["metadata"].get("chat_profile"):
                        selected = data["metadata"]["chat_profile"]
                        if value.get("chat_profile", selected) != selected:
                            raise HTTPException(409, "Start a new thread to change agents")
                    data[key] = {**data.get(key, {}), **value} if key == "metadata" else value
            db.execute(
                "INSERT OR REPLACE INTO threads VALUES(?,?,?)", (thread_id, owner, json.dumps(data))
            )

    def threads(self, owner: str, search: str = "") -> list[dict]:
        """List only the caller's thread summaries."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT data FROM threads WHERE owner=? ORDER BY rowid DESC LIMIT 200", (owner,)
            ).fetchall()
        return [
            data
            for row in rows
            if search.lower() in (data := json.loads(row["data"]))["name"].lower()
        ]

    def put_object(self, owner: str, kind: str, data: dict) -> None:
        """Upsert a message or element only within its original thread."""
        self.thread(owner, data["threadId"])
        with self.connect() as db:
            old = db.execute("SELECT thread_id FROM objects WHERE id=?", (data["id"],)).fetchone()
            if old and old["thread_id"] != data["threadId"]:
                raise HTTPException(404, "Object not found")
            db.execute(
                "INSERT INTO objects VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (data["id"], data["threadId"], kind, json.dumps(data)),
            )

    def object(self, owner: str, object_id: str, thread_id: str | None = None) -> dict:
        """Read a scoped object and check an optional enclosing thread."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if row is None or (thread_id is not None and row["thread_id"] != thread_id):
            raise HTTPException(404, "Object not found")
        self.thread(owner, row["thread_id"])
        return json.loads(row["data"])

    def admit(self, owner: str, thread_id: str, run_id: str, question: str) -> bool:
        """Deduplicate admission and reject concurrent turns in this sample."""
        self.thread(owner, thread_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if old:
                if (old["owner"], old["thread_id"], old["question"]) != (
                    owner,
                    thread_id,
                    question,
                ):
                    raise HTTPException(409, "Idempotency conflict")
                return False
            if db.execute(
                "SELECT 1 FROM runs WHERE thread_id=? AND status IN ('running','unknown')",
                (thread_id,),
            ).fetchone():
                raise HTTPException(409, "The thread already has an unresolved turn")
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (run_id, thread_id, owner, question, "running", "{}"),
            )
        return True

    def complete(self, owner: str, run_id: str, result: dict, status: str = "completed") -> None:
        """Persist the outcome for a previously admitted turn."""
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?, result=? WHERE id=? AND owner=?",
                (status, json.dumps(result), run_id, owner),
            )

    def run(self, owner: str, run_id: str) -> dict:
        """Read a run without revealing another subject's IDs."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE id=? AND owner=?", (run_id, owner)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found")
        return {**dict(row), "result": json.loads(row["result"])}

    def runs(self, owner: str, thread_id: str) -> list[dict]:
        """Read a thread's run history."""
        self.thread(owner, thread_id)
        with self.connect() as db:
            ids = db.execute(
                "SELECT id FROM runs WHERE owner=? AND thread_id=? ORDER BY rowid",
                (owner, thread_id),
            ).fetchall()
        return [self.run(owner, row["id"]) for row in ids]

    def recover(self) -> None:
        """Mark interrupted runs unknown; never replay HTTP requests at startup."""
        with self.connect() as db:
            db.execute("UPDATE runs SET status='unknown' WHERE status='running'")
