"""Bounded Phase 2C task planning and parent reconciliation."""
from __future__ import annotations

import json

from api.jarviz_tasks import TERMINAL_STATUSES, TaskConflictError


def create_complex_children(store, parent: dict) -> list[dict]:
    """Create the fixed Phase 2C research/coding plan at depth two."""
    from api.jarviz_specialists import create_specialist_task

    specs = (
        ("research", "Research required for the parent request:\n" + parent["request"]),
        ("coding", "Implement the parent request using the available workspace:\n" + parent["request"]),
    )
    children = [
        create_specialist_task(store, parent, category, request=request,
                               title=f"{category.title()} step: {parent['title']}", start=False)
        for category, request in specs
    ]
    metadata = json.loads(parent["metadata_json"])
    metadata["orchestrator"]["required_child_ids"] = [child["task_id"] for child in children]
    metadata["orchestrator"]["max_depth"] = 3
    metadata["orchestrator"]["execution"] = "parallel"
    store.update_task(parent["task_id"], expected_status="running",
                      metadata_json=json.dumps(metadata))
    return children


def reconcile_complex_parent(store, parent_task_id: str) -> dict:
    """Settle only from the parent's frozen required-child set."""
    parent = store.get_task(parent_task_id)
    metadata = json.loads(parent["metadata_json"])
    required_ids = metadata.get("orchestrator", {}).get("required_child_ids") or []
    children_by_id = {child["task_id"]: child for child in store.list_children(parent_task_id)}
    if not required_ids or any(task_id not in children_by_id for task_id in required_ids):
        raise ValueError("complex task plan is incomplete")
    children = [children_by_id[task_id] for task_id in required_ids]
    if any((child["root_session_id"], child["project_id"])
           != (parent["root_session_id"], parent["project_id"]) for child in children):
        raise ValueError("complex child origin mismatch")

    blocked = [child for child in children if child["status"] == "blocked"]
    awaiting = [child for child in children if child["status"] == "awaiting_approval"]
    failures = [child for child in children if child["status"] in {"failed", "cancelled"}]
    pending = [child for child in children if child["status"] not in TERMINAL_STATUSES]
    changes = None
    if blocked:
        changes = {"status": "blocked", "error": _failure_summary(blocked, "blocked")}
    elif awaiting:
        changes = {"status": "awaiting_approval",
                   "error": "A required child task is awaiting approval."}
    elif pending:
        # Surface known failures while required siblings finish, but do not
        # mark the parent terminal before every required child is terminal.
        if failures:
            changes = {"status": "running", "error": _failure_summary(failures, "failed")}
    elif failures:
        changes = {"status": "failed", "error": _failure_summary(failures, "failed")}
    else:
        result = "\n\n".join(
            f"## {child['title']}\n{child['result'] or ''}" for child in children
        )
        changes = {"status": "completed", "result": result, "error": None}
    if changes is None:
        return parent
    try:
        return store.update_task(parent_task_id, **changes)
    except TaskConflictError:
        return store.get_task(parent_task_id)


def _failure_summary(children: list[dict], state: str) -> str:
    details = "; ".join(
        f"{child['title']}: {child['error'] or child['status']}" for child in children
    )
    return f"Required child task {state}: {details}"
