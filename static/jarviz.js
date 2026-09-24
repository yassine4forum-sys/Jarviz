/* JarViz activity view. SQLite/API state is authoritative; SSE only accelerates updates. */
const JARVIZ_ACTIVE = new Set(['queued', 'running', 'blocked', 'awaiting_approval']);
let _jarvizSessionId = null;
let _jarvizTasks = new Map();
let _jarvizProjects = new Map();
let _jarvizLoadGeneration = 0;
let _jarvizLoadPromise = null;
let _jarvizError = '';

function _jarvizCurrentSessionId(explicit) {
  if (explicit) return String(explicit);
  try { return S && S.session && String(S.session.session_id || ''); } catch (_) { return ''; }
}

async function loadJarvizTasks(sessionId, force) {
  const sid = _jarvizCurrentSessionId(sessionId);
  if (!sid) {
    _jarvizSessionId = null;
    _jarvizTasks = new Map();
    _jarvizProjects = new Map();
    _jarvizError = '';
    renderJarvizActivity();
    return [];
  }
  if (!force && _jarvizSessionId === sid && _jarvizLoadPromise) return _jarvizLoadPromise;
  const generation = ++_jarvizLoadGeneration;
  const sessionChanged = _jarvizSessionId !== sid;
  if (sessionChanged) {
    // Never leave the previous session's tasks visible while the new durable
    // snapshot is loading.
    _jarvizTasks = new Map();
    _jarvizProjects = new Map();
  }
  _jarvizSessionId = sid;
  _jarvizError = '';
  if (sessionChanged) renderJarvizActivity();
  const run = (async () => {
    try {
      const response = await fetch(_apiUrl('api/jarviz/tasks?session_id=' + encodeURIComponent(sid)), {
        credentials: 'same-origin', cache: 'no-store',
      });
      if (!response.ok) throw new Error('Agent task state is unavailable.');
      const payload = await response.json();
      if (generation !== _jarvizLoadGeneration || sid !== _jarvizSessionId) return [];
      const tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
      _jarvizTasks = new Map(tasks.map(task => [String(task.task_id), task]));
      await _loadJarvizProjectDetails(tasks, generation, sid);
      if (generation === _jarvizLoadGeneration && sid === _jarvizSessionId) renderJarvizActivity();
      return tasks;
    } catch (_) {
      if (generation === _jarvizLoadGeneration && sid === _jarvizSessionId) {
        _jarvizError = 'Could not load durable agent task state.';
        renderJarvizActivity();
      }
      return [];
    }
  })();
  _jarvizLoadPromise = run.finally(() => {
    if (generation === _jarvizLoadGeneration) _jarvizLoadPromise = null;
  });
  return _jarvizLoadPromise;
}

async function _loadJarvizProjectDetails(tasks, generation, sid) {
  const ids = [...new Set(tasks.map(task => task.project_id).filter(Boolean))];
  const loaded = await Promise.all(ids.map(async projectId => {
    try {
      const response = await fetch(_apiUrl('api/projects/' + encodeURIComponent(projectId) + '/jarviz'), {
        credentials: 'same-origin', cache: 'no-store',
      });
      if (!response.ok) return [projectId, null];
      const payload = await response.json();
      return [projectId, payload.project && payload.project.jarviz];
    } catch (_) { return [projectId, null]; }
  }));
  if (generation !== _jarvizLoadGeneration || sid !== _jarvizSessionId) return;
  _jarvizProjects = new Map(loaded.filter(([, details]) => details));
}

function handleJarvizTaskEvent(event, streamSessionId) {
  try {
    const envelope = JSON.parse(event && event.data || '{}');
    const sid = String(streamSessionId || '');
    if (envelope.schema_version !== 1 || String(envelope.session_id || '') !== sid) return;
    if (sid !== _jarvizCurrentSessionId() || sid !== _jarvizSessionId) return;
    const task = envelope.payload;
    if (!task || String(task.session_id || '') !== sid || String(task.task_id || '') !== String(envelope.task_id || '')) return;
    _jarvizTasks.set(String(task.task_id), task);
    renderJarvizActivity();
    if (window.JarVizLive) window.JarVizLive.onTaskEvent(envelope);
  } catch (_) {}
}

