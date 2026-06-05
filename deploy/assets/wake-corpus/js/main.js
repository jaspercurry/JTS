// main.js — /wake-corpus/ wake-word corpus recorder.
//
// The browser half of the multi-leg UDP wake-capture recorder: it polls
// api/status every 2 s, lists/loads/deletes sessions, drives the
// enter/exit corpus-test-mode flow, runs the click-to-record clip loop,
// and shows the live mic-level meter (Server-Sent Events). The Python
// backend (jasper.web.wake_corpus_setup) owns all recording, bridge
// reconfiguration, and systemctl side effects — this module only talks to
// it over the same api/* JSON endpoints it always did.
//
// Relocated VERBATIM from the page's old inline <script> when /wake-corpus/
// moved onto the canonical design system. Only three behaviour-neutral seams
// changed:
//   * api() now builds its headers via jsonHeaders() from the shared http.js
//     module (was a hand-rolled {'Content-Type': 'application/json'} +
//     X-CSRF-Token). jsonHeaders() reads the token from the <meta name=
//     "jts-csrf"> tag canonical_page() renders; the server's _check_csrf
//     still compares that X-CSRF-Token header to its per-process token.
//   * jtsConfirm comes from the shared dialog.js module (was the inline
//     dialog_helpers_js twin) — never window.confirm, which the browser can
//     suppress.
//   * The Python-injected leg labels/order + USB AEC3 labels (formerly
//     {aec3_sweep_js_labels} / {aec3_sweep_js_order} / {usb_aec3_corpus_label}
//     / {usb_aec3_sweep_baseline_label} template substitutions) now ride in a
//     <script type="application/json" id="wake-corpus-config"> block the page
//     renders, read once below. A cached ES module can't carry per-deploy
//     Python data, so it's injected via the (no-store) HTML instead.
//
// All api/* paths stay RELATIVE ('api/...' not '/api/...') so the same module
// works standalone (http://host:8782/) AND behind nginx
// (http://host/wake-corpus/) — absolute paths would 502 under the prefix strip.

import { jsonHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";

const $ = id => document.getElementById(id);
let elapsedTimer = null;
let latestStatus = null;
let capturePlanRefreshSeq = 0;

// Server-injected config (leg labels + ordering + USB AEC3 labels). Read from
// the JSON island the page renders; falls back to empty so a missing block
// can't throw (the labels just degrade to the raw leg ids).
const _configEl = document.getElementById('wake-corpus-config');
let _config = {};
if (_configEl) {
  try { _config = JSON.parse(_configEl.textContent || '{}'); }
  catch (_) { _config = {}; }
}
const LEG_LABELS = {
  on: 'XVF WebRTC AEC3',
  off: 'XVF raw',
  dtln: 'XVF DTLN',
  raw0: 'XVF raw0',
  ref: 'Reference',
  usb_raw: 'USB raw',
  usb_webrtc: _config.usb_aec3_corpus_label || 'USB AEC3',
  usb_dtln: 'USB DTLN',
  chip_aec_150: 'Chip AEC ASR 150',
  chip_aec_210: 'Chip AEC ASR 210',
  xvf_raw0_webrtc_aec3: 'XVF raw0 WebRTC AEC3',
  xvf_raw0_dtln: 'XVF raw0 DTLN',
};
// Merge the Python-injected AEC3 sweep + legacy-sweep labels (formerly the
// {aec3_sweep_js_labels} substitution inside the LEG_LABELS literal).
Object.assign(LEG_LABELS, _config.aec3_sweep_labels || {});
const USB_AEC3_SWEEP_BASELINE_LABEL =
  _config.usb_aec3_sweep_baseline_label || '';
function planLegLabel(plan, leg) {
  for (const detail of plan?.legs || []) {
    if (detail?.token === leg && detail?.label) return detail.label;
  }
  return '';
}
function legLabel(leg, session = latestStatus) {
  const planned = planLegLabel(session?.capture_plan, leg);
  if (planned) return planned;
  if (
    leg === 'usb_webrtc' &&
    session?.include_aec3_sweep &&
    session?.aec3_sweep_source === 'usb'
  ) {
    return USB_AEC3_SWEEP_BASELINE_LABEL;
  }
  return LEG_LABELS[leg] || leg;
}
function applyAec3SweepVariants(variants) {
  for (const variant of variants || []) {
    if (variant?.leg && variant?.label) {
      LEG_LABELS[variant.leg] = variant.label;
    }
  }
}
const LEG_ORDER = [
  'chip_aec_150', 'chip_aec_210', 'xvf_raw0_webrtc_aec3',
  'xvf_raw0_dtln', 'raw0', 'usb_raw', 'usb_webrtc',
  'on', 'off', 'dtln',
  // AEC3 sweep legs (formerly the {aec3_sweep_js_order} substitution) sit
  // between 'dtln' and 'usb_dtln', preserving the original playback ordering.
  ...(_config.aec3_sweep_order || []),
  'usb_dtln', 'ref',
];
function resetSessionForm() {
  $('member').value = 'jasper';
  $('include-chip-aec-profile').checked = true;
  $('include-raw-mic-0').checked = false;
  $('include-dtln').checked = false;
  $('include-xvf-raw0-dtln').checked = false;
  $('include-aec3-sweep').checked = false;
  $('include-usb-mic').checked = false;
  $('include-usb-dtln').checked = false;
  syncCorpusProfileControls(false);
}
function syncCorpusProfileControls(sessionLoaded = false) {
  const chipProfile = $('include-chip-aec-profile').checked;
  if (chipProfile) {
    $('include-raw-mic-0').checked = true;
    $('include-aec3-sweep').checked = false;
  }
  $('include-raw-mic-0').disabled = sessionLoaded || chipProfile;
  $('include-usb-mic').disabled = sessionLoaded;
  $('include-aec3-sweep').disabled = sessionLoaded || chipProfile;
}
function orderedLegs(files) {
  const present = files || {};
  const known = LEG_ORDER.filter(leg =>
    Object.prototype.hasOwnProperty.call(present, leg)
  );
  const extra = Object.keys(present).filter(leg => !LEG_ORDER.includes(leg));
  return known.concat(extra);
}

// API base — relative to the current page so the same JS works
// standalone (http://host:8782/) AND behind nginx
// (http://host/wake-corpus/). All endpoint paths must be relative
// ("api/...") NOT absolute ("/api/..."), otherwise the nginx
// prefix gets stripped and the request 502s.
async function api(method, path, body) {
  // jsonHeaders() attaches X-CSRF-Token (read from the <meta name=jts-csrf>
  // tag) plus Content-Type: application/json. Mutating requests need the
  // token; GET ignores it harmlessly, matching the prior behaviour where
  // every request carried the JSON content type.
  const headers = jsonHeaders();
  const opts = { method, headers };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) {
    const e = await r.json().catch(() => ({error: 'request failed'}));
    const err = new Error(e.error || `${r.status}`);
    err.status = r.status;
    err.body = e;
    throw err;
  }
  return r.json();
}

function currentSessionPayload() {
  const includeAec3Sweep = $('include-aec3-sweep').checked;
  return {
    member: $('member').value.trim(),
    corpus_profile: $('include-chip-aec-profile').checked
      ? 'chip_aec_comparison_v1'
      : 'standard',
    include_raw_mic_0: $('include-raw-mic-0').checked,
    include_dtln: $('include-dtln').checked,
    include_usb_mic: $('include-usb-mic').checked || includeAec3Sweep,
    include_usb_dtln: $('include-usb-dtln').checked,
    include_xvf_raw0_dtln: $('include-xvf-raw0-dtln').checked,
    include_aec3_sweep: includeAec3Sweep,
    aec3_sweep_source: includeAec3Sweep ? 'usb' : 'xvf',
  };
}

