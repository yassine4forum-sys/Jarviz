"""Phase 2B specialist task creation and parent lifecycle projection."""
from __future__ import annotations

import json

from api.jarviz_tasks import TaskConflictError

SPECIALISTS = {
    "coding": ("coding_agent", "Coding Agent"),
    "research": ("research_agent", "Research Agent"),
    "smart_home": ("smart_home_agent", "Smart Home Agent"),
}


def specialist_system_prompt(category: str) -> str:
    if category == "coding":
        return ("You are the JarViz Coding Agent. Complete the assigned coding task with only the "
                "provided file and terminal tools. Return a concise final result for the parent task.")
    if category == "research":
        return ("You are the JarViz Research Agent. Research the assigned question with only the "
                "provided web tools. Return a concise, sourced final result for the parent task.")
    if category == "smart_home":
        return ("You are the JarViz Smart Home Agent. Use only the provided Home Assistant tools. "
                "Read current entity state when needed, make only the requested home change, and "
                "report the affected entity and resulting state. You have no terminal, filesystem, "
                "web, email, or general-purpose tools.")
    raise ValueError("unsupported specialist")


def create_specialist_task(store, parent: dict, category: str, *, request=None,
                           title=None, start=True) -> dict:
    """Create one durable child without creating or changing a WebUI session."""
    try:
        assigned_agent, label = SPECIALISTS[category]
    except KeyError as exc:
        raise ValueError("unsupported specialist") from exc
    metadata = {
        "orchestrator": {
            "version": 1,
            "category": category,
            "toolsets": ({"coding": ["file", "terminal"], "research": ["web"],
                          "smart_home": ["jarviz-smart-home"]}[category]),
        },
        "specialist": {
            "version": 1,
            "kind": category,
            "parent_task_id": parent["task_id"],
        }
    }
    child = store.create_task(
        session_id=parent["session_id"],
        project_id=parent["project_id"],
        parent_task_id=parent["task_id"],
        title=title or f"{label}: {parent['title']}",
        request=request or parent["request"],
        assigned_agent=assigned_agent,
        task_type=category,
        metadata_json=json.dumps(metadata),
    )
    if not start:
        return child
    return store.update_task(child["task_id"], expected_status="queued", status="running")


def project_specialist_outcome(store, child: dict) -> dict:
    """Return the child's result/error to its exact durable parent task."""
    parent_id = child.get("parent_task_id")
    if not parent_id:
        raise ValueError("specialist has no parent task")
    parent = store.get_task(parent_id)
    if (parent["session_id"], parent["project_id"]) != (child["session_id"], child["project_id"]):
        raise ValueError("specialist origin mismatch")
    if child["status"] not in {"completed", "failed", "cancelled", "blocked"}:
        return parent
    if parent["task_type"] == "complex":
        from api.jarviz_complex import reconcile_complex_parent

        return reconcile_complex_parent(store, parent["task_id"])
    changes = {"status": child["status"], "result": child["result"], "error": child["error"]}
    try:
        return store.update_task(parent_id, expected_status="running", **changes)
    except TaskConflictError:
        return store.get_task(parent_id)
