# JarViz Phases 1A–1C: durable Agent Tasks, live notifications, and API

`api.jarviz_tasks.TaskStore` owns JarViz task records and lifecycle history in
`api.config.STATE_DIR / jarviz / jarviz.db`. The existing configuration resolves
`HERMES_WEBUI_STATE_DIR`. An explicit `state_dir` supports isolated tests.
Construction initializes schema version 3. Version 2 adds the
[project extension table](jarviz-project-extensions.md); version 3 adds immutable
root-session and hierarchy-depth task fields. Module import does not create a DB.

Phase 1A adds persistence; Phase 1B publishes committed lifecycle events through
the existing persistent SessionChannel; Phase 1C adds task API routes. These phases
do not start agents or change Hermes Agent, sessions, run journals, or turn journals.
The task store records task intent/outcome; existing journals remain authoritative
for execution and turn history. Future integration may correlate their IDs in
task `metadata_json` without creating replacement execution infrastructure.

## Storage contract

- `jarviz_tasks` contains the requested task fields. IDs are generated UUID hex
  strings; timestamps are UTC Unix seconds. Result/error are nullable text and
  metadata is a JSON object encoded as text.
- `session_id` is required and immutable: it is the originating return address.
  `root_session_id` records that same root return address across descendants.
  `hierarchy_depth` starts at one and child creation is rejected beyond depth three.
  `project_id` is nullable to support existing unassigned sessions. Task identity,
  project, parent, and session cannot be reassigned. A parent must already exist
  and share the same session and project.
- Every creation starts `queued`. Supported statuses are `queued`, `running`,
  `blocked`, `awaiting_approval`, `completed`, `failed`, and `cancelled`.
  Nonterminal tasks can transition among these statuses. Terminal tasks cannot
  change; retrying requires a new task. Exact no-ops do not produce events.
- `started_at` records the first transition to running. `completed_at` records
  any terminal transition, including cancellation before execution.
- `create_task` and `update_task` commit the task and its lifecycle event in one
  SQLite `BEGIN IMMEDIATE` transaction, with foreign keys and synchronous FULL.
  Failure rolls back both; each operation closes its connection. Writers wait
  up to ten seconds for locks, then propagate SQLite errors to the caller.
- Events have an increasing integer cursor, task/project/session identity,
  event type (`task.created`, `task.updated`, `task.started`, `task.blocked`,
  `approval.requested`, `task.completed`, `task.failed`, or `task.cancelled`), prior/current
  status, timestamp, and a complete resulting task snapshot in `payload_json`.
  Events are append-only through this API. No deletion API is provided.
- `expected_status` enables atomic compare-and-set claims. Stale claims and
  attempts to modify terminal tasks raise `TaskConflictError`; missing tasks
  raise `KeyError`; invalid input raises `ValueError`.
- Schema initialization is transactional and repeatable; unknown schema versions
  fail rather than downgrade. Future migrations must explicitly advance it.

`get_task`, `list_tasks`, and `list_events` read committed state. This internal
module does not validate external session/project existence or provide user/profile
authorization. Route adapters must authorize against existing sessions before
accessing it.

## Live delivery contract

After a successful create/update transaction commits and releases its connection,
the store emits `jarviz_task_event` on the existing channel obtained through
`api.background_process.get_session_channel(event["session_id"])`.
The envelope is:

```json
{
  "schema_version": 1,
  "event_type": "task.completed",
  "task_id": "task-id",
  "project_id": "project-id",
  "session_id": "originating-session-id",
  "created_at": 1789800000.0,
  "payload": {}
}
```

Envelope fields come from the inserted database event; `created_at` remains Unix
seconds and `payload` is its decoded task snapshot with public-content redaction.
The immutable
originating session is the only routing authority. Task metadata, current UI
selection, and other sessions in the same project never determine the destination.
A channel-owner mismatch fails closed. All subscribed tabs of the origin receive
the notification via the existing channel broadcast.

Rolled-back mutations, rejected updates, and no-ops emit nothing. Missing channels,
full subscriber queues, or delivery exceptions do not undo successful commits or
reroute results. No channels are created just to publish. A process exit between
commit and emission can lose a live notification; the committed database history
remains available through `list_events`. This is best-effort in-process delivery,
not a new replay system or a cross-process message bus. Concurrent notifications
are not guaranteed to arrive in commit order; durable history is authoritative.