function renderCapturePlan(plan) {
  const root = $('capture-plan-preview');
  if (!root) return;
  root.replaceChildren();
  if (!plan) {
    const empty = document.createElement('span');
    empty.className = 'hint';
    empty.textContent = 'No capture plan yet.';
    root.appendChild(empty);
    return;
  }

  const header = document.createElement('div');
  header.className = 'capture-plan-header';
  const title = document.createElement('strong');
  title.textContent = 'Capture plan';
  const resource = document.createElement('span');
  const level = plan.resource?.level || 'unknown';
  resource.className = `pill tiny load-${level}`;
  resource.textContent = `load: ${level}`;
  header.append(title, resource);
  root.appendChild(header);

  const devices = document.createElement('div');
  devices.className = 'capture-plan-devices';
  for (const device of plan.devices || []) {
    const block = document.createElement('div');
    block.className = 'capture-plan-device';
    const label = document.createElement('span');
    label.className = 'capture-plan-device-label';
    label.textContent = device.label || device.device_id || 'Unknown device';
    const legs = document.createElement('span');
    legs.className = 'capture-plan-legs';
    const names = (device.legs || []).map(leg => (
      planLegLabel(plan, leg) || legLabel(leg)
    )).join(', ');
    legs.textContent = names || 'no legs';
    block.append(label, legs);
    devices.appendChild(block);
  }
  root.appendChild(devices);

  if ((plan.warnings || []).length) {
    const warnings = document.createElement('ul');
    warnings.className = 'capture-plan-warnings';
    for (const warning of plan.warnings) {
      const item = document.createElement('li');
      item.textContent = warning;
      warnings.appendChild(item);
    }
    root.appendChild(warnings);
  }
}

async function refreshCapturePlan() {
  if (latestStatus?.session_id) {
    renderCapturePlan(latestStatus.capture_plan);
    return null;
  }
  const seq = ++capturePlanRefreshSeq;
  try {
    const r = await api('POST', 'api/capture-plan', currentSessionPayload());
    if (seq === capturePlanRefreshSeq) renderCapturePlan(r.capture_plan);
    return r.capture_plan;
  } catch (e) {
    if (seq !== capturePlanRefreshSeq) return null;
    const root = $('capture-plan-preview');
    if (root) {
      root.replaceChildren();
      const err = document.createElement('span');
      err.className = 'err';
      err.textContent = `capture plan: ${e.message}`;
      root.appendChild(err);
    }
    return null;
  }
}

function showErr(msg) {
  $('err').textContent = msg || '';
  if (msg) console.error(msg);
}

