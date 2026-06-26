// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pair-balance walkthrough (#23 P2, equal-loudness redesign). One
// speaker at a time: stream this phone's in-band mic meter to the
// backend, ask the server to play that speaker's quiet-to-loud ramp,
// and render the backend's floor/target/lock decisions. Shared
// measurement-audio primitives own mono mic capture and the
// no-monitoring graph invariant; this page owns only the 500 Hz–2 kHz
// meter policy so HVAC rumble moves neither the stimulus nor the needle.

import {
  closeAudioGraph,
  createBandpassRmsMeter,
  openMonoMic,
  rmsToDbfs,
} from '/assets/shared/js/measurement-audio.js';
// jsonHeaders attaches X-CSRF-Token AND the X-JTS-Token control token
// (meta[name=jts-control-token]); the latter is required for /balance/apply,
// which fans out to each speaker's token-gated /grouping/set.
import { jsonHeaders } from '/assets/shared/js/http.js';

const els = {};
for (const id of ['status', 'meter', 'meter-fill', 'meter-target',
  'meter-row', 'meter-db', 'progress', 'verdict', 'start', 'stop',
  'retry', 'apply', 'again']) {
  els[id] = document.getElementById(id);
}

const REQUIRED_SR = 48000;
const METER_RANGE = [-80, -20]; // bar display range, dB

let ctx = null;
let workletNode = null;
let sourceNode = null;
let micStream = null;
let latestDb = -120;
let members = null;
let rampDuration = 26;
let session = null;  // {channel, target, phase, timer, pollTimer}
let meterInFlight = false;
let pendingMeterDb = null;

function setStatus(text, tone) {
  els.status.textContent = text || '';
  els.status.dataset.tone = tone || '';
}

