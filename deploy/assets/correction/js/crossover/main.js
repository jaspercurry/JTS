// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { jtsConfirm } from '/assets/shared/js/dialog.js';
import { csrfHeaders, postJSON } from '/assets/shared/js/http.js';
import {
  DEFAULT_SAMPLE_RATE,
  createMonoRecorder,
  delayMs,
  float32ToWavBlob,
  micCaptureSupport,
} from '/assets/shared/js/measurement-audio.js';

const REQUIRED_SR = 48000;
const CAPTURE_PREROLL_MS = 250;
const CAPTURE_POSTROLL_MS = 650;

const els = {
  support: document.getElementById('mic-support'),
  supportMessage: document.getElementById('mic-support-message'),
  checkMic: document.getElementById('check-mic'),
  refresh: document.getElementById('refresh-status'),
  drivers: document.getElementById('driver-targets'),
  summed: document.getElementById('summed-targets'),
  status: document.getElementById('capture-status'),
};

let support = micCaptureSupport();
let currentPayload = null;

function setStatus(message, tone = '') {
  els.status.textContent = message || '';
  els.status.dataset.tone = tone;
}

function setSupport(nextSupport) {
  support = nextSupport;
  els.support.dataset.tone = support.ok ? 'ok' : 'bad';
  els.supportMessage.textContent = support.message;
  if (currentPayload) render();
}

async function fetchJSON(path) {
  const resp = await fetch(path, {cache: 'no-store'});
  let data = null;
  try { data = await resp.json(); } catch (_) { /* non-JSON */ }
  if (!resp.ok) {
    throw new Error(data && data.error ? data.error : 'HTTP ' + resp.status);
  }
  return data;
}

async function postWav(path, params, blob) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, String(value));
    }
  }
  const url = path + (query.toString() ? '?' + query.toString() : '');
  const resp = await fetch(url, {
    method: 'POST',
    headers: csrfHeaders({'Content-Type': 'audio/wav'}),
    body: blob,
  });
  let data = null;
  try { data = await resp.json(); } catch (_) { /* non-JSON */ }
  if (!resp.ok) {
    throw new Error(data && data.error ? data.error : 'HTTP ' + resp.status);
  }
  return data;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'disabled') node.disabled = !!value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}

function targetId(groupId, role) {
  return String(groupId || '') + ':' + String(role || '');
}

function roleLabel(role) {
  const value = String(role || '');
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : 'Driver';
}

function latestDriver(target) {
  const summary = currentPayload && currentPayload.measurements &&
    currentPayload.measurements.summary || {};
  const latest = summary.latest_driver_measurements || {};
  return latest[targetId(target.speaker_group_id, target.role)] || null;
}

function latestSummed(groupId) {
  const summary = currentPayload && currentPayload.measurements &&
    currentPayload.measurements.summary || {};
  const latest = summary.latest_summed_tests || {};
  return latest[groupId] || null;
}

function latestSummedValidation(groupId) {
  const summary = currentPayload && currentPayload.measurements &&
    currentPayload.measurements.summary || {};
  const latest = summary.latest_summed_validations || {};
  return latest[groupId] || null;
}

function issueMessage(payload, fallback) {
  const issues = payload && Array.isArray(payload.issues) ? payload.issues : [];
  for (const issue of issues) {
    const message = issue && (issue.message || issue.label || issue.code);
    if (message) return String(message);
  }
  const playback = payload && payload.playback || {};
  const playbackIssues = Array.isArray(playback.issues) ? playback.issues : [];
  for (const issue of playbackIssues) {
    const message = issue && (issue.message || issue.label || issue.code);
    if (message) return String(message);
  }
  if (payload && payload.next_step) return String(payload.next_step);
  if (payload && payload.reason) return String(payload.reason);
  return fallback;
}

function errorMessage(err) {
  return issueMessage(err && err.body, err && err.message || String(err));
}

function pendingDriver() {
  const commission = currentPayload && currentPayload.commission || {};
  const ramp = commission.ramp && typeof commission.ramp === 'object'
    ? commission.ramp : {};
  const pending = ramp.pending && typeof ramp.pending === 'object'
    ? ramp.pending : null;
  if (!pending) return null;
  return {
    speaker_group_id: ramp.speaker_group_id || '',
    role: pending.role || '',
    playback_id: pending.playback_id || '',
    gain_db: pending.gain_db,
  };
}

function isPendingTarget(target) {
  const pending = pendingDriver();
  return !!(pending &&
    String(pending.speaker_group_id) === String(target.speaker_group_id || '') &&
    String(pending.role) === String(target.role || ''));
}

function assertCapturePlayback(payload) {
  const playback = payload && payload.playback || {};
  if (payload && payload.status === 'completed' && playback.audio_emitted) {
    return playback;
  }
  throw new Error(issueMessage(payload, 'The measurement sweep did not play.'));
}

