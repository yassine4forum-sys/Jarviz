"""Deterministic routing of JarViz tasks through existing Hermes turns."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from api.jarviz_tasks import TaskConflictError, TaskStore

CATEGORIES = frozenset({"simple", "coding", "research", "communications", "files", "smart_home", "complex"})
TOOLSETS = {
    "simple": (), "coding": ("file", "terminal"), "research": ("web",),
    "smart_home": ("jarviz-smart-home",),
}


def classify_request(request: str) -> str:
    """Conservative, local classifier; no model call or hidden tool discovery."""
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be nonempty text")
    text = request.lower()
    if re.search(r"\b(email|slack|sms|whatsapp|send (?:a )?message|post to)\b", text):
        return "communications"
    coding = bool(re.search(r"\b(code|coding|debug|bug|refactor|python|javascript|typescript|repository|repo|unit test|implement|compile)\b", text))
    research = bool(re.search(r"\b(research|search|sources|citations|browse|look up|latest|web|investigate)\b|https?://", text))
    if (coding and research) or re.search(r"\b(complex|multi.step|multi.agent|end.to.end|coordinate|parallel)\b", text):
        return "complex"
    if coding:
        return "coding"
    if research:
        return "research"
    if re.search(
        r"\b(smart.?home|thermostat|air conditioner|home assistant|"
        r"turn (?:on|off) (?:the )?(?:lights?|fans?|switches?)|"
        r"(?:open|close) (?:the )?(?:blinds?|covers?)|set (?:the )?.{0,32}temperature)\b",
        text,
    ):
        return "smart_home"
    if re.search(r"\b(files?|folders?|directory|directories|rename|move|delete|organize|organise)\b", text):
        return "files"
    return "simple"


@dataclass(frozen=True)
class TaskRun:
    task_id: str
    session_id: str
    project_id: str | None
    category: str
    parent_task_id: str | None = None
    return_to_parent: bool = False

    def __post_init__(self):
        if self.category not in TOOLSETS:
            raise ValueError("unsupported task category")


def restricted_agent_factory(factory, parameters, policy: TaskRun):
    """Enforce controls on every fresh/retry agent before any conversation call."""
    required = {"enabled_toolsets", "skip_memory", "skip_background_review", "max_iterations"}
    if not required <= set(parameters):
        raise RuntimeError("Hermes lacks required restricted-run controls")

    def construct(**kwargs):
        from toolsets import resolve_toolset

        selected = TOOLSETS[policy.category]
        if policy.category == "smart_home":
            from api.jarviz_home_assistant import TOOL_NAMES, ensure_registered

            ensure_registered()
            allowed = set(TOOL_NAMES)
        else:
            allowed = {name for toolset in selected for name in resolve_toolset(toolset, include_registry=False)}
        kwargs.update(enabled_toolsets=list(selected), skip_memory=True,
                      skip_background_review=True,
                      max_iterations=min(kwargs.get("max_iterations") or 8, 8))
        agent = factory(**kwargs)
        actual = {tool["function"]["name"] for tool in (agent.tools or [])}
        invalid_tools = not actual <= allowed or set(agent.valid_tool_names) != actual
        missing_smart_home_tools = policy.category == "smart_home" and actual != allowed
        if invalid_tools or missing_smart_home_tools:
            # Never send an overprivileged schema to the model. No global
            # registry monkeypatches or profile/session setting mutations.
            raise RuntimeError("Hermes exposed tools outside the task allowlist")
        return agent

    return construct


def orchestrate_task(*, session_id: str, project_id: str | None, request: str, task_id: str) -> dict:
    """Dispatch simple work directly and coding/research to one specialist."""
    from api.jarviz_routes import _Access
    from api.routes import start_session_turn

    store = TaskStore()
    task = _Access().task(store.get_task(task_id))
    if (task["session_id"], task["project_id"], task["request"]) != (session_id, project_id, request):
        raise ValueError("task origin/request mismatch")
    category = classify_request(request)
    metadata = json.loads(task["metadata_json"])
    metadata["orchestrator"] = {"version": 1, "category": category, "toolsets": list(TOOLSETS.get(category, ()))}
    supported = category in TOOLSETS or category == "complex"
    parent = store.update_task(
        task_id, expected_status="queued", status="running" if supported else "blocked",
        task_type=category, assigned_agent="orchestrator" if supported else None,
        metadata_json=json.dumps(metadata),
        error=None if supported else f"Category {category} is not supported in Phase 2A.",
    )
    if not supported:
        return parent
    if category == "complex":
        return _orchestrate_complex(store, parent, start_session_turn)
    run_task = parent
    uses_specialist = False
    if category in {"coding", "research", "smart_home"}:
        from api.jarviz_specialists import create_specialist_task

        try:
            run_task = create_specialist_task(store, parent, category)
            uses_specialist = True
        except Exception:
            _fail_running(store, task_id, "The specialist task could not be created.")
            return store.get_task(task_id)
    policy = TaskRun(run_task["task_id"], session_id, project_id, category,
                     parent_task_id=run_task["parent_task_id"],
                     return_to_parent=uses_specialist)
    try:
        response = start_session_turn(session_id, request, source="jarviz",
                                      jarviz_run=policy)
        if not isinstance(response, dict) or not response.get("stream_id") or response.get("_status", 200) >= 300:
            status = (response or {}).get("_status", 500) if isinstance(response, dict) else 500
            _finish_run_task(
                store, policy, "blocked" if status in (409, 501) else "failed",
                "Session is busy or this runtime cannot enforce task restrictions."
                if status in (409, 501) else "Hermes could not start the task.",
            )
    except TaskConflictError:
        pass  # Another lifecycle owner already settled/cancelled it.
    except Exception:
        _finish_run_task(store, policy, "failed", "Hermes could not start the task.")
    return store.get_task(task_id)


def _orchestrate_complex(store, parent, start_session_turn):
    from api.jarviz_complex import create_complex_children
    from api.profiles import clear_request_profile, get_active_profile_name, set_request_profile

    try:
        children = create_complex_children(store, parent)
    except Exception:
        _fail_running(store, parent["task_id"], "The bounded complex-task plan could not be created.")
        return store.get_task(parent["task_id"])

    policies = []
    for child in children:
        child = store.update_task(child["task_id"], expected_status="queued", status="running")
        policies.append(TaskRun(child["task_id"], child["root_session_id"], child["project_id"],
                                child["task_type"], child["parent_task_id"], True))

    profile = get_active_profile_name()

    def start(policy):
        set_request_profile(profile)
        try:
            task = store.get_task(policy.task_id)
            return policy, start_session_turn(
                policy.session_id, task["request"], source="jarviz", jarviz_run=policy)
        finally:
            clear_request_profile()

    # Hermes' existing start path remains the execution owner. Independent
    # starts are submitted together; runtimes that serialize a session return
    # a normal busy response, which is durably projected as a blocker.
    with ThreadPoolExecutor(max_workers=len(policies)) as pool:
        futures = [pool.submit(start, policy) for policy in policies]
        for future, policy in zip(futures, policies, strict=True):
            try:
                _, response = future.result()
                if (not isinstance(response, dict) or not response.get("stream_id")
                        or response.get("_status", 200) >= 300):
                    status = (response or {}).get("_status", 500) if isinstance(response, dict) else 500
                    _finish_run_task(
                        store, policy, "blocked" if status in (409, 501) else "failed",
                        "A required child could not start because the session is busy or restricted."
                        if status in (409, 501) else "A required child could not start.",
                    )
            except Exception:
                _finish_run_task(store, policy, "failed", "A required child could not start.")
    return store.get_task(parent["task_id"])


def _fail_running(store, task_id, error):
    try:
        store.update_task(task_id, expected_status="running", status="failed", error=error)
    except TaskConflictError:
        pass


def _finish_run_task(store, policy, status, error, result=None):
    from api.jarviz_specialists import project_specialist_outcome

    try:
        task = store.update_task(policy.task_id, expected_status="running", status=status,
                                 result=result, error=error)
    except TaskConflictError:
        task = store.get_task(policy.task_id)
    if policy.return_to_parent:
        project_specialist_outcome(store, task)
    return task


def settle_run(policy: TaskRun, stream_id: str, store=None):
    """Project the existing durable run outcome onto the original JarViz task."""
    from api.run_journal import read_run_events, select_authoritative_terminal_event

    store = store or TaskStore()
    task = store.get_task(policy.task_id)
    if (task["session_id"], task["project_id"]) != (policy.session_id, policy.project_id):
        raise ValueError("task origin mismatch")
    if task["parent_task_id"] != policy.parent_task_id:
        raise ValueError("task parent mismatch")
    if json.loads(task["metadata_json"]).get("orchestrator", {}).get("run_id") != stream_id:
        raise ValueError("task run mismatch")
    events = read_run_events(policy.session_id, stream_id)["events"]
    terminal = select_authoritative_terminal_event(events)
    state = (terminal or {}).get("terminal_state")
    payload = (terminal or {}).get("payload") or {}
    status, result, error = "failed", None, "Hermes run ended without a confirmed result."
    if terminal and terminal["event"] == "done" and state == "completed":
        # Use the final run payload, never whichever session happens to be open.
        messages = (payload.get("session") or {}).get("messages") or []
        baseline = json.loads(task["metadata_json"]).get("orchestrator", {}).get("message_count", 0)
        messages = messages[baseline:]
        result = next((m.get("content") for m in reversed(messages)
                       if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()), None)
        if result is not None:
            status, error = "completed", None
    elif state == "interrupted-by-user":
        status, error = "cancelled", "Hermes run was cancelled."
    elif state == "tool_limit_reached":
        status, error = "blocked", "Task reached its bounded execution limit."
    elif state in {"errored", "interrupted-by-crash"}:
        error = "Hermes run failed. Inspect the originating session for details."
    _finish_run_task(store, policy, status, error, result)


def run_task_worker(session_id, message, model, workspace, stream_id, attachments=None, *, jarviz_run, **kwargs):
    """Wrap the existing worker, not its SSE transport or execution engine."""
    from api.streaming import _run_agent_streaming
    from api.models import get_session

    store = TaskStore()
    policy = jarviz_run
    task = store.get_task(policy.task_id)
    if ((session_id, task["session_id"], task["project_id"])
            != (policy.session_id, policy.session_id, policy.project_id)
            or task["parent_task_id"] != policy.parent_task_id):
        _finish_run_task(store, policy, "failed", "Task origin validation failed.")
        return
    metadata = json.loads(task["metadata_json"])
    metadata["orchestrator"]["run_id"] = stream_id
    metadata["orchestrator"]["message_count"] = len(getattr(get_session(session_id), "messages", []))
    try:
        store.update_task(policy.task_id, expected_status="running", metadata_json=json.dumps(metadata))
        _run_agent_streaming(session_id, message, model, workspace, stream_id, attachments,
                             jarviz_policy=policy, **kwargs)
        settle_run(policy, stream_id, store)
    except TaskConflictError:
        return
    except Exception:
        _finish_run_task(store, policy, "failed", "Hermes task execution failed.")
