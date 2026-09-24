# JarViz project extensions

Hermes `projects.json` remains authoritative for project identity, name, color,
and profile ownership. Existing `Session.project_id` remains the only project/session
relationship. Existing `jarviz_tasks.project_id` remains the task association,
queryable through `GET /api/jarviz/tasks?project_id=...`. No additional membership
lists or JarViz project creation API are introduced.

## Storage

Schema version 2 of `<HERMES_WEBUI_STATE_DIR>/jarviz/jarviz.db` adds one table,
`jarviz_project_details`, keyed by the existing Hermes `project_id`. Version 1
databases upgrade transactionally without changing task/event records. The new
table stores only:

- `root_workspace`: an absolute local/Windows path reference, or null.
- `metadata_json`: a JSON object for descriptive project metadata.
- `artifacts_json`: an array of reference objects, each requiring `reference`.
- `blockers_json` and `decisions_json`: arrays of objects, each requiring `text`.
- `updated_at`: UTC Unix seconds of the last extension update.

JSON fields are presented as `metadata`, `artifacts`, `blockers`, and `decisions`
in the backend/API. They can contain descriptive fields such as labels, reasons,
and timestamps. An optional `task_id` on an artifact/blocker/decision must reference
an existing task in this project whose originating session belongs to the active
profile. It is a link, not another task membership or lifecycle mechanism.

`api.jarviz_projects.ProjectStore` reuses the existing TaskStore database initializer,
connection settings, and transaction context. Its `get_project(project_id)` and
`update_project(project_id, **changes)` always check the existing Hermes project and
current profile using the existing `_profiles_match` helper. Missing or foreign
projects cannot acquire extension records. Legacy projects return empty defaults
without inserting a row. Returned objects preserve existing project fields and
add one `jarviz` extension object.

Updates patch only supplied top-level fields; supplied objects/arrays replace that
field rather than deep merge it. Omitted fields are preserved. Clear fields using
null for `root_workspace`, `{}` for metadata, and `[]` for reference arrays. SQLite
serializes read/merge/write, preventing concurrent updates to different fields
from discarding each other. Concurrent writes to the same field are last-writer-wins.
Invalid input or a failed write leaves the previous extension intact.

No writes to `projects.json` are needed for extensions, so existing project create,
rename, and profile migration cannot overwrite extension data. Deleted projects
make their extensions inaccessible; sidecar rows are retained, like task history.
There is no cross-database foreign key to the JSON registry or automatic deletion
hook in this increment. Access rechecks the registry rather than trusting a cached
or duplicated project owner.

## API

These routes extend the existing `/api/projects` namespace and use the existing
WebUI authentication, request profile, and POST CSRF handling:

- `GET /api/projects/<project_id>/jarviz`
- `POST /api/projects/<project_id>/jarviz/update`

Both return `200 {"project": {existing project fields, "jarviz": {...}}}`.
The POST body contains one or more of `root_workspace`, `metadata`, `artifacts`,
`blockers`, or `decisions`. Identity/name/profile/membership changes are rejected;
continue using existing Hermes routes for those operations. Unknown fields and
invalid values return 400; hidden/missing resources return 404; wrong methods
return 405; storage failures return 503. Internal errors use fixed messages.
Public extension content uses the same forced secret/traceback redaction as tasks.
The existing project listing and UI are unchanged.

Example update body:

```json
{
  "root_workspace": "C:\\JarViz\\workspace",
  "metadata": {"description": "Local assistant development"},
  "artifacts": [{"reference": "reports/design.md", "label": "Design notes"}],
  "blockers": [{"text": "Waiting for voice integration"}],
  "decisions": [{"text": "Keep Hermes as the execution engine"}]
}
```

## Deliberate scope

Root workspace and artifacts are inert references: no paths are created, opened,
fetched, moved, or used to override existing session workspaces. They grant no
permissions. Project memory retrieval, policy enforcement, agent orchestration,
artifact serving, and UI redesign are outside this increment. Descriptive metadata
must not be interpreted as executable policy or user approval.

## Focused verification

Add `tests/test_jarviz_projects.py` to the focused command documented in
[JarViz task persistence](jarviz-task-persistence.md#focused-verification), using
isolated state and the Agent discovery sentinel. Tests cover real project JSON,
SQLite persistence/migration, existing project rename/list compatibility, task
links, profile isolation, CSRF, redaction, rollback, and concurrent partial updates.
