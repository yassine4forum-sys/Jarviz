"""Focused Phase 2A classification, privilege and lifecycle tests."""
import json
import sys
from types import SimpleNamespace

import pytest

from api import config, jarviz_orchestrator, jarviz_routes, models, routes, run_journal, streaming
from api.jarviz_orchestrator import TaskRun
from api.jarviz_tasks import TaskStore


@pytest.mark.parametrize(("prompt", "category"), [
    ("Say hello", "simple"),
    ("Debug the Python unit test", "coding"),
    ("Research the latest sources", "research"),
    ("Research sources and implement code", "complex"),
    ("Send an email", "communications"),
    ("Rename the files", "files"),
    ("Turn off the lights", "smart_home"),
    ("Implement a Home Assistant integration", "coding"),
])
def test_classifier_covers_declared_categories(prompt, category):
    assert jarviz_orchestrator.classify_request(prompt) == category


@pytest.mark.parametrize(("category", "toolsets", "tools"), [
    ("simple", [], []),
    ("coding", ["file", "terminal"], ["read_file", "run_command"]),
    ("research", ["web"], ["web_search"]),
    ("smart_home", ["jarviz-smart-home"],
     ["home_assistant_get_states", "home_assistant_call_service"]),
])
def test_restricted_factory_enforces_minimal_toolsets(monkeypatch, category, toolsets, tools):
    mapping = {"file": ["read_file"], "terminal": ["run_command"], "web": ["web_search"]}
    monkeypatch.setitem(sys.modules, "toolsets", SimpleNamespace(
        resolve_toolset=lambda name, include_registry=False: mapping[name]))
    monkeypatch.setattr("api.jarviz_home_assistant.ensure_registered", lambda: None)
    captured = {}

    def factory(*, enabled_toolsets, skip_memory, skip_background_review, max_iterations):
        captured.update(enabled_toolsets=enabled_toolsets, skip_memory=skip_memory,
                        skip_background_review=skip_background_review,
                        max_iterations=max_iterations)
        definitions = [{"function": {"name": name}} for name in tools]
        return SimpleNamespace(tools=definitions, valid_tool_names=set(tools))

    policy = TaskRun("t", "s", "p", category)
    construct = jarviz_orchestrator.restricted_agent_factory(
        factory, {"enabled_toolsets", "skip_memory", "skip_background_review", "max_iterations"}, policy)
    construct(enabled_toolsets=["everything"], skip_memory=False,
              skip_background_review=False, max_iterations=99)
    assert captured == {"enabled_toolsets": toolsets, "skip_memory": True,
                        "skip_background_review": True, "max_iterations": 8}


@pytest.mark.parametrize("category", ["simple", "coding", "research"])
def test_non_smart_runs_never_register_or_load_smart_home_tools(monkeypatch, category):
    mapping = {"file": ["read_file"], "terminal": ["run_command"], "web": ["web_search"]}
    monkeypatch.setitem(sys.modules, "toolsets", SimpleNamespace(
        resolve_toolset=lambda name, include_registry=False: mapping[name]))
    monkeypatch.setattr("api.jarviz_home_assistant.ensure_registered",
                        lambda: pytest.fail("smart-home registry must stay unloaded"))
    tools = [name for toolset in jarviz_orchestrator.TOOLSETS[category]
             for name in mapping.get(toolset, [])]
    agent = SimpleNamespace(tools=[{"function": {"name": name}} for name in tools],
                            valid_tool_names=set(tools))
    construct = jarviz_orchestrator.restricted_agent_factory(
        lambda **kwargs: agent,
        {"enabled_toolsets", "skip_memory", "skip_background_review", "max_iterations"},
        TaskRun("t", "s", "p", category))
    construct()


def test_restricted_factory_rejects_extra_tool_schema(monkeypatch):
    monkeypatch.setitem(sys.modules, "toolsets", SimpleNamespace(
        resolve_toolset=lambda name, include_registry=False: ["read_file"]))
    agent = SimpleNamespace(tools=[{"function": {"name": "skills_list"}}],
                            valid_tool_names={"skills_list"})
    construct = jarviz_orchestrator.restricted_agent_factory(
        lambda **kwargs: agent,
        {"enabled_toolsets", "skip_memory", "skip_background_review", "max_iterations"},
        TaskRun("t", "s", "p", "coding"))
    with pytest.raises(RuntimeError, match="outside"):
        construct()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    store = TaskStore(tmp_path)
    monkeypatch.setattr(jarviz_routes, "_Access", lambda: SimpleNamespace(task=lambda task: task))
    return store


def _task(store, request="Implement the parser"):
    return store.create_task(session_id="session-a", project_id="project-a",
                             title="Task", request=request)


def test_orchestrator_starts_supported_task_and_records_policy(store, monkeypatch):
    task = _task(store)
    captured = {}

    def start(session_id, request, **kwargs):
        captured.update(session_id=session_id, request=request, **kwargs)
        return {"stream_id": "run-a"}

    monkeypatch.setattr(routes, "start_session_turn", start)
    result = jarviz_orchestrator.orchestrate_task(
        session_id="session-a", project_id="project-a",
        request=task["request"], task_id=task["task_id"])
    assert result["status"] == "running" and result["task_type"] == "coding"
    assert captured["source"] == "jarviz"
    children = [item for item in store.list_tasks() if item["parent_task_id"] == task["task_id"]]
    assert len(children) == 1
    child = children[0]
    assert child["assigned_agent"] == "coding_agent"
    assert captured["jarviz_run"] == TaskRun(
        child["task_id"], "session-a", "project-a", "coding", task["task_id"], True)
    metadata = json.loads(result["metadata_json"])["orchestrator"]
    assert metadata == {"version": 1, "category": "coding", "toolsets": ["file", "terminal"]}


