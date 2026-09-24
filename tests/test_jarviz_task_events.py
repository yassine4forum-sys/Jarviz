"""Real SessionChannel queues and isolated SQLite; no server/Agent fixtures."""
import json
import queue
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from api import background_process
from api.jarviz_tasks import TaskConflictError, TaskStore
from api.run_journal import SSE_RELAY_CLOSE_EVENTS


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Isolate the existing registry, not the channel implementation.
    monkeypatch.setattr(background_process, "SESSION_CHANNELS", {})
    return TaskStore(tmp_path)


@pytest.fixture
def subscribe(store):
    subscriptions = []

    def attach(sid, maxsize=16):
        channel, subscriber = background_process.subscribe_to_session_channel(sid, maxsize)
        subscriptions.append((channel, subscriber))
        return channel, subscriber

    yield attach
    for channel, subscriber in subscriptions:
        channel.unsubscribe(subscriber)


def create(store, sid="origin", **fields):
    return store.create_task(session_id=sid, project_id="shared-project",
                             title="Task", request="Do work", **fields)


def receive(subscriber):
    name, envelope = subscriber.get_nowait()
    assert name == "jarviz_task_event"
    assert name not in SSE_RELAY_CLOSE_EVENTS
    assert set(envelope) == {
        "schema_version", "event_type", "task_id", "project_id",
        "session_id", "created_at", "payload",
    }
    assert envelope["schema_version"] == 1
    return envelope


def test_create_and_update_match_committed_event(store, subscribe):
    _, subscriber = subscribe("origin")
    task = create(store)
    for expected_task in [task, store.update_task(task["task_id"], assigned_agent="specialist")]:
        envelope = receive(subscriber)
        durable = store.list_events(task["task_id"])[0 if expected_task == task else 1]
        assert envelope == {
            "schema_version": 1,
            "event_type": durable["event_type"],
            "task_id": durable["task_id"],
            "project_id": durable["project_id"],
            "session_id": durable["session_id"],
            "created_at": durable["created_at"],
            "payload": json.loads(durable["payload_json"]),
        }
        assert envelope["payload"] == expected_task
    assert subscriber.empty()


@pytest.mark.parametrize("status", ["running", "blocked", "awaiting_approval", "completed", "failed", "cancelled"])
def test_lifecycle_only_reaches_origin_and_all_its_tabs(store, subscribe, status):
    _, origin = subscribe("origin")
    _, second_tab = subscribe("origin")
    _, unrelated = subscribe("another-session")
    task = create(store, metadata_json='{"session_id":"another-session"}')
    receive(origin)
    receive(second_tab)
    task = store.update_task(task["task_id"], status=status,
                             result="private result" if status == "completed" else None,
                             error="private error" if status == "failed" else None)
    envelope = receive(origin)
    assert envelope == receive(second_tab)
    assert envelope["session_id"] == "origin"
    assert envelope["payload"] == task
    assert envelope["event_type"] == {
        "running": "task.started",
        "blocked": "task.blocked",
        "awaiting_approval": "approval.requested",
        "completed": "task.completed",
        "failed": "task.failed",
        "cancelled": "task.cancelled",
    }[status]
    assert unrelated.empty()


def test_emit_observes_commit_and_released_write_lock(store, subscribe, monkeypatch):
    channel, subscriber = subscribe("origin")
    original_emit = channel.emit

    def emit(name, data):
        # Independent connection sees the committed snapshot and can acquire a
        # write lock: notification must happen after COMMIT, not before it.
        with closing(sqlite3.connect(store.db_path, timeout=0)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_json FROM jarviz_events WHERE task_id = ? ORDER BY event_id DESC",
                               (data["task_id"],)).fetchone()
            assert json.loads(row[0]) == data["payload"]
            conn.rollback()
        return original_emit(name, data)

    monkeypatch.setattr(channel, "emit", emit)
    task = create(store)
    receive(subscriber)  # Also fails if a publisher swallowed the assertion.
    store.update_task(task["task_id"], status="completed", result="done")
    assert receive(subscriber)["payload"]["result"] == "done"


@pytest.mark.parametrize("operation", ["create", "update"])
def test_rollback_emits_nothing(store, subscribe, operation):
    _, subscriber = subscribe("origin")
    task = create(store)
    receive(subscriber)
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON jarviz_events
                        BEGIN SELECT RAISE(ABORT, 'event write failed'); END""")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        if operation == "create":
            create(store)
        else:
            store.update_task(task["task_id"], status="completed", result="never delivered")
    assert subscriber.empty()
    assert store.get_task(task["task_id"]) == task


def test_noop_and_rejected_origin_change_emit_nothing(store, subscribe):
    _, origin = subscribe("origin")
    _, unrelated = subscribe("another-session")
    task = create(store)
    receive(origin)
    store.update_task(task["task_id"], status="queued")
    with pytest.raises(ValueError):
        store.update_task(task["task_id"], session_id="another-session", result="secret")
    with pytest.raises(TaskConflictError):
        store.update_task(task["task_id"], expected_status="running", status="completed")
    assert origin.empty() and unrelated.empty()


def test_channel_failure_does_not_undo_commit(store, subscribe, monkeypatch):
    channel, _ = subscribe("origin")
    _, unrelated = subscribe("another-session")

    def fail(*args):
        raise RuntimeError("channel unavailable")

    monkeypatch.setattr(channel, "emit", fail)
    task = create(store)
    completed = store.update_task(task["task_id"], status="completed", result="durable")
    assert store.get_task(task["task_id"]) == completed
    assert len(store.list_events(task["task_id"])) == 2
    assert unrelated.empty()


def test_absent_origin_does_not_fall_back_to_another_session(store, subscribe):
    _, unrelated = subscribe("another-session")
    task = create(store)
    store.update_task(task["task_id"], status="failed", error="durable error")
    assert unrelated.empty()
    assert len(store.list_events(task["task_id"])) == 2


def test_mismatched_channel_owner_fails_closed(store, subscribe, monkeypatch):
    wrong_channel, unrelated = subscribe("another-session")
    monkeypatch.setitem(background_process.SESSION_CHANNELS, "origin", wrong_channel)
    task = create(store)
    store.update_task(task["task_id"], status="completed", result="private")
    assert unrelated.empty()
    assert store.get_task(task["task_id"])["result"] == "private"
    assert len(store.list_events(task["task_id"])) == 2


def test_full_queue_keeps_database_authoritative(store, subscribe):
    _, subscriber = subscribe("origin", maxsize=1)
    task = create(store)
    store.update_task(task["task_id"], status="completed", result="durable")
    assert receive(subscriber)["payload"]["status"] == "queued"
    assert subscriber.empty()
    assert store.get_task(task["task_id"])["result"] == "durable"
    assert len(store.list_events(task["task_id"])) == 2


def test_concurrent_sessions_in_same_project_remain_isolated(store, subscribe):
    queues = {sid: subscribe(sid)[1] for sid in ("origin", "another-session")}

    def work(sid):
        task = create(store, sid)
        store.update_task(task["task_id"], status="completed", result=f"private to {sid}")
        return task["task_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = dict(zip(queues, pool.map(work, queues), strict=True))
    for sid, subscriber in queues.items():
        events = [receive(subscriber), receive(subscriber)]
        assert all(e["session_id"] == sid and e["task_id"] == ids[sid] for e in events)
        assert events[-1]["payload"]["result"] == f"private to {sid}"
        with pytest.raises(queue.Empty):
            subscriber.get_nowait()
