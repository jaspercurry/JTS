// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Room correction — measurement + DSP wizard.
//
// Static ES module served from /assets/correction/js/ (revalidated by
// nginx, same delivery model as /system/ and /sound/). Relocated VERBATIM
// from the previously-inline IIFE in jasper/web/correction_setup.py; the
// only changes are mechanical: (a) the CSRF helpers (csrfHeaders /
// jsonHeaders) and the confirm/alert dialog (jtsConfirm / jtsAlert) are now
// imported from the shared modules instead of being string-substituted /
// inlined at render time, and (b) REQUIRED_SR is the literal 48000 (was the
// __REQUIRED_SR__ Python substitution). The getUserMedia mic capture, the
// AudioWorklet RMS/capture pipeline, the /status polling state machine, the
// autolevel ramp, the sweep upload flow, and the canvas chart math are
// UNCHANGED — this page can only be fully exercised on real Pi hardware
// (HTTPS secure context + mic + CamillaDSP), so it was moved, not
// refactored. The IIFE wrapper is kept so the body's var/function
// declarations are byte-for-byte the original. See
// docs/HANDOFF-management-ui.md (restyle-in-place) and
// docs/HANDOFF-correction.md.
import { csrfHeaders, jsonHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";
// This page's local copy was named escapeText; import the shared escapeHtml
// under that name so its call sites stay byte-for-byte. The local copy coerced
// with `|| ''` vs the shared `?? ''`; every call site here passes a string or
// its own `|| 'fallback'`, so no falsy non-string reaches it — output is
// unchanged.
import { escapeHtml as escapeText } from "/assets/shared/js/escape.js";
(function () {
  'use strict';

  var REQUIRED_SR = 48000;  // REQUIRED_SAMPLE_RATE — see jasper/web/correction_setup.py

  var pageRoot = document.querySelector('main.correction-stack');
  var relayConfigured = !!(
    pageRoot && pageRoot.dataset.captureRelayEnabled === '1'
  );
  var relayMode = relayConfigured;
  var relayPanel = document.getElementById('relay-panel');
  var relayStatus = document.getElementById('relay-status');
  var relayLinkRow = document.getElementById('relay-link-row');
  var relayTapLink = document.getElementById('relay-tap-link');
  var relayStartBtn = document.getElementById('relay-start-capture');
  var localCaptureFallbackBtn = document.getElementById('local-capture-fallback');
  var localInputRow = document.getElementById('local-input-row');
  var localInputHint = document.getElementById('local-input-hint');
  var startBtn = document.getElementById('start');
  var inputDeviceSelect = document.getElementById('input-device-select');
  var refreshInputsBtn = document.getElementById('refresh-inputs');
  var micModelSelect = document.getElementById('mic-model-select');
  var micSerialInput = document.getElementById('mic-serial');
  var micOrientationSelect = document.getElementById('mic-orientation');
  var serialRow = document.getElementById('serial-row');
  var uploadRow = document.getElementById('upload-row');
  var calibrationFileInput = document.getElementById('calibration-file');
  var calibrationSignSelect = document.getElementById('calibration-sign');
  var fetchCalibrationBtn = document.getElementById('fetch-calibration');
  var uploadCalibrationBtn = document.getElementById('upload-calibration');
  var calibrationStatus = document.getElementById('calibration-status');
  var calibrationPreview = document.getElementById('calibration-preview');
  var currentCorrectionBanner = document.getElementById('current-correction');
  var currentCorrectionLabel = document.getElementById('current-correction-label');
  var currentCorrectionResetBtn = document.getElementById('current-correction-reset');
  var constraintsBlock = document.getElementById('constraints');
  var rowsTbody = document.getElementById('constraint-rows');
  var errBanner = document.getElementById('err-banner');
  var browserAudioReport = document.getElementById('browser-audio-report');
  var levelBar = document.getElementById('level-bar-fill');
  var levelReadout = document.getElementById('level-db');
  var measureSection = document.getElementById('measure-section');
  var stateBadge = document.getElementById('state-badge');
  var stateDetail = document.getElementById('state-detail');
  var qualityBanner = document.getElementById('quality-banner');
  var autolevelBtn = document.getElementById('autolevel');
  var autolevelLockBtn = document.getElementById('autolevel-lock');
  var autolevelCancelBtn = document.getElementById('autolevel-cancel');
  var autolevelStatus = document.getElementById('autolevel-status');
  var autolevelLine = document.getElementById('autolevel-line');
  var autolevelDetail = document.getElementById('autolevel-detail');
  var autolevelHint = document.getElementById('autolevel-hint');
  var runBtn = document.getElementById('run-measurement');
  var repeatBtn = document.getElementById('repeat-position');
  var continueBtn = document.getElementById('continue-position');
  var applyBtn = document.getElementById('apply-correction');
  var verifyBtn = document.getElementById('verify-correction');
  var resetBtn = document.getElementById('reset-correction');
  var cancelMeasureBtn = document.getElementById('cancel-measurement');
  var positionsSelect = document.getElementById('positions-select');
  var repeatMainPosition = document.getElementById('repeat-main-position');
  var repeatMainPositionRow = document.getElementById('repeat-main-position-row');
  var repeatMainPositionHint = document.getElementById('repeat-main-position-hint');
  var targetSelect = document.getElementById('target-select');
  var strategySelect = document.getElementById('strategy-select');
  var positionPrompt = document.getElementById('position-prompt');
  var positionCurrent = document.getElementById('position-current');
  var positionTotal = document.getElementById('position-total');
  var resultSection = document.getElementById('result-section');
  var resultsSummary = document.getElementById('results-summary');
  var chartSmoothing = document.getElementById('chart-smoothing');
  var chartShowSpread = document.getElementById('chart-show-spread');
  var chartShowFilter = document.getElementById('chart-show-filter');
  var chartShowBand = document.getElementById('chart-show-band');
  var canvas = document.getElementById('chart');
  var peqList = document.getElementById('peq-list');
  var verifySummary = document.getElementById('verify-summary');
  var designReport = document.getElementById('design-report');
  var confidencePanel = document.getElementById('confidence-panel');
  var runtimeIntegrityPanel = document.getElementById('runtime-integrity-panel');
  var loadSessionsBtn = document.getElementById('load-sessions');
  var sessionHistory = document.getElementById('session-history');
  var sessionReport = document.getElementById('session-report');

  var ctx = null;
  var micStream = null;
  var workletNode = null;
  var pollTimer = null;
  var sessionId = null;
  var currentState = null;
  var lastResult = null;
  var lastVerify = null;
  var inVerifyMode = false;
  var captureMode = 'measurement';
  // Latest mic RMS in dBFS, updated by the AudioWorklet at ~20 Hz.
  // The autolevel loop reads this to decide when the speaker level
  // has reached the target range.
  var latestMicRmsDb = -120;
  var lastNoiseFloorDb = null;
  var autolevelRmsBuffer = [];  // recent dB samples for smoothing
  var selectedCalibrationId = null;
  var selectedCalibrationMeta = null;
  var selectedInputDevice = null;
  var wakeLockSentinel = null;

  // For the three audio-processing flags (echoCancellation,
  // noiseSuppression, autoGainControl), iOS Safari often returns
  // `undefined` from getSettings() rather than echoing back the
  // value we requested. That's NOT a bug — it means Safari simply
  // doesn't surface the setting (and on iOS, these features are
  // off by default for getUserMedia anyway). We treat undefined
  // and false as both "constraint was honored." Only TRUE counts
  // as bad.
  function isAudioProcessingOff(value) {
    return value === false || value === undefined || value === null;
  }

  function stopMicStream() {
    if (micStream) {
      micStream.getTracks().forEach(function (t) { t.stop(); });
      micStream = null;
    }
    if (ctx && ctx.state !== 'closed') {
      ctx.close().catch(function () {});
    }
    ctx = null;
    workletNode = null;
  }

  function currentPathname() {
    var loc = window.location || {};
    if (typeof loc.pathname === 'string') return loc.pathname;
    var href = String(loc.href || '');
    var pathish = href.split('#')[0].split('?')[0];
    var schemeIdx = pathish.indexOf('://');
    if (schemeIdx !== -1) {
      var slashIdx = pathish.indexOf('/', schemeIdx + 3);
      return slashIdx === -1 ? '/' : pathish.slice(slashIdx);
    }
    return pathish || '';
  }

  function endpoint(path) {
    path = String(path || '').replace(/^\/+/, '');
    if (currentPathname().indexOf('/correction/') === 0) {
      return '/correction/' + path;
    }
    return path;
  }

  function hideEl(el, hidden) {
    if (!el) return;
    if (hidden) el.classList.add('hidden');
    else el.classList.remove('hidden');
  }

  function setRelayStatus(text, level) {
    if (!relayStatus) return;
    relayStatus.className = 'relay-status ' + (level || 'idle');
    relayStatus.textContent = text || '';
  }

  function renderRelayCapture(relay) {
    if (!relay) {
      hideEl(relayLinkRow, true);
      if (relayTapLink) relayTapLink.href = '#';
      return;
    }
    var tapLink = relay.tap_link || '';
    if (tapLink && relayTapLink) {
      relayTapLink.href = tapLink;
      hideEl(relayLinkRow, false);
    }
    if (relay.status === 'complete') {
      setRelayStatus('Phone capture received. Processing finished on the speaker.', 'ok');
    } else if (relay.status === 'failed') {
      setRelayStatus(relay.error || 'Phone capture failed. Cancel and try again.', 'bad');
    } else if (relay.status === 'starting') {
      setRelayStatus('Creating phone capture link…', 'idle');
    } else {
      setRelayStatus('Open the capture page on the phone and keep it awake until the sweep finishes.', 'idle');
    }
  }

  function setRelayMode(enabled) {
    relayMode = !!enabled;
    hideEl(relayPanel, !relayMode);
    hideEl(relayStartBtn, !relayMode);
    hideEl(localInputRow, relayMode);
    hideEl(localInputHint, relayMode);
    hideEl(startBtn, relayMode);
    hideEl(constraintsBlock, relayMode);
    hideEl(autolevelBtn, relayMode);
    hideEl(autolevelLockBtn, true);
    hideEl(autolevelCancelBtn, true);
    hideEl(autolevelStatus, true);
    hideEl(autolevelHint, relayMode);
    if (repeatMainPosition) {
      repeatMainPosition.checked = !relayMode;
      repeatMainPosition.disabled = relayMode;
    }
    hideEl(repeatMainPositionRow, relayMode);
    hideEl(repeatMainPositionHint, relayMode);
    if (relayMode) {
      stopMicStream();
      measureSection.classList.remove('hidden');
      runBtn.textContent = 'Start phone measurement';
      continueBtn.textContent = 'Create next phone capture';
      autolevelBtn.disabled = true;
      runBtn.disabled = false;
      if (relayStartBtn) {
        relayStartBtn.textContent = 'Create phone capture link';
        relayStartBtn.disabled = false;
      }
      setRelayStatus('Ready to create a phone capture link.', 'idle');
    } else {
      runBtn.textContent = 'Run measurement';
      continueBtn.textContent = 'Continue to next position';
      renderRelayCapture(null);
      setRelayStatus('', 'idle');
      runBtn.disabled = true;
      if (relayStartBtn) relayStartBtn.disabled = true;
      autolevelBtn.disabled = true;
    }
  }

  async function populateInputDevices(selectedId) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      return;
    }
    try {
      var devices = await navigator.mediaDevices.enumerateDevices();
      var inputs = devices.filter(function (d) { return d.kind === 'audioinput'; });
      var prior = selectedId || inputDeviceSelect.value || '';
      // iOS/CoreAudio reports a synthetic "default"/"communications" alias
      // of a physical mic on top of the real entry, so the same mic shows
      // up twice ("Default" + "iPhone"). Collapse by groupId, preferring the
      // entry with a concrete deviceId and a real label.
      var byGroup = {};
      inputs.forEach(function (d) {
        var key = d.groupId || d.deviceId;
        if (!key) { byGroup[d.deviceId || Math.random()] = d; return; }
        var cur = byGroup[key];
        if (!cur) { byGroup[key] = d; return; }
        var dAlias = (d.deviceId === 'default' || d.deviceId === 'communications');
        var curAlias = (cur.deviceId === 'default' || cur.deviceId === 'communications');
        if (curAlias && !dAlias) byGroup[key] = d;
        else if (curAlias === dAlias && !cur.label && d.label) byGroup[key] = d;
      });
      var unique = Object.keys(byGroup).map(function (k) { return byGroup[k]; });
      // A disabled placeholder (not a selectable value="" "Default mic")
      // forces an explicit choice; startMicCapture refuses an empty value
      // rather than silently capturing the OS default (built-in) mic.
      inputDeviceSelect.innerHTML = '';
      var placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.disabled = true;
      placeholder.textContent = unique.length
        ? 'Select a microphone…'
        : 'No mic found — tap “Refresh microphones”';
      inputDeviceSelect.appendChild(placeholder);
      unique.forEach(function (d, idx) {
        var opt = document.createElement('option');
        opt.value = d.deviceId;
        opt.textContent = d.label || ('Microphone ' + (idx + 1));
        inputDeviceSelect.appendChild(opt);
      });
      inputDeviceSelect.value = prior;
      if (!inputDeviceSelect.value) placeholder.selected = true;
    } catch (e) {
      console.warn('enumerateDevices failed', e);
    }
  }

  // Remember the serial that produced a successful calibration, keyed by mic
  // model, in this browser only. Raw serials are kept off the Pi by design
  // (the server stores serial_hash, not the serial), so the device the user
  // measures from is the right home for the convenience auto-fill.
  var SAVED_SERIALS_KEY = 'jts.correction.serials';
  function loadSavedSerial(model) {
    try {
      var map = JSON.parse(localStorage.getItem(SAVED_SERIALS_KEY) || '{}');
      return (model && map[model]) || '';
    } catch (e) {
      return '';  // localStorage blocked (private mode) — non-fatal
    }
  }
  function saveSerial(model, serial) {
    if (!model || model === 'other' || !serial) return;
    try {
      var map = JSON.parse(localStorage.getItem(SAVED_SERIALS_KEY) || '{}');
      map[model] = serial;
      localStorage.setItem(SAVED_SERIALS_KEY, JSON.stringify(map));
    } catch (e) { /* non-fatal */ }
  }

  function updateMicCalibrationRows() {
    var model = micModelSelect.value;
    selectedCalibrationId = null;
    selectedCalibrationMeta = null;
    calibrationPreview.classList.add('hidden');
    calibrationPreview.textContent = '';
    calibrationStatus.className = 'mic-status';
    if (!model) {
      serialRow.classList.add('hidden');
      uploadRow.classList.add('hidden');
      calibrationStatus.textContent =
        'No calibration loaded. This is okay for a quick check, but a calibrated mic is recommended before trusting filter decisions.';
    } else if (model === 'other') {
      serialRow.classList.add('hidden');
      uploadRow.classList.remove('hidden');
      calibrationStatus.textContent =
        'Upload a calibration file for this microphone.';
    } else {
      serialRow.classList.remove('hidden');
      uploadRow.classList.remove('hidden');
      calibrationStatus.textContent =
        'Enter the mic serial so JTS can fetch the calibration file. Upload is available as a fallback.';
      // If we already know this mic's serial from a prior successful fetch,
      // fill it in and fetch automatically so the user doesn't re-type or tap
      // Fetch. Only when the field is empty; fetchCalibration fails soft if the
      // vendor is unreachable, leaving the serial filled for a manual retry.
      var saved = loadSavedSerial(model);
      if (saved && !micSerialInput.value.trim()) {
        micSerialInput.value = saved;
        fetchCalibration();
      }
    }
  }

  function invalidateLoadedCalibration() {
    if (!selectedCalibrationId && !selectedCalibrationMeta) return;
    selectedCalibrationId = null;
    selectedCalibrationMeta = null;
    calibrationPreview.classList.add('hidden');
    calibrationPreview.textContent = '';
    calibrationStatus.className = 'mic-status bad';
    if (micModelSelect.value === 'other') {
      calibrationStatus.textContent =
        'Calibration settings changed. Upload the file again before measuring.';
    } else {
      calibrationStatus.textContent =
        'Calibration settings changed. Fetch or upload again before measuring.';
    }
  }

  function showCalibrationLoaded(payload) {
    selectedCalibrationMeta = payload.calibration || null;
    selectedCalibrationId = selectedCalibrationMeta ?
      selectedCalibrationMeta.calibration_id : null;
    if (!selectedCalibrationId) return;
    calibrationStatus.className = 'mic-status ok';
    calibrationStatus.textContent =
      'Loaded ' + selectedCalibrationMeta.label + ' calibration (' +
      selectedCalibrationMeta.point_count + ' points).';
    if (payload.preview && payload.preview.freqs_hz) {
      var n = payload.preview.freqs_hz.length;
      var f0 = payload.preview.freqs_hz[0];
      var f1 = payload.preview.freqs_hz[n - 1];
      calibrationPreview.textContent =
        'Preview range: ' + Math.round(f0) + '–' + Math.round(f1) +
        ' Hz · hash ' + selectedCalibrationMeta.file_sha256.slice(0, 12);
      calibrationPreview.classList.remove('hidden');
    }
  }

  async function fetchCalibration() {
    var model = micModelSelect.value;
    var serial = micSerialInput.value.trim();
    if (!model || model === 'other') return;
    if (!serial) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent = 'Enter the microphone serial number first.';
      return;
    }
    fetchCalibrationBtn.disabled = true;
    calibrationStatus.className = 'mic-status';
    calibrationStatus.textContent = 'Fetching calibration from vendor…';
    try {
      var payload = await postJson('calibration/fetch', {
        model: model,
        serial: serial,
        orientation: micOrientationSelect.value || 'unknown'
      });
      showCalibrationLoaded(payload);
      if (selectedCalibrationId) saveSerial(model, serial);
    } catch (e) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent =
        'Lookup failed: ' + e.message + '. Use upload as a fallback.';
    } finally {
      fetchCalibrationBtn.disabled = false;
    }
  }

  async function uploadCalibration() {
    var file = calibrationFileInput.files && calibrationFileInput.files[0];
    if (!file) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent = 'Choose a calibration file first.';
      return;
    }
    uploadCalibrationBtn.disabled = true;
    calibrationStatus.className = 'mic-status';
    calibrationStatus.textContent = 'Reading calibration file…';
    try {
      var content = await file.text();
      var payload = await postJson('calibration/upload', {
        filename: file.name,
        content: content,
        model: micModelSelect.value || 'other',
        label: micModelSelect.options[micModelSelect.selectedIndex].text,
        orientation: micOrientationSelect.value || 'unknown',
        sign_convention: calibrationSignSelect.value || 'correction'
      });
      showCalibrationLoaded(payload);
    } catch (e) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent = 'Upload failed: ' + e.message;
    } finally {
      uploadCalibrationBtn.disabled = false;
    }
  }

  function selectedInputDeviceMetadata(actual) {
    var opt = inputDeviceSelect.options[inputDeviceSelect.selectedIndex];
    var requestedId = inputDeviceSelect.value || null;
    return {
      device_id: actual.deviceId || requestedId,
      requested_device_id: requestedId,
      actual_device_id: actual.deviceId || null,
      label: actual.label || (opt && opt.textContent) || null,
      browser_label: actual.label || null,
      sample_rate: actual.sampleRate,
      channel_count: actual.channelCount,
      echo_cancellation: actual.echoCancellation,
      noise_suppression: actual.noiseSuppression,
      auto_gain_control: actual.autoGainControl
    };
  }

  // This is the UX-side mirror of the authoritative server gate
  // (_BUILTIN_MIC_LABEL_RE / _calibration_device_mismatch in
  // jasper/web/correction_setup.py). Keep the two patterns in sync; the
  // backend is the one that actually blocks a wrong-mic measurement.
  function looksLikeBuiltInMic(label) {
    return /iphone|ipad|ipod|macbook|built[- ]?in|^\s*default/i.test(label || '');
  }

  function normalizeMicToken(s) {
    return (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  // The OS already names the mic — infer its calibration model from the label
  // so the user doesn't re-identify it. Registry-driven: matches the device
  // label against the aliases the server emits from calibration.SUPPORTED_MODELS
  // (the data-aliases attribute on each model option), so adding a mic to the
  // registry teaches inference automatically — no model map lives here.
  function inferCalibrationModelFromLabel(label) {
    var norm = normalizeMicToken(label);
    if (!norm) return null;
    for (var i = 0; i < micModelSelect.options.length; i++) {
      var opt = micModelSelect.options[i];
      if (!opt.value || opt.value === 'other') continue;
      var aliases = (opt.dataset.aliases || '').split(',');
      for (var j = 0; j < aliases.length; j++) {
        var token = normalizeMicToken(aliases[j]);
        if (token && norm.indexOf(token) !== -1) return opt.value;
      }
    }
    return null;
  }

  // If we can identify the calibrated mic and the user hasn't already picked a
  // model, pre-select it and reveal the serial field. Only acts when the
  // server actually offers that model, and never overrides an explicit choice.
  function maybeInferCalibrationModel(label) {
    if (micModelSelect.value) return;
    var key = inferCalibrationModelFromLabel(label);
    if (!key) return;
    var hasOption = Array.prototype.some.call(micModelSelect.options, function (o) {
      return o.value === key;
    });
    if (!hasOption) return;
    micModelSelect.value = key;
    updateMicCalibrationRows();
  }

  // A vendor measurement-mic calibration (Dayton / miniDSP) can never be the
  // phone's own mic, so capturing from a built-in mic with such a calibration
  // loaded would silently apply the curve to the wrong microphone. Returns a
  // user-facing message when that mismatch is detected, else null.
  function calibrationDeviceMismatch(capturedLabel) {
    if (!selectedCalibrationMeta) return null;
    var prov = selectedCalibrationMeta.provider || '';
    if (prov !== 'dayton_audio' && prov !== 'minidsp') return null;
    if (!looksLikeBuiltInMic(capturedLabel)) return null;
    return 'Captured device “' + (capturedLabel || 'default') + '” looks like ' +
      'this phone’s built-in mic, but you loaded a ' +
      selectedCalibrationMeta.label + ' calibration. Select the USB ' +
      'measurement mic under Input device, then Start mic capture again.';
  }

  function renderConstraints(actual, problems) {
    var mismatch = calibrationDeviceMismatch(actual.label);
    if (mismatch) problems.push(mismatch);
    var rows = [
      ['inputDevice', 'selected',
       actual.label || actual.deviceId || 'default microphone', true],
      ['sampleRate', REQUIRED_SR + ' Hz', actual.sampleRate + ' Hz',
       actual.sampleRate === REQUIRED_SR],
      ['echoCancellation', 'false',
       (actual.echoCancellation === undefined ? 'undefined (= off)' : String(actual.echoCancellation)),
       isAudioProcessingOff(actual.echoCancellation)],
      ['noiseSuppression', 'false',
       (actual.noiseSuppression === undefined ? 'undefined (= off)' : String(actual.noiseSuppression)),
       isAudioProcessingOff(actual.noiseSuppression)],
      ['autoGainControl', 'false',
       (actual.autoGainControl === undefined ? 'undefined (= off)' : String(actual.autoGainControl)),
       isAudioProcessingOff(actual.autoGainControl)],
      ['channelCount', '1', String(actual.channelCount),
       actual.channelCount === 1]
    ];
    rowsTbody.innerHTML = '';
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + escapeText(r[0]) + '</td><td>' + escapeText(r[1]) + '</td><td>' + escapeText(r[2]) + '</td>' +
        '<td class="' + (r[3] ? 'ok' : 'bad') + '">' +
          (r[3] ? '✓ ok' : '✗ bad') + '</td>';
      rowsTbody.appendChild(tr);
    });
    if (problems.length > 0) {
      errBanner.textContent =
        'Capture settings did not match what we requested: ' +
        problems.join(', ') +
        '. The measurement will refuse to start in this state.';
      errBanner.classList.remove('hidden');
      runBtn.disabled = true;
      autolevelBtn.disabled = true;
    } else {
      errBanner.classList.add('hidden');
      runBtn.disabled = false;
      autolevelBtn.disabled = false;
    }
    renderBrowserAudioLocal(actual, problems);
  }

  function renderBrowserAudioLocal(actual, problems) {
    var issues = problems.slice();
    if (actual.channelCount !== 1) {
      issues.push('channelCount is ' + actual.channelCount + ' (JTS will use the first channel)');
    }
    var level = problems.length ? 'fail' : (issues.length ? 'warn' : 'ok');
    browserAudioReport.className = 'browser-audio-card ' + level;
    browserAudioReport.innerHTML =
      '<strong>Browser audio path: ' +
      (level === 'ok' ? 'ready' : (level === 'fail' ? 'blocked' : 'usable with warnings')) +
      '</strong>' +
      (issues.length
        ? '<ul>' + issues.map(function (issue) {
            return '<li>' + escapeText(issue) + '</li>';
          }).join('') + '</ul>'
        : '<p class="hint">Input metadata looks ready for measurement. Capture quality is still checked after each sweep.</p>');
  }

  function renderBrowserAudioReport(report) {
    if (!report) return;
    var level = report.level || (report.failed ? 'fail' : 'warn');
    browserAudioReport.className = 'browser-audio-card ' + level;
    var issues = report.issues || [];
    browserAudioReport.innerHTML =
      '<strong>Browser audio path: ' +
      escapeText(level === 'ok' ? 'ready' : (level === 'fail' ? 'blocked' : 'usable with warnings')) +
      '</strong><p class="hint">' + escapeText(report.summary || '') + '</p>' +
      (issues.length
        ? '<ul>' + issues.map(function (issue) {
            return '<li>' + escapeText(issue.message || issue.code) + '</li>';
          }).join('') + '</ul>'
        : '');
  }

  async function startMicCapture() {
    if (!inputDeviceSelect.value) {
      jtsAlert('Pick a microphone first. Tap “Refresh microphones” (and ' +
        'replug the USB mic if it is not listed), then choose your ' +
        'measurement mic under Input device.');
      return;
    }
    startBtn.disabled = true;
    startBtn.textContent = 'Capturing…';
    stopMicStream();

    try {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      ctx = new Ctor({sampleRate: REQUIRED_SR});
    } catch (e) {
      jtsAlert('Could not create AudioContext: ' + e.message);
      startBtn.disabled = false;
      startBtn.textContent = 'Start mic capture';
      return;
    }

    var stream;
    var audioConstraints = {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
      sampleRate: REQUIRED_SR,
      channelCount: 1
    };
    // Always pin the exact device. Without {exact} Safari silently falls
    // back to the built-in mic, which is how an iMM-6C calibration ended up
    // applied to iPhone-mic audio.
    audioConstraints.deviceId = {exact: inputDeviceSelect.value};
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: audioConstraints,
        video: false
      });
      micStream = stream;
    } catch (e) {
      stopMicStream();
      if (e && e.name === 'OverconstrainedError') {
        jtsAlert('That microphone is no longer available (was it unplugged?). ' +
          'Tap “Refresh microphones”, reselect it, and try again.');
      } else {
        jtsAlert('Microphone permission denied or unavailable: ' + e.message);
      }
      startBtn.disabled = false;
      startBtn.textContent = 'Start mic capture';
      return;
    }

    constraintsBlock.classList.remove('hidden');
    measureSection.classList.remove('hidden');

    var settings = stream.getAudioTracks()[0].getSettings();
    var trackLabel = stream.getAudioTracks()[0].label || '';
    var actual = {
      sampleRate: settings.sampleRate || ctx.sampleRate,
      echoCancellation: settings.echoCancellation,
      noiseSuppression: settings.noiseSuppression,
      autoGainControl: settings.autoGainControl,
      channelCount: settings.channelCount || 1,
      deviceId: settings.deviceId || inputDeviceSelect.value || '',
      label: trackLabel
    };
    await populateInputDevices(actual.deviceId);
    selectedInputDevice = selectedInputDeviceMetadata(actual);
    maybeInferCalibrationModel(actual.label);
    var problems = [];
    if (actual.sampleRate !== REQUIRED_SR) problems.push('sampleRate');
    // Only TRUE counts as a problem — undefined / null mean Safari
    // didn't echo the value back, which on iOS means the feature
    // is off (it's off by default for getUserMedia on iOS anyway).
    if (actual.echoCancellation === true) problems.push('echoCancellation enabled');
    if (actual.noiseSuppression === true) problems.push('noiseSuppression enabled');
    if (actual.autoGainControl === true) problems.push('autoGainControl enabled');
    renderConstraints(actual, problems);

    var workletSrc =
      'class M extends AudioWorkletProcessor {' +
        'constructor(){super();this.r=0;this.n=0;this.cap=false;this.buf=[];' +
          'this.port.onmessage=(e)=>{' +
            'if(e.data===\'startCapture\'){this.buf=[];this.cap=true;}' +
            'else if(e.data===\'stopCapture\'){' +
              'this.cap=false;' +
              'var total=0;for(var i=0;i<this.buf.length;i++)total+=this.buf[i].length;' +
              'var out=new Float32Array(total);var pos=0;' +
              'for(var i=0;i<this.buf.length;i++){out.set(this.buf[i],pos);pos+=this.buf[i].length;}' +
              'this.port.postMessage({type:\'capture\',buffer:out.buffer},[out.buffer]);' +
              'this.buf=[];' +
            '}};}' +
        'process(inp){' +
          'var ch=inp[0]&&inp[0][0];if(!ch)return true;' +
          'var s=0;for(var i=0;i<ch.length;i++)s+=ch[i]*ch[i];' +
          'this.r+=s;this.n+=ch.length;' +
          'if(this.n>=2400){' +
            'var rms=Math.sqrt(this.r/this.n);' +
            'this.port.postMessage({type:\'rms\',value:rms});' +
            'this.r=0;this.n=0;' +
          '}' +
          'if(this.cap){' +
            'var copy=new Float32Array(ch.length);copy.set(ch);' +
            'this.buf.push(copy);' +
          '}' +
          'return true;' +
        '}}' +
      'registerProcessor("m",M);';
    var blobUrl = URL.createObjectURL(
      new Blob([workletSrc], {type: 'application/javascript'})
    );
    try {
      await ctx.audioWorklet.addModule(blobUrl);
    } catch (e) {
      jtsAlert('AudioWorklet load failed: ' + e.message);
      return;
    }
    var src = ctx.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(ctx, 'm');
    workletNode.port.onmessage = function (ev) {
      if (ev.data && ev.data.type === 'rms') {
        var rms = ev.data.value;
        var db = rms > 0 ? 20 * Math.log10(rms) : -120;
        // Live meter
        var pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
        levelBar.style.width = pct.toFixed(1) + '%';
        levelReadout.textContent = db.toFixed(1);
        // Stash for the autolevel loop (which polls this).
        latestMicRmsDb = db;
      } else if (ev.data && ev.data.type === 'capture') {
        onCaptureReady(ev.data.buffer, captureMode);
      }
    };
    src.connect(workletNode);
    // No mic→destination connection — would create a feedback loop
    // on the smart speaker that's the listening target.

    startBtn.textContent = 'Capturing (mic level live below)';

    await acquireWakeLock();
  }

  // iOS auto-releases the screen wake lock when the tab is backgrounded, which
  // stalls the poll loop and strands the sweep waiting for a capture upload.
  // Store the sentinel and re-acquire on visibilitychange so a glance away
  // mid-measurement doesn't wedge the session.
  async function acquireWakeLock() {
    try {
      if ('wakeLock' in navigator) {
        wakeLockSentinel = await navigator.wakeLock.request('screen');
        wakeLockSentinel.addEventListener('release', function () {
          wakeLockSentinel = null;
        });
      }
    } catch (e) {
      console.warn('wakeLock not granted', e);
    }
  }

  function setStateBadge(state, detail) {
    stateBadge.className = 'state-badge ' + state;
    stateBadge.textContent = state;
    stateDetail.textContent = detail || '';
  }

  function qualityReports(payload) {
    var reports = [];
    if (payload && Array.isArray(payload.capture_quality)) {
      reports = reports.concat(payload.capture_quality);
    }
    if (payload && payload.verify_quality) {
      reports.push(payload.verify_quality);
    }
    return reports;
  }

  function renderQuality(payload) {
    var seen = {};
    var issues = [];
    qualityReports(payload).forEach(function (report) {
      (report && report.issues || []).forEach(function (issue) {
        var key = [issue.severity, issue.code, issue.message].join('|');
        if (!seen[key]) {
          seen[key] = true;
          issues.push(issue);
        }
      });
    });
    if (!issues.length) {
      qualityBanner.className = 'quality-banner hidden';
      qualityBanner.innerHTML = '';
      return;
    }
    var hasFail = issues.some(function (issue) {
      return issue.severity === 'fail';
    });
    qualityBanner.className = 'quality-banner ' + (hasFail ? 'fail' : 'warn');
    qualityBanner.innerHTML =
      '<strong>' + (hasFail ? 'Measurement blocked:' : 'Measurement quality warnings:') +
      '</strong><ul>' +
      issues.map(function (issue) {
        return '<li>' + escapeText(issue.message || issue.code) + '</li>';
      }).join('') +
      '</ul>';
  }

  function formatAppliedAt(epoch) {
    if (!epoch) return '';
    var d = new Date(epoch * 1000);
    if (isNaN(d.getTime())) return '';
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var pad = function (n) { return n < 10 ? '0' + n : String(n); };
    return days[d.getDay()] + ' ' + d.getFullYear() + '-' +
      pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
      pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  // Map the backend config `kind` to a banner CSS class. The class set
  // (applied/custom/flat) is presentation and stays here; the human copy is
  // OWNED by the backend (correction.status.describe_current_config -> {label,
  // message}) and rendered verbatim so the two surfaces cannot drift (C4a-2).
  function correctionBannerClass(kind) {
    if (kind === 'custom' || kind === 'unknown') return 'custom';
    return 'flat';
  }

  function renderCurrentCorrection(cc, config) {
    // `cc` is the parsed JTS room-correction descriptor. When a correction is
    // applied JTS formats the live PEQ count + timestamp client-side (dynamic
    // data, not copy). Otherwise the backend `config` descriptor owns the
    // label/message; the browser renders it rather than re-deriving per-kind
    // strings that have drifted from the backend.
    if (cc && cc.applied_at_epoch) {
      currentCorrectionBanner.className = 'applied';
      var when = formatAppliedAt(cc.applied_at_epoch);
      var count = cc.peq_count || 0;
      var noun = count === 1 ? 'filter' : 'filters';
      currentCorrectionLabel.textContent =
        'Current correction: ' + count + ' PEQ ' + noun +
        (when ? ' applied ' + when : '');
      currentCorrectionResetBtn.classList.remove('hidden');
      return;
    }
    var kind = config && config.kind || '';
    currentCorrectionBanner.className = correctionBannerClass(kind);
    currentCorrectionLabel.textContent =
      (config && (config.message || config.label)) ||
      'No correction applied — speaker is flat.';
    // A non-JTS/advanced config offers a reset to the flat baseline; managed
    // (flat/preference/active-speaker/measurement) states have nothing to reset.
    if (kind === 'custom') {
      currentCorrectionResetBtn.classList.remove('hidden');
    } else {
      currentCorrectionResetBtn.classList.add('hidden');
    }
  }

  async function refreshCurrentCorrection() {
    try {
      var s = await fetchStatus();
      renderCurrentCorrection(s.current_correction, s.current_config);
    } catch (e) {
      currentCorrectionBanner.className = 'flat';
      currentCorrectionLabel.textContent =
        'Could not read current correction: ' + e.message;
      currentCorrectionResetBtn.classList.add('hidden');
    }
  }

  async function resetFromBanner() {
    currentCorrectionResetBtn.disabled = true;
    currentCorrectionLabel.textContent = 'Resetting correction…';
    try {
      await postJson('reset', {});
    } catch (e) {
      currentCorrectionLabel.textContent =
        'Reset failed: ' + e.message;
      currentCorrectionResetBtn.disabled = false;
      return;
    }
    await refreshCurrentCorrection();
    currentCorrectionResetBtn.disabled = false;
  }

  function renderPEQs(peqs) {
    if (!peqs || peqs.length === 0) {
      peqList.innerHTML = '<p class="hint">No filters needed — your room\'s bass is already flat (or close enough). Nothing to apply.</p>';
      return;
    }
    var rows = peqs.map(function (p, i) {
      return '<tr><td>peq_' + (i+1) + '</td>' +
             '<td>' + p.freq_hz.toFixed(1) + ' Hz</td>' +
             '<td>Q ' + p.q.toFixed(2) + '</td>' +
             '<td>' + p.gain_db.toFixed(2) + ' dB</td></tr>';
    }).join('');
    peqList.innerHTML =
      '<table><thead><tr><th>Filter</th><th>Freq</th><th>Q</th><th>Gain</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  function renderDesignReport(report) {
    if (!report || !report.correction_strategy) {
      designReport.classList.add('hidden');
      designReport.innerHTML = '';
      return;
    }
    var before = report.before || {};
    var after = report.after || {};
    var improvement = report.improvement || {};
    var warnings = report.warnings || [];
    var filterAudits = report.filters || [];
    var warningHtml = '';
    if (warnings.length) {
      warningHtml = '<ul>' + warnings.map(function (w) {
        return '<li>' + escapeText(w.message || w.code) + '</li>';
      }).join('') + '</ul>';
    }
    var filterHtml = '';
    if (filterAudits.length) {
      filterHtml = '<ul>' + filterAudits.map(function (f) {
        return '<li>' + escapeText(f.rationale || (
          'Filter near ' + Math.round(f.freq_hz) + ' Hz'
        )) + '</li>';
      }).join('') + '</ul>';
    }
    designReport.classList.remove('hidden');
    designReport.innerHTML =
      '<h3>Design audit</h3>' +
      '<p class="hint">' +
      'Strategy: <strong>' + escapeText(report.correction_strategy.label) +
      '</strong> · Target: <strong>' + escapeText(report.target_profile.label) +
      '</strong> · Band: ' + Math.round(report.band_hz[0]) + '-' +
      Math.round(report.band_hz[1]) + ' Hz.</p>' +
      '<p class="hint">Predicted modal-band RMS error: ' +
      (before.rms_db || 0).toFixed(1) + ' dB -> ' +
      (after.rms_db || 0).toFixed(1) + ' dB' +
      ' (' + (improvement.rms_db || 0).toFixed(1) +
      ' dB improvement).</p>' +
      warningHtml + filterHtml;
  }

  function renderConfidence(payload) {
    var report = payload && (
      payload.confidence_report ||
      (payload.design_report && payload.design_report.confidence_report)
    );
    if (!report) {
      confidencePanel.className = 'confidence-card hidden';
      confidencePanel.innerHTML = '';
      return;
    }

    var level = report.level || 'low';
    var score = typeof report.score === 'number' ? report.score : 0;
    var variance = report.position_variance || {};
    var gates = report.strategy_gates || {};
    var findings = (report.findings || []).slice(0, 5);
    var gateHtml = ['safe', 'balanced', 'assertive'].map(function (name) {
      var gate = gates[name] || {};
      var allowed = !!gate.allowed;
      return '<span class="gate ' + (allowed ? 'allowed' : 'blocked') + '">' +
        escapeText(name) + ': ' + (allowed ? 'allowed' : 'blocked') +
        '</span>';
    }).join('');

    var varianceHtml = '';
    if (variance.available) {
      varianceHtml =
        '<p class="hint">Position variance: ' +
        'p90 std ' + Number(variance.p90_std_db || 0).toFixed(1) + ' dB, ' +
        'max range ' + Number(variance.max_range_db || 0).toFixed(1) +
        ' dB across ' + Number(variance.position_count || 0) +
        ' positions.</p>';
    } else {
      varianceHtml =
        '<p class="hint">Position variance unavailable: ' +
        escapeText(variance.reason || 'need more completed positions') +
        '.</p>';
    }

    var findingsHtml = '';
    if (findings.length) {
      findingsHtml = '<ul>' + findings.map(function (finding) {
        return '<li>' + escapeText(finding.message || finding.code) + '</li>';
      }).join('') + '</ul>';
    }

    confidencePanel.className = 'confidence-card ' + level;
    confidencePanel.innerHTML =
      '<h3>Measurement confidence</h3>' +
      '<p><strong>' + escapeText(level.toUpperCase()) + '</strong> · ' +
      '<span class="confidence-score">' + score + '/100</span></p>' +
      '<p class="hint">' + escapeText(report.summary || '') + '</p>' +
      varianceHtml +
      '<div class="gate-list">' + gateHtml + '</div>' +
      findingsHtml;
  }

  function renderRuntimeIntegrity(payload) {
    var report = payload && (
      payload.runtime_integrity ||
      (payload.confidence_report && payload.confidence_report.runtime_integrity)
    );
    if (!report || (!report.snapshot_count && !report.capture_count)) {
      runtimeIntegrityPanel.className = 'runtime-card hidden';
      runtimeIntegrityPanel.innerHTML = '';
      return;
    }
    var level = report.level || 'ok';
    var latest = report.latest_snapshot || {};
    var memory = latest.memory || {};
    var load = latest.load_per_core;
    var issues = (report.issues || []).slice(0, 5);
    var issueHtml = issues.length
      ? '<ul>' + issues.map(function (issue) {
          return '<li>' + escapeText(issue.message || issue.code) + '</li>';
        }).join('') + '</ul>'
      : '<p class="hint">No runtime warnings were recorded around the sweep.</p>';
    var loadText = Number.isFinite(Number(load))
      ? Number(load).toFixed(2) + ' load/core'
      : 'load unavailable';
    var memText = Number.isFinite(Number(memory.available_mb))
      ? Number(memory.available_mb).toFixed(0) + ' MB free'
      : 'memory unavailable';
    runtimeIntegrityPanel.className = 'runtime-card ' + level;
    runtimeIntegrityPanel.innerHTML =
      '<h3>Runtime integrity</h3>' +
      '<p><strong>' + escapeText(level.toUpperCase()) + '</strong> · ' +
      Number(report.capture_count || 0) + ' capture artifact(s), ' +
      Number(report.snapshot_count || 0) + ' system snapshot(s).</p>' +
      '<p class="hint">' + escapeText(loadText) + ' · ' +
      escapeText(memText) + '</p>' +
      issueHtml;
  }

  function numberOrNull(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function formatHz(value) {
    var n = numberOrNull(value);
    if (n === null) return '—';
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + ' kHz';
    return n.toFixed(n >= 100 ? 0 : 1) + ' Hz';
  }

  function formatDb(value) {
    var n = numberOrNull(value);
    if (n === null) return '—';
    return (n > 0 ? '+' : '') + n.toFixed(1) + ' dB';
  }

  function formatMaybeDb(value) {
    var n = numberOrNull(value);
    return n === null ? '—' : n.toFixed(1) + ' dB';
  }

  function formatBytes(bytes) {
    var n = Number(bytes || 0);
    if (!isFinite(n) || n <= 0) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var idx = 0;
    while (n >= 1024 && idx < units.length - 1) {
      n = n / 1024;
      idx += 1;
    }
    return (idx === 0 ? String(Math.round(n)) : n.toFixed(1)) + ' ' + units[idx];
  }

  function reportIssueList(items, fallback) {
    items = (items || []).filter(function (item) { return !!item; }).slice(0, 8);
    if (!items.length) return '<p class="hint">' + escapeText(fallback) + '</p>';
    return '<ul>' + items.map(function (item) {
      return '<li>' + escapeText(
        item.message || item.reason || item.code || item.kind || String(item)
      ) + '</li>';
    }).join('') + '</ul>';
  }

  async function loadSessionReports() {
    loadSessionsBtn.disabled = true;
    sessionHistory.textContent = 'Loading recent sessions…';
    try {
      var resp = await fetch(endpoint('sessions'), {cache: 'no-store'});
      if (!resp.ok) throw new Error('sessions ' + resp.status);
      var payload = await resp.json();
      renderSessionHistory(payload.sessions || []);
    } catch (e) {
      sessionHistory.textContent = 'Could not load measurement reports: ' + e.message;
    } finally {
      loadSessionsBtn.disabled = false;
    }
  }

  function renderSessionHistory(sessions) {
    sessionHistory.innerHTML = '';
    if (!sessions.length) {
      var empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = 'No completed measurement bundles found yet.';
      sessionHistory.appendChild(empty);
      return;
    }
    sessions.forEach(function (session) {
      var item = document.createElement('div');
      item.className = 'session-item';
      var title = document.createElement('strong');
      title.textContent = 'Session ' + (session.session_id || 'unknown');
      var meta = document.createElement('p');
      meta.className = 'hint';
      var state = session.state || 'unknown';
      var positions = Number(session.current_position || 0) + '/' +
        Number(session.total_positions || 0);
      var started = formatAppliedAt(session.started_at);
      meta.textContent = state + ' · positions ' + positions +
        (started ? ' · ' + started : '') +
        (session.has_result ? ' · result saved' : ' · no result yet') +
        ' · ' + formatBytes(session.bundle_size_bytes);
      var privacy = document.createElement('p');
      privacy.className = 'hint';
      var rawCount = Number(session.private_raw_audio_count || 0);
      if (rawCount > 0) {
        var badge = document.createElement('span');
        badge.className = 'privacy-badge';
        badge.textContent = 'Private raw recordings';
        privacy.appendChild(badge);
        privacy.appendChild(document.createTextNode(
          ' ' + rawCount + ' file' + (rawCount === 1 ? '' : 's') +
          ' · ' + formatBytes(session.private_raw_audio_bytes)
        ));
      } else {
        privacy.textContent = 'No raw recording files in this bundle.';
      }
      var actions = document.createElement('div');
      actions.className = 'session-actions';
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = 'View report';
      button.dataset.sessionId = session.session_id || '';
      var deleteButton = document.createElement('button');
      deleteButton.type = 'button';
      deleteButton.className = 'danger';
      deleteButton.textContent = 'Delete';
      deleteButton.dataset.deleteSessionId = session.session_id || '';
      actions.appendChild(button);
      actions.appendChild(deleteButton);
      item.appendChild(title);
      item.appendChild(meta);
      item.appendChild(privacy);
      item.appendChild(actions);
      sessionHistory.appendChild(item);
    });
  }

  async function deleteSessionBundle(sessionId) {
    if (!sessionId) return;
    var ok = await jtsConfirm(
      'Delete this measurement bundle from the speaker? Raw recordings and derived evidence for this session will be removed.',
      {danger: true}
    );
    if (!ok) return;
    try {
      await postJson('session/delete', {id: sessionId});
      if (sessionReport.dataset.sessionId === sessionId) {
        sessionReport.className = 'session-report hidden';
        sessionReport.textContent = '';
        delete sessionReport.dataset.sessionId;
      }
      await loadSessionReports();
    } catch (e) {
      sessionReport.className = 'session-report blocked';
      sessionReport.textContent = 'Could not delete bundle: ' + e.message;
    }
  }

  async function loadSessionReport(sessionId) {
    if (!sessionId) return;
    sessionReport.className = 'session-report';
    sessionReport.textContent = 'Loading report…';
    try {
      var resp = await fetch(
        endpoint('session-report') + '?id=' + encodeURIComponent(sessionId),
        {cache: 'no-store'}
      );
      var text = await resp.text();
      var payload;
      try {
        payload = JSON.parse(text);
      } catch (_e) {
        payload = {error: text};
      }
      if (!resp.ok) {
        throw new Error(payload.error || ('session-report ' + resp.status));
      }
      sessionReport.dataset.sessionId = sessionId;
      renderSessionReport(payload);
    } catch (e) {
      sessionReport.className = 'session-report blocked';
      sessionReport.textContent = 'Could not load report: ' + e.message;
    }
  }

  function renderSessionReport(payload) {
    var evidence = payload.evidence || {};
    var readiness = evidence.agent_readiness || {};
    var bundle = evidence.bundle || {};
    var measurement = evidence.measurement || {};
    var confidence = evidence.confidence || {};
    var acoustic = (evidence.acoustic_quality || {}).summary || {};
    var runtime = (evidence.runtime_integrity || {}).summary || {};
    var position = evidence.position_analysis || {};
    var repeatability = evidence.repeatability || {};
    var versions = payload.artifact_versions || {};
    var readinessLevel = readiness.level || 'caution';
    var suspicious = []
      .concat(bundle.issues || [])
      .concat(((evidence.runtime_integrity || {}).issues || []))
      .concat(((evidence.acoustic_quality || {}).issues || []))
      .concat(repeatability.issues || [])
      .concat(position.feature_flags || []);
    var trusted = [];
    if (bundle.has_result) trusted.push({message: 'Analysis result is present.'});
    if (bundle.has_artifact_manifest) trusted.push({message: 'Artifact manifest is present.'});
    if (acoustic.snr_level && acoustic.snr_level !== 'unavailable') {
      trusted.push({message: 'SNR evidence is ' + acoustic.snr_level + '.'});
    }
    if (runtime.level === 'ok') trusted.push({message: 'Runtime integrity is OK.'});
    if (repeatability.available) {
      trusted.push({message: 'Same-seat repeatability is ' + repeatability.level + '.'});
    }
    var gates = confidence.strategy_gates || {};
    var refused = ['safe', 'balanced', 'assertive'].filter(function (name) {
      return gates[name] && gates[name].allowed === false;
    }).map(function (name) {
      var reason = (gates[name].reasons || [])[0] || 'strategy gate blocked';
      return {message: name + ' correction blocked: ' + reason};
    });
    sessionReport.className = 'session-report ' + readinessLevel;
    sessionReport.innerHTML =
      '<h3>Measurement report · ' + escapeText(evidence.session_id || payload.session_id || 'unknown') + '</h3>' +
      '<p class="hint"><strong>Recommended next action:</strong> ' +
      escapeText(readiness.recommended_action || 'review evidence before applying stronger correction') + '</p>' +
      '<div class="metric-grid">' +
        '<div class="metric"><span class="label">Readiness</span><span class="value">' +
        escapeText(readinessLevel) + '</span></div>' +
        '<div class="metric"><span class="label">Confidence</span><span class="value">' +
        escapeText(confidence.level || '—') + ' · ' + Number(confidence.score || 0).toFixed(0) + '/100</span></div>' +
        '<div class="metric"><span class="label">SNR</span><span class="value">' +
        escapeText(acoustic.snr_level || '—') + ' · ' + formatMaybeDb(acoustic.min_estimated_snr_db) + '</span></div>' +
        '<div class="metric"><span class="label">Runtime</span><span class="value">' +
        escapeText(runtime.level || 'unknown') + '</span></div>' +
        '<div class="metric"><span class="label">Positions</span><span class="value">' +
        Number(position.position_count || measurement.positions_completed || 0) + '</span></div>' +
        '<div class="metric"><span class="label">Repeatability</span><span class="value">' +
        escapeText(repeatability.level || 'unavailable') + '</span></div>' +
      '</div>' +
      '<h4>What happened</h4>' +
      '<p class="hint">State ' + escapeText(bundle.state || 'unknown') +
      ' · target ' + escapeText(measurement.target_choice || 'unknown') +
      ' · strategy ' + escapeText(measurement.strategy_choice || 'unknown') +
      ' · bundle schema v' + escapeText(bundle.schema_version || 'unknown') + '.</p>' +
      '<h4>What looks trustworthy</h4>' +
      reportIssueList(trusted, 'No positive evidence was available yet.') +
      '<h4>What looks suspicious or missing</h4>' +
      reportIssueList(suspicious.concat((readiness.reasons || []).map(function (reason) {
        return {message: reason};
      })), 'No warnings were recorded in the read-only evidence packet.') +
      '<h4>What JTS refused to correct</h4>' +
      reportIssueList(refused, 'No strategy gate refusal was recorded.') +
      '<h4>Artifact versions</h4>' +
      '<p class="hint">bundle v' + escapeText(versions.bundle_schema_version || bundle.schema_version || 'unknown') +
      ' · manifest v' + escapeText(versions.artifact_manifest_schema_version || 'missing') +
      ' · result v' + escapeText(versions.result_json_schema_version || 'missing') +
      ' · runtime v' + escapeText(versions.runtime_integrity_schema_version || 'missing') +
      ' · acoustic v' + escapeText(versions.acoustic_quality_schema_version || 'missing') +
      ' · evidence packet v' + escapeText(versions.evidence_packet_schema_version || evidence.artifact_schema_version || 'unknown') +
      '.</p>';
  }

  function chartPayload(payload) {
    payload = payload || lastResult || {};
    return {
      confidence: payload.confidence_report ||
        (payload.design_report && payload.design_report.confidence_report) ||
        null,
      design: payload.design_report || null,
      position: payload.position_analysis ||
        (payload.design_report && payload.design_report.position_report) ||
        null,
      runtime: payload.runtime_integrity ||
        (payload.confidence_report && payload.confidence_report.runtime_integrity) ||
        null,
      peqs: payload.peqs || []
    };
  }

  function recommendedNextAction(payload) {
    var p = chartPayload(payload);
    var confidence = p.confidence || {};
    var level = confidence.level || 'low';
    var failed = (confidence.findings || []).some(function (finding) {
      return finding.severity === 'fail';
    });
    if (p.runtime && p.runtime.level === 'fail') {
      return 'Remeasure — runtime evidence says this sweep may be corrupted.';
    }
    if (p.runtime && p.runtime.level === 'warn' && level !== 'high') {
      return 'Review runtime warnings, then remeasure if the curve looks surprising.';
    }
    if (failed || level === 'low') {
      return 'Remeasure before trusting aggressive correction.';
    }
    if (!p.peqs.length) {
      return 'No correction is needed from this measurement.';
    }
    if (level === 'medium') {
      return 'Apply only a conservative strategy, then verify.';
    }
    return 'Apply the proposed correction, then verify from the main seat.';
  }

  function renderResultsSummary(payload) {
    if (!payload || !payload.measured) {
      resultsSummary.className = 'results-summary hidden';
      resultsSummary.innerHTML = '';
      return;
    }
    var p = chartPayload(payload);
    var confidence = p.confidence || {};
    var design = p.design || {};
    var improvement = design.improvement || {};
    var strategy = design.correction_strategy || {};
    var position = p.position || {};
    var bands = (position.bands || []).filter(function (band) {
      return band.available;
    }).slice(0, 5);
    var flags = position.feature_flags || [];
    var flagText = flags.length
      ? flags.slice(0, 3).map(function (flag) {
          return escapeText(flag.reason || flag.kind);
        }).join('<br>')
      : 'No rejected high-risk features were flagged.';
    var bandRows = bands.map(function (band) {
      var confidenceLevel = band.confidence_level || 'low';
      var residual = band.residual || {};
      return '<tr><td data-label="Band">' +
        escapeText(band.label || band.band_id) + '</td>' +
        '<td data-label="Range">' + formatHz((band.band_hz || [])[0]) + '-' +
        formatHz((band.band_hz || [])[1]) + '</td>' +
        '<td data-label="Confidence"><span class="band-pill ' + escapeText(confidenceLevel) + '">' +
        escapeText(confidenceLevel) + '</span></td>' +
        '<td data-label="Spread">' + formatDb(band.p90_std_db) + '</td>' +
        '<td data-label="RMS error">' + formatDb(residual.rms_db) + '</td></tr>';
    }).join('');

    resultsSummary.className = 'results-summary';
    resultsSummary.innerHTML =
      '<h3>Correction readout</h3>' +
      '<p class="hint"><strong>Recommended next action:</strong> ' +
      escapeText(recommendedNextAction(payload)) + '</p>' +
      '<div class="metric-grid">' +
        '<div class="metric"><span class="label">Confidence</span>' +
        '<span class="value">' + escapeText(confidence.level || '—') +
        ' · ' + Number(confidence.score || 0).toFixed(0) + '/100</span></div>' +
        '<div class="metric"><span class="label">Positions</span>' +
        '<span class="value">' + Number(position.position_count || 0) +
        '</span></div>' +
        '<div class="metric"><span class="label">Strategy</span>' +
        '<span class="value">' + escapeText(strategy.label || strategy.strategy_id || '—') +
        '</span></div>' +
        '<div class="metric"><span class="label">Filters</span>' +
        '<span class="value">' + Number(p.peqs.length || 0) + '</span></div>' +
        '<div class="metric"><span class="label">Runtime</span>' +
        '<span class="value">' + escapeText((p.runtime && p.runtime.level) || '—') +
        '</span></div>' +
        '<div class="metric"><span class="label">Predicted RMS change</span>' +
        '<span class="value">' + formatDb(improvement.rms_db) + '</span></div>' +
      '</div>' +
      (bandRows
        ? '<table class="band-table"><thead><tr><th>Band</th><th>Range</th>' +
          '<th>Confidence</th><th>Spread</th><th>RMS error</th></tr></thead>' +
          '<tbody>' + bandRows + '</tbody></table>'
        : '<p class="hint">Band confidence is unavailable for this run.</p>') +
      '<p class="hint"><strong>Rejected / caution areas:</strong><br>' +
      flagText + '</p>';
  }

  function smoothingWidthOctaves() {
    var mode = chartSmoothing ? chartSmoothing.value : 'none';
    if (mode === '1/3') return 1 / 3;
    if (mode === '1/6') return 1 / 6;
    if (mode === '1/12') return 1 / 12;
    return 0;
  }

  function smoothValues(freqs, values) {
    var width = smoothingWidthOctaves();
    if (!width || !freqs || !values || freqs.length !== values.length) {
      return values ? values.slice() : [];
    }
    var half = width / 2;
    return values.map(function (_value, i) {
      var f0 = Number(freqs[i]);
      if (!Number.isFinite(f0) || f0 <= 0) return Number(values[i] || 0);
      var sum = 0;
      var count = 0;
      for (var j = 0; j < values.length; j++) {
        var f = Number(freqs[j]);
        var v = Number(values[j]);
        if (!Number.isFinite(f) || f <= 0 || !Number.isFinite(v)) continue;
        if (Math.abs(Math.log2(f / f0)) <= half) {
          sum += v;
          count += 1;
        }
      }
      return count ? sum / count : Number(values[i] || 0);
    });
  }

  function smoothCurve(curve) {
    if (!curve || !curve.freqs_hz || !curve.magnitude_db) return curve;
    return {
      freqs_hz: curve.freqs_hz,
      magnitude_db: smoothValues(curve.freqs_hz, curve.magnitude_db)
    };
  }

  function filterEffectCurve(measured, predicted) {
    if (
      !measured || !predicted ||
      !measured.freqs_hz || !measured.magnitude_db ||
      !predicted.magnitude_db ||
      measured.magnitude_db.length !== predicted.magnitude_db.length
    ) {
      return null;
    }
    return {
      freqs_hz: measured.freqs_hz,
      magnitude_db: measured.magnitude_db.map(function (value, idx) {
        return Number(predicted.magnitude_db[idx] || 0) - Number(value || 0);
      })
    };
  }

  function drawChart(measured, target, predicted, payload) {
    var dpr = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    // Defensive: a hidden canvas (display:none ancestor) reports
    // 0×0. Drawing into it silently produces an empty chart. Bail
    // and log — caller is responsible for re-invoking after the
    // canvas becomes visible.
    if (rect.width < 10 || rect.height < 10) {
      console.warn('drawChart skipped — canvas not laid out yet ' +
        '(' + rect.width + '×' + rect.height + ')');
      return;
    }
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    var c = canvas.getContext('2d');
    c.scale(dpr, dpr);
    c.clearRect(0, 0, rect.width, rect.height);

    // Margins
    var ml = 40, mr = 10, mt = 10, mb = 22;
    var W = rect.width - ml - mr;
    var H = rect.height - mt - mb;

    var fMin = 20, fMax = 20000;
    var dbMin = -20, dbMax = 20;

    function fx(f) { return ml + W * (Math.log2(f / fMin) / Math.log2(fMax / fMin)); }
    function fy(db) { return mt + H * (1 - (db - dbMin) / (dbMax - dbMin)); }
    var displayMeasured = smoothCurve(measured);
    var displayTarget = smoothCurve(target);
    var displayPredicted = smoothCurve(predicted);
    var p = chartPayload(payload);
    var band = p.design && p.design.band_hz;

    if (chartShowBand && chartShowBand.checked && band && band.length === 2) {
      c.fillStyle = 'rgba(29, 185, 84, 0.08)';
      var x0 = fx(Math.max(fMin, Number(band[0])));
      var x1 = fx(Math.min(fMax, Number(band[1])));
      c.fillRect(x0, mt, Math.max(0, x1 - x0), H);
    }

    // Grid
    c.strokeStyle = '#e6e6e6'; c.fillStyle = '#888';
    c.font = '11px sans-serif'; c.lineWidth = 1;
    [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach(function (f) {
      var x = fx(f);
      c.beginPath(); c.moveTo(x, mt); c.lineTo(x, mt + H); c.stroke();
      var label = f >= 1000 ? (f / 1000) + 'k' : '' + f;
      c.fillText(label, x - 8, mt + H + 14);
    });
    [-20, -10, 0, 10, 20].forEach(function (db) {
      var y = fy(db);
      c.beginPath(); c.moveTo(ml, y); c.lineTo(ml + W, y); c.stroke();
      c.fillText(db + ' dB', 2, y + 3);
    });
    // 0 dB emphasis
    c.strokeStyle = '#bbb';
    c.beginPath(); c.moveTo(ml, fy(0)); c.lineTo(ml + W, fy(0)); c.stroke();

    function drawSpread(chart) {
      if (
        !chart || !chart.freqs_hz || !chart.min_db || !chart.max_db ||
        chart.freqs_hz.length !== chart.min_db.length ||
        chart.freqs_hz.length !== chart.max_db.length
      ) return;
      var minDb = smoothValues(chart.freqs_hz, chart.min_db);
      var maxDb = smoothValues(chart.freqs_hz, chart.max_db);
      c.fillStyle = 'rgba(212, 68, 68, 0.14)';
      c.beginPath();
      var first = true;
      for (var i = 0; i < chart.freqs_hz.length; i++) {
        var x = fx(chart.freqs_hz[i]);
        var y = fy(maxDb[i]);
        if (first) { c.moveTo(x, y); first = false; }
        else c.lineTo(x, y);
      }
      for (var j = chart.freqs_hz.length - 1; j >= 0; j--) {
        c.lineTo(fx(chart.freqs_hz[j]), fy(minDb[j]));
      }
      c.closePath();
      c.fill();
    }

    function drawCurve(curve, color, dashed, width) {
      if (!curve || !curve.freqs_hz) return;
      c.strokeStyle = color;
      c.lineWidth = width || 2;
      if (dashed) c.setLineDash([4, 4]); else c.setLineDash([]);
      c.beginPath();
      var first = true;
      for (var i = 0; i < curve.freqs_hz.length; i++) {
        var x = fx(curve.freqs_hz[i]);
        var y = fy(curve.magnitude_db[i]);
        if (first) { c.moveTo(x, y); first = false; }
        else c.lineTo(x, y);
      }
      c.stroke();
      c.setLineDash([]);
    }

    if (
      chartShowSpread && chartShowSpread.checked &&
      p.position && p.position.chart
    ) {
      drawSpread(p.position.chart);
    }

    drawCurve(displayTarget, '#888', true, 2);
    drawCurve(displayMeasured, '#d44', false, 2);
    drawCurve(displayPredicted, '#1db954', false, 2);
    if (chartShowFilter && chartShowFilter.checked) {
      drawCurve(
        filterEffectCurve(displayMeasured, displayPredicted),
        '#2b7bb9',
        true,
        1.6,
      );
    }
    // Phase 2: post-correction verify pass overlay (purple dashed).
    if (lastVerify) {
      drawCurve(smoothCurve(lastVerify), '#a050d0', true, 2);
    }

    (p.peqs || []).forEach(function (peq, idx) {
      var freq = Number(peq.freq_hz);
      if (!Number.isFinite(freq) || freq < fMin || freq > fMax) return;
      var x = fx(freq);
      c.strokeStyle = 'rgba(43, 123, 185, 0.45)';
      c.lineWidth = 1;
      c.beginPath(); c.moveTo(x, mt); c.lineTo(x, mt + H); c.stroke();
      c.fillStyle = '#2b7bb9';
      c.fillText(String(idx + 1), x - 3, mt + 11);
    });

    var flags = (p.position && p.position.feature_flags) || [];
    flags.slice(0, 6).forEach(function (flag) {
      var freq = Number(flag.freq_hz || flag.worst_freq_hz);
      if (!Number.isFinite(freq) || freq < fMin || freq > fMax) return;
      var x = fx(freq);
      c.strokeStyle = 'rgba(214, 130, 0, 0.55)';
      c.setLineDash([2, 3]);
      c.beginPath(); c.moveTo(x, mt); c.lineTo(x, mt + H); c.stroke();
      c.setLineDash([]);
    });
  }

  function redrawLatestChart() {
    if (lastResult && lastResult.measured) {
      drawChart(
        lastResult.measured,
        lastResult.target,
        lastResult.predicted,
        lastResult,
      );
    }
  }

  // -- Network --


  async function postJson(path, body) {
    var url = endpoint(path);
    var resp = await fetch(url, {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(body || {})
    });
    if (!resp.ok) {
      var msg = await resp.text();
      try {
        var payload = JSON.parse(msg);
        if (payload && payload.error) msg = payload.error;
      } catch (_e) {}
      throw new Error('POST ' + url + ' → ' + resp.status + ': ' + msg);
    }
    return await resp.json();
  }

  async function fetchStatus() {
    var resp = await fetch(endpoint('status'), {cache: 'no-store'});
    if (!resp.ok) throw new Error('status ' + resp.status);
    return await resp.json();
  }

  // -- WAV encoding --

  function float32ToWav(samples, sampleRate) {
    var len = samples.length;
    var buf = new ArrayBuffer(44 + len * 2);
    var view = new DataView(buf);
    function w8s(off, str) {
      for (var i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i));
    }
    w8s(0, 'RIFF');
    view.setUint32(4, 36 + len * 2, true);
    w8s(8, 'WAVE');
    w8s(12, 'fmt ');
    view.setUint32(16, 16, true);          // fmt chunk size
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, 1, true);           // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);  // byte rate (mono * 2 bytes)
    view.setUint16(32, 2, true);           // block align
    view.setUint16(34, 16, true);          // 16-bit
    w8s(36, 'data');
    view.setUint32(40, len * 2, true);
    var off = 44;
    for (var i = 0; i < len; i++) {
      var s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(off, s * 0x7FFF, true);
      off += 2;
    }
    return new Blob([buf], {type: 'audio/wav'});
  }

  // -- Workflow --

  function capturePreSweepNoise() {
    if (!workletNode) return;
    captureMode = 'noise';
    setStateBadge('needs_noise_capture', 'recording room noise…');
    workletNode.port.postMessage('startCapture');
    setTimeout(function () {
      if (captureMode === 'noise' && workletNode) {
        workletNode.port.postMessage('stopCapture');
      }
    }, 700);
  }

  function resetMeasurementUiForStart() {
    continueBtn.classList.add('hidden');
    applyBtn.classList.add('hidden');
    verifyBtn.classList.add('hidden');
    resetBtn.classList.add('hidden');
    resultSection.classList.add('hidden');
    positionPrompt.classList.add('hidden');
    verifySummary.classList.add('hidden');
    resultsSummary.className = 'results-summary hidden';
    resultsSummary.innerHTML = '';
    designReport.classList.add('hidden');
    designReport.innerHTML = '';
    confidencePanel.className = 'confidence-card hidden';
    confidencePanel.innerHTML = '';
    runtimeIntegrityPanel.className = 'runtime-card hidden';
    runtimeIntegrityPanel.innerHTML = '';
    qualityBanner.className = 'quality-banner hidden';
    qualityBanner.innerHTML = '';
    lastVerify = null;
    inVerifyMode = false;
    setStateBadge('preparing', 'pausing music…');
  }

  function measurementStartPayload() {
    var totalPositions = parseInt(positionsSelect.value, 10) || 1;
    var targetChoice = targetSelect.value || 'flat';
    var strategyChoice = strategySelect.value || 'balanced';
    return {
      total_positions: totalPositions,
      target_choice: targetChoice,
      strategy_choice: strategyChoice,
      noise_floor_db: relayMode ? null : lastNoiseFloorDb,
      calibration_id: selectedCalibrationId,
      input_device: relayMode ? null : selectedInputDevice,
      repeat_main_position: relayMode
        ? false
        : !!(repeatMainPosition && repeatMainPosition.checked)
    };
  }

  async function startRelayCaptureForCurrentPosition() {
    setRelayStatus('Creating phone capture link…', 'idle');
    if (relayStartBtn) relayStartBtn.disabled = true;
    renderRelayCapture({status: 'starting'});
    try {
      var resp = await postJson('relay/capture', {});
      renderRelayCapture(resp.relay);
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      setRelayStatus(e.message, 'bad');
      runBtn.disabled = false;
      if (relayStartBtn) relayStartBtn.disabled = false;
      continueBtn.disabled = false;
    }
  }

  async function startRelayMeasurement() {
    runBtn.disabled = true;
    if (relayStartBtn) relayStartBtn.disabled = true;
    resetMeasurementUiForStart();
    try {
      var resp = await postJson('start', measurementStartPayload());
      sessionId = resp.session_id;
    } catch (e) {
      setStateBadge('failed', e.message);
      setRelayStatus(e.message, 'bad');
      runBtn.disabled = false;
      if (relayStartBtn) relayStartBtn.disabled = false;
      return;
    }
    await startRelayCaptureForCurrentPosition();
  }

  async function startMeasurement() {
    if (relayMode) {
      await startRelayMeasurement();
      return;
    }
    var capturedLabel = selectedInputDevice && selectedInputDevice.browser_label;
    var mismatch = calibrationDeviceMismatch(capturedLabel);
    if (mismatch) {
      jtsAlert(mismatch);
      return;
    }
    runBtn.disabled = true;
    resetMeasurementUiForStart();
    try {
      var resp = await postJson('start', measurementStartPayload());
      sessionId = resp.session_id;
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
      return;
    }
    capturePreSweepNoise();
    pollState();
  }

  async function continueToNextPosition() {
    // Hide + disable Continue immediately so a double-tap can't fire
    // a second /next-position before the server transitions out of
    // NEEDS_NEXT_POSITION. (A user hit this in first-pass testing
    // — race between a double-tap and the worklet's stopCapture
    // → upload → state-transition cycle.)
    continueBtn.classList.add('hidden');
    continueBtn.disabled = true;
    positionPrompt.classList.add('hidden');
    setStateBadge('preparing', 'pausing music…');
    try {
      await postJson('next-position', {});
    } catch (e) {
      setStateBadge('failed', e.message);
      // pollState will reapply the button policy on next tick —
      // user can retry from the new state.
      return;
    }
    if (relayMode) {
      await startRelayCaptureForCurrentPosition();
      return;
    }
    capturePreSweepNoise();
    pollState();
  }

  async function repeatMainSeat() {
    repeatBtn.classList.add('hidden');
    repeatBtn.disabled = true;
    setStateBadge('preparing', 'preparing repeat sweep…');
    captureMode = 'repeat';
    if (workletNode) workletNode.port.postMessage('startCapture');
    try {
      await postJson('repeat-position', {});
    } catch (e) {
      captureMode = 'discard';
      if (workletNode) workletNode.port.postMessage('stopCapture');
      setStateBadge('failed', e.message);
      repeatBtn.disabled = false;
      return;
    }
    pollState();
  }

  // Auto-level: how much SNR (dB) above the measured room noise
  // floor we want the tone to sit at when we lock. 20-30 dB above
  // noise gives 15-25 dB SNR on the sweep (which is 6 dB quieter
  // than the tone source). Clamped on both ends:
  //   - lower clamp -30 dBFS: don't lock at very quiet absolute
  //     levels even in dead-silent rooms (capture would still work
  //     but the user wouldn't believe a measurement happened).
  //   - upper clamp -10 dBFS: avoid pushing the iPhone mic near
  //     its clipping ceiling.
  //
  // Previous hard-coded -20..-10 target was unreachable in normal
  // rooms (user's "decently loud voice at 10 cm" peaked at -25 dBFS
  // — a speaker tone at couch distance would land around -25 to
  // -35 dBFS at best). Adaptive band picks a target that's
  // physically achievable for whatever noise floor you've got.
  var AUTOLEVEL_SNR_DESIRED_LOW = 20;   // 20 dB above noise = minimum
  var AUTOLEVEL_SNR_DESIRED_HIGH = 30;  // 30 dB above noise = ideal
  var AUTOLEVEL_TARGET_DB_FLOOR = -30;  // lower clamp (absolute)
  var AUTOLEVEL_TARGET_DB_CEILING = -10; // upper clamp (absolute)

  function computeTargetBand(noiseFloorDb) {
    var high = Math.min(
      noiseFloorDb + AUTOLEVEL_SNR_DESIRED_HIGH,
      AUTOLEVEL_TARGET_DB_CEILING,
    );
    var low = Math.max(
      noiseFloorDb + AUTOLEVEL_SNR_DESIRED_LOW,
      AUTOLEVEL_TARGET_DB_FLOOR,
    );
    // In very noisy rooms the clamps can collide. Force a minimum
    // 5 dB window so a momentary RMS spike can satisfy the lock
    // condition.
    if (low > high - 5) low = high - 5;
    return { low: low, high: high };
  }

  async function startAutolevel() {
    autolevelBtn.disabled = true;
    runBtn.disabled = true;
    autolevelStatus.classList.remove('hidden');
    autolevelLockBtn.classList.remove('hidden');
    autolevelCancelBtn.classList.remove('hidden');
    autolevelRmsBuffer = [];

    // Step 1: measure ambient noise floor for ~500 ms BEFORE the
    // tone starts. This gives us a real number for "what counts as
    // quiet in this room right now", which we then use to pick a
    // target SNR band that's actually achievable. Hard-coded bands
    // from the previous version were unreachable in rooms where
    // the speaker-to-listener path attenuated more than I'd
    // assumed (real complaint from first-user test).
    autolevelLine.textContent = 'Measuring room noise…';
    autolevelDetail.textContent = '';
    var noiseSamples = [];
    var noiseSampler = setInterval(function () {
      if (latestMicRmsDb > -100) noiseSamples.push(latestMicRmsDb);
    }, 30);
    await new Promise(function (r) { setTimeout(r, 500); });
    clearInterval(noiseSampler);
    var noiseFloorDb;
    if (noiseSamples.length >= 3) {
      var nsum = 0;
      for (var ni = 0; ni < noiseSamples.length; ni++) nsum += noiseSamples[ni];
      noiseFloorDb = nsum / noiseSamples.length;
    } else {
      // Couldn't measure (mic stream not ready?). Fall back to a
      // reasonable assumption.
      noiseFloorDb = -50;
    }
    lastNoiseFloorDb = noiseFloorDb;
    var targetBand = computeTargetBand(noiseFloorDb);
    autolevelDetail.textContent =
      'Noise floor ' + noiseFloorDb.toFixed(0) + ' dBFS — target ' +
      targetBand.low.toFixed(0) + ' to ' + targetBand.high.toFixed(0) +
      ' dBFS. Tap Lock now if the tone sounds like a comfortable measurement level.';

    var lockSent = false;
    var sendLock = function (reason) {
      if (lockSent) return;
      lockSent = true;
      console.log('autolevel lock signal:', reason);
      fetch(endpoint('autolevel/lock'), {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      }).catch(function (e) { console.warn('lock POST failed', e); });
    };
    // Manual Lock button → send lock signal immediately.
    var prevLockHandler = autolevelLockBtn.onclick;
    autolevelLockBtn.onclick = function () { sendLock('manual'); };
    var prevCancelHandler = autolevelCancelBtn.onclick;
    autolevelCancelBtn.onclick = function () { cancelAutolevel(); };

    // Watch the latest mic RMS at 50 ms granularity. As soon as the
    // smoothed (last ~250 ms) RMS lands in the target range, send
    // auto-lock. Target band is the adaptive one computed above.
    var watcher = setInterval(function () {
      if (lockSent) return;
      var db = latestMicRmsDb;
      if (db <= -100) return;
      autolevelRmsBuffer.push(db);
      if (autolevelRmsBuffer.length > 5) autolevelRmsBuffer.shift();
      var sum = 0;
      for (var i = 0; i < autolevelRmsBuffer.length; i++) sum += autolevelRmsBuffer[i];
      var avg = sum / autolevelRmsBuffer.length;
      if (autolevelRmsBuffer.length >= 3 &&
          avg >= targetBand.low &&
          avg <= targetBand.high) {
        sendLock('mic ' + avg.toFixed(1) + ' dBFS in band ' +
          targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0));
      }
    }, 50);

    try {
      await postJson('autolevel/start', {});
    } catch (e) {
      clearInterval(watcher);
      autolevelLockBtn.onclick = prevLockHandler;
      autolevelCancelBtn.onclick = prevCancelHandler;
      autolevelLine.textContent = 'Auto-level failed: ' + e.message;
      autolevelLockBtn.classList.add('hidden');
      autolevelCancelBtn.classList.add('hidden');
      autolevelBtn.disabled = false;
      runBtn.disabled = false;
      return;
    }

    // Poll /status every 200 ms until autolevel reaches terminal.
    var pollOnce = async function () {
      try {
        var s = await fetchStatus();
        if (!s.autolevel) return true;
        var al = s.autolevel;
        var volStr = (al.current_main_volume_db !== null && al.current_main_volume_db !== undefined)
          ? al.current_main_volume_db.toFixed(1) : '?';
        autolevelLine.textContent = 'Auto-leveling at main_volume=' + volStr +
          ' dB · mic ' + latestMicRmsDb.toFixed(1) + ' dBFS · target ' +
          targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0) +
          (lockSent ? ' · lock sent' : '');
        if (al.status === 'ramping') return false;
        if (al.status === 'locked') {
          autolevelLine.textContent = '✓ Locked — speaker at ' +
            al.locked_main_volume_db.toFixed(1) + ' dB. Ready to measure.';
          autolevelDetail.textContent = '';
        } else if (al.status === 'maxed_out') {
          var capStr = (al.cap_db != null) ? al.cap_db.toFixed(0) : '?';
          autolevelLine.textContent =
            '✓ Leveled to ' + capStr + ' dB — the safe maximum for this room. ' +
            'You can measure now.';
          autolevelDetail.textContent =
            'The mic read ' + latestMicRmsDb.toFixed(0) + ' dBFS (target ' +
            targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0) + ' dBFS), ' +
            'so auto-level couldn’t confirm by ear. If the tone sounded reasonable, ' +
            'just tap Run measurement. To go louder, turn up your amplifier and retry.';
        } else if (al.status === 'cancelled') {
          autolevelLine.textContent = 'Auto-level cancelled — speaker volume restored.';
          autolevelDetail.textContent = '';
        } else if (al.status === 'error') {
          autolevelLine.textContent = 'Auto-level error.';
          autolevelDetail.textContent = al.error || '(no details)';
        }
        return true;
      } catch (e) {
        autolevelLine.textContent = 'Status fetch failed: ' + e.message;
        return true;
      }
    };

    while (true) {
      var done = await pollOnce();
      if (done) break;
      await new Promise(function (r) { setTimeout(r, 200); });
    }

    clearInterval(watcher);
    autolevelLockBtn.onclick = prevLockHandler;
    autolevelCancelBtn.onclick = prevCancelHandler;
    autolevelLockBtn.classList.add('hidden');
    autolevelCancelBtn.classList.add('hidden');
    autolevelBtn.disabled = false;
    runBtn.disabled = false;
  }

  async function cancelAutolevel() {
    try {
      await postJson('autolevel/cancel', {});
    } catch (e) {
      autolevelLine.textContent = 'Cancel POST failed: ' + e.message;
    }
  }

  async function startVerify() {
    verifyBtn.disabled = true;
    inVerifyMode = true;
    setStateBadge('verifying', 'pausing music for re-measurement…');
    captureMode = 'verify';
    if (workletNode) workletNode.port.postMessage('startCapture');
    try {
      await postJson('verify', {});
    } catch (e) {
      captureMode = 'discard';
      if (workletNode) workletNode.port.postMessage('stopCapture');
      setStateBadge('failed', e.message);
      verifyBtn.disabled = false;
      inVerifyMode = false;
      return;
    }
    verifyBtn.disabled = false;
    pollState();
  }

  // Centralised button-state policy. Every transition through
  // pollState first hides ALL action buttons, then re-shows the
  // ones the current state allows. Without this, stale buttons
  // (e.g. a still-visible Continue button during the next sweep)
  // accept double-clicks and trigger /next-position from the
  // wrong state.
  function applyButtonPolicy(state, autolevelStatus) {
    // Default: everything hidden / disabled.
    positionPrompt.classList.add('hidden');
    repeatBtn.classList.add('hidden');
    repeatBtn.disabled = false;
    continueBtn.classList.add('hidden');
    continueBtn.disabled = false;
    applyBtn.classList.add('hidden');
    applyBtn.disabled = false;
    verifyBtn.classList.add('hidden');
    verifyBtn.disabled = false;
    resetBtn.classList.add('hidden');
    resetBtn.disabled = false;
    cancelMeasureBtn.classList.add('hidden');
    cancelMeasureBtn.disabled = false;
    if (relayStartBtn) relayStartBtn.disabled = true;
    autolevelLockBtn.classList.add('hidden');
    autolevelLockBtn.disabled = false;
    autolevelCancelBtn.classList.add('hidden');
    autolevelCancelBtn.disabled = false;
    // Run + Auto-level: enabled only when nothing is in flight.
    // Disabled during measurement, autolevel ramp, etc.
    var idleStates = ['idle', 'ready', 'applied', 'verified', 'failed'];
    var sessionIdle = idleStates.indexOf(state) !== -1;
    var autolevelRamping = autolevelStatus === 'ramping';
    // Show Cancel only where the session is genuinely *waiting* on the user or
    // browser and no background task is about to overwrite the state. During
    // preparing/sweeping/analyzing/verifying a fire-and-forget sweep/analysis
    // task is running; /reset would race it (the task sets AWAITING_CAPTURE
    // *after* reset's IDLE) and the server now rejects it outright, so Cancel
    // would appear to fail. Those phases take seconds and land in a waiting
    // state on their own, where Cancel is shown. needs_noise_capture is a
    // waiting state too: the browser auto-records pre-sweep room noise, and if
    // mic permission is denied or the tab is backgrounded the upload never
    // arrives — without Cancel here the user was stranded until the
    // server-side watchdog fired (~120 s).
    var cancellableStates = [
      'needs_noise_capture',
      'awaiting_capture', 'awaiting_repeat_capture', 'awaiting_verify_capture',
      'needs_next_position', 'needs_repeat_capture',
    ];
    if (cancellableStates.indexOf(state) !== -1) {
      cancelMeasureBtn.classList.remove('hidden');
    }
    runBtn.disabled = !sessionIdle || autolevelRamping;
    autolevelBtn.disabled = !sessionIdle || autolevelRamping;
    if (relayMode) {
      autolevelBtn.disabled = true;
      hideEl(autolevelBtn, true);
      if (relayStartBtn) relayStartBtn.disabled = !sessionIdle;
    }
    // Per-state additions:
    if (autolevelRamping && !relayMode) {
      // Manual Lock + Cancel always available during the ramp so
      // the user can override the auto-detection (iOS Safari AGC
      // makes the mic-based decision unreliable in some setups).
      autolevelLockBtn.classList.remove('hidden');
      autolevelCancelBtn.classList.remove('hidden');
    }
    if (state === 'needs_next_position') {
      positionPrompt.classList.remove('hidden');
      continueBtn.classList.remove('hidden');
      runBtn.disabled = true;
      autolevelBtn.disabled = true;
    } else if (state === 'needs_repeat_capture') {
      positionPrompt.classList.remove('hidden');
      positionCurrent.textContent = '1';
      positionTotal.textContent = '1';
      if (!relayMode) repeatBtn.classList.remove('hidden');
      runBtn.disabled = true;
      autolevelBtn.disabled = true;
    } else if (state === 'ready') {
      applyBtn.classList.remove('hidden');
      resetBtn.classList.remove('hidden');
    } else if (state === 'applied' || state === 'verified') {
      if (!relayMode) verifyBtn.classList.remove('hidden');
      resetBtn.classList.remove('hidden');
    }
  }

  function renderRelayStatusFromSnapshot(snapshot) {
    if (!relayMode) return;
    if (snapshot && snapshot.relay) {
      renderRelayCapture(snapshot.relay);
      return;
    }
    if (snapshot && snapshot.state === 'ready') {
      setRelayStatus('Measurement is ready. Review confidence, then apply or reset.', 'ok');
    } else if (snapshot && snapshot.state === 'failed') {
      setRelayStatus(snapshot.error || 'Measurement failed. Cancel or start again.', 'bad');
    }
  }

  async function pollState() {
    if (pollTimer) clearTimeout(pollTimer);
    try {
      var s = await fetchStatus();
      currentState = s.state;
      renderRelayStatusFromSnapshot(s);
      var detail = s.error || '';
      if (s.total_positions > 1 && s.current_position !== undefined &&
          (s.state === 'preparing' || s.state === 'sweeping' ||
           s.state === 'awaiting_capture' || s.state === 'analyzing')) {
        detail = 'position ' + (s.current_position + 1) + ' of ' + s.total_positions;
      }
      setStateBadge(s.state, detail);
      renderQuality(s);
      renderBrowserAudioReport(s.browser_audio_report);
      renderConfidence(s);
      renderRuntimeIntegrity(s);
      applyButtonPolicy(s.state, s.autolevel ? s.autolevel.status : 'idle');

      if (s.state === 'needs_next_position') {
        positionCurrent.textContent = (s.current_position + 1);
        positionTotal.textContent = s.total_positions;
        return;
      }
      if (s.state === 'needs_repeat_capture') {
        return;
      }
      if (
        s.state === 'awaiting_capture' ||
        s.state === 'awaiting_verify_capture' ||
        s.state === 'awaiting_repeat_capture'
      ) {
        if (relayMode && s.state === 'awaiting_capture') {
          pollTimer = setTimeout(pollState, 500);
          return;
        }
        if (workletNode) workletNode.port.postMessage('stopCapture');
        return;  // upload-capture handler resumes polling
      }
      if (s.state === 'verified' && s.verify_metrics) {
        verifySummary.innerHTML =
          '<strong>Post-correction (50–350 Hz):</strong> RMS deviation ' +
          s.verify_metrics.rms_db.toFixed(1) + ' dB, max ' +
          s.verify_metrics.max_db.toFixed(1) + ' dB.<br>' +
          '<span class="hint">' +
          'Verify is a <em>single-position</em> measurement vs the ' +
          'multi-position averaged design — in a modal room (especially a ' +
          'cube), per-position swings of 10–15 dB at modal frequencies ' +
          'are normal. Some bands will look corrected, some over-corrected, ' +
          'some under-corrected. The audible test is what actually matters: ' +
          'play familiar bass-heavy music and listen for the bass tightening ' +
          'and modal "boom" reducing without the music sounding thinned-out.' +
          '</span>';
        verifySummary.classList.remove('hidden');
        return;
      }
      if (s.state === 'ready' || s.state === 'applied' || s.state === 'failed') {
        return;
      }
      // Mid-flight states: keep polling.
      pollTimer = setTimeout(pollState, 500);
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
    }
  }

  async function onCaptureReady(arrayBuffer, kind) {
    kind = kind || 'measurement';
    if (kind === 'discard') {
      return;
    }
    var float32 = new Float32Array(arrayBuffer);
    var wav = float32ToWav(float32, REQUIRED_SR);
    if (kind === 'noise') {
      setStateBadge('needs_noise_capture', 'uploading room noise…');
      captureMode = 'measurement';
      if (workletNode) workletNode.port.postMessage('startCapture');
      try {
        var noiseResp = await fetch(endpoint('upload-noise'), {
          method: 'POST',
          headers: csrfHeaders({'Content-Type': 'audio/wav'}),
          body: wav
        });
        if (!noiseResp.ok) {
          var noiseMsg = await noiseResp.text();
          throw new Error('upload-noise → ' + noiseResp.status + ': ' + noiseMsg);
        }
        await noiseResp.json();
        pollState();
      } catch (e) {
        captureMode = 'discard';
        if (workletNode) workletNode.port.postMessage('stopCapture');
        setStateBadge('failed', e.message);
        runBtn.disabled = false;
      }
      return;
    }

    setStateBadge('analyzing', 'uploading capture (' +
      Math.round(float32.length / REQUIRED_SR * 10) / 10 + ' s of audio)…');
    try {
      var resp = await fetch(endpoint('upload-capture'), {
        method: 'POST',
        headers: csrfHeaders({'Content-Type': 'audio/wav'}),
        body: wav
      });
      if (!resp.ok) {
        var msg = await resp.text();
        throw new Error('upload-capture → ' + resp.status + ': ' + msg);
      }
      var data = await resp.json();
      lastResult = data;
      // Verify pass: hold the design curves, overlay the new
      // measurement as `lastVerify`. Otherwise: redraw with the
      // freshly designed curves.
      if (data.verify) {
        lastVerify = data.verify;
      }
      // CRITICAL: show resultSection BEFORE drawing the chart. A
      // hidden canvas has zero bounding-rect dimensions, so
      // canvas.width gets set to 0 and the chart renders empty —
      // which is exactly the "frequency response is blank" bug a
      // user hit on the first measurement. Show, then draw, then
      // request a follow-up frame to redraw in case layout hadn't
      // settled (mobile Safari sometimes lags one frame on
      // display:block transitions).
      if (data.peqs) {
        renderPEQs(data.peqs);
      }
      renderDesignReport(data.design_report);
      renderConfidence(data);
      renderRuntimeIntegrity(data);
      renderResultsSummary(data);
      renderQuality(data);
      renderBrowserAudioReport(data.browser_audio_report);
      var hasResultPayload = !!(
        data.measured || data.verify || data.design_report ||
        (data.peqs && data.peqs.length)
      );
      if (hasResultPayload) resultSection.classList.remove('hidden');
      if (hasResultPayload && data.measured) {
        // Force a layout flush so getBoundingClientRect returns
        // real dimensions on the first draw.
        void canvas.offsetWidth;
        drawChart(data.measured, data.target, data.predicted, data);
        // Safety redraw next frame.
        requestAnimationFrame(function () {
          drawChart(data.measured, data.target, data.predicted, data);
        });
      }
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
      try {
        renderQuality(await fetchStatus());
      } catch (ignored) {}
    }
  }

  async function applyCorrection() {
    applyBtn.disabled = true;
    setStateBadge('analyzing', 'applying to CamillaDSP…');
    try {
      await postJson('apply', {});
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
    } finally {
      applyBtn.disabled = false;
    }
    refreshCurrentCorrection();
  }

  async function resetCorrection() {
    resetBtn.disabled = true;
    setStateBadge('analyzing', 'resetting correction…');
    try {
      await postJson('reset', {});
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
    } finally {
      resetBtn.disabled = false;
    }
    refreshCurrentCorrection();
  }

  // Always-available escape from an in-flight measurement. POSTs the same
  // /reset that restores the pre-measurement graph and forces the session to IDLE,
  // so a stranded awaiting_capture (or any active state) is recoverable from
  // the UI without SSH. The server-side watchdog also auto-recovers after
  // ~2 min; this is the instant manual path.
  async function cancelMeasurement() {
    if (!(await jtsConfirm('Cancel this measurement and restore the speaker?', {danger: true}))) {
      return;
    }
    cancelMeasureBtn.disabled = true;
    setStateBadge('idle', 'cancelling…');
    try {
      await postJson('reset', {});
    } catch (e) {
      setStateBadge('failed', e.message);
    } finally {
      cancelMeasureBtn.disabled = false;
    }
    pollState();
    refreshCurrentCorrection();
  }

  // iOS Safari only surfaces real device labels — and fully enumerates USB
  // audio devices — after a getUserMedia grant. Open a throwaway stream,
  // stop it immediately, then enumerate so the USB measurement mic appears
  // without the unplug/replug dance.
  async function detectMicrophones() {
    refreshInputsBtn.disabled = true;
    try {
      var tmp = await navigator.mediaDevices.getUserMedia({audio: true, video: false});
      tmp.getTracks().forEach(function (t) { t.stop(); });
    } catch (e) {
      console.warn('mic prime failed', e);
    }
    await populateInputDevices();
    refreshInputsBtn.disabled = false;
  }

  startBtn.addEventListener('click', function () { startMicCapture(); });
  refreshInputsBtn.addEventListener('click', function () { detectMicrophones(); });
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', function () {
      populateInputDevices(inputDeviceSelect.value);
    });
  }
  inputDeviceSelect.addEventListener('change', function () {
    var opt = inputDeviceSelect.options[inputDeviceSelect.selectedIndex];
    maybeInferCalibrationModel(opt ? opt.textContent : '');
    if (micStream) {
      startMicCapture().catch(function (e) {
        setStateBadge('failed', e.message);
      });
    }
  });
  micModelSelect.addEventListener('change', function () { updateMicCalibrationRows(); });
  micSerialInput.addEventListener('input', function () { invalidateLoadedCalibration(); });
  micOrientationSelect.addEventListener('change', function () { invalidateLoadedCalibration(); });
  calibrationSignSelect.addEventListener('change', function () { invalidateLoadedCalibration(); });
  calibrationFileInput.addEventListener('change', function () { invalidateLoadedCalibration(); });
  fetchCalibrationBtn.addEventListener('click', function () { fetchCalibration(); });
  uploadCalibrationBtn.addEventListener('click', function () { uploadCalibration(); });
  if (localCaptureFallbackBtn) {
    localCaptureFallbackBtn.addEventListener('click', function () {
      setRelayMode(false);
      detectMicrophones();
    });
  }
  if (relayStartBtn) {
    relayStartBtn.addEventListener('click', function () { startMeasurement(); });
  }
  runBtn.addEventListener('click', function () { startMeasurement(); });
  repeatBtn.addEventListener('click', function () { repeatMainSeat(); });
  continueBtn.addEventListener('click', function () { continueToNextPosition(); });
  applyBtn.addEventListener('click', function () { applyCorrection(); });
  verifyBtn.addEventListener('click', function () { startVerify(); });
  resetBtn.addEventListener('click', function () { resetCorrection(); });
  cancelMeasureBtn.addEventListener('click', function () { cancelMeasurement(); });
  document.addEventListener('visibilitychange', function () {
    // Re-acquire the wake lock the OS dropped while we were backgrounded,
    // but only while a capture is actually live.
    if (!document.hidden && micStream && !wakeLockSentinel) {
      acquireWakeLock();
    }
  });
  autolevelBtn.addEventListener('click', function () { startAutolevel(); });
  autolevelCancelBtn.addEventListener('click', function () { cancelAutolevel(); });
  currentCorrectionResetBtn.addEventListener('click', function () { resetFromBanner(); });
  loadSessionsBtn.addEventListener('click', function () { loadSessionReports(); });
  sessionHistory.addEventListener('click', function (ev) {
    var target = ev.target && ev.target.closest
      ? ev.target.closest('button[data-session-id], button[data-delete-session-id]')
      : null;
    if (!target) return;
    if (target.dataset.deleteSessionId) {
      deleteSessionBundle(target.dataset.deleteSessionId || '');
    } else {
      loadSessionReport(target.dataset.sessionId || '');
    }
  });

  // Auto-detect microphones on landing only for the local same-origin capture
  // flow. Relay-configured boxes start with the cloud phone-capture flow, so
  // this controller tab must not ask for mic permission unless the user chooses
  // the local fallback.
  if (relayConfigured) {
    setRelayMode(true);
    pollState();
  } else {
    if (!window.isSecureContext && currentPathname().indexOf('/correction/') === 0) {
      window.location.href = '/correction/proceed/room';
      return;
    }
    setRelayMode(false);
    detectMicrophones();
  }
  updateMicCalibrationRows();
  refreshCurrentCorrection();

  // Redraw chart on resize / orientation change — without this, the
  // canvas's drawing surface stays at the dimensions it had on the
  // first draw and gets CSS-stretched (blurry) on rotation.
  var resizeTimer = null;
  function scheduleChartRedraw() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      redrawLatestChart();
    }, 150);
  }
  window.addEventListener('resize', scheduleChartRedraw);
  window.addEventListener('orientationchange', scheduleChartRedraw);
  chartSmoothing.addEventListener('change', redrawLatestChart);
  chartShowSpread.addEventListener('change', redrawLatestChart);
  chartShowFilter.addEventListener('change', redrawLatestChart);
  chartShowBand.addEventListener('change', redrawLatestChart);
})();
