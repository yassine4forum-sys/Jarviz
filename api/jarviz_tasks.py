"""Durable JarViz task state; independent of Hermes execution and run journals."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

TASK_STATUSES = frozenset({
    "queued", "running", "blocked", "awaiting_approval",
    "completed", "failed", "cancelled",
})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
LIFECYCLE_EVENT_TYPES = {
    "queued": "task.queued",
    "running": "task.started",
    "blocked": "task.blocked",
    "awaiting_approval": "approval.requested",
    "completed": "task.completed",
    "failed": "task.failed",
    "cancelled": "task.cancelled",
}
_MUTABLE_FIELDS = frozenset({
    "title", "request", "status", "assigned_agent", "task_type",
    "result", "error", "metadata_json",
})


class TaskConflictError(ValueError):
    """A stale writer or a terminal task cannot accept this mutation."""


def _text(value, field, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_changes(changes):
    for field, value in changes.items():
        if field == "status":
            if not isinstance(value, str) or value not in TASK_STATUSES:
                raise ValueError("invalid task status")
        elif field == "metadata_json":
            if not isinstance(value, str):
                raise ValueError("metadata_json must be a JSON object string")
            try:
                parsed = json.loads(value, parse_constant=_reject_json_constant)
            except (ValueError, TypeError) as exc:
                raise ValueError("metadata_json must be a JSON object string") from exc
            if not isinstance(parsed, dict):
                raise ValueError("metadata_json must be a JSON object string")
        elif field in {"result", "error"}:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or None")
        else:
            _text(value, field, optional=field in {"assigned_agent", "task_type"})


class TaskStore:
    """One connection per operation, serialized writers, explicit atomic commits.

    Default location: api.config.STATE_DIR / jarviz / jarviz.db. Pass an
    isolated state_dir for tests. Construction initializes schema only; nothing
    imports or mutates Hermes Agent state. This is an internal persistence API,
    not an authorization boundary; future routes must enforce session access.
    """

    def __init__(self, state_dir: Path | str | None = None):
        if state_dir is None:
            from api.config import STATE_DIR

            state_dir = STATE_DIR
        self.db_path = Path(state_dir) / "jarviz" / "jarviz.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise RuntimeError(f"Unsupported JarViz schema version: {version}")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jarviz_tasks (
                    task_id TEXT PRIMARY KEY NOT NULL,
                    project_id TEXT,
                    session_id TEXT NOT NULL,
                    root_session_id TEXT NOT NULL,
                    parent_task_id TEXT REFERENCES jarviz_tasks(task_id),
                    hierarchy_depth INTEGER NOT NULL CHECK (hierarchy_depth BETWEEN 1 AND 3),
                    title TEXT NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'queued', 'running', 'blocked', 'awaiting_approval',
                        'completed', 'failed', 'cancelled')),
                    assigned_agent TEXT,
                    task_type TEXT,
                    result TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jarviz_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES jarviz_tasks(task_id),
                    project_id TEXT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    previous_status TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS jarviz_tasks_session ON jarviz_tasks(session_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS jarviz_tasks_project ON jarviz_tasks(project_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS jarviz_tasks_parent ON jarviz_tasks(parent_task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS jarviz_events_task ON jarviz_events(task_id, event_id)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jarviz_tasks)")}
            if "root_session_id" not in columns:
                conn.execute("ALTER TABLE jarviz_tasks ADD COLUMN root_session_id TEXT")
            if "hierarchy_depth" not in columns:
                conn.execute("ALTER TABLE jarviz_tasks ADD COLUMN hierarchy_depth INTEGER")
            rows = {row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT task_id, parent_task_id, session_id FROM jarviz_tasks"
            )}
            ancestry = {}
            while len(ancestry) < len(rows):
                progressed = False
                for task_id, (parent_id, session_id) in rows.items():
                    if task_id in ancestry:
                        continue
                    if parent_id is None:
                        ancestry[task_id] = (session_id, 1)
                        progressed = True
                    elif parent_id in ancestry:
                        root_session_id, parent_depth = ancestry[parent_id]
                        ancestry[task_id] = (root_session_id, parent_depth + 1)
                        progressed = True
                if not progressed:
                    raise RuntimeError("Invalid JarViz task hierarchy")
            for task_id, (root_session_id, depth) in ancestry.items():
                if depth > 3:
                    raise RuntimeError("JarViz task hierarchy exceeds maximum depth 3")
                conn.execute(
                    "UPDATE jarviz_tasks SET root_session_id = ?, hierarchy_depth = ? "
                    "WHERE task_id = ? AND (root_session_id IS NULL OR hierarchy_depth IS NULL)",
                    (root_session_id, depth, task_id),
                )
            # Only extension data lives here. Hermes projects.json remains the
            # authority for project identity, ownership and session membership.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jarviz_project_details (
                    project_id TEXT PRIMARY KEY NOT NULL,
                    root_workspace TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    blockers_json TEXT NOT NULL DEFAULT '[]',
                    decisions_json TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jarviz_personas (
                    profile_id TEXT PRIMARY KEY NOT NULL,
                    name TEXT NOT NULL,
                    tone TEXT NOT NULL,
                    verbosity TEXT NOT NULL,
                    languages_json TEXT NOT NULL,
                    voice TEXT NOT NULL,
                    announce_task_start INTEGER NOT NULL CHECK (announce_task_start IN (0, 1)),
                    announce_task_completion INTEGER NOT NULL CHECK (announce_task_completion IN (0, 1)),
                    announce_blockers INTEGER NOT NULL CHECK (announce_blockers IN (0, 1)),
                    speak_technical_logs INTEGER NOT NULL CHECK (speak_technical_logs IN (0, 1)),
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("PRAGMA user_version = 4")

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.db_path, timeout=10, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = FULL")
            yield conn

    @contextmanager
    def _transaction(self):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _get(conn, task_id):
        row = conn.execute("SELECT * FROM jarviz_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    @staticmethod
    def _event(conn, task, event_type, previous_status):
        cursor = conn.execute("""
            INSERT INTO jarviz_events
                (task_id, project_id, session_id, event_type, previous_status,
                 status, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (task["task_id"], task["project_id"], task["session_id"], event_type,
              previous_status, task["status"], task["updated_at"],
              json.dumps(task, ensure_ascii=False, allow_nan=False)))
        return dict(conn.execute(
            "SELECT * FROM jarviz_events WHERE event_id = ?", (cursor.lastrowid,)
        ).fetchone())

    @staticmethod
    def _publish_committed_event(event):
        """Best-effort notification through the existing, origin-owned channel.

        Call only after transaction exit. SQLite is authoritative even when no
        subscriber exists, the queue is full, or live delivery fails. Never use
        active-session state or metadata to choose a fallback destination.
        """
        try:
            from api.background_process import get_session_channel

            session_id = event["session_id"]
            channel = get_session_channel(session_id)
            if channel is None:
                return
            # Fail closed if a registry entry ever disagrees with its owner.
            if channel.session_id != session_id:
                raise ValueError("JarViz channel owner mismatch")
            from api.jarviz_public import public_task

            channel.emit("jarviz_task_event", {
                "schema_version": 1,
                "event_type": event["event_type"],
                "task_id": event["task_id"],
                "project_id": event["project_id"],
                "session_id": session_id,
                "created_at": event["created_at"],
                "payload": public_task(json.loads(event["payload_json"])),
            })
        except Exception:
            # A delivery error must not make callers retry a committed mutation.
            # Do not log the task result, request, or error contents.
            logger.warning("JarViz live delivery failed for committed event %s", event["event_id"])

    def create_task(self, *, session_id: str, title: str, request: str,
                    project_id: str | None = None, parent_task_id: str | None = None,
                    assigned_agent: str | None = None, task_type: str | None = None,
                    metadata_json: str = "{}") -> dict:
        """Create a queued task and task.created event in the same transaction."""
        _text(session_id, "session_id")
        _text(project_id, "project_id", optional=True)
        _text(parent_task_id, "parent_task_id", optional=True)
        values = dict(title=title, request=request, assigned_agent=assigned_agent,
                      task_type=task_type, metadata_json=metadata_json)
        _validate_changes(values)
        with self._transaction() as conn:
            root_session_id = session_id
            hierarchy_depth = 1
            if parent_task_id is not None:
                parent = self._get(conn, parent_task_id)
                if (parent["session_id"], parent["project_id"]) != (session_id, project_id):
                    raise ValueError("parent task must belong to the same session and project")
                root_session_id = parent.get("root_session_id") or parent["session_id"]
                hierarchy_depth = (parent.get("hierarchy_depth") or 1) + 1
                if hierarchy_depth > 3:
                    raise ValueError("maximum task hierarchy depth is 3")
            now = time.time()
            task = dict(task_id=uuid.uuid4().hex, project_id=project_id,
                        session_id=session_id, root_session_id=root_session_id,
                        parent_task_id=parent_task_id, hierarchy_depth=hierarchy_depth,
                        **values, status="queued", result=None, error=None,
                        created_at=now, updated_at=now, started_at=None, completed_at=None)
            columns = ", ".join(task)
            placeholders = ", ".join("?" for _ in task)
            conn.execute(f"INSERT INTO jarviz_tasks ({columns}) VALUES ({placeholders})", tuple(task.values()))
            event = self._event(conn, task, "task.created", None)
        self._publish_committed_event(event)
        return task

    def get_task(self, task_id: str) -> dict:
        with self._connection() as conn:
            return self._get(conn, task_id)

    def list_tasks(self, *, session_id: str | None = None,
                   project_id: str | None = None, status: str | None = None) -> list[dict]:
        """List tasks; None means no filter. Internal callers must scope access."""
        filters = {key: value for key, value in
                   dict(session_id=session_id, project_id=project_id, status=status).items()
                   if value is not None}
        if status is not None:
            _validate_changes({"status": status})
        where = " AND ".join(f"{key} = ?" for key in filters) or "1=1"
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                f"SELECT * FROM jarviz_tasks WHERE {where} ORDER BY created_at, task_id",
                tuple(filters.values()))]

    def list_children(self, parent_task_id: str) -> list[dict]:
        with self._connection() as conn:
            self._get(conn, parent_task_id)
            return [dict(row) for row in conn.execute(
                "SELECT * FROM jarviz_tasks WHERE parent_task_id = ? ORDER BY created_at, task_id",
                (parent_task_id,),
            )]

    def update_task(self, task_id: str, /, *, expected_status: str | None = None, **changes) -> dict:
        """Atomically update a task and snapshot event; terminal tasks are final.

        Nonterminal statuses may move to any supported status. Retrying terminal
        work requires a new task. expected_status supports compare-and-set claims.
        Identity/origin and timestamps cannot be changed by callers.
        """
        if changes.keys() - _MUTABLE_FIELDS:
            raise ValueError("unknown or immutable task fields")
        _validate_changes(changes)
        if expected_status is not None:
            _validate_changes({"status": expected_status})
        with self._transaction() as conn:
            task = self._get(conn, task_id)
            old_status = task["status"]
            if expected_status is not None and old_status != expected_status:
                raise TaskConflictError("task status changed")
            changes = {key: value for key, value in changes.items() if task[key] != value}
            if not changes:
                return task
            if old_status in TERMINAL_STATUSES:
                raise TaskConflictError("terminal tasks cannot be changed")
            task.update(changes)
            task["updated_at"] = max(time.time(), task["updated_at"])
            if task["status"] == "running" and task["started_at"] is None:
                task["started_at"] = task["updated_at"]
            if task["status"] in TERMINAL_STATUSES:
                task["completed_at"] = task["updated_at"]
            fields = [*_MUTABLE_FIELDS, "updated_at", "started_at", "completed_at"]
            conn.execute(
                "UPDATE jarviz_tasks SET " + ", ".join(f"{key} = ?" for key in fields) + " WHERE task_id = ?",
                (*[task[key] for key in fields], task_id),
            )
            event_type = (LIFECYCLE_EVENT_TYPES[task["status"]]
                          if task["status"] != old_status else "task.updated")
            event = self._event(conn, task, event_type, old_status)
        self._publish_committed_event(event)
        return task

    def list_events(self, task_id: str, *, after_event_id: int = 0) -> list[dict]:
        """Read committed task history in durable cursor order."""
        with self._connection() as conn:
            self._get(conn, task_id)
            return [dict(row) for row in conn.execute(
                "SELECT * FROM jarviz_events WHERE task_id = ? AND event_id > ? ORDER BY event_id",
                (task_id, after_event_id))]
