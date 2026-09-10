// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Seat-level leveling card for /sound/speaker/ — starts, polls, and stops
// jasper-seat-level (jasper/web/sound_seat_level.py). Self-contained: main.js
// only calls initSeatLevel() once and, on pagehide, isSeatLevelRunning() /
// stopSeatLevel() — the same safety-stop shape it already gives the
// volume-floor tone, so a leveling pass never keeps ramping the household's
// volume after the page is gone.

import { getJSON, postJSON } from "/assets/shared/js/http.js";

var POLL_MS = 1500;
var pollTimer = null;
var running = false;

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
    var detail = payload.detail && typeof payload.detail === 'object' ? payload.detail : {};
    var reached = typeof detail.measured_db_spl === 'number' ?
      detail.measured_db_spl.toFixed(1) + ' dB SPL' : 'the target level';
    e.status.textContent = 'Reached ' + reached + ' and banked the reference.';
  } else if (payload.state === 'refused') {
    var d = payload.detail;
    var reason = d && typeof d === 'object' ? (d.detail || d.reason) : d;
    e.status.textContent = reason ? String(reason) : 'The last leveling pass was refused.';
  } else {
    e.status.textContent = 'Mic ready: ' + (mic.label || 'calibrated') + '.';
  }
}

function schedulePoll() {
  if (pollTimer) return;
  pollTimer = setInterval(function() {
    if (document.hidden) return;
    fetchStatus();
  }, POLL_MS);
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function fetchStatus() {
  return getJSON('./active-speaker/seat-level/status')
    .then(function(payload) {
      renderState(payload);
      if (payload.state === 'running') schedulePoll(); else stopPoll();
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
      schedulePoll();
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
  e.start.addEventListener('click', startLeveling);
  e.stop.addEventListener('click', function() { stopSeatLevel(); });
  fetchStatus();
}
