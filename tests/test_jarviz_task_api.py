"""Exercise the real HTTP dispatcher with isolated storage and ownership fixtures.

Run via scripts/test.sh --noconftest with isolated HERMES_HOME/STATE_DIR and an
isolated HERMES_WEBUI_AGENT_DIR discovery sentinel (no Agent is executed).
"""
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


class Handler:
    def __init__(self, method, path, body, origin="http://localhost"):
        raw = json.dumps(body).encode() if body is not None else b""
        self.command = method
        self.path = path
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(raw)),
                        "Host": "localhost", "Origin": origin}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def api(tmp_path, monkeypatch):
    # Import after the runner's isolated env has been installed, never against
    # live home/Agent configuration. Fixtures replace only ownership lookups;
    # routing, CSRF, profile matching, serialization and SQLite are real.
    from api import background_process, config, jarviz_routes, profiles, routes
    from api.jarviz_tasks import TaskStore

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(background_process, "SESSION_CHANNELS", {})
    sessions = {
        "session-a": SimpleNamespace(session_id="session-a", profile="alpha", project_id="project-a"),
        "session-b": SimpleNamespace(session_id="session-b", profile="beta", project_id="project-b"),
        "session-free": SimpleNamespace(session_id="session-free", profile="alpha", project_id=None),
        "session-default": SimpleNamespace(session_id="session-default", profile=None, project_id=None),
    }
    projects = [{"project_id": "project-a", "profile": "alpha"},
                {"project_id": "project-b", "profile": "beta"},
                {"project_id": "project-other", "profile": "alpha"}]

    def get_session(sid, **kwargs):
        return sessions[sid]

    monkeypatch.setattr(jarviz_routes, "get_session", get_session)
    monkeypatch.setattr(jarviz_routes, "load_projects", lambda **kwargs: projects)
    monkeypatch.setattr(routes, "get_session", get_session)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *args, **kwargs: False)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    profiles.set_request_profile("alpha")

    def request(method, path="/api/jarviz/tasks", body=None, profile="alpha", **kwargs):
        profiles.set_request_profile(profile)
        handler = Handler(method, path, body, **kwargs)
        result = (routes.handle_get if method == "GET" else routes.handle_post)(handler, urlparse(path))
        assert result is not False  # Existing early errors may return j()'s None.
        assert handler.response_headers["Content-Type"].startswith("application/json")
        return handler.status, json.loads(handler.wfile.getvalue())

    yield SimpleNamespace(request=request, store=TaskStore(tmp_path), sessions=sessions,
                          projects=projects, module=jarviz_routes)
    profiles.clear_request_profile()


def create(api, **fields):
    body = dict(session_id="session-a", title="Research", request="Find answers")
    body.update(fields)
    return api.request("POST", body=body)


def test_create_get_filter_update_and_event(api):
    from api.background_process import subscribe_to_session_channel

    channel, subscriber = subscribe_to_session_channel("session-a")
    try:
        status, body = create(api)
        assert status == 201
        task = body["task"]
        assert task["project_id"] == "project-a"
        assert task["status"] == "queued"
        tid = task["task_id"]
        assert api.request("GET", f"/api/jarviz/tasks/{tid}") == (200, {"task": task})
        assert api.request("GET", "/api/jarviz/tasks?session_id=session-a&project_id=project-a&status=queued") == (200, {"tasks": [task]})
        status, body = api.request("POST", f"/api/jarviz/tasks/{tid}/update",
                                   {"status": "running", "expected_status": "queued"})
        assert status == 200 and body["task"]["started_at"] is not None
        status, body = api.request("POST", f"/api/jarviz/tasks/{tid}/update",
                                   {"status": "completed", "result": "Answer"})
        assert status == 200 and body["task"]["result"] == "Answer"
        assert body["task"]["completed_at"] is not None
        assert api.request("GET", "/api/jarviz/tasks?status=queued") == (200, {"tasks": []})
        frames = [subscriber.get_nowait() for _ in range(3)]
        assert all(name == "jarviz_task_event" and data["session_id"] == "session-a" for name, data in frames)
        assert len(api.store.list_events(tid)) == 3
    finally:
        channel.unsubscribe(subscriber)


