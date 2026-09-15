// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { getJSON, postJSON, startPolling } from "/assets/shared/js/http.js";

var running = false;
var stopPoll = null;
var mountedCard = null;

export function renderSeatLevelCard() {
  return `<section class="info-card" id="seat-level-card">
    <h2 class="eyebrow">Seat-level leveling</h2>
    <p class="form-hint">Ramp the volume until a calibrated mic at your
    listening seat reads your target level, then bank it as the crossover
    session's measurement reference.</p>
    <div class="field">
      <label for="seat-level-target">Target level (dB SPL)</label>
      <input id="seat-level-target" type="number" step="0.5" inputmode="decimal"
             autocomplete="off">
    </div>
    <div class="form-actions">
      <button type="button" class="btn btn--ghost" id="seat-level-start">Start leveling</button>
      <button type="button" class="btn btn--danger" id="seat-level-stop" hidden>Stop</button>
    </div>
    <p class="form-hint" id="seat-level-status" role="status" aria-live="polite"></p>
  </section>`;
}

function els() {
  return {
    card: document.getElementById('seat-level-card'),
    target: document.getElementById('seat-level-target'),
    start: document.getElementById('seat-level-start'),
    stop: document.getElementById('seat-level-stop'),
    status: document.getElementById('seat-level-status')
  };
}

function renderState(payload) {
  var e = els();
  if (!e.card) return;
  var mic = payload.mic || {};
  running = payload.state === 'running';
  e.start.hidden = running;
  e.stop.hidden = !running;
  e.target.disabled = running;
  if (!e.target.value && typeof payload.default_target_db_spl === 'number') {
    e.target.value = payload.default_target_db_spl;
  }
  if (!mic.available) {
    e.start.disabled = true;
    e.status.textContent = running ? e.status.textContent :
      'No calibrated measurement mic is set up for this household yet. ' +
      'Run mic calibration first.';
    return;
  }
  e.start.disabled = running;
  if (running) {
    e.status.textContent = 'Leveling toward ' + payload.target_db_spl +
      ' dB SPL using ' + (mic.label || 'the household mic') + '…';
  } else if (payload.state === 'converged') {
    var reached = typeof payload.measured_db_spl === 'number' ?
      payload.measured_db_spl.toFixed(1) + ' dB SPL' : 'the target level';
    e.status.textContent = 'Reached ' + reached + ' and banked the reference.';
  } else if (payload.state === 'refused') {
    e.status.textContent = payload.detail ?
      String(payload.detail) : 'The last leveling pass was refused.';
  } else {
    e.status.textContent = 'Mic ready: ' + (mic.label || 'calibrated') + '.';
  }
}

// A pass is a rare, occasional action -- poll only while one is running,
// via the shared scheduler (startPolling), rather than forever.
function schedulePoll() {
  if (stopPoll) return;
  stopPoll = startPolling(fetchStatus, {intervalMs: 1500});
}

function stopPollNow() {
  if (stopPoll) { stopPoll(); stopPoll = null; }
}

function fetchStatus() {
  return getJSON('./active-speaker/seat-level/status')
    .then(function(payload) {
      renderState(payload);
      if (payload.state === 'running') schedulePoll(); else stopPollNow();
      return payload;
    })
    .catch(function() {});
}

function startLeveling() {
  var e = els();
  var target = parseFloat(e.target.value);
  if (!isFinite(target)) {
    e.status.textContent = 'Enter a target level in dB SPL.';
    return;
  }
  e.start.disabled = true;
  postJSON('./active-speaker/seat-level/start', {target_db_spl: target})
    .then(function(payload) {
      if (payload && payload.status === 'refused') {
        e.status.textContent = payload.detail || 'Could not start leveling.';
        e.start.disabled = false;
        return;
      }
      fetchStatus();
    })
    .catch(function(err) {
      e.status.textContent = 'Could not start leveling: ' + err.message;
      e.start.disabled = false;
    });
}

// The big stop control. No confirm dialog: this is the emergency exit, and a
// confirm step is friction the owner's ruling (#2761) explicitly rejected —
// "a big stop button that saves them if anything goes wrong."
export function stopSeatLevel(options) {
  options = options || {};
  return postJSON('./active-speaker/seat-level/stop', {}, {keepalive: !!options.keepalive})
    .then(function() {
      if (!options.quiet) fetchStatus();
    })
    .catch(function() {});
}

export function isSeatLevelRunning() {
  return running;
}

export function initSeatLevel() {
  var e = els();
  if (!e.card) return;
  if (mountedCard) { e.card.replaceWith(mountedCard); return; }
  mountedCard = e.card;
  e.start.addEventListener('click', startLeveling);
  e.stop.addEventListener('click', function() { stopSeatLevel(); });
  fetchStatus();
}
