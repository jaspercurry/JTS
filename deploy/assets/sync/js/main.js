// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Stereo-pair sync measurement. The server plays the marker through the
// bonded chain; this page records a short mono WAV and uploads it for
// correlation analysis.

import {
  createMonoRecorder,
  delayMs,
  float32ToWavBlob,
} from '/assets/shared/js/measurement-audio.js';
// jsonHeaders attaches X-CSRF-Token AND the X-JTS-Token control token
// (meta[name=jts-control-token]); the latter is required for /sync/apply,
// which writes the leader's token-gated /grouping/set.
import { jsonHeaders } from '/assets/shared/js/http.js';

const csrf =
  (document.querySelector('meta[name="jts-csrf"]') || {}).content || '';
const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');
const startBtn = document.getElementById('start');
const playBtn = document.getElementById('play');
const applyBtn = document.getElementById('apply');
const stopBtn = document.getElementById('stop');

function setStatus(text) {
  statusEl.textContent = text || '';
}

function render(data) {
  resultEl.textContent = JSON.stringify(data, null, 2);
}

async function postJson(path, body) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify(body || {}),
  });
  const data = await resp.json();
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || (path + ' HTTP ' + resp.status));
  }
  return data;
}

async function recordMarker() {
  const recorder = await createMonoRecorder({ sampleRate: 48000 });
  try {
    recorder.start();
    await postJson('play', {});
    await delayMs(3300);
    const samples = await recorder.stop({ timeoutMs: 1200 });
    const sampleRate = recorder.context.sampleRate || 48000;
    return float32ToWavBlob(samples, sampleRate);
  } finally {
    await recorder.close();
  }
}

async function analyzeBlob(blob) {
  const resp = await fetch('analyze', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'audio/wav' },
    body: blob,
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || 'analyze failed');
  return data;
}

startBtn.addEventListener('click', async () => {
  try {
    setStatus('Opening measurement window...');
    const data = await postJson('start', {});
    render(data);
    playBtn.disabled = false;
    applyBtn.disabled = true;
    setStatus('Ready. Hold this phone at the listening position.');
  } catch (err) {
    setStatus(err.message);
  }
});

playBtn.addEventListener('click', async () => {
  try {
    playBtn.disabled = true;
    setStatus('Recording marker...');
    const wav = await recordMarker();
    setStatus('Analyzing...');
    const data = await analyzeBlob(wav);
    render(data);
    applyBtn.disabled = !data.ok;
    setStatus(data.ok ? 'Measurement ready.' : 'Measurement needs a retry.');
  } catch (err) {
    setStatus(err.message);
  } finally {
    playBtn.disabled = false;
  }
});

applyBtn.addEventListener('click', async () => {
  try {
    setStatus('Applying delay...');
    const data = await postJson('apply', {});
    render(data);
    setStatus('Applied.');
    applyBtn.disabled = true;
  } catch (err) {
    setStatus(err.message);
  }
});

stopBtn.addEventListener('click', async () => {
  try {
    const data = await postJson('stop', {});
    render(data);
    setStatus('Stopped.');
    playBtn.disabled = true;
    applyBtn.disabled = true;
  } catch (err) {
    setStatus(err.message);
  }
});