function renderJarvizActivity() {
  renderJarvizSessionActivity();
  const body = document.getElementById('jarvizActivityBody');
  const sidebar = document.getElementById('jarvizSidebarList');
  const summary = document.getElementById('jarvizSidebarSummary');
  if (!body || !sidebar || !summary) return;
  body.replaceChildren();
  sidebar.replaceChildren();
  const tasks = [..._jarvizTasks.values()].sort((a, b) => Number(a.created_at) - Number(b.created_at));
  if (!_jarvizSessionId) {
    summary.textContent = 'Select a conversation to view its tasks.';
    body.append(_jarvizEmpty('Select a conversation', 'Agent tasks are scoped to their originating JarViz session.'));
    return;
  }
  const running = tasks.filter(task => task.status === 'running');
  const completed = tasks.filter(task => task.status === 'completed');
  const attention = tasks.filter(task => ['blocked', 'awaiting_approval', 'failed'].includes(task.status));
  summary.textContent = `${running.length} active · ${attention.length} need attention · ${completed.length} completed`;
  _renderJarvizSidebar(sidebar, running, attention);
  if (_jarvizError) body.append(_jarvizNotice(_jarvizError, 'error'));
  body.append(_jarvizStats(tasks));
  const blockers = _jarvizProjectEntries('blockers');
  if (blockers.length) body.append(_jarvizProjectNotice('Project blockers', blockers));
  if (!tasks.length && !_jarvizError) {
    body.append(_jarvizEmpty('No agent tasks yet', 'Durable tasks for this conversation will appear here.'));
    return;
  }
  const activeSection = _jarvizSection('Task hierarchy', 'Active agents, blockers, approvals, and errors');
  const childIds = new Set(tasks.filter(task => task.parent_task_id).map(task => String(task.task_id)));
  const roots = tasks.filter(task => !childIds.has(String(task.task_id)) && task.status !== 'completed');
  const byParent = new Map();
  tasks.forEach(task => {
    const key = task.parent_task_id && String(task.parent_task_id);
    if (!key) return;
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(task);
  });
  const tree = document.createElement('div');
  tree.className = 'jarviz-task-tree';
  roots.forEach(task => tree.append(_jarvizTaskNode(task, byParent, 1)));
  if (!roots.length) tree.append(_jarvizEmpty('No active tasks', 'All required work for this conversation is complete.'));
  activeSection.append(tree);
  body.append(activeSection);

  const completedSection = _jarvizSection('Completed tasks', `${completed.length} durable result${completed.length === 1 ? '' : 's'}`);
  const completedList = document.createElement('div');
  completedList.className = 'jarviz-completed-list';
  completed.slice().sort((a, b) => Number(b.completed_at || 0) - Number(a.completed_at || 0))
    .forEach(task => completedList.append(_jarvizTaskCard(task, true)));
  if (!completed.length) completedList.append(_jarvizEmpty('Nothing completed yet', 'Finished tasks and their artifacts will remain available here.'));
  completedSection.append(completedList);
  body.append(completedSection);
}

// Keep the transcript concise: derive only user-meaningful milestones from the
// durable task snapshot. Tool/run detail remains in the Activity view.
function _jarvizImportantEvents(tasks) {
  const byId = new Map(tasks.map(task => [String(task.task_id), task]));
  const events = [];
  const add = (task, kind, label, at, detail, detailLabel) => {
    if (!at) return;
    events.push({ task, kind, label, at: Number(at), detail: detail || '', detailLabel: detailLabel || '' });
  };
  tasks.forEach(task => {
    const parent = task.parent_task_id && byId.get(String(task.parent_task_id));
    if (!task.parent_task_id && task.started_at) {
      add(task, 'started', 'Task started', task.started_at);
    }
    if (task.parent_task_id && task.assigned_agent) {
      add(task, 'delegated', 'Agent delegated', task.created_at);
    }
    if (task.status === 'blocked' && (!parent || parent.status !== 'blocked')) {
      add(task, 'blocked', 'Blocked', task.updated_at, task.error, 'Blocker');
    }
    if (task.status === 'awaiting_approval' && (!parent || parent.status !== 'awaiting_approval')) {
      add(task, 'approval', 'Approval required', task.updated_at, task.error, 'Details');
    }
    if (task.status === 'failed' && (!parent || parent.status !== 'failed')) {
      add(task, 'failed', 'Task failed', task.completed_at || task.updated_at, task.error, 'Error');
    }
    if (task.status === 'completed' && !task.parent_task_id) {
      add(task, 'completed', 'Task completed', task.completed_at || task.updated_at, task.result, 'Result');
    }
  });
  events.sort((a, b) => a.at - b.at || String(a.task.task_id).localeCompare(String(b.task.task_id)) || a.kind.localeCompare(b.kind));
  return events.slice(-40);
}