def test_unsupported_category_is_blocked_without_starting_turn(store, monkeypatch):
    task = _task(store, "Send an email")
    monkeypatch.setattr(routes, "start_session_turn",
                        lambda *args, **kwargs: pytest.fail("unsupported task must not start"))
    result = jarviz_orchestrator.orchestrate_task(
        session_id="session-a", project_id="project-a",
        request=task["request"], task_id=task["task_id"])
    assert result["status"] == "blocked" and result["task_type"] == "communications"
    assert "not supported" in result["error"]


def test_origin_mismatch_never_mutates_or_dispatches(store, monkeypatch):
    task = _task(store)
    monkeypatch.setattr(routes, "start_session_turn",
                        lambda *args, **kwargs: pytest.fail("mismatched task must not start"))
    with pytest.raises(ValueError, match="origin"):
        jarviz_orchestrator.orchestrate_task(
            session_id="session-b", project_id="project-a",
            request=task["request"], task_id=task["task_id"])
    assert store.get_task(task["task_id"])["status"] == "queued"


def _running(store):
    task = _task(store)
    metadata = {"orchestrator": {"version": 1, "category": "coding",
                                  "toolsets": ["file", "terminal"],
                                  "run_id": "run-a", "message_count": 1}}
    return store.update_task(task["task_id"], status="running", expected_status="queued",
                             metadata_json=json.dumps(metadata))


def test_completed_run_returns_only_to_originating_task(store, monkeypatch):
    task = _running(store)
    event = {"event": "done", "terminal_state": "completed", "payload": {"session": {
        "messages": [{"role": "assistant", "content": "Old"},
                     {"role": "assistant", "content": "Final result"}]}}}
    monkeypatch.setattr(run_journal, "read_run_events",
                        lambda session_id, run_id: {"events": [event]})
    monkeypatch.setattr(run_journal, "select_authoritative_terminal_event", lambda events: event)
    policy = TaskRun(task["task_id"], "session-a", "project-a", "coding")
    jarviz_orchestrator.settle_run(policy, "run-a", store)
    result = store.get_task(task["task_id"])
    assert result["status"] == "completed" and result["result"] == "Final result"
    assert result["session_id"] == "session-a"


@pytest.mark.parametrize(("state", "status"), [
    ("errored", "failed"), ("interrupted-by-user", "cancelled"),
    ("tool_limit_reached", "blocked"),
])
def test_terminal_failures_are_projected_to_task(store, monkeypatch, state, status):
    task = _running(store)
    event = {"event": "error", "terminal_state": state, "payload": {}}
    monkeypatch.setattr(run_journal, "read_run_events", lambda *args: {"events": [event]})
    monkeypatch.setattr(run_journal, "select_authoritative_terminal_event", lambda events: event)
    jarviz_orchestrator.settle_run(
        TaskRun(task["task_id"], "session-a", "project-a", "coding"), "run-a", store)
    assert store.get_task(task["task_id"])["status"] == status


def test_settlement_rejects_cross_session_policy(store):
    task = _running(store)
    with pytest.raises(ValueError, match="origin"):
        jarviz_orchestrator.settle_run(
            TaskRun(task["task_id"], "session-b", "project-a", "coding"), "run-a", store)
    assert store.get_task(task["task_id"])["status"] == "running"


def test_background_worker_binds_run_and_policy_to_origin(store, monkeypatch):
    task = _running(store)
    # The worker owns the run id, so remove the synthetic settlement id first.
    metadata = json.loads(task["metadata_json"])
    metadata["orchestrator"].pop("run_id")
    store.update_task(task["task_id"], metadata_json=json.dumps(metadata))
    policy = TaskRun(task["task_id"], "session-a", "project-a", "coding")
    observed = {}
    monkeypatch.setattr(models, "get_session", lambda sid: SimpleNamespace(messages=[{"role": "user"}]))

    def execute(*args, **kwargs):
        observed["execute"] = (args, kwargs)

    def settle(actual_policy, stream_id, actual_store):
        observed["settle"] = (actual_policy, stream_id, actual_store.get_task(actual_policy.task_id))

    monkeypatch.setattr(streaming, "_run_agent_streaming", execute)
    monkeypatch.setattr(jarviz_orchestrator, "settle_run", settle)
    jarviz_orchestrator.run_task_worker(
        "session-a", "Implement", "model", "workspace", "run-worker",
        jarviz_run=policy, model_provider="groq")
    assert observed["execute"][1]["jarviz_policy"] == policy
    assert observed["settle"][0:2] == (policy, "run-worker")
    stored = json.loads(observed["settle"][2]["metadata_json"])["orchestrator"]
    assert stored["run_id"] == "run-worker" and stored["message_count"] == 1


def test_streaming_restricted_runs_do_not_inherit_ambient_context():
    text = open(streaming.__file__, encoding="utf-8").read()
    assert "conversation_history=([] if jarviz_policy is not None" in text
    assert "jarviz_policy.category != 'coding'" in text
    assert "_prefill_messages = []" in text
