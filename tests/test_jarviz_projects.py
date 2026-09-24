"""Existing project JSON + isolated JarViz SQLite; no new project registry."""
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


@pytest.fixture
def project_api(tmp_path, monkeypatch):
    from api import config, jarviz_projects, jarviz_routes, models, profiles, routes
    from api.jarviz_tasks import TaskStore

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    project_file = tmp_path / "projects.json"
    monkeypatch.setattr(models, "PROJECTS_FILE", project_file)
    monkeypatch.setattr(config, "PROJECTS_FILE", project_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *a, **kw: False)
    projects = [
        {"project_id": "project-a", "name": "Original", "profile": "alpha", "color": "#abcdef"},
        {"project_id": "project-b", "name": "Private", "profile": "beta"},
        {"project_id": "project-other", "name": "Other", "profile": "alpha"},
    ]
    models.save_projects(projects)
    sessions = {
        "session-a": SimpleNamespace(session_id="session-a", profile="alpha", project_id="project-a"),
        "session-b": SimpleNamespace(session_id="session-b", profile="beta", project_id="project-b"),
    }

    def get_session(sid, **kwargs):
        return sessions[sid]

    monkeypatch.setattr(jarviz_projects, "get_session", get_session)
    monkeypatch.setattr(jarviz_routes, "get_session", get_session)
    monkeypatch.setattr(routes, "get_session", get_session)
    profiles.set_request_profile("alpha")
    store = jarviz_projects.ProjectStore(tmp_path)

    def request(method="GET", path="/api/projects/project-a/jarviz", body=None, profile="alpha", origin="http://localhost"):
        profiles.set_request_profile(profile)
        raw = json.dumps(body).encode() if body is not None else b""
        handler = SimpleNamespace(
            command=method, path=path, client_address=("127.0.0.1", 12345),
            headers={"Content-Type": "application/json", "Content-Length": str(len(raw)),
                     "Host": "localhost", "Origin": origin},
            rfile=io.BytesIO(raw), wfile=io.BytesIO(), status=None,
            send_header=lambda *a: None, end_headers=lambda: None,
        )
        handler.send_response = lambda status: setattr(handler, "status", status)
        (routes.handle_get if method == "GET" else routes.handle_post)(handler, urlparse(path))
        return handler.status, json.loads(handler.wfile.getvalue())

    yield SimpleNamespace(store=store, request=request, file=project_file, projects=projects,
                          models=models, sessions=sessions, task_store=TaskStore(tmp_path), module=jarviz_projects)
    profiles.clear_request_profile()


def test_defaults_do_not_create_another_project_record(project_api):
    before = project_api.file.read_bytes()
    project = project_api.store.get_project("project-a")
    assert project["name"] == "Original"
    assert project["jarviz"] == {"root_workspace": None, "metadata": {}, "artifacts": [],
                                  "blockers": [], "decisions": [], "updated_at": None}
    assert "sessions" not in project and "tasks" not in project
    assert project_api.file.read_bytes() == before
    with closing(sqlite3.connect(project_api.store.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM jarviz_project_details").fetchone()[0] == 0


def test_round_trip_preserves_existing_identity_and_membership(project_api, tmp_path):
    from api.jarviz_projects import ProjectStore

    before = project_api.file.read_bytes()
    task = project_api.task_store.create_task(session_id="session-a", project_id="project-a", title="T", request="R")
    root = str(tmp_path / "not-created")
    project = project_api.store.update_project(
        "project-a", root_workspace=root, metadata={"description": "My project"},
        artifacts=[{"reference": "reports/result.md", "task_id": task["task_id"], "label": "Result"}],
        blockers=[{"text": "Waiting for input", "task_id": task["task_id"]}],
        decisions=[{"text": "Use SQLite", "reason": "Local persistence"}],
    )
    assert ProjectStore(tmp_path).get_project("project-a") == project
    assert project_api.file.read_bytes() == before
    assert not (tmp_path / "not-created").exists()
    assert project_api.sessions["session-a"].project_id == "project-a"
    assert project_api.task_store.list_tasks(project_id="project-a") == [task]
    assert project["jarviz"]["root_workspace"] == root


def test_partial_updates_and_clear_are_explicit(project_api):
    store = project_api.store
    store.update_project("project-a", root_workspace=r"C:\JarViz\workspace", metadata={"a": 1},
                         artifacts=[{"reference": "https://example.com/report"}])
    result = store.update_project("project-a", blockers=[{"text": "Need approval"}])
    assert result["jarviz"]["metadata"] == {"a": 1}
    assert len(result["jarviz"]["artifacts"]) == 1
    result = store.update_project("project-a", root_workspace=None, metadata={}, artifacts=[])
    assert result["jarviz"]["root_workspace"] is None
    assert result["jarviz"]["artifacts"] == []
    assert result["jarviz"]["blockers"] == [{"text": "Need approval"}]


@pytest.mark.parametrize("changes", [
    {}, {"name": "replacement"}, {"profile": "beta"}, {"sessions": []}, {"tasks": []},
    {"project_id": "new-project"}, {"root_workspace": "relative/path"},
    {"root_workspace": "C:relative"}, {"root_workspace": 123}, {"root_workspace": ""},
    {"metadata": []}, {"metadata": {"bad": float("nan")}}, {"artifacts": {}},
    {"artifacts": ["report.md"]}, {"artifacts": [{"reference": ""}]},
    {"blockers": [{}]}, {"decisions": [42]}, {"decisions": [{"text": "X", "task_id": None}]},
])
def test_invalid_changes_do_not_write(project_api, changes):
    with pytest.raises((ValueError, TypeError)):
        project_api.store.update_project("project-a", **changes)
    assert project_api.store.get_project("project-a")["jarviz"]["updated_at"] is None


def test_missing_foreign_and_deleted_projects_fail_closed(project_api):
    for pid in ("missing", "project-b"):
        with pytest.raises(KeyError):
            project_api.store.get_project(pid)
        with pytest.raises(KeyError):
            project_api.store.update_project(pid, metadata={"x": 1})
    project_api.store.update_project("project-a", metadata={"private": "data"})
    project_api.models.save_projects([p for p in project_api.projects if p["project_id"] != "project-a"])
    with pytest.raises(KeyError):
        project_api.store.get_project("project-a")
    with pytest.raises(KeyError):
        project_api.store.update_project("project-a", metadata={})


def test_task_references_use_existing_project_and_profile_ownership(project_api):
    other = project_api.task_store.create_task(session_id="session-a", project_id="project-other", title="T", request="R")
    hidden = project_api.task_store.create_task(session_id="session-b", project_id="project-a", title="T", request="R")
    for tid in (other["task_id"], hidden["task_id"], "missing"):
        with pytest.raises(KeyError):
            project_api.store.update_project("project-a", artifacts=[{"reference": "a.txt", "task_id": tid}])
    assert project_api.store.get_project("project-a")["jarviz"]["updated_at"] is None


def test_concurrent_partial_updates_do_not_lose_other_fields(project_api):
    from api import profiles

    def update(changes):
        profiles.set_request_profile("alpha")
        try:
            project_api.store.update_project("project-a", **changes)
        finally:
            profiles.clear_request_profile()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, [{"metadata": {"a": 1}}, {"decisions": [{"text": "Keep it local"}]}]))
    details = project_api.store.get_project("project-a")["jarviz"]
    assert details["metadata"] == {"a": 1}
    assert details["decisions"] == [{"text": "Keep it local"}]