def test_foreign_tasks_hidden_in_lists_detail_and_update(api):
    _, body = create(api)
    own = body["task"]
    foreign = api.store.create_task(session_id="session-b", project_id="project-b", title="Private", request="Secret request")
    orphan = api.store.create_task(session_id="deleted", title="Deleted", request="Hidden")
    for task in [foreign, orphan]:
        path = f'/api/jarviz/tasks/{task["task_id"]}'
        assert api.request("GET", path)[0] == 404
        assert api.request("POST", path + "/update", {"status": "running"})[0] == 404
    assert api.request("GET") == (200, {"tasks": [own]})
    assert api.request("GET", profile="beta") == (200, {"tasks": [foreign]})
    assert api.request("GET", f'/api/jarviz/tasks/{own["task_id"]}', profile="beta")[0] == 404


def test_concurrent_profile_requests_are_isolated(api):
    _, body = create(api)
    own = body["task"]
    foreign = api.store.create_task(session_id="session-b", project_id="project-b", title="Private", request="Work")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda profile: api.request("GET", profile=profile), ["alpha", "beta"]))
    assert responses == [(200, {"tasks": [own]}), (200, {"tasks": [foreign]})]


def test_deleted_project_hides_existing_task(api):
    _, body = create(api)
    tid = body["task"]["task_id"]
    api.projects[:] = [p for p in api.projects if p["project_id"] != "project-a"]
    assert api.request("GET") == (200, {"tasks": []})
    assert api.request("GET", f"/api/jarviz/tasks/{tid}")[0] == 404
    assert api.request("POST", f"/api/jarviz/tasks/{tid}/update", {"status": "running"})[0] == 404
    assert len(api.store.list_events(tid)) == 1


@pytest.mark.parametrize("query", ["session_id=session-b", "project_id=project-b", "session_id=missing", "project_id=missing"])
def test_invisible_filters_are_not_found(api, query):
    assert api.request("GET", "/api/jarviz/tasks?" + query)[0] == 404


@pytest.mark.parametrize("fields,status", [
    ({"session_id": "session-b"}, 404), ({"session_id": "missing"}, 404),
    ({"session_id": "../../private"}, 400), ({"session_id": []}, 400),
    ({"project_id": "project-b"}, 404), ({"project_id": "missing"}, 404),
    ({"project_id": "project-other"}, 400), ({"project_id": None}, 400),
    ({"profile": "beta"}, 400), ({"status": "running"}, 400),
    ({"status": "invalid"}, 400), ({"title": ""}, 400),
    ({"metadata_json": "[]"}, 400),
])
def test_create_validation_and_ownership(api, fields, status):
    assert create(api, **fields)[0] == status
    assert api.store.list_tasks() == []


def test_unassigned_session_and_legacy_root_profile(api):
    status, body = create(api, session_id="session-free")
    assert status == 201 and body["task"]["project_id"] is None
    status, body = api.request("POST", body={"session_id": "session-default", "title": "Root", "request": "Work"}, profile="default")
    assert status == 201
    assert api.request("GET", f'/api/jarviz/tasks/{body["task"]["task_id"]}')[0] == 404


def test_invisible_project_blocks_even_visible_session(api):
    task = api.store.create_task(session_id="session-a", project_id="project-b", title="Task", request="Private")
    path = f'/api/jarviz/tasks/{task["task_id"]}'
    assert api.request("GET", path)[0] == 404
    assert api.request("GET") == (200, {"tasks": []})
    assert api.request("POST", path + "/update", {"status": "running"})[0] == 404
    assert len(api.store.list_events(task["task_id"])) == 1


def test_parent_visibility_and_membership(api):
    _, body = create(api)
    parent = body["task"]["task_id"]
    assert create(api, parent_task_id=parent)[0] == 201
    foreign = api.store.create_task(session_id="session-b", project_id="project-b", title="Task", request="Private")
    assert create(api, parent_task_id=foreign["task_id"])[0] == 404
    assert create(api, parent_task_id="0" * 32)[0] == 404
    assert create(api, session_id="session-free", parent_task_id=parent)[0] == 400


@pytest.mark.parametrize("query", ["status=unknown", "status=", "status=queued&status=running", "profile=beta", "all_profiles=1", "session_id=../secret"])
def test_invalid_filters(api, query):
    assert api.request("GET", "/api/jarviz/tasks?" + query)[0] == 400


@pytest.mark.parametrize("changes", [{"session_id": "session-b"}, {"project_id": "project-b"},
                                     {"status": "invalid"}, {"status": []}, {"profile": "beta"}, {},
                                     {"expected_status": "queued"}, {"result": {"not": "text"}}])
def test_update_validation_does_not_mutate(api, changes):
    _, body = create(api)
    task = body["task"]
    assert api.request("POST", f'/api/jarviz/tasks/{task["task_id"]}/update', changes)[0] == 400
    assert api.store.get_task(task["task_id"]) == task
    assert len(api.store.list_events(task["task_id"])) == 1


