"""Persistence rules independent of the UI framework."""

import pytest
from fastapi import HTTPException
from spike.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.db")
    value.update_thread("demo/alice", "t1", metadata={"chat_profile": "rmprep-demo"})
    return value


def test_objects_keep_order_and_cannot_move_between_owners(store):
    for identifier in ("a", "b"):
        store.put_object("demo/alice", "step", {"id": identifier, "threadId": "t1"})
    store.put_object("demo/alice", "step", {"id": "a", "threadId": "t1", "output": "updated"})
    assert [s["id"] for s in store.thread("demo/alice", "t1")["steps"]] == ["a", "b"]
    store.update_thread("demo/bob", "t2")
    with pytest.raises(HTTPException):
        store.put_object("demo/bob", "step", {"id": "a", "threadId": "t2"})


def test_admission_deduplicates_and_recovery_never_replays(store):
    assert store.admit("demo/alice", "t1", "r1", "Question")
    assert not store.admit("demo/alice", "t1", "r1", "Question")
    with pytest.raises(HTTPException):
        store.admit("demo/alice", "t1", "r1", "Changed")
    with pytest.raises(HTTPException):
        store.admit("demo/alice", "t1", "r2", "Concurrent")
    store.recover()
    assert store.run("demo/alice", "r1")["status"] == "unknown"
    assert not store.admit("demo/alice", "t1", "r1", "Question")
    with pytest.raises(HTTPException):
        store.admit("demo/alice", "t1", "r2", "Try again")


@pytest.mark.parametrize("owner", ["demo/bob", "other/alice"])
def test_owner_and_tenant_scope(store, owner):
    store.admit("demo/alice", "t1", "r1", "Question")
    assert store.threads(owner) == []
    for read in (
        lambda: store.thread(owner, "t1"),
        lambda: store.run(owner, "r1"),
        lambda: store.update_thread(owner, "t1", name="Take over"),
    ):
        with pytest.raises(HTTPException) as error:
            read()
        assert error.value.status_code == 404


def test_agent_binding_cannot_change_and_revoked_sessions_fail(store):
    with pytest.raises(HTTPException):
        store.update_thread("demo/alice", "t1", metadata={"chat_profile": "help-demo"})
    token = store.session("demo/alice")
    assert store.owner(token) == "demo/alice"
    store.revoke(token)
    with pytest.raises(HTTPException):
        store.owner(token)