function renderEmpty(container, message) {
  container.replaceChildren(el('p', {class: 'form-hint', text: message}));
}

function renderDrivers() {
  const targets = currentPayload && currentPayload.targets &&
    Array.isArray(currentPayload.targets.drivers)
    ? currentPayload.targets.drivers : [];
  if (!targets.length) {
    renderEmpty(els.drivers, 'No active crossover driver targets are saved yet.');
    return;
  }
  const rows = targets.map((target) => {
    const latest = latestDriver(target);
    const playbackId = latest && latest.playback_id || '';
    const confirmed = !!(latest && latest.captured);
    const hasMicEvidence = !!(latest && latest.acoustic);
    const pending = isPendingTarget(target);
    const canCapture = support.ok && playbackId && confirmed && !pending;
    const playButton = el('button', {
      class: 'btn btn--ghost',
      type: 'button',
      disabled: pending,
      'data-driver-action': 'start',
      'data-group-id': target.speaker_group_id || '',
      'data-role': target.role || '',
      text: 'Play test',
    });
    const confirmButton = el('button', {
      class: 'btn btn--primary',
      type: 'button',
      'data-driver-action': 'confirm',
      text: 'I hear it',
    });
    const abortButton = el('button', {
      class: 'btn btn--ghost',
      type: 'button',
      'data-driver-action': 'abort',
      text: 'Stop',
    });
    const captureButton = el('button', {
      class: 'btn btn--primary',
      type: 'button',
      disabled: !canCapture,
      'data-capture-kind': 'driver',
      'data-group-id': target.speaker_group_id || '',
      'data-role': target.role || '',
      'data-playback-id': playbackId,
      'data-test-level-dbfs': latest && latest.test_level_dbfs || '',
      text: hasMicEvidence ? 'Record again' : 'Record mic',
    });
    const actions = pending
      ? [confirmButton, abortButton]
      : [playButton, captureButton];
    const meta = pending
      ? 'Waiting for confirmation from the active driver test.'
      : (hasMicEvidence
        ? 'Mic evidence saved for this driver.'
        : (confirmed
          ? (support.ok ? 'Ready for secure mic sweep.' : support.message)
          : 'Play and confirm this driver before recording mic evidence.'));
    return el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {
          class: 'measurement-row__title',
          text: (target.speaker_group_label || target.speaker_group_id || 'Speaker') +
            ' · ' + roleLabel(target.role),
        }),
        el('p', {class: 'measurement-row__meta', text: meta}),
      ]),
      el('div', {class: 'measurement-row__actions'}, actions),
    ]);
  });
  els.drivers.replaceChildren(...rows);
}

function renderSummed() {
  const targets = currentPayload && currentPayload.targets &&
    Array.isArray(currentPayload.targets.summed)
    ? currentPayload.targets.summed : [];
  if (!targets.length) {
    renderEmpty(els.summed, 'No active crossover groups are saved yet.');
    return;
  }
  const rows = targets.map((target) => {
    const latestTest = latestSummed(target.speaker_group_id);
    const latestValidation = latestSummedValidation(target.speaker_group_id);
    const testId = latestTest &&
      (latestTest.summed_test_id || latestTest.playback_id) || '';
    const hasAudibleTest = !!(latestTest && latestTest.captured &&
      latestTest.audio_emitted);
    const hasMicEvidence = !!(latestValidation && latestValidation.acoustic);
    const canCapture = support.ok && hasAudibleTest && testId;
    const playButton = el('button', {
      class: 'btn btn--ghost',
      type: 'button',
      'data-summed-action': 'start',
      'data-group-id': target.speaker_group_id || '',
      text: 'Play combined test',
    });
    const captureButton = el('button', {
      class: 'btn btn--primary',
      type: 'button',
      disabled: !canCapture,
      'data-capture-kind': 'summed',
      'data-group-id': target.speaker_group_id || '',
      'data-summed-test-id': testId,
      text: hasMicEvidence ? 'Record again' : 'Record mic',
    });
    const meta = hasMicEvidence
      ? 'Summed crossover evidence is saved.'
      : (canCapture
        ? 'Ready for secure mic sweep.'
        : (hasAudibleTest ? support.message :
          'Run the combined crossover test before recording mic evidence.'));
    return el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {
          class: 'measurement-row__title',
          text: target.speaker_group_label || target.speaker_group_id || 'Speaker',
        }),
        el('p', {class: 'measurement-row__meta', text: meta}),
      ]),
      el('div', {class: 'measurement-row__actions'}, [playButton, captureButton]),
    ]);
  });
  els.summed.replaceChildren(...rows);
}

function render() {
  renderDrivers();
  renderSummed();
}

async function refresh() {
  setStatus('Refreshing…');
  currentPayload = await fetchJSON('status');
  render();
  setStatus('Ready.', 'ok');
}