function renderJarvizSessionActivity() {
  const inner = document.getElementById('msgInner');
  if (!inner) return;
  const previous = document.getElementById('jarvizSessionActivity');
  if (previous) previous.remove();
  const sid = _jarvizCurrentSessionId();
  if (!sid || sid !== _jarvizSessionId) return;
  const allEvents = _jarvizImportantEvents([..._jarvizTasks.values()]);
  if (!allEvents.length) return;

  const region = document.createElement('section');
  region.id = 'jarvizSessionActivity';
  region.className = 'jarviz-transcript-activity';
  region.setAttribute('aria-label', 'JarViz activity');
  const header = document.createElement('div'); header.className = 'jarviz-transcript-header';
  const title = document.createElement('strong'); title.textContent = 'JarViz activity';
  const hint = document.createElement('span'); hint.textContent = 'Details in Activity';
  header.append(title, hint); region.append(header);
  allEvents.forEach(event => {
    const card = document.createElement('article');
    card.className = 'jarviz-transcript-event event-' + event.kind;
    const main = document.createElement('div'); main.className = 'jarviz-transcript-event-main';
    const label = document.createElement('strong'); label.textContent = event.label;
    const taskTitle = document.createElement('span');
    const agent = event.kind === 'delegated' && event.task.assigned_agent
      ? event.task.assigned_agent.replaceAll('_', ' ') + ' · ' : '';
    taskTitle.textContent = agent + (event.task.title || 'Untitled task');
    const time = document.createElement('time');
    const date = new Date(event.at * 1000);
    time.dateTime = Number.isNaN(date.getTime()) ? '' : date.toISOString();
    time.textContent = Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    main.append(label, taskTitle, time); card.append(main);
    if (event.detail) {
      const details = document.createElement('details');
      const summary = document.createElement('summary'); summary.textContent = event.detailLabel;
      const content = document.createElement('pre'); content.textContent = event.detail;
      details.append(summary, content); card.append(details);
    }
    region.append(card);
  });
  if ([..._jarvizTasks.values()].length > allEvents.length) {
    const note = document.createElement('div'); note.className = 'jarviz-transcript-note';
    note.textContent = 'Detailed agent and tool activity is available in Activity.'; region.append(note);
  }
  inner.append(region);
}

function _renderJarvizSidebar(root, running, attention) {
  const agents = running.filter(task => task.assigned_agent);
  const heading = document.createElement('div');
  heading.className = 'jarviz-sidebar-heading';
  heading.textContent = 'Active agents';
  root.append(heading);
  if (!agents.length) root.append(_jarvizSidebarRow('No active agents', 'Idle'));
  agents.forEach(task => root.append(_jarvizSidebarRow(task.assigned_agent, _jarvizElapsed(task))));
  if (attention.length) {
    const alertHeading = heading.cloneNode();
    alertHeading.textContent = 'Needs attention';
    root.append(alertHeading);
    attention.forEach(task => root.append(_jarvizSidebarRow(task.title, _jarvizStatusLabel(task.status))));
  }
}

function _jarvizSidebarRow(title, meta) {
  const row = document.createElement('div');
  row.className = 'jarviz-sidebar-row';
  const strong = document.createElement('strong'); strong.textContent = title || 'Agent';
  const span = document.createElement('span'); span.textContent = meta || '';
  row.append(strong, span);
  return row;
}

function _jarvizStats(tasks) {
  const stats = document.createElement('div');
  stats.className = 'jarviz-stats';
  const values = [
    ['Active agents', tasks.filter(task => task.status === 'running' && task.assigned_agent).length],
    ['Queued', tasks.filter(task => task.status === 'queued').length],
    ['Attention', tasks.filter(task => ['blocked', 'awaiting_approval', 'failed'].includes(task.status)).length],
    ['Completed', tasks.filter(task => task.status === 'completed').length],
  ];
  values.forEach(([label, value]) => {
    const card = document.createElement('div'); card.className = 'jarviz-stat';
    const number = document.createElement('strong'); number.textContent = String(value);
    const caption = document.createElement('span'); caption.textContent = label;
    card.append(number, caption); stats.append(card);
  });
  return stats;
}

function _jarvizTaskNode(task, byParent, depth) {
  const node = document.createElement('div');
  node.className = 'jarviz-task-node';
  node.style.setProperty('--jarviz-depth', String(Math.max(0, depth - 1)));
  node.append(_jarvizTaskCard(task, false));
  const children = byParent.get(String(task.task_id)) || [];
  children.forEach(child => node.append(_jarvizTaskNode(child, byParent, depth + 1)));
  return node;
}

