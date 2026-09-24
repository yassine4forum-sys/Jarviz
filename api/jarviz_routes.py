"""Profile-scoped JarViz task API, dispatched by the existing WebUI router."""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import parse_qs

from api.helpers import j
from api.jarviz_public import public_task
from api.jarviz_tasks import TASK_STATUSES, TaskConflictError, TaskStore
from api.models import get_session, is_safe_session_id, load_projects
from api.profiles import _profiles_match, get_active_profile_name

_BASE = "/api/jarviz/tasks"
_TASK_ID = re.compile(r"^[0-9a-f]{32}$")
_CREATE_FIELDS = frozenset({
    "session_id", "project_id", "parent_task_id", "title", "request",
    "assigned_agent", "task_type", "metadata_json", "status",
})
_UPDATE_FIELDS = frozenset({
    "title", "request", "status", "assigned_agent", "task_type",
    "result", "error", "metadata_json", "expected_status",
})


class _NotFound(Exception):
    pass


def _reply(handler, payload, status=200):
    j(handler, payload, status=status)
    return True


def _identifier(value):
    if not isinstance(value, str) or value in {".", ".."} or not is_safe_session_id(value):
        raise ValueError("invalid identifier")
    return value


class _Access:
    """Capture request profile once; resolve ownership from existing records."""

    def __init__(self):
        self.profile = get_active_profile_name()
        self.projects = {p["project_id"]: p for p in load_projects(_migrate=False)}

    def session(self, sid):
        _identifier(sid)
        try:
            session = get_session(sid, metadata_only=True)
        except KeyError:
            raise _NotFound from None
        owner = getattr(session, "profile", None)
        if owner is not None and not isinstance(owner, str):
            raise _NotFound
        if not _profiles_match(owner, self.profile):
            raise _NotFound
        return session

    def project(self, pid):
        if pid is None:
            return
        _identifier(pid)
        project = self.projects.get(pid)
        if project is None:
            raise _NotFound
        owner = project.get("profile")
        if owner is not None and not isinstance(owner, str):
            raise _NotFound
        if not _profiles_match(owner, self.profile):
            raise _NotFound

    def task(self, task):
        self.session(task["session_id"])
        self.project(task["project_id"])
        return task


def _task_id(path, *, action=None):
    suffix = path[len(_BASE) + 1:]
    if action:
        action_suffix = f"/{action}"
        if not suffix.endswith(action_suffix):
            raise _NotFound
        suffix = suffix[:-len(action_suffix)]
    if not _TASK_ID.fullmatch(suffix):
        raise _NotFound
    return suffix


def _handle_get(parsed):
    access = _Access()
    filters = parse_qs(parsed.query, keep_blank_values=True)
    if filters.keys() - {"session_id", "project_id", "status"}:
        raise ValueError("unknown filter")
    if any(len(values) != 1 or not values[0] for values in filters.values()):
        raise ValueError("invalid filter")
    filters = {key: values[0] for key, values in filters.items()}
    if "status" in filters and filters["status"] not in TASK_STATUSES:
        raise ValueError("invalid status")
    if "session_id" in filters:
        access.session(filters["session_id"])
    if "project_id" in filters:
        access.project(filters["project_id"])
    store = TaskStore()
    if parsed.path == _BASE:
        tasks = []
        for task in store.list_tasks(**filters):
            try:
                access.task(task)
            except (_NotFound, ValueError):
                continue  # Orphaned or foreign tasks must not leak in a list.
            tasks.append(public_task(task))
        return {"tasks": tasks}, 200
    if filters:
        raise ValueError("detail endpoint does not accept filters")
    return {"task": public_task(access.task(store.get_task(_task_id(parsed.path))))}, 200


def _handle_post(parsed, body):
    if parsed.query or not isinstance(body, dict):
        raise ValueError("invalid task request")
    access = _Access()
    store = TaskStore()
    if parsed.path == _BASE:
        if body.keys() - _CREATE_FIELDS or not {"session_id", "title", "request"} <= body.keys():
            raise ValueError("invalid create fields")
        if body.get("status", "queued") != "queued":
            raise ValueError("new tasks must be queued")
        session = access.session(body["session_id"])
        project_id = body.get("project_id", session.project_id)
        access.project(project_id)
        if project_id != session.project_id:
            raise ValueError("project must match the originating session")
        fields = dict(body, project_id=project_id)
        fields.pop("status", None)
        parent_id = fields.get("parent_task_id")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not _TASK_ID.fullmatch(parent_id):
                raise ValueError("invalid parent")
            access.task(store.get_task(parent_id))
        task = store.create_task(**fields)
        return {"ok": True, "task": public_task(task)}, 201
    if parsed.path.endswith("/run"):
        if set(body) != {"session_id", "project_id", "request"}:
            raise ValueError("invalid run fields")
        task_id = _task_id(parsed.path, action="run")
        access.task(store.get_task(task_id))
        from api.jarviz_orchestrator import orchestrate_task

        task = orchestrate_task(task_id=task_id, session_id=body["session_id"],
                                project_id=body["project_id"], request=body["request"])
        return {"ok": True, "task": public_task(task)}, 202 if task["status"] == "running" else 200
    task_id = _task_id(parsed.path, action="update")
    access.task(store.get_task(task_id))
    if body.keys() - _UPDATE_FIELDS or not (body.keys() - {"expected_status"}):
        raise ValueError("invalid update fields")
    task = store.update_task(task_id, **body)
    return {"ok": True, "task": public_task(task)}, 200


def _dispatch(handler, parsed, method, body=None):
    try:
        payload, status = _handle_get(parsed) if method == "GET" else _handle_post(parsed, body)
    except (_NotFound, KeyError):
        payload, status = {"error": "Task resource not found"}, 404
    except TaskConflictError:
        payload, status = {"error": "Task state conflict"}, 409
    except (ValueError, TypeError):
        payload, status = {"error": "Invalid task request"}, 400
    except sqlite3.Error:
        payload, status = {"error": "Task storage unavailable"}, 503
    except Exception:
        payload, status = {"error": "Task request failed"}, 500
    return _reply(handler, payload, status)


def handle_jarviz_get(handler, parsed):
    return _dispatch(handler, parsed, "GET")


def handle_jarviz_post(handler, parsed, body):
    return _dispatch(handler, parsed, "POST", body)
