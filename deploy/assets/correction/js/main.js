// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Room correction — measurement + DSP wizard.
//
// Static ES module served from /assets/correction/js/ (revalidated by nginx,
// same delivery model as /system/ and /sound/). GET /envelope owns the exact
// whole-page section order and single forward action; this module validates
// and renders that closed contract while /status continues to drive capture,
// upload, autolevel, and safety-control mechanics. The getUserMedia,
// AudioWorklet, local/relay capture, and canvas paths still require a real Pi
// browser pass (HTTPS secure context + mic + CamillaDSP). See
// docs/HANDOFF-correction.md.
import { csrfHeaders, jsonHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";
// This page's local copy was named escapeText; import the shared escapeHtml
// under that name so its call sites stay byte-for-byte. The local copy coerced
// with `|| ''` vs the shared `?? ''`; every call site here passes a string or
// its own `|| 'fallback'`, so no falsy non-string reaches it — output is
// unchanged.
import { escapeHtml as escapeText } from "/assets/shared/js/escape.js";
import { renderRelayQr } from "/assets/shared/js/qr.js";
(function () {
  'use strict';

  var REQUIRED_SR = 48000;  // REQUIRED_SAMPLE_RATE — see jasper/web/correction_setup.py

  var pageRoot = document.querySelector('main.correction-stack');
  // The shared measurement kernel owns this trust margin. If the server ever
  // omits or corrupts it, suppress automatic lock and leave the bounded manual
  // Lock/cancel path available instead of treating ambient sound as the tone.
  var autolevelTrustMarginDb = Number(
    pageRoot ? pageRoot.dataset.levelTrustMarginDb : NaN
  );
  if (!Number.isFinite(autolevelTrustMarginDb) || autolevelTrustMarginDb < 0) {
    autolevelTrustMarginDb = Infinity;
  }
  var relayConfigured = !!(
    pageRoot && pageRoot.dataset.captureRelayEnabled === '1'
  );
  var relayMode = relayConfigured;
  var captureHandoffCopy = document.getElementById('capture-handoff-copy');
  var relayStatus = document.getElementById('relay-status');
  var relayLinkRow = document.getElementById('relay-link-row');
  var relayTapLink = document.getElementById('relay-tap-link');
  var relayQr = document.getElementById('relay-qr');
  var localCaptureFallbackBtn = document.getElementById('local-capture-fallback');
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
  // Household-mic prefill (Wave-2 persistence) — server-rendered default
  // selection from a prior session's successful calibration.
  var householdMicBanner = document.getElementById('household-mic-banner');
  var householdMicBannerText = document.getElementById('household-mic-banner-text');
  var householdMicChangeBtn = document.getElementById('household-mic-change');
  var currentCorrectionBanner = document.getElementById('current-correction');
  var currentCorrectionLabel = document.getElementById('current-correction-label');
  var currentCorrectionResetBtn = document.getElementById('current-correction-reset');
  // Latest server answer to "is reset allowed at all" — cached so the
  // one-reset-at-a-time reconciliation below (syncCurrentCorrectionReset)
  // can re-derive banner visibility without a fresh /status fetch whenever
  // the wizard's own reset button appears or disappears.
  var currentCorrectionResetAllowed = false;
  // Stepped-wizard chrome (P3b) — driven by GET /envelope.
  var wizardChrome = document.getElementById('wizard-chrome');
  var wizardSteps = document.getElementById('wizard-steps');
  var wizardVerdict = document.getElementById('wizard-verdict');
  var wizardNudges = document.getElementById('wizard-nudges');
  var wizardNextBtn = document.getElementById('wizard-next');
  var readinessBlockerMessage = document.getElementById('readiness-blocker-message');
  var readinessBlockerAction = document.getElementById('readiness-blocker-action');
  // P6 tuning assistant elements.
  var tuningPanel = document.getElementById('tuning-panel');
  var tuningNudge = document.getElementById('tuning-nudge');
  var tuningActions = document.getElementById('tuning-actions');
  var tuningInterpretBtn = document.getElementById('tuning-interpret');
  var tuningProposeBtn = document.getElementById('tuning-propose');
  var tuningStatus = document.getElementById('tuning-status');
  var tuningExplanation = document.getElementById('tuning-explanation');
  var tuningProvenance = document.getElementById('tuning-provenance');
  var tuningProposals = document.getElementById('tuning-proposals');
  var constraintsBlock = document.getElementById('constraints');
  var rowsTbody = document.getElementById('constraint-rows');
  var errBanner = document.getElementById('err-banner');
  var browserAudioReport = document.getElementById('browser-audio-report');
  var levelBar = document.getElementById('level-bar-fill');
  var levelReadout = document.getElementById('level-db');
  var envelopeSections = document.getElementById('envelope-sections');
  var measurementReview = document.getElementById('measurement-review');
  var resultProof = document.getElementById('result-proof');
  var qualityBanner = document.getElementById('quality-banner');
  var autolevelLockBtn = document.getElementById('autolevel-lock');
  var autolevelCancelBtn = document.getElementById('autolevel-cancel');
  var autolevelStatus = document.getElementById('autolevel-status');
  var autolevelLine = document.getElementById('autolevel-line');
  var autolevelDetail = document.getElementById('autolevel-detail');
  var autolevelHint = document.getElementById('autolevel-hint');
  var resetBtn = document.getElementById('reset-correction');
  var cancelMeasureBtn = document.getElementById('cancel-measurement');
  var emergencyStopActive = false;
  var measurementOptions = document.getElementById('measurement-options');
  var changeRunDefaultsBtn = document.getElementById('change-run-defaults');
  var runDefaultsSummary = document.getElementById('run-defaults-summary');
  var repeatMainPositionDisclosure = document.getElementById(
    'repeat-main-position-disclosure'
  );
  var positionsSelect = document.getElementById('positions-select');
  var targetSelect = document.getElementById('target-select');
  var strategySelect = document.getElementById('strategy-select');
  var positionPrompt = document.getElementById('position-prompt');
  var positionCurrent = document.getElementById('position-current');
  var positionTotal = document.getElementById('position-total');
  var resultSection = document.getElementById('result-section');
  var chartShowFilter = document.getElementById('chart-show-filter');
  var canvas = document.getElementById('chart');
  var loadSessionsBtn = document.getElementById('load-sessions');
  var sessionHistory = document.getElementById('session-history');
  var sessionReport = document.getElementById('session-report');

  var ctx = null;
  var micStream = null;
  var workletNode = null;
  var pollTimer = null;
  var sessionId = null;
  var runTransportLocked = false;
  var localCaptureSetupBound = false;
  var localRunOwnedByThisTab = false;
  var localRunOwnerSessionId = null;
  var wizardActionInFlight = false;
  var envelopeRequestInFlight = null;
  var envelopeRefreshQueued = false;
  var envelopeFetchTimeoutMs = 5000;
  var idleEnvelopeRefreshMs = 10000;
  var noiseCaptureCompletion = null;
  var noiseCaptureResolve = null;
  var noiseCaptureReject = null;
  var noiseCaptureTimeout = null;
  var lastChartEnvelope = null;
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

  // Stepped-wizard (P3b) envelope-poll bookkeeping. The /status poll stays
  // the capture/upload/autolevel mechanism layer; the ENVELOPE drives the
  // user-facing wizard chrome (step, verdict, nudges, primary action). To
  // honour the P3b-1 reviewer's poll discipline — hot-poll only during
  // active capture, fetch once per state change on static screens — we
  // track status observations separately from the last successfully rendered
  // envelope and refresh on a transition, plus a low-frequency tick while a
  // capture screen is live. A failed envelope must not make every unchanged
  // 500 ms status tick look like a new transition.
  var lastObservedStatusState = null;
  var lastRenderedEnvelopeScreen = null;
  var lastRenderedEnvelopeState = null;
  var lastRenderedReadinessSignature = null;
  var envelopeTimer = null;
  var envelopePollingEnabled = true;
  // A typed POST refusal belongs to the current interaction, not the session
  // mechanism state. Keep it across the immediate envelope refresh so a
  // still-ready session cannot overwrite the homeowner sentence with stale
  // "Ready" copy. A server blocker/failure or the next action clears it.
  var pendingHomeownerFailure = null;
  // One bounded retry credit per failure streak: each successful envelope
  // fetch (and each fresh trigger — state change, wizard click, landing)
  // re-arms it; a failure consumes it to schedule exactly one retry. Two
  // consecutive failures stop until the next external trigger, so a
  // persistent outage never turns into a retry loop on a static screen.
  var envelopeRetryArmed = false;
  // Probe-visible fetch counter (harness reads this to prove fetch-once
  // discipline: it must increment once per state change, not per poll tick).
  var envelopeFetchCount = 0;
  var runDefaultsDirty = false;
  var currentRunDefaults = null;

  // Logical screens whose data is live-updating (capture in flight or a
  // short server-side transient) — the only screens the envelope is
  // re-fetched on a timer. Static screens (idle/review/apply/result) are
  // edge-triggered off a state change, never hot-polled.
  var ACTIVE_ENVELOPE_SCREENS = {
    mic: true, level: true, sweep: true, verify: true,
  };

  // Schema v9 has one exact six-step progress spine. The server supplies its
  // homeowner labels; the browser owns only the structural bound.
  var WIZARD_PROGRESS_TOTAL = 6;

  var SUPPORTED_ENVELOPE_SCHEMA = 9;
  var KNOWN_ENVELOPE_SCREENS = {
    idle: true, mic: true, level: true, sweep: true,
    review: true, apply: true, verify: true, result: true,
  };
  var KNOWN_ACTION_ENDPOINTS = {
    '/start': true,
    '/next-position': true,
    '/repeat-position': true,
    '/local-capture/setup': true,
    '/autolevel/start': true,
    '/upload-noise': true,
    '/apply': true,
    '/reset': true,
    '/verify': true,
    '/relay/level-match': true,
    '/relay/capture': true,
    '/relay/verify': true,
  };
  // Closed presentation vocabulary. Codes are duplicated at this wire
  // boundary deliberately: a malformed/partially deployed server must not
  // smuggle arbitrary diagnostics into a block the browser treats as safe.
  var KNOWN_FAILURES = {
    speaker_setup_incomplete: {text: "Finish speaker setup first.", retryable: false},
    speaker_readiness_unavailable: {text: "Speaker setup could not be checked. Try again.", retryable: true},
    measurement_in_progress: {text: "A measurement is already in progress. Finish or stop it before starting again.", retryable: true},
    measurement_setup_invalid: {text: "The measurement setup changed. Review the microphone choices and try again.", retryable: true},
    speaker_measurement_unsafe: {text: "The speaker is not ready to measure safely. Review speaker setup, then try again.", retryable: false},
    microphone_setup_unavailable: {text: "The saved microphone setup is unavailable. Choose the microphone again.", retryable: true},
    phone_capture_unavailable: {text: "Phone capture could not be opened. Try again or use this device.", retryable: true},
    measurement_stopped: {text: "Measurement stopped.", retryable: true},
    test_signal_unavailable: {text: "The speaker could not play the test sound. Try again.", retryable: true},
    measurement_analysis_failed: {text: "The speaker could not finish this measurement. Try measuring again.", retryable: true},
    measurement_evidence_unsafe: {text: "This measurement did not pass its safety checks. Measure again.", retryable: true},
    correction_update_failed: {text: "The correction could not be applied. Check the current correction before trying again.", retryable: true},
    correction_restore_failed: {text: "The previous sound could not be confirmed restored. The correction may still be applied.", retryable: true},
    correction_auto_revert_failed: {text: "That measured worse, but the correction could not be removed automatically. It is STILL APPLIED. Use Reset to remove it.", retryable: true},
    tuning_busy: {text: "The tuning assistant just ran. Wait a moment, then try again.", retryable: true},
    tuning_spend_limit: {text: "The daily assistant budget is reached. Try again after the daily rollover.", retryable: false},
    tuning_unavailable: {text: "The tuning assistant is not set up yet.", retryable: false},
    tuning_request_failed: {text: "The tuning assistant could not continue. Try again.", retryable: true},
    tuning_proposal_rejected: {text: "That suggestion was not applied because it did not pass the speaker's safety checks.", retryable: true},
    unknown_failure: {text: "The speaker could not continue this step. Try again.", retryable: true},
  };
  var KNOWN_SECTION_IDS = [
    'current-correction', 'run-defaults', 'readiness-blocker',
    'capture-handoff', 'placement', 'capture-setup',
    'local-certificate-warning', 'level-check', 'position-capture',
    'measurement-review', 'apply-status', 'verification', 'result-proof',
    'tuning', 'reports',
  ];
  var sectionNodes = {};
  KNOWN_SECTION_IDS.forEach(function (sectionId) {
    var node = document.querySelector('[data-envelope-section="' + sectionId + '"]');
    if (node) sectionNodes[sectionId] = node;
  });

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
      renderRelayQr(relayQr, null);
      return;
    }
    var tapLink = relay.tap_link || '';
    if (relay.status === 'awaiting_phone' && tapLink && relayTapLink) {
      relayTapLink.href = tapLink;
      hideEl(relayLinkRow, false);
      renderRelayQr(relayQr, tapLink);
    } else {
      hideEl(relayLinkRow, true);
      if (relayTapLink) relayTapLink.href = '#';
      renderRelayQr(relayQr, null);
    }
    if (relay.status === 'complete') {
      setRelayStatus('Phone capture received. Wait for the next instruction on this page.', 'ok');
    } else if (relay.status === 'failed') {
      console.warn('phone capture failed', relay.error || '');
      setRelayStatus('Phone capture stopped. Try that step again.', 'bad');
    } else if (relay.status === 'starting') {
      setRelayStatus('Creating phone capture link…', 'idle');
    } else {
      setRelayStatus('Open the capture page on the phone and keep it awake until the sweep finishes.', 'idle');
    }
  }

  function setRelayMode(enabled) {
    relayMode = !!enabled;
    if (captureHandoffCopy) {
      captureHandoffCopy.textContent = relayMode
        ? 'Continue on the phone while the speaker coordinates each capture.'
        : 'This device will capture the microphone signal locally.';
    }
    if (localCaptureFallbackBtn) {
      localCaptureFallbackBtn.textContent = relayMode
        ? "Use this device's microphone"
        : 'Use phone capture';
    }
    hideEl(localCaptureFallbackBtn, runTransportLocked || !relayConfigured);
    hideEl(autolevelLockBtn, true);
    hideEl(autolevelCancelBtn, true);
    hideEl(autolevelStatus, true);
    hideEl(autolevelHint, relayMode);
    if (relayMode) {
      stopMicStream();
      setRelayStatus('', 'idle');
    } else {
      renderRelayCapture(null);
      setRelayStatus('', 'idle');
    }
  }

  function setRunTransportLocked(locked) {
    runTransportLocked = !!locked;
    if (changeRunDefaultsBtn) changeRunDefaultsBtn.disabled = runTransportLocked;
    if (runTransportLocked) {
      setMeasurementOptionsOpen(false);
      hideEl(localCaptureFallbackBtn, true);
    } else {
      hideEl(localCaptureFallbackBtn, !relayConfigured);
    }
  }

  function setMeasurementOptionsOpen(open) {
    hideEl(measurementOptions, !open);
    if (changeRunDefaultsBtn) {
      changeRunDefaultsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  }

  function selectedOptionLabel(select) {
    if (!select || select.selectedIndex < 0 || !select.options) return '';
    var option = select.options[select.selectedIndex];
    return option ? String(option.textContent || '').split(' — ')[0].trim() : '';
  }

  function updateRunDefaultsSummaryFromControls() {
    var positions = Number(positionsSelect && positionsSelect.value);
    var selectedPosition = positionsSelect && positionsSelect.options &&
      positionsSelect.options[positionsSelect.selectedIndex];
    var positionsLabel = selectedPosition &&
      selectedPosition.getAttribute('data-summary-label');
    var targetLabel = selectedOptionLabel(targetSelect).toLowerCase();
    if (!currentRunDefaults || !Number.isInteger(positions) ||
        positions <= 0 || !positionsLabel || !targetLabel) return;
    runDefaultsSummary.textContent = currentRunDefaults.summary_template
      .split('{positions_label}').join(positionsLabel)
      .split('{target}').join(targetLabel);
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

  // Household-mic prefill (Wave-2 persistence): the server embeds the
  // household's remembered mic + calibration as a JSON data island
  // (#household-mic-data) when jasper/correction/household_mic.py has a
  // record whose calibration still resolves on disk. Applied once at page
  // load, before any device/permission flow runs, so the household need not
  // re-enter a serial or re-upload a file it already gave JTS. Deliberately
  // does NOT call updateMicCalibrationRows() — that resets
  // selectedCalibrationId to null and can trigger a per-browser
  // localStorage-remembered-serial auto-fetch (loadSavedSerial), neither of
  // which is wanted when the server has already resolved the exact
  // calibration file. "Change" clears the prefill and falls back to the
  // ordinary blank-state flow (model reset, rows hidden, no auto-fetch).
  function applyHouseholdMicPrefill() {
    var island = document.getElementById('household-mic-data');
    if (!island) return;
    var data;
    try {
      data = JSON.parse(island.textContent || 'null');
    } catch (e) {
      return;
    }
    if (!data || !data.calibration) return;
    var modelKey = data.model_key || '';
    var hasOption = modelKey === 'other' || Array.prototype.some.call(
      micModelSelect.options,
      function (o) { return o.value === modelKey; }
    );
    if (!hasOption) return;  // registry drifted since the record was saved
    micModelSelect.value = modelKey;
    if (!modelKey) {
      serialRow.classList.add('hidden');
      uploadRow.classList.add('hidden');
    } else if (modelKey === 'other') {
      serialRow.classList.add('hidden');
      uploadRow.classList.remove('hidden');
    } else {
      serialRow.classList.remove('hidden');
      uploadRow.classList.remove('hidden');
    }
    showCalibrationLoaded(data);
    if (!selectedCalibrationId) return;
    householdMicBannerText.textContent =
      'Using ' + selectedCalibrationMeta.label + ' — remembered from your last measurement.';
    householdMicBanner.classList.remove('hidden');
  }

  householdMicChangeBtn.addEventListener('click', function () {
    householdMicBanner.classList.add('hidden');
    micModelSelect.value = '';
    updateMicCalibrationRows();
  });

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
      console.warn('calibration lookup failed', e);
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent =
        'Calibration lookup failed. Use upload as a fallback.';
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
      console.warn('calibration upload failed', e);
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent = 'Calibration upload failed. Try again.';
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
      'measurement mic under Input device, then choose Allow microphone again.';
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
    } else {
      errBanner.classList.add('hidden');
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
    browserAudioReport.innerHTML =
      '<strong>Browser audio path: ' +
      escapeText(level === 'ok' ? 'ready' : (level === 'fail' ? 'blocked' : 'usable with warnings')) +
      '</strong><p class="hint">' + (level === 'ok'
        ? 'The microphone settings are ready for measurement.'
        : (level === 'fail'
          ? 'The microphone settings are not safe for this measurement.'
          : 'The microphone may reduce measurement accuracy.')) + '</p>';
  }

  var LOCAL_CAPTURE_MEMORY_KEY = 'jts-room-local-capture-v1';

  function readLocalCaptureMemory() {
    try {
      var raw = window.sessionStorage &&
        window.sessionStorage.getItem(LOCAL_CAPTURE_MEMORY_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch (_e) {
      return null;
    }
  }

  function rememberLocalCapture(deviceId) {
    try {
      if (!window.sessionStorage || !sessionId) return;
      window.sessionStorage.setItem(LOCAL_CAPTURE_MEMORY_KEY, JSON.stringify({
        session_id: sessionId,
        device_id: deviceId || null,
        calibration_id: selectedCalibrationId || null
      }));
    } catch (_e) {}
  }

  function syncSessionMechanics(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    var serverSessionId = snapshot.session_id
      ? String(snapshot.session_id) : null;
    localCaptureSetupBound = snapshot.local_capture_setup_bound === true;
    var liveRun = snapshot.state !== 'idle' && snapshot.state !== 'failed';
    var remembered = readLocalCaptureMemory();
    var matchingMemory = remembered && remembered.session_id === serverSessionId
      ? remembered : null;
    if (liveRun && snapshot.capture_transport !== 'relay') {
      // Only the tab that received /start may perform the one-shot local bind.
      // A different stale tab can observe the live session, but must not adopt
      // its identity and attach an arbitrary microphone to it.
      localRunOwnedByThisTab = !!matchingMemory ||
        localRunOwnerSessionId === serverSessionId;
      sessionId = localRunOwnedByThisTab ? serverSessionId : null;
    } else {
      localRunOwnedByThisTab = false;
      sessionId = serverSessionId;
    }
    if (liveRun) {
      setRelayMode(snapshot.capture_transport === 'relay');
    }
    setRunTransportLocked(liveRun);
    if (matchingMemory) {
      selectedCalibrationId = matchingMemory.calibration_id || null;
    }
  }

  async function refreshSessionMechanics() {
    var snapshot = await fetchStatus();
    syncSessionMechanics(snapshot);
    return snapshot;
  }

  async function startMicCapture() {
    try {
      await refreshSessionMechanics();
    } catch (_e) {
      jtsAlert('The speaker could not confirm the current measurement. Try again.');
      return false;
    }
    if (!relayMode && !localRunOwnedByThisTab) {
      jtsAlert('This measurement was started in another tab. Return to that tab, ' +
        'or cancel the measurement and start again here.');
      return false;
    }
    var remembered = readLocalCaptureMemory();
    var matchingMemory = remembered && remembered.session_id === sessionId
      ? remembered : null;
    if (localCaptureSetupBound && !matchingMemory) {
      jtsAlert('This tab no longer has the microphone identity for the active run. ' +
        'Cancel this measurement and start again.');
      return false;
    }
    stopMicStream();

    try {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      ctx = new Ctor({sampleRate: REQUIRED_SR});
    } catch (e) {
      console.warn('AudioContext unavailable', e);
      jtsAlert('This browser could not start microphone capture. Try again.');
      return false;
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
    var desiredDeviceId = inputDeviceSelect.value ||
      (matchingMemory && matchingMemory.device_id) || '';
    if (desiredDeviceId) audioConstraints.deviceId = {exact: desiredDeviceId};
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
        console.warn('microphone permission unavailable', e);
        jtsAlert('Microphone access was not available. Check permission and try again.');
      }
      return false;
    }

    constraintsBlock.classList.remove('hidden');

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
    if (!desiredDeviceId && !localCaptureSetupBound) {
      // Safari reveals the real USB device list only after a permission grant.
      // Treat this first default stream as discovery only; never commit it as
      // the run's one-shot microphone identity.
      stopMicStream();
      await populateInputDevices();
      jtsAlert('Microphones are now available. Select the one you want, then ' +
        'choose Allow microphone again.');
      return false;
    }
    if (desiredDeviceId && actual.deviceId &&
        actual.deviceId !== desiredDeviceId) {
      stopMicStream();
      jtsAlert('The browser opened a different microphone than the one bound ' +
        'to this run. Cancel the measurement and start again.');
      return false;
    }
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
      stopMicStream();
      console.warn('microphone processor unavailable', e);
      jtsAlert('This browser could not prepare microphone capture. Try again.');
      return false;
    } finally {
      URL.revokeObjectURL(blobUrl);
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
        if (levelReadout) levelReadout.textContent = db.toFixed(1);
        // Stash for the autolevel loop (which polls this).
        latestMicRmsDb = db;
      } else if (ev.data && ev.data.type === 'capture') {
        onCaptureReady(ev.data.buffer, captureMode);
      }
    };
    src.connect(workletNode);
    // No mic→destination connection — would create a feedback loop
    // on the smart speaker that's the listening target.

    rememberLocalCapture(actual.deviceId || desiredDeviceId);
    if (!localCaptureSetupBound) {
      var boundSetup = null;
      var bindError = null;
      var bindPayload = {
        session_id: sessionId,
        calibration_id: selectedCalibrationId,
        input_device: selectedInputDevice
      };
      // A response can be lost after the server commits the binding. The server
      // treats this exact identity retry as idempotent; keep the browser retry
      // bounded so a real rejection never becomes a request loop.
      for (var bindAttempt = 0; bindAttempt < 2; bindAttempt++) {
        try {
          boundSetup = await postJson('local-capture/setup', bindPayload);
          bindError = null;
          break;
        } catch (e) {
          bindError = e;
          if (bindAttempt === 0) {
            await new Promise(function (resolve) { setTimeout(resolve, 200); });
          }
        }
      }
      if (bindError) {
        console.warn('local capture setup bind failed', bindError);
        stopMicStream();
        jtsAlert('The speaker could not use that microphone setup. Try again.');
        return false;
      }

      if (boundSetup && boundSetup.state === 'needs_noise_capture') {
        localCaptureSetupBound = true;
        pollState();
      }
    }
    await acquireWakeLock();
    return true;
  }

  async function ensureLocalCaptureReady() {
    if (relayMode) return false;
    if (workletNode && micStream) return true;
    return await startMicCapture();
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
    // Whole-page progress/copy comes from the server envelope. Keep raw
    // mechanism details out of the DOM; the envelope refresh replaces this
    // bounded fallback with the server's typed homeowner failure.
    if (state === 'failed') {
      console.warn('room-correction mechanism failed', detail || '');
      if (wizardVerdict) {
        wizardVerdict.textContent =
          'The speaker could not continue this step. Try again.';
      }
    }
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
        var key = [issue.severity, issue.code].join('|');
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
      '</strong><p>' + (hasFail
        ? 'This capture could not be used safely. Try this position again.'
        : 'A quieter re-measure may improve confidence, but you can continue.') +
      '</p>';
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

  function setCurrentCorrectionTone(tone) {
    currentCorrectionBanner.classList.remove('applied', 'custom', 'flat');
    currentCorrectionBanner.classList.add(tone);
  }

  // One reset affordance visible at a time. resetBtn (#reset-correction) is
  // the wizard-flow control renderSections() re-homes into whichever of
  // measurement-review / apply-status / result-proof is the live section;
  // applyButtonPolicy() owns its own hidden class on top of that. When both
  // agree it is on-screen, the user's attention is already on the wizard
  // section that hosts it, so the persistent status banner defers to it —
  // the banner stays the reset home for every other state (e.g. idle with
  // an applied correction, where no wizard section hosts the button).
  // Structural, not positional: renderSections MOVES the single button node
  // between host sections, so "any host section visible" could false-positive
  // while the button sits inside a hidden one (leaving ZERO visible resets).
  // Walk the button's own ancestor chain instead.
  function isWizardResetVisible() {
    for (var node = resetBtn; node; node = node.parentNode || null) {
      if (node.classList && node.classList.contains('hidden')) return false;
    }
    return true;
  }

  function syncCurrentCorrectionReset() {
    hideEl(
      currentCorrectionResetBtn,
      !currentCorrectionResetAllowed || isWizardResetVisible()
    );
  }

  function renderCurrentCorrectionUnavailable() {
    setCurrentCorrectionTone('flat');
    currentCorrectionLabel.textContent =
      'The current correction could not be checked. Try again.';
    currentCorrectionResetAllowed = false;
    syncCurrentCorrectionReset();
  }

  function renderCurrentCorrection(presentation) {
    var tones = {applied: true, custom: true, flat: true};
    if (!presentation || typeof presentation !== 'object' ||
        !tones[presentation.tone] ||
        typeof presentation.message_template !== 'string' ||
        !presentation.message_template.trim() ||
        typeof presentation.reset_allowed !== 'boolean' ||
        !Object.prototype.hasOwnProperty.call(
          presentation, 'applied_at_epoch'
        ) ||
        (presentation.applied_at_epoch !== null &&
          (!Number.isInteger(presentation.applied_at_epoch) ||
           presentation.applied_at_epoch < 0))) {
      renderCurrentCorrectionUnavailable();
      return;
    }
    var message = presentation.message_template;
    if (message.indexOf('{applied_at}') !== -1) {
      var when = formatAppliedAt(presentation.applied_at_epoch);
      if (!when) {
        renderCurrentCorrectionUnavailable();
        return;
      }
      message = message.split('{applied_at}').join(when);
    }
    if (/\{[^{}]+\}/.test(message)) {
      renderCurrentCorrectionUnavailable();
      return;
    }
    setCurrentCorrectionTone(presentation.tone);
    currentCorrectionLabel.textContent = message;
    currentCorrectionResetAllowed = !!presentation.reset_allowed;
    syncCurrentCorrectionReset();
  }

  async function refreshCurrentCorrection() {
    try {
      var s = await fetchStatus();
      renderCurrentCorrection(s.current_correction_presentation);
    } catch (e) {
      renderCurrentCorrectionUnavailable();
    }
  }

  async function resetFromBanner() {
    if (!(await jtsConfirm(
      'Reset room correction? This removes the applied adjustments and cannot be undone.',
      {danger: true},
    ))) {
      return;
    }
    currentCorrectionResetBtn.disabled = true;
    currentCorrectionLabel.textContent = 'Resetting correction…';
    try {
      await postJson('reset', {});
    } catch (e) {
      currentCorrectionLabel.textContent = safeErrorMessage(
        e,
        GENERIC_STEP_FAILURE
      );
      currentCorrectionResetBtn.disabled = false;
      return;
    }
    await refreshCurrentCorrection();
    currentCorrectionResetBtn.disabled = false;
  }

  // ==========================================================================
  // Stepped-wizard router (P3b) — dumb frontend over GET /envelope.
  //
  // The server hands one JSON screen envelope per step (jasper/correction/
  // envelope.py): {screen, verdict_text, nudges[], next_action, progress,
  // curves (server-smoothed), fill_segments, headline}. The browser renders
  // it verbatim — no client DSP, no client thresholds, no re-deriving the
  // step. Nudges are homeowner sentences with a severity (info|warn) and
  // NEVER gate: the primary action stays live even under a warn nudge. This
  // is the presentation contract; the /status poll below still owns the
  // capture/upload/autolevel/relay mechanics the envelope can't express.
  // ==========================================================================

  // Render the step indicator from envelope.progress
  // ({position, total, labels}). Both the current position and homeowner
  // labels come from the server, so the browser never re-counts or authors
  // the flow.
  function renderProgress(progress) {
    if (!wizardSteps) return;
    var total = progress.total;
    var position = progress.position;
    wizardSteps.innerHTML = '';
    for (var i = 1; i <= total; i++) {
      var li = document.createElement('li');
      li.className = 'wizard-step' +
        (i === position ? ' current' : (i < position ? ' done' : ''));
      var dot = document.createElement('span');
      dot.className = 'wizard-step__dot';
      dot.setAttribute('aria-hidden', 'true');
      var label = document.createElement('span');
      label.className = 'wizard-step__label';
      label.textContent = progress.labels[i - 1];
      li.appendChild(dot);
      li.appendChild(label);
      wizardSteps.appendChild(li);
    }
    wizardSteps.setAttribute(
      'aria-label', 'Step ' + position + ' of ' + total,
    );
  }

  // Render the homeowner nudges (a sentence + a severity). Each nudge text
  // is server-authored plain English; escapeText keeps it inert if a future
  // nudge ever carries an interpolated device/room string. Severity drives
  // the tone class only — a nudge NEVER disables anything (the "measurement
  // quality nudges, never blocks" rule).
  function renderNudges(nudges) {
    if (!wizardNudges) return;
    wizardNudges.innerHTML = '';
    if (!nudges || !nudges.length) {
      wizardNudges.classList.add('hidden');
      return;
    }
    wizardNudges.classList.remove('hidden');
    nudges.forEach(function (nudge) {
      if (!nudge || !nudge.text) return;
      var sev = nudge.severity === 'warn' ? 'warn' : 'info';
      var row = document.createElement('div');
      row.className = 'wizard-nudge ' + sev;
      var icon = document.createElement('span');
      icon.className = 'wizard-nudge__icon';
      icon.setAttribute('aria-hidden', 'true');
      // A checkmark for info ("you can continue"), a caret for warn — both
      // are advisory; the copy itself always says continue is fine.
      icon.textContent = sev === 'warn' ? '!' : '✓';
      var text = document.createElement('span');
      text.className = 'wizard-nudge__text';
      text.textContent = String(nudge.text);
      row.appendChild(icon);
      row.appendChild(text);
      wizardNudges.appendChild(row);
    });
  }

  // Render the single primary action from envelope.next_action
  // ({label, endpoint}) or null. The button is ALWAYS live when present —
  // nudges never disable it. Clicking POSTs the server-named endpoint and
  // then refreshes both the mechanism (/status) and the chrome (/envelope).
  // next_action === null means a browser-driven or terminal step: no button.
  function renderPrimaryAction(nextAction) {
    if (!wizardNextBtn) return;
    if (wizardActionInFlight) {
      wizardNextBtn.classList.add('hidden');
      wizardNextBtn.disabled = true;
      return;
    }
    if (!nextAction || !nextAction.endpoint) {
      wizardNextBtn.classList.add('hidden');
      wizardNextBtn.textContent = '';
      wizardNextBtn.removeAttribute('data-endpoint');
      return;
    }
    wizardNextBtn.textContent = String(nextAction.label);
    wizardNextBtn.setAttribute('data-endpoint', String(nextAction.endpoint));
    wizardNextBtn.disabled = false;   // nudges never gate the action
    wizardNextBtn.classList.remove('hidden');
  }

  // Delegated click for the wizard primary action. Reads the endpoint from
  // the data-* attribute the router set (no interpolated inline handler).
  async function onWizardNextClick() {
    if (wizardActionInFlight) return;
    var ep = wizardNextBtn.getAttribute('data-endpoint');
    if (!ep) return;
    pendingHomeownerFailure = null;
    wizardActionInFlight = true;
    wizardNextBtn.disabled = true;
    wizardNextBtn.classList.add('hidden');
    try {
      if (ep === '/start') {
        await startMeasurement();
      } else if (ep === '/relay/level-match') {
        await startRelayLevelMatch();
      } else if (ep === '/relay/capture') {
        await startRelayCaptureForCurrentPosition();
      } else if (ep === '/relay/verify') {
        await startRelayVerify();
      } else if (ep === '/next-position') {
        await continueToNextPosition();
      } else if (ep === '/repeat-position') {
        await repeatMainSeat();
      } else if (ep === '/local-capture/setup') {
        // The OS permission dialog can take a moment to appear (or need a
        // second tap to bring the browser forward); say so immediately so
        // the tap doesn't look like it did nothing.
        if (wizardVerdict) {
          wizardVerdict.textContent = "Waiting for the browser's permission prompt…";
        }
        await startMicCapture();
      } else if (ep === '/autolevel/start') {
        if (!(await ensureLocalCaptureReady())) {
          throw new Error('local microphone capture is not ready');
        }
        await startAutolevel();
      } else if (ep === '/upload-noise') {
        if (!(await ensureLocalCaptureReady())) {
          throw new Error('local microphone capture is not ready');
        }
        await capturePreSweepNoise();
      } else if (ep === '/apply') {
        await applyCorrection(wizardNextBtn);
      } else if (ep === '/reset') {
        await resetCorrection();
      } else if (ep === '/verify') {
        await startVerify(wizardNextBtn);
      } else {
        throw new Error('unsupported room-correction action');
      }
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
    } finally {
      wizardActionInFlight = false;
      wizardNextBtn.disabled = false;
    }
    pollState();
    envelopeRetryArmed = true;   // a fresh trigger grants one retry credit
    refreshEnvelope();
    // Apply/reset also move the current-correction banner.
    refreshCurrentCorrection();
  }

  function validateEnvelope(env) {
    if (!env || env.schema_version !== SUPPORTED_ENVELOPE_SCHEMA) {
      throw new Error('unsupported room-correction envelope');
    }
    if (!KNOWN_ENVELOPE_SCREENS[String(env.screen || '')]) {
      throw new Error('unknown room-correction screen');
    }
    validateProgress(env.progress);
    if (!Array.isArray(env.sections) || env.sections.length === 0) {
      throw new Error('room-correction sections missing');
    }
    validateRunDefaults(env.run_defaults);
    if (env.run_defaults.change_allowed !== (
      env.screen === 'idle' && env.state === 'idle'
    )) {
      throw new Error('room-correction Change authority is inconsistent');
    }
    var seen = {};
    env.sections.forEach(function (sectionId) {
      if (typeof sectionId !== 'string' || !sectionNodes[sectionId] || seen[sectionId]) {
        throw new Error('unknown or duplicate room-correction section');
      }
      seen[sectionId] = true;
    });
    if (env.next_action !== null) {
      var action = env.next_action;
      if (!action || typeof action.label !== 'string' || !action.label.trim() ||
          !KNOWN_ACTION_ENDPOINTS[String(action.endpoint || '')]) {
        throw new Error('unknown room-correction action');
      }
    }
    if (!Object.prototype.hasOwnProperty.call(env, 'blocker') ||
        !Object.prototype.hasOwnProperty.call(env, 'failure')) {
      throw new Error('room-correction failure blocks missing');
    }
    var blocker = validatePublicFailure(env.blocker);
    var failure = validatePublicFailure(env.failure);
    if (blocker && blocker.code !== 'speaker_setup_incomplete' &&
        blocker.code !== 'speaker_readiness_unavailable') {
      throw new Error('room-correction blocker code mismatch');
    }
    var hasBlockerSection = env.sections.indexOf('readiness-blocker') !== -1;
    if (hasBlockerSection !== !!blocker) {
      throw new Error('room-correction blocker section mismatch');
    }
    if (blocker && (env.screen !== 'idle' || env.next_action !== null)) {
      throw new Error('blocked room-correction entry offered an action');
    }
    if (String(env.state) === 'failed' && !failure) {
      throw new Error('room-correction failure/state mismatch');
    }
    if (failure && String(env.state) !== 'failed' &&
        (env.screen !== 'review' || failure.code !== 'measurement_evidence_unsafe')) {
      throw new Error('room-correction failure/screen mismatch');
    }
    if (failure && env.next_action !== null && !(
      String(env.state) === 'failed' && env.screen === 'result' &&
      env.next_action.endpoint === '/reset'
    )) {
      throw new Error('failed room-correction envelope offered an action');
    }
    return env;
  }

  function validateProgress(progress) {
    if (!progress || typeof progress !== 'object' ||
        !Number.isInteger(progress.position) || progress.position < 1 ||
        progress.position > WIZARD_PROGRESS_TOTAL ||
        !Number.isInteger(progress.total) ||
        progress.total !== WIZARD_PROGRESS_TOTAL ||
        !Array.isArray(progress.labels) ||
        progress.labels.length !== WIZARD_PROGRESS_TOTAL ||
        !progress.labels.every(function (label) {
          return typeof label === 'string' && !!label.trim();
        })) {
      throw new Error('invalid room-correction progress');
    }
    return progress;
  }

  function validateRunDefaults(block) {
    var summaryRemainder = (
      block && typeof block.summary_template === 'string'
        ? block.summary_template
        : ''
    );
    var requiredSummaryTokens = ['{positions_label}', '{target}'];
    var summaryTokensValid = requiredSummaryTokens.every(function (token) {
      if (summaryRemainder.split(token).length !== 2) return false;
      summaryRemainder = summaryRemainder.replace(token, '');
      return true;
    }) && summaryRemainder.indexOf('{') === -1 &&
      summaryRemainder.indexOf('}') === -1;
    if (!block || typeof block !== 'object' ||
        typeof block.summary !== 'string' || !block.summary.trim() ||
        typeof block.summary_template !== 'string' ||
        !block.summary_template.trim() ||
        !summaryTokensValid ||
        !Number.isInteger(block.total_positions) || block.total_positions <= 0 ||
        !block.target || typeof block.target.id !== 'string' ||
        typeof block.target.label !== 'string' || !block.target.label.trim() ||
        !block.strategy || typeof block.strategy.id !== 'string' ||
        typeof block.strategy.label !== 'string' || !block.strategy.label.trim() ||
        typeof block.repeat_main_position !== 'boolean' ||
        typeof block.repeat_disclosure !== 'string' ||
        (block.capture_transport !== 'relay' && block.capture_transport !== 'local') ||
        typeof block.change_allowed !== 'boolean') {
      throw new Error('invalid room-correction run defaults');
    }
    if (!selectSupportsValue(positionsSelect, block.total_positions) ||
        !selectSupportsValue(targetSelect, block.target.id) ||
        !selectSupportsValue(strategySelect, block.strategy.id)) {
      throw new Error('unsupported room-correction run defaults');
    }
    return block;
  }

  function selectSupportsValue(select, value) {
    if (!select || !select.options) return false;
    var expected = String(value);
    for (var i = 0; i < select.options.length; i++) {
      if (String(select.options[i].value) === expected) return true;
    }
    return false;
  }

  function renderRunDefaults(block) {
    if (!block || !runDefaultsSummary) return;
    currentRunDefaults = block;
    if (!block.change_allowed || !runDefaultsDirty) {
      positionsSelect.value = String(block.total_positions);
      targetSelect.value = String(block.target.id);
      strategySelect.value = String(block.strategy.id);
      if (positionsSelect.value !== String(block.total_positions) ||
          targetSelect.value !== String(block.target.id) ||
          strategySelect.value !== String(block.strategy.id)) {
        throw new Error('room-correction run choice is not supported by this page');
      }
      runDefaultsSummary.textContent = String(block.summary);
    }
    if (repeatMainPositionDisclosure) {
      repeatMainPositionDisclosure.textContent = String(block.repeat_disclosure);
      hideEl(repeatMainPositionDisclosure, !block.repeat_disclosure);
    }
    setRunTransportLocked(!block.change_allowed);
  }

  function validatePublicFailure(block) {
    if (block === null) return null;
    if (!block || typeof block !== 'object' ||
        typeof block.code !== 'string' || !KNOWN_FAILURES[block.code] ||
        typeof block.text !== 'string' || !block.text.trim() ||
        typeof block.retryable !== 'boolean') {
      throw new Error('invalid room-correction failure');
    }
    var expected = KNOWN_FAILURES[block.code];
    if (block.text !== expected.text || block.retryable !== expected.retryable) {
      throw new Error('room-correction failure presentation mismatch');
    }
    var action = block.recovery_action;
    if (action !== null) {
      if (!action || typeof action.label !== 'string' || !action.label.trim() ||
          typeof action.href !== 'string' || !action.href.startsWith('/') ||
          action.href.startsWith('//') || action.href.indexOf('\\') !== -1 ||
          /[\u0000-\u001f]/.test(action.href)) {
        throw new Error('invalid room-correction recovery action');
      }
    }
    return block;
  }

  function renderReadinessBlocker(blocker) {
    if (!readinessBlockerMessage || !readinessBlockerAction) return;
    readinessBlockerMessage.textContent = blocker ? String(blocker.text) : '';
    var action = blocker && blocker.recovery_action;
    if (action) {
      readinessBlockerAction.textContent = String(action.label);
      readinessBlockerAction.href = String(action.href);
      readinessBlockerAction.classList.remove('hidden');
    } else {
      readinessBlockerAction.textContent = '';
      readinessBlockerAction.removeAttribute('href');
      readinessBlockerAction.classList.add('hidden');
    }
  }

  function renderEnvelopeFailure() {
    Object.keys(sectionNodes).forEach(function (sectionId) {
      sectionNodes[sectionId].classList.add('hidden');
    });
    renderPrimaryAction(null);
    if (wizardChrome) wizardChrome.classList.remove('hidden');
    if (wizardVerdict) {
      wizardVerdict.textContent =
        'The speaker could not refresh this step. Wait a moment and try again.';
    }
    renderNudges([]);
    renderReadinessBlocker(null);
  }

  // The server supplies both membership and order. This renderer knows only
  // the closed section vocabulary and moves those DOM roots into the exact
  // order received; it contains no screen-to-section policy.
  function renderSections(sections, curves) {
    Object.keys(sectionNodes).forEach(function (sectionId) {
      sectionNodes[sectionId].classList.add('hidden');
    });
    sections.forEach(function (sectionId) {
      var node = sectionNodes[sectionId];
      envelopeSections.appendChild(node);
      node.classList.remove('hidden');
    });

    // Review and result use one neutral evidence subtree. The selected
    // server section owns its host; no two section names alias one DOM root.
    if (sections.indexOf('measurement-review') !== -1) {
      measurementReview.appendChild(resultSection);
      measurementReview.appendChild(resetBtn);
    } else if (sections.indexOf('result-proof') !== -1) {
      resultProof.appendChild(resultSection);
      resultProof.appendChild(resetBtn);
    } else if (sections.indexOf('apply-status') !== -1) {
      sectionNodes['apply-status'].appendChild(resetBtn);
    }
    var haveResult = !!(curves && curves.measured);
    hideEl(resultSection, !haveResult);
    // Whole-section visibility just settled — re-derive whether the banner's
    // own reset control should defer to the wizard's.
    syncCurrentCorrectionReset();
  }

  // The envelope router. Renders the full wizard chrome from one envelope
  // and, on review/result, draws its server-smoothed curves into the shared
  // result canvas (server data verbatim; no client smoothing or /status
  // presentation path).
  function renderEnvelope(env) {
    if (!env || !wizardChrome) return;
    wizardChrome.classList.remove('hidden');
    if (env.blocker || env.failure) pendingHomeownerFailure = null;
    if (wizardVerdict) {
      wizardVerdict.textContent = String(
        env.failure && env.failure.text || env.verdict_text || ''
      );
    }
    renderNudges(env.nudges);
    renderPrimaryAction(env.next_action);
    renderProgress(env.progress);
    renderSections(env.sections, env.curves || {});
    renderRunDefaults(env.run_defaults);
    renderReadinessBlocker(env.blocker);
    if (pendingHomeownerFailure) {
      wizardVerdict.textContent = String(pendingHomeownerFailure.text);
      if (!pendingHomeownerFailure.retryable) renderPrimaryAction(null);
    }
    // On the review/result screens the envelope carries the honest,
    // server-smoothed curves + Pi-classified two-tone fill. Draw them into
    // the shared canvas via drawEnvelopeCurves so the "what your room is
    // doing" view is the server's numbers, not a client recomputation.
    if (env.screen === 'review' || env.screen === 'result') {
      drawEnvelopeCurves(env);
    }
    renderTuning(env.tuning_llm);
  }

  // P6: render the tuning-assistant affordance from the envelope's
  // tuning_llm block ({offered, available, provider, model?, nudge?}).
  // Whole-section visibility is owned only by renderSections(). This function
  // fills the section's internals: when offered but the household has no
  // OpenAI key, show only the nudge; when available, show the two per-tap
  // actions. A missing block clears both internal affordances.
  function renderTuning(block) {
    if (!tuningPanel) return;
    if (!block || !block.offered) {
      tuningActions.classList.add('hidden');
      tuningNudge.classList.add('hidden');
      return;
    }
    if (block.available) {
      tuningNudge.classList.add('hidden');
      tuningActions.classList.remove('hidden');
    } else {
      tuningActions.classList.add('hidden');
      tuningNudge.textContent = String(block.nudge || 'Tuning assistant unavailable.');
      tuningNudge.classList.remove('hidden');
    }
  }

  // Set the tuning status line (a short "thinking…" / error string).
  function setTuningStatus(text) {
    if (!tuningStatus) return;
    if (!text) { tuningStatus.classList.add('hidden'); tuningStatus.textContent = ''; return; }
    tuningStatus.textContent = String(text);
    tuningStatus.classList.remove('hidden');
  }

  function setTuningBusy(busy) {
    if (tuningInterpretBtn) tuningInterpretBtn.disabled = busy;
    if (tuningProposeBtn) tuningProposeBtn.disabled = busy;
  }

  // Render the plain-language explanation panel + the provenance note.
  // Untrusted model text reaches the DOM only via textContent.
  function renderTuningExplanation(payload) {
    if (tuningExplanation) {
      var text = String((payload && payload.explanation) || '');
      tuningExplanation.textContent = text;
      hideEl(tuningExplanation, !text);
    }
    if (tuningProvenance) {
      var prov = payload && payload.provenance;
      if (prov && prov.ok === false && prov.unverified && prov.unverified.length) {
        // The assistant stated a number that isn't in the measurement — flag
        // it so the reader doesn't trust an authored figure as a fact.
        tuningProvenance.textContent =
          'Note: some figures above were not in the measurement and may be '
          + 'the assistant guessing — trust the plotted curve, not those numbers.';
        tuningProvenance.classList.remove('hidden');
      } else {
        tuningProvenance.classList.add('hidden');
        tuningProvenance.textContent = '';
      }
    }
  }

  function setTuningError(e) {
    setTuningStatus(safeErrorMessage(
      e,
      'The tuning assistant could not continue. Try again.'
    ));
  }

  async function onTuningInterpret() {
    setTuningBusy(true);
    setTuningStatus('Reading your measurement…');
    if (tuningProposals) tuningProposals.innerHTML = '';
    try {
      var resp = await fetch(endpoint('interpret'), {
        method: 'POST', headers: jsonHeaders(), body: '{}',
      });
      if (!resp.ok) {
        throw await responseError(
          resp,
          'The tuning assistant could not continue. Try again.'
        );
      }
      var payload = await resp.json();
      renderTuningExplanation(payload);
      setTuningStatus('');
    } catch (e) {
      setTuningError(e);
    } finally {
      setTuningBusy(false);
    }
  }

  async function onTuningPropose() {
    setTuningBusy(true);
    setTuningStatus('Thinking about a tweak…');
    try {
      var resp = await fetch(endpoint('propose'), {
        method: 'POST', headers: jsonHeaders(), body: '{}',
      });
      if (!resp.ok) {
        throw await responseError(
          resp,
          'The tuning assistant could not continue. Try again.'
        );
      }
      var payload = await resp.json();
      renderTuningExplanation(payload);
      renderTuningProposals(payload.proposals || []);
      setTuningStatus('');
    } catch (e) {
      setTuningError(e);
    } finally {
      setTuningBusy(false);
    }
  }

  // Render each proposal as its own card. A room-correction proposal that
  // passed the deterministic simulation shows an Apply button (which
  // confirms, then POSTs /propose/apply — the server re-simulates before
  // applying). A rejected one shows why. A preference/target move is
  // phrased as a question (taste, not a correction claim).
  function renderTuningProposals(proposals) {
    if (!tuningProposals) return;
    tuningProposals.innerHTML = '';
    if (!proposals.length) return;
    proposals.forEach(function (p) {
      if (!p || typeof p !== 'object') return;
      if (p.kind === 'room_correction') {
        tuningProposals.appendChild(buildCorrectionProposalCard(p));
      } else if (p.kind === 'preference_question') {
        tuningProposals.appendChild(buildTargetProposalCard(p));
      }
    });
  }

  function buildCorrectionProposalCard(p) {
    var card = document.createElement('div');
    card.className = 'tuning-proposal' + (p.applicable ? '' : ' tuning-proposal--rejected');
    if (p.rationale) {
      var rat = document.createElement('p');
      rat.className = 'tuning-proposal-rationale';
      rat.textContent = String(p.rationale);
      card.appendChild(rat);
    }
    var filters = document.createElement('div');
    filters.className = 'tuning-proposal-filters';
    filters.textContent = describeFilters(p.correction_peqs || []);
    card.appendChild(filters);
    // Predicted improvement from the deterministic simulation (server number).
    var acc = p.simulation && p.simulation.acceptance;
    if (acc && typeof acc.overall_rms_delta_db === 'number') {
      var detail = document.createElement('p');
      detail.className = 'tuning-proposal-detail';
      detail.textContent = 'Simulated: ' + acc.verdict
        + ' (predicted ' + acc.overall_rms_delta_db.toFixed(1) + ' dB RMS change vs target).';
      card.appendChild(detail);
    }
    if (p.applicable) {
      var applyBtn = document.createElement('button');
      applyBtn.type = 'button';
      applyBtn.className = 'btn btn--primary';
      applyBtn.textContent = 'Apply this correction';
      applyBtn.addEventListener('click', function () { applyCorrectionProposal(p, applyBtn); });
      card.appendChild(applyBtn);
    } else {
      var why = document.createElement('p');
      why.className = 'tuning-proposal-detail';
      var issues = (p.simulation && p.simulation.issues) || [];
      why.textContent = issues.length
        ? ('Not offered — ' + issues.map(function (i) { return i.message || i.code; }).join('; '))
        : 'Not offered — the simulation did not accept this.';
      card.appendChild(why);
    }
    return card;
  }

  function buildTargetProposalCard(p) {
    var card = document.createElement('div');
    card.className = 'tuning-proposal';
    if (p.rationale) {
      var rat = document.createElement('p');
      rat.className = 'tuning-proposal-rationale';
      rat.textContent = String(p.rationale);
      card.appendChild(rat);
    }
    // Suggestion-only, honestly: there is no apply path for a target move.
    // Preference is subjective — phrase it as a question and tell the
    // household where to change the target themselves. This is plain text,
    // NOT a link to #target-select: that picker lives inside
    // #measurement-options, which the router hides in relay (phone-mic)
    // mode, so an anchor would silently scroll nowhere on the review
    // screen. The instruction stands on its own.
    var q = document.createElement('p');
    q.className = 'tuning-question';
    var dest = p.target_id ? ('a "' + p.target_id + '" target') : ('a warmth of ' + p.warmth);
    q.textContent = 'This would move you toward ' + dest
      + ' — worth a listen? Pick it under Target curve when you next measure.';
    card.appendChild(q);
    return card;
  }

  function describeFilters(peqs) {
    return peqs.map(function (f) {
      var g = Number(f.gain_db);
      var sign = g >= 0 ? '+' : '';
      return Math.round(Number(f.freq_hz)) + ' Hz, Q ' + Number(f.q).toFixed(1)
        + ', ' + sign + g.toFixed(1) + ' dB';
    }).join('  •  ');
  }

  async function applyCorrectionProposal(p, btn) {
    var ok = await jtsConfirm(
      'Apply this correction to your speaker? You can undo it with Reset.',
      {}
    );
    if (!ok) return;
    if (btn) btn.disabled = true;
    setTuningStatus('Applying…');
    try {
      var resp = await fetch(endpoint('propose/apply'), {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({ confirm: true, correction_peqs: p.correction_peqs }),
      });
      if (!resp.ok) throw await responseError(resp, GENERIC_STEP_FAILURE);
      var payload = await resp.json();
      if (payload.applied) {
        setTuningStatus('Applied. Measure once more to verify it worked.');
        pollState();
        refreshEnvelope();
      } else {
        var failure = null;
        try { failure = validatePublicFailure(payload.failure || null); }
        catch (_e) {}
        setTuningStatus(
          failure ? failure.text : 'That suggestion was not applied.'
        );
        if (btn) btn.disabled = false;
        pollState();
        refreshEnvelope();
      }
    } catch (e) {
      setTuningStatus(safeErrorMessage(e, GENERIC_STEP_FAILURE));
      if (btn) btn.disabled = false;
    }
  }

  // Draw the envelope's already-smoothed curves and server-classified fill.
  // The canvas is deliberately a dumb renderer: no /status result payload,
  // client smoothing, confidence policy, or verdict derivation enters here.
  function drawEnvelopeCurves(env) {
    var curves = env.curves || {};
    if (!curves.measured) return;   // nothing to draw before the first sweep
    lastChartEnvelope = env;
    // result-section visibility is owned by renderSections (gated on
    // these same envelope curves), which renderEnvelope ran just before
    // this — the canvas is already laid out; no second un-hide here.
    void canvas.offsetWidth;   // force layout so getBoundingClientRect is real
    if (!drawChart(curves, env.fill_segments || [])) {
      // Mobile Safari can report 0×0 for one frame after revealing the
      // result section. Retry once, bounded, after layout catches up.
      window.requestAnimationFrame(function () {
        if (lastChartEnvelope === env) {
          drawChart(curves, env.fill_segments || []);
        }
      });
    }
  }

  // Fetch the screen envelope once and render it. Increments a probe-visible
  // counter so the harness can prove the fetch-once-per-state-change
  // discipline. A failed or unsupported envelope keeps the /status
  // mechanism path alive but clears every user-facing section and action.
  async function fetchPresentationJson(path, label) {
    var controller = new AbortController();
    var fetchTimeout = setTimeout(function () {
      controller.abort();
    }, envelopeFetchTimeoutMs);
    try {
      var resp = await fetch(endpoint(path), {
        cache: 'no-store', signal: controller.signal,
      });
      if (!resp.ok) throw new Error(label + ' ' + resp.status);
      return await resp.json();
    } finally {
      clearTimeout(fetchTimeout);
    }
  }

  async function refreshEnvelope() {
    envelopePollingEnabled = true;
    if (envelopeRequestInFlight) {
      envelopeRefreshQueued = true;
      return envelopeRequestInFlight;
    }
    if (envelopeTimer) { clearTimeout(envelopeTimer); envelopeTimer = null; }
    envelopeFetchCount += 1;
    envelopeRequestInFlight = (async function () {
      try {
        var envelopePath = 'envelope?capture_transport=' +
          (relayMode ? 'relay' : 'local');
        var env = validateEnvelope(
          await fetchPresentationJson(envelopePath, 'envelope')
        );
        envelopeRetryArmed = true;   // success re-arms one retry credit
        renderEnvelope(env);
        lastRenderedEnvelopeScreen = env.screen;
        lastRenderedEnvelopeState = env.state;
        if (env.screen === 'idle') {
          lastRenderedReadinessSignature = JSON.stringify(env.blocker);
        }
        scheduleEnvelopePoll(env.screen);
        return true;
      } catch (e) {
        // Fail closed immediately: no stale action or section policy survives
        // an unsupported/malformed envelope. Spend one retry credit so a
        // transient deploy restart can self-heal without creating a loop.
        console.warn('envelope refresh failed', e);
        renderEnvelopeFailure();
        var shouldMonitorIdle = lastRenderedEnvelopeScreen === 'idle' ||
          lastObservedStatusState === 'idle';
        // The last presentation was retired above. Mark it stale so a later
        // lightweight readiness read forces one full policy refresh even when
        // readiness itself did not change during the outage.
        lastRenderedEnvelopeScreen = null;
        if (envelopeRetryArmed && envelopePollingEnabled) {
          envelopeRetryArmed = false;
          envelopeTimer = setTimeout(refreshEnvelope, 1500);
        } else if (shouldMonitorIdle && envelopePollingEnabled) {
          // Two consecutive failures spend the fast retry. Resume through the
          // cheap idle probe instead of repeatedly scanning report bundles.
          envelopeTimer = setTimeout(refreshIdleEntry, idleEnvelopeRefreshMs);
        }
        return false;
      }
    })();
    var succeeded = false;
    try {
      succeeded = await envelopeRequestInFlight;
    } finally {
      envelopeRequestInFlight = null;
      var queued = envelopeRefreshQueued;
      envelopeRefreshQueued = false;
      // A newer trigger that arrived during a successful read gets one
      // coalesced follow-up. After failure, the bounded backoff above owns
      // recovery; never turn queued status ticks into immediate retries.
      if (queued && succeeded) refreshEnvelope();
    }
  }

  // Idle readiness and the current-correction banner can change outside the
  // Room session. Refresh those two cheap facts together, and fetch the full
  // envelope only when its readiness policy or logical screen is stale. This
  // keeps report discovery off the steady-state idle cadence.
  async function refreshIdleEntry() {
    if (envelopeTimer) { clearTimeout(envelopeTimer); envelopeTimer = null; }
    var keepPolling = lastRenderedEnvelopeScreen === 'idle' ||
      lastObservedStatusState === 'idle';
    try {
      var entry = await fetchPresentationJson(
        'entry-status', 'entry status'
      );
      if (!entry || typeof entry !== 'object' ||
          !KNOWN_ENVELOPE_SCREENS[String(entry.screen || '')] ||
          typeof entry.state !== 'string' || !entry.state.trim() ||
          !Object.prototype.hasOwnProperty.call(entry, 'readiness_blocker')) {
        throw new Error('invalid room-correction entry status');
      }
      var blocker = validatePublicFailure(entry.readiness_blocker);
      renderCurrentCorrection(entry.current_correction_presentation);
      var signature = JSON.stringify(blocker);
      if (entry.screen !== lastRenderedEnvelopeScreen ||
          entry.state !== lastRenderedEnvelopeState ||
          signature !== lastRenderedReadinessSignature) {
        envelopeRetryArmed = true;
        await refreshEnvelope();
        return;
      }
    } catch (e) {
      console.warn('entry status refresh failed', e);
      renderCurrentCorrectionUnavailable();
      renderEnvelopeFailure();
      lastRenderedEnvelopeScreen = null;
      lastRenderedEnvelopeState = null;
    }
    if (envelopePollingEnabled &&
        (keepPolling || lastObservedStatusState === 'idle')) {
      envelopeTimer = setTimeout(refreshIdleEntry, idleEnvelopeRefreshMs);
    }
  }

  // Poll discipline (P3b-1 reviewer advisory): active capture is hot-polled;
  // idle gets a lightweight readiness/banner refresh because both are
  // external to Room state. Other static screens remain state-edge triggered.
  function scheduleEnvelopePoll(screen) {
    if (envelopeTimer) { clearTimeout(envelopeTimer); envelopeTimer = null; }
    if (!envelopePollingEnabled) return;
    if (ACTIVE_ENVELOPE_SCREENS[screen]) {
      envelopeTimer = setTimeout(refreshEnvelope, 900);
    } else if (screen === 'idle') {
      envelopeTimer = setTimeout(refreshIdleEntry, idleEnvelopeRefreshMs);
    }
  }

  // Edge-trigger: called from pollState. A /status state transition is the
  // one moment a static screen's envelope can change, so refresh exactly
  // then — not on every 500 ms /status tick. The one exception is the
  // "level" screen, which the server derives from the autolevel sub-state
  // while the session stays IDLE; so also refresh when the autolevel status
  // changes under an unchanged state. `autolevelStatus` may be undefined
  // (no ramp) — treated as a stable value so it only fires on real changes.
  var lastAutolevelStatus = null;
  function maybeRefreshEnvelopeOnStateChange(state, autolevelStatus) {
    var al = autolevelStatus || 'idle';
    var stateChanged = state !== lastObservedStatusState;
    if (stateChanged || al !== lastAutolevelStatus) {
      if (stateChanged) pendingHomeownerFailure = null;
      lastObservedStatusState = state;
      lastAutolevelStatus = al;
      envelopeRetryArmed = true;   // a fresh trigger grants one retry credit
      refreshEnvelope();
    }
  }

  function numberOrNull(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
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
      console.warn('measurement report list failed', e);
      sessionHistory.textContent = 'Measurement reports could not be loaded. Try again.';
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
      envelopeRetryArmed = true;
      await refreshEnvelope();
    } catch (e) {
      sessionReport.className = 'session-report blocked';
      sessionReport.textContent = safeErrorMessage(e, GENERIC_STEP_FAILURE);
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
        console.warn('measurement report request failed', {
          status: resp.status,
          diagnostic: payload && payload.error,
        });
        throw homeownerError(
          null,
          'That measurement report could not be loaded. Try again.'
        );
      }
      sessionReport.dataset.sessionId = sessionId;
      renderSessionReport(payload);
    } catch (e) {
      sessionReport.className = 'session-report blocked';
      sessionReport.textContent = safeErrorMessage(
        e,
        'That measurement report could not be loaded. Try again.'
      );
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

  function drawChart(curves, fillSegments) {
    curves = curves || {};
    var measured = curves.measured || null;
    var target = curves.target || null;
    var predicted = curves.predicted || null;
    var verify = curves.verify || null;
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

    // Honest before/after fill: shade the area between the
    // pre-correction measured curve and the post-correction verify
    // curve, green where the correction moved toward the target
    // (improved), amber where it moved away (regressed). The segment
    // classification + grid indices come from the Pi envelope; this only
    // renders them against the exact server-smoothed curves.
    function drawBeforeAfterFill(segments, beforeCurve, afterCurve) {
      if (
        !segments || !segments.length ||
        !beforeCurve || !beforeCurve.freqs_hz || !beforeCurve.magnitude_db ||
        !afterCurve || !afterCurve.magnitude_db ||
        beforeCurve.freqs_hz.length !== beforeCurve.magnitude_db.length ||
        beforeCurve.freqs_hz.length !== afterCurve.magnitude_db.length
      ) return;
      var freqs = beforeCurve.freqs_hz;
      var beforeDb = beforeCurve.magnitude_db;
      var afterDb = afterCurve.magnitude_db;
      var n = freqs.length;
      segments.forEach(function (seg) {
        var lo = Number(seg.i_lo);
        var hi = Number(seg.i_hi);
        if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi < lo) return;
        lo = Math.max(0, lo);
        hi = Math.min(n - 1, hi);
        c.fillStyle = seg.tone === 'improved'
          ? 'rgba(29, 185, 84, 0.22)'   // green — moved toward target
          : 'rgba(214, 130, 0, 0.22)';  // amber — moved away
        c.beginPath();
        var first = true;
        for (var i = lo; i <= hi; i++) {
          var x = fx(freqs[i]);
          var y = fy(afterDb[i]);
          if (first) { c.moveTo(x, y); first = false; }
          else c.lineTo(x, y);
        }
        for (var j = hi; j >= lo; j--) {
          c.lineTo(fx(freqs[j]), fy(beforeDb[j]));
        }
        c.closePath();
        c.fill();
      });
    }

    // Measured before/after fill (green=improved, amber=regressed),
    // under the curves so both edges stay visible. The improved/
    // regressed verdict + grid indices are Pi-computed; we only fill
    // between the server-smoothed before/after curves within each
    // server-classified segment. Render only when a verify exists.
    if (verify && fillSegments && fillSegments.length) {
      drawBeforeAfterFill(fillSegments, measured, verify);
    }

    drawCurve(target, '#888', true, 2);
    drawCurve(measured, '#d44', false, 2);
    drawCurve(predicted, '#1db954', false, 2);
    if (chartShowFilter && chartShowFilter.checked) {
      drawCurve(
        filterEffectCurve(measured, predicted),
        '#2b7bb9',
        true,
        1.6,
      );
    }
    // Phase 2: post-correction verify pass overlay (purple dashed).
    drawCurve(verify, '#a050d0', true, 2);
    return true;
  }

  function redrawLatestChart() {
    if (lastChartEnvelope) drawEnvelopeCurves(lastChartEnvelope);
  }

  // -- Network --

  var GENERIC_STEP_FAILURE =
    'The speaker could not continue this step. Try again.';

  function homeownerError(failure, fallback) {
    var err = new Error(
      failure && failure.text
        ? String(failure.text)
        : String(fallback || GENERIC_STEP_FAILURE)
    );
    err.homeownerSafe = true;
    err.failure = failure || null;
    return err;
  }

  function safeErrorMessage(error, fallback) {
    return error && error.homeownerSafe
      ? String(error.message)
      : String(fallback || GENERIC_STEP_FAILURE);
  }

  function showHomeownerFailure(error) {
    var failure = error && error.failure;
    pendingHomeownerFailure = failure || {
      text: safeErrorMessage(error, GENERIC_STEP_FAILURE),
      retryable: true,
    };
    if (wizardVerdict) wizardVerdict.textContent = pendingHomeownerFailure.text;
    if (!pendingHomeownerFailure.retryable) renderPrimaryAction(null);
  }

  async function responseError(resp, fallback) {
    var text = '';
    var payload = null;
    try {
      text = await resp.text();
      payload = JSON.parse(text);
    } catch (_e) {}
    var failure = null;
    try {
      failure = validatePublicFailure(payload && payload.failure || null);
    } catch (_e) {}
    console.warn('room-correction request failed', {
      status: resp.status,
      failureCode: failure && failure.code,
    });
    return homeownerError(failure, fallback);
  }

  async function postJson(path, body) {
    var url = endpoint(path);
    var resp;
    try {
      resp = await fetch(url, {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body || {})
      });
    } catch (e) {
      console.warn('room-correction request unavailable', {url: url, error: e});
      throw homeownerError(null, GENERIC_STEP_FAILURE);
    }
    if (!resp.ok) {
      throw await responseError(resp, GENERIC_STEP_FAILURE);
    }
    return await resp.json();
  }

  async function fetchStatus() {
    var resp;
    try {
      resp = await fetch(endpoint('status'), {cache: 'no-store'});
    } catch (e) {
      console.warn('room-correction status unavailable', e);
      throw homeownerError(null, GENERIC_STEP_FAILURE);
    }
    if (!resp.ok) throw await responseError(resp, GENERIC_STEP_FAILURE);
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

  function finishNoiseCapture(error) {
    if (noiseCaptureTimeout) clearTimeout(noiseCaptureTimeout);
    noiseCaptureTimeout = null;
    var resolve = noiseCaptureResolve;
    var reject = noiseCaptureReject;
    noiseCaptureCompletion = null;
    noiseCaptureResolve = null;
    noiseCaptureReject = null;
    if (error) {
      if (reject) reject(error);
    } else if (resolve) {
      resolve();
    }
  }

  function capturePreSweepNoise() {
    if (!workletNode) {
      return Promise.reject(new Error('local microphone capture is not ready'));
    }
    if (noiseCaptureCompletion) return noiseCaptureCompletion;
    noiseCaptureCompletion = new Promise(function (resolve, reject) {
      noiseCaptureResolve = resolve;
      noiseCaptureReject = reject;
    });
    captureMode = 'noise';
    setStateBadge('needs_noise_capture', 'recording room noise…');
    workletNode.port.postMessage('startCapture');
    setTimeout(function () {
      if (captureMode === 'noise' && workletNode) {
        workletNode.port.postMessage('stopCapture');
      }
    }, 700);
    noiseCaptureTimeout = setTimeout(function () {
      captureMode = 'discard';
      if (workletNode) workletNode.port.postMessage('stopCapture');
      finishNoiseCapture(new Error('the room-noise capture did not finish'));
    }, 5000);
    return noiseCaptureCompletion;
  }

  function resetMeasurementUiForStart() {
    resetBtn.classList.add('hidden');
    resultSection.classList.add('hidden');
    positionPrompt.classList.add('hidden');
    qualityBanner.className = 'quality-banner hidden';
    qualityBanner.innerHTML = '';
    lastChartEnvelope = null;
    inVerifyMode = false;
    setStateBadge('preparing', 'pausing music…');
  }

  function measurementStartPayload() {
    var totalPositions = Number(positionsSelect.value);
    var targetChoice = String(targetSelect.value || '');
    var strategyChoice = String(strategySelect.value || '');
    if (!Number.isInteger(totalPositions) || totalPositions <= 0 ||
        !targetChoice || !strategyChoice) {
      throw new Error('measurement choices are unavailable; refresh the page');
    }
    return {
      total_positions: totalPositions,
      target_choice: targetChoice,
      strategy_choice: strategyChoice,
      noise_floor_db: relayMode ? null : lastNoiseFloorDb,
      calibration_id: selectedCalibrationId,
      input_device: relayMode ? null : selectedInputDevice,
      capture_transport: relayMode ? 'relay' : 'local'
    };
  }

  async function startRelayLevelMatch() {
    setRelayStatus('Creating safe level-check link…', 'idle');
    renderRelayCapture({status: 'starting'});
    try {
      var resp = await postJson('relay/level-match', {});
      renderRelayCapture(resp.relay);
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      setRelayStatus(safeErrorMessage(e, GENERIC_STEP_FAILURE), 'bad');
    }
  }

  async function startRelayCaptureForCurrentPosition() {
    setRelayStatus('Creating phone capture link…', 'idle');
    renderRelayCapture({status: 'starting'});
    try {
      var resp = await postJson('relay/capture', {});
      renderRelayCapture(resp.relay);
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      setRelayStatus(safeErrorMessage(e, GENERIC_STEP_FAILURE), 'bad');
    }
  }

  async function startRelayVerify() {
    setRelayStatus('Creating verification capture link…', 'idle');
    renderRelayCapture({status: 'starting'});
    try {
      var resp = await postJson('relay/verify', {});
      renderRelayCapture(resp.relay);
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      setRelayStatus(safeErrorMessage(e, GENERIC_STEP_FAILURE), 'bad');
    }
  }

  async function startRelayMeasurement() {
    resetMeasurementUiForStart();
    try {
      var resp = await postJson('start', measurementStartPayload());
      sessionId = resp.session_id;
      setRunTransportLocked(true);
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      setRelayStatus(safeErrorMessage(e, GENERIC_STEP_FAILURE), 'bad');
      return;
    }
    await startRelayLevelMatch();
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
    resetMeasurementUiForStart();
    try {
      var resp = await postJson('start', measurementStartPayload());
      sessionId = resp.session_id;
      localRunOwnedByThisTab = true;
      localRunOwnerSessionId = sessionId;
      rememberLocalCapture(null);
      setRunTransportLocked(true);
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      return;
    }
    pollState();
  }

  async function continueToNextPosition() {
    // The envelope-owned wizard button is disabled by its dispatcher before
    // this runs, preventing a second /next-position double-tap.
    positionPrompt.classList.add('hidden');
    setStateBadge('preparing', 'pausing music…');
    if (!relayMode && !(await ensureLocalCaptureReady())) {
      throw new Error('local microphone capture is not ready');
    }
    try {
      await postJson('next-position', {});
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      // pollState will reapply the button policy on next tick —
      // user can retry from the new state.
      return;
    }
    if (relayMode) {
      await startRelayCaptureForCurrentPosition();
      return;
    }
    await capturePreSweepNoise();
    pollState();
  }

  async function repeatMainSeat() {
    setStateBadge('preparing', 'preparing repeat sweep…');
    if (!relayMode && !(await ensureLocalCaptureReady())) {
      throw new Error('local microphone capture is not ready');
    }
    captureMode = 'repeat';
    if (workletNode) workletNode.port.postMessage('startCapture');
    try {
      await postJson('repeat-position', {});
    } catch (e) {
      captureMode = 'discard';
      if (workletNode) workletNode.port.postMessage('stopCapture');
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      return;
    }
    pollState();
  }

  // Keep the local-browser UMIK path on the same Room-owned acoustic
  // window as the relay path. The 2026-07-15 JTS3 smoke showed that even
  // Room's initial 3 dB reserve could let the following ESS clip: its RMS
  // rose 3.24 dB above the locked tone and its peak reached full scale.
  // Noise is measured and reported for the downstream SNR gates, but it
  // must not raise this bounded level target.
  var ROOM_LEVEL_WINDOW_LOW_DBFS = -26;
  var ROOM_LEVEL_WINDOW_HIGH_DBFS = -18;

  function computeTargetBand(_noiseFloorDb) {
    return {
      low: ROOM_LEVEL_WINDOW_LOW_DBFS,
      high: ROOM_LEVEL_WINDOW_HIGH_DBFS,
    };
  }

  function autolevelAutoLockEligible(
    averageDb, targetBand, noiseFloorDb, trustMarginDb
  ) {
    return Number.isFinite(averageDb) &&
      Number.isFinite(noiseFloorDb) &&
      Number.isFinite(trustMarginDb) &&
      averageDb >= targetBand.low &&
      averageDb <= targetBand.high &&
      averageDb >= noiseFloorDb + trustMarginDb;
  }

  async function startAutolevel() {
    autolevelStatus.classList.remove('hidden');
    autolevelLockBtn.classList.remove('hidden');
    autolevelCancelBtn.classList.remove('hidden');
    autolevelRmsBuffer = [];

    // Step 1: measure ambient noise floor for ~500 ms BEFORE the
    // tone starts. This gives us a real number for "what counts as
    // quiet in this room right now", which we then use to pick a
    // target readout and downstream capture-quality evidence. The lock
    // window itself is fixed above so local and relay captures reserve the
    // same ESS headroom.
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
      // No measured ambient means no automatic-lock authority. Keep the
      // visible manual Lock/cancel path, but never invent a quiet floor that
      // could let ordinary room sound impersonate the calibration tone.
      noiseFloorDb = null;
    }
    lastNoiseFloorDb = noiseFloorDb;
    var targetBand = computeTargetBand(noiseFloorDb);
    autolevelDetail.textContent = Number.isFinite(noiseFloorDb)
      ? 'Noise floor ' + noiseFloorDb.toFixed(0) + ' dBFS — target ' +
        targetBand.low.toFixed(0) + ' to ' + targetBand.high.toFixed(0) +
        ' dBFS. Tap Lock now if the tone sounds like a comfortable measurement level.'
      : 'Room noise could not be measured, so automatic lock is off. ' +
        'Tap Lock now only after the tone starts at a comfortable measurement level, ' +
        'or cancel and retry.';

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

    // Watch the latest mic RMS at 50 ms granularity after the server confirms
    // the bounded tone/ramp has started. Automatic lock requires both the
    // fixed Room headroom window and the shared ambient trust margin; a noisy
    // room that cannot satisfy both keeps the manual/retry path instead of
    // mistaking ambient sound for the calibration tone.
    var watcher = null;
    var watchAutolevelRms = function () {
      if (lockSent) return;
      var db = latestMicRmsDb;
      if (db <= -100) return;
      autolevelRmsBuffer.push(db);
      if (autolevelRmsBuffer.length > 5) autolevelRmsBuffer.shift();
      var sum = 0;
      for (var i = 0; i < autolevelRmsBuffer.length; i++) sum += autolevelRmsBuffer[i];
      var avg = sum / autolevelRmsBuffer.length;
      if (autolevelRmsBuffer.length >= 3 &&
          autolevelAutoLockEligible(
            avg, targetBand, noiseFloorDb, autolevelTrustMarginDb
          )) {
        sendLock('mic ' + avg.toFixed(1) + ' dBFS in band ' +
          targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0));
      }
    };

    try {
      await postJson('autolevel/start', {});
    } catch (e) {
      clearInterval(watcher);
      autolevelLockBtn.onclick = prevLockHandler;
      autolevelCancelBtn.onclick = prevCancelHandler;
      autolevelLine.textContent = safeErrorMessage(e, GENERIC_STEP_FAILURE);
      autolevelLockBtn.classList.add('hidden');
      autolevelCancelBtn.classList.add('hidden');
      return;
    }
    watcher = setInterval(watchAutolevelRms, 50);

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
            'Level check stopped at ' + capStr + ' dB — the safe software maximum.';
          autolevelDetail.textContent =
            'The mic read ' + latestMicRmsDb.toFixed(0) + ' dBFS (target ' +
            targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0) + ' dBFS), ' +
            'so no measurement level was locked. Raise the external amplifier a little, ' +
            'then retry the level check.';
        } else if (al.status === 'cancelled') {
          autolevelLine.textContent = 'Auto-level cancelled — speaker volume restored.';
          autolevelDetail.textContent = '';
        } else if (al.status === 'error') {
          autolevelLine.textContent = 'Auto-level error.';
          console.warn('autolevel failed', al.error || '');
          autolevelDetail.textContent = 'The level check stopped safely. Try again.';
        }
        return true;
      } catch (e) {
        autolevelLine.textContent = safeErrorMessage(e, GENERIC_STEP_FAILURE);
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
  }

  async function cancelAutolevel() {
    try {
      await postJson('autolevel/cancel', {});
    } catch (e) {
      autolevelLine.textContent = safeErrorMessage(e, GENERIC_STEP_FAILURE);
    }
  }

  async function startVerify(triggerBtn) {
    if (triggerBtn) triggerBtn.disabled = true;
    if (!relayMode && !(await ensureLocalCaptureReady())) {
      if (triggerBtn) triggerBtn.disabled = false;
      throw new Error('local microphone capture is not ready');
    }
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
      showHomeownerFailure(e);
      if (triggerBtn) triggerBtn.disabled = false;
      inVerifyMode = false;
      return;
    }
    if (triggerBtn) triggerBtn.disabled = false;
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
    resetBtn.classList.add('hidden');
    resetBtn.disabled = false;
    cancelMeasureBtn.classList.add('hidden');
    cancelMeasureBtn.disabled = false;
    emergencyStopActive = false;
    autolevelLockBtn.classList.add('hidden');
    autolevelLockBtn.disabled = false;
    autolevelCancelBtn.classList.add('hidden');
    autolevelCancelBtn.disabled = false;
    var autolevelRamping = autolevelStatus === 'ramping';
    // The persistent shell owns the emergency action outside the envelope.
    // Audio-producing preparation/sweep/verify phases use Stop: the server
    // cancels and reaps their exact playback task before graph rollback.
    // ANALYZING is intentionally absent (no audio is playing and its worker
    // must finish coherently). Parked browser/user states use Cancel.
    var cancellableStates = [
      'preparing', 'sweeping', 'verifying',
      'needs_noise_capture',
      'awaiting_capture', 'awaiting_repeat_capture', 'awaiting_verify_capture',
      'needs_next_position', 'needs_repeat_capture',
    ];
    if (cancellableStates.indexOf(state) !== -1) {
      emergencyStopActive = (
        state === 'preparing' || state === 'sweeping' || state === 'verifying'
      );
      cancelMeasureBtn.textContent = emergencyStopActive
        ? 'Stop measurement'
        : 'Cancel measurement';
      cancelMeasureBtn.classList.remove('hidden');
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
    } else if (state === 'needs_repeat_capture') {
      positionPrompt.classList.remove('hidden');
      positionCurrent.textContent = '1';
      positionTotal.textContent = '1';
    } else if (state === 'ready') {
      resetBtn.classList.remove('hidden');
    } else if (state === 'applied' || state === 'verified') {
      resetBtn.classList.remove('hidden');
    }
    // resetBtn's own hidden class just settled — re-derive whether the
    // banner's reset control should defer to it (see syncCurrentCorrectionReset).
    syncCurrentCorrectionReset();
  }

  function renderRelayStatusFromSnapshot(snapshot) {
    if (!relayMode) return;
    if (snapshot && snapshot.state === 'needs_next_position') {
      renderRelayCapture(null);
      setRelayStatus(
        'Position ' + Number(snapshot.current_position || 0) +
          ' received. Move the phone to position ' +
          (Number(snapshot.current_position || 0) + 1) + ' of ' +
          Number(snapshot.total_positions || 0) +
          ', then create the next phone capture.',
        'ok'
      );
      return;
    }
    if (snapshot && snapshot.relay) {
      renderRelayCapture(snapshot.relay);
      return;
    }
    if (snapshot && snapshot.state === 'failed') {
      console.warn('room-correction session failed', snapshot.error || '');
      setRelayStatus(GENERIC_STEP_FAILURE, 'bad');
    }
  }

  async function pollState(options) {
    options = options || {};
    if (pollTimer) clearTimeout(pollTimer);
    try {
      var s = await fetchStatus();
      syncSessionMechanics(s);
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
      applyButtonPolicy(s.state, s.autolevel ? s.autolevel.status : 'idle');
      // Edge-trigger the envelope-driven wizard chrome on a real transition
      // (state change, or autolevel sub-state change that flips the "level"
      // screen). Static screens are refreshed exactly here — never on every
      // /status tick — honouring the P3b-1 poll discipline.
      var autolevelState = s.autolevel ? s.autolevel.status : 'idle';
      if (options.skipEnvelopeRefresh) {
        pendingHomeownerFailure = null;
        lastObservedStatusState = s.state;
        lastAutolevelStatus = autolevelState || 'idle';
      } else {
        maybeRefreshEnvelopeOnStateChange(
          s.state, autolevelState,
        );
      }

      if (s.state === 'needs_next_position') {
        positionCurrent.textContent = (s.current_position + 1);
        positionTotal.textContent = s.total_positions;
        return;
      }
      if (s.state === 'needs_repeat_capture') {
        var repeatRelayPending = relayMode && s.relay && (
          s.relay.status === 'starting' ||
          s.relay.status === 'awaiting_phone'
        );
        if (repeatRelayPending) {
          pollTimer = setTimeout(pollState, 500);
        } else if (relayMode && s.relay) {
          envelopeRetryArmed = true;
          refreshEnvelope();
        }
        return;
      }
      if (
        s.state === 'awaiting_capture' ||
        s.state === 'awaiting_verify_capture' ||
        s.state === 'awaiting_repeat_capture'
      ) {
        if (relayMode && (
          s.state === 'awaiting_capture' ||
          s.state === 'awaiting_repeat_capture'
        )) {
          pollTimer = setTimeout(pollState, 500);
          return;
        }
        if (workletNode) workletNode.port.postMessage('stopCapture');
        return;  // upload-capture handler resumes polling
      }
      if (s.state === 'idle' || s.state === 'ready' ||
          s.state === 'applied' || s.state === 'verified' ||
          s.state === 'failed') {
        return;
      }
      // Mid-flight states: keep polling.
      pollTimer = setTimeout(pollState, 500);
    } catch (e) {
      setStateBadge('failed', e.message);
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
          throw await responseError(noiseResp, GENERIC_STEP_FAILURE);
        }
        await noiseResp.json();
        finishNoiseCapture(null);
        pollState();
      } catch (e) {
        captureMode = 'discard';
        if (workletNode) workletNode.port.postMessage('stopCapture');
        finishNoiseCapture(e);
        setStateBadge('failed', e.message);
        showHomeownerFailure(e);
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
        throw await responseError(resp, GENERIC_STEP_FAILURE);
      }
      // The upload response is only an acknowledgement. Presentation comes
      // from the envelope after the server has committed the new state.
      await resp.json();
      pendingHomeownerFailure = null;
      envelopeRetryArmed = true;
      await Promise.all([
        pollState({skipEnvelopeRefresh: true}),
        refreshEnvelope(),
      ]);
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
      try {
        renderQuality(await fetchStatus());
      } catch (ignored) {}
    }
  }

  async function applyCorrection(triggerBtn) {
    if (triggerBtn) triggerBtn.disabled = true;
    setStateBadge('analyzing', 'applying to CamillaDSP…');
    try {
      await postJson('apply', {});
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
    } finally {
      if (triggerBtn) triggerBtn.disabled = false;
    }
    refreshCurrentCorrection();
  }

  async function resetCorrection() {
    if (!(await jtsConfirm(
      'Reset room correction? This removes the applied adjustments and cannot be undone.',
      {danger: true},
    ))) {
      return;
    }
    resetBtn.disabled = true;
    setStateBadge('analyzing', 'resetting correction…');
    try {
      await postJson('reset', {});
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
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
    // Disable immediately, before the confirm dialog even opens — a fast
    // double-tap could otherwise fire this handler twice and stack two
    // confirms. Re-enable if the operator backs out below.
    cancelMeasureBtn.disabled = true;
    // Active audio is an emergency control: the first tap must dispatch the
    // server-side stop/reap path immediately. Parked-state cancellation keeps
    // its destructive confirmation because no audio needs urgent silencing.
    if (!emergencyStopActive && !(await jtsConfirm(
      'Cancel this measurement and restore the speaker?',
      {danger: true},
    ))) {
      cancelMeasureBtn.disabled = false;
      return;
    }
    setStateBadge('idle', 'cancelling…');
    try {
      await postJson('reset', {});
      // A pending mic-permission prompt (startMicCapture's getUserMedia
      // await) can outlive this cancel — clear the in-flight latch and force
      // a fresh render so the primary action isn't stranded hidden behind a
      // flag that may never otherwise clear.
      wizardActionInFlight = false;
    } catch (e) {
      setStateBadge('failed', e.message);
      showHomeownerFailure(e);
    } finally {
      cancelMeasureBtn.disabled = false;
    }
    pollState();
    envelopeRetryArmed = true;   // a fresh trigger grants one retry credit
    refreshEnvelope();
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

  refreshInputsBtn.addEventListener('click', function () { detectMicrophones(); });
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', function () {
      populateInputDevices(inputDeviceSelect.value);
    });
  }
  inputDeviceSelect.addEventListener('change', function () {
    var opt = inputDeviceSelect.options[inputDeviceSelect.selectedIndex];
    maybeInferCalibrationModel(opt ? opt.textContent : '');
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
      if (runTransportLocked) return;
      setRelayMode(!relayMode);
      if (!relayMode) populateInputDevices();
      envelopeRetryArmed = true;
      refreshEnvelope();
    });
  }
  if (changeRunDefaultsBtn) {
    changeRunDefaultsBtn.addEventListener('click', function () {
      if (runTransportLocked) return;
      setMeasurementOptionsOpen(
        measurementOptions.classList.contains('hidden')
      );
    });
  }
  [positionsSelect, targetSelect, strategySelect].forEach(function (select) {
    if (!select) return;
    select.addEventListener('change', function () {
      runDefaultsDirty = true;
      updateRunDefaultsSummaryFromControls();
    });
  });
  resetBtn.addEventListener('click', function () { resetCorrection(); });
  cancelMeasureBtn.addEventListener('click', function () { cancelMeasurement(); });
  if (tuningInterpretBtn) tuningInterpretBtn.addEventListener('click', function () { onTuningInterpret(); });
  if (tuningProposeBtn) tuningProposeBtn.addEventListener('click', function () { onTuningPropose(); });
  document.addEventListener('visibilitychange', function () {
    // Re-acquire the wake lock the OS dropped while we were backgrounded,
    // but only while a capture is actually live.
    if (!document.hidden && micStream && !wakeLockSentinel) {
      acquireWakeLock();
    }
  });
  autolevelCancelBtn.addEventListener('click', function () { cancelAutolevel(); });
  currentCorrectionResetBtn.addEventListener('click', function () { resetFromBanner(); });
  if (wizardNextBtn) {
    wizardNextBtn.addEventListener('click', function () { onWizardNextClick(); });
  }
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

  // Landing never asks for microphone permission. Local capture requests it
  // only after /start exposes the server-owned Allow microphone action; the
  // refresh control is likewise inside that post-Start setup section.
  if (relayConfigured) {
    setRelayMode(true);
  } else {
    if (!window.isSecureContext && currentPathname().indexOf('/correction/') === 0) {
      window.location.href = '/correction/proceed/room';
      return;
    }
    setRelayMode(false);
    populateInputDevices();
  }
  pollState();
  updateMicCalibrationRows();
  applyHouseholdMicPrefill();
  refreshCurrentCorrection();
  // Initial paint of the stepped-wizard chrome from the server envelope.
  // (Both landing paths reach here; the plain-HTTP deep-link fallback above
  // returns before this, so it never fires there.)
  envelopeRetryArmed = true;   // landing grants one retry credit
  refreshEnvelope();

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
  chartShowFilter.addEventListener('change', redrawLatestChart);
})();