def test_conflicts_and_unknown_endpoints(api):
    _, body = create(api)
    path = f'/api/jarviz/tasks/{body["task"]["task_id"]}/update'
    assert api.request("POST", path, {"status": "running", "expected_status": "blocked"})[0] == 409
    assert api.request("POST", path, {"status": "cancelled"})[0] == 200
    assert api.request("POST", path, {"status": "running"})[0] == 409
    assert api.request("GET", "/api/jarviz/tasks/not-an-id")[0] == 404
    assert api.request("POST", "/api/jarviz/tasks/unknown", {})[0] == 404


def test_run_endpoint_preserves_origin_and_dispatches(api, monkeypatch):
    from api import jarviz_orchestrator

    _, body = create(api, request="Implement the parser")
    task = body["task"]
    captured = {}

    def orchestrate_task(**kwargs):
        captured.update(kwargs)
        return api.store.update_task(task["task_id"], expected_status="queued",
                                     status="running", task_type="coding")

    monkeypatch.setattr(jarviz_orchestrator, "orchestrate_task", orchestrate_task)
    run_body = {"session_id": "session-a", "project_id": "project-a",
                "request": "Implement the parser"}
    status, response = api.request("POST", f'/api/jarviz/tasks/{task["task_id"]}/run', run_body)
    assert status == 202 and response["task"]["status"] == "running"
    assert captured == dict(run_body, task_id=task["task_id"])


@pytest.mark.parametrize("body", [
    {}, {"session_id": "session-a", "project_id": "project-a"},
    {"session_id": "session-a", "project_id": "project-a", "request": "Work", "extra": True},
])
def test_run_endpoint_requires_exact_origin_fields(api, body):
    _, created = create(api)
    tid = created["task"]["task_id"]
    assert api.request("POST", f"/api/jarviz/tasks/{tid}/run", body)[0] == 400
    assert api.store.get_task(tid)["status"] == "queued"


def test_cross_origin_post_still_rejected_by_existing_csrf(api):
    status, _ = api.request("POST", body={"session_id": "session-a", "title": "T", "request": "R"}, origin="https://evil.example")
    assert status == 403
    assert api.store.list_tasks() == []


@pytest.mark.parametrize("exception,status", [(sqlite3.OperationalError, 503), (RuntimeError, 500)])
def test_internal_errors_are_fixed_messages(api, monkeypatch, exception, status):
    private = "Traceback (most recent call last): C:/private/.env GROQ_API_KEY=gsk_private_key_123456789"

    def fail():
        raise exception(private)

    monkeypatch.setattr(api.module, "TaskStore", fail)
    for method, body in [("GET", None), ("POST", {"session_id": "session-a", "title": "T", "request": "R"})]:
        actual, response = api.request(method, body=body)
        assert actual == status
        assert "private" not in json.dumps(response)
        assert "Traceback" not in json.dumps(response)


def test_responses_redact_secrets_metadata_and_tracebacks(api, monkeypatch):
    from api import config
    from api.background_process import subscribe_to_session_channel

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setenv("JARVIZ_TEST_SECRET", "private-environment-value-123")
    key = "gsk_abcdefghijklmnopqrstuvwxyz123456789"
    trace = 'Traceback (most recent call last):\n  File "C:/private/worker.py", line 42\nSecret failure'
    metadata = json.dumps({"nested": {"api_key": "unrecognized-secret", "env": {"VALUE": "private"}}, "traceback": trace})
    channel, subscriber = subscribe_to_session_channel("session-a")
    # Fixture registry isolation also removes the channel if an assertion fails.
    status, body = create(api, title=key, request="private-environment-value-123", metadata_json=metadata)
    assert status == 201
    tid = body["task"]["task_id"]
    status, updated = api.request("POST", f"/api/jarviz/tasks/{tid}/update", {"status": "failed", "error": trace, "result": key})
    assert status == 200
    responses = [body, updated, api.request("GET", f"/api/jarviz/tasks/{tid}")[1], api.request("GET")[1]]
    responses.extend(subscriber.get_nowait()[1] for _ in range(2))
    channel.unsubscribe(subscriber)
    for response in responses:
        text = json.dumps(response)
        for secret in [key, "private-environment-value-123", "unrecognized-secret", "C:/private/worker.py"]:
            assert secret not in text
    assert api.store.get_task(tid)["error"] == trace  # Public projection only.
