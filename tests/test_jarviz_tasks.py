"""Isolated SQLite tests; run with --noconftest (no server or Agent needed)."""
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from api.jarviz_tasks import TASK_STATUSES, TaskConflictError, TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path)


def create(store, **kwargs):
    fields = dict(session_id="origin", project_id="project", title="Research", request="Find answers")
    fields.update(kwargs)
    return store.create_task(**fields)


def test_configured_path_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "api.config", SimpleNamespace(STATE_DIR=tmp_path))
    store = TaskStore()
    task = create(store, assigned_agent="researcher", task_type="research", metadata_json='{"tag":"é"}')
    assert store.db_path == tmp_path / "jarviz" / "jarviz.db"
    reopened = TaskStore(tmp_path)
    assert reopened.get_task(task["task_id"]) == task
    assert set(task) == {
        "task_id", "project_id", "session_id", "root_session_id", "parent_task_id",
        "hierarchy_depth", "title", "request",
        "status", "assigned_agent", "task_type", "result", "error", "created_at",
        "updated_at", "started_at", "completed_at", "metadata_json",
    }
    event, = reopened.list_events(task["task_id"])
    assert event["event_type"] == "task.created"
    assert event["session_id"] == "origin"
    assert event["previous_status"] is None
    assert json.loads(event["payload_json"]) == task


def test_committed_task_survives_process_exit(store, tmp_path):
    task = create(store)
    code = (
        "import sys; from api.jarviz_tasks import TaskStore; "
        "TaskStore(sys.argv[1]).update_task(sys.argv[2], status='completed', result='persisted')"
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path), task["task_id"]],
                   check=True, timeout=15)
    recovered = store.get_task(task["task_id"])
    assert recovered["result"] == "persisted"
    assert recovered["status"] == "completed"
    assert json.loads(store.list_events(task["task_id"])[-1]["payload_json"]) == recovered


@pytest.mark.parametrize("fields", [
    {"session_id": ""}, {"session_id": None}, {"project_id": ""},
    {"title": ""}, {"request": None}, {"metadata_json": "null"},
])
def test_invalid_creation_writes_nothing(store, fields):
    with pytest.raises(ValueError):
        create(store, **fields)
    assert store.list_tasks() == []
    with closing(sqlite3.connect(store.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jarviz_events").fetchone()[0] == 0


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_lifecycle_and_terminal_timestamps(store, terminal):
    task = create(store)
    tid = task["task_id"]
    assert task["status"] == "queued"
    assert task["started_at"] is task["completed_at"] is None
    started = store.update_task(tid, status="running")["started_at"]
    for status in ["blocked", "awaiting_approval", "running"]:
        task = store.update_task(tid, status=status)
        assert task["started_at"] == started
        assert task["completed_at"] is None
    task = store.update_task(tid, status=terminal, result="answer" if terminal == "completed" else None,
                             error="failure" if terminal == "failed" else None)
    assert task["created_at"] <= started <= task["completed_at"] == task["updated_at"]
    assert json.loads(store.list_events(tid)[-1]["payload_json"]) == task
    with pytest.raises(TaskConflictError):
        store.update_task(tid, status="running")
    assert store.update_task(tid, status=terminal) == task  # idempotent no-op
    assert len(store.list_events(tid)) == 6


@pytest.mark.parametrize("status", sorted(TASK_STATUSES))
def test_supported_statuses(store, status):
    tid = create(store)["task_id"]
    assert store.update_task(tid, status=status)["status"] == status


def test_updates_filters_parent_and_cursor(store):
    parent = create(store)
    child = create(store, parent_task_id=parent["task_id"])
    other = create(store, session_id="another", project_id="other")
    tid = child["task_id"]
    task = store.update_task(tid, title="New title", assigned_agent="specialist", metadata_json='{"run_id":"r1"}')
    events = store.list_events(tid)
    assert events[-1]["event_type"] == "task.updated"
    assert json.loads(events[-1]["payload_json"]) == task
    assert store.list_events(tid, after_event_id=events[0]["event_id"]) == events[1:]
    assert {t["task_id"] for t in store.list_tasks(session_id="origin", status="queued")} == {parent["task_id"], tid}
    assert store.list_tasks(project_id="other") == [other]
    with pytest.raises(ValueError, match="same session"):
        create(store, session_id="wrong", parent_task_id=tid)
    with pytest.raises(ValueError, match="same session"):
        create(store, project_id="wrong", parent_task_id=tid)
    with pytest.raises(KeyError):
        create(store, parent_task_id="missing")
    assert len(store.list_tasks()) == 3


def test_root_session_and_maximum_hierarchy_depth(store):
    root = create(store)
    child = create(store, parent_task_id=root["task_id"])
    grandchild = create(store, parent_task_id=child["task_id"])
    assert [(task["root_session_id"], task["hierarchy_depth"])
            for task in (root, child, grandchild)] == [
                ("origin", 1), ("origin", 2), ("origin", 3)]
    with pytest.raises(ValueError, match="maximum"):
        create(store, parent_task_id=grandchild["task_id"])
    assert store.list_children(root["task_id"]) == [child]


@pytest.mark.parametrize("changes", [
    {"status": "unknown"}, {"status": []}, {"session_id": "wrong"},
    {"project_id": "wrong"}, {"parent_task_id": "wrong"}, {"task_id": "wrong"},
    {"created_at": 0}, {"metadata_json": "[]"}, {"metadata_json": "{"},
    {"metadata_json": '{"bad":NaN}'}, {"metadata_json": {}},
    {"title": " "}, {"request": None}, {"result": {}}, {"error": 12},
])
def test_invalid_mutation_leaves_no_event(store, changes):
    task = create(store)
    with pytest.raises(ValueError):
        store.update_task(task["task_id"], **changes)
    assert store.get_task(task["task_id"]) == task
    assert len(store.list_events(task["task_id"])) == 1


def test_missing_tasks(store):
    for operation in [store.get_task, store.update_task, store.list_events]:
        with pytest.raises(KeyError):
            operation("missing")


@pytest.mark.parametrize("operation", ["create", "update"])
def test_event_insert_failure_rolls_back_task(store, operation):
    task = create(store)
    # Real SQLite failure after the task write, not a mock of transactions.
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON jarviz_events
                        BEGIN SELECT RAISE(ABORT, 'event write failed'); END""")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="event write failed"):
        if operation == "create":
            create(store)
        else:
            store.update_task(task["task_id"], status="completed", result="answer")
    assert store.list_tasks() == [task]
    assert len(store.list_events(task["task_id"])) == 1


def test_competing_claims_commit_once(store, tmp_path):
    tid = create(store)["task_id"]
    stores = [TaskStore(tmp_path), TaskStore(tmp_path)]

    def claim(worker):
        try:
            worker.update_task(tid, expected_status="queued", status="running")
            return True
        except TaskConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, stores)) == [False, True]
    assert store.get_task(tid)["status"] == "running"
    assert len(store.list_events(tid)) == 2


def test_newer_schema_is_not_downgraded(store, tmp_path):
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema version"):
        TaskStore(tmp_path)
    with closing(sqlite3.connect(store.db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
