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
import { wireCopyButtons } from "/assets/shared/js/copy.js";
import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { initAbListen, renderAbListenCard } from "/assets/sound-profile/js/ab-listen.js";
import { initSeatLevel, isSeatLevelRunning, renderSeatLevelCard, stopSeatLevel } from "/assets/sound-profile/js/seat-level.js";
import { rearCalibrationBank, rearCalibrationSeed, rearCalibrationValidate, renderRearCalibrationPanel, setRearCalibrationText } from "/assets/sound-profile/js/rear-calibration.js";
import { applyInstallationToSetting, installationFromSetting } from "/assets/sound-profile/js/installation.js";
import {
  activeSpeakerStepState,
  clampSubwooferCrossoverFcHz,
  nextActionAct,
  timingStatusLine,
  defaultActiveSpeakerStep,
  humanRole,
  levelMatchSummary,
  outputStatusClass
} from "/assets/sound-profile/js/active-speaker-ui.js";
import {
  GAINLESS_TYPES
} from "/assets/sound-profile/js/eq-math.js";
import {
  kaBeamingNoteHtml,
  renderAdvancedDriverSettings,
  renderBuildNotes,
  renderComponentSettings,
  renderCrossoverPreviewRows,
  renderDriverEchoBack,
  renderDriverSafetyIssues,
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
  crossoverPreviewReviewIssues,
  driverResearchFlowComplete,
  driverResearchHasPreviewInputs,
  driverResearchStepSatisfied,
  driverResearchTargets,
  driverSafetyNoteRoles,
  driverSafetyIssues,
  extractDriverResearchJson,
  ingestCrossoverPreview,
  levelDurationLimitsFromSetting,
  manualCrossoverVocabularyValidationError,
  driverFields,
  driverVocabularyLoaded,
  driverNumberFields,
  padFromSetting,
  previewStatusClass,
  proposeSensitivityTrims,
  protectionFiltersFromSetting,
  safetyBandFromSetting,
  setManualCrossoverField,
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
  gx,
  gy,
  padB,
  padL,
  padR,
  padT,
  pointsFor,
  specActive,
  summedDbAt
} from "/assets/sound-profile/js/eq-curve.js";
import { FREQUENCY_SLIDER_STEPS, freqToSlider, sliderToFreq } from "/assets/shared/js/frequency-scale.js";
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
  roleSentenceText
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
  resetOutputTemplateDraft
} from "/assets/sound-profile/js/state.js";
import {
  activeCrossoverPairs,
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
  outputChannelLabel,
  outputClockDomainReport,
  outputGroups,
  outputHardware,
  outputHardwareMismatch,
  outputHasSubwoofer,
  outputTemplateDefinition,
  outputTemplateGroups,
  outputTemplateKindFromAxes,
  pairRoleKey,
  physicalTargetId,
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
    loading: false, action: '',
    calibrationLevel: null, measurements: null,
    baselineProfile: null, error: '', levelDbfs: null,
    commissioningView: null,
    commissionBusy: ''
  };
  // The handoff card's copy state. `copiedRevision` is the declaration
  // revision the copied prompt was MINTED against (server-stamped), so a
  // later declaration edit turns the copy stale instead of drifting silently.
  var tuningHandoff = {prompt: '', copied: false, selected: false, copiedRevision: null};
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
      if (node) node.hidden = !!eqEditor.carrierBlock;
    });
    if (eqEditor.carrierBlock) {
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
      renderNextActionCard() + renderI2sHatSetting() + renderOutputTopologySetup() +
      '</section></div>';
  }

  function renderSpeaker() {
    el('view-body').innerHTML =
      '<div class="saved-stack"><section class="active-speaker-setup">' +
      renderNextActionCard() + renderOutputTopologySetup() + '</section></div>';
    initSeatLevel();
    initAbListen();
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
    eqEditor.carrierBlock = {
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
        '<p>' + escapeHtml(eqEditor.carrierBlock.message || EQ_BLOCKED_MESSAGE) + '</p>' +
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
    if (field.startsWith('installation_')) return;
    driverResearch.safetyDirty = true;
    driverResearch.editedDriverTargets[targetId] = true;
    if (field === 'driver_class' || field === 'pad_kind' || field === 'enclosure_kind') render();
  }
  function refreshDriverResearchDerivedUi() {
    var topology = outputTopology.payload;
    var proposal = el('view-body').querySelector('[data-driver-proposal]');
    if (proposal) proposal.innerHTML = renderCrossoverPreviewCardBody(topology);
    var footer = el('view-body').querySelector('[data-driver-research-footer]');
    if (footer) footer.innerHTML = driverResearchStepFooterButtonHtml();
    var echo = el('view-body').querySelector('[data-driver-echo]');
    if (echo) echo.innerHTML = renderDriverEchoBack(topology);
    var issues = el('driver-safety-issues');
    if (issues && driverResearch.safetyDirty) issues.innerHTML = '';
    updateDriverResearchImportSummary();
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
      driverNumberFields().forEach(function(field) {
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
      var installation = installationFromSetting(setting);
      if (installation) out.installation = installation;
      var pad = padFromSetting(setting);
      if (pad) out.pad = pad;
      var limits = levelDurationLimitsFromSetting(setting);
      if (Object.keys(limits).length) out.level_duration_limits = limits;
      return out;
    }).filter(function(driver) {
      return driverFields().some(function(field) {
        if (field === 'role' || field === 'target_id') return false;
        var value = driver[field];
        return Array.isArray(value) ? value.length > 0 : value != null && value !== '';
      });
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
      if (delayMs != null) {
        candidate.delay_ms = delayMs;
        candidate.delay_target_role = delayTarget;
      }
      return candidate;
    }).filter(Boolean);
    var spacing = manualNumberValue(driverResearch.settings.driver_spacing_mm);
    return drivers.length || candidates.length || spacing != null
      ? Object.assign({}, (driverResearch.designDraft || {}).manual_settings,
        {drivers: drivers, crossover_candidates: candidates, driver_spacing_mm: spacing})
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
    ['max_effective_peak_dbfs', 'max_sweep_duration_s'].forEach(function(field) {
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
  function applyDriverResearchToSetting(driver, targetSetting) {
    driverNumberFields().forEach(function(field) {
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
  }
  function applyDriverResearchToManualSettings(payload) {
    if (!payload || typeof payload !== 'object') return;
    var topology = outputTopology.payload;
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
        driverResearch.prompt = '';
      }
      applyDriverResearchToSetting(driver, driverSetting(target.target_id));
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
  function driverResearchWorkingStatusLabel(status) {
    if (driverResearch.dirty) return 'editing';
    if (driverResearchPreviewInputsReady(outputTopology.payload)) return 'ready to preview';
    if (status === 'blocked') return 'needs speaker layout';
    if (status === 'unreadable') return 'needs review';
    if (status === 'needs_research') return 'needs crossover info';
    return 'working setup';
  }
  function driverResearchWorkingStatusClass(status) {
    if (!driverResearch.dirty && driverResearchPreviewInputsReady(outputTopology.payload)) {
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
    driverResearch.prompt = '';
    var inputs = payload.operator_inputs || {};
    ['full_range', 'woofer', 'mid', 'tweeter', 'subwoofer', 'notes'].forEach(function(key) {
      driverResearch.inputs[key] = inputs[key] || '';
    });
    driverResearch.inputs.target_models = Object.assign({}, inputs.target_models || {});
    driverResearch.settings = {drivers: {}, crossovers: {}};
    var manual = payload.manual_settings || {};
    driverResearch.settings.driver_spacing_mm = manual.driver_spacing_mm;
    (Array.isArray(manual.drivers) ? manual.drivers : []).forEach(function(driver) {
      if (!driver || !driver.role) return;
      var role = String(driver.role);
      var targetId = String(driver.target_id || '');
      if (!targetId) {
        var roleTargets = driverResearchTargets(outputTopology.payload).filter(function(item) {
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
      applyInstallationToSetting(driver, driverResearch.settings.drivers[targetId]);
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
      driverResearch.importedPayload = payload.driver_research;
    } else {
      driverResearch.importText = '';
      driverResearch.importedPayload = null;
    }
    driverResearch.error = '';
    driverResearch.dirty = false;
    driverResearch.safetyDirty = false;
    driverResearch.editedDriverTargets = {};
    ((payload.driver_research || {}).drivers || []).forEach(function(driver) {
      var visible = (manual.drivers || []).find(function(row) {
        return row.target_id === driver.target_id;
      });
      if (!visible) return;
      var setting = driverSetting(driver.target_id);
      var projected = Object.assign({}, setting);
      applyDriverResearchToSetting(driver, projected);
      if (Object.keys(projected).some(function(field) {
        return projected[field] !== setting[field];
      })) driverResearch.editedDriverTargets[driver.target_id] = true;
    });
  }
  async function fetchDesignDraft() {
    var payload = await getJSON('./active-speaker/design-draft');
    ingestDesignDraft(payload);
    return payload;
  }
  async function fetchCrossoverPreview() {
    var payload = await getJSON('./active-speaker/crossover-preview');
    ingestCrossoverPreview(payload);
    return payload;
  }
  function renderOutputTopologySetup() {
    return '<div class="setting-row setting-row--stack output-setup">' +
      '<div class="output-setup__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Active crossover setup</p>' +
          '<p class="setting-row__hint">Declare the speaker, confirm driver limits, measure, then apply.</p>' +
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
  function baselineProfileAppliedRecord() {
    var view = activeSpeaker.commissioningView || {};
    var record = view.applied_profile;
    return record && record.exists === true ? record : null;
  }

  function baselineProfileApplied() {
    var record = baselineProfileAppliedRecord();
    return record !== null && record.stands === true;
  }
  function outputStepContext(topology) {
    return {
      hasLayout: outputGroups(topology).length > 0,
      dirty: outputTopology.dirty,
      hardwareMatchesSaved: !outputHardwareMismatch(topology),
      driverResearchSatisfied: driverResearchFlowComplete(outputTopology.payload),
      steps: (activeSpeaker.commissioningView || {}).steps,
      currentStep: (activeSpeaker.commissioningView || {}).current_step
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
    var action = (activeSpeaker.commissioningView || {}).next_action;
    if (action && action.id) return nextActionAct(action).step;
    if (!outputTopology.dirty && !outputHardwareMismatch(currentOutputTopology()) &&
        !driverResearch.dirty) {
      var backendStep = commissioningCurrentStep();
      if (backendStep) return backendStep;
    }
    return defaultActiveSpeakerStep(outputStepContext(currentOutputTopology()));
  }
  function outputStepIsOpen(step) {
    return (outputPage.stepOverride || defaultOutputStep()) === step;
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
    var open = outputStepIsOpen(step);
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
  function renderNextActionButton(action) {
    var behavior = nextActionAct(action);
    var busy = outputTopology.saving || driverResearch.saving ||
      activeSpeaker.commissionBusy;
    return '<button type="button" class="btn btn--primary" data-next-action="' + escapeHtml(action.id) +
      '" data-act="' + escapeHtml(behavior.act) + '" data-program="' + escapeHtml(behavior.program || '') + '"' +
      (behavior.command ? ' data-copy="tuning-run-command"' : '') +
      (action.enabled === false || busy ? ' disabled' : '') + '>' + escapeHtml(action.label) + '</button>' +
      (action.reason ? '<p class="form-hint">' + escapeHtml(action.reason) + '</p>' : '');
  }
  function renderNextActionCard() {
    var action = (activeSpeaker.commissioningView || {}).next_action;
    if (!action || !action.id) return '';
    if (action.id === 'copy_prompt') return renderTuningHandoffCard(action);
    var command = nextActionAct(action).command;
    return '<section class="info-card" data-next-action-card>' + renderNextActionButton(action) +
      (command ? '<label class="field">Run in the console<textarea id="tuning-run-command" readonly rows="2">' +
        escapeHtml(command) + '</textarea></label>' : '') + '</section>';
  }
  function driverResearchStepFooterButtonHtml() {
    return '<button type="button" class="btn btn--ghost" data-act="save-driver-design"' +
      (driverResearch.saving ? ' disabled' : '') + '>' +
      (driverResearch.saving ? 'Saving' : 'Save values') + '</button>';
  }
  function renderDriverResearchStepFooter() {
    return '<span data-driver-research-footer>' +
      driverResearchStepFooterButtonHtml() +
    '</span>';
  }
  function outputTemplateAxesForTopology(topology) {
    var mainGroups = outputGroups(topology).filter(function(group) {
      return group.kind !== 'subwoofer' && group.mode !== 'subwoofer';
    });
    if (!mainGroups.length) {
      return {
        layout: outputPage.templateDraftAxes.layout || '',
        speakerMode: outputPage.templateDraftAxes.speakerMode || '',
        cardioid: !!outputPage.templateDraftAxes.cardioid
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
    var cardioid = mainGroups.some(function(group) {
      return (group.channels || []).some(function(channel) { return channel.output_variant === 'rear'; });
    });
    return {layout: layout, speakerMode: speakerMode, cardioid: cardioid};
  }
  function outputTemplateAxisButton(axis, value, label, hint, selected) {
    return '<button type="button" class="output-template-option" data-act="output-template-axis" ' +
      'data-axis="' + escapeHtml(axis) + '" data-value="' + escapeHtml(value) + '" ' +
      'aria-pressed="' + (selected ? 'true' : 'false') + '">' +
        '<strong>' + escapeHtml(label) + '</strong>' +
        '<span>' + escapeHtml(hint) + '</span>' +
      '</button>';
  }
  function renderOutputSetupTemplates(topology) {
    var hardware = outputHardware(topology);
    var count = Number(hardware && hardware.physical_output_count) || 0;
    var axes = outputTemplateAxesForTopology(topology);
    var selectedTemplate = outputTemplateDefinition(
      outputTemplateKindFromAxes(axes.layout, axes.speakerMode, axes.cardioid)
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
    var layoutChoices = [
      {value: 'mono', label: 'Mono', hint: 'One speaker or cabinet'},
      {value: 'stereo', label: 'Stereo', hint: 'Left and right speakers'}
    ];
    var speakerChoices = [
      {value: 'passive', label: 'Passive', hint: 'Full-range output per speaker'},
      {value: 'active_2way', label: axes.cardioid ? 'Active 2-way with rear woofer (cardioid)' : 'Active 2-way', hint: 'Woofer + tweeter'},
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
                axes.layout === choice.value
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
                axes.speakerMode === choice.value
              );
            }).join('') +
          '</div>' +
        '</div>' +
      '</div>' +
      (axes.speakerMode === 'active_2way'
        ? '<label class="setting-row"><span><strong>Cardioid</strong>' +
          '<span class="setting-row__hint">Front woofer, rear woofer and tweeter. Rear starts muted for tuning.</span></span>' +
          '<input type="checkbox" data-output-cardioid' + (axes.cardioid ? ' checked' : '') + '></label>'
        : '') +
      '<dl class="active-speaker-facts output-facts output-template-summary">' +
        '<div><dt>Selected setup</dt><dd>' + escapeHtml(selectedLabel) + '</dd></div>' +
        '<div><dt>Outputs needed</dt><dd>' + escapeHtml(
          outputCount ? String(outputCount) + ' of ' + String(count || 0) + ' available' : 'Choose a setup'
        ) + '</dd></div>' +
      '</dl>' +
    '</div>';
  }
  function renderOutputSubwooferCard(topology) {
    var hasLayout = outputGroups(topology).length > 0;
    var hasSub = outputHasSubwoofer(topology);
    var nextOutput = firstUnusedOutputIndex(topology);
    var disabled = !hasLayout || (!hasSub && nextOutput == null);
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
        : ('Adds one subwoofer group on ' + (nextOutputLabel || ('DAC output ' + (Number(nextOutput) + 1))))));
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
  function renderDriverResearchSummary() {
    var saved = driverResearch.designDraft || {};
    var savedStatus = saved.status || '';
    var topology = outputTopology.payload;
    var safetyRoles = driverSafetyNoteRoles(topology);
    var savedHtml =
      '<div class="driver-research__summary driver-research__summary--saved">' +
        '<span class="status-pill' + driverResearchWorkingStatusClass(savedStatus) + '">' +
          escapeHtml(driverResearchWorkingStatusLabel(savedStatus)) + '</span>' +
        '<p class="setting-row__hint">' + escapeHtml(workingSetupSummary(topology)) + '</p>' +
        (safetyRoles.length ? '<p class="setting-row__hint">' + escapeHtml(
          'Driver safety notes captured for ' + roleSentenceText(safetyRoles) + '.'
        ) + '</p>' : '') +
        (driverResearch.safetyDirty ? '<p class="setting-row__hint">Driver issue list: save your current edits to update it.</p>' : '') +
      '</div>';
    if (driverResearch.error) {
      return savedHtml +
        '<p class="setting-row__hint driver-research__error">' +
        escapeHtml(driverResearch.error) + '</p>';
    }
    return savedHtml;
  }
  function renderDriverResearchAiHelper(topology) {
    return '<section class="driver-research__section driver-research__ai">' +
      '<div><h3 class="setting-row__title">Research your components</h3>' +
        '<p class="setting-row__hint">Copy the populated prompt, use it with the research assistant of your choice, then paste the JSON response here.</p></div>' +
      '<div class="driver-research__grid driver-research__grid--ai">' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<div><p class="setting-row__title">1. Copy the prompt</p>' +
              '<p class="setting-row__hint">Add the component models, enclosure and tweeter type. Build notes are optional.</p></div>' +
            '<button type="button" class="btn btn--ghost" data-act="copy-driver-research-prompt">Copy prompt</button>' +
          '</div>' +
          '<textarea id="driver-research-prompt" class="driver-research__textarea driver-research__textarea--' +
            (driverResearch.prompt ? 'compact' : 'hidden') + '" readonly ' +
            'aria-label="Driver research prompt">' +
            escapeHtml(driverResearch.prompt) + '</textarea>' +
        '</div>' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<div><p class="setting-row__title">2. Paste the response</p>' +
              '<p class="setting-row__hint">JTS loads the proposed values into the working setup for your review. Nothing is applied to the speaker.</p></div>' +
            '<div class="driver-research__actions">' +
              '<button type="button" class="btn btn--ghost" data-act="parse-driver-research">Load information</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="driver-research-import" class="driver-research__textarea driver-research__textarea--compact" data-driver-import ' +
            'rows="4" placeholder="{...}" aria-label="Driver research JSON result">' +
            escapeHtml(driverResearch.importText || '') + '</textarea>' +
          '<div id="driver-research-import-summary">' + renderDriverResearchSummary() + '</div>' +
        '</div>' +
      '</div>' +
      '<div data-driver-echo>' + renderDriverEchoBack(topology) + '</div>' +
    '</section>';
  }
  function applySafetyLimitsDeepLink() {
    if (window.location.hash !== '#driver-safety-issues' || !driverSafetyIssues().length) return;
    outputPage.stepOverride = 'research';
    render();
    var node = document.getElementById('driver-safety-issues');
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({block: 'center'});
  }
  function renderDriverResearchCard() {
    var topology = outputTopology.payload;
    return '<div class="output-card output-card--driver-research">' +
      '<div class="output-card__head"><div><p class="output-card__title">Component setup</p>' +
        '<p class="setting-row__hint">Start with what is physically installed. JTS uses these choices as authoritative context, not facts for AI to guess.</p></div></div>' +
      (outputTopology.dirty ? '<p class="setting-row__hint" data-saved-layout-values>These values and research prompts use the saved speaker layout.</p>' : '') +
      renderDriverSafetyIssues() +
      '<div class="driver-research__section">' +
        '<h3 class="setting-row__title">Your components</h3>' +
        renderComponentSettings(topology) +
      '</div>' +
      renderBuildNotes() +
      renderDriverResearchAiHelper(topology) +
      renderCrossoverPreviewCard(topology) +
      renderRearCalibrationPanel() +
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
    var hasPreviewGroups = !driverResearch.dirty && Array.isArray(payload.groups) && payload.groups.length;
    var displayPayload = hasPreviewGroups ? payload : {};
    var summary = displayPayload.summary || {};
    var readyCount = crossoverPreviewReadyCount(displayPayload);
    var warningIssues = crossoverPreviewReviewIssues(displayPayload.issues);
    var laterSafetyCount = Math.max(0, Number(summary.blocker_count || 0));
    var hasPreviewInputs = driverResearchPreviewInputsReady(topology);
    var needsCrossover = activeCrossoverPairs(topology).length > 0;
    var label = !needsCrossover ? 'not needed' :
      (hasPreviewGroups ? crossoverPreviewDisplayStatus(payload) :
        (hasPreviewInputs ? 'working proposal' : 'waiting for information'));
    var hint = !needsCrossover ?
      'A single full-range driver does not need an active crossover.' :
      'Preview of the current setup. Save changes to update it.';
    if (crossoverPreview.error) hint = crossoverPreview.error;
    return '<div class="output-card__head"><div><h3 class="output-card__title">Proposed starting crossover</h3>' +
        '<p class="setting-row__hint">' + escapeHtml(hint) + '</p></div>' +
        '<span class="status-pill' + previewStatusClass(label) + '">' + escapeHtml(label) + '</span></div>' +
      (hasPreviewGroups
        ? renderCrossoverPreviewRows(payload)
        : renderWorkingCrossoverRows(topology)) +
      '<p class="setting-row__hint">' + escapeHtml(
        (readyCount > 0 ? 'Preview shows ' + String(readyCount) +
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
          renderOutputHardwareCard(topology, layoutStatusValue) +
          renderCrossChildNoticeCard(topology) + renderOutputGroupsCard(topology),
        renderOutputHardwareRefresh() +
          '<button type="button" class="btn btn--ghost" data-act="save-output-topology"' +
            (!outputTopology.dirty || outputTopology.saving ? ' disabled' : '') + '>Save</button>'
      ) +
      renderOutputStepCard(
        'research',
        'Driver values',
        outputStepHint('research', 'Describe each installed driver, then research a starting crossover.'),
        topology,
        renderDriverResearchCard(),
        renderDriverResearchStepFooter()
      ) +
      renderOutputStepCard(
        'experiment',
        'First speaker experiment',
        outputStepHint('experiment', 'Measure the speaker at the design mark.'),
        topology,
        (commissioningStepNotRequired('experiment')
          ? renderStepNotRequiredCard('experiment', 'No active crossover experiment is needed.')
          : '<a class="btn btn--ghost" href="/sound/speaker/crossover/">Open speaker experiment</a>') +
          (followerMode ? '' : renderSeatLevelCard()),
        ''
      ) +
      renderOutputStepCard(
        'profile',
        'Apply speaker profile',
        outputStepHint('profile', 'Apply the candidate from the experiment packet.'),
        topology,
        commissioningStepNotRequired('profile')
          ? renderStepNotRequiredCard(
              'profile',
              'This speaker does not use an active speaker profile.')
          : renderBaselineProfileCard() + renderAbListenCard(),
        ''
      ) +
      (((activeSpeaker.commissioningView || {}).next_action || {}).id === 'copy_prompt' ? '' : renderTuningHandoffCard()) +
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
        'Re-run the 15-minute drift measurement for the new pair, then Apply the baseline to resume audio.'
      ) + '</p>' +
      '<button type="button" class="btn btn--primary" data-act="repin-output-topology"' +
        (outputHardwareActionBusy() ? ' disabled' : '') + '>' +
        escapeHtml(outputTopology.repinning ? 'Pinning' : 'Keep setup, pin the new DAC') +
      '</button>' +
    '</div>';
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
        '<p class="setting-row__hint">Choose a speaker layout first.</p>' +
      '</div>';
    }
    var outputs = physicalOutputOptions(topology);
    return '<div class="output-card output-card--groups">' +
      '<div class="output-card__head"><div><p class="output-card__title">DAC output assignments</p>' +
        '<p class="setting-row__hint">Assign each driver to one DAC channel.</p></div>' +
        '<span class="status-pill' + (outputTopology.dirty ? '' : ' status-pill--ready') + '">' +
          escapeHtml(outputTopology.dirty ? 'draft' : 'saved') + '</span></div>' +
      '<div class="output-roles output-roles--flat">' + assignments.map(function(item) {
        var group = item.group;
        var channel = item.channel;
        var selected = channel.physical_output_index == null ? '' : String(channel.physical_output_index);
        var label = channel.human_output_label ||
          (channel.physical_output_index == null ? 'No output assigned' :
            physicalOutputLabel(topology, channel.physical_output_index));
        var otherAssigned = outputAssignedToOtherMap(topology, group.id || '', channel.role || '', channel.output_variant);
        var selectOptions = ['<option value="">Choose output</option>'].concat(
          outputs.map(function(output) {
            var value = String(output.index);
            var usedByOther = otherAssigned[value];
            return '<option value="' + escapeHtml(value) + '"' +
              (value === selected ? ' selected' : '') + '>' +
              escapeHtml(output.label + (usedByOther && value !== selected ?
                ' — used by ' + usedByOther : '')) +
              '</option>';
          })
        ).join('');
        var model = targetModel({
          target_id: physicalTargetId(group.id, channel.role, channel.output_variant),
          role: String(channel.role || '')
        }, topology);
        var hardwareLabel = (group.label || group.id) + ' · ' + outputChannelLabel(group, channel) +
          (model ? ' · ' + model : '');
        return '<div class="output-role">' +
          '<div class="output-role__text">' +
            '<span>' + escapeHtml(label) + '</span>' +
            '<strong>' + escapeHtml(hardwareLabel) + '</strong>' +
            (channel.physical_output_index == null ? '<small>Assign a DAC output.</small>' : '') +
          '</div>' +
          '<label class="output-role__select">' +
            '<span>DAC channel</span>' +
            '<select data-output-channel data-group-id="' + escapeHtml(group.id || '') +
              '" data-role="' + escapeHtml(channel.role || '') + '" data-output-variant="' +
              escapeHtml(channel.output_variant || 'primary') + '">' +
              selectOptions +
            '</select>' +
          '</label>' +
        '</div>';
      }).join('') + '</div>' +
    '</div>';
  }
  function renderStepNotRequiredCard(step, fallback) {
    return '<div class="output-card output-card--not-required">' +
      '<div class="output-card__head"><div>' +
        '<p class="output-card__title">Not needed for this speaker</p>' +
        '<p class="setting-row__hint">' + escapeHtml(outputStepHint(step, fallback)) +
        '</p></div>' +
        '<span class="status-pill">not needed</span></div>' +
      '<p class="setting-row__hint">Apply the baseline to complete speaker setup.</p>' +
    '</div>';
  }
  function baselineProfileApplyBlocked(profile) {
    var issues = Array.isArray(profile && profile.issues) ? profile.issues : [];
    return issues.some(function(issue) {
      return issue && issue.code === 'baseline_output_handoff_not_supported';
    });
  }
  function baselineProfileIssueMessage(issue) {
    if (!issue) return 'Profile is not ready yet.';
    if (issue.code === 'baseline_output_handoff_not_supported') {
      return 'This output hardware can save the active profile, but JTS cannot switch normal playback to it from here yet.';
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
    var timing = timingStatusLine(activeSpeaker.commissioningView, 'saved');
    var timingVerification = timingStatusLine(activeSpeaker.commissioningView, 'verification');
    var config = appliedRecord ? {path: appliedRecord.config_path} : (profile.config || {});
    var permissions = profile.permissions || {};
    var applied = baselineProfileApplied();
    var readyToApply = permissions.may_apply === true;
    var mayCompile = ((activeSpeaker.commissioningView || {}).review || {}).may_apply === true;
    var applyBlocked = baselineProfileApplyBlocked(profile);
    var busy = activeSpeaker.action === 'Finishing active profile';
    var canFinish = !applyBlocked && (mayCompile || readyToApply);
    var issues = Array.isArray(profile.issues) ? profile.issues : [];
    var issueRows = issues.filter(function(issue) {
      return issue && issue.severity === 'blocker';
    }).slice(0, 3).map(function(issue) {
      return '<li>' + escapeHtml(baselineProfileIssueMessage(issue)) + '</li>';
    }).join('');
    var body = appliedRecord ?
      '<p class="setting-row__hint">' + (applied ? 'This is now your active speaker profile: ' :
        'This saved speaker profile is not active: ') +
        escapeHtml(config.basename || config.path || 'active speaker baseline') + '.</p>' +
      '<p class="setting-row__hint">Candidate: ' + escapeHtml(String(appliedRecord.candidate_fingerprint || '').slice(0, 12)) +
        ' · Record: ' + escapeHtml(appliedRecord.record || '') +
        ' · Applied: ' + escapeHtml(appliedRecord.applied_at || '') + '</p>' :
      (applyBlocked ?
        '<p class="setting-row__hint">This profile cannot be made active from this page yet. Review the setup issue below.</p>' :
      (readyToApply ?
        '<p class="setting-row__hint">Your active speaker profile is saved. Finish applying it to start using it.</p>' :
        '<p class="setting-row__hint">' + escapeHtml(mayCompile ?
          'Save the checked crossover as your active speaker profile. JTS validates and applies it in one step; no sound plays.' :
          'Apply the candidate named in the speaker experiment packet.') + '</p>'));
    var actionLabel = busy ?
      'Saving and applying' :
      'Save and apply';
    var actions = applyBlocked || applied ? '' :
      '<div class="active-speaker-actions active-speaker-profile-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="save-apply-baseline-profile"' +
          ((busy || !canFinish) ? ' disabled' : '') + '>' + escapeHtml(actionLabel) + '</button></div>';
    return '<div class="output-card output-card--baseline-profile">' +
      '<div class="output-card__head"><div><p class="output-card__title">Active speaker profile</p>' +
        '<p class="setting-row__hint">Your active speaker profile, built from the checked crossover and assigned outputs.</p></div>' +
        '<span class="status-pill' + (applied || readyToApply ? ' status-pill--ready' : '') + '">' +
          escapeHtml(applied ? 'active' : (appliedRecord || readyToApply ? 'saved' : (applyBlocked ? 'blocked' : 'not saved'))) + '</span></div>' +
      body +
      (timing ? '<p class="setting-row__hint">' + escapeHtml(timing) + '</p>' : '') +
      (timingVerification ? '<p class="setting-row__hint">' + escapeHtml(timingVerification) + '</p>' : '') +
      renderLevelMatchSummary(profile) +
      (issueRows ? '<ul class="active-speaker-issues active-speaker-issues--warning">' + issueRows + '</ul>' : '') +
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
  function renderTuningHandoffCard(action) {
    if (!action && !baselineProfileApplied()) return '';
    var stale = tuningHandoffStale();
    var copyState = promptCopyState(tuningHandoff, stale);
    var programs = (activeSpeaker.baselineProfile || {}).tuning_programs || [];
    var programId = (((activeSpeaker.commissioningView || {}).next_action || {}).program || 'speaker');
    return '<section class="info-card"' + (action ? ' data-next-action-card' : '') + '>' +
      '<p class="output-card__title">Tune with an AI operator</p>' +
      '<p class="setting-row__hint">Copy a prompt into an AI session with access to this speaker.</p>' +
      (action ? renderNextActionButton(action) : '<button type="button" class="btn btn--ghost" ' +
        'data-act="copy-tuning-handoff" data-program="' + escapeHtml(programId) + '">Copy the ' +
        escapeHtml(programId) + ' prompt</button>') +
      '<details class="disclosure" data-other-prompts><summary>Other programs</summary><div class="disclosure__body">' +
      programs.filter(function(program) { return program.id !== programId; }).map(function(program) {
        return '<div class="output-card__head"><div>' +
          '<p class="output-card__title">' + escapeHtml(program.title) + '</p>' +
          '<p class="setting-row__hint">' + escapeHtml(program.description) + '</p></div>' +
          '<button type="button" class="btn btn--ghost" data-act="copy-tuning-handoff" data-program="' +
            escapeHtml(program.id) + '">Copy the ' + escapeHtml(program.id) + ' prompt</button></div>';
      }).join('') +
      '</div></details>' +
      (stale ? '<p class="setting-row__hint" data-tuning-handoff-stale>' +
        'Your declarations changed. Copy a fresh prompt before the next session.</p>' : '') +
      '<textarea id="tuning-handoff-prompt" class="' + copyState.promptClass + '" readonly ' +
        (copyState.selected ? 'rows="6" ' : '') +
        'aria-label="AI operator prompt">' + escapeHtml(tuningHandoff.prompt || '') + '</textarea>' +
    '</section>';
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
        '<input type="range" class="range__input" min="' + (opts.log ? 0 : min) + '" max="' + (opts.log ? FREQUENCY_SLIDER_STEPS : max) +
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
      var payload = await postJSON('./preview',
        liveProfile() || Object.assign(FLAT(), {enabled: false}));
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
      var payload = await postJSON('./live-draft', {profile: draft, dsp_write_epoch: dspWriteEpoch});
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
      var payload = await postJSON('./apply', profile);
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
      var payload = await postJSON(path, body || {});
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
      var payload = await postJSON('./i2s-hat', {profile_id: profileId || null});
      if ('desired_profile_id' in payload) outputPage.i2sHat = payload;
      if (payload.warnings && payload.warnings.length)
        return status(payload.warnings[0], true);
      status(payload.restart_required ?
        'I²S HAT setting saved. Restart required.' : 'I²S HAT setting saved.');
    } catch (e) {
      if (e.body && 'desired_profile_id' in e.body) {
        outputPage.i2sHat = e.body;
        return status('Setting saved, but the boot change could not be applied. Try again; if it still fails, open System and run diagnostics.', true);
      }
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
    if (carrier) eqEditor.carrierBlock = carrier.status === 'blocked' ? carrier : null;
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
    else if (act === 'open-output-layout') {
      openOutputStep('layout');
      el('view-body').querySelector('[data-output-step="layout"]').scrollIntoView({block: 'start'});
    }
    else if (act === 'save-output-topology') { saveOutputTopology(); }
    else if (act === 'reset-output-topology') { resetOutputTopology(); }
    else if (act === 'repin-output-topology') { repinOutputTopology(); }
    else if (act === 'copy-driver-research-prompt') { copyDriverResearchPrompt(t); }
    else if (act === 'parse-driver-research') { parseDriverResearchImport(); }
    else if (act === 'save-driver-design') { saveDriverResearchDraft(); }
    else if (act === 'save-apply-baseline-profile') { saveAndApplyBaselineProfile(); }
    else if (act === 'copy-tuning-handoff') { copyTuningHandoffPrompt(t.getAttribute('data-program')); }
    else if (act === 'toggle-volume-floor-tone') {
      if (volumeFloorTone.active) stopVolumeFloorTone();
      else startVolumeFloorTone();
    }
    else if (act === 'save-volume-floor') { saveVolumeFloor(); }
    else if (act === 'reset-volume-floor') { resetVolumeFloor(); }
    else if (act === 'rear-calibration-seed') { rearCalibrationSeed(); }
    else if (act === 'rear-calibration-validate') { rearCalibrationValidate(); }
    else if (act === 'rear-calibration-bank') { rearCalibrationBank(); }
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
      updateDriverResearchPromptPreview();
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-field')) {
      var driverField = ev.target.getAttribute('data-driver-field');
      driverResearch.inputs[driverField] = ev.target.value;
      driverResearch.error = '';
      driverResearch.dirty = true;
      driverResearch.safetyDirty = true;
      updateDriverResearchPromptPreview();
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-import')) {
      driverResearch.importText = ev.target.value;
      driverResearch.error = '';
      driverResearch.importedPayload = null;
      driverResearch.dirty = true;
      updateDriverResearchImportSummary();
      refreshDriverResearchDerivedUi();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-rear-calibration-text')) {
      setRearCalibrationText(ev.target.value);
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-spacing')) {
      driverResearch.settings.driver_spacing_mm = ev.target.value;
      driverResearch.error = '';
      driverResearch.dirty = true;
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
            crossoverKey.split(':'), ev.target.value, outputTopology.payload
          );
        }
      }
      refreshDriverResearchDerivedUi();
      return;
    }
    var field = ev.target.getAttribute('data-field');
    var range = ev.target.getAttribute('data-range');
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
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-output-cardioid')) {
      setOutputTemplateAxis('cardioid', String(ev.target.checked));
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-output-channel')) {
      setOutputChannelAssignment(
        ev.target.getAttribute('data-group-id') || '',
        ev.target.getAttribute('data-role') || '',
        ev.target.value,
        ev.target.getAttribute('data-output-variant') || 'primary'
      );
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-style')) {
      setOutputChannelDriverStyle(
        ev.target.getAttribute('data-group-id') || '',
        ev.target.getAttribute('data-role') || '',
        ev.target.value
      );
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
    // Not resetEqEditor(): the branches below still read nameMode and nameDraft.
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
    outputTopology.clockDomain = payload && payload.clock_domain || topology && topology.clock_domain || null;
    outputTopology.observedHardware = payload && payload.output_hardware || null;
    outputTopology.hardwareAdoption = payload && payload.hardware_adoption || null;
    outputTopology.hardwareMismatch = payload && payload.hardware_mismatch || null;
    outputTopology.hardwareRepin = payload && payload.hardware_repin || null;
    outputPage.i2sHat = payload && payload.i2s_hat || outputPage.i2sHat;
    outputTopology.error = '';
    outputTopology.dirty = false;
    outputTopology.saving = false;
    outputTopology.resetting = false;
    outputTopology.repinning = false;
    outputTopology.loading = false;
    if (outputGroups(topology).length) resetOutputTemplateDraft();
  }
  // The Output page renders the HAT picker and the sound settings, nothing
  // else, so it reads the topology payload for `i2s_hat` alone and skips the
  // six crossover/commissioning reads only the speaker page draws.
  async function loadOutputHardware() {
    try {
      ingestOutputTopology(await getJSON('./output-topology'));
    } catch (e) {
      outputTopology.error = e.message;
    }
    render();
  }
  async function refreshOutputTopology(options) {
    options = options || {};
    if (!options.silent && outputTopology.dirty &&
        !await jtsConfirm('Refresh hardware and lose the unsaved speaker layout draft?')) return;
    if (!options.silent) outputTopology.touched = true;
    outputTopology.loading = true;
    outputTopology.error = '';
    if (!options.silent) render();
    try {
      ingestOutputTopology(await getJSON('./output-topology'));
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
      await refreshCommissioningView();
    } catch (e) {
      outputTopology.loading = false;
      outputTopology.error = e.message;
    }
    render();
  }
  async function refreshCommissioningView() {
    try {
      var view = await getJSON('./active-speaker/commissioning-view');
      var previous = (activeSpeaker.commissioningView || {}).next_action || {};
      if (view.next_action && (view.next_action.id !== previous.id || view.next_action.program !== previous.program)) {
        outputPage.stepOverride = '';
      }
      patchActiveSpeaker({commissioningView: view});
    } catch (viewError) {
      patchActiveSpeaker({commissioningView: activeSpeaker.commissioningView || null});
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
  function setOutputDraft(next) {
    outputTopology.draft = next;
    if (outputGroups(next).length) resetOutputTemplateDraft();
    outputTopology.dirty = true;
    outputTopology.touched = true;
    outputTopology.error = '';
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
  function setOutputChannelAssignment(groupId, role, rawValue, variant) {
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
    var targetChannel = null;
    outputGroups(next).forEach(function(group) {
      if ((group.id || '') !== groupId) return;
      (group.channels || []).forEach(function(channel) {
        if ((channel.role || '') === role && (channel.output_variant || 'primary') === (variant || 'primary')) {
          targetChannel = channel;
        }
      });
    });
    if (!targetChannel) {
      status('Could not find that driver in the speaker layout.', true);
      return;
    }
    targetChannel.physical_output_index = selected;
    delete targetChannel.human_output_label;
    outputPage.stepOverride = 'layout';
    setOutputDraft(next);
    status('Channel assignment updated. Save the speaker layout.');
  }
  function setOutputChannelDriverStyle(groupId, role, rawValue) {
    var next = baseOutputDraft(outputTopology.payload);
    var value = String(rawValue || '').trim();
    function update(topology) {
      var group = outputGroups(topology).find(function(item) { return item.id === groupId; });
      var channel = group && group.channels.find(function(item) { return item.role === role; });
      if (!channel) return false;
      if (value) channel.driver_style = value;
      else delete channel.driver_style;
      return true;
    }
    if (!next || !update(next)) return;
    update(outputTopology.draft);
    saveOutputTopology({nextStep: 'research', topology: next});
  }
  async function setOutputTemplate(kind) {
    var next = baseOutputDraft();
    if (!next || !next.hardware) {
      status('Load output hardware before creating a speaker layout.', true);
      return;
    }
    var keepSubwoofer = outputHasSubwoofer(next);
    var template = outputTemplateDefinition(kind);
    if (!template) {
      status('Choose a supported speaker layout template.', true);
      return;
    }
    resetOutputTemplateDraft();
    next.name = template.name;
    next.speaker_groups = outputTemplateGroups(template, next);
    next.artifact_schema_version = kind.endsWith('_cardioid') ? 2 : 1;
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
    axes.cardioid = axis === 'cardioid' ? value === 'true' : axes.cardioid;
    outputPage.templateDraftAxes = {layout: layout || '', speakerMode: speakerMode || '', cardioid: axes.cardioid};
    if (!layout || !speakerMode) {
      status(layout ? 'Choose passive, active 2-way, or active 3-way to continue.' :
        'Choose mono or stereo to continue.');
      render();
      return;
    }
    var kind = outputTemplateKindFromAxes(layout, speakerMode, axes.cardioid);
    if (!kind) {
      status('Choose a supported speaker layout option.', true);
      return;
    }
    await setOutputTemplate(kind);
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
    setOutputDraft(next);
    status(modeValue === 'remove' ?
      'Removed subwoofer from the speaker layout draft.' :
      'Added subwoofer to the speaker layout draft. Save before verification.');
  }
  function updateDriverResearchPromptPreview() {
    driverResearch.prompt = '';
    var prompt = el('driver-research-prompt');
    if (prompt) prompt.value = '';
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
  async function copyPromptField(fieldId, state, copiedMessage) {
    var field = el(fieldId);
    if (!field) return;
    var copied = await copyTextToClipboard(field.value, field);
    state.copied = copied;
    state.selected = !copied;
    render();
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
    try {
      var payload = await postJSON('./active-speaker/driver-research-request', {
        operator_inputs: driverResearch.inputs
      });
      driverResearch.prompt = String(payload.prompt || '');
      prompt.value = driverResearch.prompt;
    } catch (e) {
      status('Could not prepare the target-bound research prompt: ' + e.message, true);
      return;
    }
    var copied = await copyTextToClipboard(prompt.value, prompt);
    button.textContent = copied ? 'Copied' : 'Selected';
    prompt.className = 'driver-research__textarea driver-research__textarea--' +
      (copied ? 'hidden' : 'compact');
    if (!copied) {
      prompt.rows = 6;
      prompt.focus();
      prompt.select();
      prompt.setSelectionRange(0, prompt.value.length);
    }
    status(copied ? 'Copied driver research prompt.' :
      'Copy was blocked by the browser. Prompt text is selected.', !copied);
  }
  async function copyTuningHandoffPrompt(programId) {
    var field = el('tuning-handoff-prompt');
    if (!field) return;
    try {
      var payload = await getJSON('./active-speaker/tuning-handoff?program=' + encodeURIComponent(programId));
      if (payload.status !== 'ready') {
        throw new Error('this speaker has no applied profile to hand over yet');
      }
      if (!payload.prompt) throw new Error('the selected tuning program is unavailable');
      tuningHandoff.prompt = String(payload.prompt);
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
      if (!payload) throw new Error('Paste a driver research document.');
      driverResearch.importedPayload = payload;
      applyDriverResearchToManualSettings(payload);
      driverResearch.error = '';
      driverResearch.dirty = true;
      driverResearch.safetyDirty = true;
      driverResearch.editedDriverTargets = {};
      status('Imported driver research. Review the visible values before updating the working setup.');
    } catch (e) {
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
      status('Working setup is already current.');
      render();
      return true;
    }
    if (!outputTopology.payload) {
      status('Load output hardware before updating the working setup.', true);
      return false;
    }
    if (!driverVocabularyLoaded()) {
      driverResearch.error = 'This page did not load its driver field list. Reload the page before saving.';
      status(driverResearch.error, true);
      return false;
    }
    var manualTopology = outputTopology.payload;
    var manualError = manualCrossoverVocabularyValidationError(manualTopology);
    if (manualError) {
      driverResearch.error = manualError;
      status(manualError, true);
      render();
      return false;
    }
    var manualPayload = manualSettingsPayload(outputTopology.payload);
    var researchPayload = null;
    var importWarning = '';
    if ((driverResearch.importText || '').trim()) {
      try {
        researchPayload = extractDriverResearchJson(driverResearch.importText);
        if (!researchPayload) throw new Error('Paste a driver research document.');
        driverResearch.importedPayload = researchPayload;
      } catch (e) {
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
    if (!importWarning) driverResearch.error = '';
    render();
    try {
      var payload = await postJSON('./active-speaker/design-draft', {
        operator_inputs: driverResearch.inputs,
        manual_settings: manualPayload,
        driver_research: researchPayload
      });
      var rejectedImport = importWarning
        ? {text: driverResearch.importText, error: driverResearch.error}
        : null;
      ingestDesignDraft(payload, {force: true});
      if (rejectedImport) {
        driverResearch.importText = rejectedImport.text;
        driverResearch.error = rejectedImport.error;
      }
      await fetchCrossoverPreview();
      await refreshCommissioningView();
      if (options.nextStep) outputPage.stepOverride = options.nextStep;
      status(importWarning
        ? 'Working setup updated from visible fields. Imported JSON was not saved: ' +
          importWarning
        : 'Working setup updated. No filters are active and no sound was played.',
        !!importWarning);
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
  async function saveOutputTopology(options) {
    options = options || {};
    if (!outputTopology.draft) return;
    var pendingDraft = options.topology && outputTopology.dirty ? outputTopology.draft : null;
    outputTopology.saving = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    render();
    try {
      var payload = await postJSON('./output-topology', {
        output_topology: options.topology || outputTopology.draft
      });
      ingestOutputTopology(payload);
      if (pendingDraft) {
        outputTopology.draft = pendingDraft;
        outputTopology.dirty = true;
      }
      updateDriverResearchPromptPreview();
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
      var payload = await postJSON('./output-topology/reset', {});
      ingestOutputTopology(payload);
      patchActiveSpeaker({
        commissioningView: null,
        measurements: null,
        baselineProfile: null,
        error: '',
        commissionBusy: ''
      });
      // The server deleted the draft; a dirty form would otherwise keep the
      // old driver values and ship them back on the next save. Empty the form
      // before the fetch so a failed fetch cannot leave it stale either.
      ingestDesignDraft({status: 'not_saved', revision: 0}, {force: true});
      try {
        await fetchDesignDraft();
      } catch (draftError) {
        driverResearch.designDraft = {
          status: 'unreadable',
          summary: {},
          issues: [{message: draftError.message}]
        };
      }
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
    if (!outputTopology.hardwareRepin) return;
    var ok = await jtsConfirm(
      'JTS keeps your speaker layout, driver roles, output assignment and ' +
      'tuning, and pins the DAC attached now. Re-run the drift measurement, ' +
      'then Apply the baseline to resume audio.',
      // danger: the speaker goes silent immediately and the pair's drift
      // measurement is dropped, so a stray Enter must not land on confirm.
      {title: 'Pin the new DAC?', confirmLabel: 'Pin the new DAC', danger: true}
    );
    if (!ok) return;
    outputTopology.repinning = true;
    outputTopology.error = '';
    render();
    try {
      var payload = await postJSON('./output-topology/repin', {});
      ingestOutputTopology(payload);
      await refreshCommissioningView();
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
      if (e.status === 409 && e.body && e.body.output_topology) ingestOutputTopology(e.body);
      outputTopology.repinning = false;
      status('Could not pin the new DAC: ' + e.message, true);
    }
    render();
  }
  async function saveAndApplyBaselineProfile() {
    patchActiveSpeaker({
      loading: false, action: 'Finishing active profile',
      error: ''
    });
    render();
    try {
      var payload = await postJSON('./active-speaker/baseline-profile/save-and-apply', {});
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
    return await getJSON('./active-speaker/measurements');
  }
  async function fetchActiveSpeakerBaselineProfile() {
    var payload = await getJSON('./active-speaker/baseline-profile');
    tuningHandoff.programs = payload.tuning_programs || [];
    return payload;
  }
  async function loadState() {
    try {
      var payload = await getJSON('./state');
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
    if (volumeFloorTone.active || volumeFloorTone.inFlight) {
      stopVolumeFloorTone({keepalive: true, quiet: true, reason: 'pagehide'});
    }
    if (isSeatLevelRunning()) {
      stopSeatLevel({keepalive: true, quiet: true});
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
  wireCopyButtons(el('view-body'));
})();