async function refreshStatus() {
  try {
    const s = await api('GET', 'api/status');
    latestStatus = s;
    applyAec3SweepVariants(s.aec3_sweep_variants);
    const modeEl = $('corpus-mode-status');
    const voiceEl = $('voice-status');
    const exitEl = $('corpus-mode-exit');
    const bridgeEl = $('bridge-output-status');
    const unloadEl = $('session-unload');
    const bridgeOutputs = s.bridge_outputs || {};
    const recorderOutputs = bridgeOutputs.recorder_outputs || {};
    const bridgeActive = Boolean(bridgeOutputs.active);
    const voiceActive = Boolean(s.voice_daemon_active);
    const inCorpusMode = !voiceActive || bridgeActive;
    const sessionLoaded = Boolean(s.session_id);
    const sessionInputs = [
      $('member'), $('include-chip-aec-profile'),
      $('include-raw-mic-0'), $('include-dtln'), $('include-xvf-raw0-dtln'),
      $('include-aec3-sweep'), $('include-usb-mic'), $('include-usb-dtln'),
    ];
    const chipProfile = s.corpus_profile === 'chip_aec_comparison_v1';
    const sessionNeedsUsb = Boolean(
      s.include_usb_mic ||
      s.include_usb_dtln ||
      (s.include_aec3_sweep && s.aec3_sweep_source === 'usb')
    );
    const sessionBridgeReady = !sessionLoaded || (
      (!s.include_dtln || bridgeOutputs.dtln) &&
      (!s.include_aec3_sweep || bridgeOutputs.aec3_sweep) &&
      (!sessionNeedsUsb || (bridgeOutputs.ref && bridgeOutputs.usb)) &&
      (!s.include_usb_dtln || bridgeOutputs.usb_dtln) &&
      (!chipProfile || (
        bridgeOutputs.chip_aec &&
        bridgeOutputs.xvf_raw0_webrtc_aec3 &&
        bridgeOutputs.outputd_ref
      )) &&
      (!s.include_xvf_raw0_dtln || bridgeOutputs.xvf_raw0_dtln)
    );
    const activeBridgeLabels = [];
    if (recorderOutputs.dtln) activeBridgeLabels.push('XVF DTLN');
    if (recorderOutputs.aec3_sweep) {
      const source = recorderOutputs.aec3_sweep_source === 'usb'
        ? 'USB' : 'XVF';
      activeBridgeLabels.push(`${source} AEC3 sweep`);
    }
    if (recorderOutputs.ref) activeBridgeLabels.push('ref');
    if (recorderOutputs.usb) activeBridgeLabels.push('USB');
    if (recorderOutputs.usb_dtln) activeBridgeLabels.push('USB DTLN');
    if (recorderOutputs.chip_aec) activeBridgeLabels.push('chip AEC');
    if (recorderOutputs.xvf_raw0_webrtc_aec3) activeBridgeLabels.push('XVF raw0 AEC3');
    if (recorderOutputs.xvf_raw0_dtln) activeBridgeLabels.push('XVF raw0 DTLN');
    if (recorderOutputs.outputd_ref) activeBridgeLabels.push('outputd ref');
    if (voiceActive && bridgeActive) {
      modeEl.textContent = 'mixed: voice + test outputs';
      modeEl.className = 'pill red';
    } else if (!voiceActive) {
      modeEl.textContent = 'corpus test mode';
      modeEl.className = 'pill green';
    } else {
      modeEl.textContent = 'production ready';
      modeEl.className = 'pill green';
    }
    exitEl.onclick = exitCorpusTestMode;
    exitEl.disabled = s.is_recording || !inCorpusMode;
    exitEl.style.visibility = inCorpusMode ? 'visible' : 'hidden';
    if (activeBridgeLabels.length) {
      bridgeEl.textContent = `ON: ${activeBridgeLabels.join(', ')}`;
      bridgeEl.className = 'pill red';
    } else if (bridgeOutputs.active) {
      bridgeEl.textContent = 'cleanup pending';
      bridgeEl.className = 'pill red';
    } else {
      bridgeEl.textContent = 'off';
      bridgeEl.className = 'pill green';
    }
    if (voiceActive) {
      voiceEl.textContent = 'running';
      voiceEl.className = 'pill green';
    } else {
      voiceEl.textContent = 'stopped';
      voiceEl.className = 'pill gray';
    }
    $('session-card-title').textContent = sessionLoaded
      ? 'Loaded session'
      : 'Begin a new session';
    if (sessionLoaded) {
      $('include-chip-aec-profile').checked = chipProfile;
      $('member').value = s.member || '';
      $('include-raw-mic-0').checked = Boolean(s.include_raw_mic_0);
      $('include-dtln').checked = Boolean(s.include_dtln);
      $('include-xvf-raw0-dtln').checked = Boolean(s.include_xvf_raw0_dtln);
      $('include-aec3-sweep').checked = Boolean(s.include_aec3_sweep);
      $('include-usb-mic').checked = Boolean(s.include_usb_mic);
      $('include-usb-dtln').checked = Boolean(s.include_usb_dtln);
    }
    for (const input of sessionInputs) input.disabled = sessionLoaded;
    syncCorpusProfileControls(sessionLoaded);
    const beginEl = $('session-begin');
    unloadEl.style.display = sessionLoaded && !inCorpusMode
      ? 'inline-block' : 'none';
    unloadEl.disabled = s.is_recording;
    if (sessionLoaded) {
      if (voiceActive && bridgeActive && sessionBridgeReady) {
        beginEl.textContent = 'Stop voice & resume recording';
        beginEl.disabled = s.is_recording;
      } else if (voiceActive && bridgeActive) {
        beginEl.textContent = 'Stop voice & apply outputs';
        beginEl.disabled = s.is_recording;
      } else if (voiceActive) {
        beginEl.textContent = 'Enter corpus test mode';
        beginEl.disabled = s.is_recording;
      } else if (!sessionBridgeReady) {
        beginEl.textContent = 'Apply bridge outputs';
        beginEl.disabled = s.is_recording;
      } else {
        beginEl.textContent = 'Ready to record';
        beginEl.disabled = true;
      }
    } else {
      beginEl.textContent = 'Enter corpus test mode & begin';
      beginEl.disabled = s.is_recording;
    }
    const sessionLabel = s.session_id
      ? `${s.member} / ${s.session_id}`
        + (s.include_raw_mic_0 ? ' · raw mic 0 ✓' : '')
        + (chipProfile ? ' · chip AEC profile ✓' : '')
        + (!s.include_dtln ? ' · XVF DTLN off' : '')
        + (s.include_xvf_raw0_dtln ? ' · XVF raw0 DTLN ✓' : '')
        + (s.include_aec3_sweep ? ` · ${s.aec3_sweep_source === 'usb' ? 'USB' : 'XVF'} AEC3 sweep ✓` : '')
        + (s.include_usb_mic ? ' · USB/ref ✓' : '')
        + (s.include_usb_dtln ? ' · USB DTLN ✓' : '')
        + (s.enabled_legs?.length ? ` · ${s.enabled_legs.map(leg => legLabel(leg, s)).join(', ')}` : '')
      : '(no session)';
    $('session-id').textContent = sessionLabel;
    if (sessionLoaded) {
      $('record-card').style.display = 'block';
      $('counts-card').style.display = 'block';
      $('clips-card').style.display = 'block';
      renderCapturePlan(s.capture_plan);
    } else {
      $('record-card').style.display = 'none';
      $('counts-card').style.display = 'none';
      $('clips-card').style.display = 'none';
    }
    if (s.is_recording) {
      $('recording-info').style.display = 'block';
      $('record-btn').textContent = '■ STOP';
      $('record-btn').classList.add('recording');
      $('record-btn').classList.remove('primary');
      $('record-btn').disabled = false;
    } else {
      $('recording-info').style.display = 'none';
      $('record-btn').textContent = '● RECORD';
      $('record-btn').classList.remove('recording');
      $('record-btn').classList.add('primary');
      $('record-btn').disabled = !s.session_id || voiceActive || !sessionBridgeReady;
    }
  } catch (e) { showErr(`status: ${e.message}`); }
}

