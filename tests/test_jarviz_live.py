"""Focused contracts for native Gemini Live and its narrow JarViz controls."""
import io
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


class Handler:
    def __init__(self, path, body, method="POST"):
        raw = json.dumps(body).encode() if body is not None else b""
        self.command = method
        self.path = path
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(raw)),
                        "Host": "localhost", "Origin": "http://localhost"}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.response_headers = {}

    def send_response(self, status): self.status = status
    def send_header(self, key, value): self.response_headers[key] = value
    def end_headers(self): pass


@pytest.fixture
def live_api(tmp_path, monkeypatch):
    from api import background_process, config, jarviz_live, jarviz_routes, profiles, routes
    from api.jarviz_tasks import TaskStore

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(background_process, "SESSION_CHANNELS", {})
    sessions = {
        "session-a": SimpleNamespace(session_id="session-a", profile="alpha", project_id="project-a", title="JarViz"),
        "session-b": SimpleNamespace(session_id="session-b", profile="beta", project_id="project-b", title="Private"),
    }
    projects = [
        {"project_id": "project-a", "profile": "alpha", "name": "Alpha"},
        {"project_id": "project-other", "profile": "alpha", "name": "Other"},
        {"project_id": "project-b", "profile": "beta", "name": "Beta"},
    ]
    monkeypatch.setattr(jarviz_routes, "get_session", lambda sid, **_: sessions[sid])
    monkeypatch.setattr(jarviz_routes, "load_projects", lambda **_: projects)
    monkeypatch.setattr(jarviz_live, "load_projects", lambda **_: projects)
    monkeypatch.setattr(routes, "get_session", lambda sid, **_: sessions[sid])
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *args, **kwargs: False)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    profiles.set_request_profile("alpha")

    def request(path, body=None, profile="alpha", method="POST"):
        profiles.set_request_profile(profile)
        handler = Handler(path, body, method)
        (routes.handle_get if method == "GET" else routes.handle_post)(handler, urlparse(path))
        return handler.status, json.loads(handler.wfile.getvalue())

    yield SimpleNamespace(request=request, store=TaskStore(tmp_path), module=jarviz_live)
    profiles.clear_request_profile()


def test_control_surface_contains_only_jarviz_functions(live_api):
    names = {item["name"] for item in live_api.module.CONTROL_FUNCTIONS}
    assert names == {
        "submit_task", "get_task_status", "cancel_task", "approve_action",
        "reject_action", "switch_project", "get_session_context", "update_persona",
    }
    assert names.isdisjoint({"terminal", "filesystem", "email", "smart_home", "browser"})


