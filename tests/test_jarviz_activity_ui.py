"""Static contracts for the Phase 3A activity view and existing SSE wiring."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
JARVIZ = (ROOT / "static" / "jarviz.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SERVICE_WORKER = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_activity_view_extends_existing_navigation_and_main_view():
    assert INDEX.count('data-panel="activity"') == 2
    assert 'id="panelActivity"' in INDEX
    assert 'id="mainActivity"' in INDEX
    assert 'static/jarviz.js?v=__WEBUI_VERSION__' in INDEX
    assert "'./static/jarviz.js' + VQ" in SERVICE_WORKER
    assert "'activity'" in PANELS.split("const MAIN_VIEW_PANELS", 1)[1].split(";", 1)[0]
    assert "nextPanel === 'activity'" in PANELS
    assert "main.main.showing-activity > #mainActivity" in STYLE


def test_session_and_chat_streams_consume_jarviz_events():
    assert MESSAGES.count("addEventListener('jarviz_task_event'") == 2
    assert "handleJarvizTaskEvent(e, sid)" in MESSAGES
    assert "handleJarvizTaskEvent(e,activeSid)" in MESSAGES
    assert "addEventListener('initial'" in MESSAGES
    initial = MESSAGES.split("addEventListener('initial'", 1)[1].split("});", 1)[0]
    assert "loadJarvizTasks(sid, true)" in initial


def test_durable_hydration_is_authoritative_and_session_scoped():
    assert "api/jarviz/tasks?session_id=" in JARVIZ
    assert "cache: 'no-store'" in JARVIZ
    assert "_jarvizTasks = new Map(tasks.map" in JARVIZ
    assert "api/projects/" in JARVIZ and "/jarviz" in JARVIZ
    assert "envelope.session_id" in JARVIZ
    assert "task.session_id" in JARVIZ


def test_view_renders_required_activity_information():
    for label in (
        "Active agents", "Task hierarchy", "Approval required", "Artifacts",
        "Completed tasks", "Project blockers",
    ):
        assert label in JARVIZ
    for status in ("running", "blocked", "awaiting_approval", "failed", "completed"):
        assert status in JARVIZ
    assert "parent_task_id" in JARVIZ
    assert "_jarvizElapsed" in JARVIZ
    assert "textContent" in JARVIZ  # untrusted task content is not HTML-injected


def test_transcript_surfaces_only_meaningful_durable_milestones():
    for label in (
        "Task started", "Agent delegated", "Blocked", "Approval required",
        "Task failed", "Task completed",
    ):
        assert label in JARVIZ
    assert "_jarvizImportantEvents" in JARVIZ
    assert "events.slice(-40)" in JARVIZ
    assert "event.task.result" not in JARVIZ  # results flow through the normalized detail field
    assert "task.result, 'Result'" in JARVIZ
    assert "task.error, 'Error'" in JARVIZ
    assert "renderJarvizSessionActivity" in UI
    assert "jarvizSessionActivity" in JARVIZ