async function enterCorpusTestMode(options) {
  await api('POST', 'api/corpus-test-mode', {
    action: 'enter',
    corpus_profile: options.corpusProfile || 'standard',
    include_dtln: options.includeDtln,
    include_usb_mic: options.includeUsbMic,
    include_usb_dtln: options.includeUsbDtln,
    include_xvf_raw0_dtln: options.includeXvfRaw0Dtln,
    include_aec3_sweep: options.includeAec3Sweep,
    aec3_sweep_source: options.aec3SweepSource || 'usb',
  });
}

async function exitCorpusTestMode() {
  try {
    await api('POST', 'api/corpus-test-mode', {action: 'exit'});
    resetSessionForm();
    await refreshStatus();
    await refreshClips();
    await refreshSessions();
  } catch (e) { showErr(`corpus test mode exit: ${e.message}`); }
}

async function unloadSession() {
  try {
    await api('POST', 'api/session/unload', {});
    resetSessionForm();
    showErr('');
    await refreshStatus();
    await refreshClips();
    await refreshSessions();
  } catch (e) { showErr(`unload: ${e.message}`); }
}

async function beginSession() {
  if (latestStatus?.session_id) {
    try {
      await enterCorpusTestMode({
        corpusProfile: latestStatus.corpus_profile || 'standard',
        includeDtln: Boolean(latestStatus.include_dtln),
        includeUsbMic: Boolean(latestStatus.include_usb_mic),
        includeUsbDtln: Boolean(latestStatus.include_usb_dtln),
        includeXvfRaw0Dtln: Boolean(latestStatus.include_xvf_raw0_dtln),
        includeAec3Sweep: Boolean(latestStatus.include_aec3_sweep),
        aec3SweepSource: latestStatus.aec3_sweep_source || 'usb',
      });
      showErr('');
      await refreshStatus();
      return;
    } catch (e) {
      showErr(`corpus test mode enter: ${e.message}`);
      return;
    }
  }
  const member = $('member').value.trim();
  if (!member) { showErr('member is required'); return; }
  const payload = currentSessionPayload();
  try {
    const plan = await refreshCapturePlan();
    if (plan && ['high', 'unsafe'].includes(plan.resource?.level)) {
      const warnings = (plan.warnings || []).join('\n\n');
      const ok = await jtsConfirm(
        `This recording plan is marked ${plan.resource.level} load.\n\n` +
        `${warnings}\n\nContinue?`,
        {danger: plan.resource.level === 'unsafe'},
      );
      if (!ok) {
        showErr('begin session: capture plan not confirmed');
        return;
      }
    }
    await enterCorpusTestMode({
      corpusProfile: payload.corpus_profile,
      includeDtln: payload.include_dtln,
      includeUsbMic: payload.include_usb_mic,
      includeUsbDtln: payload.include_usb_dtln,
      includeXvfRaw0Dtln: payload.include_xvf_raw0_dtln,
      includeAec3Sweep: payload.include_aec3_sweep,
      aec3SweepSource: payload.aec3_sweep_source,
    });
    await api('POST', 'api/session', payload);
    showErr('');
    await refreshStatus();
    await refreshClips();
    await refreshSessions();
  } catch (e) {
    const body = e.body || {};
    if (e.status === 409 && body.can_enable_bridge_outputs) {
      const labels = body.missing_bridge_output_labels || [];
      const ok = await jtsConfirm(
        `The bridge is not currently emitting: ${labels.join(', ')}.\n\n` +
        `Enable those corpus outputs and restart the affected audio daemons now? ` +
        `This can add CPU/RAM load, especially DTLN paths.`
      );
      if (!ok) {
        showErr('begin session: bridge outputs not enabled');
        return;
      }
      try {
        await api('POST', 'api/session', {
          ...payload,
          enable_bridge_outputs: true,
        });
        showErr('');
        await refreshStatus();
        await refreshClips();
        await refreshSessions();
        return;
      } catch (retryErr) {
        showErr(`begin session: ${retryErr.message}`);
        return;
      }
    }
    showErr(`begin session: ${e.message}`);
  }
}