The existing `/api/session/stream` forwards the event. `/api/chat/stream` termination
semantics and `SSE_RELAY_CLOSE_EVENTS` are unchanged. There is no frontend wiring
or automatic JarViz replay in this phase.

## HTTP API (Phase 1C)

Routes reuse the existing WebUI dispatcher, authentication, request profile context,
JSON helpers, and POST CSRF gate. No new server or frontend is introduced.

| Method and path | Behavior |
| --- | --- |
| `POST /api/jarviz/tasks` | Create a queued task; returns `201 {"ok": true, "task": {...}}`. |
| `GET /api/jarviz/tasks` | Return `200 {"tasks": [...]}` scoped to the active profile. Optional filters: `session_id`, `project_id`, `status`. |
| `GET /api/jarviz/tasks/<task_id>` | Return `200 {"task": {...}}` for a visible task. |
| `POST /api/jarviz/tasks/<task_id>/update` | Update lifecycle/content; returns `200 {"ok": true, "task": {...}}`. Uses the repo's POST-based update convention. |

Create requires `session_id`, `title`, and `request`. Optional fields are
`project_id`, `parent_task_id`, `assigned_agent`, `task_type`, and `metadata_json`
(a JSON object encoded as a string). If supplied, `status` must be `queued`.
Omitting `project_id` inherits the session's project; an explicit value must match
that project. Unassigned sessions may create tasks with a null project.

Update accepts `title`, `request`, `status`, `assigned_agent`, `task_type`, `result`,
`error`, `metadata_json`, and optional `expected_status`. Origin/identity fields
and profile overrides are rejected. A nonempty mutation is required. The storage
contract above still governs statuses, timestamps, no-ops, and terminal tasks.

The adapter snapshots the active request profile and uses existing session/project
metadata plus `_profiles_match` (including legacy root aliases) as its ownership
authority. Session existence is checked explicitly: the generic request guard's
missing-session pass-through is insufficient here. Every task in a list is checked,
including when no filters are supplied. A task's session and non-null project must
both exist and belong to that profile. Parent tasks must also be visible and meet
the store's same-session/project rule. There is no `all_profiles` bypass.
Deleted or invisible owners hide the task; immutable task origins are not rewritten
when a session's project association later changes.

Invalid fields/statuses/filters return 400; missing and foreign resources share a
404 response; terminal/stale-status conflicts return 409; SQLite failures return
503; other internal failures return 500. Errors use fixed messages, never exception
strings or tracebacks. Duplicate/blank filters and unknown fields are rejected.

`api.jarviz_public.public_task` projects task content for both HTTP and live channel
payloads. Credential redaction is forced even if general API redaction is disabled.
It filters sensitive nested metadata/JSON keys, credential assignments, recognized
credential formats, known secret environment values, and traceback text. Routing
identity and timestamps remain unchanged. Original data remains in the local
database; redaction changes only public projections.

## Focused verification

Run `./scripts/test.sh --noconftest tests/test_jarviz_tasks.py tests/test_jarviz_task_events.py tests/test_jarviz_task_api.py -q`
with isolated `HERMES_HOME`, `HERMES_WEBUI_STATE_DIR`, and `HERMES_WEBUI_AGENT_DIR`.
For Agent-free tests, point the last variable at a disposable directory containing
an empty `run_agent.py` discovery sentinel. No Agent is imported from that file or
executed. Set `HERMES_WEBUI_TEST_PYTHON` to an available supported interpreter when
needed. A writable `--basetemp` and `-p no:cacheprovider` can avoid Windows cache ACL
issues.
These tests use temporary databases and do not require the shared conftest's
server/Agent fixtures. They cover durable reopen, origin preservation, parent
constraints, statuses/timestamps, query cursors, real event-insert failure rollback,
competing claims, input rejection, and schema version protection.
Phase 1B tests use real SessionChannel subscriptions for correct-session delivery,
cross-session isolation, multi-tab broadcast, post-commit visibility, rollback
silence, and delivery-failure durability.
API tests execute the real GET/POST dispatcher and JSON/CSRF/profile helpers with
fixture session/project lookups and temporary SQLite databases. They cover ownership
isolation, filtering, validation, lifecycle updates, fixed errors, and forced
HTTP/live redaction. They do not start an HTTP listener or an agent.
