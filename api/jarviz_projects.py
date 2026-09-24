"""Small JarViz extension of existing Hermes project identities, not a registry."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path, PureWindowsPath

from api.helpers import j
from api.jarviz_public import public_value
from api.jarviz_tasks import TaskStore
from api.models import get_session, is_safe_session_id, load_projects
from api.profiles import _profiles_match, get_active_profile_name

_FIELDS = frozenset({"root_workspace", "metadata", "artifacts", "blockers", "decisions"})
_JSON_FIELDS = ("metadata", "artifacts", "blockers", "decisions")
_ROUTE = re.compile(r"^/api/projects/([A-Za-z0-9_.-]+)/jarviz(?P<update>/update)?$")


def _owned_project(project_id, profile):
    if project_id in (".", "..") or not is_safe_session_id(project_id):
        raise ValueError("invalid project id")
    project = next((p for p in load_projects(_migrate=False) if p.get("project_id") == project_id), None)
    if project is None:
        raise KeyError(project_id)
    owner = project.get("profile")
    if (owner is not None and not isinstance(owner, str)) or not _profiles_match(owner, profile):
        raise KeyError(project_id)
    return project


def _validate(changes):
    if not isinstance(changes, dict) or not changes or changes.keys() - _FIELDS:
        raise ValueError("invalid project fields")
    # Detach the caller's objects before locking and reject non-JSON values/NaN.
    changes = json.loads(json.dumps(changes, allow_nan=False))
    if "root_workspace" in changes:
        root = changes["root_workspace"]
        if root is not None and (
            not isinstance(root, str) or not root.strip() or any(c in root for c in "\x00\r\n")
            or not (Path(root).is_absolute() or PureWindowsPath(root).is_absolute())
        ):
            raise ValueError("root workspace must be an absolute path or null")
    if "metadata" in changes and not isinstance(changes["metadata"], dict):
        raise ValueError("metadata must be an object")
    for field in ("artifacts", "blockers", "decisions"):
        if field not in changes:
            continue
        entries = changes[field]
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("project references must be arrays of objects")
        for entry in entries:
            required = "reference" if field == "artifacts" else "text"
            if not isinstance(entry.get(required), str) or not entry[required].strip():
                raise ValueError("project reference text required")
            if "task_id" in entry and (not isinstance(entry["task_id"], str) or not entry["task_id"]):
                raise ValueError("invalid task reference")
    return changes


class ProjectStore:
    """Only read/update extensions for visible, existing Hermes projects.

    Uses TaskStore's existing SQLite connection/transaction implementation and
    schema initialization. No project IDs, profiles or memberships are created.
    """

    def __init__(self, state_dir=None):
        self._db = TaskStore(state_dir)
        self.db_path = self._db.db_path

    @staticmethod
    def _details(conn, project_id):
        row = conn.execute("SELECT * FROM jarviz_project_details WHERE project_id = ?", (project_id,)).fetchone()
        if row is None:
            return dict(root_workspace=None, metadata={}, artifacts=[], blockers=[], decisions=[], updated_at=None)
        return {"root_workspace": row["root_workspace"], "updated_at": row["updated_at"],
                **{key: json.loads(row[key + "_json"]) for key in _JSON_FIELDS}}

    def get_project(self, project_id):
        project = _owned_project(project_id, get_active_profile_name())
        with self._db._connection() as conn:
            return {**project, "jarviz": self._details(conn, project_id)}

    def update_project(self, project_id, **changes):
        profile = get_active_profile_name()
        changes = _validate(changes)
        with self._db._transaction() as conn:
            project = _owned_project(project_id, profile)
            # Optional links refer to the existing task association, never add a
            # second task membership list. Read under the write transaction.
            for field in ("artifacts", "blockers", "decisions"):
                for entry in changes.get(field, []):
                    if "task_id" not in entry:
                        continue
                    task = self._db._get(conn, entry["task_id"])
                    if task["project_id"] != project_id:
                        raise KeyError(entry["task_id"])
                    session = get_session(task["session_id"], metadata_only=True)
                    owner = getattr(session, "profile", None)
                    if (owner is not None and not isinstance(owner, str)) or not _profiles_match(owner, profile):
                        raise KeyError(entry["task_id"])
            details = self._details(conn, project_id)
            details.update(changes)
            details["updated_at"] = max(time.time(), details["updated_at"] or 0)
            conn.execute("""
                INSERT INTO jarviz_project_details
                    (project_id, root_workspace, metadata_json, artifacts_json,
                     blockers_json, decisions_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    root_workspace=excluded.root_workspace,
                    metadata_json=excluded.metadata_json, artifacts_json=excluded.artifacts_json,
                    blockers_json=excluded.blockers_json, decisions_json=excluded.decisions_json,
                    updated_at=excluded.updated_at
            """, (project_id, details["root_workspace"],
                  *(json.dumps(details[key], ensure_ascii=False, allow_nan=False) for key in _JSON_FIELDS),
                  details["updated_at"]))
        return {**project, "jarviz": details}


def handle_project_extension(handler, parsed, *, method, body=None):
    """Existing project namespace; normal WebUI authentication/CSRF still apply."""
    match = _ROUTE.fullmatch(parsed.path)
    if match is None:
        return False
    try:
        if parsed.query:
            raise ValueError("query parameters unsupported")
        project_id = match[1]
        if method == "GET" and not match["update"]:
            project = ProjectStore().get_project(project_id)
        elif method == "POST" and match["update"]:
            if not isinstance(body, dict):
                raise ValueError("object required")
            project = ProjectStore().update_project(project_id, **body)
        else:
            j(handler, {"error": "Method not allowed"}, status=405)
            return True
        # Existing identity fields stay authoritative, content gets the same
        # forced credential/traceback filtering as task HTTP/SSE responses.
        project["jarviz"] = public_value(project["jarviz"])
        if "name" in project:
            project["name"] = public_value(project["name"])
        payload, status = {"project": project}, 200
    except KeyError:
        payload, status = {"error": "Project resource not found"}, 404
    except (ValueError, TypeError):
        payload, status = {"error": "Invalid project extension"}, 400
    except sqlite3.Error:
        payload, status = {"error": "Project storage unavailable"}, 503
    except Exception:
        payload, status = {"error": "Project request failed"}, 500
    j(handler, payload, status=status)
    return True