async function refreshSessions() {
  try {
    const r = await api('GET', 'api/sessions');
    const wrap = $('sessions-list');
    if (!r.sessions.length) {
      wrap.innerHTML = '<p style="color:#888;margin:0">No sessions yet — begin one below.</p>';
      return;
    }
    wrap.innerHTML = '';
    for (const s of r.sessions) {
      const row = document.createElement('div');
      row.className = 'session-row' + (s.is_active ? ' active' : '');
      const condText = Object.entries(s.conditions)
        .map(([k, v]) => `${k}=${v}`).join(' · ') || 'no clips';
      const rawPill = s.include_raw_mic_0
        ? '<span class="pill tiny purple">raw mic 0</span>'
        : '';
      const dtlnPill = s.include_dtln
        ? '<span class="pill tiny purple">XVF DTLN</span>'
        : '';
      const usbPill = s.include_usb_mic
        ? '<span class="pill tiny purple">USB/ref</span>'
        : '';
      const usbDtlnPill = s.include_usb_dtln
        ? '<span class="pill tiny purple">USB DTLN</span>'
        : '';
      const chipPill = s.corpus_profile === 'chip_aec_comparison_v1'
        ? '<span class="pill tiny purple">chip AEC</span>'
        : '';
      const xvfRaw0DtlnPill = s.include_xvf_raw0_dtln
        ? '<span class="pill tiny purple">XVF raw0 DTLN</span>'
        : '';
      const aec3SweepPill = s.include_aec3_sweep
        ? `<span class="pill tiny purple">${s.aec3_sweep_source === 'usb' ? 'USB' : 'XVF'} AEC3 sweep</span>`
        : '';
      const legsText = (s.enabled_legs || []).map(leg => legLabel(leg, s)).join(', ');
      const activeMark = s.is_active
        ? '<span class="pill tiny green">loaded</span> ' : '';
      const date = new Date(s.mtime * 1000).toLocaleString();
      row.innerHTML = `
        <div class="session-meta">
          <div>${activeMark}<strong>${s.member}</strong>
            ${chipPill} ${rawPill} ${dtlnPill} ${aec3SweepPill} ${usbPill} ${usbDtlnPill} ${xvfRaw0DtlnPill}
            <span class="id">${s.session_id}</span></div>
          <div class="breakdown">${s.clip_count} clip(s) · ${condText} · legs: ${legsText || 'none'} · ${date}</div>
        </div>
        <div class="session-actions">
          <button data-load="${s.session_id}" ${s.is_active ? 'disabled' : ''}>Load</button>
          <button class="danger" data-delete="${s.session_id}" data-summary="${s.clip_count} clip(s)">Delete</button>
        </div>
      `;
      row.querySelector('[data-load]').onclick = (ev) => loadSession(
        ev.target.dataset.load,
      );
      row.querySelector('[data-delete]').onclick = (ev) => deleteSession(
        ev.target.dataset.delete, ev.target.dataset.summary,
      );
      wrap.appendChild(row);
    }
  } catch (e) { showErr(`sessions: ${e.message}`); }
}

async function loadSession(sessionId) {
  try {
    await api('POST', 'api/session/load', {session_id: sessionId});
    showErr('');
    await refreshStatus();
    await refreshClips();
    await refreshSessions();
  } catch (e) { showErr(`load: ${e.message}`); }
}