async function post(path, body) {
  const resp = await fetch(path, {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON error */ }
  if (!data) throw new Error(path + ' → HTTP ' + resp.status);
  return data;
}

function showMeter(on) {
  els.meter.hidden = !on;
  els['meter-row'].hidden = !on;
}

function renderMeter(db, target) {
  const [lo, hi] = METER_RANGE;
  const pct = (v) => Math.max(0, Math.min(100,
    ((v - lo) / (hi - lo)) * 100));
  els['meter-fill'].style.width = pct(db).toFixed(1) + '%';
  els['meter-db'].textContent = db.toFixed(1) + ' dB';
  if (target != null) {
    els['meter-target'].style.left = pct(target).toFixed(1) + '%';
    els['meter-target'].style.display = '';
  } else {
    els['meter-target'].style.display = 'none';
  }
}

function speakerName(ch) {
  const m = members && members[ch];
  return (ch === 'left' ? 'left' : 'right') + ' speaker'
    + (m ? ' (' + m.label + ')' : '');
}

function progressRow(text, value) {
  const row = document.createElement('div');
  row.className = 'row';
  const name = document.createElement('span');
  name.textContent = text;
  const lvl = document.createElement('span');
  lvl.className = 'lvl';
  lvl.textContent = value;
  row.append(name, lvl);
  return row;
}

async function openMic() {
  if (workletNode) return;
  try {
    const opened = await openMonoMic({ sampleRate: REQUIRED_SR });
    micStream = opened.stream;
    ctx = opened.context;
    const meter = await createBandpassRmsMeter({
      context: ctx,
      stream: micStream,
      frequencyHz: 1000,
      q: 0.67,
      frameSize: 4800,
    });
    sourceNode = meter.sourceNode;
    workletNode = meter.workletNode;
    workletNode.port.onmessage = (ev) => {
      if (ev.data && ev.data.type === 'rms') {
        latestDb = rmsToDbfs(ev.data.value);
        onMeterFrame(latestDb);
        queueMeterFrame(latestDb);
      }
    };
  } catch (e) {
    await closeMic();
    throw e;
  }
}

async function closeMic() {
  await closeAudioGraph({
    stream: micStream,
    context: ctx,
    sourceNode: sourceNode,
    workletNode: workletNode,
  });
  ctx = null;
  workletNode = null;
  sourceNode = null;
  micStream = null;
}

function onMeterFrame(db) {
  if (!session) { renderMeter(db, null); return; }
  renderMeter(db, session.target);
}

function clearSessionTimers() {
  if (session) {
    clearTimeout(session.timer);
    clearInterval(session.pollTimer);
  }
}

function backendTarget(data) {
  if (data && data.target_dbfs != null) return data.target_dbfs;
  if (data && data.meter && data.meter.target_dbfs != null) {
    return data.meter.target_dbfs;
  }
  return null;
}

function floorWaitStatus(data) {
  if (data && data.error) return data.error;
  return 'Checking background noise…';
}

function handleBackendLock(data) {
  if (!session || !data || !data.locked) return;
  const ch = session.channel;
  if (data.channel && data.channel !== ch) return;
  clearSessionTimers();
  els.progress.append(progressRow(
    'Heard the ' + speakerName(ch), 'drive '
    + data.drive_dbfs.toFixed(1) + ' dB'));
  els.progress.style.display = 'block';
  if (data.phase === 'analyzed') {
    session = null;
    showMeter(false);
    renderResult(data);
  } else {
    const next = ch === 'left' ? 'right' : 'left';
    setStatus('Got it. Next: the ' + speakerName(next) + '.', 'ok');
    setTimeout(() => runStep(next), 1200);
  }
}

async function flushMeterFrame() {
  if (meterInFlight || pendingMeterDb == null) return;
  const db = pendingMeterDb;
  pendingMeterDb = null;
  meterInFlight = true;
  try {
    const data = await post('meter', { db: db });
    const target = backendTarget(data);
    if (session && target != null) session.target = target;
    handleBackendLock(data);
  } catch (e) {
    // Best effort: the status poll and explicit ramp calls surface hard errors.
  } finally {
    meterInFlight = false;
    if (pendingMeterDb != null) flushMeterFrame();
  }
}

function queueMeterFrame(db) {
  pendingMeterDb = db;
  flushMeterFrame();
}

function failStep(message) {
  clearSessionTimers();
  const ch = session && session.channel;
  session = null;
  setStatus(message, 'bad');
  els.retry.hidden = false;
  els.retry.dataset.channel = ch || 'left';
}

async function runStep(channel) {
  clearSessionTimers();
  session = { channel: channel, phase: 'floor', target: null,
              timer: null, pollTimer: null };
  els.retry.hidden = true;
  showMeter(true);
  setStatus('Checking background noise…');

  let data = await post('ramp', { channel: channel });
  while (session && session.channel === channel && data.need_floor) {
    setStatus(floorWaitStatus(data));
    await new Promise((r) => setTimeout(r, data.wait_ms || 300));
    data = await post('ramp', { channel: channel });
  }
  if (!session || session.channel !== channel) return;
  if (!data.ok) {
    failStep(data.error || 'could not start the test sound');
    return;
  }
  rampDuration = data.duration_s || rampDuration;
  session.target = backendTarget(data);
  session.phase = 'ramping';
  setStatus('Listening for the ' + speakerName(channel)
    + ' — it starts almost silent and slowly gets louder…');

  // If the ramp ends with no lock, the server marks not_heard; a
  // light status poll picks that up (and the local timer backstops).
  session.pollTimer = setInterval(async () => {
    try {
      const st = await (await fetch('status')).json();
      const lock = (st.locks || {})[channel];
      if (session && session.channel === channel
          && lock && lock.not_heard) {
        failStep('Couldn’t hear the ' + speakerName(channel)
          + ' during the normalized test ramp — check that it’s powered '
          + 'and connected, then retry.');
      }
    } catch (e) { /* poll is best-effort */ }
  }, 1500);
  session.timer = setTimeout(() => {
    if (session && session.channel === channel) {
      failStep('Couldn’t hear the ' + speakerName(channel)
        + ' — check that it’s powered and connected, then retry.');
    }
  }, (rampDuration + 4) * 1000);
}

function renderResult(data) {
  const rec = data.recommendation;
  const delta = rec.delta_db;
  const louder = delta >= 0 ? 'left' : 'right';
  const diff = Math.abs(delta);
  els.progress.append(
    progressRow('New trim — ' + speakerName('left'),
      rec.left_trim_db.toFixed(1) + ' dB'),
    progressRow('New trim — ' + speakerName('right'),
      rec.right_trim_db.toFixed(1) + ' dB'),
  );
  let text;
  if (diff < 0.5) {
    text = 'Already balanced (difference '
      + diff.toFixed(1) + ' dB).';
  } else {
    text = 'The ' + speakerName(louder) + ' is '
      + diff.toFixed(1) + ' dB louder. Apply sets the trims above.';
  }
  if (rec.clamped) {
    text += ' Note: the difference exceeds the −24 dB trim range, '
      + 'so this is the closest possible match.';
  }
  els.verdict.textContent = text;
  setStatus('Walkthrough complete.', 'ok');
  els.stop.hidden = true;
  els.apply.hidden = diff < 0.5 && !rec.clamped;
  els.again.hidden = false;
}

function resetUi() {
  clearSessionTimers();
  session = null;
  members = null;
  showMeter(false);
  els.progress.textContent = '';
  els.progress.style.display = 'none';
  els.verdict.textContent = '';
  els.stop.hidden = true;
  els.retry.hidden = true;
  els.apply.hidden = true;
  els.again.hidden = true;
  els.start.hidden = false;
  els.start.disabled = false;
}

els.start.addEventListener('click', async () => {
  els.start.disabled = true;
  try {
    setStatus('Asking for the microphone…');
    await openMic();
    setStatus('Pausing music for the walkthrough…');
    const data = await post('start');
    if (!data.ok) {
      setStatus(data.error || 'Could not start.', 'bad');
      els.start.disabled = false;
      return;
    }
    members = data.members;
    els.start.hidden = true;
    els.stop.hidden = false;
    runStep('left');
  } catch (e) {
    setStatus(e.message, 'bad');
    els.start.disabled = false;
  }
});

els.stop.addEventListener('click', async () => {
  try { await post('stop'); } catch (e) { /* soft */ }
  resetUi();
  setStatus('Stopped — music is back.', '');
});

els.retry.addEventListener('click', () => {
  const ch = els.retry.dataset.channel || 'left';
  els.retry.hidden = true;
  runStep(ch);
});

els.apply.addEventListener('click', async () => {
  els.apply.disabled = true;
  try {
    const data = await post('apply');
    if (data.ok) {
      setStatus('Trims applied — run the walkthrough again to '
        + 'verify.', 'ok');
      els.apply.hidden = true;
    } else {
      const failed = Object.values(data.writes || {})
        .filter((w) => !w.ok).map((w) => w.label).join(', ');
      setStatus('Apply failed for: ' + (failed || 'unknown')
        + ' — check the pair and try again.', 'bad');
    }
  } catch (e) {
    setStatus(e.message, 'bad');
  }
  els.apply.disabled = false;
});

els.again.addEventListener('click', async () => {
  try { await post('reset'); } catch (e) { /* soft */ }
  resetUi();
  setStatus('');
});

window.addEventListener('pagehide', () => {
  // Best-effort cleanup; the server's session watchdog is the real
  // guarantee that renderers come back.
  if (session) {
    navigator.sendBeacon && fetch('stop', {
      method: 'POST', headers: jsonHeaders(), body: '{}',
      keepalive: true,
    }).catch(() => {});
  }
  closeMic().catch(() => {});
});

(async function init() {
  try {
    const resp = await fetch('status');
    const st = await resp.json();
    if (!st.bonded) {
      setStatus('No stereo pair is bonded — set one up at '
        + 'jts.local/rooms first.', 'bad');
      els.start.disabled = true;
    } else if (st.role !== 'leader') {
      setStatus('Open this page on the pair leader.', 'bad');
      els.start.disabled = true;
    } else if (st.phase === 'measuring') {
      // Another phone mid-walkthrough (or a stale session pre-watchdog).
      setStatus('A balance session is already running — Stop it to '
        + 'take over.', 'bad');
      els.stop.hidden = false;
    }
    if (st.ramp_duration_s) rampDuration = st.ramp_duration_s;
  } catch (e) {
    setStatus('Could not reach the speaker: ' + e.message, 'bad');
    els.start.disabled = true;
  }
})();