def test_write_failure_preserves_previous_extension(project_api):
    before = project_api.store.update_project("project-a", metadata={"a": 1})
    with closing(sqlite3.connect(project_api.store.db_path)) as conn:
        conn.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON jarviz_project_details BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        project_api.store.update_project("project-a", metadata={"a": 2})
    assert project_api.store.get_project("project-a") == before


def test_schema_upgrade_preserves_tasks_and_events(project_api, tmp_path):
    from api.jarviz_projects import ProjectStore

    task = project_api.task_store.create_task(session_id="session-a", project_id="project-a", title="T", request="R")
    with closing(sqlite3.connect(project_api.store.db_path)) as conn:
        conn.execute("DROP TABLE jarviz_project_details")
        conn.execute("PRAGMA user_version = 1")
    upgraded = ProjectStore(tmp_path)
    assert upgraded.get_project("project-a")["jarviz"]["metadata"] == {}
    assert project_api.task_store.get_task(task["task_id"]) == task
    assert len(project_api.task_store.list_events(task["task_id"])) == 1
    with closing(sqlite3.connect(upgraded.db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_existing_project_routes_keep_extensions(project_api):
    status, _ = project_api.request("POST", "/api/projects/project-a/jarviz/update", {"metadata": {"a": 1}})
    assert status == 200
    status, _ = project_api.request("POST", "/api/projects/rename", {"project_id": "project-a", "name": "Renamed"})
    assert status == 200
    status, response = project_api.request()
    assert status == 200
    assert response["project"]["name"] == "Renamed"
    assert response["project"]["jarviz"]["metadata"] == {"a": 1}
    status, listed = project_api.request("GET", "/api/projects")
    assert status == 200
    assert all("jarviz" not in p for p in listed["projects"])


def test_api_ownership_csrf_and_secret_projection(project_api, monkeypatch):
    assert project_api.request(profile="beta")[0] == 404
    assert project_api.request("POST", "/api/projects/project-a/jarviz/update", {"metadata": {}}, profile="beta")[0] == 404
    assert project_api.request("POST", "/api/projects/project-a/jarviz/update", {"metadata": {}}, origin="https://evil.example")[0] == 403
    monkeypatch.setenv("JARVIZ_SECRET", "very-private-value")
    status, response = project_api.request("POST", "/api/projects/project-a/jarviz/update", {
        "metadata": {"api_key": "very-private-value"}, "decisions": [{"text": "token=very-private-value"}],
    })
    assert status == 200
    assert "very-private-value" not in json.dumps(response)
    assert "very-private-value" not in json.dumps(project_api.request()[1])
    assert project_api.store.get_project("project-a")["jarviz"]["metadata"]["api_key"] == "very-private-value"
