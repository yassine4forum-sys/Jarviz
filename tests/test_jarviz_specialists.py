"""Phase 2B specialist ownership, completion and event routing tests."""
import json
from queue import Empty
from types import SimpleNamespace

import pytest

from api import (background_process, config, jarviz_orchestrator, jarviz_routes,
                 models, routes, run_journal, streaming)
from api.jarviz_tasks import TaskStore
from api.jarviz_specialists import SPECIALISTS, specialist_system_prompt


@pytest.fixture
def specialist(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(background_process, "SESSION_CHANNELS", {})
    monkeypatch.setattr(jarviz_routes, "_Access", lambda: SimpleNamespace(task=lambda task: task))
    monkeypatch.setattr(models, "get_session", lambda sid: SimpleNamespace(messages=[]))
    store = TaskStore(tmp_path)
    captured = {}

    def start(session_id, request, **kwargs):
        captured.update(session_id=session_id, request=request, **kwargs)
        return {"stream_id": "run-specialist"}

    monkeypatch.setattr(routes, "start_session_turn", start)

    def dispatch(request):
        parent = store.create_task(session_id="session-a", project_id="project-a",
                                   title="Parent", request=request)
        result = jarviz_orchestrator.orchestrate_task(
            session_id="session-a", project_id="project-a", request=request,
            task_id=parent["task_id"])
        child = next(task for task in store.list_tasks()
                     if task["parent_task_id"] == parent["task_id"])
        return result, child, captured["jarviz_run"]

    return SimpleNamespace(store=store, captured=captured, dispatch=dispatch)


def _terminal(monkeypatch, state, messages=None):
    event = {"event": "done" if state == "completed" else "error",
             "terminal_state": state,
             "payload": {"session": {"messages": messages or []}}}
    monkeypatch.setattr(run_journal, "read_run_events", lambda *args: {"events": [event]})
    monkeypatch.setattr(run_journal, "select_authoritative_terminal_event", lambda events: event)


def _bind_run(store, child, run_id="run-specialist"):
    metadata = json.loads(child["metadata_json"])
    metadata["orchestrator"].update(run_id=run_id, message_count=0)
    return store.update_task(child["task_id"], metadata_json=json.dumps(metadata))


@pytest.mark.parametrize(("prompt", "agent", "toolsets"), [
    ("Implement the parser", "coding_agent", ["file", "terminal"]),
    ("Research current primary sources", "research_agent", ["web"]),
    ("Turn off the lights", "smart_home_agent", ["jarviz-smart-home"]),
])
def test_only_declared_specialists_are_created(specialist, prompt, agent, toolsets):
    assert set(SPECIALISTS) == {"coding", "research", "smart_home"}
    parent, child, policy = specialist.dispatch(prompt)
    assert parent["status"] == child["status"] == "running"
    assert child["assigned_agent"] == agent
    assert child["parent_task_id"] == parent["task_id"]
    assert (child["session_id"], child["project_id"]) == ("session-a", "project-a")
    assert policy.task_id == child["task_id"] and policy.parent_task_id == parent["task_id"]
    assert json.loads(child["metadata_json"])["orchestrator"]["toolsets"] == toolsets
    assert agent.replace("_", " ").title() in specialist_system_prompt(policy.category)


def test_specialist_success_returns_result_to_parent(specialist, monkeypatch):
    parent, child, policy = specialist.dispatch("Implement the parser")
    _bind_run(specialist.store, child)
    _terminal(monkeypatch, "completed", [{"role": "assistant", "content": "Implemented"}])
    jarviz_orchestrator.settle_run(policy, "run-specialist", specialist.store)
    child = specialist.store.get_task(child["task_id"])
    parent = specialist.store.get_task(parent["task_id"])
    assert (child["status"], child["result"]) == ("completed", "Implemented")
    assert (parent["status"], parent["result"]) == ("completed", "Implemented")


def test_specialist_failure_returns_safe_error_to_parent(specialist, monkeypatch):
    parent, child, policy = specialist.dispatch("Research current primary sources")
    _bind_run(specialist.store, child)
    _terminal(monkeypatch, "errored")
    jarviz_orchestrator.settle_run(policy, "run-specialist", specialist.store)
    child = specialist.store.get_task(child["task_id"])
    parent = specialist.store.get_task(parent["task_id"])
    assert child["status"] == parent["status"] == "failed"
    assert parent["error"] == child["error"]
    assert "originating session" in parent["error"]


def test_background_worker_completes_specialist_and_parent(specialist, monkeypatch):
    parent, child, policy = specialist.dispatch("Implement the parser")
    _terminal(monkeypatch, "completed", [{"role": "assistant", "content": "Background result"}])
    monkeypatch.setattr(streaming, "_run_agent_streaming", lambda *args, **kwargs: None)
    jarviz_orchestrator.run_task_worker(
        "session-a", child["request"], "model", "workspace", "run-background",
        jarviz_run=policy)
    child = specialist.store.get_task(child["task_id"])
    parent = specialist.store.get_task(parent["task_id"])
    assert child["status"] == parent["status"] == "completed"
    assert parent["result"] == "Background result"


def test_lifecycle_events_return_only_to_originating_session(specialist, monkeypatch):
    channel_a, queue_a = background_process.subscribe_to_session_channel("session-a")
    channel_b, queue_b = background_process.subscribe_to_session_channel("session-b")
    try:
        parent, child, policy = specialist.dispatch("Implement the parser")
        _bind_run(specialist.store, child)
        _terminal(monkeypatch, "completed", [{"role": "assistant", "content": "Same session"}])
        jarviz_orchestrator.settle_run(policy, "run-specialist", specialist.store)
        frames = []
        while True:
            try:
                frames.append(queue_a.get_nowait())
            except Empty:
                break
        assert frames
        assert all(name == "jarviz_task_event" and payload["session_id"] == "session-a"
                   for name, payload in frames)
        assert {payload["task_id"] for _, payload in frames} == {parent["task_id"], child["task_id"]}
        with pytest.raises(Empty):
            queue_b.get_nowait()
    finally:
        channel_a.unsubscribe(queue_a)
        channel_b.unsubscribe(queue_b)
