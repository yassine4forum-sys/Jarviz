/* Native Gemini Live voice for JarViz. The durable WebUI session remains the
   identity anchor; Gemini only receives the fixed control functions provisioned
   by the server. */
(function () {
  'use strict';

  const STATE = {
    active: false, sessionId: '', projectId: null, socket: null, stream: null,
    inputContext: null, inputSource: null, processor: null, outputContext: null,
    outputAt: 0, generation: 0, sessionWatch: 0,
    callResults: new Map(), lifecycleEvents: new Set(), persona: null,
  };
  const IMPORTANT_STATUSES = new Set(['blocked', 'awaiting_approval', 'completed', 'failed', 'cancelled']);

  const el = id => document.getElementById(id);
  const currentSession = () => (window.S && S.session) || null;

  function setState(state, text) {
    const bar = el('voiceModeBar');
    const indicator = el('voiceModeIndicator');
    const label = el('voiceModeLabel');
    const button = el('btnVoiceMode');
    if (bar) bar.style.display = STATE.active ? '' : 'none';
    if (indicator) indicator.dataset.state = state;
    if (label) label.textContent = text || '';
    if (button) {
      button.classList.toggle('active', STATE.active);
      button.setAttribute('aria-pressed', STATE.active ? 'true' : 'false');
      button.dataset.tooltip = STATE.active ? 'End Gemini Live' : 'Gemini Live';
      button.title = button.dataset.tooltip;
    }
  }

  function preferenceEnabled() {
    try { return localStorage.getItem('hermes-voice-mode-button') === 'true'; }
    catch (_) { return false; }
  }

  function applyPreference() {
    const button = el('btnVoiceMode');
    if (button) button.style.display = preferenceEnabled() ? '' : 'none';
    if (!preferenceEnabled() && STATE.active) void deactivate();
  }

  function bytesToBase64(bytes) {
    let binary = '';
    const size = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += size) {
      binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + size));
    }
    return btoa(binary);
  }

  function base64ToInt16(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return new Int16Array(bytes.buffer);
  }

  function resampleToPcm16(input, inputRate, outputRate) {
    const ratio = inputRate / outputRate;
    const length = Math.max(1, Math.floor(input.length / ratio));
    const pcm = new Int16Array(length);
    for (let i = 0; i < length; i += 1) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j += 1) sum += input[j];
      const sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
      pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    return pcm;
  }

  function send(message) {
    if (STATE.socket && STATE.socket.readyState === WebSocket.OPEN) {
      STATE.socket.send(JSON.stringify(message));
      return true;
    }
    return false;
  }

  async function startMicrophone(generation) {
    if (generation !== STATE.generation || !STATE.active || !STATE.stream) return;
    const AudioCtor = window.AudioContext || window.webkitAudioContext;
    STATE.inputContext = new AudioCtor();
    await STATE.inputContext.resume();
    if (generation !== STATE.generation) return;
    STATE.inputSource = STATE.inputContext.createMediaStreamSource(STATE.stream);
    STATE.processor = STATE.inputContext.createScriptProcessor(4096, 1, 1);
    STATE.processor.onaudioprocess = event => {
      if (!STATE.active) return;
      const pcm = resampleToPcm16(event.inputBuffer.getChannelData(0), STATE.inputContext.sampleRate, 16000);
      send({ realtimeInput: { audio: { data: bytesToBase64(new Uint8Array(pcm.buffer)), mimeType: 'audio/pcm;rate=16000' } } });
    };
    STATE.inputSource.connect(STATE.processor);
    STATE.processor.connect(STATE.inputContext.destination);
    setState('listening', 'Gemini Live · listening');
  }

  async function playAudio(data, mimeType) {
    if (!STATE.active || !data) return;
    const match = /rate=(\d+)/i.exec(mimeType || '');
    const rate = match ? Number(match[1]) : 24000;
    const pcm = base64ToInt16(data);
    if (!STATE.outputContext) {
      const AudioCtor = window.AudioContext || window.webkitAudioContext;
      STATE.outputContext = new AudioCtor({ sampleRate: rate });
    }
    await STATE.outputContext.resume();
    const buffer = STATE.outputContext.createBuffer(1, pcm.length, rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
    const source = STATE.outputContext.createBufferSource();
    source.buffer = buffer;
    source.connect(STATE.outputContext.destination);
    const start = Math.max(STATE.outputContext.currentTime, STATE.outputAt);
    source.start(start);
    STATE.outputAt = start + buffer.duration;
    setState('speaking', 'Gemini Live · speaking');
    source.onended = () => {
      if (STATE.active && STATE.outputContext && STATE.outputContext.currentTime >= STATE.outputAt - 0.05) {
        setState('listening', 'Gemini Live · listening');
      }
    };
  }

  async function approvalControl(name, directive) {
    const pendingPayload = await api('/api/approval/pending?session_id=' + encodeURIComponent(STATE.sessionId), { timeoutToast: false });
    const pending = pendingPayload && pendingPayload.pending;
    if (!pending || String(pending.approval_id || '') !== String(directive.approval_id || '')) {
      throw new Error('That approval is no longer pending.');
    }
    return api('/api/approval/respond', {
      method: 'POST',
      body: JSON.stringify({
        session_id: STATE.sessionId,
        approval_id: directive.approval_id,
        choice: name === 'approve_action' ? 'once' : 'deny',
        run_id: pending.run_id || '',
        mirror_token: pending.mirror_token || '',
      }),
    });
  }

  async function switchProject(projectId) {
    if (typeof _setActiveProjectFilter === 'function') _setActiveProjectFilter(projectId);
    const sessions = typeof _allSessions !== 'undefined' && Array.isArray(_allSessions) ? _allSessions : [];
    const target = sessions.filter(item => item && item.project_id === projectId)
      .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))[0];
    if (!target || typeof loadSession !== 'function') {
      return { switched: false, project_id: projectId, message: 'Project selected; it has no existing session to open.' };
    }
    return {
      switched: true, project_id: projectId, available_session_id: target.session_id,
      message: 'Project selected. The current voice call remains anchored to its originating session.',
    };
  }

  async function runControl(call) {
    const name = String(call.name || '');
    const args = call.args && typeof call.args === 'object' ? call.args : {};
    const payload = await api('/api/jarviz/live/control', {
      method: 'POST',
      body: JSON.stringify({
        session_id: STATE.sessionId, project_id: STATE.projectId, name, args,
      }),
    });
    const result = payload.result || {};
    if (name === 'update_persona' && result.persona) STATE.persona = result.persona;
    if (name === 'approve_action' || name === 'reject_action') {
      result.response = await approvalControl(name, result.approval || {});
    } else if (name === 'switch_project') {
      result.navigation = await switchProject(args.project_id);
    }
    return result;
  }

  async function handleToolCalls(message) {
    const calls = message.toolCall && message.toolCall.functionCalls;
    if (!Array.isArray(calls)) return;
    setState('working', 'Gemini Live · contacting JarViz');
    const responses = [];
    for (const call of calls) {
      const callId = String(call.id || '');
      if (callId && STATE.callResults.has(callId)) {
        responses.push(STATE.callResults.get(callId));
        continue;
      }
      let response;
      try {
        response = { id: call.id, name: call.name, response: { result: await runControl(call) } };
      } catch (error) {
        response = { id: call.id, name: call.name, response: { error: String(error && error.message || 'Control failed') } };
      }
      responses.push(response);
      if (callId) STATE.callResults.set(callId, response);
    }
    send({ toolResponse: { functionResponses: responses } });
  }

  function handleMessage(event, generation) {
    if (generation !== STATE.generation || !STATE.active) return;
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.setupComplete) {
      void startMicrophone(generation);
      return;
    }
    if (message.toolCall) void handleToolCalls(message);
    const content = message.serverContent;
    const parts = content && content.modelTurn && content.modelTurn.parts;
    if (Array.isArray(parts)) parts.forEach(part => {
      if (part.inlineData && part.inlineData.data) void playAudio(part.inlineData.data, part.inlineData.mimeType);
    });
    if (content && content.interrupted) {
      STATE.outputAt = STATE.outputContext ? STATE.outputContext.currentTime : 0;
      setState('listening', 'Gemini Live · listening');
    }
    const transcript = content && content.outputTranscription && content.outputTranscription.text;
    if (transcript) setState('speaking', transcript.slice(0, 120));
  }

  async function activate() {
    const session = currentSession();
    if (!session || !session.session_id) {
      showToast('Open a conversation before starting Gemini Live.');
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.WebSocket) {
      showToast('Gemini Live requires microphone and WebSocket support.');
      return;
    }
    const generation = ++STATE.generation;
    STATE.active = true;
    STATE.sessionId = String(session.session_id);
    STATE.projectId = session.project_id || null;
    STATE.callResults.clear();
    STATE.lifecycleEvents.clear();
    setState('connecting', 'Gemini Live · connecting');
    try {
      STATE.stream = await navigator.mediaDevices.getUserMedia({ audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      } });
      const provision = await api('/api/jarviz/live/token', {
        method: 'POST',
        body: JSON.stringify({ session_id: STATE.sessionId, project_id: STATE.projectId }),
      });
      if (generation !== STATE.generation || !STATE.active) return;
      STATE.persona = provision.persona || null;
      const url = provision.websocket_url + '?access_token=' + encodeURIComponent(provision.token);
      const socket = new WebSocket(url);
      STATE.socket = socket;
      socket.onopen = () => {
        if (generation !== STATE.generation) return socket.close();
        socket.send(JSON.stringify({ setup: provision.setup }));
      };
      socket.onmessage = event => handleMessage(event, generation);
      socket.onerror = () => { if (generation === STATE.generation) setState('error', 'Gemini Live · connection error'); };
      socket.onclose = () => {
        if (generation === STATE.generation && STATE.active) {
          showToast('Gemini Live disconnected.');
          void deactivate();
        }
      };
      STATE.sessionWatch = window.setInterval(() => {
        const active = currentSession();
        if (!active || String(active.session_id) !== STATE.sessionId) void deactivate();
      }, 500);
    } catch (error) {
      const message = String(error && error.message || 'Could not start Gemini Live.');
      showToast(message);
      await deactivate();
    }
  }

  async function deactivate() {
    ++STATE.generation;
    STATE.active = false;
    if (STATE.sessionWatch) clearInterval(STATE.sessionWatch);
    STATE.sessionWatch = 0;
    if (STATE.processor) { try { STATE.processor.disconnect(); } catch (_) {} }
    if (STATE.inputSource) { try { STATE.inputSource.disconnect(); } catch (_) {} }
    if (STATE.stream) STATE.stream.getTracks().forEach(track => track.stop());
    if (STATE.socket) { try { STATE.socket.close(); } catch (_) {} }
    const contexts = [STATE.inputContext, STATE.outputContext];
    STATE.socket = STATE.stream = STATE.processor = STATE.inputSource = null;
    STATE.inputContext = STATE.outputContext = null;
    STATE.outputAt = 0;
    await Promise.all(contexts.filter(Boolean).map(context => context.close().catch(() => {})));
    setState('idle', '');
  }

  function onTaskEvent(envelope) {
    if (!STATE.active || !envelope || String(envelope.session_id || '') !== STATE.sessionId) return;
    const task = envelope.payload;
    const eventType = String(envelope.event_type || '');
    if (!task) return;
    const persona = STATE.persona || {};
    const shouldSpeak = (
      eventType === 'task.started' ? persona.announce_task_start !== false
        : eventType === 'task.completed' ? persona.announce_task_completion !== false
          : ['task.blocked', 'approval.requested', 'task.failed', 'task.cancelled'].includes(eventType)
            ? persona.announce_blockers !== false
            : eventType === 'task.updated' && persona.speak_technical_logs === true
    );
    if (!shouldSpeak || (!IMPORTANT_STATUSES.has(task.status) && eventType !== 'task.started' && eventType !== 'task.updated')) return;
    const eventKey = [envelope.task_id, eventType, envelope.created_at].join(':');
    if (STATE.lifecycleEvents.has(eventKey)) return;
    STATE.lifecycleEvents.add(eventKey);
    const safe = {
      event_type: eventType, task_id: task.task_id, title: task.title, status: task.status,
      result: task.status === 'completed' ? task.result : null,
      error: ['failed', 'blocked', 'cancelled'].includes(task.status) ? task.error : null,
    };
    send({ realtimeInput: { text: 'JarViz task lifecycle update: ' + JSON.stringify(safe) } });
  }

  function init() {
    const button = el('btnVoiceMode');
    if (!button) return;
    button.onclick = () => { if (STATE.active) void deactivate(); else void activate(); };
    button.setAttribute('aria-pressed', 'false');
    window._applyVoiceModePref = applyPreference;
    window._voiceModeActive = () => STATE.active;
    window._voiceModeDeactivate = deactivate;
    window._voiceModeImmediateSend = function () {};
    applyPreference();
  }

  window.JarVizLive = { init, activate, deactivate, onTaskEvent };
})();
