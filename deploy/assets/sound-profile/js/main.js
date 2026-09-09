// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — parametric EQ editor.
//
// Static ES module served from /assets/sound-profile/js/ (revalidated by
// nginx, same delivery model as /system/). CSRF helpers read the
// <meta name=jts-csrf> tag. Module scope is strict mode — the IIFE
// declares all its state with var/function.
//
// This entry module owns the page's reassigned state, the render and IO paths
// that write it, and the event wiring. Helpers that read no reassigned state
// moved to sibling concern modules where they formed a coherent seam; the
// rest stay here. A module cannot assign to an imported binding, so moving a
// state WRITER out is a rewrite of the state, not a move. The editor's
// live-draft path (band-drag + live-draft → CamillaDSP) still has to be
// exercised on a Pi before that rewrite; do not blind-refactor it.
//
// jsonHeaders is imported from /assets/shared/js/http.js — the one
// cross-page owner of the CSRF/JSON plumbing. A conventions guard in
// tests/test_web_wizard_conventions.py keeps a local re-declaration from
// creeping back (same shared-by-promotion rule as escape.js / dialog.js).
import { jtsConfirm } from "/assets/shared/js/dialog.js";
import { escapeHtml } from "/assets/shared/js/escape.js";
import { jsonHeaders, postJSON } from "/assets/shared/js/http.js";
import {
  DEFAULT_SUB_CROSSOVER_HZ,
  SUB_CROSSOVER_HZ_HI,
  SUB_CROSSOVER_HZ_LO,
  activeCommissionGroup,
  activeSpeakerStepState,
  clampSubwooferCrossoverFcHz,
  commissioningStepFooter,
  commissionPayloadHasIssue,
  commissionPayloadFailure,
  defaultActiveSpeakerStep,
  humanRole,
  levelMatchSummary,
  outputStatusClass,
  outputStepTitle,
  sensitivityTrimsFromGap,
  subwooferCrossoverFcHz,
  summedGroupFailureHint,
  SUMMED_TEST_GENERIC_RETRY_HINT
} from "/assets/sound-profile/js/active-speaker-ui.js";
import {
  magnitudeDb,
  GAINLESS_TYPES
} from "/assets/sound-profile/js/eq-math.js";
import {
  kaBeamingNoteHtml,
  renderAdvancedDriverSettings,
  renderBuildNotes,
  renderComponentSettings,
  renderCrossoverPreviewRows,
  renderDriverEchoBack,
  renderDriverSafetyWarnings,
  renderIssueList,
  renderManualCrossoverSettings,
  renderPreviewIssues,
  renderSubwooferCrossoverControl,
  renderWorkingCrossoverRows
} from "/assets/sound-profile/js/driver-fields.js";
import {
  applySafetyBandToSetting,
  cabinetFromSetting,
  candidateConfidenceRank,
  candidateFrequency,
  crossoverPreviewDisplayStatus,
  crossoverPreviewReadyCount,
  crossoverPreviewReadyForProtectedStaging,
  crossoverPreviewReviewIssues,
  driverResearchFlowComplete,
  driverResearchHasPreviewInputs,
  driverResearchMissingPreviewMessage,
  driverResearchPrompt,
  driverResearchPromptReady,
  driverResearchStepSatisfied,
  driverResearchTargets,
  driverSafetyConflicts,
  driverSafetyNoteRoles,
  driverSafetyReviewHint,
  extractDriverResearchJson,
  ingestCrossoverPreview,
  invalidateDriverResearchBinding,
  levelDurationLimitsFromSetting,
  manualCrossoverDelayValidationError,
  manualCrossoverVocabularyValidationError,
  padFromSetting,
  previewStatusClass,
  proposeSensitivityTrims,
  protectionFiltersFromSetting,
  safetyBandFromSetting,
  setManualCrossoverField,
  summarizeDriverResearchPayload,
  targetModel,
  workingSetupSummary
} from "/assets/sound-profile/js/driver-model.js";
import {
  H,
  MAXDB,
  MINDB,
  W,
  advancedSpecs,
  drawArea,
  drawPath,
  freqToSlider,
  gx,
  gy,
  padB,
  padL,
  padR,
  padT,
  pointsFor,
  sliderToFreq,
  specActive,
  summedDbAt
} from "/assets/sound-profile/js/eq-curve.js";
import {
  clamp,
  clone,
  fmtDb,
  fmtFreq,
  fmtFreqShort,
  fmtQ,
  fmtTrim,
  ico,
  manualNumberValue,
  roleSentenceText,
  sleepMs
} from "/assets/sound-profile/js/format.js";
import {
  ACTIVE_GAIN_EPSILON_DB,
  crossoverPreview,
  crossoverVocabulary,
  driverResearch,
  el,
  eqEditor,
  followerMode,
  outputPage,
  outputTopology,
  pageMode,
  resetEqEditor,
  resetOutputPage
} from "/assets/sound-profile/js/state.js";
import {
  activeCommissionRoles,
  activeCrossoverPairs,
  activeOutputGroups,
  baseOutputDraft,
  crossChildGroupVerdicts,
  crossoverSetting,
  crossoverSettingKey,
  currentOutputTopology,
  driverSetting,
  firstUnusedOutputIndex,
  hardwareOutputCount,
  nextSubwooferGroupId,
  observedOutputHardware,
  outputAssignedToOtherMap,
  outputChannel,
  outputChannelGuardReady,
  outputClockDomainReport,
  outputGroups,
  outputHardware,
  outputHardwareMismatch,
  outputHasSubwoofer,
  outputTemplateKindFromAxes,
  outputTemplateUnavailableReason,
  pairRoleKey,
  physicalOutputLabel,
  physicalOutputOptions,
  removeSubwooferFromTopology
} from "/assets/sound-profile/js/topology.js";
(function() {
  var LIMIT_DEFAULTS = {
    simple_gain_db: 12, advanced_gain_db: 12, max_parametric_bands: 8,
    min_freq_hz: 20, max_freq_hz: 20000, min_q: 0.2, max_q: 10, cut_max_q: 1.4,
    simple_bands: [], headroom_trim_max_db: 12,
    // volume_floor_default_db is owned by the backend (volume_curve.
    // DEFAULT_VOLUME_FLOOR_DB → /state limits) and read via volumeFloorDefault().
    // These three are the payload-absent fallbacks only.
    volume_floor_min_db: -60, volume_floor_max_db: -10, volume_floor_default_db: -50
  };
  var DEFAULT_SAVED_ID = 'stock:flat';
  // Fallback for a `status: "blocked"` body with no message of its own.
  var EQ_BLOCKED_MESSAGE = 'Sound EQ is unavailable for this speaker setup.';
  // What the settings card says while the loaded graph refuses to carry EQ.
  // The per-reason remedy belongs on /sound/eq/, not on a setting's card.
  var EQ_BLOCKED_CARD_MESSAGE = 'The setting is saved, but sound EQ is not ' +
    'audible until this speaker’s setup can carry it.';
  var FLAT = function() {
    return {enabled: true, curve_id: 'flat',
            simple_eq: zeroSimple(), parametric_bands: [],
            profile_id: '', profile_name: ''};
  };

  var limits = Object.assign({}, LIMIT_DEFAULTS);

  var draft = FLAT();          // working profile in the Draft tab
  var allCollapsed = false;

  var applied = FLAT();        // persisted profile
  var volumeFloorSaving = false;
  var dspWriteEpoch = 'none';
  var applying = false;
  var liveSourceSeq = 0, liveSourcePending = false, liveSourceOptions = {};
  var previewTimer = null, previewSeq = 0;
  var liveTimer = null, liveSeq = 0, liveInFlight = false, livePending = false;
  var statusText = '', statusErr = false;
  var activeSpeaker = {
    loading: false, action: '', session: null,
    calibrationLevel: null, measurements: null,
    baselineProfile: null, error: '', levelDbfs: null,
    combinedTestLevelDbfs: null,
    commission: null, commissioningView: null,
    commissionBusy: '', commissionError: ''
  };
  // The handoff card's copy state. `copiedRevision` is the declaration
  // revision the copied prompt was MINTED against (server-stamped), so a
  // later declaration edit turns the copy stale instead of drifting silently.
  var tuningHandoff = {prompt: '', copied: false, selected: false, copiedRevision: null};
  var summedTestRequest = {token: 0, armTimer: null, current: null};
  var summedTestLevelUpdate = {timer: null, inFlight: false, pending: null};
  var commissionAutoRamp = {
    running: false,
    token: 0,
    targetKey: '',
    stepCount: 0,
    levelDbfs: null,
    message: ''
  };
  var COMMISSION_RAMP_LISTEN_MS = 900;
  var COMMISSION_RAMP_NEXT_PULSE_MS = 80;
  var SUMMED_TEST_STOP_ARM_MS = 250;
  // Issue #1820 defect 3 / #1821: the DOM id the measurement wizard's
  // profile-not-confirmed hard stop deep-links to
  // (crossover_v2_flow.REASON_PROGRAM_PROFILE_NOT_CONFIRMED's next_action href
  // is "/sound/speaker/#confirm-safety-limits"). Both halves of that link — the id
  // rendered here and the href in the registry — are pinned by
  // tests/test_sound_profile_confirm_deeplink.py so neither can move alone.
  var CONFIRM_SAFETY_ANCHOR_ID = 'confirm-safety-limits';
  var driverAdvancedOpen = false;
  var ZERO_DETENT_DB = 0.1;
  var volumeFloorTone = {
    active: false,
    timer: null,
    inFlight: false,
    pending: null,
    generation: 0,
    savedNotice: false
  };
  function patchActiveSpeaker(patch) {
    activeSpeaker = Object.assign({}, activeSpeaker, patch || {});
    return activeSpeaker;
  }
  function zeroSimple() {
    var out = {};
    (eqEditor.simpleBands.length ? eqEditor.simpleBands : LIMIT_DEFAULTS.simple_bands).forEach(function(b) {
      out[b.field] = 0;
    });
    if (!eqEditor.simpleBands.length) {
      ['sub_bass_db', 'bass_db', 'mid_db', 'presence_db', 'treble_db'].forEach(function(f) {
        if (!(f in out)) out[f] = 0;
      });
    }
    return out;
  }
  function status(msg, isErr) {
    statusText = msg || '';
    statusErr = !!isErr;
    var node = el('status');
    if (node) {
      node.textContent = statusText;
      node.className = 'status-line' + (statusErr ? ' status-line--err' : '');
    }
  }

  // ---- profile helpers ------------------------------------------------
  function normalizeProfile(raw) {
    raw = raw || {};
    var simple = raw.simple_eq || {};
    var normSimple = {};
    var bands = eqEditor.simpleBands.length ? eqEditor.simpleBands : [
      {field: 'sub_bass_db'}, {field: 'bass_db'}, {field: 'mid_db'},
      {field: 'presence_db'}, {field: 'treble_db'}
    ];
    bands.forEach(function(b) { normSimple[b.field] = Number(simple[b.field] || 0); });
    return {
      enabled: raw.enabled !== false,
      curve_id: raw.curve_id || 'flat',
      simple_eq: normSimple,
      parametric_bands: (raw.parametric_bands || []).map(function(b) {
        return {
          enabled: b.enabled !== false,
          type: b.type || b.biquad_type || 'Peaking',
          freq_hz: Number(b.freq_hz || b.freq || 1000),
          gain_db: Number(b.gain_db || b.gain || 0),
          q: Number(b.q || 1)
        };
      }),
      profile_id: raw.profile_id || '',
      profile_name: raw.profile_name || ''
    };
  }
  function profileKey(profile) {
    profile = normalizeProfile(profile);
    return JSON.stringify({
      enabled: profile.enabled, curve_id: profile.curve_id,
      simple_eq: profile.simple_eq, parametric_bands: profile.parametric_bands
    });
  }
  function entryById(id) {
    return eqEditor.library.find(function(e) { return e.id === id; }) || null;
  }
  function userEntries() { return eqEditor.library.filter(function(e) { return e.kind === 'custom'; }); }
  function presetEntries() { return eqEditor.library.filter(function(e) { return e.kind === 'stock'; }); }
  function fallbackSavedId() {
    if (entryById(DEFAULT_SAVED_ID)) return DEFAULT_SAVED_ID;
    return eqEditor.library.length ? eqEditor.library[0].id : null;
  }
  function selectedSavedEntry() {
    var entry = entryById(eqEditor.selectedId);
    if (entry) return entry;
    eqEditor.selectedId = fallbackSavedId();
    return eqEditor.selectedId ? entryById(eqEditor.selectedId) : null;
  }
  function selectedSavedProfile() {
    var entry = selectedSavedEntry();
    return entry ? withIdentity(normalizeProfile(entry.profile), entry.id, entry.name) : null;
  }
  function findIdFor(profile) {
    profile = normalizeProfile(profile);
    if (profile.profile_id && entryById(profile.profile_id)) return profile.profile_id;
    var key = profileKey(profile);
    var stock = eqEditor.library.find(function(e) { return e.kind === 'stock' && profileKey(e.profile) === key; });
    if (stock) return stock.id;
    var custom = eqEditor.library.find(function(e) { return e.kind === 'custom' && profileKey(e.profile) === key; });
    if (custom) return custom.id;
    return 'stock:' + (profile.curve_id || 'flat');
  }
  // The profile the editor sources from (for the modified/dirty check).
  function sourceProfile() {
    if (eqEditor.editing.kind === 'new') return FLAT();
    var entry = entryById(eqEditor.editing.id);
    return entry ? normalizeProfile(entry.profile) : FLAT();
  }
  function draftModified() {
    return profileKey(draft) !== profileKey(sourceProfile());
  }
  function withIdentity(profile, id, name) {
    profile = clone(profile);
    profile.profile_id = id || '';
    profile.profile_name = name || '';
    return profile;
  }
  // The profile currently driving the speaker per the active tab.
  function liveProfile() {
    if (eqEditor.view === 'off') return null;
    if (eqEditor.view === 'saved') {
      var entry = selectedSavedEntry();
      return entry ? normalizeProfile(entry.profile) : null;
    }
    return draft;
  }
  function liveLabel() {
    if (eqEditor.view === 'off') return 'Bypass';
    if (eqEditor.view === 'saved') {
      var entry = selectedSavedEntry();
      return entry ? entry.name : 'No profile selected';
    }
    if (eqEditor.editing.kind === 'new') return 'New profile' + (draftModified() ? ' · edited' : '');
    var lead = eqEditor.editing.kind === 'preset' ? 'From preset: ' : 'Editing: ';
    return lead + eqEditor.editing.name + (draftModified() ? ' · edited' : '');
  }

  // ---- preview math ---------------------------------------------------
  // Optimistic client mirror of jasper/sound/profile.py's response math,
  // for instant graph feedback before /preview returns (and for graphing a
  // saved profile without a round-trip). Both sides are deliberately
  // illustrative approximations; CamillaDSP owns the real biquads, and the
  // authoritative /preview payload overwrites this within ~90 ms. Keep the
  // two shelf/peak formulas in sync.
  function previewFreqs() {
    var out = [];
    for (var i = 0; i <= 120; i += 1) {
      out.push(limits.min_freq_hz * Math.pow(limits.max_freq_hz / limits.min_freq_hz, i / 120));
    }
    return out;
  }
  // Cut filters (HP/LP) get a tighter Q ceiling — a high-Q cut is a big
  // resonant boost at the corner. Mirrors CUT_MAX_Q in jasper/sound/profile.py.
  function bandQMax(type) {
    return (type === 'Highpass' || type === 'Lowpass') ? limits.cut_max_q : limits.max_q;
  }
  function curveSpecs(profile) { return (eqEditor.curvesById[profile.curve_id] || {}).filters || []; }
  function simpleSpecs(profile) {
    var simple = profile.simple_eq || {};
    return (eqEditor.simpleBands.length ? eqEditor.simpleBands : []).map(function(b) {
      return {type: b.type, freq_hz: b.freq_hz, gain_db: simple[b.field] || 0,
              q: b.type === 'Peaking' ? 1.0 : undefined};
    });
  }
  function previewPayload(profile) {
    profile = normalizeProfile(profile);
    var freqs = previewFreqs();
    if (profile.enabled === false) {
      return {preview: [], off: true};
    }
    var all = curveSpecs(profile).concat(simpleSpecs(profile), advancedSpecs(profile));
    return {preview: pointsFor(all, freqs, false)};
  }

  // ---- graph rendering ------------------------------------------------
  // One dot per band, sitting on the summed curve. Only the expanded band
  // adds a frequency guide line (+ width shading for Peaking) — no per-band
  // marker lines or component curves clutter the default view.
  function drawBandMarkers(summed) {
    if (eqEditor.view !== 'draft' || eqEditor.mode !== 'peq') return '';
    var expandedBand = expandedPeqBandIndex();
    var html = '';
    (draft.parametric_bands || []).forEach(function(b, i) {
      if (!b || b.enabled === false) return;
      var sel = i === expandedBand;
      var fx = clamp(b.freq_hz, 20, 20000);
      var cx = gx(fx), cy = gy(clamp(summedDbAt(summed, fx), MINDB, MAXDB));
      if (sel) {
        if ((b.type || 'Peaking') === 'Peaking') {
          var q = Math.max(Number(b.q || 1), 0.2);
          var lo = gx(clamp(b.freq_hz / Math.pow(2, 1 / q), 20, 20000));
          var hi = gx(clamp(b.freq_hz * Math.pow(2, 1 / q), 20, 20000));
          html += '<rect class="band-width" x="' + Math.min(lo, hi).toFixed(1) +
                  '" y="' + padT + '" width="' + Math.abs(hi - lo).toFixed(1) +
                  '" height="' + (H - padB - padT) + '"></rect>';
        }
        html += '<line class="band-guide" x1="' + cx.toFixed(1) + '" x2="' + cx.toFixed(1) +
                '" y1="' + cy.toFixed(1) + '" y2="' + (H - padB) + '"></line>';
      }
      html += '<circle class="band-dot' + (sel ? ' selected' : '') + '" cx="' + cx.toFixed(1) +
              '" cy="' + cy.toFixed(1) + '" r="' + (sel ? 4.5 : 3.5) + '"></circle>';
    });
    return html;
  }
  function expandedPeqBandIndex() {
    if (eqEditor.view !== 'draft' || eqEditor.mode !== 'peq' || allCollapsed || eqEditor.activeBand < 0) return -1;
    return eqEditor.activeBand;
  }
  function renderGraph(payload, enabled) {
    var svg = el('plot');
    if (!svg) return;
    svg.classList.toggle('off', !enabled);
    var html = '';
    [-6, 0, 6].forEach(function(db) {
      html += '<line class="' + (db === 0 ? 'zero' : 'grid') + '" x1="' + padL + '" x2="' + (W - padR) +
              '" y1="' + gy(db).toFixed(1) + '" y2="' + gy(db).toFixed(1) + '"></line>';
      html += '<text x="6" y="' + (gy(db) + 3).toFixed(1) + '">' + fmtDb(db) + '</text>';
    });
    [20, 100, 1000, 10000, 20000].forEach(function(f) {
      html += '<line class="grid" y1="' + padT + '" y2="' + (H - padB) + '" x1="' + gx(f).toFixed(1) +
              '" x2="' + gx(f).toFixed(1) + '"></line>';
      html += '<text text-anchor="middle" x="' + gx(f).toFixed(1) + '" y="' + (H - 8) + '">' +
              (f >= 1000 ? (f / 1000) + 'k' : f) + '</text>';
    });
    // One line only: the summed response. The selected band is marked by its
    // on-curve dot + width shading (drawBandMarkers), not a second curve.
    if (enabled) {
      html += drawArea(payload.preview || []);
    }
    var curvePts = enabled
      ? (payload.preview || [])
      : [{freq_hz: 20, db: 0}, {freq_hz: 20000, db: 0}];
    html += drawPath(curvePts, 'curve');
    if (enabled) html += drawBandMarkers(curvePts);
    svg.innerHTML = html;
    var peak = (payload.preview || []).reduce(function(m, p) { return Math.max(m, p.db); }, 0);
    var summary = el('plot-summary');
    if (summary) {
      summary.textContent = enabled
        ? 'EQ response preview. Peak boost ' + fmtDb(peak) + ' dB.'
        : 'EQ bypassed. Flat response.';
    }
  }
  // Render the graph for whatever is the live source right now.
  function renderLiveGraph() {
    var profile = liveProfile();
    if (!el('live-label')) return;
    el('live-label').textContent = liveLabel();
    if (!profile) { renderGraph({preview: []}, false); return; }
    renderGraph(previewPayload(profile), profile.enabled !== false);
  }

  // ---- view rendering -------------------------------------------------
  function renderTabs() {
    ['off', 'saved', 'draft'].forEach(function(v) {
      var btn = el('tab-' + v);
      btn.setAttribute('aria-pressed', v === eqEditor.view ? 'true' : 'false');
      btn.classList.toggle('is-live', v === eqEditor.view);
    });
  }
  function render() {
    if (followerMode) {
      renderFollower();
      status(statusText, statusErr);
      return;
    }
    if (pageMode !== 'eq') {
      if (pageMode === 'speaker') renderSpeaker(); else renderOutput();
      status(statusText, statusErr);
      return;
    }
    // The tab strip and the now-playing plot describe an editor this page is
    // not showing, and the plot would sit empty, so both go with it.
    ['eq-tabs', 'now-playing'].forEach(function(id) {
      var node = el(id);
      if (node) node.hidden = !!outputPage.eqCarrierBlock;
    });
    if (outputPage.eqCarrierBlock) {
      renderEqCarrierBlocked();
      status(statusText, statusErr);
      return;
    }
    renderTabs();
    renderLiveGraph();
    if (eqEditor.view === 'off') renderOff();
    else if (eqEditor.view === 'saved') renderSaved();
    else renderDraft();
    status(statusText, statusErr);
  }

  // A follower's local page carries the I2S HAT too: its Output page is
  // delegated to the leader. No EQ tabs/plot exist on a follower.
  function renderFollower() {
    el('view-body').innerHTML =
      '<div class="saved-stack"><section class="active-speaker-setup">' +
      renderI2sHatSetting() + renderOutputTopologySetup() +
      '</section></div>';
  }

  function renderSpeaker() {
    el('view-body').innerHTML =
      '<div class="saved-stack"><section class="active-speaker-setup">' +
      renderOutputTopologySetup() + '</section></div>';
  }

  function renderOutput() {
    el('view-body').innerHTML = '<div class="saved-stack">' +
      renderI2sHatSetting() + renderSetupSoundSettings() + '</div>';
  }

  function renderI2sHatSetting() {
    var hat = outputPage.i2sHat;
    if (!hat || hat.visibility === 'hidden') return '';
    var profiles = hat.profiles || [];
    var selectedId = hat.desired_profile_id || '';
    var issue = hat.intent_error ? 'Saved setting could not be read: ' + hat.intent_error : hat.reason;
    var warnings = hat.warnings || [];
    var options = '<option value=""' + (selectedId ? '' : ' selected') + '>None / unmanaged</option>' +
      profiles.map(function(p) {
        return '<option value="' + escapeHtml(p.id) + '"' +
          (selectedId === p.id ? ' selected' : '') + '>' + escapeHtml(p.label) + '</option>';
      }).join('');
    // A HAT that identifies itself is applied for the operator, so the picker
    // is shown only for the HATs that cannot be detected (ADR-0234).
    var control = hat.detected_profile_id ?
      '<p class="setting-row__hint"><strong>I²S audio HAT.</strong> Detected: ' +
        escapeHtml(hat.detected_label || hat.detected_profile_id) +
        ' — managed automatically.</p>' :
      '<div class="field">' +
        '<label for="set-i2s-hat">I²S audio HAT</label>' +
        '<select id="set-i2s-hat"' + (!hat.available ? ' disabled' : '') +
          ' aria-label="I²S audio HAT">' + options + '</select>' +
        '<p class="setting-row__hint">Pick the HAT you fitted. Only HATs that cannot identify themselves are listed.</p>' +
      '</div>';
    return '<section class="sound-settings">' +
      '<div class="setting-row setting-row--stack">' +
        control +
        (issue ? '<p class="setting-row__hint">' + escapeHtml(issue) + '</p>' : '') +
        warnings.map(function(w) {
          return '<p class="setting-row__hint output-template-warning">' + escapeHtml(w) + '</p>';
        }).join('') +
        (hat.restart_required ? '<div class="info-card info-card--accent" role="status">' +
          '<p><strong>Restart required.</strong> The saved boot setting changed.</p>' +
          '<a class="btn btn--primary" href="/system/">Open Restart control</a></div>' : '') +
        (hat.shared_usb_data_port ? '<p class="setting-row__hint"><strong>Shared USB data port:</strong> Enabling this setting reserves this shared port for gadget/peripheral use after restart, so it can no longer host a USB output DAC and output moves to the HAT. While the HAT powers the Pi, do not connect an ordinary powered micro-USB host cable: it supplies 5 V and can back-power the Pi. Use a VBUS-isolated data connection/adapter or leave the port disconnected.</p>' : '') +
        '<p class="setting-row__hint"><strong>Hardware safety:</strong> Shut down and remove all power ' +
          'before installing or removing the HAT. Never power the Pi through the HAT and another power input at the same time. ' +
          'Never hot-plug. Start the first playback at a very low level.</p>' +
      '</div></section>';
  }

  // ./apply and ./live-draft refuse with the same typed body /state carries,
  // so a refusal mid-session becomes the page's state too. Recorded whatever
  // the request's sequence: it describes the loaded graph, not this request.
  function noteCarrierRefusal(payload) {
    outputPage.eqCarrierBlock = {
      status: 'blocked',
      reason_code: payload.reason_code || '',
      message: payload.message || EQ_BLOCKED_MESSAGE
    };
  }

  // The whole page when the loaded graph cannot host EQ: the editor would only
  // offer edits every save refuses, so it is replaced by the reason and the
  // one page that can change it.
  function renderEqCarrierBlocked() {
    el('view-body').innerHTML =
      '<div class="saved-stack"><section class="info-card" role="status">' +
        '<p>' + escapeHtml(outputPage.eqCarrierBlock.message || EQ_BLOCKED_MESSAGE) + '</p>' +
        '<div class="form-actions">' +
          '<a class="btn btn--primary" href="/sound/speaker/">Open Speaker setup</a>' +
        '</div>' +
      '</section></div>';
  }

  function renderOff() {
    el('view-body').innerHTML =
      '<div class="saved-stack">' +
      '<section class="off-card">' +
        '<div class="off-card__icon">' + ico('spark') + '</div>' +
        '<p class="off-card__text">Create a sound profile that changes how your speaker sounds.</p>' +
        '<div class="form-actions">' +
          '<button type="button" class="btn btn--ghost" data-act="browse-presets">Try a stock profile</button>' +
          '<button type="button" class="btn btn--primary" data-act="new-draft">Create custom profile</button>' +
        '</div>' +
      '</section>' +
      '</div>';
  }

  function profileRow(entry, live, deletable) {
    return '<div class="profile-row">' +
      '<button type="button" class="profile-row__select" data-act="select" data-id="' + escapeHtml(entry.id) + '">' +
        '<span class="profile-row__dot' + (live ? ' profile-row__dot--on' : '') + '"></span>' +
        '<span style="min-width:0">' +
          '<p class="profile-row__name">' + escapeHtml(entry.name) + '</p>' +
          '<p class="profile-row__meta">' + (live ? 'Now playing · ' : '') +
            bandCountLabel(entry.profile) + '</p>' +
        '</span>' +
      '</button>' +
      '<span class="profile-row__actions">' +
        '<button type="button" class="profile-row__action" data-act="edit" data-id="' + escapeHtml(entry.id) +
          '" aria-label="Edit ' + escapeHtml(entry.name) + '">' + ico('pencil') + '</button>' +
        (deletable ? '<button type="button" class="profile-row__action profile-row__action--danger" data-act="delete" data-id="' +
          escapeHtml(entry.id) + '" aria-label="Delete ' + escapeHtml(entry.name) + '">' + ico('trash') + '</button>' : '') +
      '</span>' +
    '</div>';
  }
  function bandCountLabel(profile) {
    profile = normalizeProfile(profile);
    var n = 0;
    Object.keys(profile.simple_eq).forEach(function(k) {
      if (Math.abs(profile.simple_eq[k]) >= ACTIVE_GAIN_EPSILON_DB) n += 1;
    });
    n += profile.parametric_bands.filter(function(b) { return b.enabled !== false && specActive(b); }).length;
    if (profile.curve_id && profile.curve_id !== 'flat') n += 1;
    return n === 0 ? 'Flat' : n + ' band' + (n === 1 ? '' : 's');
  }
  function renderSaved() {
    selectedSavedEntry();
    var users = userEntries(), presets = presetEntries();
    var userSection = '<section><div class="section-header">' +
      '<h2 class="eyebrow">Your profiles</h2>' +
      '<button type="button" class="text-button" data-act="new-draft">' + ico('plus') + 'New</button></div>' +
      (users.length
        ? '<div class="list-card"><div class="list-card__rows">' +
            users.map(function(e) { return profileRow(e, e.id === eqEditor.selectedId, true); }).join('') + '</div></div>'
        : '<div class="empty-card"><p>No profiles yet.</p>' +
            '<button type="button" class="btn btn--primary" data-act="new-draft">Create your first</button></div>') +
      '</section>';
    var presetSection = '<section><div class="section-header"><h2 class="eyebrow">Presets</h2></div>' +
      '<div class="list-card"><div class="list-card__rows">' +
        presets.map(function(e) { return profileRow(e, e.id === eqEditor.selectedId, false); }).join('') + '</div></div></section>';
    el('view-body').innerHTML = '<div class="saved-stack">' + userSection + presetSection + '</div>';
  }
  function fmtVolumeFloor(v) {
    v = Number(v);
    if (!isFinite(v)) v = volumeFloorDefault();
    return v.toFixed(1) + ' dB';
  }
  function volumeFloorLimits() {
    var floorMin = Number(limits.volume_floor_min_db);
    var floorMax = Number(limits.volume_floor_max_db);
    if (!isFinite(floorMin)) floorMin = -60;
    if (!isFinite(floorMax)) floorMax = -10;
    return {min: floorMin, max: floorMax};
  }
  // The reset/default volume floor, owned by the backend (volume_curve.
  // DEFAULT_VOLUME_FLOOR_DB → /state limits.volume_floor_default_db). Single
  // read point so the page never hardcodes the value; LIMIT_DEFAULTS supplies
  // the payload-absent fallback.
  function volumeFloorDefault() {
    var value = Number(limits.volume_floor_default_db);
    if (!isFinite(value)) value = Number(LIMIT_DEFAULTS.volume_floor_default_db);
    return value;
  }
  function savedVolumeFloorDb() {
    var bounds = volumeFloorLimits();
    var floor = Number(outputPage.soundSettings.volume_floor_db);
    if (!isFinite(floor)) floor = volumeFloorDefault();
    return clamp(floor, bounds.min, bounds.max);
  }
  function coerceVolumeFloorDb(value) {
    var bounds = volumeFloorLimits();
    var floor = Number(value);
    if (!isFinite(floor)) floor = savedVolumeFloorDb();
    return clamp(floor, bounds.min, bounds.max);
  }
  function volumeFloorValue() {
    return outputPage.volumeFloorDraftDb === null || outputPage.volumeFloorDraftDb === undefined ?
      savedVolumeFloorDb() : coerceVolumeFloorDb(outputPage.volumeFloorDraftDb);
  }
  function volumeFloorDirty(v) {
    return Math.abs(coerceVolumeFloorDb(v) - savedVolumeFloorDb()) >= 0.05;
  }
  function syncVolumeFloorControls(v) {
    var value = coerceVolumeFloorDb(v);
    var node = el('set-volume-floor-readout');
    if (node) node.textContent = fmtVolumeFloor(value);
    var resetButton = el('view-body').querySelector('[data-act="reset-volume-floor"]');
    if (resetButton) resetButton.disabled = Math.abs(value - volumeFloorDefault()) < 0.05;
    var saveButton = el('volume-floor-save-button');
    if (saveButton) {
      var dirty = volumeFloorDirty(value);
      saveButton.disabled = volumeFloorSaving || !dirty;
      saveButton.textContent = volumeFloorSaving ? 'Saving' : (dirty ? 'Save floor' : 'Saved');
    }
  }
  function setVolumeFloorDraft(v) {
    outputPage.volumeFloorDraftDb = coerceVolumeFloorDb(v);
    syncVolumeFloorControls(outputPage.volumeFloorDraftDb);
  }
  function renderMatchLoudnessSetting() {
    var ml = outputPage.soundSettings.match_loudness ? ' checked' : '';
    return '<div class="setting-row">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Match loudness</p>' +
          '<p class="setting-row__hint">Level-match profiles so switching compares tone, not volume.</p>' +
        '</div>' +
        '<label class="toggle"><input type="checkbox" id="set-match-loudness"' + ml +
          ' aria-label="Match loudness"><span class="track"></span></label>' +
      '</div>';
  }
  function renderSetupSoundSettings() {
    var trim = Number(outputPage.soundSettings.headroom_trim_db) || 0;
    var trimMax = Number(limits.headroom_trim_max_db) || 12;  // backend clamps authoritatively
    var floorBounds = volumeFloorLimits();
    var floorMin = floorBounds.min;
    var floorMax = floorBounds.max;
    var defaultFloor = volumeFloorDefault();
    var floor = volumeFloorValue();
    var advancedOpen = trim > 0 || Math.abs(floor - defaultFloor) >= 0.05;
    var toneLabel = volumeFloorTone.active ? 'Stop tone' : 'Start tone';
    var resetDisabled = Math.abs(floor - defaultFloor) < 0.05 ? ' disabled' : '';
    var saveDisabled = (volumeFloorSaving || !volumeFloorDirty(floor)) ? ' disabled' : '';
    var saveLabel = volumeFloorSaving ? 'Saving' :
      (volumeFloorDirty(floor) ? 'Save floor' : 'Saved');
    return '<section class="sound-settings">' +
      (outputPage.blocked ? '<div class="info-card" role="status"><p>' +
        EQ_BLOCKED_CARD_MESSAGE + '</p></div>' : '') +
      renderMatchLoudnessSetting() +
      '<details class="advanced"' + (advancedOpen ? ' open' : '') + '>' +
        '<summary>Advanced</summary>' +
        '<div class="setting-row setting-row--stack">' +
          '<div class="setting-row__text">' +
            '<p class="setting-row__title">Volume floor</p>' +
            '<p class="setting-row__hint">The 1% listening level. 0% stays fully muted.</p>' +
          '</div>' +
          '<div class="headroom-control">' +
            '<input type="range" class="headroom-range" id="set-volume-floor" min="' + floorMin +
              '" max="' + floorMax + '" step="1" value="' + floor + '" aria-label="Volume floor in dB">' +
            '<button type="button" class="btn btn--ghost btn--compact" id="volume-floor-tone-button" ' +
              'data-act="toggle-volume-floor-tone">' + toneLabel + '</button>' +
            '<button type="button" class="btn btn--primary btn--compact" id="volume-floor-save-button" ' +
              'data-act="save-volume-floor"' + saveDisabled + '>' + saveLabel + '</button>' +
            '<button type="button" class="btn btn--ghost btn--compact" data-act="reset-volume-floor"' +
              resetDisabled + '>Reset floor</button>' +
            '<span class="headroom-readout" id="set-volume-floor-readout">' + fmtVolumeFloor(floor) + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="setting-row setting-row--stack">' +
          '<div class="setting-row__text">' +
            '<p class="setting-row__title">Extra headroom</p>' +
            '<p class="setting-row__hint">Digital attenuation for full-volume setups into your own amp. ' +
              'Leave at Off unless you hear clipping.</p>' +
          '</div>' +
          '<div class="headroom-control">' +
            '<input type="range" class="headroom-range" id="set-headroom" min="0" max="' + trimMax +
              '" step="0.5" value="' + trim + '" aria-label="Extra headroom in dB">' +
            '<span class="headroom-readout" id="set-headroom-readout">' + fmtTrim(trim) + '</span>' +
          '</div>' +
        '</div>' +
      '</details>' +
    '</section>';
  }
  function identityReportFromTopology(topology) {
    var targets = [];
    outputGroups(topology).forEach(function(group) {
      (Array.isArray(group.channels) ? group.channels : []).forEach(function(channel) {
        if (!channel || !channel.role) return;
        var assigned = channel.physical_output_index != null;
        targets.push({
          id: (group.id || '') + ':' + channel.role,
          speaker_group_id: group.id || '',
          speaker_label: group.label || group.id || '',
          role: channel.role,
          assigned: assigned,
          identity_verified: !!channel.identity_verified,
          physical_output_index: assigned ? channel.physical_output_index : null
        });
      });
    });
    if (!targets.length) return null;
    var assignedCount = targets.filter(function(target) { return target.assigned; }).length;
    var verifiedCount = targets.filter(function(target) {
      return target.assigned && target.identity_verified;
    }).length;
    return {
      kind: 'jts_output_channel_identity_report',
      status: assignedCount && verifiedCount === assignedCount ? 'verified' : 'needs_confirmation',
      assigned_channel_count: assignedCount,
      verified_channel_count: verifiedCount,
      unverified_channel_count: assignedCount - verifiedCount,
      targets: targets
    };
  }
  function outputIdentityReport() {
    return outputTopology.identity || identityReportFromTopology(currentOutputTopology());
  }
  function identityTargetFor(groupId, role) {
    var report = outputIdentityReport();
    var targets = report && Array.isArray(report.targets) ? report.targets : [];
    return targets.find(function(target) {
      return target.speaker_group_id === groupId && target.role === role;
    }) || null;
  }
  function addSubwooferToTopology(topology) {
    var next = baseOutputDraft(topology);
    if (!next || outputHasSubwoofer(next)) return next;
    var outputIndex = firstUnusedOutputIndex(next);
    if (outputIndex == null) return next;
    var groupId = nextSubwooferGroupId(next);
    next.speaker_groups = (next.speaker_groups || []).concat([{
      id: groupId,
      label: 'Subwoofer',
      kind: 'subwoofer',
      mode: 'subwoofer',
      position: {x: 0, y: -0.72, rotation_degrees: 0},
      channels: [outputChannel('subwoofer', outputIndex)]
    }]);
    next.routing = Object.assign({}, next.routing || {}, {
      subwoofer_group_ids: (next.routing && next.routing.subwoofer_group_ids || []).concat([groupId])
    });
    return next;
  }
  function savedDriverResearchHasPreviewInputs() {
    var draft = driverResearch.designDraft || {};
    var summary = draft.summary || {};
    if (driverResearch.dirty || draft.status !== 'ready_for_review') return false;
    return ['missing_driver_info_target_ids', 'missing_driver_info_roles',
      'missing_crossover_candidate_pairs'].every(function(field) {
      return !Array.isArray(summary[field]) || summary[field].length === 0;
    });
  }
  function driverResearchPreviewInputsReady(topology) {
    // Physical-target fields remain strict for browser edits. A clean saved
    // draft may also be ready because the backend can safely interpret older
    // role-only stereo data without copying it into target-specific edit rows.
    return driverResearchHasPreviewInputs(topology) ||
      savedDriverResearchHasPreviewInputs();
  }
  function setManualDriverField(targetId, field, value) {
    var setting = driverSetting(targetId);
    setting[field] = value;
    if (field === 'gain_offset_db') {
      setting.gain_offset_db_provenance = 'operator_pinned';
    }
    driverResearch.error = '';
    driverResearch.dirty = true;
    driverResearch.safetyDirty = true;
    driverResearch.editedDriverTargets[targetId] = true;
    invalidateDriverResearchBinding();
    // driver_class/pad_kind each gate which OTHER fields this row shows
    // (a radiating diameter or nothing; resistor inputs vs the direct-dB
    // input) -- unlike every other manual-driver field above, a selection
    // here must re-render immediately or the newly-relevant field stays
    // hidden until some unrelated action repaints the page. Mirrors
    // setOutputChannelDriverStyle's existing full-repaint-on-select pattern.
    if (field === 'driver_class' || field === 'pad_kind') render();
  }
  function refreshDriverResearchDerivedUi() {
    var topology = currentOutputTopology();
    var proposal = el('view-body').querySelector('[data-driver-proposal]');
    if (proposal) proposal.innerHTML = renderCrossoverPreviewCardBody(topology);
    var footer = el('view-body').querySelector('[data-driver-research-footer]');
    if (footer) footer.innerHTML = driverResearchStepFooterButtonHtml(topology);
    var echo = el('view-body').querySelector('[data-driver-echo]');
    if (echo) echo.innerHTML = renderDriverEchoBack(topology);
  }
  function manualSettingsPayload(topology) {
    var drivers = driverResearchTargets(topology).map(function(target) {
      var role = target.role;
      var setting = driverSetting(target.target_id);
      var out = {
        target_id: target.target_id,
        role: role,
        model: targetModel(target, topology)
      };
      [
        'sensitivity_db_2v83_1m',
        'nominal_impedance_ohm',
        'recommended_highpass_hz',
        // #2603: the low limit's slope condition travels with the frequency it
        // conditions. Dropping it here would leave the owner half-declared.
        'recommended_highpass_slope_db_per_octave',
        'recommended_lowpass_hz',
        'do_not_test_below_hz',
        'gain_offset_db',
        'radiating_diameter_mm'
      ].forEach(function(field) {
        var value = manualNumberValue(setting[field]);
        if (value != null) out[field] = value;
      });
      if (out.gain_offset_db != null) {
        out.gain_offset_db_provenance =
          setting.gain_offset_db_provenance || 'operator_pinned';
      }
      // Preserve absent versus explicitly chosen "unknown" for both
      // driver_class and enclosure_kind. An untouched saved payload must not
      // claim the operator selected "Not sure"; the server already treats an
      // absent class as unknown where a conservative fallback is required.
      if ((setting.driver_class || '').trim()) out.driver_class = setting.driver_class;
      if ((setting.notes || '').trim()) out.notes = String(setting.notes).trim();
      var hardBand = safetyBandFromSetting(setting, 'hard_excitation');
      var measurementBand = safetyBandFromSetting(setting, 'measurement');
      if (hardBand) out.hard_excitation_band_hz = hardBand;
      if (measurementBand) out.measurement_band_hz = measurementBand;
      out.required_protection_filters = protectionFiltersFromSetting(setting);
      var cabinet = cabinetFromSetting(setting);
      if (Object.keys(cabinet).length) out.cabinet = cabinet;
      var pad = padFromSetting(setting);
      if (pad) out.pad = pad;
      var limits = levelDurationLimitsFromSetting(setting);
      if (Object.keys(limits).length) out.level_duration_limits = limits;
      return out;
    }).filter(function(driver) {
      return driver.model ||
        driver.sensitivity_db_2v83_1m != null ||
        driver.nominal_impedance_ohm != null ||
        driver.recommended_highpass_hz != null ||
        driver.recommended_lowpass_hz != null ||
        driver.do_not_test_below_hz != null ||
        driver.gain_offset_db != null ||
        driver.radiating_diameter_mm != null ||
        driver.driver_class ||
        driver.cabinet ||
        driver.pad ||
        driver.hard_excitation_band_hz ||
        driver.measurement_band_hz ||
        driver.required_protection_filters.length ||
        driver.notes;
    });
    var candidates = activeCrossoverPairs(topology).map(function(pair) {
      var setting = crossoverSetting(pair);
      var frequency = manualNumberValue(setting.frequency_hz);
      if (frequency == null) return null;
      var candidate = {
        between_roles: pair,
        frequency_hz: frequency,
        filter_type: setting.filter_type || crossoverVocabulary.defaultFilterType,
        slope_db_per_octave:
          manualNumberValue(setting.slope_db_per_octave) || crossoverVocabulary.defaultSlope,
        confidence: 'medium',
        rationale: 'Operator-entered crossover setting.'
      };
      // Omit 'non-inverted'/unset so absent-in -> absent-out (mirrors the
      // server's own default and keeps an untouched draft byte-minimal).
      if (setting.lower_polarity === 'inverted') candidate.lower_polarity = 'inverted';
      if (setting.upper_polarity === 'inverted') candidate.upper_polarity = 'inverted';
      var delayMs = manualNumberValue(setting.delay_ms);
      var delayTarget = String(setting.delay_target_role || '').trim();
      // delayMs !== null (never truthiness) — 0.0 ms is a legitimate, server-
      // pinned value, not "no delay entered". Guarded by
      // manualCrossoverDelayValidationError before this ever runs, but stay
      // defensive here too: never emit a delay without a driver it targets.
      if (delayMs != null && (delayTarget === pair[0] || delayTarget === pair[1])) {
        candidate.delay_ms = Math.max(0, Math.min(20, delayMs));
        candidate.delay_target_role = delayTarget;
      }
      return candidate;
    }).filter(Boolean);
    return drivers.length || candidates.length
      ? {drivers: drivers, crossover_candidates: candidates}
      : null;
  }
  function applyDriverSafetyToSetting(driver, setting) {
    applySafetyBandToSetting(setting, 'hard_excitation', driver.hard_excitation_band_hz);
    applySafetyBandToSetting(setting, 'measurement', driver.measurement_band_hz);
    (Array.isArray(driver.required_protection_filters)
      ? driver.required_protection_filters : []).forEach(function(filter) {
      if (!filter || (filter.kind !== 'highpass' && filter.kind !== 'lowpass')) return;
      setting['required_' + filter.kind + '_cutoff_hz'] = filter.cutoff_hz;
      setting['required_' + filter.kind + '_min_slope_db_per_octave'] =
        filter.minimum_slope_db_per_octave;
      setting['required_' + filter.kind + '_family_or_equivalent'] =
        filter.family_or_equivalent || 'equivalent_or_steeper';
    });
    var cabinet = driver.cabinet || {};
    if (cabinet.enclosure_kind) setting.enclosure_kind = cabinet.enclosure_kind;
    if (cabinet.radiator_count != null) setting.radiator_count = cabinet.radiator_count;
    if (cabinet.effective_radiating_diameter_mm != null) {
      setting.effective_radiating_diameter_mm = cabinet.effective_radiating_diameter_mm;
    }
    if (cabinet.baffle_width_mm != null) setting.baffle_width_mm = cabinet.baffle_width_mm;
    var limits = driver.level_duration_limits || {};
    [
      'max_effective_peak_dbfs',
      'max_sweep_duration_s',
      'max_repeat_count',
      'minimum_cooldown_s'
    ].forEach(function(field) {
      if (limits[field] != null) setting[field] = limits[field];
    });
    // #1665: driver_class/radiating_diameter_mm are AI-researchable, so this
    // unpacks them the same as every other researched field above. pad never
    // appears in research JSON (it is operator-only), but IS present here
    // when `driver` is a persisted manual_settings.drivers[] record reloaded
    // after a save (ingestDesignDraft merges the whole record onto `setting`
    // first, so setting.pad already holds the server-computed
    // attenuation_db/effective_impedance_ohm before this runs -- see
    // renderDriverPadSettings' read-only readout, which reads setting.pad
    // directly rather than the pad_* input fields below).
    if (driver.driver_class) setting.driver_class = driver.driver_class;
    if (driver.radiating_diameter_mm != null) {
      setting.radiating_diameter_mm = driver.radiating_diameter_mm;
    }
    var pad = driver.pad || null;
    if (pad && pad.kind) {
      setting.pad_kind = pad.kind;
      if (pad.kind === 'direct_db') {
        if (pad.attenuation_db != null) setting.pad_attenuation_db = pad.attenuation_db;
      } else {
        if (pad.series_ohm != null) setting.pad_series_ohm = pad.series_ohm;
        if (pad.shunt_ohm != null) setting.pad_shunt_ohm = pad.shunt_ohm;
      }
    }
  }
  function applyDriverResearchToManualSettings(payload) {
    if (!payload || typeof payload !== 'object') return;
    var topology = currentOutputTopology();
    var targets = driverResearchTargets(topology);
    var driversByRole = {};
    (Array.isArray(payload.drivers) ? payload.drivers : []).forEach(function(driver) {
      if (!driver || !driver.role) return;
      var role = String(driver.role);
      var target = null;
      if (driver.target_id) {
        target = targets.find(function(item) {
          return item.target_id === String(driver.target_id);
        }) || null;
      } else {
        var roleTargets = targets.filter(function(item) { return item.role === role; });
        if (roleTargets.length === 1) target = roleTargets[0];
      }
      if (!target) return;
      if (targets.filter(function(item) { return item.role === role; }).length === 1) {
        driversByRole[role] = Object.assign({_target_id: target.target_id}, driver);
      }
      if (driver.model && !(driverResearch.inputs.target_models || {})[target.target_id]) {
        driverResearch.inputs.target_models[target.target_id] = String(driver.model);
      }
      var targetSetting = driverSetting(target.target_id);
      [
        'sensitivity_db_2v83_1m',
        'nominal_impedance_ohm',
        'recommended_highpass_hz',
        // #2603: the low limit's slope condition travels with the frequency it
        // conditions. Dropping it here would leave the owner half-declared.
        'recommended_highpass_slope_db_per_octave',
        'recommended_lowpass_hz',
        'do_not_test_below_hz',
        'gain_offset_db'
      ].forEach(function(field) {
        if (driver[field] != null) targetSetting[field] = driver[field];
      });
      if (driver.gain_offset_db != null) {
        targetSetting.gain_offset_db_provenance =
          driver.gain_offset_db_provenance || 'research_estimate';
      }
      // Physical installation choices belong to the operator. Research can
      // fill product geometry, but it cannot change the declared enclosure,
      // an explicitly chosen driver/loading class, or a resistor pad. Strip
      // those fields at this untrusted-import boundary; the shared apply
      // helper also handles trusted persisted records during reload.
      var researchDriver = Object.assign({}, driver);
      if (researchDriver.cabinet && typeof researchDriver.cabinet === 'object') {
        researchDriver.cabinet = Object.assign({}, researchDriver.cabinet);
        delete researchDriver.cabinet.enclosure_kind;
      }
      if (targetSetting.driver_class &&
          targetSetting.driver_class !== 'unknown') {
        delete researchDriver.driver_class;
      }
      delete researchDriver.pad;
      applyDriverSafetyToSetting(researchDriver, targetSetting);
    });
    // Pick ONE crossover per role-pair: the highest-confidence candidate with a
    // usable frequency (ties keep the first listed). The old code applied every
    // candidate last-write-wins, so a low-confidence value listed after the
    // recommended one became the form's "starting crossover" while the preview
    // chose the recommended one — the two surfaces then disagreed.
    var bestByPair = {};
    (Array.isArray(payload.crossover_candidates) ? payload.crossover_candidates : [])
      .forEach(function(candidate) {
        if (!candidate || !Array.isArray(candidate.between_roles) ||
            candidate.between_roles.length !== 2) return;
        if (candidateFrequency(candidate) == null) return;
        var key = pairRoleKey(candidate.between_roles);
        var current = bestByPair[key];
        if (!current ||
            candidateConfidenceRank(candidate) >
              candidateConfidenceRank(current.candidate)) {
          bestByPair[key] = {
            pair: candidate.between_roles.map(String),
            candidate: candidate
          };
        }
      });
    Object.keys(bestByPair).forEach(function(key) {
      var pick = bestByPair[key];
      var candidate = pick.candidate;
      var setting = crossoverSetting(pick.pair);
      var frequency = candidateFrequency(candidate);
      setting.frequency_hz = frequency;
      if (candidate.filter_type) setting.filter_type = String(candidate.filter_type);
      if (candidate.slope_db_per_octave != null) {
        setting.slope_db_per_octave = candidate.slope_db_per_octave;
      }
      // Research candidates may legitimately propose polarity/delay too;
      // _normalise_candidate validates server-side. Mirrors the filter_type/
      // slope copy above, including its pick.pair orientation assumption —
      // a reversed research JSON is a pre-existing, out-of-scope limitation.
      if (candidate.lower_polarity) setting.lower_polarity = String(candidate.lower_polarity);
      if (candidate.upper_polarity) setting.upper_polarity = String(candidate.upper_polarity);
      if (candidate.delay_ms != null) setting.delay_ms = candidate.delay_ms;
      if (candidate.delay_target_role) setting.delay_target_role = String(candidate.delay_target_role);
    });
    proposeSensitivityTrims(driversByRole);
  }
  function driverResearchCanPreparePreview() {
    var draftPayload = driverResearch.designDraft || {};
    var savedStatus = draftPayload.status || '';
    return savedStatus && savedStatus !== 'not_saved' && savedStatus !== 'unreadable' &&
      !driverResearch.dirty && driverResearchPreviewInputsReady(currentOutputTopology());
  }
  function driverResearchWorkingStatusLabel(status) {
    if (driverResearch.dirty) return 'editing';
    if (driverResearchPreviewInputsReady(currentOutputTopology())) return 'ready to preview';
    if (status === 'blocked') return 'needs speaker layout';
    if (status === 'unreadable') return 'needs review';
    if (status === 'needs_research') return 'needs crossover info';
    return 'working setup';
  }
  function driverResearchWorkingStatusClass(status) {
    if (!driverResearch.dirty && driverResearchPreviewInputsReady(currentOutputTopology())) {
      return ' status-pill--ready';
    }
    if (status === 'blocked' || status === 'unreadable') return ' status-pill--blocked';
    return '';
  }
  function ingestDesignDraft(payload, options) {
    options = options || {};
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    driverResearch.designDraft = payload;
    driverResearch.saving = false;
    if (!options.force && driverResearch.dirty) return;
    var inputs = payload.operator_inputs || {};
    ['full_range', 'woofer', 'mid', 'tweeter', 'subwoofer', 'notes'].forEach(function(key) {
      driverResearch.inputs[key] = inputs[key] || '';
    });
    driverResearch.inputs.target_models = Object.assign({}, inputs.target_models || {});
    driverResearch.settings = {drivers: {}, crossovers: {}};
    var manual = payload.manual_settings || {};
    (Array.isArray(manual.drivers) ? manual.drivers : []).forEach(function(driver) {
      if (!driver || !driver.role) return;
      var role = String(driver.role);
      var targetId = String(driver.target_id || '');
      if (!targetId) {
        var roleTargets = driverResearchTargets(currentOutputTopology()).filter(function(item) {
          return item.role === role;
        });
        if (roleTargets.length === 1) targetId = roleTargets[0].target_id;
      }
      if (!targetId) return;
      if (driver.model && !driverResearch.inputs.target_models[targetId]) {
        driverResearch.inputs.target_models[targetId] = String(driver.model);
      }
      driverResearch.settings.drivers[targetId] = Object.assign(
        {},
        driverResearch.settings.drivers[targetId] || {},
        driver
      );
      applyDriverSafetyToSetting(
        driver,
        driverResearch.settings.drivers[targetId]
      );
    });
    (Array.isArray(manual.crossover_candidates) ? manual.crossover_candidates : [])
      .forEach(function(candidate) {
        if (!candidate || !Array.isArray(candidate.between_roles) ||
            candidate.between_roles.length !== 2) return;
        var key = crossoverSettingKey(candidate.between_roles.map(String));
        driverResearch.settings.crossovers[key] = Object.assign(
          {},
          driverResearch.settings.crossovers[key] || {},
          candidate
        );
      });
    if (payload.driver_research) {
      driverResearch.importText = JSON.stringify(payload.driver_research, null, 2);
      try {
        driverResearch.parsed = summarizeDriverResearchPayload(payload.driver_research);
        driverResearch.importedPayload = payload.driver_research;
        driverResearch.error = '';
      } catch (e) {
        driverResearch.parsed = null;
        driverResearch.importedPayload = null;
        driverResearch.error = e.message;
      }
    } else {
      driverResearch.importText = '';
      driverResearch.parsed = null;
      driverResearch.importedPayload = null;
      driverResearch.error = '';
    }
    driverResearch.dirty = false;
    driverResearch.safetyDirty = false;
    driverResearch.editedDriverTargets = {};
    driverResearch.promptCopy.copied = false;
    driverResearch.promptCopy.selected = false;
    driverResearch.researchRequest = payload.driver_research_request || null;
  }
  async function fetchDesignDraft() {
    var resp = await fetch('./active-speaker/design-draft', {cache: 'no-store'});
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'speaker design draft failed');
    ingestDesignDraft(payload);
    return payload;
  }
  async function fetchCrossoverPreview() {
    var resp = await fetch('./active-speaker/crossover-preview', {cache: 'no-store'});
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'crossover preview failed');
    ingestCrossoverPreview(payload);
    return payload;
  }
  function outputRoleStatusText(group, channel) {
    if (!channel || channel.physical_output_index == null) return 'No DAC output assigned yet.';
    if (!channel.identity_verified) {
      return 'Play this driver quietly, then confirm what you hear.';
    }
    var proof = driverMeasurementCaptured(
      group && group.id || '',
      channel.role || ''
    );
    if (!outputChannelGuardReady(channel)) {
      return 'Confirmed. JTS will add the tweeter guard before any sound starts.';
    }
    if (channel.protection_required) {
      return channel.protection_status === 'present' ?
        'Confirmed. Extra protection noted; tests still start very quiet.' :
        'Confirmed. JTS will start it very quiet.';
    }
    return proof ? 'Heard and confirmed.' : 'Confirmed. Play and confirm the driver.';
  }
  function renderOutputTopologySetup() {
    return '<div class="setting-row setting-row--stack output-setup">' +
      '<div class="output-setup__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Active crossover setup</p>' +
          '<p class="setting-row__hint">Choose layout, set crossover values, confirm outputs, then validate.</p>' +
        '</div></div>' +
      renderOutputTopologyBody() +
    '</div>';
  }
  function renderOutputHardwareRefresh() {
    var topology = currentOutputTopology();
    return '<div class="output-setup__actions">' +
      '<button type="button" class="btn btn--ghost" data-act="refresh-output-topology"' +
        (outputTopology.loading ? ' disabled' : '') + '>' + (topology ? 'Refresh hardware' : 'Find hardware') + '</button>' +
    '</div>';
  }
  function outputIdentityComplete() {
    if (outputTopology.dirty) return false;
    var report = outputIdentityReport();
    if (!report) return false;
    return Number(report.assigned_channel_count || 0) > 0 &&
      Number(report.unverified_channel_count || 0) === 0;
  }
  function measurementSummary() {
    return activeSpeaker.measurements && activeSpeaker.measurements.summary || {};
  }
  function commissioningDriverChecksComplete() {
    var view = activeSpeaker.commissioningView || {};
    var checks = view.driver_checks && typeof view.driver_checks === 'object' ?
      view.driver_checks : {};
    return checks.complete === true;
  }
  function driverChecksComplete() {
    var summary = measurementSummary();
    return summary.driver_checks_complete === true ||
      summary.driver_measurements_complete === true ||
      commissioningDriverChecksComplete();
  }
  function driverTargetProofComplete() {
    var view = activeSpeaker.commissioningView || {};
    var proof = view.driver_target_proof && typeof view.driver_target_proof === 'object' ?
      view.driver_target_proof : {};
    return proof.complete === true ||
      (outputIdentityComplete() && driverChecksComplete());
  }
  function summedValidationComplete() {
    var view = activeSpeaker.commissioningView || {};
    var summed = view.summed_validation && typeof view.summed_validation === 'object' ?
      view.summed_validation : {};
    return measurementSummary().summed_validation_complete === true ||
      summed.complete === true;
  }
  function baselineProfileAppliedRecord() {
    // The rebuild's own status cannot reach 'applied' for a measured profile;
    // `applied_profile_stands` is the payload's verdict. See ADR-0195.
    var profile = activeSpeaker.baselineProfile || {};
    if (profile.applied_profile_stands === true) {
      var anchor = profile.applied_recomposition_profile;
      if (anchor && typeof anchor === 'object') return anchor;
    }
    // The save-and-apply response replaces this state with the record it just
    // wrote — the record itself, not a rebuild, so it carries no verdict and
    // its own status is the answer.
    return profile.status === 'applied' ? profile : null;
  }
  function baselineProfileApplied() {
    return baselineProfileAppliedRecord() !== null;
  }
  function appliedProfileCorrections(record) {
    // What the basic door would not re-emit, named for the household.
    if (!record) return '';
    var parts = [];
    var linearization = record.linearization;
    if (linearization && typeof linearization === 'object' &&
      Object.keys(linearization).length) {
      parts.push('its per-driver linearization');
    }
    if (Array.isArray(record.blend_correction) && record.blend_correction.length) {
      parts.push('its blend correction');
    }
    return parts.join(' and ');
  }
  function baselineProfileRevalidation() {
    var profile = activeSpeaker.baselineProfile || {};
    return profile.revalidation && typeof profile.revalidation === 'object' ?
      profile.revalidation : {};
  }
  function baselineProfileNeedsRevalidation() {
    return baselineProfileRevalidation().required === true;
  }
  function outputStepContext(topology) {
    return {
      hasLayout: outputGroups(topology).length > 0,
      dirty: outputTopology.dirty,
      hardwareMatchesSaved: !outputHardwareMismatch(topology),
      driverResearchSatisfied: driverResearchFlowComplete(topology),
      outputIdentityComplete: outputIdentityComplete(),
      driverTargetProofComplete: driverTargetProofComplete(),
      driverChecksComplete: driverChecksComplete(),
      summedValidationComplete: summedValidationComplete(),
      baselineProfileApplied: baselineProfileApplied(),
      baselineProfileNeedsRevalidation: baselineProfileNeedsRevalidation()
    };
  }
  function commissioningStepView(step) {
    var view = activeSpeaker.commissioningView || {};
    var steps = Array.isArray(view.steps) ? view.steps : [];
    for (var i = 0; i < steps.length; i += 1) {
      if (String(steps[i].id || '') === String(step || '')) return steps[i];
    }
    return null;
  }
  function commissioningStepState(step) {
    var item = commissioningStepView(step);
    var state = item && String(item.status || '');
    return state === 'done' || state === 'active' || state === 'todo' ||
      state === 'not_required' ? state : '';
  }
  // A rung this speaker's shape will never run (a full-range passive speaker
  // has no combined driver test and no active speaker profile). The backend
  // coordinator owns the decision; the page only renders it. Without this the
  // unknown status fell through commissioningStepState to the client-side
  // guess, which put the step back on 'todo' and re-opened the dead end.
  function commissioningStepNotRequired(step) {
    return commissioningStepState(step) === 'not_required';
  }
  function commissioningCurrentStep() {
    var view = activeSpeaker.commissioningView || {};
    var step = String(view.current_step || '');
    return commissioningStepState(step) ? step : '';
  }
  function outputStepState(step, topology) {
    if (!outputTopology.dirty && !outputHardwareMismatch(topology) && !driverResearch.dirty) {
      var backendState = commissioningStepState(step);
      if (backendState) return backendState;
    }
    return activeSpeakerStepState(step, outputStepContext(topology));
  }
  function defaultOutputStep() {
    if (!outputTopology.dirty && !outputHardwareMismatch(currentOutputTopology()) &&
        !driverResearch.dirty) {
      var backendStep = commissioningCurrentStep();
      if (backendStep) return backendStep;
    }
    return defaultActiveSpeakerStep(outputStepContext(currentOutputTopology()));
  }
  function outputStepIsOpen(step, topology) {
    return (outputPage.stepOverride || defaultOutputStep()) === step;
  }
  function outputStepCanOpen(step, topology) {
    if (outputStepState(step, topology) !== 'todo') return true;
    // Dirty output remaps are saved from the map card itself.
    return step === 'map' && outputTopology.dirty && outputPage.stepOverride === 'map';
  }
  function openOutputStep(step) {
    outputPage.stepOverride = step;
    render();
  }
  function outputStepHint(step, fallback) {
    var item = commissioningStepView(step);
    return String(item && item.message || fallback || '');
  }
  function renderOutputStepCard(step, title, hint, topology, bodyHtml, footerHtml) {
    var state = outputStepState(step, topology);
    var open = outputStepIsOpen(step, topology);
    var done = state === 'done';
    return '<details class="output-step output-step--' + escapeHtml(state) + '"' +
      ' data-output-step="' + escapeHtml(step) + '"' +
      (open ? ' open' : '') + '>' +
      '<summary class="output-step__summary">' +
        '<span class="output-step__marker" aria-hidden="true">' + (done ? '&#10003;' : '') + '</span>' +
        '<span class="output-step__text"><strong>' + escapeHtml(title) + '</strong>' +
          '<span>' + escapeHtml(hint) + '</span></span>' +
        '<span class="output-step__chevron" aria-hidden="true"></span>' +
      '</summary>' +
      '<div class="output-step__body">' + bodyHtml +
        (footerHtml ? '<div class="output-step__footer">' + footerHtml + '</div>' : '') +
      '</div>' +
    '</details>';
  }
  function renderOutputStepButton(step, label, primary, disabled) {
    return '<button type="button" class="btn ' + escapeHtml(primary ? 'btn--primary' : 'btn--ghost') +
      '" data-act="output-step-next" data-step="' + escapeHtml(step) + '"' +
      (disabled ? ' disabled' : '') + '>' +
      escapeHtml(label) + '</button>';
  }
  // Render a {label, primary, disabled, act, step} footer descriptor from
  // commissioningStepFooter. An 'output-step-next' act carries the step; a
  // bare act (save-driver-design / prepare-crossover-preview) is a direct
  // click; an empty act is a disabled waiting affordance.
  function renderStepFooterButton(desc) {
    desc = desc || {};
    if (desc.act === 'output-step-next') {
      return renderOutputStepButton(desc.step || '', desc.label, desc.primary, desc.disabled);
    }
    return '<button type="button" class="btn ' +
      escapeHtml(desc.primary !== false ? 'btn--primary' : 'btn--ghost') + '"' +
      (desc.act ? ' data-act="' + escapeHtml(desc.act) + '"' : '') +
      (desc.disabled ? ' disabled' : '') + '>' +
      escapeHtml(desc.label || '') + '</button>';
  }
  function driverResearchStepFooterButtonHtml(topology) {
    // Clean-draft readiness comes from the backend commissioning view-model;
    // the client fallback covers only the unsaved-edit cases it cannot see.
    // A pending layout save blocks first; a draft save-in-flight is "Saving";
    // an unsaved draft offers "Save values" (same act as the backend path).
    var clientFallback = outputTopology.dirty ?
      {label: 'Save layout first', primary: true, disabled: true} :
      (driverResearch.saving ?
        {label: 'Saving', primary: true, disabled: true} :
        {label: 'Save values', primary: true, act: 'save-driver-design'});
    return renderStepFooterButton(commissioningStepFooter('research',
      activeSpeaker.commissioningView, {
        layoutDirty: outputTopology.dirty,
        draftDirty: driverResearch.dirty,
        saving: driverResearch.saving,
        previewInputsReady: driverResearchPreviewInputsReady(topology),
        clientFallback: clientFallback
      }));
  }
  function renderDriverResearchStepFooter(topology) {
    return '<span data-driver-research-footer>' +
      driverResearchStepFooterButtonHtml(topology) +
    '</span>';
  }
  function renderOutputMapStepFooter() {
    var clientFallback = {label: 'Save', primary: true, disabled: false,
      act: 'save-output-topology'};
    return renderStepFooterButton(commissioningStepFooter('map',
      activeSpeaker.commissioningView, {
        layoutDirty: outputTopology.dirty,
        clientFallback: clientFallback
      }));
  }
  function outputTemplateAxesForTopology(topology) {
    var mainGroups = outputGroups(topology).filter(function(group) {
      return group.kind !== 'subwoofer' && group.mode !== 'subwoofer';
    });
    if (!mainGroups.length) {
      return {
        layout: outputPage.templateDraftAxes.layout || '',
        speakerMode: outputPage.templateDraftAxes.speakerMode || ''
      };
    }
    var kinds = mainGroups.map(function(group) { return group.kind; });
    var layout = (kinds.indexOf('left') >= 0 || kinds.indexOf('right') >= 0)
      ? 'stereo'
      : 'mono';
    var mode = mainGroups.length ? mainGroups[0].mode : 'full_range_passive';
    var speakerMode = {
      full_range_passive: 'passive',
      active_2_way: 'active_2way',
      active_3_way: 'active_3way'
    }[mode] || 'passive';
    return {layout: layout, speakerMode: speakerMode};
  }
  function outputTemplateChoiceDisabled(count, axis, value, axes) {
    count = Number(count) || 0;
    var topology = currentOutputTopology();
    var hasSub = outputHasSubwoofer(topology);
    var layout = axis === 'layout' ? value : axes.layout;
    var speakerMode = axis === 'speaker-mode' ? value : axes.speakerMode;
    if (layout && speakerMode) {
      var template = outputTemplateDefinition(outputTemplateKindFromAxes(layout, speakerMode));
      return outputTemplateUnavailableReason(template, topology, hasSub);
    }
    if (axis === 'layout') {
      return ['passive', 'active_2way', 'active_3way'].every(function(mode) {
        var template = outputTemplateDefinition(outputTemplateKindFromAxes(value, mode));
        return !!outputTemplateUnavailableReason(template, topology, hasSub);
      }) ? 'No available speaker type fits this install for ' + value + '.' : '';
    }
    var mono = outputTemplateDefinition(outputTemplateKindFromAxes('mono', value));
    var stereo = outputTemplateDefinition(outputTemplateKindFromAxes('stereo', value));
    return [mono, stereo].every(function(template) {
      return !!outputTemplateUnavailableReason(template, topology, hasSub);
    }) ? 'No available speaker count fits this install for ' + value + '.' : '';
  }
  function outputTemplateAxisButton(axis, value, label, hint, selected, disabled) {
    var disabledReason = disabled ? String(disabled) : '';
    return '<button type="button" class="output-template-option" data-act="output-template-axis" ' +
      'data-axis="' + escapeHtml(axis) + '" data-value="' + escapeHtml(value) + '" ' +
      'aria-pressed="' + (selected ? 'true' : 'false') + '"' +
      (disabledReason ? ' disabled title="' + escapeHtml(disabledReason) + '"' : '') + '>' +
        '<strong>' + escapeHtml(label) + '</strong>' +
        '<span>' + escapeHtml(hint) + '</span>' +
      '</button>';
  }
  function renderOutputSetupTemplates(topology) {
    var hardware = outputHardware(topology);
    var count = Number(hardware && hardware.physical_output_count) || 0;
    var axes = outputTemplateAxesForTopology(topology);
    var selectedTemplate = outputTemplateDefinition(
      outputTemplateKindFromAxes(axes.layout, axes.speakerMode)
    );
    var hasSub = outputHasSubwoofer(topology);
    var selectedLabel = selectedTemplate
      ? selectedTemplate.label + (hasSub ? ' + subwoofer' : '')
      : (axes.layout || axes.speakerMode
        ? 'Choose ' + (axes.layout ? 'speaker type' : 'mono or stereo')
        : 'Choose layout');
    var outputCount = selectedTemplate
      ? selectedTemplate.minOutputs + (hasSub ? 1 : 0)
      : 0;
    var selectedIssue = selectedTemplate ? outputTemplateUnavailableReason(
      selectedTemplate,
      topology,
      hasSub
    ) : '';
    var layoutChoices = [
      {value: 'mono', label: 'Mono', hint: 'One speaker or cabinet'},
      {value: 'stereo', label: 'Stereo', hint: 'Left and right speakers'}
    ];
    var speakerChoices = [
      {value: 'passive', label: 'Passive', hint: 'Full-range output per speaker'},
      {value: 'active_2way', label: 'Active 2-way', hint: 'Woofer + tweeter'},
      {value: 'active_3way', label: 'Active 3-way', hint: 'Woofer + mid + tweeter'}
    ];
    return '<div class="output-card output-card--templates">' +
      '<div class="output-card__head"><div><p class="output-card__title">Main speakers</p>' +
        '<p class="setting-row__hint">Choose what you are wiring.</p></div></div>' +
      '<div class="output-template-axes">' +
        '<div class="output-template-axis">' +
          '<p class="output-template-axis__label">Speaker count</p>' +
          '<div class="output-template-options output-template-options--layout">' +
            layoutChoices.map(function(choice) {
              return outputTemplateAxisButton(
                'layout',
                choice.value,
                choice.label,
                choice.hint,
                axes.layout === choice.value,
                outputTemplateChoiceDisabled(count, 'layout', choice.value, axes)
              );
            }).join('') +
          '</div>' +
        '</div>' +
        '<div class="output-template-axis">' +
          '<p class="output-template-axis__label">Speaker type</p>' +
          '<div class="output-template-options output-template-options--mode">' +
            speakerChoices.map(function(choice) {
              return outputTemplateAxisButton(
                'speaker-mode',
                choice.value,
                choice.label,
                choice.hint,
                axes.speakerMode === choice.value,
                outputTemplateChoiceDisabled(count, 'speaker-mode', choice.value, axes)
              );
            }).join('') +
          '</div>' +
        '</div>' +
      '</div>' +
      '<dl class="active-speaker-facts output-facts output-template-summary">' +
        '<div><dt>Selected setup</dt><dd>' + escapeHtml(selectedLabel) + '</dd></div>' +
        '<div><dt>Outputs needed</dt><dd>' + escapeHtml(
          outputCount ? String(outputCount) + ' of ' + String(count || 0) + ' available' : 'Choose a setup'
        ) + '</dd></div>' +
      '</dl>' +
      (selectedIssue
        ? '<p class="setting-row__hint output-template-warning">' + escapeHtml(selectedIssue) + '</p>'
        : '') +
    '</div>';
  }
  function renderOutputSubwooferCard(topology) {
    var hasLayout = outputGroups(topology).length > 0;
    var hasSub = outputHasSubwoofer(topology);
    var nextOutput = firstUnusedOutputIndex(topology);
    var axes = outputTemplateAxesForTopology(topology);
    var selectedTemplate = outputTemplateDefinition(
      outputTemplateKindFromAxes(axes.layout, axes.speakerMode)
    );
    var addIssue = outputTemplateUnavailableReason(selectedTemplate, topology, true);
    var disabled = !hasLayout || (!hasSub && (nextOutput == null || addIssue));
    var nextOutputLabel = null;
    var outputs = outputHardware(topology) && Array.isArray(outputHardware(topology).outputs)
      ? outputHardware(topology).outputs : [];
    outputs.forEach(function(output) {
      if (Number(output.index) === Number(nextOutput)) nextOutputLabel = output.human_label;
    });
    var hint = hasSub
      ? 'Subwoofer is included in this draft. Remove it to free that output lane.'
      : (!hasLayout
        ? 'Choose a speaker layout first, then add a subwoofer if you have a spare amplifier channel.'
        : (nextOutput == null
        ? 'No unused physical output is available for a subwoofer in this layout.'
        : (addIssue || 'Adds one subwoofer group on ' + (nextOutputLabel || ('DAC output ' + (Number(nextOutput) + 1))))));
    return '<div class="output-card output-card--subwoofer">' +
      '<div class="output-card__head"><div><p class="output-card__title">Subwoofer add-on</p>' +
        '<p class="setting-row__hint">Optional local sub output.</p></div>' +
        '<span class="status-pill' + (hasSub ? ' status-pill--ready' : '') + '">' + escapeHtml(hasSub ? 'added' : 'optional') + '</span></div>' +
      '<p class="setting-row__hint">' + escapeHtml(hint) + '</p>' +
      (hasSub ? renderSubwooferCrossoverControl(topology) : '') +
      '<div class="output-setup__actions">' +
        '<button type="button" class="btn btn--ghost btn--compact" data-act="toggle-output-subwoofer" data-mode="' +
          escapeHtml(hasSub ? 'remove' : 'add') + '"' + (disabled ? ' disabled' : '') + '>' +
          escapeHtml(hasSub ? 'Remove' : 'Add local sub') + '</button>' +
      '</div>' +
    '</div>';
  }
  function renderDriverResearchSummary(options) {
    options = options || {};
    var saved = driverResearch.designDraft || {};
    var savedStatus = saved.status || '';
    var topology = currentOutputTopology();
    var safetyRoles = driverSafetyNoteRoles(topology);
    var safetyProfile = saved.driver_safety_profile || {};
    var safetyEvaluation = saved.driver_safety_profile_evaluation || {};
    var safetyStatus = safetyEvaluation.status || safetyProfile.status || 'missing';
    var safetyReady = !driverResearch.safetyDirty &&
      safetyEvaluation.confirmed_and_current === true;
    var savedHtml =
      '<div class="driver-research__summary driver-research__summary--saved">' +
        '<span class="status-pill' + driverResearchWorkingStatusClass(savedStatus) + '">' +
          escapeHtml(driverResearchWorkingStatusLabel(savedStatus)) + '</span>' +
        '<p class="setting-row__hint">' + escapeHtml(workingSetupSummary(topology)) + '</p>' +
        (safetyRoles.length ? '<p class="setting-row__hint">' + escapeHtml(
          'Driver safety notes captured for ' + roleSentenceText(safetyRoles) + '.'
        ) + '</p>' : '') +
        '<p class="setting-row__hint">Safety profile: ' + escapeHtml(
          safetyReady ? 'declared for the current outputs' :
            (driverResearch.safetyDirty ? 'save your current edits to update it' :
            (safetyStatus === 'stale' ? 'the outputs changed; save the visible values again' :
              (safetyStatus === 'incomplete' ?
                (driverSafetyConflicts(safetyEvaluation.reasons).length ?
                  'resolve the limits that do not line up' :
                  'add the missing limits') :
                'save the visible limits again')))
        ) + '.</p>' +
      '</div>';
    if (driverResearch.error) {
      return savedHtml +
        '<p class="setting-row__hint driver-research__error">' +
        escapeHtml(driverResearch.error) + '</p>';
    }
    if (options.savedOnly) return savedHtml;
    if (!driverResearch.parsed) {
      return savedHtml +
        '<p class="setting-row__hint">Paste the JSON code block the assistant returns to sanity-check the shape. JTS will not apply it automatically.</p>';
    }
    var summary = driverResearch.parsed;
    return savedHtml + '<div class="driver-research__summary">' +
      '<span class="status-pill status-pill--ready">import ready</span>' +
      '<p class="setting-row__hint">' + escapeHtml(
        'Imported driver notes for ' + roleSentenceText(summary.roles) +
        '. Review them before updating the working setup.'
      ) + '</p>' +
      (summary.schemaVersion === 2 ? '<p class="setting-row__hint">' + escapeHtml(
        'Target-bound research includes ' + summary.provenanceFieldCount +
        ' sourced field assertions and ' + summary.unknownCount + ' explicit unknowns.'
      ) + '</p>' : '<p class="setting-row__hint">Legacy research is advisory only and cannot satisfy the confirmed safety-profile contract by itself.</p>') +
      (summary.warnings.length ? '<div class="driver-research__notes">' +
        '<p class="setting-row__title">Review notes</p>' +
        '<ul>' + summary.warnings.map(function(warning) {
          return '<li>' + escapeHtml(String(warning)) + '</li>';
        }).join('') + '</ul></div>' : '') +
    '</div>';
  }
  function renderDriverResearchAiHelper(topology) {
    var promptReady = driverResearchPromptReady(topology);
    var copyState = promptCopyState(driverResearch.promptCopy);
    var promptSelected = copyState.selected;
    var promptClass = copyState.promptClass;
    var promptButtonLabel = copyState.label;
    return '<section class="driver-research__section driver-research__ai">' +
      '<div><h3 class="setting-row__title">Research your components</h3>' +
        '<p class="setting-row__hint">Copy the populated prompt, use it with the research assistant of your choice, then paste the JSON response here.</p></div>' +
      '<div class="driver-research__grid driver-research__grid--ai">' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<div><p class="setting-row__title">1. Copy the prompt</p>' +
              '<p class="setting-row__hint">The button unlocks after every component has a model and its enclosure or tweeter type is selected. Build notes are optional.</p></div>' +
            '<button type="button" class="btn btn--ghost" data-act="copy-driver-research-prompt"' +
              (promptReady ? '' : ' disabled') + '>' +
              escapeHtml(promptButtonLabel) + '</button>' +
          '</div>' +
          '<textarea id="driver-research-prompt" class="' + promptClass + '" readonly ' +
            (promptSelected ? 'rows="6" ' : '') +
            'aria-label="Driver research prompt">' +
            escapeHtml(driverResearchPrompt(topology)) + '</textarea>' +
        '</div>' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<div><p class="setting-row__title">2. Paste the response</p>' +
              '<p class="setting-row__hint">JTS loads the proposed values into the working setup for your review. Nothing is applied to the speaker.</p></div>' +
            '<div class="driver-research__actions">' +
              '<button type="button" class="btn btn--primary" data-act="parse-driver-research">Load information</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="driver-research-import" class="driver-research__textarea driver-research__textarea--compact" data-driver-import ' +
            'rows="4" placeholder="{...}" aria-label="Driver research JSON result">' +
            escapeHtml(driverResearch.importText || '') + '</textarea>' +
          '<div id="driver-research-import-summary">' + renderDriverResearchSummary() + '</div>' +
        '</div>' +
      '</div>' +
      // Stable container so a manual edit can repaint just this panel. A
      // number input's own keystrokes must not trigger a full render (focus
      // loss), but leaving the echo showing a value the operator has already
      // changed is the exact dishonesty this panel exists to end -- so it gets
      // the same targeted refresh [data-driver-proposal] has.
      '<div data-driver-echo>' + renderDriverEchoBack(topology) + '</div>' +
    '</section>';
  }
  // Saving the declaration IS declaring it, so there is no confirm control and
  // nothing to un-confirm. What remains is the set of states in which the
  // server still refuses a measurement — 'incomplete', 'stale', 'malformed' —
  // each of which needs a DIFFERENT edit before a save can succeed. This
  // resolves that state once so the hoisted callout and the Advanced editor
  // cannot disagree about whether the declared values are usable.
  function driverSafetyReviewState(topology) {
    var draft = driverResearch.designDraft || {};
    var evaluation = draft.driver_safety_profile_evaluation || {};
    var profile = draft.driver_safety_profile || {};
    var status = String(evaluation.status || profile.status || '');
    return {
      // 'missing' stays out: a speaker with no active crossover pair has no
      // declaration to review, and the callout would be pure noise.
      needsReview: !!topology && !!status && status !== 'missing' &&
        evaluation.confirmed_and_current !== true,
      status: status,
      reasons: Array.isArray(evaluation.reasons) ? evaluation.reasons : []
    };
  }
  function renderDriverSafetyReviewCallout(topology) {
    var state = driverSafetyReviewState(topology);
    if (!state.needsReview) return '';
    // No estimate COUNT here any more (#2195). A tally told the operator how
    // many numbers to distrust without saying which, so it could only produce
    // unease. "Here's what we're running with" below the paste box names every
    // value, its published/estimated badge, and its source instead.
    return '<div class="driver-research__section driver-research__confirm" id="' +
        CONFIRM_SAFETY_ANCHOR_ID + '">' +
      '<div><h3 class="setting-row__title">Review the safety limits</h3>' +
        '<p class="setting-row__hint">' +
          escapeHtml(driverSafetyReviewHint(state)) + '</p>' +
        '</div>' +
    '</div>';
  }
  // Deep link from the measurement wizard's profile hard stop. A bare fragment
  // is not enough: the callout lives inside a collapsible step card that is
  // only open when it is the current step, so this opens the owning step,
  // re-renders, and then scrolls the callout into view. No-ops when there is
  // nothing to review, so a stale bookmark cannot yank an unrelated page into
  // the component step.
  function applySafetyLimitsDeepLink() {
    if (window.location.hash !== '#' + CONFIRM_SAFETY_ANCHOR_ID) return;
    if (!driverSafetyReviewState(currentOutputTopology()).needsReview) return;
    outputPage.stepOverride = 'research';
    render();
    var node = document.getElementById(CONFIRM_SAFETY_ANCHOR_ID);
    if (node && typeof node.scrollIntoView === 'function') {
      node.scrollIntoView({block: 'center'});
    }
  }
  function renderDriverResearchCard(topology) {
    return '<div class="output-card output-card--driver-research">' +
      '<div class="output-card__head"><div><p class="output-card__title">Component setup</p>' +
        '<p class="setting-row__hint">Start with what is physically installed. JTS uses these choices as authoritative context, not facts for AI to guess.</p></div></div>' +
      renderDriverSafetyReviewCallout(topology) +
      renderDriverSafetyWarnings() +
      '<div class="driver-research__section">' +
        '<h3 class="setting-row__title">Your components</h3>' +
        renderComponentSettings(topology) +
      '</div>' +
      renderBuildNotes() +
      renderDriverResearchAiHelper(topology) +
      renderCrossoverPreviewCard(topology) +
      '<details class="driver-research__advanced-editor" data-driver-advanced' +
        (driverAdvancedOpen ? ' open' : '') + '>' +
        '<summary><span>Advanced</span><small>Review and edit every research value, safety limit, and crossover detail.</small></summary>' +
        '<div class="driver-research__advanced-body">' +
          '<section class="driver-research__advanced-section">' +
            '<div><h3 class="setting-row__title">Driver values</h3>' +
              '<p class="setting-row__hint">Research-populated values remain editable per physical output.</p></div>' +
            renderAdvancedDriverSettings(topology) +
          '</section>' +
          '<section class="driver-research__advanced-section">' +
            '<div><h3 class="setting-row__title">Crossover points</h3>' +
              '<p class="setting-row__hint">Edit the proposed split, filter, slope, polarity, and delay.</p></div>' +
            renderManualCrossoverSettings(topology) +
          '</section>' +
        '</div>' +
      '</details>' +
    '</div>';
  }
  function renderCrossoverPreviewCardBody(topology) {
    var payload = crossoverPreview.payload || {};
    // A prepared preview belongs to the last saved working draft. Any visible
    // component/crossover edit makes it stale immediately, so the prominent
    // summary must echo the current working values until a new preview is
    // prepared rather than showing an older saved proposal as "ready".
    var hasPreparedGroups = !driverResearch.dirty &&
      Array.isArray(payload.groups) && payload.groups.length;
    var displayPayload = hasPreparedGroups ? payload : {};
    var summary = displayPayload.summary || {};
    var readyCount = crossoverPreviewReadyCount(displayPayload);
    var warningIssues = crossoverPreviewReviewIssues(displayPayload.issues);
    var laterSafetyCount = Math.max(0, Number(summary.blocker_count || 0));
    var hasPreviewInputs = driverResearchPreviewInputsReady(topology);
    var needsCrossover = activeCrossoverPairs(topology).length > 0;
    var label = !needsCrossover ? 'not needed' :
      (hasPreparedGroups ? crossoverPreviewDisplayStatus(payload) :
        (hasPreviewInputs ? 'working proposal' : 'waiting for information'));
    var canPrepare = hasPreviewInputs && !outputTopology.dirty && !driverResearch.saving;
    var hint = !needsCrossover ?
      'A single full-range driver does not need an active crossover.' :
      (canPrepare ?
      'The working values below are ready to save and turn into a no-audio preview.' :
      (driverResearch.saving
        ? 'Working setup is updating before the preview.'
        : (outputTopology.dirty
        ? 'Save the speaker layout before preparing a crossover preview.'
        : driverResearchMissingPreviewMessage(topology))));
    if (crossoverPreview.error) hint = crossoverPreview.error;
    return '<div class="output-card__head"><div><h3 class="output-card__title">Proposed starting crossover</h3>' +
        '<p class="setting-row__hint">' + escapeHtml(hint) + '</p></div>' +
        '<span class="status-pill' + previewStatusClass(label) + '">' + escapeHtml(label) + '</span></div>' +
      (hasPreparedGroups
        ? renderCrossoverPreviewRows(payload)
        : renderWorkingCrossoverRows(topology)) +
      '<p class="setting-row__hint">' + escapeHtml(
        (readyCount > 0 ? 'Ready to preview ' + String(readyCount) +
        ' crossover split' + (readyCount === 1 ? '' : 's') + '. ' :
        (needsCrossover ? 'Needs crossover info. ' : 'No active crossover is required. ')) +
        String(warningIssues.length) + ' review note' +
        (warningIssues.length === 1 ? '' : 's') + '. No filters are active yet.' +
        (laterSafetyCount ? ' JTS still checks the setup before any sound.' : '')
      ) + '</p>' +
      renderPreviewIssues(warningIssues);
  }
  function renderCrossoverPreviewCard(topology) {
    return '<section class="driver-research__section driver-research__proposal" ' +
      'data-driver-proposal>' +
      renderCrossoverPreviewCardBody(topology) +
    '</section>';
  }
  function renderOutputTopologyBody() {
    if (outputTopology.loading && !currentOutputTopology()) {
      return '<p class="setting-row__hint">Loading output topology…</p>';
    }
    if (outputTopology.error) {
      return '<div class="output-error">' +
        '<span class="status-pill status-pill--blocked">Active crossover setup unavailable</span>' +
        '<p class="setting-row__hint">' + escapeHtml(outputTopology.error) + '</p>' +
        renderOutputHardwareRefresh() +
      '</div>';
    }
    var topology = currentOutputTopology();
    if (!topology) {
      return '<div class="output-empty">' +
        '<p class="setting-row__hint">Refresh hardware to start a speaker layout.</p>' +
        renderOutputHardwareRefresh() +
      '</div>';
    }
    var layoutStatusValue = outputTopology.dirty ? 'draft' : 'layout ready';
    return '<div class="output-layout">' +
      renderOutputStepCard(
        'layout',
        'Choose speaker layout',
        outputStepHint('layout', 'Choose speakers and active or passive wiring.'),
        topology,
        renderOutputSetupTemplates(topology) +
          renderOutputSubwooferCard(topology) +
          renderOutputHardwareCard(topology, layoutStatusValue),
        renderOutputHardwareRefresh() +
          renderOutputStepButton('layout',
          outputTopology.dirty ? 'Save' : 'Continue',
          true)
      ) +
      renderOutputStepCard(
        'research',
        'Add your components',
        outputStepHint('research', 'Describe each installed driver, then research a starting crossover.'),
        topology,
        renderDriverResearchCard(topology),
        renderDriverResearchStepFooter(topology)
      ) +
      renderOutputStepCard(
        'map',
        'Confirm outputs',
        outputStepHint('map', 'Assign DAC channels, then play each driver quietly.'),
        topology,
        renderCrossChildNoticeCard(topology) +
          renderOutputStageCard(topology) +
          renderOutputGroupsCard(topology) +
          renderOutputIdentityCard(),
        renderOutputMapStepFooter()
      ) +
      renderOutputStepCard(
        'safety',
        'Test combined drivers',
        outputStepHint('safety', 'Play the saved crossover with all confirmed drivers.'),
        topology,
        commissioningStepNotRequired('safety')
          ? renderStepNotRequiredCard(
              'safety',
              'This speaker has no combined driver test to run.')
          : renderSummedValidationCard(topology),
        ''
      ) +
      renderOutputStepCard(
        'profile',
        'Validate and apply',
        outputStepHint('profile', 'Save and apply the checked active profile.'),
        topology,
        commissioningStepNotRequired('profile')
          ? renderStepNotRequiredCard(
              'profile',
              'This speaker does not use an active speaker profile.')
          : renderBaselineProfileCard(),
        ''
      ) +
      renderTuningHandoffCard() +
      renderOutputTopologyResetAction() +
    '</div>';
  }
  function renderOutputTopologyResetAction() {
    var busy = outputTopology.loading || outputTopology.saving || outputTopology.resetting;
    return '<div class="output-setup__actions output-setup__actions--reset">' +
      '<button type="button" class="btn btn--danger" data-act="reset-output-topology"' +
        (busy ? ' disabled' : '') + '>' +
        escapeHtml(outputTopology.resetting ? 'Resetting' : 'Reset speaker setup') +
      '</button>' +
    '</div>';
  }
  function renderOutputHardwareCard(topology, statusValue) {
    var hardware = outputHardware(topology) || {};
    var observed = observedOutputHardware() || null;
    var mismatch = outputHardwareMismatch(topology);
    var clock = outputClockDomainReport();
    var clockStatus = clock && clock.status || '';
    var compositeClock = clockStatus.indexOf('dual_apple_composite_clock') === 0;
    var clockSupportLabel = compositeClock ? 'Composite clock' : 'Multi-DAC aggregate';
    var clockSupportValue = compositeClock
      ? (clock && clock.composite_clock_supported ? 'supported' : 'check setup')
      : (clock && clock.multi_device_aggregate_supported ? 'supported' : 'not configured');
    var rows = [
      ['Device', hardware.device_id || 'unknown'],
      ['Outputs', String(hardware.physical_output_count || 0) + ' physical'],
      ['Route', hardware.route || 'default'],
      ['Clock domain', clock && clock.clock_domain_label ||
        hardware.clock_domain_label || 'Single output device clock'],
      [clockSupportLabel, clockSupportValue],
      ['Topology', topology.name || topology.topology_id || 'Speaker outputs']
    ];
    var observedRows = observed ? [
      ['Profile', observed.profile_id || observed.device_id || 'unknown'],
      ['Outputs', String(hardwareOutputCount(observed)) + ' physical'],
      ['Status', observed.status || 'unknown'],
      ['Selected card', observed.selected_card_id || 'none'],
      ['Selected PCM', observed.selected_pcm || 'none']
    ] : [];
    var channelCount = Number(hardware.physical_output_count || 0);
    var savedCard = '<div class="output-card output-card--hardware">' +
      '<div class="output-card__head">' +
        '<div><p class="output-card__title">' + escapeHtml(hardware.device_label || 'Unknown output device') + '</p>' +
        '<p class="setting-row__hint">' + escapeHtml(
          String(channelCount || 0) + ' channel' + (channelCount === 1 ? '' : 's') + ' available'
        ) + '</p></div>' +
        '<span class="status-pill' + outputStatusClass(statusValue) + '">' + escapeHtml(statusValue) + '</span>' +
      '</div>' +
      '<details class="output-hardware-details">' +
        '<summary>Hardware details</summary>' +
        '<dl class="active-speaker-facts output-facts">' + rows.map(function(row) {
          return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
        }).join('') +
        (observedRows.length ? observedRows.map(function(row) {
          return '<div><dt>' + escapeHtml('Attached ' + row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
        }).join('') : '') +
        '</dl>' +
      '</details>' +
    '</div>';
    var repinOffer = renderOutputRepinOffer();
    var mismatchCard = mismatch ? (
      '<div class="output-card output-card--hardware">' +
        '<div class="output-card__head">' +
          '<div><p class="output-card__title">Hardware mismatch</p>' +
          '<p class="setting-row__hint">' + escapeHtml(mismatch.message) + '</p></div>' +
          '<span class="status-pill status-pill--blocked">blocked</span>' +
        '</div>' +
        '<p class="setting-row__hint">Reconnect the saved hardware or reconfigure the speaker layout after the attached hardware is stable. JTS keeps the saved topology intact.</p>' +
        repinOffer +
        (outputTopology.hardwareAdoption && outputTopology.hardwareAdoption.allowed ?
          '<button type="button" class="btn ' +
            (repinOffer ? 'btn--ghost' : 'btn--primary') +
            '" data-act="reset-output-topology"' +
            (outputHardwareActionBusy() ? ' disabled' : '') +
            '>Use detected hardware</button>' : '') +
      '</div>'
    ) : '';
    return mismatchCard + savedCard;
  }
  function outputHardwareActionBusy() {
    return !!(outputTopology.loading || outputTopology.saving ||
      outputTopology.resetting || outputTopology.repinning);
  }
  function renderOutputRepinOffer() {
    // The narrow alternative to "Use detected hardware": the same rig with a
    // replacement DAC of the same kind in the same USB port. The server decides
    // whether that is true (hardware_repin is null unless it is); this only
    // discloses what will be kept and what the household must still redo.
    var plan = outputTopology.hardwareRepin;
    if (!plan) return '';
    var replaced = Number(plan.replaced_child_count) || 0;
    var labels = Array.isArray(plan.reverify_output_labels) ?
      plan.reverify_output_labels : [];
    return '<div class="output-repin">' +
      '<p class="output-repin__title">' + escapeHtml(
        replaced === 1
          ? 'Same speakers, one new DAC'
          : 'Same speakers, ' + replaced + ' new DACs'
      ) + '</p>' +
      '<p class="setting-row__hint">' + escapeHtml(
        'JTS can keep your speaker layout, driver roles, output assignment and ' +
        'tuning, and pin ' + (replaced === 1 ? 'the new unit' : 'the new units') +
        ' in place of the old.'
      ) + '</p>' +
      '<p class="setting-row__hint">' + escapeHtml(
        'You still confirm ' +
        (labels.length ? labels.join(' and ') : 'the affected outputs') +
        ' by ear — audio stays off until you do and the speaker re-arms — and ' +
        're-run the 15-minute drift measurement for the new pair.'
      ) + '</p>' +
      '<button type="button" class="btn btn--primary" data-act="repin-output-topology"' +
        (outputHardwareActionBusy() ? ' disabled' : '') + '>' +
        escapeHtml(outputTopology.repinning ? 'Pinning' : 'Keep setup, pin the new DAC') +
      '</button>' +
    '</div>';
  }
  function renderOutputStageCard(topology) {
    var groups = outputGroups(topology);
    var cabinets = groups.map(function(group) {
      var channels = (Array.isArray(group.channels) ? group.channels : []).slice();
      var roleOrder = {tweeter: 0, mid: 1, woofer: 2, full_range: 3, subwoofer: 4};
      channels.sort(function(a, b) {
        return (roleOrder[a.role] == null ? 99 : roleOrder[a.role]) -
          (roleOrder[b.role] == null ? 99 : roleOrder[b.role]);
      });
      var channelCards = channels.map(function(channel) {
        var outputLabel = channel.human_output_label ||
          (channel.physical_output_index == null ? 'Unassigned' :
            physicalOutputLabel(topology, channel.physical_output_index));
        var model = targetModel({
          target_id: String(group.id || '') + ':' + String(channel.role || ''),
          role: String(channel.role || '')
        }, topology);
        return '<div class="speaker-stack__driver" data-role="' + escapeHtml(channel.role || '') + '">' +
          '<strong>' + escapeHtml(humanRole(channel.role)) + '</strong>' +
          '<span>' + escapeHtml(outputLabel) + '</span>' +
          (model ? '<small>' + escapeHtml(model) + '</small>' : '') +
        '</div>';
      }).join('');
      return '<div class="speaker-stack">' +
        '<div class="speaker-stack__label">' + escapeHtml(group.label || group.id || 'Speaker') + '</div>' +
        '<div class="speaker-stack__cabinet">' +
          (channelCards || '<p class="setting-row__hint">No channels yet.</p>') +
        '</div>' +
      '</div>';
    }).join('');
    return '<div class="output-card output-card--stage">' +
      '<div class="output-card__head"><div><p class="output-card__title">Speaker layout</p>' +
        '<p class="setting-row__hint">Drivers are stacked roughly like the cabinet you are wiring.</p></div></div>' +
      '<div class="speaker-stack-grid">' +
        (cabinets || '<p class="setting-row__hint">Choose a speaker layout first.</p>') +
      '</div>' +
    '</div>';
  }
  function renderOutputRoleToneControls(group, channel) {
    var activeGroup = activeCommissionGroup(currentOutputTopology());
    var role = channel && channel.role || '';
    if (!activeGroup || String(activeGroup.id || '') !== String(group.id || '')) return '';
    if (activeCommissionRoles(group).indexOf(role) < 0) return '';
    var targetKey = commissionTargetKey(group.id, role);
    var pending = commissionPendingStep();
    var loadedKey = commissionLoadedTargetKey(group.id);
    var tonePlaying = !!(pending && (pending.role || '') === role &&
      (!loadedKey || loadedKey === targetKey));
    var toneStarting = commissionAutoRamp.running &&
      commissionAutoRamp.targetKey === targetKey && !tonePlaying;
    var otherToneRunning = commissionAutoRamp.running &&
      commissionAutoRamp.targetKey !== targetKey;
    var otherPendingTone = !!(pending && (pending.role || '') !== role);
    var disabled = outputTopology.dirty ||
      channel.physical_output_index == null ||
      otherToneRunning ||
      otherPendingTone;
    if (tonePlaying) {
      return '<button type="button" class="btn btn--danger btn--compact output-role__action" ' +
        'data-act="commission-abort">' +
        'Stop</button>' +
        '<button type="button" class="btn btn--primary btn--compact output-role__action" ' +
          'data-act="commission-ack" data-outcome="heard_correct_driver" ' +
          'data-confirm-output-identity="true">' +
          'I hear ' + escapeHtml(String(humanRole(role)).toLowerCase()) + '</button>';
    }
    if (toneStarting) {
      return '<button type="button" class="btn btn--primary btn--compact output-role__action" disabled>' +
        'Starting</button>';
    }
    return '<button type="button" class="btn btn--ghost btn--compact output-role__action" ' +
      'data-act="commission-step" data-identity-audition="true" ' +
      'data-role="' + escapeHtml(role) + '"' +
      (disabled ? ' disabled' : '') + '>Play</button>';
  }
  function renderCrossChildNoticeCard(topology) {
    var verdicts = crossChildGroupVerdicts(topology);
    if (!verdicts.length) return '';
    return '<div class="output-card output-card--hardware">' +
      '<div class="output-card__head">' +
        '<div><p class="output-card__title">One speaker is split across two DACs</p>' +
        '<p class="setting-row__hint">Each USB DAC runs on its own clock and JTS does not correct between them, so a crossover split across two of them drifts. This layout still plays. Move that speaker&rsquo;s drivers onto one DAC when you can.</p></div>' +
        '<span class="status-pill">check wiring</span>' +
      '</div>' +
      renderIssueList(verdicts, 4) +
    '</div>';
  }
  function renderOutputGroupsCard(topology) {
    var assignments = [];
    var roleOrder = {woofer: 0, mid: 1, tweeter: 2, full_range: 3, subwoofer: 4};
    outputGroups(topology).forEach(function(group) {
      (Array.isArray(group.channels) ? group.channels : []).forEach(function(channel) {
        assignments.push({group: group, channel: channel});
      });
    });
    assignments.sort(function(a, b) {
      return String(a.group.label || a.group.id || '').localeCompare(String(b.group.label || b.group.id || '')) ||
        (roleOrder[a.channel.role] == null ? 99 : roleOrder[a.channel.role]) -
          (roleOrder[b.channel.role] == null ? 99 : roleOrder[b.channel.role]) ||
        String(a.channel.role || '').localeCompare(String(b.channel.role || ''));
    });
    if (!assignments.length) {
      return '<div class="output-card output-card--groups">' +
        '<p class="output-card__title">DAC output assignments</p>' +
        '<p class="setting-row__hint">Choose a speaker layout first. JTS keeps it as a draft until you confirm the wires.</p>' +
      '</div>';
    }
    var outputs = physicalOutputOptions(topology);
    var commissionError = activeSpeaker.commissionError ?
      '<p class="commission-card__error">' + escapeHtml(activeSpeaker.commissionError) + '</p>' : '';
    return '<div class="output-card output-card--groups">' +
      '<div class="output-card__head"><div><p class="output-card__title">DAC output assignments</p>' +
        '<p class="setting-row__hint">Assign each driver to one DAC channel. Play starts quiet and ramps.</p></div>' +
        '<span class="status-pill' + (outputTopology.dirty ? '' : ' status-pill--ready') + '">' +
          escapeHtml(outputTopology.dirty ? 'draft' : 'saved') + '</span></div>' +
      commissionError +
      '<div class="output-roles output-roles--flat">' + assignments.map(function(item) {
        var group = item.group;
        var channel = item.channel;
        var selected = channel.physical_output_index == null ? '' : String(channel.physical_output_index);
        var label = channel.human_output_label ||
          (channel.physical_output_index == null ? 'No output assigned' :
            physicalOutputLabel(topology, channel.physical_output_index));
        var target = identityTargetFor(group.id, channel.role) || {};
        var targetId = target.id || (group.id + ':' + channel.role);
        var busy = outputTopology.identitySaving === targetId;
        var disabled = outputTopology.dirty || busy ||
          channel.physical_output_index == null;
        var otherAssigned = outputAssignedToOtherMap(topology, group.id || '', channel.role || '');
        var allowPeerSwap = Array.isArray(group.channels) && group.channels.length === 2;
        var peerOutputIndexes = {};
        if (allowPeerSwap) {
          group.channels.forEach(function(peer) {
            if (peer === channel || peer.physical_output_index == null) return;
            peerOutputIndexes[String(peer.physical_output_index)] = true;
          });
        }
        var selectOptions = ['<option value="">Choose output</option>'].concat(
          outputs.map(function(output) {
            var value = String(output.index);
            var usedByOther = otherAssigned[value];
            var usedByPeerSwap = allowPeerSwap && peerOutputIndexes[value];
            var disableUsed = usedByOther && value !== selected && !usedByPeerSwap;
            return '<option value="' + escapeHtml(value) + '"' +
              (value === selected ? ' selected' : '') +
              (disableUsed ? ' disabled' : '') + '>' +
              escapeHtml(output.label + (usedByOther && value !== selected ?
                (usedByPeerSwap ? ' — swaps with ' : ' — used by ') + usedByOther : '')) +
              '</option>';
          })
        ).join('');
        var model = targetModel({
          target_id: String(group.id || '') + ':' + String(channel.role || ''),
          role: String(channel.role || '')
        }, topology);
        var hardwareLabel = (group.label || group.id) + ' · ' + humanRole(channel.role) +
          (model ? ' · ' + model : '');
        return '<div class="output-role">' +
          '<div class="output-role__text">' +
            '<span>' + escapeHtml(label) + '</span>' +
            '<strong>' + escapeHtml(hardwareLabel) + '</strong>' +
            '<small>' + escapeHtml(outputRoleStatusText(group, channel)) + '</small>' +
          '</div>' +
          '<label class="output-role__select">' +
            '<span>DAC channel</span>' +
            '<select data-output-channel data-group-id="' + escapeHtml(group.id || '') +
              '" data-role="' + escapeHtml(channel.role || '') + '">' +
              selectOptions +
            '</select>' +
          '</label>' +
          '<div class="output-role__actions">' +
            renderOutputRoleToneControls(group, channel) +
            '<button type="button" class="btn btn--ghost output-role__action" ' +
              'data-act="mark-output-identity" ' +
              'data-group-id="' + escapeHtml(group.id) + '" ' +
              'data-role="' + escapeHtml(channel.role) + '" ' +
              'data-verified="' + (channel.identity_verified ? 'false' : 'true') + '" ' +
              'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
              (disabled ? ' disabled' : '') + '>' +
              escapeHtml(busy ? 'Saving' : (channel.identity_verified ? 'Change' : 'Confirm output')) + '</button>' +
          '</div>' +
        '</div>';
      }).join('') + '</div>' +
    '</div>';
  }
  function renderOutputIdentityCard() {
    if (outputTopology.dirty) {
      return '<div class="output-card output-card--identity">' +
        '<div class="output-card__head"><div><p class="output-card__title">Confirmation progress</p>' +
        '<p class="setting-row__hint">Save this speaker layout draft before confirming outputs.</p></div>' +
        '<span class="status-pill">draft</span></div>' +
        '<p class="setting-row__hint">JTS will re-check the layout after save, then you can confirm each DAC output.</p>' +
      '</div>';
    }
    var report = outputIdentityReport();
    if (!report) {
      return '<div class="output-card output-card--identity">' +
        '<div class="output-card__head"><div><p class="output-card__title">Confirmation progress</p>' +
        '<p class="setting-row__hint">Load or save the speaker layout to see verification progress.</p></div></div>' +
      '</div>';
    }
    var assigned = Number(report.assigned_channel_count || 0);
    var verified = Number(report.verified_channel_count || 0);
    var unverified = Number(report.unverified_channel_count || 0);
    var proof = activeSpeaker.commissioningView &&
      activeSpeaker.commissioningView.driver_target_proof || {};
    var summary = measurementSummary();
    var proofComplete = proof.complete === true ||
      (outputIdentityComplete() && driverChecksComplete());
    var proofCaptured = Number(
      proof.captured ||
      summary.captured_driver_check_count ||
      summary.captured_driver_count ||
      0
    );
    var proofRequired = Number(
      proof.required ||
      summary.required_driver_check_count ||
      summary.required_driver_count ||
      assigned ||
      0
    );
    // The backend already publishes WHY the driver proof is what it is. When no
    // driver listening check was ever required, "0/1 heard" (styled ready) read
    // as a verification result nothing had produced; report the confirmation
    // that actually happened — output identity — instead.
    var proofNotRequired = String(proof.source || '') === 'not_required';
    var proofPill = proofNotRequired ?
      (verified + '/' + assigned + ' confirmed') :
      (proofCaptured + '/' + proofRequired + ' heard');
    var targets = Array.isArray(report.targets) ? report.targets : [];
    var rows = targets.length ? targets.map(function(target) {
      var heard = driverMeasurementCaptured(target.speaker_group_id, target.role);
      return '<li class="output-identity-row">' +
        '<span>' + escapeHtml(target.speaker_label || target.speaker_group_id || 'Speaker') +
          ' · ' + escapeHtml(humanRole(target.role)) + '</span>' +
        '<strong>' + escapeHtml(heard ? 'Heard' : target.identity_verified ? 'Confirmed' :
          (target.assigned ? 'Needs confirmation' : 'Unassigned')) + '</strong>' +
      '</li>';
    }).join('') : '<li class="output-identity-row"><span>No channels configured</span><strong>Draft</strong></li>';
    return '<div class="output-card output-card--identity">' +
      '<div class="output-card__head"><div><p class="output-card__title">Confirmation progress</p>' +
        '<p class="setting-row__hint">Play each quiet ramp, then confirm the driver you hear.</p></div>' +
        '<span class="status-pill' + (proofComplete ? ' status-pill--ready' : '') + '">' +
          escapeHtml(proofPill) + '</span></div>' +
      (outputTopology.dirty ? '<p class="setting-row__hint">Save the draft before changing confirmed outputs.</p>' : '') +
      '<ul class="output-identity-list">' + rows + '</ul>' +
      '<p class="setting-row__hint">' + escapeHtml(
        proofComplete && proofNotRequired ?
          'Every output is confirmed. This speaker needs no separate driver checks.' :
        proofComplete ? 'Outputs and drivers are confirmed. Continue to the combined test.' :
        unverified > 0 ? 'Play and confirm each assigned output above to continue.' :
          'Each output is assigned; finish hearing each driver to continue.'
      ) + '</p>' +
      '<p class="setting-row__hint commission-card__followup">Confirming each driver ' +
        'by ear proves it is wired and audible through the crossover and limiter. ' +
        'Mic-based level matching is a separate HTTPS measurement step after this basic setup.</p>' +
    '</div>';
  }
  function playbackHasBlocker(playback) {
    var issues = Array.isArray(playback && playback.issues) ? playback.issues : [];
    return issues.some(function(issue) {
      return issue && issue.severity === 'blocker';
    });
  }
  function playbackConfirmable(playback) {
    return !!playback &&
      playback.audio_emitted === true &&
      playback.playback_id &&
      playback.confirmable !== false &&
      !playbackHasBlocker(playback);
  }
  function commissionTargetKey(groupId, role) {
    return [groupId || '', role || ''].join(':');
  }
  function commissionLoadedTargetKey(fallbackGroupId) {
    var commission = activeSpeaker.commission || {};
    var load = commission.commission_load || {};
    var target = load.target || {};
    if (load.status !== 'loaded' || !target.role) return '';
    return commissionTargetKey(target.speaker_group_id || fallbackGroupId || '', target.role || '');
  }
  function commissionAutoRampCurrent(groupId, role, token) {
    var targetKey = commissionTargetKey(groupId, role);
    if (!commissionAutoRamp.running || token !== commissionAutoRamp.token ||
        commissionAutoRamp.targetKey !== targetKey) return false;
    var loadedKey = commissionLoadedTargetKey(groupId);
    if (loadedKey && loadedKey !== targetKey) return false;
    var pending = commissionPendingStep();
    return !(pending && (pending.role || '') !== role);
  }
  function stopCommissionAutoRamp(message) {
    commissionAutoRamp = Object.assign({}, commissionAutoRamp, {
      running: false,
      token: commissionAutoRamp.token + 1,
      message: message || ''
    });
  }
  function commissionPendingStep() {
    var commission = activeSpeaker.commission || {};
    var ramp = commission.ramp || {};
    return ramp.pending || null;
  }
  function measurementTargetId(groupId, role) {
    return String(groupId || '') + ':' + String(role || '').trim().toLowerCase();
  }
  function latestDriverMeasurement(groupId, role) {
    var summary = measurementSummary();
    var latest = summary.latest_driver_checks || summary.latest_driver_measurements || {};
    return latest[measurementTargetId(groupId, role)] || null;
  }
  function driverMeasurementCaptured(groupId, role) {
    var latest = latestDriverMeasurement(groupId, role);
    return latest && latest.captured === true;
  }
  function latestSummedValidation(groupId) {
    var latest = measurementSummary().latest_summed_validations || {};
    return latest[String(groupId || '')] || null;
  }
  function latestSummedTest(groupId) {
    var latest = measurementSummary().latest_summed_tests || {};
    return latest[String(groupId || '')] || null;
  }
  function commissioningGroupView(groupId) {
    var view = activeSpeaker.commissioningView || {};
    var groups = Array.isArray(view.combined_groups) ? view.combined_groups : [];
    var key = String(groupId || '');
    for (var i = 0; i < groups.length; i += 1) {
      if (String(groups[i].group_id || '') === key) return groups[i];
    }
    return null;
  }
  function commissioningGroupAction(groupView, actionId) {
    var actions = groupView && groupView.actions || {};
    var action = actions[actionId] || null;
    return action && typeof action === 'object' ? action : null;
  }
  // The banner for a combined test that answered 200 and emitted nothing. The
  // backend has already resolved WHY for this group (the caller refreshes the
  // commissioning view first), so carry the remedy here instead of telling the
  // household to "review the message in this card": the ladder can legitimately
  // keep that card closed, and then the message pointed nowhere at all.
  function summedTestFailureBanner(groupId) {
    return summedGroupFailureHint(commissioningGroupView(groupId)) ||
      SUMMED_TEST_GENERIC_RETRY_HINT;
  }
  function combinedTestLevelConfig() {
    var viewLevel = activeSpeaker.commissioningView &&
      activeSpeaker.commissioningView.test_level || {};
    var signal = activeSpeaker.calibrationLevel &&
      activeSpeaker.calibrationLevel.test_signal || {};
    var localValue = activeSpeaker.combinedTestLevelDbfs == null ?
      NaN : Number(activeSpeaker.combinedTestLevelDbfs);
    var requested = isFinite(localValue) ? localValue : Number(
      viewLevel.requested_level_dbfs != null ?
        viewLevel.requested_level_dbfs : signal.requested_level_dbfs
    );
    var min = Number(
      viewLevel.min_level_dbfs != null ? viewLevel.min_level_dbfs : signal.min_level_dbfs
    );
    var max = Number(
      viewLevel.max_level_dbfs != null ? viewLevel.max_level_dbfs : signal.max_level_dbfs
    );
    var step = Number(
      viewLevel.step_db != null ? viewLevel.step_db : signal.step_db
    );
    if (!isFinite(min)) min = -80;
    if (!isFinite(max)) max = 0;
    if (!isFinite(step) || step <= 0) step = 1;
    if (!isFinite(requested)) requested = min;
    requested = clamp(requested, min, max);
    return {
      min: min,
      max: max,
      step: step,
      value: requested
    };
  }
  function combinedTestLevelDbfs() {
    var cfg = combinedTestLevelConfig();
    var value = activeSpeaker.combinedTestLevelDbfs == null ?
      NaN : Number(activeSpeaker.combinedTestLevelDbfs);
    return clamp(isFinite(value) ? value : cfg.value, cfg.min, cfg.max);
  }
  function combinedTestLevelDbfsFrom(value) {
    var cfg = combinedTestLevelConfig();
    return clamp(value, cfg.min, cfg.max);
  }
  function renderSummedLevelControl(groupId, options) {
    options = options || {};
    var cfg = combinedTestLevelConfig();
    var value = combinedTestLevelDbfs();
    var disabled = options.disabled === true;
    var live = options.live === true;
    var hint = live ?
      'Changes apply while the test audio is playing.' :
      (disabled ?
        'Preparing the test path. Level changes will be available in a moment.' :
        'Choose a careful level. You can adjust it while the test audio plays (about 12 seconds).');
    return '<label class="active-speaker-summed-level">' +
      '<span class="active-speaker-summed-level__head">' +
        '<span>Combined test level</span>' +
        '<strong data-summed-level-readout="' + escapeHtml(groupId) + '">' +
          escapeHtml(fmtDb(value)) +
        '</strong>' +
      '</span>' +
      '<input type="range" data-summed-test-level="' + escapeHtml(groupId) + '"' +
        ' min="' + escapeHtml(String(cfg.min)) + '"' +
        ' max="' + escapeHtml(String(cfg.max)) + '"' +
        ' step="' + escapeHtml(String(cfg.step)) + '"' +
        ' value="' + escapeHtml(String(value)) + '"' +
        (disabled ? ' disabled' : '') +
        ' aria-label="Combined test level">' +
      '<span class="setting-row__hint">' + escapeHtml(hint) + '</span>' +
    '</label>';
  }
  // The body of a step this speaker's shape will never run. Says why, and says
  // the setup is finished, instead of leaving a titled card with nothing in it.
  // The sentence comes from the backend step message; `fallback` only covers a
  // view that has not loaded yet.
  function renderStepNotRequiredCard(step, fallback) {
    return '<div class="output-card output-card--not-required">' +
      '<div class="output-card__head"><div>' +
        '<p class="output-card__title">Not needed for this speaker</p>' +
        '<p class="setting-row__hint">' + escapeHtml(outputStepHint(step, fallback)) +
        '</p></div>' +
        '<span class="status-pill">not needed</span></div>' +
      '<p class="setting-row__hint">Speaker setup is complete once every output is confirmed.</p>' +
    '</div>';
  }
  // Is the combined test actually on offer? The BACKEND owns the whole
  // prerequisite chain — the values must be saved AND previewed AND the outputs
  // confirmed, because the test plays through the staged crossover graph — and
  // publishes the verdict as each group's start action. Re-deriving readiness
  // here from the driver proof alone let the card head invite "play the
  // combined speaker" over a button the backend had disabled. Only fall back to
  // the local guess when the backend view has not loaded.
  function combinedTestOnOffer(groups) {
    var backendAnswered = false;
    var offered = false;
    (groups || []).forEach(function(group) {
      var action = commissioningGroupAction(
        commissioningGroupView(group.id), 'start_combined_test');
      if (!action) return;
      backendAnswered = true;
      if (action.enabled === true) offered = true;
    });
    return backendAnswered ? offered : driverTargetProofComplete();
  }
  function renderSummedValidationCard(topology) {
    var groups = activeOutputGroups(topology);
    // Reached today, not hypothetically: a passive-mains-WITH-sub layout keeps
    // the safety step live (it still compiles a degenerate 1-way bass-management
    // profile) but has no active driver group to test together, so this runs
    // with zero groups on every render of that shape. The SUBLESS passive
    // layout never arrives — the backend terminates it (`not_required`) before
    // this call. Either way, say why instead of rendering a step title over an
    // empty body: that silent blank is how the passive dead end went unnoticed.
    if (!groups.length) {
      return '<div class="output-card output-card--not-required">' +
        '<div class="output-card__head"><div>' +
          '<p class="output-card__title">No combined test available</p>' +
          '<p class="setting-row__hint">This speaker layout has no group of drivers to test together.</p>' +
        '</div><span class="status-pill">unavailable</span></div>' +
      '</div>';
    }
    var canRecord = combinedTestOnOffer(groups);
    var revalidation = baselineProfileRevalidation();
    var revalidating = revalidation.required === true;
    var revalidationNeedsCombined = revalidating &&
      (revalidation.next_step || '') === 'combined_check';
    var rows = groups.map(function(group) {
      var groupView = commissioningGroupView(group.id);
      var startAction = commissioningGroupAction(groupView, 'start_combined_test');
      var recordAction = commissioningGroupAction(groupView, 'record_combined_result');
      var latest = latestSummedValidation(group.id);
      var latestTest = latestSummedTest(group.id);
      var ok = groupView ? groupView.validated === true :
        (latest && latest.validated === true);
      var hasAudibleTest = latestTest && latestTest.captured === true &&
        latestTest.audio_emitted === true && !playbackHasBlocker(latestTest);
      if (groupView && groupView.has_audible_test === true) hasAudibleTest = true;
      var statusText = groupView && groupView.status_label ? groupView.status_label :
        (ok ? 'validated' : (hasAudibleTest ? 'ready' : 'not tested'));
      var combinedStarting = activeSpeaker.action === 'Starting combined test';
      var combinedPlaying = activeSpeaker.action === 'Playing combined test';
      var combinedStopping = activeSpeaker.action === 'Stopping combined test';
      var combinedSaving = activeSpeaker.action === 'Saving combined check';
      // Server-authoritative "a combined test is still looping" — lets a freshly
      // loaded/reloaded page render Stop even though this tab never held the
      // local 'Playing combined test' action (the un-stoppable-after-reload bug).
      var serverTestActive = !!(groupView && groupView.summed_test_active === true);
      var localTestCurrent = summedTestRequest.current || null;
      var localCombinedPlaying = combinedPlaying &&
        localTestCurrent && localTestCurrent.groupId === group.id &&
        localTestCurrent.promise;
      var combinedControlsLocked = combinedStarting || combinedStopping || combinedSaving;
      var combinedPlaybackActive = combinedStarting || combinedPlaying || combinedStopping || serverTestActive;
      var showStop = (combinedPlaying || serverTestActive) && !combinedStopping;
      var testButton;
      if (showStop) {
        testButton = '<button type="button" class="btn btn--danger" ' +
          'data-act="stop-summed-test" data-group-id="' + escapeHtml(group.id) + '"' +
          '>Stop</button>';
      } else if (combinedStarting || combinedStopping) {
        testButton = '<button type="button" class="btn ' +
          (combinedStopping ? 'btn--danger' : 'btn--primary') + '" disabled>' +
          escapeHtml(combinedStopping ? 'Stopping' : 'Preparing combined test') +
          '</button>';
      } else {
        testButton = '<button type="button" class="btn btn--primary" ' +
          'data-act="prepare-summed-test" data-group-id="' + escapeHtml(group.id) + '"' +
          ' data-label="' + escapeHtml(group.label || group.id || 'speaker') + '"' +
          ((startAction ? startAction.enabled !== true : !canRecord) ? ' disabled' : '') +
          '>' + escapeHtml(startAction && startAction.label || 'Play combined test') +
          '</button>';
      }
      var recordEnabled = !combinedControlsLocked &&
        (recordAction ? recordAction.enabled === true : hasAudibleTest);
      // Only the tab that owns the active play request can turn "Sounds right"
      // into a confirmed stop + fresh validation. A reloaded page may know from
      // the server that a test is active, but it has no pending play promise to
      // await, so it offers Stop only.
      if (serverTestActive && !localCombinedPlaying) recordEnabled = false;
      if (localCombinedPlaying) recordEnabled = true;
      var summedTestId =
        localCombinedPlaying ? '' :
        recordAction && recordAction.body && recordAction.body.summed_test_id ||
        latestTest && (latestTest.summed_test_id || latestTest.playback_id) || '';
      // Positive by-ear path for the core /sound flow. Mic-backed level/delay
      // work belongs in the separate HTTPS measurement experience.
      var blendOkButton = '<button type="button" class="btn btn--primary" ' +
        'data-act="record-summed-validation" data-group-id="' + escapeHtml(group.id) +
        '" data-summed-test-id="' + escapeHtml(summedTestId) +
        '" data-outcome="blend_ok"' + (recordEnabled ? '' : ' disabled') +
        '>Sounds right</button>';
      var backButton = '<button type="button" class="btn btn--ghost" ' +
        'data-act="back-to-crossover-config"' +
        (combinedPlaybackActive || combinedSaving ? ' disabled' : '') + '>Back to adjust crossover</button>';
      var hint = revalidationNeedsCombined ?
        (hasAudibleTest ?
          'Revalidation test played. Save the result if the speaker sounds coherent.' :
          'Your active speaker setup changed after the current profile was applied. Play the combined check again, then save the result.') :
        groupView && groupView.message ? groupView.message : (hasAudibleTest ?
        'After the combined test, save the result if the speaker sounds coherent.' :
        (canRecord ?
          'Run the combined speaker test first. It uses the prepared crossover setup at the level you choose.' :
          'Confirm outputs first, then test the combined speaker.'));
      // Backend owns the per-failure-code copy (groupView.failure_message); the
      // helper falls back to ONE generic line only when the view is unavailable.
      var retryHint = summedGroupFailureHint(groupView, { suppress: hasAudibleTest });
      return '<div class="active-speaker-validation__group">' +
        '<div class="row-between">' +
          '<div><p class="setting-row__title">' + escapeHtml(group.label || group.id || 'Speaker') + '</p>' +
          '<p class="setting-row__hint">' + escapeHtml(hint) + '</p>' +
          (retryHint ? '<p class="setting-row__hint">' +
            escapeHtml(retryHint) + '</p>' : '') +
          '</div>' +
          '<span class="status-pill' + (ok ? ' status-pill--ready' : '') + '">' +
            escapeHtml(statusText) + '</span>' +
        '</div>' +
        renderSummedLevelControl(group.id, {
          disabled: combinedControlsLocked,
          live: combinedPlaying || serverTestActive
        }) +
        '<div class="active-speaker-actions">' + testButton + blendOkButton +
          backButton + '</div>' +
      '</div>';
    }).join('');
    return '<div class="output-card output-card--summed-validation">' +
      '<div class="output-card__head"><div><p class="output-card__title">' +
        escapeHtml(revalidationNeedsCombined ? 'Revalidate crossover blend' : 'Combined crossover check') + '</p>' +
        '<p class="setting-row__hint">' + escapeHtml(canRecord ?
          (revalidationNeedsCombined ?
            'Play the combined speaker again, then save the check if it still sounds right.' :
            'Choose a careful level, play the combined speaker, then save the check if it sounds right.') :
          // Which earlier rung is missing varies (unsaved values, unconfirmed
          // outputs), and each group row below names it from the backend. Do
          // not blame the outputs here — that copy sent a household whose
          // outputs WERE confirmed back to a card with nothing left to do.
          'Finish the steps above, then validate the combined crossover.') + '</p></div>' +
        '<span class="status-pill' + (summedValidationComplete() ? ' status-pill--ready' : '') + '">' +
          escapeHtml(summedValidationComplete() ? 'ready' : (revalidationNeedsCombined ? 'recheck' : (canRecord ? 'next' : 'after setup'))) + '</span></div>' +
      '<div class="active-speaker-validation">' + rows + '</div>' +
    '</div>';
  }
  function baselineProfileApplyBlocked(profile) {
    var issues = Array.isArray(profile && profile.issues) ? profile.issues : [];
    return (profile && profile.status) === 'compiled_apply_blocked' ||
      issues.some(function(issue) {
        return issue && issue.code === 'baseline_output_handoff_not_supported';
      });
  }
  function baselineProfileIssueMessage(issue) {
    if (!issue) return 'Profile is not ready yet.';
    if (issue.code === 'baseline_output_handoff_not_supported') {
      return 'This output hardware can save the active profile, but JTS cannot switch normal playback to it from here yet.';
    }
    if (issue.code === 'baseline_subwoofer_not_supported') {
      return 'Subwoofer groups are not included in the active profile compiler yet.';
    }
    return 'The active profile is not ready yet.';
  }
  function renderLevelMatchSummary(profile) {
    var summary = levelMatchSummary(profile);
    if (!summary.available) return '';
    var rows = summary.rows.map(function(row) {
      var trim = row.trimDb === 0 ? '0 dB (reference)' :
        (row.trimDb.toFixed(1) + ' dB');
      return '<div><dt>' + escapeHtml(row.label) + '</dt><dd>' +
        escapeHtml(trim) + ' · ' + escapeHtml(row.sourceLabel) + '</dd></div>';
    }).join('');
    var badge = summary.badge === 'measured'
      ? ' status-pill status-pill--ready' : ' status-pill';
    return '<div class="active-speaker-level-match">' +
      '<div class="output-card__head"><div>' +
        '<p class="setting-row__title">Driver levels</p>' +
        '<p class="setting-row__hint">' + escapeHtml(summary.note) + '</p></div>' +
        '<span class="' + badge + '">' +
          escapeHtml(summary.badge) + '</span></div>' +
      '<dl class="active-speaker-facts">' + rows + '</dl>' +
      (summary.badge !== 'measured' ?
        '<p class="setting-row__hint">' + escapeHtml(summary.guidance) + '</p>' : '') +
    '</div>';
  }
  function renderBaselineProfileCard() {
    var profile = activeSpeaker.baselineProfile || {};
    var appliedRecord = baselineProfileAppliedRecord();
    var config = (appliedRecord || profile).config || {};
    var permissions = profile.permissions || {};
    var applied = appliedRecord !== null;
    var readyToApply = permissions.may_apply === true;
    var mayCompile = summedValidationComplete();
    var applyBlocked = baselineProfileApplyBlocked(profile);
    var revalidating = baselineProfileNeedsRevalidation();
    var busy = activeSpeaker.action === 'Finishing active profile';
    var canFinish = !applyBlocked && (mayCompile || readyToApply);
    var issues = Array.isArray(profile.issues) ? profile.issues : [];
    var issueRows = issues.filter(function(issue) {
      return issue && issue.severity === 'blocker';
    }).slice(0, 3).map(function(issue) {
      return '<li>' + escapeHtml(baselineProfileIssueMessage(issue)) + '</li>';
    }).join('');
    var body = applied ?
      '<p class="setting-row__hint">This is now your active speaker profile: ' +
        escapeHtml(config.basename || config.path || 'active speaker baseline') + '.</p>' :
      (applyBlocked ?
        '<p class="setting-row__hint">This profile cannot be made active from this page yet. Review the setup issue below.</p>' :
      (readyToApply ?
        '<p class="setting-row__hint">Your active speaker profile is saved. Finish applying it to start using it.</p>' :
      (revalidating ?
        '<p class="setting-row__hint">' + escapeHtml(mayCompile ?
          'Revalidation is saved. Save and apply a fresh active profile.' :
          'Your active speaker setup changed after the current profile was applied. Revalidate the combined crossover, then save and apply a fresh profile.') + '</p>' :
        '<p class="setting-row__hint">' + escapeHtml(mayCompile ?
          'Save the checked crossover as your active speaker profile. JTS validates and applies it in one step; no sound plays.' :
          'Finish the combined crossover check before saving the active profile.') + '</p>')));
    var actionLabel = busy ?
      'Saving and applying' :
      'Save and apply';
    // Applied: the basic door stays reachable — the household may want it —
    // but it is never the primary here and never offered without saying what
    // it replaces (ADR-0195).
    var replaces = appliedProfileCorrections(appliedRecord);
    var actions = applyBlocked ? '' : (applied ?
      (replaces ?
        '<div class="active-speaker-actions active-speaker-profile-actions">' +
          '<button type="button" class="btn" data-act="save-apply-baseline-profile"' +
            (busy ? ' disabled' : '') + '>' +
            escapeHtml(busy ? actionLabel : 'Replace with basic profile') + '</button>' +
          '<p class="setting-row__hint">The basic profile compiles the saved ' +
            'crossover with driver trims only. Applying it replaces the ' +
            'measured profile: ' + escapeHtml(replaces) + ' are not re-emitted.</p>' +
        '</div>' : '') :
      '<div class="active-speaker-actions active-speaker-profile-actions">' +
        '<button type="button" class="btn btn--primary' +
          '" data-act="save-apply-baseline-profile"' +
          ((busy || !canFinish) ? ' disabled' : '') + '>' +
          escapeHtml(actionLabel) + '</button>' +
      '</div>');
    return '<div class="output-card output-card--baseline-profile">' +
      '<div class="output-card__head"><div><p class="output-card__title">Active speaker profile</p>' +
        '<p class="setting-row__hint">Your active speaker profile, built from the checked crossover and confirmed outputs.</p></div>' +
        '<span class="status-pill' + (applied || readyToApply ? ' status-pill--ready' : '') + '">' +
          escapeHtml(applied ? 'active' : (readyToApply ? 'saved' : (applyBlocked ? 'blocked' : (revalidating ? 'recheck' : 'not saved')))) + '</span></div>' +
      body +
      renderLevelMatchSummary(appliedRecord || profile) +
      (issueRows && mayCompile ? '<ul class="active-speaker-issues active-speaker-issues--warning">' + issueRows + '</ul>' : '') +
      actions +
    '</div>';
  }
  // Only a revision the page has seen move PAST the minted one is stale. The
  // page's cached draft can lag the server's (the mint re-reads it), and a
  // behind-by-one cache is not an edit the operator made.
  function tuningHandoffStale() {
    if (tuningHandoff.copiedRevision === null) return false;
    var draft = driverResearch.designDraft || {};
    var live = typeof draft.revision === 'number' ? draft.revision : 0;
    return live > tuningHandoff.copiedRevision;
  }
  // Gated on the applied baseline and nothing else: no tuning flow is a
  // prerequisite for using the speaker, so this card cannot appear before the
  // speaker plays (#2883).
  function renderTuningHandoffCard() {
    if (!baselineProfileApplied()) return '';
    var stale = tuningHandoffStale();
    var copyState = promptCopyState(tuningHandoff, stale);
    var selected = copyState.selected;
    var promptClass = copyState.promptClass;
    var label = copyState.label;
    return '<div class="output-card">' +
      '<div class="output-card__head"><div>' +
        '<p class="output-card__title">Tune with an AI operator</p>' +
        '<p class="setting-row__hint">Copy this prompt into a fresh AI session ' +
          'that has an SSH connection to this speaker. It points at the ' +
          'instructions installed on the box rather than repeating them, so it ' +
          'cannot go out of date.</p></div>' +
        '<button type="button" class="btn btn--ghost" data-act="copy-tuning-handoff">' +
          escapeHtml(label) + '</button></div>' +
      (stale ? '<p class="setting-row__hint" data-tuning-handoff-stale>' +
        'Your declarations changed after you copied this prompt. Copy it again ' +
        'before you start a session.</p>' : '') +
      '<textarea id="tuning-handoff-prompt" class="' + promptClass + '" readonly ' +
        (selected ? 'rows="6" ' : '') +
        'aria-label="AI operator handoff prompt">' +
        escapeHtml(tuningHandoff.prompt || '') + '</textarea>' +
    '</div>';
  }
  function rangeRow(label, value, min, max, opts) {
    opts = opts || {};
    var pct, thumb;
    if (opts.log) {
      var lmin = Math.log(min), lmax = Math.log(max);
      pct = (Math.log(clamp(value, min, max)) - lmin) / (lmax - lmin) * 100;
    } else {
      pct = (clamp(value, min, max) - min) / (max - min) * 100;
    }
    if (opts.variant === 'thumb') {
      thumb = '<div class="range__track"></div><div class="range__thumb" style="left:calc(' + pct + '% - 6px)"></div>';
    } else {
      thumb = '<div class="range__fill-track"><div class="range__fill" style="width:' + pct + '%"></div></div>';
    }
    return '<div class="range-row">' +
      '<span class="range-row__label">' + escapeHtml(label) + '</span>' +
      '<div class="range">' + thumb +
        '<input type="range" class="range__input" min="' + (opts.log ? 0 : min) + '" max="' + (opts.log ? 1000 : max) +
          '" step="' + (opts.step || 0.1) + '" value="' + (opts.log ? freqToSlider(value, min, max) : value) +
          '" data-range="' + opts.kind + '" aria-label="' + escapeHtml(label) + '"></div>' +
      '<div class="range__readout"><span class="range__readout-value" data-readout="' + opts.kind + '">' +
        escapeHtml(opts.format(value)) + '</span></div>' +
    '</div>';
  }
  function bandRow(band, index) {
    var open = !allCollapsed && index === eqEditor.activeBand;
    var type = band.type || 'Peaking';
    var shelf = type === 'Lowshelf' || type === 'Highshelf';
    var gainless = GAINLESS_TYPES.indexOf(type) >= 0;
    var body = '';
    if (open) {
      // Gain hidden for cut/notch (no gain term); Width hidden for shelves
      // (JTS draws and emits every shelf at the fixed Butterworth SHELF_Q, so
      // the control would be inert — see eq-math.js).
      body = '<div class="band-row__body">' +
        '<div class="range-row"><span class="range-row__label">Type</span>' +
          '<div class="segmented" data-band="' + index + '">' +
            typeBtn('Lowshelf', 'Low', type) + typeBtn('Peaking', 'Peak', type) +
            typeBtn('Highshelf', 'High', type) + typeBtn('Highpass', 'HP', type) +
            typeBtn('Lowpass', 'LP', type) + typeBtn('Notch', 'Notch', type) + '</div></div>' +
        rangeRow('Freq', band.freq_hz, limits.min_freq_hz, limits.max_freq_hz,
          {kind: 'freq', log: true, variant: 'thumb', step: 1, format: function(v) { return fmtFreq(v); }}) +
        (gainless ? '' : rangeRow('Gain', band.gain_db, -limits.advanced_gain_db, limits.advanced_gain_db,
          {kind: 'gain', step: 0.1, format: function(v) { return fmtDb(v) + ' dB'; }})) +
        (shelf ? '' : rangeRow('Width', band.q, limits.min_q, bandQMax(type),
          {kind: 'q', step: 0.1, format: function(v) { return fmtQ(v); }})) +
        '<button type="button" class="band-row__delete" data-act="del-band" data-index="' + index + '">' +
          ico('trash') + 'Delete band</button>' +
      '</div>';
    }
    var meta = escapeHtml(type) + ' · ' + Math.round(band.freq_hz) + ' Hz';
    if (!gainless) meta += ' · ' + band.gain_db.toFixed(1) + ' dB';
    if (!shelf) meta += ' · Q ' + band.q.toFixed(1);
    return '<div class="band-row" data-index="' + index + '" data-open="' + (open ? 'true' : 'false') + '">' +
      '<button type="button" class="band-row__header" data-act="toggle-band" data-index="' + index + '">' +
        '<span class="band-row__title">' +
          '<span class="band-dot' + (open ? ' band-dot--active' : '') + '">' + (index + 1) + '</span>' +
          '<span><p class="band-row__name">Band ' + (index + 1) + '</p>' +
            '<p class="band-row__meta">' + meta + '</p></span>' +
        '</span>' + ico('chevron', 'band-row__chev') +
      '</button>' + body + '</div>';
  }
  function typeBtn(value, label, current) {
    return '<button type="button" class="segmented__btn" data-band-type="' + value + '" aria-pressed="' +
      (current === value ? 'true' : 'false') + '">' + label + '</button>';
  }
  function simpleColumn(slot, value) {
    var min = -limits.simple_gain_db, max = limits.simple_gain_db;
    var pct = (clamp(value, min, max) - min) / (max - min) * 100;
    return '<div class="simple-col" data-field="' + escapeHtml(slot.field) + '">' +
      '<div class="simple-col__readout"><span class="simple-col__readout-value" data-readout-field="' +
        escapeHtml(slot.field) + '">' + fmtDb(value) + '</span></div>' +
      '<div class="vrange"><div class="vrange__track"></div><div class="vrange__zero"></div>' +
        '<div class="vrange__thumb" style="bottom:calc(' + pct + '% - 6px)"></div>' +
        '<input type="range" class="vrange__input" min="' + min + '" max="' + max + '" step="0.1" value="' + value +
          '" data-field="' + escapeHtml(slot.field) + '" aria-label="' + escapeHtml(slot.label) + ' gain"></div>' +
      '<div class="band-dot">' + (slot.idx + 1) + '</div>' +
      '<div class="simple-col__caption"><p>' + escapeHtml(slot.label) + '</p><p>' + fmtFreqShort(slot.freq_hz) + ' Hz</p></div>' +
    '</div>';
  }
  function renderDraft() {
    var modeSection = '<section class="mode-toggle"><div class="section-header"><h2 class="eyebrow">Mode</h2></div>' +
      '<div class="segmented" id="mode-tabs">' +
        '<button type="button" class="segmented__btn" data-mode="simple" aria-pressed="' + (eqEditor.mode === 'simple' ? 'true' : 'false') + '">Simple</button>' +
        '<button type="button" class="segmented__btn" data-mode="peq" aria-pressed="' + (eqEditor.mode === 'peq' ? 'true' : 'false') + '">PEQ</button>' +
      '</div></section>';

    var bandsContent;
    if (eqEditor.mode === 'simple') {
      var cols = (eqEditor.simpleBands.length ? eqEditor.simpleBands : []).map(function(slot, i) {
        return simpleColumn(Object.assign({idx: i}, slot), draft.simple_eq[slot.field] || 0);
      }).join('');
      bandsContent = '<div class="bands-card bands-card--simple"><div class="simple-grid">' + cols + '</div></div>';
    } else {
      var rows = (draft.parametric_bands || []).map(bandRow).join('');
      bandsContent = '<div class="bands-card"><div class="bands-card__rows">' + rows +
        '<button type="button" class="add-band" data-act="add-band"' +
        (draft.parametric_bands.length >= limits.max_parametric_bands ? ' disabled' : '') + '>' +
        ico('plus') + 'Add band</button></div></div>';
    }
    var activeCount = eqEditor.mode === 'simple'
      ? Object.keys(draft.simple_eq).filter(function(k) {
        return Math.abs(draft.simple_eq[k]) >= ACTIVE_GAIN_EPSILON_DB;
      }).length
      : draft.parametric_bands.filter(function(b) { return b.enabled !== false; }).length;
    var bandsSection = '<section class="bands-section"><div class="row-between">' +
      '<h2 class="eyebrow">Bands</h2>' +
      '<div class="bands-meta"><span id="active-count">' + activeCount + ' active</span>' +
      (eqEditor.mode === 'peq' ? '<button type="button" class="text-button text-button--muted" data-act="toggle-collapse">' +
        (allCollapsed ? 'Expand all' : 'Collapse all') + '</button>' : '') +
      '</div></div>' + bandsContent + '</section>';

    el('view-body').innerHTML = '<div>' + modeSection + bandsSection +
      '<section class="draft-footer">' + footerHtml() + '</section></div>';
  }
  function footerHtml() {
    if (eqEditor.naming) {
      var isRename = eqEditor.nameMode === 'rename';
      return '<div class="naming-card">' +
        '<label class="eyebrow">' + (isRename ? 'Rename profile' : 'Name your profile') + '</label>' +
        '<input type="text" id="name-input" maxlength="48" autocomplete="off" value="' + escapeHtml(eqEditor.nameDraft) + '">' +
        '<div class="form-actions">' +
          '<button type="button" class="btn btn--primary" data-act="finalize-name">' +
            (isRename ? 'Rename' : 'Save profile') + '</button>' +
          '<button type="button" class="btn btn--ghost" data-act="cancel-name">Cancel</button>' +
        '</div></div>';
    }
    var dirty = draftModified();
    if (eqEditor.editing.kind === 'user') {
      return '<div class="form-actions">' +
          '<button type="button" class="btn btn--primary" data-act="overwrite" data-dirty-action' + (dirty ? '' : ' disabled') + '>Overwrite</button>' +
          '<button type="button" class="btn btn--ghost" data-act="begin-name">Save as new</button></div>' +
        '<div class="form-actions">' +
          '<button type="button" class="btn btn--ghost" data-act="begin-rename">Rename</button>' +
          '<button type="button" class="btn btn--ghost" data-act="reset-draft" data-dirty-action' +
            (dirty ? '' : ' disabled') + '>Reset draft</button></div>';
    }
    if (eqEditor.editing.kind === 'preset') {
      return '<div class="form-actions">' +
          '<button type="button" class="btn btn--primary" data-act="begin-name">Save as new</button>' +
          '<button type="button" class="btn btn--ghost" data-act="reset-draft" data-dirty-action' + (dirty ? '' : ' disabled') + '>Reset draft</button></div>';
    }
    return '<div class="form-actions">' +
        '<button type="button" class="btn btn--primary" data-act="begin-name">Save profile</button>' +
        '<button type="button" class="btn btn--ghost" data-act="reset-draft" data-dirty-action' + (dirty ? '' : ' disabled') + '>Reset draft</button></div>';
  }

  // ---- backend integration -------------------------------------------
  function schedulePreview() {
    renderLiveGraph();          // optimistic local graph
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(preview, 90);
  }
  async function preview() {
    var seq = ++previewSeq;
    try {
      var resp = await fetch('./preview', {method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify(liveProfile() || Object.assign(FLAT(), {enabled: false}))});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'preview failed');
      if (seq !== previewSeq) return;
      var profile = liveProfile();
      renderGraph(payload, profile ? profile.enabled !== false : false);
    } catch (e) {
      if (seq === previewSeq) status('Could not preview EQ: ' + e.message, true);
    }
  }
  function scheduleLiveDraft(immediate) {
    liveSeq += 1; livePending = true;
    window.clearTimeout(liveTimer);
    liveTimer = window.setTimeout(runLiveDraft, immediate ? 0 : 180);
  }
  function cancelLiveDrafts() { liveSeq += 1; livePending = false; window.clearTimeout(liveTimer); }
  async function runLiveDraft() {
    if (!livePending || applying || liveInFlight) return;
    livePending = false; liveInFlight = true;
    var seq = liveSeq;
    try {
      var resp = await fetch('./live-draft', {method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify({profile: draft, dsp_write_epoch: dspWriteEpoch})});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'live draft failed');
      if (seq === liveSeq) {
        if (payload.status === 'blocked') {
          // The loaded graph can't host EQ (e.g. an active crossover). Show
          // the server's honest hint; do not touch the draft/epoch state.
          status(payload.message || EQ_BLOCKED_MESSAGE, true);
          noteCarrierRefusal(payload);
          render();
        } else {
          if (payload.dsp_write_epoch) dspWriteEpoch = payload.dsp_write_epoch;
          if (payload.live_status === 'live') status('Listening to this draft live.');
          else if (payload.live_status === 'stale') status('Speaker DSP changed — move a control again to hear this draft.');
          else status('Live preview unavailable on this CamillaDSP connection.', true);
        }
      }
    } catch (e) {
      if (seq === liveSeq) status('Could not update live draft: ' + e.message, true);
    } finally {
      liveInFlight = false;
      if (livePending && !applying) { window.clearTimeout(liveTimer); liveTimer = window.setTimeout(runLiveDraft, 0); }
    }
  }
  // okMsg is shown only for explicit actions (save/overwrite). Tab-driven
  // applies (Off, Saved-select) pass none and stay silent on success — the
  // active tab + "Now playing" label already convey state. Errors always
  // surface (no silent failure).
  function requestLiveSource(options) {
    liveSourceSeq += 1;
    liveSourcePending = true;
    liveSourceOptions = options || {};
    return reconcileLiveSource();
  }
  async function reconcileLiveSource() {
    if (!liveSourcePending || applying) return;
    var options = liveSourceOptions || {};
    liveSourcePending = false;
    liveSourceOptions = {};
    var seq = liveSourceSeq;
    if (eqEditor.view === 'off') {
      return applyProfile(Object.assign(normalizeProfile(applied), {enabled: false}), options.okMsg, seq);
    }
    if (eqEditor.view === 'saved') {
      return applySavedSelection(options.okMsg, seq);
    }
    scheduleLiveDraft(options.immediate === false ? false : true);
  }
  async function applyProfile(profile, okMsg, sourceSeq) {
    sourceSeq = sourceSeq || liveSourceSeq;
    applying = true; cancelLiveDrafts();
    if (okMsg && sourceSeq === liveSourceSeq) status('Applying…');
    try {
      var resp = await fetch('./apply', {method: 'POST', headers: jsonHeaders(), body: JSON.stringify(profile)});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'apply failed');
      if (payload.status === 'blocked') {
        // Refused (e.g. EQ over an active crossover). Surface the honest hint
        // and skip ingestState — a blocked body carries no profile state.
        if (sourceSeq === liveSourceSeq) status(payload.message || EQ_BLOCKED_MESSAGE, true);
        noteCarrierRefusal(payload);   // the `finally` below renders it
      } else {
        ingestState(payload);
        if (sourceSeq === liveSourceSeq) status(okMsg || '');
      }
    } catch (e) {
      if (sourceSeq === liveSourceSeq) status('Could not apply: ' + e.message, true);
    } finally {
      applying = false; render();
      if (liveSourcePending) reconcileLiveSource();
    }
  }
  async function profileMutate(path, body) {
    applying = true;
    try {
      var resp = await fetch(path, {method: 'POST', headers: jsonHeaders(), body: JSON.stringify(body || {})});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'profile update failed');
      if (payload.profile_library) eqEditor.library = payload.profile_library;
      return payload;
    } catch (e) {
      status('Could not update profiles: ' + e.message, true);
      return null;
    } finally {
      applying = false;
      if (liveSourcePending) reconcileLiveSource();
    }
  }

  // Global sound settings. Optimistic: the controls already show the user's
  // input, so on success we just ingest; on failure we revert and re-render.
  async function saveSettings(patch) {
    var prev = outputPage.soundSettings;
    outputPage.soundSettings = Object.assign({}, outputPage.soundSettings, patch);
    try {
      var payload = await postJSON('./settings', patch);
      // The setting is saved either way; a blocked body says the loaded graph
      // refused to carry it, and the card holds that until the next save. The
      // card is the refusal's surface, so the status line is left for the
      // warnings a save can ALSO raise (a blocked body never carries
      // `warning` — same server branch — so in practice that is volume_warning).
      var blocked = payload.status === 'blocked';
      var blockChanged = blocked !== outputPage.blocked;
      outputPage.blocked = blocked;
      ingestState(payload);
      if (blockChanged) render();
      if (payload.warning) status(payload.warning, true);
      else if (payload.volume_warning) status(payload.volume_warning, true);
      return true;
    } catch (e) {
      outputPage.soundSettings = prev;
      status('Could not save sound settings: ' + e.message, true);
      render();
      return false;
    }
  }

  async function saveI2sHatProfileId(profileId, input) {
    if (input) input.disabled = true;
    try {
      var resp = await fetch('./i2s-hat', {
        method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify({profile_id: profileId || null})
      });
      var payload = await resp.json();
      if ('desired_profile_id' in payload) outputPage.i2sHat = payload;
      if (!resp.ok && 'desired_profile_id' in payload)
        return status('Setting saved, but the boot change could not be applied. Try again; if it still fails, open System and run diagnostics.', true);
      if (!resp.ok) throw new Error(payload.error || 'I²S HAT setting failed');
      if (payload.warnings && payload.warnings.length)
        return status(payload.warnings[0], true);
      status(payload.restart_required ?
        'I²S HAT setting saved. Restart required.' : 'I²S HAT setting saved.');
    } catch (e) {
      status('Could not save I²S HAT setting: ' + e.message, true);
    } finally {
      render();
    }
  }

  function setVolumeFloorToneButton() {
    var button = el('volume-floor-tone-button');
    if (!button) return;
    button.textContent = volumeFloorTone.active ? 'Stop tone' : 'Start tone';
  }

  function scheduleVolumeFloorToneUpdate(value, options) {
    options = options || {};
    value = Number(value);
    if (!isFinite(value)) return;
    if (!volumeFloorTone.active && !options.force) return;
    volumeFloorTone.pending = value;
    if (volumeFloorTone.timer) clearTimeout(volumeFloorTone.timer);
    volumeFloorTone.timer = setTimeout(function() {
      volumeFloorTone.timer = null;
      flushVolumeFloorToneUpdate();
    }, options.immediate ? 0 : 120);
  }

  async function flushVolumeFloorToneUpdate() {
    if (volumeFloorTone.inFlight) return;
    var value = volumeFloorTone.pending;
    var generation = volumeFloorTone.generation;
    volumeFloorTone.pending = null;
    if (value === null || value === undefined) return;
    volumeFloorTone.inFlight = true;
    try {
      var payload = await postJSON('./volume-floor/audition', {volume_floor_db: value});
      if (generation !== volumeFloorTone.generation) {
        if (!volumeFloorTone.active) stopVolumeFloorTone({quiet: true});
        return;
      }
      volumeFloorTone.active = true;
      setVolumeFloorToneButton();
      var toneStatus = '1% calibration tone at ' +
        fmtVolumeFloor(payload.volume_floor_db || value) + '.';
      if (volumeFloorTone.savedNotice) {
        toneStatus = 'Volume floor saved. ' + toneStatus;
        volumeFloorTone.savedNotice = false;
      }
      status(toneStatus);
    } catch (e) {
      volumeFloorTone.active = false;
      setVolumeFloorToneButton();
      status('Could not play volume-floor tone: ' + e.message, true);
    } finally {
      volumeFloorTone.inFlight = false;
      if (volumeFloorTone.pending !== null && volumeFloorTone.pending !== undefined) {
        flushVolumeFloorToneUpdate();
      }
    }
  }

  function startVolumeFloorTone() {
    volumeFloorTone.active = true;
    volumeFloorTone.generation += 1;
    volumeFloorTone.savedNotice = false;
    setVolumeFloorToneButton();
    scheduleVolumeFloorToneUpdate(volumeFloorValue(), {force: true, immediate: true});
  }

  async function resetVolumeFloor() {
    var floor = volumeFloorDefault();
    var floorInput = el('set-volume-floor');
    if (floorInput) floorInput.value = floor;
    setVolumeFloorDraft(floor);
    await saveVolumeFloor();
  }

  async function saveVolumeFloor() {
    var floor = volumeFloorValue();
    volumeFloorSaving = true;
    syncVolumeFloorControls(floor);
    var saved = await saveSettings({volume_floor_db: floor});
    volumeFloorSaving = false;
    if (saved) {
      outputPage.volumeFloorDraftDb = null;
      syncVolumeFloorControls(savedVolumeFloorDb());
      if (volumeFloorTone.active) {
        volumeFloorTone.savedNotice = true;
        scheduleVolumeFloorToneUpdate(savedVolumeFloorDb(), {immediate: true});
      } else {
        status('Volume floor saved.');
      }
    } else {
      syncVolumeFloorControls(volumeFloorValue());
    }
  }

  async function stopVolumeFloorTone(options) {
    options = options || {};
    volumeFloorTone.active = false;
    volumeFloorTone.generation += 1;
    volumeFloorTone.savedNotice = false;
    volumeFloorTone.pending = null;
    if (volumeFloorTone.timer) {
      clearTimeout(volumeFloorTone.timer);
      volumeFloorTone.timer = null;
    }
    setVolumeFloorToneButton();
    try {
      await postJSON('./volume-floor/stop', {reason: options.reason || 'stop'},
        {keepalive: !!options.keepalive});
      if (!options.quiet) status('Volume-floor tone stopped.');
    } catch (e) {
      if (!options.quiet) status('Could not stop volume-floor tone: ' + e.message, true);
    }
  }

  function ingestState(payload) {
    limits = Object.assign({}, LIMIT_DEFAULTS, payload.limits || {});
    eqEditor.simpleBands = limits.simple_bands || [];
    if (payload.curves) { eqEditor.curvesById = {}; payload.curves.forEach(function(c) { eqEditor.curvesById[c.id] = c; }); }
    if (payload.profile_library) eqEditor.library = payload.profile_library;
    if (payload.dsp_write_epoch) dspWriteEpoch = payload.dsp_write_epoch;
    if (payload.sound_settings) outputPage.soundSettings = payload.sound_settings;
    // Every /state and every successful apply carries the field, so a fixed
    // layout drops the block at the next render instead of needing a reload.
    var carrier = payload.eq_carrier;
    if (carrier) outputPage.eqCarrierBlock = carrier.status === 'blocked' ? carrier : null;
    applied = normalizeProfile(payload.profile || {});
  }

  // ---- tab + edit transitions ----------------------------------------
  function setView(v) {
    eqEditor.view = v;
    render();
    // Off and Saved are durable: clicking Off applies a bypass; tapping a
    // saved profile applies it (see selectSaved). Draft is a live, non-
    // persistent preview until the footer Save commits it.
    requestLiveSource({immediate: true});
  }
  function applySavedSelection(okMsg, sourceSeq) {
    var profile = selectedSavedProfile();
    if (!profile) {
      status('No saved profiles available.', true);
      return;
    }
    return applyProfile(profile, okMsg, sourceSeq);
  }
  function selectSaved(id) {
    eqEditor.selectedId = id;
    render();
    requestLiveSource({immediate: true});
  }
  function newDraft() {
    draft = FLAT(); eqEditor.editing = {kind: 'new'}; eqEditor.mode = 'simple'; eqEditor.activeBand = 0; resetEqEditor();
    eqEditor.view = 'draft'; status(''); render(); requestLiveSource({immediate: true});
  }
  function editEntry(id) {
    var entry = entryById(id);
    if (!entry) return;
    draft = normalizeProfile(entry.profile);
    eqEditor.editing = {kind: entry.kind === 'custom' ? 'user' : 'preset', id: entry.id, name: entry.name};
    eqEditor.mode = draft.parametric_bands.length ? 'peq' : 'simple';
    eqEditor.activeBand = 0; resetEqEditor(); eqEditor.view = 'draft';
    status('Editing ' + entry.name + '.'); render(); requestLiveSource({immediate: true});
  }
  // Body re-render + optimistic graph (via schedulePreview) + live audio.
  function onDraftChanged(immediate) { renderDraft(); schedulePreview(); requestLiveSource({immediate: immediate}); }
  function refreshDraftActionState() {
    var dirty = draftModified();
    el('view-body').querySelectorAll('[data-dirty-action]').forEach(function(btn) {
      btn.disabled = !dirty;
    });
  }
  function refreshActiveCount() {
    var e = el('active-count');
    if (!e) return;
    var n = eqEditor.mode === 'simple'
      ? Object.keys(draft.simple_eq).filter(function(k) {
        return Math.abs(draft.simple_eq[k]) >= ACTIVE_GAIN_EPSILON_DB;
      }).length
      : draft.parametric_bands.filter(function(b) { return b.enabled !== false; }).length;
    e.textContent = n + ' active';
  }
  // During a drag we patch the DOM in place (no full re-render, so the <input>
  // keeps focus). The visible thumb/fill is a separate element positioned by
  // inline style at render time, so move it here too — otherwise the handle
  // stays put while only the readout changes.
  function positionThumb(input) {
    var min = parseFloat(input.min), max = parseFloat(input.max);
    if (!(max > min)) return;
    var pct = (clamp(parseFloat(input.value), min, max) - min) / (max - min) * 100;
    var wrap = input.parentNode, hit;
    if ((hit = wrap.querySelector('.vrange__thumb'))) hit.style.bottom = 'calc(' + pct + '% - 6px)';
    else if ((hit = wrap.querySelector('.range__thumb'))) hit.style.left = 'calc(' + pct + '% - 6px)';
    else if ((hit = wrap.querySelector('.range__fill'))) hit.style.width = pct + '%';
  }

  // ---- events ---------------------------------------------------------
  // The Off/Saved/Draft tabs only exist on the solo page; a follower omits them.
  if (!followerMode && pageMode === 'eq') {
    ['off', 'saved', 'draft'].forEach(function(v) {
      el('tab-' + v).addEventListener('click', function() { if (eqEditor.view !== v) setView(v); });
    });
  }
  el('back').addEventListener('click', function(e) { e.preventDefault(); window.location.href = '/sound/'; });

  el('view-body').addEventListener('click', function(ev) {
    var t = ev.target.closest('[data-act]');
    if (!t) return;
    var act = t.getAttribute('data-act');
    var id = t.getAttribute('data-id');
    var index = Number(t.getAttribute('data-index'));
    if (act === 'browse-presets') { setView('saved'); }
    else if (act === 'new-draft') { newDraft(); }
    else if (act === 'select') { selectSaved(id); }
    else if (act === 'edit') { editEntry(id); }
    else if (act === 'delete') { deleteEntry(id); }
    else if (act === 'add-band') { addBand(); }
    else if (act === 'del-band') { delBand(index); }
    else if (act === 'toggle-band') { eqEditor.activeBand = (eqEditor.activeBand === index && !allCollapsed) ? -1 : index; allCollapsed = false; renderDraft(); renderLiveGraph(); }
    else if (act === 'toggle-collapse') { allCollapsed = !allCollapsed; renderDraft(); }
    else if (act === 'begin-name') { eqEditor.naming = true; eqEditor.nameMode = 'save'; eqEditor.nameDraft = defaultName(); renderDraft(); focusNameInput(); }
    else if (act === 'begin-rename') { eqEditor.naming = true; eqEditor.nameMode = 'rename'; eqEditor.nameDraft = eqEditor.editing.name || ''; renderDraft(); focusNameInput(); }
    else if (act === 'cancel-name') { resetEqEditor(); renderDraft(); }
    else if (act === 'finalize-name') { finalizeName(); }
    else if (act === 'overwrite') { overwrite(); }
    else if (act === 'reset-draft') { resetDraft(); }
    else if (act === 'refresh-output-topology') { refreshOutputTopology(); }
    else if (act === 'output-template-axis') {
      setOutputTemplateAxis(
        t.getAttribute('data-axis') || '',
        t.getAttribute('data-value') || ''
      );
    }
    else if (act === 'toggle-output-subwoofer') { toggleOutputSubwoofer(t.getAttribute('data-mode') || 'add'); }
    else if (act === 'output-step-next') { advanceOutputStep(t.getAttribute('data-step') || ''); }
    else if (act === 'save-output-topology') { saveOutputTopology(); }
    else if (act === 'reset-output-topology') { resetOutputTopology(); }
    else if (act === 'repin-output-topology') { repinOutputTopology(); }
    else if (act === 'copy-driver-research-prompt') { copyDriverResearchPrompt(t); }
    else if (act === 'parse-driver-research') { parseDriverResearchImport(); }
    else if (act === 'save-driver-design') { saveDriverResearchDraft(); }
    else if (act === 'prepare-crossover-preview') { prepareCrossoverPreview(); }
    else if (act === 'mark-output-identity') { updateOutputChannelIdentity(t); }
    else if (act === 'back-to-output-map') { backToOutputConfiguration(); }
    else if (act === 'back-to-crossover-config') { backToCrossoverConfiguration(); }
    else if (act === 'prepare-summed-test') { prepareSummedTest(t); }
    else if (act === 'stop-summed-test') { stopSummedTest(); }
    else if (act === 'record-summed-validation') { recordSummedValidation(t); }
    else if (act === 'save-apply-baseline-profile') { saveAndApplyBaselineProfile(); }
    else if (act === 'copy-tuning-handoff') { copyTuningHandoffPrompt(); }
    else if (act === 'commission-step') {
      startCommissionAutoRamp(t.getAttribute('data-role') || '', {
        confirm: false,
        identityAudition: t.getAttribute('data-identity-audition') === 'true'
      });
    }
    else if (act === 'commission-ack') {
      commissionAck(t.getAttribute('data-outcome') || '', {
        confirmOutputIdentity: t.getAttribute('data-confirm-output-identity') === 'true'
      });
    }
    else if (act === 'commission-abort') { commissionAbort(); }
    else if (act === 'toggle-volume-floor-tone') {
      if (volumeFloorTone.active) stopVolumeFloorTone();
      else startVolumeFloorTone();
    }
    else if (act === 'save-volume-floor') { saveVolumeFloor(); }
    else if (act === 'reset-volume-floor') { resetVolumeFloor(); }
  });
  // Mode + band-type segmented buttons (delegated).
  el('view-body').addEventListener('click', function(ev) {
    var modeBtn = ev.target.closest('[data-mode]');
    if (modeBtn) { switchMode(modeBtn.getAttribute('data-mode')); return; }
    var typeBtn = ev.target.closest('[data-band-type]');
    if (typeBtn) {
      var wrap = typeBtn.closest('[data-band]');
      var bi = Number(wrap.getAttribute('data-band'));
      if (draft.parametric_bands[bi]) {
        var nextType = typeBtn.getAttribute('data-band-type');
        var b = draft.parametric_bands[bi];
        var prevType = b.type || 'Peaking';
        b.type = nextType;
        // Cut/notch types carry no gain — zero it so a stale value can't
        // linger in the draft (the backend pins it to 0 on save anyway).
        if (GAINLESS_TYPES.indexOf(nextType) >= 0) b.gain_db = 0;
        // Switching INTO a high/low-pass: snap to Butterworth Q so a band
        // inheriting a high peaking-Q doesn't surprise the user with a large
        // resonant boost at the corner (a q=8 HPF peaks ~+18 dB). The user can
        // still widen/narrow it afterwards. Notch keeps its Q (it wants to be
        // narrow); shelves ignore Q entirely.
        if ((nextType === 'Highpass' || nextType === 'Lowpass') &&
            prevType !== 'Highpass' && prevType !== 'Lowpass') {
          b.q = 0.707;
        }
        eqEditor.activeBand = bi;
        onDraftChanged(true);
      }
    }
  });
  el('view-body').addEventListener('input', function(ev) {
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-target')) {
      var driverTarget = ev.target.getAttribute('data-driver-target');
      if (!driverResearch.inputs.target_models) driverResearch.inputs.target_models = {};
      driverResearch.inputs.target_models[driverTarget] = ev.target.value;
      driverResearch.error = '';
      driverResearch.dirty = true;
      driverResearch.safetyDirty = true;
      driverResearch.editedDriverTargets[driverTarget] = true;
      invalidateDriverResearchBinding();
      updateDriverResearchPromptPreview();
      updateDriverResearchPromptButton();
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-field')) {
      var driverField = ev.target.getAttribute('data-driver-field');
      driverResearch.inputs[driverField] = ev.target.value;
      driverResearch.error = '';
      driverResearch.dirty = true;
      driverResearch.safetyDirty = true;
      invalidateDriverResearchBinding();
      updateDriverResearchPromptPreview();
      updateDriverResearchPromptButton();
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-import')) {
      driverResearch.importText = ev.target.value;
      driverResearch.error = '';
      driverResearch.parsed = null;
      driverResearch.importedPayload = null;
      driverResearch.dirty = true;
      updateDriverResearchImportSummary();
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-manual-driver')) {
      setManualDriverField(
        ev.target.getAttribute('data-manual-driver') || '',
        ev.target.getAttribute('data-manual-field') || '',
        ev.target.value
      );
      updateDriverResearchPromptPreview();
      updateDriverResearchPromptButton();
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-manual-crossover')) {
      var crossoverKey = ev.target.getAttribute('data-manual-crossover') || '';
      var crossoverField = ev.target.getAttribute('data-manual-field') || '';
      setManualCrossoverField(crossoverKey, crossoverField, ev.target.value);
      // #1675: update the ka-beaming note live as Fc is typed, WITHOUT the
      // full render() a select-driven change gets elsewhere (that would drop
      // focus out of this number input on every keystroke). Mirrors the EQ
      // band sliders' own targeted-readout pattern just below in this same
      // listener (querySelector + textContent, not a repaint).
      if (crossoverField === 'frequency_hz') {
        var noteEl = el('view-body').querySelector(
          '[data-ka-note="' + crossoverKey + '"]'
        );
        if (noteEl) {
          noteEl.innerHTML = kaBeamingNoteHtml(
            crossoverKey.split(':'), ev.target.value, currentOutputTopology()
          );
        }
      }
      refreshDriverResearchDerivedUi();
      return;
    }
    var field = ev.target.getAttribute('data-field');
    var range = ev.target.getAttribute('data-range');
    var summedLevel = ev.target.getAttribute('data-summed-test-level');
    if (summedLevel) {
      var nextSummedLevel = combinedTestLevelDbfsFrom(ev.target.value);
      activeSpeaker.combinedTestLevelDbfs = nextSummedLevel;
      ev.target.value = nextSummedLevel;
      var summedReadout = el('view-body').querySelector(
        '[data-summed-level-readout="' + summedLevel + '"]'
      );
      if (summedReadout) summedReadout.textContent = fmtDb(nextSummedLevel);
      scheduleSummedTestLevelUpdate(summedLevel, nextSummedLevel);
      return;
    }
    // Continuous drag (this 'input' stream, one event per tick): update the
    // draft and the instant local/optimistic graph, but do not send a live
    // draft — that would duck audio once per tick (#3309 rejected skipping
    // the duck itself; this skips the redundant sends instead). The single
    // live-draft send fires on 'change' (release/keyboard-step commit) below.
    if (field) {
      draft.simple_eq[field] = clamp(ev.target.value, -limits.simple_gain_db, limits.simple_gain_db);
      var readout = el('view-body').querySelector('[data-readout-field="' + field + '"]');
      if (readout) readout.textContent = fmtDb(draft.simple_eq[field]);
      positionThumb(ev.target);
      refreshActiveCount();
      refreshDraftActionState();
      schedulePreview();
    } else if (range) {
      var row = ev.target.closest('.band-row');
      var bi = Number(row.getAttribute('data-index'));
      var band = draft.parametric_bands[bi];
      if (!band) return;
      eqEditor.activeBand = bi;
      if (range === 'freq') band.freq_hz = sliderToFreq(ev.target.value, limits.min_freq_hz, limits.max_freq_hz);
      if (range === 'gain') band.gain_db = clamp(ev.target.value, -limits.advanced_gain_db, limits.advanced_gain_db);
      if (range === 'q') band.q = clamp(ev.target.value, limits.min_q, bandQMax(band.type));
      var ro = row.querySelector('[data-readout="' + range + '"]');
      if (ro) ro.textContent = range === 'freq' ? fmtFreq(band.freq_hz) : (range === 'gain' ? fmtDb(band.gain_db) + ' dB' : fmtQ(band.q));
      positionThumb(ev.target);
      refreshDraftActionState();
      schedulePreview();
    }
  });
  el('view-body').addEventListener('input', function(ev) {
    if (ev.target.id === 'name-input') { eqEditor.nameDraft = ev.target.value; return; }
    if (ev.target.id === 'set-headroom') {
      var ro = el('set-headroom-readout');           // live readout; commit on 'change'
      if (ro) ro.textContent = fmtTrim(ev.target.value);
    }
    if (ev.target.id === 'set-volume-floor') {
      var floor = Number(ev.target.value);
      setVolumeFloorDraft(floor);
      scheduleVolumeFloorToneUpdate(volumeFloorValue());
    }
  });
  el('view-body').addEventListener('change', function(ev) {
    var field = ev.target.getAttribute('data-field');
    var range = ev.target.getAttribute('data-range');
    if (field) {
      var next = clamp(ev.target.value, -limits.simple_gain_db, limits.simple_gain_db);
      if (Math.abs(next) <= ZERO_DETENT_DB) next = 0;
      draft.simple_eq[field] = next;
      ev.target.value = next;
      var readout = el('view-body').querySelector('[data-readout-field="' + field + '"]');
      if (readout) readout.textContent = fmtDb(next);
      positionThumb(ev.target);
      refreshActiveCount();
      refreshDraftActionState();
      schedulePreview(); requestLiveSource({immediate: false});
      return;
    }
    // Advanced EQ band sliders (data-range): the 'input' stream above already
    // applied every tick to the draft and the local graph, sending no live
    // audio. The drag has now ended (or a keyboard step committed) — send
    // the one live-draft for it.
    if (range) {
      schedulePreview(); requestLiveSource({immediate: false});
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-manual-crossover')) {
      setManualCrossoverField(
        ev.target.getAttribute('data-manual-crossover') || '',
        ev.target.getAttribute('data-manual-field') || '',
        ev.target.value
      );
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-output-channel')) {
      setOutputChannelAssignment(
        ev.target.getAttribute('data-group-id') || '',
        ev.target.getAttribute('data-role') || '',
        ev.target.value
      );
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-style')) {
      var saveDriverStyle = ev.target.hasAttribute('data-save-driver-style');
      if (saveDriverStyle) outputPage.stepOverride = 'research';
      setOutputChannelDriverStyle(
        ev.target.getAttribute('data-group-id') || '',
        ev.target.getAttribute('data-role') || '',
        ev.target.value
      );
      if (saveDriverStyle) saveOutputTopology({nextStep: 'research'});
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-sub-crossover-fc')) {
      // Commit on change (not every keystroke) — setOutputDraft re-renders.
      setSubwooferCrossoverFc(ev.target.value);
      return;
    }
    if (ev.target.id === 'set-match-loudness') saveSettings({match_loudness: ev.target.checked});
    else if (ev.target.id === 'set-headroom') saveSettings({headroom_trim_db: Number(ev.target.value)});
    else if (ev.target.id === 'set-i2s-hat') saveI2sHatProfileId(ev.target.value, ev.target);
    else if (ev.target.id === 'set-volume-floor') {
      var floor = Number(ev.target.value);
      setVolumeFloorDraft(floor);
      if (volumeFloorTone.active) scheduleVolumeFloorToneUpdate(volumeFloorValue(), {immediate: true});
    }
  });
  el('view-body').addEventListener('toggle', function(ev) {
    if (ev.target && ev.target.matches && ev.target.matches('[data-driver-advanced]')) {
      driverAdvancedOpen = !!ev.target.open;
      return;
    }
    if (ev.target && ev.target.classList && ev.target.classList.contains('output-step') &&
        ev.target.open) {
      var step = ev.target.getAttribute('data-output-step') || outputPage.stepOverride;
      var topology = currentOutputTopology();
      if (!outputStepCanOpen(step, topology)) {
        ev.target.open = false;
        outputPage.stepOverride = defaultOutputStep();
        status('Finish the current card before opening ' + outputStepTitle(step) + '.', true);
        render();
        return;
      }
      outputPage.stepOverride = step;
      el('view-body').querySelectorAll('.output-step[open]').forEach(function(stepEl) {
        if (stepEl !== ev.target) stepEl.open = false;
      });
    }
  }, true);
  el('view-body').addEventListener('keydown', function(ev) {
    if (ev.target.id !== 'name-input') return;
    if (ev.key === 'Enter') { ev.preventDefault(); finalizeName(); }
    else if (ev.key === 'Escape') { ev.preventDefault(); resetEqEditor(); renderDraft(); }
  });

  function switchMode(next) {
    if (next === eqEditor.mode) return;
    if (next === 'simple') {
      // Snap to the simple template, copying nearest gain by log-frequency.
      var newSimple = zeroSimple();
      (eqEditor.simpleBands || []).forEach(function(slot) {
        var nearest = null, best = 1.2;
        draft.parametric_bands.filter(function(b) { return b.enabled !== false; }).forEach(function(b) {
          var dist = Math.abs(Math.log(b.freq_hz / slot.freq_hz) / Math.log(2));
          if (dist < best) { best = dist; nearest = b; }
        });
        if (nearest) newSimple[slot.field] = clamp(nearest.gain_db, -limits.simple_gain_db, limits.simple_gain_db);
      });
      draft.simple_eq = newSimple;
      draft.parametric_bands = [];
    } else {
      // Simple -> PEQ keeps the simple bands as gains; PEQ owns the bands going forward.
      draft.parametric_bands = (eqEditor.simpleBands || []).filter(function(s) {
        return Math.abs(draft.simple_eq[s.field] || 0) >= ACTIVE_GAIN_EPSILON_DB;
      }).map(function(s) {
        // Simple EQ shelves ignore Q in the backend, but carrying a stable
        // PEQ-side default keeps converted bands predictable if the type changes.
        return {enabled: true, type: s.type, freq_hz: s.freq_hz,
                gain_db: draft.simple_eq[s.field], q: 1.0};
      });
      draft.simple_eq = zeroSimple();
      eqEditor.activeBand = 0;
    }
    eqEditor.mode = next;
    onDraftChanged(true);
  }
  function addBand() {
    if (draft.parametric_bands.length >= limits.max_parametric_bands) {
      status('Advanced EQ is limited to ' + limits.max_parametric_bands + ' bands.', true);
      return;
    }
    draft.parametric_bands.push({enabled: true, type: 'Peaking', freq_hz: 1000, gain_db: 0, q: 1});
    eqEditor.activeBand = draft.parametric_bands.length - 1;
    onDraftChanged(true);
  }
  function delBand(index) {
    draft.parametric_bands.splice(index, 1);
    eqEditor.activeBand = Math.max(0, Math.min(eqEditor.activeBand, draft.parametric_bands.length - 1));
    onDraftChanged(true);
  }
  function resetDraft() {
    draft = sourceProfile();
    if (eqEditor.editing.kind !== 'new') draft = withIdentity(draft, eqEditor.editing.id, eqEditor.editing.name);
    eqEditor.mode = draft.parametric_bands.length ? 'peq' : 'simple';
    eqEditor.activeBand = 0; resetEqEditor();
    onDraftChanged(true);
  }
  function defaultName() {
    var d = new Date();
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return 'Profile · ' + months[d.getMonth()] + ' ' + d.getDate();
  }
  function focusNameInput() { var n = el('name-input'); if (n) { n.focus(); n.select(); } }
  async function finalizeName() {
    eqEditor.naming = false;
    if (eqEditor.nameMode === 'rename' && eqEditor.editing.kind === 'user') {
      var newName = (eqEditor.nameDraft || '').trim() || eqEditor.editing.name || defaultName();
      var rp = await profileMutate('./profiles/rename', {id: eqEditor.editing.id, name: newName});
      if (rp && rp.profile_entry) {
        if (eqEditor.selectedId === eqEditor.editing.id) eqEditor.selectedId = rp.profile_entry.id;
        eqEditor.editing = {kind: 'user', id: rp.profile_entry.id, name: rp.profile_entry.name};
        status('Renamed to ' + rp.profile_entry.name + '.');
      }
      render();
      return;
    }
    var name = (eqEditor.nameDraft || '').trim() || defaultName();
    var payload = await profileMutate('./profiles/save', {id: null, name: name, profile: draft});
    if (payload && payload.profile_entry) {
      var entry = payload.profile_entry;
      eqEditor.library = payload.profile_library || eqEditor.library;
      eqEditor.selectedId = entry.id; eqEditor.view = 'saved';
      await requestLiveSource({okMsg: 'Saved ' + entry.name + '.', immediate: true});
      render();
    } else { render(); }
  }
  async function overwrite() {
    if (eqEditor.editing.kind !== 'user') return;
    var payload = await profileMutate('./profiles/save', {id: eqEditor.editing.id, name: eqEditor.editing.name, profile: draft});
    if (payload && payload.profile_entry) {
      var entry = payload.profile_entry;
      eqEditor.selectedId = entry.id; eqEditor.view = 'saved';
      await requestLiveSource({okMsg: 'Updated ' + entry.name + '.', immediate: true});
      render();
    }
  }
  async function deleteEntry(id) {
    var entry = entryById(id);
    if (!entry || entry.kind !== 'custom') return;
    if (!await jtsConfirm('Delete profile "' + entry.name + '"?', { danger: true })) return;
    var payload = await profileMutate('./profiles/delete', {id: id});
    if (payload) {
      status('Deleted ' + entry.name + '.');
      if (eqEditor.selectedId === id) {
        eqEditor.selectedId = fallbackSavedId();
        render();
        requestLiveSource({immediate: true});
      } else {
        render();
      }
    }
  }
  function ingestOutputTopology(payload) {
    var topology = payload && (payload.output_topology || payload);
    outputTopology.payload = topology || null;
    outputTopology.draft = topology ? clone(topology) : null;
    outputTopology.identity = payload && payload.channel_identity || topology && topology.channel_identity || null;
    outputTopology.clockDomain = payload && payload.clock_domain || topology && topology.clock_domain || null;
    outputTopology.activeRoute = payload && payload.active_playback_route || null;
    outputTopology.observedHardware = payload && payload.output_hardware || null;
    outputTopology.hardwareAdoption = payload && payload.hardware_adoption || null;
    outputTopology.hardwareMismatch = payload && payload.hardware_mismatch || null;
    outputTopology.hardwareRepin = payload && payload.hardware_repin || null;
    outputPage.i2sHat = payload && payload.i2s_hat || outputPage.i2sHat;
    outputTopology.revision = payload && payload.topology_revision || null;
    outputTopology.error = '';
    outputTopology.dirty = false;
    outputTopology.saving = false;
    outputTopology.resetting = false;
    outputTopology.repinning = false;
    outputTopology.loading = false;
    outputTopology.identitySaving = '';
    outputTopology.protectionSaving = '';
    if (outputGroups(topology).length) resetOutputPage();
  }
  // The Output page renders the HAT picker and the sound settings, nothing
  // else, so it reads the topology payload for `i2s_hat` alone and skips the
  // six crossover/commissioning reads only the speaker page draws.
  async function loadOutputHardware() {
    try {
      var resp = await fetch('./output-topology', {cache: 'no-store'});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker layout load failed');
      ingestOutputTopology(payload);
    } catch (e) {
      outputTopology.error = e.message;
    }
    render();
  }
  async function refreshOutputTopology(options) {
    options = options || {};
    if (!options.silent && outputTopology.dirty &&
        !await jtsConfirm('Refresh hardware and lose the unsaved speaker layout draft?')) {
      return;
    }
    if (!options.silent) outputTopology.touched = true;
    outputTopology.loading = true;
    outputTopology.error = '';
    if (!options.silent) render();
    try {
      var resp = await fetch('./output-topology', {cache: 'no-store'});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker layout load failed');
      ingestOutputTopology(payload);
      try {
        await fetchDesignDraft();
      } catch (draftError) {
        driverResearch.designDraft = {
          status: 'unreadable',
          summary: {},
          issues: [{message: draftError.message}]
        };
      }
      try {
        await fetchCrossoverPreview();
      } catch (previewError) {
        crossoverPreview.payload = null;
        crossoverPreview.error = previewError.message;
      }
      try {
        patchActiveSpeaker({measurements: await fetchActiveSpeakerMeasurements()});
      } catch (measurementError) {
        patchActiveSpeaker({measurements: activeSpeaker.measurements || null});
      }
      try {
        patchActiveSpeaker({baselineProfile: await fetchActiveSpeakerBaselineProfile()});
      } catch (profileError) {
        patchActiveSpeaker({baselineProfile: activeSpeaker.baselineProfile || null});
      }
      await refreshCommissionState();
      await refreshCommissioningView();
    } catch (e) {
      outputTopology.loading = false;
      outputTopology.error = e.message;
    }
    render();
  }
  async function refreshCommissionState() {
    try {
      var resp = await fetch('./active-speaker/commission-state', {cache: 'no-store'});
      if (resp.ok) patchActiveSpeaker({commission: await resp.json()});
    } catch (commissionError) {
      patchActiveSpeaker({commission: activeSpeaker.commission || null});
    }
  }
  async function refreshCommissioningView() {
    try {
      var resp = await fetch('./active-speaker/commissioning-view', {cache: 'no-store'});
      if (resp.ok) patchActiveSpeaker({commissioningView: await resp.json()});
    } catch (viewError) {
      patchActiveSpeaker({commissioningView: activeSpeaker.commissioningView || null});
    }
  }
  async function postCommission(url, body, busyLabel) {
    var showBusy = !!busyLabel;
    if (showBusy) {
      patchActiveSpeaker({commissionBusy: busyLabel, commissionError: ''});
      render();
    } else if (activeSpeaker.commissionError) {
      patchActiveSpeaker({commissionError: ''});
      render();
    }
    try {
      var resp = await fetch(url, {
        method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify(body || {})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error((payload && payload.error) || 'request failed');
      var failure = commissionPayloadFailure(payload);
      if (failure) {
        // The request was accepted (HTTP 200) but a guard refused/blocked it.
        // Show why instead of silently re-rendering the unchanged state — the
        // "flicker then nothing" bug. Refresh first so the card reflects the
        // persisted (still-unarmed) state alongside the reason.
        await refreshCommissionState();
        patchActiveSpeaker({commissionBusy: '', commissionError: failure});
        render();
        return {ok: false, payload: payload, error: failure};
      }
      // Success path is inside the try so a throw from the refresh/render calls
      // is handled like any other postCommission error instead of rejecting the
      // un-awaited runCommissionAutoRamp promise (which would wedge the
      // single-flight flag — the symmetric half of the C3a-7 fix).
      if (payload && payload.measurements) {
        patchActiveSpeaker({measurements: payload.measurements});
      }
      if (payload && payload.output_topology) {
        ingestOutputTopology(payload);
      }
      await refreshCommissionState();
      await refreshCommissioningView();
      if (showBusy) patchActiveSpeaker({commissionBusy: ''});
      render();
      return {ok: true, payload: payload};
    } catch (e) {
      patchActiveSpeaker({commissionBusy: '', commissionError: String(e.message || e)});
      render();
      return {ok: false, error: String(e.message || e)};
    }
  }
  async function runActiveSpeakerAction(options, operation) {
    options = options || {};
    patchActiveSpeaker(Object.assign({
      loading: false,
      action: options.busyLabel || '',
      error: ''
    }, options.beginPatch || {}));
    render();
    try {
      return {ok: true, value: await operation()};
    } catch (e) {
      var current = !options.isCurrent || options.isCurrent();
      if (current) {
        if (options.onError) options.onError(e);
        var message = String(e.message || e);
        patchActiveSpeaker({loading: false, action: '', error: message});
        if (options.errorPrefix) status(options.errorPrefix + message, true);
        render();
      }
      return {ok: false, current: current, error: e};
    }
  }
  async function commissionArm(role, options) {
    options = options || {};
    var group = activeCommissionGroup(currentOutputTopology());
    if (!group || !role) return;
    var load = activeSpeaker.commission && activeSpeaker.commission.commission_load || {};
    var target = load.target || {};
    var targetGroup = target.speaker_group_id || group.id;
    var force = load.status === 'loaded' &&
      (targetGroup !== group.id || (target.role || '') !== role);
    var body = {group: group.id, role: role, force: force};
    if (options.identityAudition) body.identity_audition = true;
    return await postCommission('./active-speaker/commission-load',
      body, 'Getting ' + humanRole(role) + ' ready');
  }
  async function ensureCommissionArmed(role, options) {
    options = options || {};
    var group = activeCommissionGroup(currentOutputTopology());
    if (!group || !role) return {ok: false, error: 'Choose a driver first.'};
    var load = activeSpeaker.commission && activeSpeaker.commission.commission_load || {};
    var target = load.target || {};
    var targetGroup = target.speaker_group_id || group.id;
    if (load.status === 'loaded' &&
        targetGroup === group.id &&
        (target.role || '') === role) {
      return {ok: true, payload: {status: 'loaded', load: load}};
    }
    return await commissionArm(role, options);
  }
  async function commissionStep(role, options) {
    options = options || {};
    var group = activeCommissionGroup(currentOutputTopology());
    if (!group || !role) return;
    if (options.confirm !== false) {
      var ok = await jtsConfirm('Make the ' + humanRole(role) + ' audible? Amps should be ' +
        'on at LOW gain — JTS will play it very quietly through the crossover.',
        {danger: true});
      if (!ok) return;
    }
    var busyLabel = Object.prototype.hasOwnProperty.call(options, 'busyLabel') ?
      options.busyLabel : 'Stepping ' + humanRole(role);
    var body = {group: group.id, role: role};
    if (options.autoRetryPending) body.auto_retry_pending = true;
    if (options.identityAudition) body.identity_audition = true;
    return await postCommission('./active-speaker/commission-ramp-step',
      body, busyLabel);
  }
  async function commissionAck(outcome, options) {
    options = options || {};
    if (!outcome) return;
    stopCommissionAutoRamp('');
    var body = {outcome: outcome};
    if (options.confirmOutputIdentity) body.confirm_output_identity = true;
    var result = await postCommission('./active-speaker/commission-ramp-ack',
      body, 'Recording');
    var confirmed = !!(result && result.payload &&
      result.payload.status === 'confirmed');
    if (outcome === 'heard_correct_driver' && confirmed) {
      if (driverTargetProofComplete()) {
        outputPage.stepOverride = 'safety';
        status('Outputs and drivers are confirmed. Continue with the combined speaker test.');
      } else {
        status('Driver confirmation saved. Continue with the next output.');
      }
      render();
    }
    return result;
  }
  async function commissionAbort() {
    stopCommissionAutoRamp('Stopped. No test tone is playing.');
    await postCommission('./active-speaker/commission-ramp-abort', {}, 'Re-muting');
  }
  async function backToOutputConfiguration() {
    var pending = commissionPendingStep();
    if (commissionAutoRamp.running || pending) {
      stopCommissionAutoRamp('Stopped. Check the channel assignments before testing again.');
      await postCommission('./active-speaker/commission-ramp-abort', {}, 'Re-muting');
    }
    outputPage.stepOverride = 'map';
    status('Check the DAC channel assignments, save, then confirm the wiring again.');
    render();
  }
  function backToCrossoverConfiguration() {
    outputPage.stepOverride = 'research';
    status('Review the crossover settings, then return to validation.');
    render();
  }
  async function stopAndAbortCommissionAutoRamp(message) {
    stopCommissionAutoRamp(message);
    await postCommission('./active-speaker/commission-ramp-abort', {}, 'Re-muting');
    patchActiveSpeaker({commissionBusy: '', commissionError: message});
    status(message, true);
    render();
  }
  async function runCommissionAutoRamp(groupId, role, token) {
    try {
      while (commissionAutoRampCurrent(groupId, role, token)) {
        var result = await commissionStep(role, {
          confirm: false,
          busyLabel: '',
          autoRetryPending: !!commissionPendingStep(),
          identityAudition: !!commissionAutoRamp.identityAudition
        });
        if (!commissionAutoRampCurrent(groupId, role, token)) return;
        if (!result || !result.ok) {
          var stopMessage = result && result.error ?
            result.error : 'Stopped. JTS could not play the driver test.';
          if (result && result.payload &&
              commissionPayloadHasIssue(result.payload, 'commission_ramp_at_limit')) {
            stopCommissionAutoRamp(stopMessage);
            patchActiveSpeaker({commissionBusy: '', commissionError: stopMessage});
            status(stopMessage, true);
            render();
            return;
          }
          await stopAndAbortCommissionAutoRamp(stopMessage);
          return;
        }
        if (!commissionAutoRampCurrent(groupId, role, token)) {
          await stopAndAbortCommissionAutoRamp('Stopped because the active driver test changed.');
          return;
        }
        var payload = result.payload || {};
        var level = Number(payload.next_gain_db);
        commissionAutoRamp = Object.assign({}, commissionAutoRamp, {
          stepCount: commissionAutoRamp.stepCount + 1,
          levelDbfs: isFinite(level) ? level : commissionAutoRamp.levelDbfs,
          message: 'Tone is playing for ' + humanRole(role) + '.'
        });
        render();
        await sleepMs(COMMISSION_RAMP_LISTEN_MS);
        if (!commissionAutoRampCurrent(groupId, role, token)) return;
        await sleepMs(COMMISSION_RAMP_NEXT_PULSE_MS);
      }
    } finally {
      // Single-flight release for ALL loop exits — normal completion, the
      // drift-based commissionAutoRampCurrent()-false returns, and any uncaught
      // throw (e.g. a render() error on a happy-path step). Token-guarded so we
      // only clear OUR own run: a newer run (or an explicit stop, which both
      // bump the token) leaves commissionAutoRamp.token !== token, so we skip.
      if (commissionAutoRamp.running && commissionAutoRamp.token === token) {
        stopCommissionAutoRamp('');
      }
    }
  }
  async function startCommissionAutoRamp(role, options) {
    options = options || {};
    var group = activeCommissionGroup(currentOutputTopology());
    if (!group || !role) return;
    var targetKey = commissionTargetKey(group.id, role);
    if (commissionAutoRamp.running) {
      if (commissionAutoRamp.targetKey === targetKey) {
        status('The ' + humanRole(role) + ' tone is already starting or playing.');
      } else {
        status('Stop the current driver tone before starting another one.', true);
      }
      render();
      return;
    }
    if (options.confirm !== false) {
      var ok = await jtsConfirm('Start the ' + humanRole(role) + ' quiet ramp? Amps should be ' +
        'on at LOW gain — JTS will play one continuous tone that gets louder over about 30 seconds.',
        {danger: true});
      if (!ok) return;
    }
    var token = commissionAutoRamp.token + 1;
    commissionAutoRamp = {
      running: true,
      token: token,
      targetKey: targetKey,
      stepCount: 0,
      levelDbfs: null,
      identityAudition: !!options.identityAudition,
      message: options.message || 'Getting ' + humanRole(role) + ' ready.'
    };
    var rampStarted = false;
    try {
      var armed = await ensureCommissionArmed(role, {
        identityAudition: !!options.identityAudition
      });
      if (!armed || !armed.ok) {
        stopCommissionAutoRamp('');
        render();
        return;
      }
      if (!commissionAutoRampCurrent(group.id, role, token)) return;
      commissionAutoRamp = Object.assign({}, commissionAutoRamp, {
        message: options.message || 'Starting quiet continuous ' + humanRole(role) + ' test.'
      });
      status('Starting quiet continuous ' + humanRole(role) + ' test. Press Stop if anything sounds wrong.');
      render();
      rampStarted = true;
      runCommissionAutoRamp(group.id, role, token);
    } finally {
      // runCommissionAutoRamp is fire-and-forget (not awaited). Once we've handed
      // off, its own try/finally releases the single-flight flag for every loop
      // exit, so we only reset here if the ramp never started — i.e. an
      // unexpected throw occurred before handoff. Token-guarded so we never clear
      // a newer run's flag.
      if (!rampStarted && commissionAutoRamp.running && commissionAutoRamp.token === token) {
        stopCommissionAutoRamp('');
      }
    }
  }
  function setOutputDraft(next) {
    outputTopology.draft = next;
    if (outputGroups(next).length) resetOutputPage();
    outputTopology.dirty = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    driverResearch.dirty = true;
    driverResearch.safetyDirty = true;
    invalidateDriverResearchBinding();
    crossoverPreview.payload = null;
    crossoverPreview.error = '';
    render();
  }
  // Persist the user-entered bass-management corner onto the draft local-sub
  // channel's crossover_fc_hz, clamped to the safe band. Mutating the draft marks
  // it dirty; the existing topology save button POSTs it (round-trips through
  // SpeakerChannel.from_mapping/to_dict). No-op when no local sub is in the draft.
  function setSubwooferCrossoverFc(value) {
    var topology = currentOutputTopology();
    if (!topology) return;
    var next = baseOutputDraft(topology);
    if (!next) return;
    var fc = clampSubwooferCrossoverFcHz(value);
    var changed = false;
    (next.speaker_groups || []).forEach(function(group) {
      if (!group || (group.kind !== 'subwoofer' && group.mode !== 'subwoofer')) return;
      (group.channels || []).forEach(function(channel) {
        if (channel && channel.role === 'subwoofer') {
          channel.crossover_fc_hz = fc;
          changed = true;
        }
      });
    });
    if (!changed) return;
    setOutputDraft(next);
  }
  function setOutputChannelAssignment(groupId, role, rawValue) {
    var topology = currentOutputTopology();
    if (!topology) return;
    var next = baseOutputDraft(topology);
    if (!next) return;
    var selected = rawValue === '' ? null : Number(rawValue);
    if (selected !== null && !isFinite(selected)) {
      status('Choose a valid DAC channel.', true);
      return;
    }
    var outputs = physicalOutputOptions(next);
    var outputIndexes = outputs.map(function(output) { return Number(output.index); });
    if (selected !== null && outputIndexes.indexOf(selected) < 0) {
      status('Choose one of the available DAC channels.', true);
      return;
    }
    var targetGroup = null;
    var targetChannel = null;
    outputGroups(next).forEach(function(group) {
      if ((group.id || '') !== groupId) return;
      (group.channels || []).forEach(function(channel) {
        if ((channel.role || '') === role) {
          targetGroup = group;
          targetChannel = channel;
        }
      });
    });
    if (!targetGroup || !targetChannel) {
      status('Could not find that driver in the speaker layout.', true);
      return;
    }
    function applyChannel(channel, index) {
      channel.physical_output_index = index;
      channel.identity_verified = false;
      delete channel.human_output_label;
    }
    var previousSelected = targetChannel.physical_output_index == null ?
      null : Number(targetChannel.physical_output_index);
    var swapPeer = null;
    if (selected !== null && Array.isArray(targetGroup.channels) &&
        targetGroup.channels.length === 2) {
      targetGroup.channels.forEach(function(channel) {
        if (channel !== targetChannel &&
            Number(channel.physical_output_index) === selected) {
          swapPeer = channel;
        }
      });
    }
    applyChannel(targetChannel, selected);
    if (swapPeer) applyChannel(swapPeer, previousSelected);
    outputPage.stepOverride = 'map';
    setOutputDraft(next);
    status('Channel assignment updated. Save before confirming the wiring.');
  }
  // Sets the safety-relevant driver_style on a topology channel (the same
  // single writer as physical_output_index — see setOutputChannelAssignment
  // above). driver_style is topology-owned, not part of manual_settings /
  // driver_research: build_driver_safety_profile reads it straight off the
  // topology channel, so a style change here folds into the safety profile's
  // fingerprint and is picked up by the next save, the same as any other
  // topology/output change (docs/active-crossover-information-design.md,
  // "Hardware research and confirmed safety profile").
  function setOutputChannelDriverStyle(groupId, role, rawValue) {
    var topology = currentOutputTopology();
    if (!topology) return;
    var next = baseOutputDraft(topology);
    if (!next) return;
    var targetChannel = null;
    outputGroups(next).forEach(function(group) {
      if ((group.id || '') !== groupId) return;
      (group.channels || []).forEach(function(channel) {
        if ((channel.role || '') === role) targetChannel = channel;
      });
    });
    if (!targetChannel) {
      status('Could not find that driver in the speaker layout.', true);
      return;
    }
    var value = String(rawValue || '').trim();
    if (value) targetChannel.driver_style = value;
    else delete targetChannel.driver_style;
    setOutputDraft(next);
    status('Tweeter style updated.');
  }
  function outputTemplateDefinition(kind) {
    return {
      mono_passive: {
        id: 'mono_passive',
        label: 'Mono passive',
        hint: 'One full-range channel',
        minOutputs: 1,
        name: 'Mono passive output',
        groups: [{
          id: 'main', label: 'Main speaker', kind: 'mono',
          mode: 'full_range_passive',
          position: {x: 0, y: 0.42, rotation_degrees: 0},
          channels: [outputChannel('full_range', 0)]
        }],
        routing: {mono_group_id: 'main'}
      },
      mono_active_2way: {
        id: 'mono_active_2way',
        label: 'Mono active 2-way',
        hint: 'Woofer + tweeter',
        minOutputs: 2,
        name: 'Mono active 2-way output',
        groups: [{
          id: 'main', label: 'Main speaker', kind: 'mono',
          mode: 'active_2_way',
          position: {x: 0, y: 0.42, rotation_degrees: 0},
          channels: [outputChannel('woofer', 0), outputChannel('tweeter', 1)]
        }],
        routing: {mono_group_id: 'main'}
      },
      mono_active_3way: {
        id: 'mono_active_3way',
        label: 'Mono active 3-way',
        hint: 'Woofer + mid + tweeter',
        minOutputs: 3,
        name: 'Mono active 3-way output',
        groups: [{
          id: 'main', label: 'Main speaker', kind: 'mono',
          mode: 'active_3_way',
          position: {x: 0, y: 0.42, rotation_degrees: 0},
          channels: [
            outputChannel('woofer', 0),
            outputChannel('mid', 1),
            outputChannel('tweeter', 2)
          ]
        }],
        routing: {mono_group_id: 'main'}
      },
      stereo_passive: {
        id: 'stereo_passive',
        label: 'Stereo passive',
        hint: 'Left + right full-range',
        minOutputs: 2,
        name: 'Stereo passive outputs',
        groups: [
          {
            id: 'left', label: 'Left speaker', kind: 'left',
            mode: 'full_range_passive',
            position: {x: -0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('full_range', 0)]
          },
          {
            id: 'right', label: 'Right speaker', kind: 'right',
            mode: 'full_range_passive',
            position: {x: 0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('full_range', 1)]
          }
        ],
        routing: {main_left_group_id: 'left', main_right_group_id: 'right'}
      },
      stereo_active_2way: {
        id: 'stereo_active_2way',
        label: 'Stereo active 2-way',
        hint: 'Two channels per speaker',
        minOutputs: 4,
        name: 'Stereo active 2-way outputs',
        groups: [
          {
            id: 'left', label: 'Left speaker', kind: 'left',
            mode: 'active_2_way',
            position: {x: -0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('woofer', 0), outputChannel('tweeter', 1)]
          },
          {
            id: 'right', label: 'Right speaker', kind: 'right',
            mode: 'active_2_way',
            position: {x: 0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('woofer', 2), outputChannel('tweeter', 3)]
          }
        ],
        routing: {main_left_group_id: 'left', main_right_group_id: 'right'}
      },
      stereo_active_3way: {
        id: 'stereo_active_3way',
        label: 'Stereo active 3-way',
        hint: 'Three channels per speaker',
        minOutputs: 6,
        name: 'Stereo active 3-way outputs',
        groups: [
          {
            id: 'left', label: 'Left speaker', kind: 'left',
            mode: 'active_3_way',
            position: {x: -0.65, y: 0.42, rotation_degrees: 0},
            channels: [
              outputChannel('woofer', 0),
              outputChannel('mid', 1),
              outputChannel('tweeter', 2)
            ]
          },
          {
            id: 'right', label: 'Right speaker', kind: 'right',
            mode: 'active_3_way',
            position: {x: 0.65, y: 0.42, rotation_degrees: 0},
            channels: [
              outputChannel('woofer', 3),
              outputChannel('mid', 4),
              outputChannel('tweeter', 5)
            ]
          }
        ],
        routing: {main_left_group_id: 'left', main_right_group_id: 'right'}
      }
    }[kind] || null;
  }
  async function setOutputTemplate(kind, options) {
    options = options || {};
    if (outputTopology.dirty && !options.skipDirtyConfirm &&
        !await jtsConfirm('Replace the unsaved speaker layout draft?')) {
      return;
    }
    var next = baseOutputDraft();
    if (!next || !next.hardware) {
      status('Load output hardware before creating a speaker layout.', true);
      return;
    }
    var keepSubwoofer = outputHasSubwoofer(next);
    var count = Number(next.hardware.physical_output_count) || 0;
    var template = outputTemplateDefinition(kind);
    if (!template) {
      status('Choose a supported speaker layout template.', true);
      return;
    }
    resetOutputPage();
    if (count < template.minOutputs) {
      status(template.name + ' needs at least ' + template.minOutputs +
        ' physical output' + (template.minOutputs === 1 ? '.' : 's.'), true);
      return;
    }
    var unavailableReason = outputTemplateUnavailableReason(
      template,
      next,
      keepSubwoofer
    );
    if (unavailableReason) {
      status(unavailableReason, true);
      return;
    }
    next.name = template.name;
    next.speaker_groups = template.groups;
    next.routing = {
      main_left_group_id: template.routing.main_left_group_id || null,
      main_right_group_id: template.routing.main_right_group_id || null,
      mono_group_id: template.routing.mono_group_id || null,
      subwoofer_group_ids: template.routing.subwoofer_group_ids || []
    };
    if (keepSubwoofer) {
      next = addSubwooferToTopology(next) || next;
    }
    setOutputDraft(next);
    status(
      keepSubwoofer && !outputHasSubwoofer(next)
        ? 'Speaker layout draft updated. Subwoofer was removed because no spare output remains.'
        : 'Speaker layout is a draft. Save to validate; no sound will play.'
    );
  }
  async function setOutputTemplateAxis(axis, value) {
    var topology = currentOutputTopology();
    if (!topology) {
      status('Load output hardware before creating a speaker layout.', true);
      return;
    }
    var axes = outputTemplateAxesForTopology(topology);
    var layout = axis === 'layout' ? value : axes.layout;
    var speakerMode = axis === 'speaker-mode' ? value : axes.speakerMode;
    outputPage.templateDraftAxes = {layout: layout || '', speakerMode: speakerMode || ''};
    if (!layout || !speakerMode) {
      status(layout ? 'Choose passive, active 2-way, or active 3-way to continue.' :
        'Choose mono or stereo to continue.');
      render();
      return;
    }
    var kind = outputTemplateKindFromAxes(layout, speakerMode);
    if (!kind) {
      status('Choose a supported speaker layout option.', true);
      return;
    }
    await setOutputTemplate(kind, {skipDirtyConfirm: true});
  }
  function toggleOutputSubwoofer(modeValue) {
    var topology = currentOutputTopology();
    if (!topology) {
      status('Load output hardware before editing the speaker layout.', true);
      return;
    }
    var next = modeValue === 'remove'
      ? removeSubwooferFromTopology(topology)
      : addSubwooferToTopology(topology);
    if (!next) {
      status('Could not update subwoofer draft.', true);
      return;
    }
    if (modeValue !== 'remove' && !outputHasSubwoofer(next)) {
      status('No unused physical output is available for a subwoofer.', true);
      return;
    }
    if (modeValue !== 'remove') {
      var axes = outputTemplateAxesForTopology(next);
      var template = outputTemplateDefinition(
        outputTemplateKindFromAxes(axes.layout, axes.speakerMode)
      );
      var unavailableReason = outputTemplateUnavailableReason(template, next, true);
      if (unavailableReason) {
        status(unavailableReason, true);
        return;
      }
    }
    setOutputDraft(next);
    status(modeValue === 'remove' ?
      'Removed subwoofer from the speaker layout draft.' :
      'Added subwoofer to the speaker layout draft. Save before verification.');
  }
  function updateDriverResearchPromptPreview() {
    var prompt = el('driver-research-prompt');
    if (prompt) prompt.value = driverResearchPrompt(currentOutputTopology());
  }
  function updateDriverResearchPromptButton() {
    var button = document.querySelector('[data-act="copy-driver-research-prompt"]');
    if (!button) return;
    var ready = driverResearchPromptReady(currentOutputTopology());
    button.disabled = !ready;
    button.textContent = promptCopyState(driverResearch.promptCopy).label;
  }
  function updateDriverResearchImportSummary() {
    var summary = el('driver-research-import-summary');
    if (summary) summary.innerHTML = renderDriverResearchSummary();
  }
  // A minted prompt's textarea stays hidden until a copy is BLOCKED, at which
  // point it is shown compact and pre-selected so the operator can finish the
  // copy by hand. `stale` demotes a completed copy back to an offer.
  function promptCopyState(state, stale) {
    var selected = state.selected && !state.copied;
    return {
      selected: selected,
      promptClass: 'driver-research__textarea' + (selected ?
        ' driver-research__textarea--compact' : ' driver-research__textarea--hidden'),
      label: (state.copied && !stale) ? 'Copied' :
        (selected ? 'Selected' : 'Copy prompt')
    };
  }
  // The shared tail of every copy-a-box-minted-prompt control: copy, record
  // which of copied/selected happened, repaint, and never leave a blocked copy
  // without selected text.
  async function copyPromptField(fieldId, state, copiedMessage, afterRender) {
    var field = el(fieldId);
    if (!field) return;
    var copied = await copyTextToClipboard(field.value, field);
    state.copied = copied;
    state.selected = !copied;
    render();
    if (afterRender) afterRender();
    if (!copied) {
      var fallback = el(fieldId);
      if (fallback) {
        fallback.focus();
        fallback.select();
        fallback.setSelectionRange(0, fallback.value.length);
      }
    }
    status(copied ? copiedMessage :
      'Copy was blocked by the browser. Prompt text is selected.', !copied);
  }
  async function copyTextToClipboard(text, sourceElement) {
    var secureContext = typeof window !== 'undefined' && window.isSecureContext;
    if (document.execCommand && !secureContext) {
      return copyTextViaCopyEvent(text) || copyTextViaSelection(text, sourceElement);
    }
    var clipboard = typeof navigator !== 'undefined' && navigator.clipboard &&
      navigator.clipboard.writeText ? navigator.clipboard : null;
    if (clipboard) {
      try {
        await clipboard.writeText(text);
        return true;
      } catch (e) {
        // Fall back for local HTTP management pages where the async clipboard
        // API is unavailable or denied outside a secure context.
      }
    }
    return copyTextViaCopyEvent(text) || copyTextViaSelection(text, sourceElement);
  }
  function copyTextViaCopyEvent(text) {
    if (!document.execCommand || !document.addEventListener) return false;
    var copied = false;
    var handler = function(event) {
      if (!event.clipboardData) return;
      event.preventDefault();
      event.clipboardData.setData('text/plain', text);
      copied = true;
    };
    document.addEventListener('copy', handler);
    try {
      document.execCommand('copy');
    } catch (eventCopyError) {
      copied = false;
    } finally {
      document.removeEventListener('copy', handler);
    }
    return copied;
  }
  function copyTextViaSelection(text, sourceElement) {
    if (!document.execCommand) return false;
    var temporary = null;
    var target = sourceElement;
    var previousStyle = null;
    if (!target) {
      temporary = document.createElement('textarea');
      temporary.value = text;
      temporary.setAttribute('readonly', '');
      temporary.style.position = 'fixed';
      temporary.style.top = '0';
      temporary.style.left = '0';
      temporary.style.width = '2px';
      temporary.style.height = '2px';
      temporary.style.opacity = '1';
      temporary.style.color = 'transparent';
      temporary.style.background = 'transparent';
      temporary.style.border = '0';
      temporary.style.padding = '0';
      document.body.appendChild(temporary);
      target = temporary;
    } else if (target.style) {
      previousStyle = target.getAttribute ? target.getAttribute('style') : null;
      target.style.position = 'fixed';
      target.style.top = '0';
      target.style.left = '0';
      target.style.width = '2px';
      target.style.height = '2px';
      target.style.minHeight = '0';
      target.style.opacity = '1';
      target.style.pointerEvents = 'auto';
      target.style.color = 'transparent';
      target.style.background = 'transparent';
      target.style.border = '0';
      target.style.padding = '0';
      target.style.zIndex = '2147483647';
    }
    target.focus();
    target.select();
    target.setSelectionRange(0, target.value.length);
    var copied = false;
    try {
      copied = document.execCommand('copy');
    } catch (fallbackError) {
      copied = false;
    }
    if (previousStyle !== null && target.setAttribute) {
      target.setAttribute('style', previousStyle);
    } else if (sourceElement && target.removeAttribute) {
      target.removeAttribute('style');
    }
    if (temporary) document.body.removeChild(temporary);
    return copied;
  }
  async function copyDriverResearchPrompt(button) {
    var prompt = el('driver-research-prompt');
    if (!prompt) return;
    if (!driverResearchPromptReady(currentOutputTopology())) {
      status('Add each model and choose its enclosure or tweeter type before copying the research prompt.', true);
      return;
    }
    try {
      var response = await fetch('./active-speaker/driver-research-request', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          operator_inputs: driverResearch.inputs,
          manual_settings: manualSettingsPayload(currentOutputTopology())
        })
      });
      var payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'research prompt preparation failed');
      prompt.value = String(payload.prompt || '');
      driverResearch.researchRequest = payload.request || null;
    } catch (e) {
      status('Could not prepare the target-bound research prompt: ' + e.message, true);
      return;
    }
    await copyPromptField('driver-research-prompt', driverResearch.promptCopy,
      'Copied driver research prompt.', updateDriverResearchPromptButton);
  }
  async function copyTuningHandoffPrompt() {
    var field = el('tuning-handoff-prompt');
    if (!field) return;
    try {
      var resp = await fetch('./active-speaker/tuning-handoff', {cache: 'no-store'});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'handoff prompt could not be minted');
      if (payload.status !== 'ready') {
        throw new Error('this speaker has no applied profile to hand over yet');
      }
      tuningHandoff.prompt = String(payload.prompt || '');
      tuningHandoff.copiedRevision = (payload.binding || {}).design_draft_revision;
      field.value = tuningHandoff.prompt;
    } catch (e) {
      status('Could not prepare the AI operator prompt: ' + e.message, true);
      return;
    }
    await copyPromptField('tuning-handoff-prompt', tuningHandoff,
      'Copied the AI operator prompt.');
  }
  function parseDriverResearchImport() {
    try {
      var payload = extractDriverResearchJson(driverResearch.importText);
      driverResearch.parsed = summarizeDriverResearchPayload(payload);
      driverResearch.importedPayload = payload;
      applyDriverResearchToManualSettings(payload);
      driverResearch.error = '';
      driverResearch.dirty = true;
      driverResearch.safetyDirty = true;
      driverResearch.editedDriverTargets = {};
      driverResearch.promptCopy.copied = false;
      driverResearch.promptCopy.selected = false;
      status('Imported driver research. Review the visible values before updating the working setup.');
    } catch (e) {
      driverResearch.parsed = null;
      driverResearch.importedPayload = null;
      driverResearch.error = e.message;
      status('Imported JSON needs review: ' + e.message, true);
    }
    render();
  }
  async function saveDriverResearchDraft(options) {
    options = options || {};
    if (!driverResearch.dirty && driverResearchStepSatisfied() && options.nextStep) {
      outputPage.stepOverride = options.nextStep;
      status(driverResearchCanPreparePreview() ?
        'Working setup is already current. Preview crossover before confirming outputs.' :
        'Save driver names and crossover points before confirming outputs.');
      render();
      return true;
    }
    if (outputTopology.dirty) {
      status('Save the speaker layout before updating the working setup.', true);
      return false;
    }
    if (!currentOutputTopology()) {
      status('Load output hardware before updating the working setup.', true);
      return false;
    }
    var manualTopology = currentOutputTopology();
    var manualError = manualCrossoverDelayValidationError(manualTopology) ||
      manualCrossoverVocabularyValidationError(manualTopology);
    if (manualError) {
      driverResearch.error = manualError;
      status(manualError, true);
      render();
      return false;
    }
    var manualPayload = manualSettingsPayload(currentOutputTopology());
    var researchPayload = null;
    var importWarning = '';
    if ((driverResearch.importText || '').trim()) {
      try {
        researchPayload = extractDriverResearchJson(driverResearch.importText);
        driverResearch.parsed = summarizeDriverResearchPayload(researchPayload);
        driverResearch.importedPayload = researchPayload;
        if (driverResearch.parsed.schemaVersion === 2 &&
            !driverResearch.researchRequest) {
          researchPayload = null;
          importWarning = 'Target-bound research was invalidated by a visible edit.';
        }
        // Both drop paths behave the same way: whatever caused the packet to
        // be dropped reaches the PANEL, not just the status line, which the
        // operator's next ordinary click overwrites (#2186).
        driverResearch.error = importWarning;
      } catch (e) {
        driverResearch.parsed = null;
        driverResearch.importedPayload = null;
        researchPayload = null;
        importWarning = e.message;
        driverResearch.error = manualPayload
          ? 'Imported JSON was not saved: ' + e.message
          : e.message;
        if (!manualPayload) {
          status('Imported JSON needs review: ' + e.message, true);
          render();
          return false;
        }
      }
    }
    driverResearch.saving = true;
    // An import rejection recorded just above is the operator's only account of
    // why the pasted packet was dropped. Clearing it unconditionally erased
    // that reason before the first render, which is what made the drop read as
    // silent (#2186). Keep it whenever something actually was rejected.
    if (!importWarning) driverResearch.error = '';
    render();
    try {
      var resp = await fetch('./active-speaker/design-draft', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          operator_inputs: driverResearch.inputs,
          manual_settings: manualPayload,
          driver_research_request: driverResearch.researchRequest,
          driver_research: researchPayload,
          expected_revision: Number((driverResearch.designDraft || {}).revision || 0)
        })
      });
      var payload = await resp.json();
      if (resp.status === 409) {
        var keptLocalEdits = driverResearch.dirty;
        ingestDesignDraft(payload, {force: !keptLocalEdits});
        var conflictMessage = payload.error || 'Speaker design changed in another tab.';
        driverResearch.error = keptLocalEdits
          ? conflictMessage + ' Your unsaved edits were kept; review and save again.'
          : conflictMessage + ' Review the refreshed values.';
        status(driverResearch.error, true);
        render();
        return false;
      }
      if (!resp.ok) throw new Error(payload.error || 'speaker design draft save failed');
      // The saved draft carries no driver_research when this save dropped the
      // packet, so ingestDesignDraft would blank both the paste box and the
      // rejection reason -- leaving an explanation with nothing to act on.
      // Hand the operator back what they pasted and why it was refused (#2186).
      var rejectedImport = importWarning
        ? {text: driverResearch.importText, error: driverResearch.error}
        : null;
      ingestDesignDraft(payload, {force: true});
      if (rejectedImport) {
        driverResearch.importText = rejectedImport.text;
        driverResearch.error = rejectedImport.error;
      }
      crossoverPreview.payload = null;
      crossoverPreview.error = '';
      if (options.nextStep) outputPage.stepOverride = options.nextStep;
      if (!options.forPreview) {
        status(importWarning
          ? 'Working setup updated from visible fields. Imported JSON was not saved: ' +
            importWarning
          : 'Working setup updated. No filters are active and no sound was played.',
          !!importWarning);
      }
      render();
      return true;
    } catch (e) {
      driverResearch.saving = false;
      driverResearch.error = e.message;
      status('Could not update working setup: ' + e.message, true);
      render();
      return false;
    }
  }
  async function prepareCrossoverPreview() {
    if (driverResearch.saving) {
      status('Working setup is still updating. Try the preview again in a moment.');
      return false;
    }
    if (outputTopology.dirty) {
      status('Save the speaker layout before preparing the crossover preview.', true);
      return false;
    }
    if (!driverResearchPreviewInputsReady(currentOutputTopology())) {
      status(driverResearchMissingPreviewMessage(currentOutputTopology()), true);
      return false;
    }
    if (driverResearch.dirty || !driverResearchStepSatisfied()) {
      if (!await saveDriverResearchDraft({forPreview: true})) return false;
    }
    if (!driverResearchCanPreparePreview()) {
      status(driverResearchMissingPreviewMessage(currentOutputTopology()), true);
      return false;
    }
    crossoverPreview.preparing = true;
    crossoverPreview.error = '';
    render();
    try {
      var resp = await fetch('./active-speaker/crossover-preview', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'crossover preview failed');
      ingestCrossoverPreview(payload);
      await refreshCommissioningView();
      outputPage.stepOverride = 'map';
      status('Crossover preview ready. No sound was played. Confirm the outputs next.');
      render();
      return true;
    } catch (e) {
      crossoverPreview.preparing = false;
      crossoverPreview.error = e.message;
      status('Could not prepare crossover preview: ' + e.message, true);
      render();
      return false;
    }
  }
  async function advanceOutputStep(step) {
    var topology = currentOutputTopology();
    if (step === 'layout') {
      if (!topology || !outputGroups(topology).length) {
        outputPage.stepOverride = 'layout';
        status('Choose a speaker layout before continuing.', true);
        render();
        return;
      }
      if (outputTopology.dirty) {
        await saveOutputTopology({nextStep: 'research'});
        return;
      }
      openOutputStep('research');
      status('Speaker layout is already saved. Continue with driver research or skip ahead.');
      return;
    }
    if (step === 'research') {
      if (driverResearch.dirty || !driverResearchStepSatisfied()) {
        if (!await saveDriverResearchDraft({forPreview: true})) return;
      }
      if (activeCommissionGroup(topology) &&
          !crossoverPreviewReadyForProtectedStaging(crossoverPreview.payload)) {
        if (!await prepareCrossoverPreview()) return;
      }
      openOutputStep('map');
      status('Driver and crossover values are ready. Confirm the outputs.');
      return;
    }
    if (step === 'map') {
      if (outputTopology.dirty) {
        outputPage.stepOverride = 'map';
        status('Save the speaker layout before confirming outputs.', true);
        render();
        return;
      }
      if (!driverTargetProofComplete()) {
        var report = outputIdentityReport();
        var assigned = Number(report && report.assigned_channel_count || 0);
        outputPage.stepOverride = 'map';
        status(assigned > 0 ?
          'Play and confirm every assigned driver before continuing.' :
          'Save a speaker layout with assigned outputs before continuing.', true);
        render();
        return;
      }
      openOutputStep('safety');
      status(commissioningStepNotRequired('safety') ?
        'Every output is confirmed. This speaker needs no crossover checks.' :
        'Outputs and drivers are confirmed. Continue with the combined speaker test.');
      return;
    }
    if (step === 'safety') {
      if (summedValidationComplete()) {
        openOutputStep('profile');
        status('Combined speaker check is saved. Save and apply the active profile.');
        return;
      }
      outputPage.stepOverride = 'safety';
      status('Run the combined speaker test and save what you heard before applying.');
      render();
      return;
    }
    if (step === 'profile') {
      outputPage.stepOverride = 'profile';
      status(baselineProfileApplied() ?
        'The active speaker profile is applied.' :
        'Finish the combined crossover check, then save and apply the active profile.');
      render();
    }
  }
  async function saveOutputTopology(options) {
    options = options || {};
    if (!outputTopology.draft) return;
    outputTopology.saving = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    render();
    try {
      var resp = await fetch('./output-topology', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          output_topology: outputTopology.draft,
          topology_revision: outputTopology.revision
        })
      });
      var payload = await resp.json();
      if (!resp.ok) {
        if (resp.status === 409 && payload.output_topology) {
          ingestOutputTopology(payload);
          outputTopology.error = payload.error || 'Speaker layout changed; refresh before saving.';
          status(outputTopology.error, true);
          render();
          return;
        }
        throw new Error(payload.error || 'speaker layout save failed');
      }
      ingestOutputTopology(payload);
      // The refusal card names the carrier this save just replaced, so a fixed
      // layout drops it at the render below instead of outliving its cause.
      outputPage.blocked = false;
      try {
        await fetchDesignDraft();
      } catch (draftError) {
        driverResearch.designDraft = {
          status: 'unreadable',
          summary: {},
          issues: [{message: draftError.message}]
        };
      }
      // A research request is fingerprinted to the saved topology, including
      // topology-owned installation facts such as tweeter type. The design
      // draft fetched above may still contain the prior binding, so invalidate
      // it after every successful topology save instead of letting Copy remain
      // visibly "done" for a request the server will reject as stale.
      invalidateDriverResearchBinding();
      try {
        await fetchCrossoverPreview();
      } catch (previewError) {
        crossoverPreview.payload = null;
        crossoverPreview.error = previewError.message;
      }
      await refreshCommissioningView();
      if (options.nextStep) outputPage.stepOverride = options.nextStep;
      var saveStatus = payload && payload.save || {};
      var needsAttention = saveStatus.status === 'needs_attention';
      status(
        saveStatus.message || 'Saved speaker layout.',
        needsAttention
      );
    } catch (e) {
      outputTopology.saving = false;
      outputTopology.error = e.message;
      status('Could not save speaker layout: ' + e.message, true);
    }
    render();
  }
  async function resetOutputTopology() {
    if (outputTopology.resetting) return;
    var ok = await jtsConfirm(
      'This clears the current speaker setup. If usable hardware is detected, it will be shown after reset. Audio stays off until you choose a speaker layout.',
      {title: 'Reset speaker setup?', confirmLabel: 'Reset speaker setup', danger: true}
    );
    if (!ok) return;
    outputTopology.resetting = true;
    outputTopology.error = '';
    render();
    try {
      var resp = await fetch('./output-topology/reset', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          topology_revision: outputTopology.revision,
          detected_hardware_identity: outputTopology.hardwareAdoption &&
            outputTopology.hardwareAdoption.identity
        })
      });
      var payload = await resp.json();
      if (!resp.ok) {
        if (resp.status === 409 && payload.output_topology) {
          ingestOutputTopology(payload);
          var conflictMessage = payload.error ||
            'Speaker setup or detected hardware changed. Review it and try again.';
          status(conflictMessage, true);
          render();
          return;
        }
        throw new Error(payload.error || 'speaker setup reset failed');
      }
      ingestOutputTopology(payload);
      stopCommissionAutoRamp('');
      patchActiveSpeaker({
        commission: null,
        commissioningView: null,
        measurements: null,
        baselineProfile: null,
        error: '',
        commissionBusy: '',
        commissionError: ''
      });
      outputPage.stepOverride = 'layout';
      var resetStatus = payload && payload.reset || {};
      if (resetStatus.status === 'needs_attention') {
        outputTopology.error = resetStatus.message || 'Speaker setup was reset, but JTS requires attention before continuing.';
        status(outputTopology.error, true);
      } else {
        status(resetStatus.message || 'Speaker setup was reset. Audio is off until you choose a speaker layout.');
      }
    } catch (e) {
      outputTopology.resetting = false;
      status('Could not reset speaker setup: ' + e.message, true);
    }
    render();
  }
  async function repinOutputTopology() {
    if (outputTopology.repinning) return;
    var plan = outputTopology.hardwareRepin;
    if (!plan) return;
    var labels = Array.isArray(plan.reverify_output_labels) ?
      plan.reverify_output_labels : [];
    var ok = await jtsConfirm(
      'JTS keeps your speaker layout, driver roles, output assignment and ' +
      'tuning, and pins the DAC attached now. You then confirm ' +
      (labels.length ? labels.join(' and ') : 'the affected outputs') +
      ' by ear — audio stays off until you do and the speaker re-arms — and ' +
      're-run the drift measurement for the new pair.',
      // danger: the speaker goes silent immediately and the pair's drift
      // measurement is dropped, so a stray Enter must not land on confirm.
      {title: 'Pin the new DAC?', confirmLabel: 'Pin the new DAC', danger: true}
    );
    if (!ok) return;
    outputTopology.repinning = true;
    outputTopology.error = '';
    render();
    try {
      var resp = await fetch('./output-topology/repin', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          topology_revision: outputTopology.revision,
          detected_hardware_identity: outputTopology.hardwareAdoption &&
            outputTopology.hardwareAdoption.identity
        })
      });
      var payload = await resp.json();
      if (!resp.ok) {
        if (resp.status === 409 && payload.output_topology) {
          ingestOutputTopology(payload);
          status(payload.error ||
            'Speaker setup or detected hardware changed. Review it and try again.',
            true);
          render();
          return;
        }
        throw new Error(payload.error || 'pinning the new DAC failed');
      }
      ingestOutputTopology(payload);
      // The commissioning design SURVIVES a re-pin, so nothing about it is
      // cleared here (unlike the reset above). Only the in-flight ramp is
      // stopped, because the server parked the graph it was pulsing.
      stopCommissionAutoRamp('');
      await refreshCommissioningView();
      // Let the backend's own current step win: identity is now unverified for
      // the replaced lanes, so the derived default lands on the right rung.
      outputPage.stepOverride = '';
      var repinStatus = payload && payload.repin || {};
      if (repinStatus.status === 'needs_attention') {
        outputTopology.error = repinStatus.message ||
          'The new DAC was pinned, but JTS requires attention before continuing.';
        status(outputTopology.error, true);
      } else {
        status(repinStatus.message || 'Pinned the new DAC and kept your speaker setup.');
      }
    } catch (e) {
      outputTopology.repinning = false;
      status('Could not pin the new DAC: ' + e.message, true);
    }
    render();
  }
  async function updateOutputChannelIdentity(button) {
    if (outputTopology.dirty) {
      status('Save the speaker layout before confirming outputs.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var verified = button.getAttribute('data-verified') !== 'false';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var message = verified
      ? 'Confirm that "' + label + '" is wired to the driver shown here?'
      // Un-confirming a driver lane silences the speaker at the click (the
      // server parks it), so the dialog says so and reads as destructive.
      : 'Mark "' + label + '" as not confirmed? The speaker goes silent until ' +
        'you confirm it again and the speaker re-arms.';
    if (commissionAutoRamp.running || commissionPendingStep()) {
      stopCommissionAutoRamp('');
      var abortResult = await postCommission('./active-speaker/commission-ramp-abort', {}, 'Re-muting');
      if (!abortResult || !abortResult.ok) return;
    }
    if (!await jtsConfirm(message, {danger: !verified})) {
      status('Stopped the test tone. Output confirmation was not changed.');
      return;
    }
    outputTopology.identitySaving = groupId + ':' + role;
    outputTopology.error = '';
    outputTopology.touched = true;
    render();
    try {
      var resp = await fetch('./active-speaker/channel-identity', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          speaker_group_id: groupId,
          role: role,
          identity_verified: verified
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'channel identity update failed');
      ingestOutputTopology(payload);
      await refreshCommissioningView();
      // When the server had to silence the speaker for this write, its own
      // sentence wins: only it knows whether the immediate park landed.
      var park = payload && payload.identity_park;
      if (park && park.message) {
        status(park.message, !park.parked);
      } else {
        status((verified ? 'Confirmed output: ' : 'Cleared output confirmation: ') + label + '.');
      }
    } catch (e) {
      outputTopology.identitySaving = '';
      outputTopology.error = e.message;
      status('Could not update channel identity: ' + e.message, true);
    }
    render();
  }
  function clearSummedTestArmTimer() {
    if (summedTestRequest.armTimer) {
      window.clearTimeout(summedTestRequest.armTimer);
      summedTestRequest.armTimer = null;
    }
  }
  function clearSummedTestLevelTimer() {
    if (summedTestLevelUpdate.timer) {
      window.clearTimeout(summedTestLevelUpdate.timer);
      summedTestLevelUpdate.timer = null;
    }
  }
  function scheduleSummedTestLevelUpdate(groupId, levelDbfs, options) {
    options = options || {};
    if (activeSpeaker.action !== 'Playing combined test') return;
    summedTestLevelUpdate.pending = {
      groupId: groupId,
      levelDbfs: combinedTestLevelDbfsFrom(levelDbfs)
    };
    clearSummedTestLevelTimer();
    summedTestLevelUpdate.timer = window.setTimeout(function() {
      summedTestLevelUpdate.timer = null;
      flushSummedTestLevelUpdate();
    }, options.immediate ? 0 : 120);
  }
  async function flushSummedTestLevelUpdate() {
    if (summedTestLevelUpdate.inFlight) return;
    var pending = summedTestLevelUpdate.pending;
    summedTestLevelUpdate.pending = null;
    if (!pending) return;
    summedTestLevelUpdate.inFlight = true;
    try {
      var resp = await fetch('./active-speaker/summed-test/level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          speaker_group_id: pending.groupId,
          level_dbfs: pending.levelDbfs
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'combined test level failed');
      if (payload.status === 'idle') return;
      if (payload.status !== 'loaded') {
        throw new Error(payload.reason || 'combined test level was not applied');
      }
      patchActiveSpeaker({
        calibrationLevel: payload.calibration_level || activeSpeaker.calibrationLevel,
        combinedTestLevelDbfs: pending.levelDbfs
      });
    } catch (e) {
      status('Could not update combined test level: ' + e.message, true);
    } finally {
      summedTestLevelUpdate.inFlight = false;
      if (summedTestLevelUpdate.pending) flushSummedTestLevelUpdate();
    }
  }
  function latestSummedTestIdFromPayload(payload, groupId) {
    var measurements = payload && payload.measurements || activeSpeaker.measurements || {};
    var summary = measurements.summary || {};
    var latest = summary.latest_summed_tests || {};
    var test = latest[String(groupId || '')] || null;
    if (!test || test.captured !== true || test.audio_emitted !== true) return '';
    return test && (test.summed_test_id || test.playback_id) || '';
  }
  async function finishPlayingSummedTestForValidation(groupId) {
    var current = summedTestRequest.current;
    if (!current || current.groupId !== groupId || !current.promise) return '';
    await stopSummedTest({reason: 'operator_confirmed', quiet: true});
    var payload = await current.promise;
    return latestSummedTestIdFromPayload(payload, groupId);
  }
  async function prepareSummedTest(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var label = button.getAttribute('data-label') || groupId || 'speaker';
    if (!groupId) {
      status('Choose the speaker group to test.', true);
      return;
    }
    if (!await jtsConfirm(
      'Play a spoken combined test for "' + label +
        '" at ' + fmtDb(combinedTestLevelDbfs()) +
        '? JTS uses the prepared crossover, keeps the test level bounded, and ' +
        'stops the test when it finishes.',
      {danger: true}
    )) {
      return;
    }
    var requestedLevel = combinedTestLevelDbfs();
    var requestToken = summedTestRequest.token + 1;
    summedTestRequest.token = requestToken;
    clearSummedTestArmTimer();
    clearSummedTestLevelTimer();
    summedTestLevelUpdate.pending = null;
    var result = await runActiveSpeakerAction({
      busyLabel: 'Starting combined test',
      beginPatch: {combinedTestLevelDbfs: requestedLevel},
      errorPrefix: 'Could not start the combined speaker test: ',
      isCurrent: function() {
        return summedTestRequest.token === requestToken;
      },
      onError: function() {
        clearSummedTestArmTimer();
        if (summedTestRequest.current &&
            summedTestRequest.current.token === requestToken) {
          summedTestRequest.current = null;
        }
      }
    }, async function() {
      summedTestRequest.armTimer = window.setTimeout(function() {
        if (summedTestRequest.token !== requestToken ||
            activeSpeaker.action !== 'Starting combined test') {
          return;
        }
        patchActiveSpeaker({
          loading: false,
          action: 'Playing combined test',
          error: '',
          combinedTestLevelDbfs: requestedLevel
        });
        render();
      }, SUMMED_TEST_STOP_ARM_MS);
      var groupView = commissioningGroupView(groupId);
      var action = commissioningGroupAction(groupView, 'start_combined_test');
      var body = Object.assign({
        speaker_group_id: groupId,
        audio: true,
        stimulus: 'speech',
        duration_ms: 12000
      }, action && action.body || {});
      body.level_dbfs = requestedLevel;
      var startPromise = fetch(action && action.endpoint || './active-speaker/summed-test', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      }).then(async function(resp) {
        var payload = await resp.json();
        if (!resp.ok) throw new Error(payload.error || 'combined speaker test failed');
        return payload;
      });
      summedTestRequest.current = {
        token: requestToken,
        groupId: groupId,
        promise: startPromise,
        payload: null
      };
      var payload = await startPromise;
      if (summedTestRequest.current &&
          summedTestRequest.current.token === requestToken) {
        summedTestRequest.current.payload = payload;
      }
      if (summedTestRequest.token !== requestToken) return false;
      clearSummedTestArmTimer();
      var appliedLevel = NaN;
      if (payload.calibration_level && payload.calibration_level.test_signal) {
        appliedLevel = Number(payload.calibration_level.test_signal.requested_level_dbfs);
      }
      patchActiveSpeaker({
        loading: false,
        action: '',
        session: payload.session || activeSpeaker.session,
        calibrationLevel: payload.calibration_level || activeSpeaker.calibrationLevel,
        measurements: payload.measurements || activeSpeaker.measurements,
        error: '',
        combinedTestLevelDbfs: isFinite(appliedLevel) ? appliedLevel : requestedLevel
      });
      await refreshCommissioningView();
      var playback = payload.playback || {};
      var emitted = playbackConfirmable(playback);
      if (playback.stop_reason !== 'operator_confirmed') {
        status(playback.status === 'stopped' ?
          'Combined speaker test stopped.' : (emitted ?
          'Combined speaker test played. Record what you heard.' :
          summedTestFailureBanner(groupId)),
          playback.status !== 'stopped' && !emitted);
      }
      if (summedTestRequest.current &&
          summedTestRequest.current.token === requestToken) {
        summedTestRequest.current = null;
      }
      return true;
    });
    if (!result.ok || result.value === false) return;
    render();
  }
  async function stopSummedTest(options) {
    options = options || {};
    var requestToken = summedTestRequest.token;
    var payload = null;
    clearSummedTestArmTimer();
    clearSummedTestLevelTimer();
    summedTestLevelUpdate.pending = null;
    var result = await runActiveSpeakerAction({
      busyLabel: 'Stopping combined test',
      errorPrefix: 'Could not stop the combined speaker test: ',
      isCurrent: function() {
        return summedTestRequest.token === requestToken;
      }
    }, async function() {
      payload = await postJSON('./active-speaker/summed-test/stop',
        {reason: options.reason || 'operator_stop'},
        {keepalive: !!options.keepalive});
      if (summedTestRequest.token !== requestToken) {
        return {payload: payload, stale: true};
      }
      patchActiveSpeaker({
        loading: false,
        action: '',
        error: ''
      });
      await refreshCommissioningView();
      if (!options.quiet) {
        status(payload.status === 'idle' ?
          'No combined speaker test is playing.' :
          'Combined speaker test stopped.');
      }
      return {payload: payload, stale: false};
    });
    if (!result.ok) {
      if (result.current) throw result.error;
      return payload;
    }
    if (result.value.stale) return result.value.payload;
    render();
    return result.value.payload;
  }
  async function recordSummedValidation(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var outcome = button.getAttribute('data-outcome') || '';
    var summedTestId = button.getAttribute('data-summed-test-id') || '';
    if (!groupId || !outcome) {
      status('Choose a speaker group and validation result before saving the combined check.', true);
      return;
    }
    var groupView = commissioningGroupView(groupId);
    var localActiveTest = summedTestRequest.current &&
      summedTestRequest.current.groupId === groupId &&
      summedTestRequest.current.promise &&
      (activeSpeaker.action === 'Starting combined test' ||
        activeSpeaker.action === 'Playing combined test');
    if (localActiveTest) {
      try {
        summedTestId = await finishPlayingSummedTestForValidation(groupId);
      } catch (e) {
        status('Could not finish the combined speaker test: ' + e.message, true);
        return;
      }
    } else if (groupView && groupView.summed_test_active === true) {
      status('Stop the combined speaker test before recording the check from this tab.', true);
      return;
    }
    if (!summedTestId) {
      status('Run the combined speaker test first, then record what you heard.', true);
      return;
    }
    var result = await runActiveSpeakerAction({
      busyLabel: 'Saving combined check',
      errorPrefix: 'Could not save combined crossover check: '
    }, async function() {
      var groupView = commissioningGroupView(groupId);
      var action = commissioningGroupAction(groupView, 'record_combined_result');
      var body = Object.assign({}, action && action.body || {}, {
        speaker_group_id: groupId,
        outcome: outcome,
        summed_test_id: summedTestId || action && action.body && action.body.summed_test_id || '',
        operator_listening_check: true,
        polarity: 'normal'
      });
      var resp = await fetch(
        action && action.endpoint || './active-speaker/summed-validation',
        {
          method: 'POST',
          headers: jsonHeaders(),
          body: JSON.stringify(body)
        }
      );
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'combined crossover check failed');
      patchActiveSpeaker({
        loading: false,
        action: '',
        measurements: payload,
        baselineProfile: activeSpeaker.baselineProfile,
        error: ''
      });
      await refreshCommissioningView();
      try {
        patchActiveSpeaker({baselineProfile: await fetchActiveSpeakerBaselineProfile()});
      } catch (profileError) {
        patchActiveSpeaker({baselineProfile: activeSpeaker.baselineProfile});
      }
      if (summedValidationComplete()) {
        outputPage.stepOverride = 'profile';
        status('Combined crossover check saved. Save and apply the active profile when ready.');
      } else {
        var latestValidations = payload && payload.summary &&
          payload.summary.latest_summed_validations || {};
        var latestValidation = latestValidations[String(groupId || '')] || {};
        var issues = Array.isArray(latestValidation.issues) ? latestValidation.issues : [];
        var blocker = issues.find(function(issue) {
          return issue && issue.severity === 'blocker';
        });
        status('Combined crossover check did not count yet: ' +
          (blocker && blocker.message || 'run the combined test again, then save the result.'),
          true);
      }
    });
    if (!result.ok) return;
    render();
  }
  async function saveAndApplyBaselineProfile() {
    var profile = activeSpeaker.baselineProfile || {};
    var config = profile.config || {};
    var readyToApply = (profile.permissions || {}).may_apply === true;
    var mayCompile = summedValidationComplete();
    var applyBlocked = baselineProfileApplyBlocked(profile);
    var configName = config.basename || 'active speaker baseline';
    if (applyBlocked) {
      status('This active profile cannot be made active from here yet. Review the issue in this card.', true);
      return;
    }
    if (!readyToApply && !mayCompile) {
      status('Confirm outputs and save the combined crossover check before saving the active profile.', true);
      return;
    }
    if (!await jtsConfirm(
      'Save and apply the active speaker profile "' + configName + '"?' +
        ' This makes it your normal speaker profile.',
      {danger: true}
    )) {
      return;
    }
    if (!readyToApply && !summedValidationComplete()) {
      status('Confirm outputs and save the combined crossover check before saving the active profile.', true);
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Finishing active profile',
      error: ''
    });
    render();
    try {
      var expectedCandidateFingerprint = String(
        profile.candidate_fingerprint || ''
      );
      var resp = await fetch('./active-speaker/baseline-profile/save-and-apply', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          expected_candidate_fingerprint: expectedCandidateFingerprint
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'active profile save/apply failed');
      patchActiveSpeaker({
        loading: false, action: '',
        baselineProfile: payload.profile || payload,
        error: ''
      });
      await refreshCommissioningView();
      status(payload.status === 'applied' ?
        'Active speaker profile saved and applied.' :
        'Active speaker profile was not applied; review the message in this card.',
        payload.status !== 'applied');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message
      });
      status('Could not save and apply active profile: ' + e.message, true);
    }
    render();
  }
  async function fetchActiveSpeakerMeasurements() {
    var resp = await fetch('./active-speaker/measurements', {cache: 'no-store'});
    if (!resp.ok) throw new Error('active-speaker measurements failed');
    return await resp.json();
  }
  async function fetchActiveSpeakerBaselineProfile() {
    var resp = await fetch('./active-speaker/baseline-profile', {cache: 'no-store'});
    if (!resp.ok) throw new Error('active-speaker baseline profile failed');
    return await resp.json();
  }
  async function loadState() {
    try {
      var resp = await fetch('./state', {cache: 'no-store'});
      if (!resp.ok) throw new Error('state failed');
      var payload = await resp.json();
      ingestState(payload);
      eqEditor.selectedId = findIdFor(applied);
      // Open on Off when no EQ is effectively applied — bypassed (enabled
      // false) OR flat (no active filters). Open on Saved with the applied
      // profile marked active otherwise. filter_count is the backend's
      // authoritative signal (len(build_sound_filters); 0 when disabled/flat).
      if (payload.filter_count > 0) {
        eqEditor.view = 'saved';
      } else {
        eqEditor.view = 'off';
      }
      render();
      // The Output page reads the I2S HAT off the topology payload; the
      // safety-limits deep link belongs to the speaker page.
      if (pageMode === 'output') loadOutputHardware();
    } catch (e) {
      status('Could not load sound profile: ' + e.message, true);
    }
  }
  // Hardware-only boot, for /sound/speaker/ and for a follower (whose leader
  // owns the program domain): no content-EQ /state fetch. Paint the shell, then
  // load the hardware state the safety-limits deep link needs to resolve.
  function loadLocalHardware() {
    render();
    refreshOutputTopology({silent: true}).then(applySafetyLimitsDeepLink);
  }
  window.addEventListener('pagehide', function() {
    if (activeSpeaker.action === 'Starting combined test' ||
        activeSpeaker.action === 'Playing combined test') {
      stopSummedTest({keepalive: true, quiet: true, reason: 'pagehide'});
    }
    if (volumeFloorTone.active || volumeFloorTone.inFlight) {
      stopVolumeFloorTone({keepalive: true, quiet: true, reason: 'pagehide'});
    }
    // A Draft is live in CamillaDSP but persisted nowhere, so leaving the page
    // would keep it audible with no surface that shows it. Put the persisted
    // profile back. Never gated on the page really going away: bfcache freezes
    // a page instead of unloading it, and a frozen page must not keep a draft
    // playing either.
    if (eqEditor.view === 'draft') {
      postJSON('./apply', normalizeProfile(applied), {keepalive: true})
        .catch(function() {});
    }
  });
  // The bfcache other half: this page comes back still showing the Draft, but
  // the pagehide above put the persisted profile back and the epoch this page
  // holds is now stale. Re-run the live-draft path — the server answers
  // `stale`, which adopts the fresh epoch and asks for a control move.
  window.addEventListener('pageshow', function(event) {
    if (event && event.persisted && eqEditor.view === 'draft') scheduleLiveDraft(true);
  });
  if (followerMode || pageMode === 'speaker') loadLocalHardware();
  else loadState();
})();