def test_token_is_single_use_constrained_and_long_lived_key_stays_server_side(live_api, monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return b'{"name":"ephemeral-one-use"}'

    def fake_open(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(live_api.module, "_api_key", lambda: "real-server-secret")
    monkeypatch.setattr(live_api.module, "urlopen", fake_open)
    status, response = live_api.request("/api/jarviz/live/token", {
        "session_id": "session-a", "project_id": "project-a",
    })
    assert status == 201
    assert response["token"] == "ephemeral-one-use"
    assert "real-server-secret" not in json.dumps(response)
    request = captured["request"]
    assert request.headers["X-goog-api-key"] == "real-server-secret"
    payload = json.loads(request.data)
    assert payload["uses"] == 1
    constrained = payload["liveConnectConstraints"]
    assert constrained["config"]["responseModalities"] == ["AUDIO"]
    assert constrained["config"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert {item["name"] for item in constrained["config"]["tools"][0]["functionDeclarations"]} == {
        item["name"] for item in live_api.module.CONTROL_FUNCTIONS
    }


def test_token_and_controls_enforce_origin_session_project_and_profile(live_api, monkeypatch):
    monkeypatch.setattr(live_api.module, "_api_key", lambda: "key")
    assert live_api.request("/api/jarviz/live/token", {"session_id": "session-a", "project_id": "project-other"})[0] == 400
    assert live_api.request("/api/jarviz/live/control", {
        "session_id": "session-b", "project_id": "project-b", "name": "get_session_context", "args": {},
    })[0] == 404
    assert live_api.request("/api/jarviz/live/control", {
        "session_id": "session-a", "project_id": "project-a", "name": "terminal", "args": {},
    })[0] == 400


def test_status_context_cancel_and_approval_directive_are_safe(live_api):
    task = live_api.store.create_task(session_id="session-a", project_id="project-a", title="Work", request="Do it")
    base = {"session_id": "session-a", "project_id": "project-a"}
    status, response = live_api.request("/api/jarviz/live/control", {
        **base, "name": "get_task_status", "args": {"task_id": task["task_id"]},
    })
    assert status == 200 and response["result"]["task"]["status"] == "queued"
    status, response = live_api.request("/api/jarviz/live/control", {
        **base, "name": "cancel_task", "args": {"task_id": task["task_id"]},
    })
    assert status == 200 and response["result"]["task"]["status"] == "cancelled"
    status, response = live_api.request("/api/jarviz/live/control", {
        **base, "name": "approve_action", "args": {"approval_id": "approval-1"},
    })
    assert status == 200 and response["result"]["approval"] == {"approval_id": "approval-1", "choice": "once"}
    status, response = live_api.request("/api/jarviz/live/control", {
        **base, "name": "get_session_context", "args": {},
    })
    assert status == 200
    assert response["result"]["session"]["session_id"] == "session-a"
    assert response["result"]["recent_tasks"][0]["status"] == "cancelled"


def test_submit_task_preserves_origin_and_enters_orchestrator(live_api, monkeypatch):
    from api import jarviz_orchestrator

    seen = {}

    def orchestrate_task(**kwargs):
        seen.update(kwargs)
        return live_api.store.update_task(kwargs["task_id"], status="running", expected_status="queued")

    monkeypatch.setattr(jarviz_orchestrator, "orchestrate_task", orchestrate_task)
    status, response = live_api.request("/api/jarviz/live/control", {
        "session_id": "session-a", "project_id": "project-a", "name": "submit_task",
        "args": {"title": "Voice work", "request": "Research the current question"},
    })
    task = response["result"]["task"]
    assert status == 200 and task["status"] == "running"
    assert task["session_id"] == "session-a" and task["project_id"] == "project-a"
    assert seen["session_id"] == "session-a" and seen["project_id"] == "project-a"


def test_natural_persona_update_control_is_persistent_and_returned_in_context(live_api):
    base = {"session_id": "session-a", "project_id": "project-a"}
    status, response = live_api.request("/api/jarviz/live/control", {
        **base, "name": "update_persona", "args": {"verbosity": "concise", "languages": ["fr", "darija", "en"]},
    })
    assert status == 200
    assert response["result"]["persona"]["verbosity"] == "concise"
    status, context = live_api.request("/api/jarviz/live/control", {
        **base, "name": "get_session_context", "args": {},
    })
    assert status == 200
    assert context["result"]["persona"]["verbosity"] == "concise"
    assert context["result"]["persona"]["languages"] == ["fr", "darija", "en"]


def test_persona_api_reads_and_updates_active_profile(live_api):
    status, response = live_api.request("/api/jarviz/persona", method="GET")
    assert status == 200 and response["persona"]["name"] == "JarViz"
    status, response = live_api.request("/api/jarviz/persona", {"tone": "calm", "voice": "Puck"})
    assert status == 200 and response["persona"]["tone"] == "calm"
    status, response = live_api.request("/api/jarviz/persona", method="GET")
    assert status == 200 and response["persona"]["voice"] == "Puck"


def test_native_client_uses_constrained_websocket_and_existing_apis_only():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = (root / "static" / "jarviz_live.js").read_text(encoding="utf-8")
    index = (root / "static" / "index.html").read_text(encoding="utf-8")
    worker = (root / "static" / "sw.js").read_text(encoding="utf-8")
    assert "getUserMedia" in source and "audio/pcm;rate=16000" in source
    assert "access_token=" in source and "new WebSocket" in source
    assert "/api/jarviz/live/token" in source and "/api/jarviz/live/control" in source
    assert "/api/approval/respond" in source
    assert "window.JarVizLive.onTaskEvent" in (root / "static" / "jarviz.js").read_text(encoding="utf-8")
    assert "jarviz_live.js?v=__WEBUI_VERSION__" in index
    assert "./static/jarviz_live.js' + VQ" in worker
    assert "GEMINI_API_KEY" not in source and "GOOGLE_API_KEY" not in source
    for event_type in ("task.blocked", "approval.requested", "task.completed", "task.failed"):
        assert event_type in source
    jarviz = (root / "static" / "jarviz.js").read_text(encoding="utf-8")
    handler = jarviz.split("function handleJarvizTaskEvent", 1)[1].split("function renderJarvizActivity", 1)[0]
    assert handler.index("_jarvizTasks.set") < handler.index("window.JarVizLive.onTaskEvent(envelope)")


def test_completed_result_is_persisted_before_one_event_fans_out(live_api):
    from api.background_process import subscribe_to_session_channel

    channel, subscriber = subscribe_to_session_channel("session-a")
    try:
        task = live_api.store.create_task(
            session_id="session-a", project_id="project-a", title="Work", request="Do it",
        )
        subscriber.get_nowait()  # task.created
        completed = live_api.store.update_task(task["task_id"], status="completed", result="Durable answer")
        event_name, envelope = subscriber.get_nowait()
        durable = live_api.store.get_task(task["task_id"])
        assert event_name == "jarviz_task_event"
        assert envelope["event_type"] == "task.completed"
        assert durable["result"] == completed["result"] == envelope["payload"]["result"] == "Durable answer"
        assert envelope["session_id"] == "session-a"
        assert subscriber.empty()
    finally:
        channel.unsubscribe(subscriber)
