"""Phase 2C bounded hierarchy and required-child reconciliation tests."""
import json
import threading
from types import SimpleNamespace

import pytest

from api import config, jarviz_orchestrator, jarviz_routes, routes, run_journal
from api.jarviz_tasks import TaskStore


@pytest.fixture
def complex_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(jarviz_routes, "_Access", lambda: SimpleNamespace(task=lambda task: task))
    store = TaskStore(tmp_path)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    policies = []

    def start(session_id, request, **kwargs):
        barrier.wait(timeout=2)
        with lock:
            policies.append(kwargs["jarviz_run"])
        return {"stream_id": "run-" + kwargs["jarviz_run"].task_id}

    monkeypatch.setattr(routes, "start_session_turn", start)
    parent = store.create_task(
        session_id="session-a", project_id="project-a", title="Complex",
        request="Research current sources and implement the code",
    )
    result = jarviz_orchestrator.orchestrate_task(
        session_id="session-a", project_id="project-a", request=parent["request"],
        task_id=parent["task_id"],
    )
    return SimpleNamespace(store=store, parent=result, policies=policies)


def _bind_and_install_terminals(run, monkeypatch, states):
    events = {}
    for policy in run.policies:
        child = run.store.get_task(policy.task_id)
        metadata = json.loads(child["metadata_json"])
        stream_id = "run-" + child["task_id"]
        metadata["orchestrator"].update(run_id=stream_id, message_count=0)
        run.store.update_task(child["task_id"], metadata_json=json.dumps(metadata))
        state = states[child["task_type"]]
        events[stream_id] = {
            "event": "done" if state == "completed" else "error",
            "terminal_state": state,
            "payload": {"session": {"messages": [
                {"role": "assistant", "content": child["task_type"] + " result"}
            ]}},
        }
    monkeypatch.setattr(
        run_journal, "read_run_events",
        lambda session_id, stream_id: {"events": [events[stream_id]]},
    )
    monkeypatch.setattr(run_journal, "select_authoritative_terminal_event", lambda values: values[0])


def test_complex_plan_starts_parallel_required_children(complex_run):
    children = complex_run.store.list_children(complex_run.parent["task_id"])
    assert len(children) == len(complex_run.policies) == 2
    assert {child["task_type"] for child in children} == {"coding", "research"}
    assert all(child["hierarchy_depth"] == 2 for child in children)
    assert all(child["root_session_id"] == "session-a" for child in children)
    assert all(child["project_id"] == "project-a" for child in children)
    metadata = json.loads(complex_run.parent["metadata_json"])["orchestrator"]
    assert set(metadata["required_child_ids"]) == {child["task_id"] for child in children}
    assert metadata["max_depth"] == 3 and metadata["execution"] == "parallel"


def test_parent_completes_only_after_all_required_children(complex_run, monkeypatch):
    _bind_and_install_terminals(complex_run, monkeypatch,
                                {"coding": "completed", "research": "completed"})
    first, second = complex_run.policies
    jarviz_orchestrator.settle_run(first, "run-" + first.task_id, complex_run.store)
    assert complex_run.store.get_task(complex_run.parent["task_id"])["status"] == "running"
    jarviz_orchestrator.settle_run(second, "run-" + second.task_id, complex_run.store)
    parent = complex_run.store.get_task(complex_run.parent["task_id"])
    assert parent["status"] == "completed"
    assert "coding result" in parent["result"] and "research result" in parent["result"]


def test_child_failure_is_visible_then_propagates_after_siblings_finish(complex_run, monkeypatch):
    _bind_and_install_terminals(complex_run, monkeypatch,
                                {"coding": "errored", "research": "completed"})
    failed = next(policy for policy in complex_run.policies
                  if complex_run.store.get_task(policy.task_id)["task_type"] == "coding")
    other = next(policy for policy in complex_run.policies if policy != failed)
    jarviz_orchestrator.settle_run(failed, "run-" + failed.task_id, complex_run.store)
    parent = complex_run.store.get_task(complex_run.parent["task_id"])
    assert parent["status"] == "running" and "failed" in parent["error"]
    jarviz_orchestrator.settle_run(other, "run-" + other.task_id, complex_run.store)
    parent = complex_run.store.get_task(complex_run.parent["task_id"])
    assert parent["status"] == "failed" and "Coding step" in parent["error"]


def test_child_start_blocker_propagates_to_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(jarviz_routes, "_Access", lambda: SimpleNamespace(task=lambda task: task))
    store = TaskStore(tmp_path)

    def start(session_id, request, **kwargs):
        policy = kwargs["jarviz_run"]
        if policy.category == "research":
            return {"error": "busy", "_status": 409}
        return {"stream_id": "run-coding"}

    monkeypatch.setattr(routes, "start_session_turn", start)
    parent = store.create_task(
        session_id="session-a", project_id="project-a", title="Complex",
        request="Research current sources and implement code")
    result = jarviz_orchestrator.orchestrate_task(
        session_id="session-a", project_id="project-a", request=parent["request"],
        task_id=parent["task_id"])
    children = store.list_children(parent["task_id"])
    assert result["status"] == "blocked"
    assert "blocked" in result["error"]
    assert {child["status"] for child in children} == {"running", "blocked"}


def test_depth_three_parent_cannot_create_another_level(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(jarviz_routes, "_Access", lambda: SimpleNamespace(task=lambda task: task))
    monkeypatch.setattr(routes, "start_session_turn",
                        lambda *args, **kwargs: pytest.fail("depth violation must not run"))
    store = TaskStore(tmp_path)
    root = store.create_task(session_id="session-a", project_id="project-a", title="R", request="R")
    child = store.create_task(session_id="session-a", project_id="project-a",
                              parent_task_id=root["task_id"], title="C", request="C")
    task = store.create_task(session_id="session-a", project_id="project-a",
                             parent_task_id=child["task_id"], title="G",
                             request="Research sources and implement code")
    result = jarviz_orchestrator.orchestrate_task(
        session_id="session-a", project_id="project-a", request=task["request"],
        task_id=task["task_id"])
    assert result["status"] == "failed"
    assert "bounded complex-task plan" in result["error"]