async function deleteSession(sessionId, summary) {
  if (!await jtsConfirm(
    `Permanently delete session ${sessionId}? This removes ${summary} ` +
    `and the session metadata. Cannot be undone.`,
    {danger: true},
  )) return;
  try {
    await api('DELETE', `api/session/${sessionId}`);
    showErr('');
    await refreshStatus();
    await refreshClips();
    await refreshSessions();
  } catch (e) { showErr(`delete: ${e.message}`); }
}

function selectedRadio(name) {
  const r = document.querySelector(`input[name="${name}"]:checked`);
  return r ? r.value : null;
}

async function toggleRecord() {
  const isRecording = $('record-btn').classList.contains('recording');
  if (isRecording) {
    try {
      await api('POST', 'api/clip/stop', {});
      if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
      await refreshStatus();
      await refreshClips();
      await refreshSessions();  // count++
    } catch (e) { showErr(`stop: ${e.message}`); }
  } else {
    const condition = selectedRadio('condition');
    const distance = selectedRadio('distance');
    try {
      const r = await api('POST', 'api/clip/start', {condition, distance});
      showErr('');
      const startMs = Date.now();
      if (elapsedTimer) clearInterval(elapsedTimer);
      elapsedTimer = setInterval(() => {
        const s = ((Date.now() - startMs) / 1000).toFixed(1);
        $('elapsed').textContent = `${s}s`;
      }, 100);
      await refreshStatus();
    } catch (e) { showErr(`start: ${e.message}`); }
  }
}

async function refreshClips() {
  try {
    const r = await api('GET', 'api/clips');
    const list = $('clips-list');
    list.innerHTML = '';
    const counts = {};
    for (const c of r.clips) {
      const key = `${c.distance}-${c.condition}`;
      counts[key] = (counts[key] || 0) + 1;
      const row = document.createElement('div');
      row.className = 'clip';
      const fileLegs = orderedLegs(c.files || {});
      const firstLeg = fileLegs.includes('on') ? 'on' : fileLegs[0];
      const options = fileLegs.map(leg => (
        `<option value="${leg}" ${leg === firstLeg ? 'selected' : ''}>${legLabel(leg, c)}</option>`
      )).join('');
      const audioHtml = firstLeg
        ? `<div class="clip-audio">
             <select data-audio-leg="${c.clip_id}">${options}</select>
             <audio controls preload="none" src="api/clip/${c.clip_id}/wav?leg=${firstLeg}"></audio>
           </div>`
        : '<span class="clip-audio">no WAVs</span>';
      row.innerHTML = `
        <span class="seq">${String(c.seq).padStart(3, '0')}</span>
        <span>${c.condition}</span>
        <span>${c.distance}</span>
        <span>${c.duration_sec.toFixed(2)}s</span>
        ${audioHtml}
        <button class="danger icon" data-id="${c.clip_id}" title="Delete clip">🗑</button>
      `;
      const selector = row.querySelector('[data-audio-leg]');
      if (selector) {
        selector.onchange = (ev) => {
          const audio = row.querySelector('audio');
          audio.src = `api/clip/${c.clip_id}/wav?leg=${encodeURIComponent(ev.target.value)}`;
          audio.load();
        };
      }
      row.querySelector('button').onclick = async (ev) => {
        const id = ev.target.dataset.id;
        if (!await jtsConfirm(`Delete clip ${c.seq}?`, {danger: true})) return;
        try {
          await api('DELETE', `api/clip/${id}`);
          await refreshClips();
          await refreshSessions();  // count--
        } catch (e) { showErr(`delete: ${e.message}`); }
      };
      list.prepend(row);  // newest first
    }
    renderCounts(counts);
  } catch (e) { showErr(`clips: ${e.message}`); }
}

function renderCounts(counts) {
  const matrix = $('counts-matrix');
  matrix.innerHTML = '';
  // Header row — quiet / ambient / music / total
  matrix.innerHTML = `
    <div class="header"></div>
    <div class="header">quiet</div>
    <div class="header">ambient</div>
    <div class="header">music</div>
    <div class="header">total</div>
  `;
  let grand = 0;
  for (const d of ['near', 'mid', 'far']) {
    const q = counts[`${d}-quiet`] || 0;
    const a = counts[`${d}-ambient`] || 0;
    const m = counts[`${d}-music`] || 0;
    const t = q + a + m;
    grand += t;
    matrix.innerHTML += `
      <div class="header">${d}</div>
      <div>${q}</div>
      <div>${a}</div>
      <div>${m}</div>
      <div>${t}</div>
    `;
  }
  matrix.innerHTML += `
    <div class="header">total</div>
    <div></div><div></div><div></div>
    <div>${grand}</div>
  `;
}