function _jarvizTaskCard(task, compact) {
  const card = document.createElement('article');
  card.className = 'jarviz-task-card status-' + String(task.status || 'queued');
  const top = document.createElement('div'); top.className = 'jarviz-task-top';
  const title = document.createElement('div'); title.className = 'jarviz-task-title'; title.textContent = task.title || 'Untitled task';
  const badge = document.createElement('span'); badge.className = 'jarviz-status'; badge.textContent = _jarvizStatusLabel(task.status);
  top.append(title, badge); card.append(top);
  const meta = document.createElement('div'); meta.className = 'jarviz-task-meta';
  const parts = [];
  if (task.assigned_agent) parts.push(task.assigned_agent.replaceAll('_', ' '));
  if (task.task_type) parts.push(task.task_type);
  parts.push(_jarvizElapsed(task));
  meta.textContent = parts.join(' · '); card.append(meta);
  if (!compact && task.request) {
    const request = document.createElement('div'); request.className = 'jarviz-task-request'; request.textContent = task.request;
    card.append(request);
  }
  if (task.status === 'awaiting_approval') card.append(_jarvizNotice('Approval required before this task can continue.', 'approval'));
  if (task.status === 'blocked') card.append(_jarvizNotice(task.error || 'This task is blocked.', 'blocked'));
  if (task.status === 'failed') card.append(_jarvizNotice(task.error || 'This task failed.', 'error'));
  if (task.status === 'cancelled') card.append(_jarvizNotice(task.error || 'This task was cancelled.', 'muted'));
  const artifacts = _jarvizArtifactsForTask(task);
  if (artifacts.length) {
    const group = document.createElement('div'); group.className = 'jarviz-artifacts';
    const label = document.createElement('strong'); label.textContent = 'Artifacts'; group.append(label);
    artifacts.forEach(artifact => {
      const item = document.createElement('span');
      item.textContent = artifact.reference || artifact.path || artifact.name || String(artifact);
      group.append(item);
    });
    card.append(group);
  }
  return card;
}

function _jarvizArtifactsForTask(task) {
  const entries = [];
  try {
    const metadata = JSON.parse(task.metadata_json || '{}');
    if (Array.isArray(metadata.artifacts)) entries.push(...metadata.artifacts);
  } catch (_) {}
  const project = task.project_id && _jarvizProjects.get(task.project_id);
  if (project && Array.isArray(project.artifacts)) {
    entries.push(...project.artifacts.filter(item => !item.task_id || String(item.task_id) === String(task.task_id)));
  }
  return entries;
}

function _jarvizProjectEntries(field) {
  const entries = [];
  _jarvizProjects.forEach(project => {
    if (Array.isArray(project[field])) entries.push(...project[field]);
  });
  return entries;
}

function _jarvizProjectNotice(title, entries) {
  const notice = document.createElement('section'); notice.className = 'jarviz-project-notice';
  const heading = document.createElement('strong'); heading.textContent = title; notice.append(heading);
  entries.forEach(entry => {
    const item = document.createElement('span'); item.textContent = entry.text || entry.reference || String(entry);
    notice.append(item);
  });
  return notice;
}

function _jarvizSection(title, subtitle) {
  const section = document.createElement('section'); section.className = 'jarviz-section';
  const header = document.createElement('div'); header.className = 'jarviz-section-header';
  const h = document.createElement('h2'); h.textContent = title;
  const sub = document.createElement('span'); sub.textContent = subtitle;
  header.append(h, sub); section.append(header); return section;
}

function _jarvizNotice(text, kind) {
  const notice = document.createElement('div');
  notice.className = 'jarviz-notice ' + (kind || 'muted');
  notice.textContent = text;
  return notice;
}

function _jarvizEmpty(title, subtitle) {
  const empty = document.createElement('div'); empty.className = 'jarviz-empty';
  const strong = document.createElement('strong'); strong.textContent = title;
  const span = document.createElement('span'); span.textContent = subtitle;
  empty.append(strong, span); return empty;
}

function _jarvizStatusLabel(status) {
  return String(status || 'queued').replaceAll('_', ' ');
}

function _jarvizElapsed(task) {
  const start = Number(task.started_at || task.created_at || 0);
  if (!start) return '0s';
  const end = Number(task.completed_at || Date.now() / 1000);
  let seconds = Math.max(0, Math.floor(end - start));
  const days = Math.floor(seconds / 86400); seconds %= 86400;
  const hours = Math.floor(seconds / 3600); seconds %= 3600;
  const minutes = Math.floor(seconds / 60); seconds %= 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

setInterval(() => {
  const panel = document.getElementById('mainActivity');
  if (panel && panel.offsetParent !== null && [..._jarvizTasks.values()].some(task => JARVIZ_ACTIVE.has(task.status))) {
    renderJarvizActivity();
  }
}, 1000);