async function captureBlob(playbackFn) {
  const recorder = await createMonoRecorder({sampleRate: REQUIRED_SR});
  try {
    recorder.start();
    await delayMs(CAPTURE_PREROLL_MS);
    const playbackPayload = await playbackFn();
    assertCapturePlayback(playbackPayload);
    await delayMs(CAPTURE_POSTROLL_MS);
    const samples = await recorder.stop({timeoutMs: 1800});
    const sampleRate = recorder.context && recorder.context.sampleRate ||
      DEFAULT_SAMPLE_RATE;
    return {
      blob: float32ToWavBlob(samples, sampleRate),
      playback: playbackPayload,
    };
  } finally {
    await recorder.close();
  }
}

async function driverAction(button) {
  const action = button.dataset.driverAction;
  const prior = button.textContent;
  button.disabled = true;
  button.textContent = action === 'start' ? 'Playing…' : 'Saving…';
  try {
    let payload = null;
    if (action === 'start') {
      setStatus('Playing driver test…');
      payload = await postJSON('driver-test', {
        speaker_group_id: button.dataset.groupId,
        role: button.dataset.role,
        force: true,
      });
    } else if (action === 'confirm') {
      setStatus('Saving driver confirmation…');
      payload = await postJSON('driver-confirm', {
        outcome: 'heard_correct_driver',
      });
    } else {
      setStatus('Stopping driver test…');
      payload = await postJSON('driver-abort', {});
    }
    setStatus(issueMessage(payload, 'Updated.'), 'ok');
    await refresh();
  } catch (err) {
    setStatus(errorMessage(err), 'bad');
  } finally {
    button.textContent = prior;
    render();
  }
}

async function summedAction(button) {
  const prior = button.textContent;
  button.disabled = true;
  button.textContent = 'Playing…';
  setStatus('Playing combined crossover test…');
  try {
    const payload = await postJSON('summed-test', {
      speaker_group_id: button.dataset.groupId,
      audio: true,
      duration_ms: 500,
    });
    setStatus(issueMessage(payload, 'Combined test complete.'), 'ok');
    await refresh();
  } catch (err) {
    setStatus(errorMessage(err), 'bad');
  } finally {
    button.textContent = prior;
    render();
  }
}

async function record(button) {
  if (!support.ok) {
    setStatus(support.message, 'bad');
    return;
  }
  const kind = button.dataset.captureKind;
  const label = kind === 'driver'
    ? roleLabel(button.dataset.role) + ' driver'
    : 'summed crossover';
  const ok = await jtsConfirm(
    'Record the ' + label + ' with this microphone while JTS plays a sweep?',
    {danger: false},
  );
  if (!ok) return;
  const prior = button.textContent;
  button.disabled = true;
  button.textContent = 'Recording…';
  setStatus('Recording mic capture while the speaker plays a sweep…');
  try {
    const playbackBody = {
      speaker_group_id: button.dataset.groupId,
    };
    if (kind === 'driver') playbackBody.role = button.dataset.role;
    const recorded = await captureBlob(() => postJSON(
      kind === 'driver' ? 'driver-capture-sweep' : 'summed-capture-sweep',
      playbackBody,
    ));
    const blob = recorded.blob;
    const playbackPayload = recorded.playback || {};
    if (kind === 'driver') {
      await postWav('driver-capture', {
        speaker_group_id: button.dataset.groupId,
        role: button.dataset.role,
        playback_id: playbackPayload.playback_id || button.dataset.playbackId,
        test_level_dbfs: playbackPayload.test_level_dbfs ||
          button.dataset.testLevelDbfs,
      }, blob);
    } else {
      await postWav('summed-capture', {
        speaker_group_id: button.dataset.groupId,
        summed_test_id: playbackPayload.summed_test_id ||
          button.dataset.summedTestId,
        playback_id: playbackPayload.playback_id || button.dataset.summedTestId,
      }, blob);
    }
    setStatus('Capture saved.', 'ok');
    await refresh();
  } catch (err) {
    setStatus('Could not record capture: ' + (err && err.message || err), 'bad');
  } finally {
    button.textContent = prior;
    render();
  }
}

els.checkMic.addEventListener('click', async () => {
  setSupport(micCaptureSupport());
  if (!support.ok) return;
  setStatus('Microphone is available.', 'ok');
});

els.refresh.addEventListener('click', () => {
  refresh().catch((err) => setStatus(err.message, 'bad'));
});

document.addEventListener('click', (ev) => {
  const driverButton = ev.target.closest('[data-driver-action]');
  if (driverButton) {
    driverAction(driverButton);
    return;
  }
  const summedButton = ev.target.closest('[data-summed-action]');
  if (summedButton) {
    summedAction(summedButton);
    return;
  }
  const button = ev.target.closest('[data-capture-kind]');
  if (!button) return;
  record(button);
});

setSupport(support);
refresh().catch((err) => setStatus(err.message, 'bad'));