$('session-begin').onclick = beginSession;
$('session-unload').onclick = unloadSession;
$('record-btn').onclick = toggleRecord;
$('include-chip-aec-profile').onchange = () => {
  syncCorpusProfileControls(false);
  refreshCapturePlan();
};
$('include-aec3-sweep').onchange = () => {
  if ($('include-aec3-sweep').checked) {
    $('include-chip-aec-profile').checked = false;
    $('include-dtln').checked = false;
    $('include-usb-dtln').checked = false;
    $('include-usb-mic').checked = true;
  }
  syncCorpusProfileControls(false);
  refreshCapturePlan();
};
$('include-dtln').onchange = () => {
  if ($('include-dtln').checked) $('include-aec3-sweep').checked = false;
  syncCorpusProfileControls(false);
  refreshCapturePlan();
};
$('include-usb-dtln').onchange = () => {
  if ($('include-usb-dtln').checked) $('include-aec3-sweep').checked = false;
  syncCorpusProfileControls(false);
  refreshCapturePlan();
};
$('include-usb-mic').onchange = () => {
  if (!$('include-usb-mic').checked && (
    $('include-aec3-sweep').checked
  )) {
    $('include-usb-mic').checked = true;
  }
  syncCorpusProfileControls(false);
  refreshCapturePlan();
};
$('include-raw-mic-0').onchange = refreshCapturePlan;
$('include-xvf-raw0-dtln').onchange = refreshCapturePlan;

// Spacebar toggles recording when a session is active + we're not
// typing in an input. Convenient for hands-on-room workflows.
document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space') return;
  if (document.activeElement.tagName === 'INPUT') return;
  if ($('record-btn').disabled) return;
  e.preventDefault();
  toggleRecord();
});

// Live mic-level meter via Server-Sent Events. Connects on page
// load, stays open for the lifetime of the tab. Server pushes
// {recording: bool, rms_dbfs: float|null} ~12 Hz when recording,
// ~2 Hz when idle. Greys out when idle so the operator still
// sees the meter exists. Color thresholds:
//   green  >= -30 dBFS  (good)
//   yellow >= -45 dBFS  (quiet but audible)
//   red    >= -60 dBFS  (very quiet; reaching threshold of room noise)
//   grey   <  -60 dBFS  (basically silent — likely a problem)
function updateLevelBar(recording, dbfs) {
  const wrap = $('mic-level');
  const fill = $('mic-level-fill');
  const out = $('mic-level-readout');
  if (!recording || dbfs === null) {
    wrap.className = 'mic-level';
    fill.style.width = '0%';
    out.textContent = '—';
    return;
  }
  // Map -60 dBFS .. 0 dBFS → 0 .. 100% bar width.
  const pct = Math.max(0, Math.min(100, (dbfs + 60) * 100 / 60));
  fill.style.width = pct + '%';
  out.textContent = dbfs.toFixed(1) + ' dBFS';
  let cls = 'mic-level';
  if (dbfs >= -30) cls += ' active';
  else if (dbfs >= -45) cls += ' warning';
  else if (dbfs >= -60) cls += ' danger';
  wrap.className = cls;
}
try {
  const es = new EventSource('api/recording/level');
  es.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      updateLevelBar(d.recording, d.rms_dbfs);
    } catch (_) { /* ignore malformed frame */ }
  };
  es.onerror = () => {
    // Browser auto-reconnects EventSource on its own. Just show
    // the idle state in the meantime.
    updateLevelBar(false, null);
  };
} catch (_) { /* EventSource unsupported — skip the meter */ }

syncCorpusProfileControls(false);
refreshCapturePlan();
refreshStatus();
refreshClips();
refreshSessions();
setInterval(refreshStatus, 2000);
// Sessions list changes slowly (only on begin/load/delete) — refresh
// every 30 s so external SSH edits show up without being chatty.
setInterval(refreshSessions, 30000);
