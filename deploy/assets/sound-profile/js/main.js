// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — parametric EQ editor.
//
// Static ES module served from /assets/sound-profile/js/ (revalidated by
// nginx, same delivery model as /system/). Relocated verbatim from the
// previously-inline _SOUND_JS in jasper/web/sound_setup.py; the only change
// is that the CSRF helpers read the <meta name=jts-csrf> tag rather than
// being string-substituted at render time. Module scope is strict mode —
// the IIFE declares all its state with var/function, so behaviour is
// unchanged.
//
// FOLLOW-UP (deferred, hardware-gated): unlike /system/'s JS — split into
// dom/format/charts/components/sections/views/api/actions/main — most render/
// state/IO logic is still one module. Pure helpers live in sibling modules:
// eq-math.js for RBJ biquad math and active-speaker-ui.js for active-crossover
// vocabulary/step policy. The editor's live-draft path still must be exercised
// on the Pi (band-drag + live-draft → CamillaDSP) before deeper splitting.
// Do not blind-refactor it. See docs/HANDOFF-management-ui.md.
import { jtsConfirm } from "/assets/shared/js/dialog.js";
import { escapeHtml } from "/assets/shared/js/escape.js";
import {
  DEFAULT_SUB_CROSSOVER_HZ,
  SUB_CROSSOVER_HZ_HI,
  SUB_CROSSOVER_HZ_LO,
  activeCommissionGroup,
  activeSpeakerStepState,
  clampSubwooferCrossoverFcHz,
  commissionCardState,
  commissionPayloadHasIssue,
  commissionPayloadFailure,
  defaultActiveSpeakerStep,
  humanMode,
  humanRole,
  levelMatchSummary,
  outputStatusClass,
  outputStepTitle,
  playbackResultMessage,
  subwooferCrossoverBand,
  subwooferCrossoverFcHz
} from "/assets/sound-profile/js/active-speaker-ui.js";
import { magnitudeDb, GAINLESS_TYPES } from "/assets/sound-profile/js/eq-math.js";
(function() {
  var LIMIT_DEFAULTS = {
    simple_gain_db: 12, advanced_gain_db: 12, max_parametric_bands: 8,
    min_freq_hz: 20, max_freq_hz: 20000, min_q: 0.2, max_q: 10, cut_max_q: 1.4,
    simple_bands: [], headroom_trim_max_db: 12,
    volume_floor_min_db: -60, volume_floor_max_db: -10
  };
  var DEFAULT_SAVED_ID = 'stock:flat';
  var FLAT = function() {
    return {enabled: true, curve_id: 'flat',
            simple_eq: zeroSimple(), parametric_bands: [],
            profile_id: '', profile_name: ''};
  };

  // Declared before FLAT() is first called below — zeroSimple() reads them.
  var simpleBands = [];        // [{key,field,label,freq_hz,type}] from /state
  var limits = Object.assign({}, LIMIT_DEFAULTS);

  var view = 'off';            // off | saved | draft
  var mode = 'simple';         // simple | peq
  var selectedId = null;       // selected library id on the Saved tab
  var draft = FLAT();          // working profile in the Draft tab
  var editing = {kind: 'new'}; // new | {kind:'user',id,name} | {kind:'preset',id,name}
  var activeBand = 0;
  var allCollapsed = false;
  var naming = false;
  var nameMode = 'save';       // 'save' (new/copy) | 'rename'
  var nameDraft = '';

  var applied = FLAT();        // persisted profile
  var library = [];            // [{id,name,kind,editable,description,profile,...}]
  var soundSettings = {
    headroom_trim_db: 0,
    match_loudness: false,
    volume_floor_db: -50
  };  // global output settings
  var volumeFloorDraftDb = null;
  var volumeFloorSaving = false;
  var curvesById = {};
  var dspWriteEpoch = 'none';
  var applying = false;
  var liveSourceSeq = 0, liveSourcePending = false, liveSourceOptions = {};
  var previewTimer = null, previewSeq = 0;
  var liveTimer = null, liveSeq = 0, liveInFlight = false, livePending = false;
  var statusText = '', statusErr = false;
  var ACTIVE_GAIN_EPSILON_DB = 0.05;
  var activeSpeaker = {
    loading: false, action: '', session: null,
    stagedConfig: null, calibrationLevel: null,
    startupLoad: null, measurements: null,
    baselineProfile: null, error: '', levelDbfs: null,
    combinedTestLevelDbfs: null,
    commission: null, commissioningView: null,
    commissionBusy: '', commissionError: ''
  };
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
  var outputTopology = {
    loading: false, saving: false, resetting: false, payload: null, draft: null,
    identity: null, clockDomain: null, activeRoute: null,
    observedHardware: null,
    revision: null,
    identitySaving: '', protectionSaving: '',
    error: '', dirty: false, touched: false
  };
  var outputStepOverride = '';
  var activeSpeakerSetupOpen = false;
  var outputTemplateDraftAxes = {layout: '', speakerMode: ''};
  var driverResearch = {
    inputs: {full_range: '', woofer: '', mid: '', tweeter: '', subwoofer: '', notes: ''},
    settings: {drivers: {}, crossovers: {}},
    importText: '',
    parsed: null,
    designDraft: null,
    error: '',
    dirty: false,
    saving: false,
    promptCopied: false,
    promptSelected: false
  };
  var crossoverPreview = {payload: null, preparing: false, error: ''};
  var ZERO_DETENT_DB = 0.1;
  var volumeFloorTone = {
    active: false,
    timer: null,
    inFlight: false,
    pending: null,
    generation: 0,
    savedNotice: false
  };
  var DRIVER_RESEARCH_NOTE_MAX_CHARS = 2048;

  function el(id) { return document.getElementById(id); }
  // Distributed-active Slice 4: a bonded active follower's /sound/ page mounts
  // this same module but emits a "sound-follower-data" island. In follower mode
  // the leader owns the program domain (content EQ / room correction / volume),
  // so we render ONLY the local active-speaker driver/crossover surface and skip
  // the content-EQ editor (its tabs + now-playing plot are absent from the page).
  //
  // The signal is the island's NON-EMPTY content — only the follower page emits
  // it; the solo page never does. Keying on content (not a bare presence check)
  // keeps a malformed island in the SAFE direction: a follower page has no tabs
  // or plot, so falling back to the solo render path would dereference absent
  // elements and blank the page. So: empty/absent → solo; present with content →
  // follower (even if the flag can't be parsed).
  var followerMode = (function() {
    var node = document.getElementById('sound-follower-data');
    var text = node && node.textContent ? node.textContent.trim() : '';
    if (!text) return false;
    try {
      return JSON.parse(text).follower !== false;
    } catch (e) {
      return true;
    }
  })();
  function csrfHeaders(headers) {
    var out = headers || {};
    var tokenEl = document.querySelector('meta[name=jts-csrf]');
    var token = tokenEl ? tokenEl.content : '';
    if (token) out['X-CSRF-Token'] = token;
    return out;
  }
  function jsonHeaders() {
    return csrfHeaders({'Content-Type': 'application/json'});
  }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, Number(v) || 0)); }
  function clone(o) { return JSON.parse(JSON.stringify(o || {})); }
  function fmtDb(v) { v = Number(v) || 0; return (v > 0 ? '+' : '') + v.toFixed(1); }
  function fmtFreq(v) {
    v = Number(v) || 0;
    return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + ' kHz' : Math.round(v) + ' Hz';
  }
  function fmtFreqShort(v) {
    v = Number(v) || 0;
    return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k' : String(Math.round(v));
  }
  function fmtQ(v) { return 'Q ' + (Number(v) || 0).toFixed(1); }
  function fmtDbfs(v) { return fmtDb(v) + ' dBFS'; }
  function patchActiveSpeaker(patch) {
    activeSpeaker = Object.assign({}, activeSpeaker, patch || {});
    return activeSpeaker;
  }
  function zeroSimple() {
    var out = {};
    (simpleBands.length ? simpleBands : LIMIT_DEFAULTS.simple_bands).forEach(function(b) {
      out[b.field] = 0;
    });
    if (!simpleBands.length) {
      ['sub_bass_db', 'bass_db', 'mid_db', 'presence_db', 'treble_db'].forEach(function(f) {
        if (!(f in out)) out[f] = 0;
      });
    }
    return out;
  }
  function ico(name, cls) {
    return '<svg class="' + (cls || 'ico') + '" aria-hidden="true"><use href="#icon-' + name + '"></use></svg>';
  }
  function status(msg, isErr) {
    statusText = msg || '';
    statusErr = !!isErr;
    var node = el('status');
    if (node) {
      node.textContent = statusText;
      node.className = 'status-line' + (statusErr ? ' err' : '');
    }
  }

  // ---- profile helpers ------------------------------------------------
  function normalizeProfile(raw) {
    raw = raw || {};
    var simple = raw.simple_eq || {};
    var normSimple = {};
    var bands = simpleBands.length ? simpleBands : [
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
    return library.find(function(e) { return e.id === id; }) || null;
  }
  function userEntries() { return library.filter(function(e) { return e.kind === 'custom'; }); }
  function presetEntries() { return library.filter(function(e) { return e.kind === 'stock'; }); }
  function fallbackSavedId() {
    if (entryById(DEFAULT_SAVED_ID)) return DEFAULT_SAVED_ID;
    return library.length ? library[0].id : null;
  }
  function selectedSavedEntry() {
    var entry = entryById(selectedId);
    if (entry) return entry;
    selectedId = fallbackSavedId();
    return selectedId ? entryById(selectedId) : null;
  }
  function selectedSavedProfile() {
    var entry = selectedSavedEntry();
    return entry ? withIdentity(normalizeProfile(entry.profile), entry.id, entry.name) : null;
  }
  function findIdFor(profile) {
    profile = normalizeProfile(profile);
    if (profile.profile_id && entryById(profile.profile_id)) return profile.profile_id;
    var key = profileKey(profile);
    var stock = library.find(function(e) { return e.kind === 'stock' && profileKey(e.profile) === key; });
    if (stock) return stock.id;
    var custom = library.find(function(e) { return e.kind === 'custom' && profileKey(e.profile) === key; });
    if (custom) return custom.id;
    return 'stock:' + (profile.curve_id || 'flat');
  }
  // The profile the editor sources from (for the modified/dirty check).
  function sourceProfile() {
    if (editing.kind === 'new') return FLAT();
    var entry = entryById(editing.id);
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
    if (view === 'off') return null;
    if (view === 'saved') {
      var entry = selectedSavedEntry();
      return entry ? normalizeProfile(entry.profile) : null;
    }
    return draft;
  }
  function liveLabel() {
    if (view === 'off') return 'Bypass';
    if (view === 'saved') {
      var entry = selectedSavedEntry();
      return entry ? entry.name : 'No profile selected';
    }
    if (editing.kind === 'new') return 'New profile' + (draftModified() ? ' · edited' : '');
    var lead = editing.kind === 'preset' ? 'From preset: ' : 'Editing: ';
    return lead + editing.name + (draftModified() ? ' · edited' : '');
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
  function bandType(s) { return s.type || s.biquad_type || 'Peaking'; }
  function isGainless(s) { return GAINLESS_TYPES.indexOf(bandType(s)) >= 0; }
  // Cut filters (HP/LP) get a tighter Q ceiling — a high-Q cut is a big
  // resonant boost at the corner. Mirrors CUT_MAX_Q in jasper/sound/profile.py.
  function bandQMax(type) {
    return (type === 'Highpass' || type === 'Lowpass') ? limits.cut_max_q : limits.max_q;
  }
  // Cut/notch bands are active by virtue of existing; gain-bearing bands
  // need a non-trivial gain to count (mirrors FilterSpec.active() in Python).
  function specActive(s) {
    return isGainless(s) || Math.abs(Number(s.gain_db || 0)) >= ACTIVE_GAIN_EPSILON_DB;
  }
  // Real RBJ biquad magnitude (shared eq-math.js, byte-equivalent to the
  // Python preview). Replaces the old exp() approximation; required for the
  // cut/notch types, which have no closed-form approximation.
  function responseDb(spec, freq) {
    return magnitudeDb(
      bandType(spec),
      Number(spec.freq_hz || spec.freq || 1000),
      Number(spec.gain_db || 0),
      Number(spec.q || 1),
      Number(freq) || 0
    );
  }
  function curveSpecs(profile) { return (curvesById[profile.curve_id] || {}).filters || []; }
  function simpleSpecs(profile) {
    var simple = profile.simple_eq || {};
    return (simpleBands.length ? simpleBands : []).map(function(b) {
      return {type: b.type, freq_hz: b.freq_hz, gain_db: simple[b.field] || 0,
              q: b.type === 'Peaking' ? 1.0 : undefined};
    });
  }
  function advancedSpecs(profile) {
    return (profile.parametric_bands || []).filter(function(b) { return b && b.enabled !== false; })
      .map(function(b) { return {type: b.type, freq_hz: b.freq_hz, gain_db: b.gain_db, q: b.q}; });
  }
  function pointsFor(specs, freqs, emptyWhenFlat) {
    specs = specs || [];
    if (emptyWhenFlat && !specs.some(specActive)) return [];
    return freqs.map(function(f) {
      var db = specs.reduce(function(sum, s) { return specActive(s) ? sum + responseDb(s, f) : sum; }, 0);
      return {freq_hz: f, db: db};
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
  var W = 620, H = 200, padL = 38, padR = 12, padT = 12, padB = 26;
  var MINDB = -12, MAXDB = 12, MINF = Math.log10(20), MAXF = Math.log10(20000);
  function gx(f) { return padL + (Math.log10(f) - MINF) / (MAXF - MINF) * (W - padL - padR); }
  function gy(db) { return padT + (MAXDB - db) / (MAXDB - MINDB) * (H - padT - padB); }
  function pathD(points) {
    var c = points.map(function(p) { return [gx(p.freq_hz), gy(clamp(p.db, MINDB, MAXDB))]; });
    var d = 'M' + c[0][0].toFixed(1) + ' ' + c[0][1].toFixed(1);
    for (var i = 1; i < c.length; i += 1) d += ' L' + c[i][0].toFixed(1) + ' ' + c[i][1].toFixed(1);
    return d;
  }
  function drawPath(points, cls) {
    if (!points || !points.length) return '';
    return '<path class="' + cls + '" d="' + pathD(points) + '"></path>';
  }
  function drawArea(points) {
    if (!points || !points.length) return '';
    return '<path class="area" d="' + pathD(points) +
      ' L' + gx(20000).toFixed(1) + ' ' + gy(MINDB).toFixed(1) +
      ' L' + gx(20).toFixed(1) + ' ' + gy(MINDB).toFixed(1) + ' Z"></path>';
  }
  // db value of the summed curve at an arbitrary frequency. The preview
  // points are ascending in freq_hz; interpolate linearly in log-frequency
  // so a band dot lands exactly ON the drawn curve regardless of filter type.
  function summedDbAt(points, freq) {
    if (!points || !points.length) return 0;
    if (freq <= points[0].freq_hz) return points[0].db;
    for (var i = 1; i < points.length; i += 1) {
      if (freq <= points[i].freq_hz) {
        var p0 = points[i - 1], p1 = points[i];
        var span = Math.log(p1.freq_hz) - Math.log(p0.freq_hz);
        var t = span > 0 ? (Math.log(freq) - Math.log(p0.freq_hz)) / span : 0;
        return p0.db + t * (p1.db - p0.db);
      }
    }
    return points[points.length - 1].db;
  }
  // One dot per band, sitting on the summed curve. Only the expanded band
  // adds a frequency guide line (+ width shading for Peaking) — no per-band
  // marker lines or component curves clutter the default view.
  function drawBandMarkers(summed) {
    if (view !== 'draft' || mode !== 'peq') return '';
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
    if (view !== 'draft' || mode !== 'peq' || allCollapsed || activeBand < 0) return -1;
    return activeBand;
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
    el('live-label').textContent = liveLabel();
    if (!profile) { renderGraph({preview: []}, false); return; }
    renderGraph(previewPayload(profile), profile.enabled !== false);
  }

  // ---- view rendering -------------------------------------------------
  function renderTabs() {
    ['off', 'saved', 'draft'].forEach(function(v) {
      var btn = el('tab-' + v);
      btn.setAttribute('aria-pressed', v === view ? 'true' : 'false');
      btn.classList.toggle('is-live', v === view);
    });
  }
  function render() {
    if (followerMode) {
      renderFollower();
      status(statusText, statusErr);
      return;
    }
    renderTabs();
    renderLiveGraph();
    if (view === 'off') renderOff();
    else if (view === 'saved') renderSaved();
    else renderDraft();
    status(statusText, statusErr);
  }

  // Follower mode renders the local driver/crossover/commissioning surface as the
  // page's primary content (expanded, not behind the Speaker setup disclosure a
  // solo box tucks it under). No EQ tabs/plot exist on a follower.
  function renderFollower() {
    el('view-body').innerHTML =
      '<div class="saved-stack"><section class="active-speaker-setup">' +
      renderOutputTopologySetup() +
      '</section></div>';
  }

  function renderOff() {
    el('view-body').innerHTML =
      '<div class="saved-stack">' +
      '<section class="off-card">' +
        '<div class="off-card__icon">' + ico('spark') + '</div>' +
        '<p class="off-card__text">Create a sound profile that changes how your speaker sounds.</p>' +
        '<div class="btn-row">' +
          '<button type="button" class="btn btn--ghost" data-act="browse-presets">Try a stock profile</button>' +
          '<button type="button" class="btn btn--primary" data-act="new-draft">Create custom profile</button>' +
        '</div>' +
      '</section>' +
      renderActiveSpeakerSetup() +
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
            users.map(function(e) { return profileRow(e, e.id === selectedId, true); }).join('') + '</div></div>'
        : '<div class="empty-card"><p>No profiles yet.</p>' +
            '<button type="button" class="btn btn--primary" data-act="new-draft">Create your first</button></div>') +
      '</section>';
    var presetSection = '<section><div class="section-header"><h2 class="eyebrow">Presets</h2></div>' +
      '<div class="list-card"><div class="list-card__rows">' +
        presets.map(function(e) { return profileRow(e, e.id === selectedId, false); }).join('') + '</div></div></section>';
    el('view-body').innerHTML = '<div class="saved-stack">' + userSection + presetSection +
      renderSoundSettings() + renderActiveSpeakerSetup() + '</div>';
  }
  function fmtTrim(v) { v = Number(v) || 0; return v > 0 ? '−' + v.toFixed(1) + ' dB' : 'Off'; }
  function fmtVolumeFloor(v) {
    v = Number(v);
    if (!isFinite(v)) v = -50;
    return v.toFixed(1) + ' dB';
  }
  function volumeFloorLimits() {
    var floorMin = Number(limits.volume_floor_min_db);
    var floorMax = Number(limits.volume_floor_max_db);
    if (!isFinite(floorMin)) floorMin = -60;
    if (!isFinite(floorMax)) floorMax = -10;
    return {min: floorMin, max: floorMax};
  }
  function savedVolumeFloorDb() {
    var bounds = volumeFloorLimits();
    var floor = Number(soundSettings.volume_floor_db);
    if (!isFinite(floor)) floor = -50;
    return clamp(floor, bounds.min, bounds.max);
  }
  function coerceVolumeFloorDb(value) {
    var bounds = volumeFloorLimits();
    var floor = Number(value);
    if (!isFinite(floor)) floor = savedVolumeFloorDb();
    return clamp(floor, bounds.min, bounds.max);
  }
  function volumeFloorValue() {
    return volumeFloorDraftDb === null || volumeFloorDraftDb === undefined ?
      savedVolumeFloorDb() : coerceVolumeFloorDb(volumeFloorDraftDb);
  }
  function volumeFloorDirty(v) {
    return Math.abs(coerceVolumeFloorDb(v) - savedVolumeFloorDb()) >= 0.05;
  }
  function syncVolumeFloorControls(v) {
    var value = coerceVolumeFloorDb(v);
    var node = el('set-volume-floor-readout');
    if (node) node.textContent = fmtVolumeFloor(value);
    var resetButton = el('view-body').querySelector('[data-act="reset-volume-floor"]');
    if (resetButton) resetButton.disabled = Math.abs(value - (-50)) < 0.05;
    var saveButton = el('volume-floor-save-button');
    if (saveButton) {
      var dirty = volumeFloorDirty(value);
      saveButton.disabled = volumeFloorSaving || !dirty;
      saveButton.textContent = volumeFloorSaving ? 'Saving' : (dirty ? 'Save floor' : 'Saved');
    }
  }
  function setVolumeFloorDraft(v) {
    volumeFloorDraftDb = coerceVolumeFloorDb(v);
    syncVolumeFloorControls(volumeFloorDraftDb);
  }
  function renderSoundSettings() {
    var ml = soundSettings.match_loudness ? ' checked' : '';
    var trim = Number(soundSettings.headroom_trim_db) || 0;
    var trimMax = Number(limits.headroom_trim_max_db) || 12;  // backend clamps authoritatively
    var floorBounds = volumeFloorLimits();
    var floorMin = floorBounds.min;
    var floorMax = floorBounds.max;
    var defaultFloor = -50;
    var floor = volumeFloorValue();
    var advancedOpen = trim > 0 || Math.abs(floor - defaultFloor) >= 0.05;
    var toneLabel = volumeFloorTone.active ? 'Stop tone' : 'Start tone';
    var resetDisabled = Math.abs(floor - defaultFloor) < 0.05 ? ' disabled' : '';
    var saveDisabled = (volumeFloorSaving || !volumeFloorDirty(floor)) ? ' disabled' : '';
    var saveLabel = volumeFloorSaving ? 'Saving' :
      (volumeFloorDirty(floor) ? 'Save floor' : 'Saved');
    return '<section class="sound-settings">' +
      '<div class="setting-row">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Match loudness</p>' +
          '<p class="setting-row__hint">Level-match profiles so switching compares tone, not volume.</p>' +
        '</div>' +
        '<label class="toggle"><input type="checkbox" id="set-match-loudness"' + ml +
          ' aria-label="Match loudness"><span class="track"></span></label>' +
      '</div>' +
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
  function renderActiveSpeakerSetup() {
    var open = activeSpeakerSetupOpen || activeSpeaker.loading ||
      activeSpeaker.session || activeSpeaker.stagedConfig ||
      activeSpeaker.error || outputTopology.loading || outputTopology.saving ||
      outputTopology.identitySaving || outputTopology.protectionSaving || outputTopology.error ||
      outputTopology.dirty || outputTopology.touched;
    return '<section class="active-speaker-setup">' +
      '<details class="advanced" data-active-speaker-setup' + (open ? ' open' : '') + '>' +
        '<summary>Speaker setup</summary>' +
        renderOutputTopologySetup() +
      '</details>' +
    '</section>';
  }
  // The "Test each driver" body for active 2/3-way groups: protected
  // single-audio-path commissioning. Arming is silent; a step makes one driver
  // audible through the production crossover/limiter graph.
  function renderCommissionCard() {
    var group = activeCommissionGroup(currentOutputTopology());
    var c = commissionCardState(
      activeSpeaker.commission,
      group,
      driverCheckRolesForGroup(group)
    );
    if (!c.available) return '';
    var busy = activeSpeaker.commissionBusy;
    var roleLabel = function(r) { return escapeHtml(humanRole(r)); };
    var buttonRoleLabel = function(r) {
      return escapeHtml(String(humanRole(r)).toLowerCase());
    };
    var toneRole = c.pendingRole || c.armedRole || c.startRole || '';
    var toneActive = !!c.canAck;
    var rampPreparing = commissionAutoRamp.running && !toneActive;
    var controlRole = toneRole || c.startRole || c.armedRole || '';
    var statusLabel = c.complete ? 'Complete' :
      (toneActive ? 'Tone playing' :
        (rampPreparing ? 'Preparing' :
          (c.stale ? 'Stopped' : (c.armed ? 'Ready' : 'Ready to start'))));
    var toneFrequency = Number(c.toneFrequencyHz);
    var toneFrequencyLabel = isFinite(toneFrequency) && toneFrequency > 0 ?
      ' at ' + fmtFreq(toneFrequency) : '';
    var statusRows =
      '<div class="commission-status">' +
      '<div><span class="commission-status__k">Armed driver</span>' +
        '<span class="commission-status__v">' +
        (c.armed ? roleLabel(c.armedRole) + ' (' +
          (c.armedGainDb == null ? '—'
            : (Number(c.armedGainDb) <= -120 ? 'silent floor'
              : escapeHtml(String(c.armedGainDb)) + ' dB')) + ')'
          : (c.startRole ? 'next: ' + roleLabel(c.startRole) : 'none — silent')) +
        '</span></div>' +
      '<div><span class="commission-status__k">Status</span>' +
        '<span class="commission-status__v">' +
        escapeHtml(statusLabel) + '</span></div>' +
      '<div><span class="commission-status__k">Confirmed</span>' +
        '<span class="commission-status__v">' +
        (c.confirmedRoles.length ? c.confirmedRoles.map(roleLabel).join(', ') : 'none') +
        '</span></div>' +
      '</div>';

    var buttons = [];
    if (c.complete) {
      buttons.push('<button type="button" class="btn btn--primary" ' +
        'data-act="output-step-next" data-step="safety">Continue to validate</button>');
    } else if (toneActive) {
      buttons.push('<button type="button" class="btn btn--danger" ' +
        'data-act="commission-abort">Stop</button>');
      buttons.push('<button type="button" class="btn btn--primary" ' +
        'data-act="commission-ack" data-outcome="heard_correct_driver"' +
        '>I hear the ' + buttonRoleLabel(toneRole || 'driver') + '</button>');
      buttons.push('<button type="button" class="btn btn--ghost" ' +
        'data-act="back-to-output-map">Back to outputs</button>');
    } else if (rampPreparing || busy) {
      buttons.push('<button type="button" class="btn btn--primary" disabled>' +
        escapeHtml(rampPreparing && controlRole ?
          'Preparing ' + humanRole(controlRole) : (busy || 'Preparing')) +
        '</button>');
    } else if (c.canStep) {
      buttons.push('<button type="button" class="btn btn--primary" ' +
        'data-act="commission-step" data-role="' + escapeHtml(c.startRole || c.armedRole || '') + '"' +
        '>Play ' + roleLabel(c.startRole || c.armedRole || 'driver') + '</button>');
      buttons.push('<button type="button" class="btn btn--ghost" ' +
        'data-act="back-to-output-map">Back to outputs</button>');
    }

    var note = activeSpeaker.commissionError ?
      '<p class="commission-card__error">' + escapeHtml(activeSpeaker.commissionError) + '</p>' :
      (toneActive ?
        '<p class="setting-row__hint">Tone is playing for ' + roleLabel(toneRole) +
          escapeHtml(toneFrequencyLabel) + '. Press Stop if anything sounds wrong.</p>' :
      (rampPreparing ?
        '<p class="setting-row__hint">JTS is preparing the protected ' +
          roleLabel(controlRole || 'driver') + ' path. No tone has started yet.</p>' :
        (c.complete ?
        '<p class="setting-row__hint">All drivers are confirmed. Continue to validate the active speaker profile.</p>' :
        (c.stale ?
        '<p class="setting-row__hint">The previous tone session expired safely. Play will reopen it quietly.</p>' :
        (c.armed ?
        '<p class="setting-row__hint">Ready to play a quiet tone for ' +
          roleLabel(c.armedRole) + '.</p>' :
        '<p class="setting-row__hint">Play a quiet tone for ' +
          roleLabel(c.startRole || 'driver') +
          '. It will stay continuous and get louder gradually.</p>')))));
    var busyNote = busy && !toneActive ?
      '<p class="setting-row__hint">' + escapeHtml(busy) + '…</p>' : '';
    var roles = activeCommissionRoles(group);
    var driverRows = roles.map(function(role) {
      var confirmed = c.confirmedRoles.indexOf(role) >= 0;
      return '<li class="output-identity-row">' +
        '<span>' + roleLabel(role) + '</span>' +
        '<strong>' + escapeHtml(confirmed ? 'Heard' : 'Not heard yet') + '</strong>' +
      '</li>';
    }).join('');

    return '<div class="commission-card">' +
      statusRows + note + busyNote +
      '<div class="active-speaker-actions commission-card__actions">' +
        buttons.join('') + '</div>' +
      (driverRows ? '<ul class="output-identity-list">' + driverRows + '</ul>' : '') +
      '<p class="setting-row__hint commission-card__followup">Confirming each driver ' +
        'by ear proves it is wired and audible through the crossover and limiter. ' +
        'Mic-based level matching is a separate HTTPS measurement step after this basic setup.</p>' +
    '</div>';
  }
  function renderActiveCommissionOnlyCard() {
    return '<div class="output-card output-card--commission-only">' +
      '<div class="output-card__head"><div><p class="output-card__title">No active driver test</p>' +
        '<p class="setting-row__hint">Per-driver audible commissioning is available only for active crossover layouts.</p></div>' +
        '<span class="status-pill">not needed</span></div>' +
      '<p class="setting-row__hint">Passive and full-range layouts use the normal listening path; there is no separate direct-DAC driver test in the product UI.</p>' +
    '</div>';
  }
  function activeCommissionRoles(group) {
    var seen = {};
    var order = ['woofer', 'mid', 'tweeter'];
    (group && Array.isArray(group.channels) ? group.channels : []).forEach(function(ch) {
      if (ch && ch.role) seen[ch.role] = true;
    });
    return order.filter(function(r) { return seen[r]; });
  }
  function currentOutputTopology() {
    return outputTopology.draft || outputTopology.payload || null;
  }
  function outputGroups(topology) {
    return topology && Array.isArray(topology.speaker_groups) ? topology.speaker_groups : [];
  }
  function outputHardware(topology) {
    return topology && topology.hardware ? topology.hardware : null;
  }
  function physicalOutputOptions(topology) {
    var hardware = outputHardware(topology) || {};
    var outputs = Array.isArray(hardware.outputs) ? hardware.outputs : [];
    if (outputs.length) {
      return outputs.map(function(output) {
        var index = Number(output.index);
        return {
          index: index,
          label: output.human_label || ('Output ' + (index + 1))
        };
      }).filter(function(output) {
        return isFinite(output.index);
      });
    }
    var count = Number(hardware.physical_output_count) || 0;
    var fallback = [];
    for (var i = 0; i < count; i += 1) {
      fallback.push({index: i, label: 'Output ' + (i + 1)});
    }
    return fallback;
  }
  function physicalOutputLabel(topology, index) {
    var wanted = Number(index);
    var options = physicalOutputOptions(topology);
    for (var i = 0; i < options.length; i += 1) {
      if (Number(options[i].index) === wanted) return options[i].label;
    }
    return isFinite(wanted) ? 'Output ' + (wanted + 1) : 'No output assigned';
  }
  function observedOutputHardware() {
    return outputTopology.observedHardware || null;
  }
  function hardwareId(hardware) {
    return hardware ? String(hardware.device_id || hardware.profile_id || '') : '';
  }
  function observedHardwareId(hardware) {
    return hardware ? String(hardware.profile_id || hardware.device_id || '') : '';
  }
  function hardwareLabel(hardware, fallback) {
    return hardware ? String(
      hardware.device_label || hardware.profile_label ||
      hardware.device_id || hardware.profile_id || fallback || 'Unknown output device'
    ) : (fallback || 'Unknown output device');
  }
  function hardwareOutputCount(hardware) {
    return Number(hardware && hardware.physical_output_count) || 0;
  }
  function hardwareSummary(label, count) {
    return label + ' (' + count + ' physical output' + (count === 1 ? '' : 's') + ')';
  }
  var observedHardwareClockIssueCodes = {
    dual_apple_observation_missing: true,
    dual_apple_usb_topology_mismatch: true,
    dual_apple_usb_topology_unknown: true,
    dual_apple_stable_identity_missing: true,
    dual_apple_endpoint_not_synchronous: true
  };
  function isObservedHardwareClockIssue(issue) {
    var code = String(issue && issue.code || '');
    return code.indexOf('dual_apple_observed_') === 0 ||
      !!observedHardwareClockIssueCodes[code];
  }
  function outputClockHardwareBlockers() {
    var clock = outputClockDomainReport();
    var issues = clock && Array.isArray(clock.issues) ? clock.issues : [];
    return issues.filter(function(issue) {
      return issue && issue.severity === 'blocker' &&
        isObservedHardwareClockIssue(issue);
    });
  }
  function outputHardwareMismatch(topology) {
    var saved = outputHardware(topology);
    var observed = observedOutputHardware();
    var clockBlockers = outputClockHardwareBlockers();
    if (!saved || (!observed && !clockBlockers.length)) return null;
    var savedId = hardwareId(saved);
    var currentId = observedHardwareId(observed);
    var savedCount = hardwareOutputCount(saved);
    var currentCount = hardwareOutputCount(observed);
    var idMismatch = !!(savedId && currentId && savedId !== currentId);
    var countMismatch = savedCount !== currentCount;
    if (!idMismatch && !countMismatch && !clockBlockers.length) return null;
    var savedLabel = hardwareLabel(saved, 'Saved hardware');
    var currentLabel = hardwareLabel(observed, 'Attached hardware');
    var currentSummary = observed
      ? 'currently attached hardware is ' + hardwareSummary(currentLabel, currentCount)
      : 'current output hardware has not been observed';
    var blockerMessages = clockBlockers.map(function(issue) {
      return String(issue.message || '');
    }).filter(Boolean);
    return {
      savedLabel: savedLabel,
      currentLabel: currentLabel,
      savedCount: savedCount,
      currentCount: currentCount,
      clockBlockers: clockBlockers,
      message: 'Saved topology expects ' +
        hardwareSummary(savedLabel, savedCount) +
        ', but ' + currentSummary + '.' +
        (blockerMessages.length ? ' ' + blockerMessages.join(' ') : '')
    };
  }
  function outputEvaluation(topology) {
    return topology && topology.evaluation ? topology.evaluation : {};
  }
  function outputIdentityReport() {
    return outputTopology.identity || null;
  }
  function outputClockDomainReport() {
    return outputTopology.clockDomain || null;
  }
  function outputActiveRoute() {
    return outputTopology.activeRoute || null;
  }
  function identityTargetFor(groupId, role) {
    var report = outputIdentityReport();
    var targets = report && Array.isArray(report.targets) ? report.targets : [];
    return targets.find(function(target) {
      return target.speaker_group_id === groupId && target.role === role;
    }) || null;
  }
  function outputAssignedMap(topology) {
    var out = {};
    outputGroups(topology).forEach(function(group) {
      (group.channels || []).forEach(function(channel) {
        if (channel.physical_output_index == null) return;
        out[String(channel.physical_output_index)] = {
          group: group.label || group.id,
          role: channel.role || 'channel'
        };
      });
    });
    return out;
  }
  function outputAssignedToOtherMap(topology, groupId, role) {
    var out = {};
    outputGroups(topology).forEach(function(group) {
      (group.channels || []).forEach(function(channel) {
        if (channel.physical_output_index == null) return;
        if ((group.id || '') === groupId && (channel.role || '') === role) return;
        out[String(channel.physical_output_index)] =
          (group.label || group.id) + ' · ' + humanRole(channel.role);
      });
    });
    return out;
  }
  function outputRoleSummary(topology) {
    var roles = [];
    outputGroups(topology).forEach(function(group) {
      (group.channels || []).forEach(function(channel) {
        var role = channel.role || '';
        if (role && roles.indexOf(role) < 0) roles.push(role);
      });
    });
    if (!roles.length) roles = ['woofer', 'tweeter'];
    return roles.sort(function(a, b) {
      var order = {full_range: 0, woofer: 1, mid: 2, tweeter: 3, subwoofer: 4};
      return (order[a] || 99) - (order[b] || 99);
    });
  }
  function driverResearchRoles(topology) {
    var pairs = activeCrossoverPairs(topology);
    if (!pairs.length) return outputRoleSummary(topology);
    var roles = [];
    pairs.forEach(function(pair) {
      pair.forEach(function(role) {
        if (roles.indexOf(role) < 0) roles.push(role);
      });
    });
    var order = {full_range: 0, woofer: 1, mid: 2, tweeter: 3};
    return roles.sort(function(a, b) {
      return (order[a] || 99) - (order[b] || 99);
    });
  }
  function assignedOutputIndices(topology) {
    var used = {};
    outputGroups(topology).forEach(function(group) {
      (group.channels || []).forEach(function(channel) {
        if (channel.physical_output_index != null) {
          used[String(channel.physical_output_index)] = true;
        }
      });
    });
    return used;
  }
  function firstUnusedOutputIndex(topology) {
    var hardware = outputHardware(topology) || {};
    var count = Number(hardware.physical_output_count || 0);
    var used = assignedOutputIndices(topology);
    for (var index = 0; index < count; index += 1) {
      if (!used[String(index)]) return index;
    }
    return null;
  }
  function outputSubwooferGroup(topology) {
    return outputGroups(topology).find(function(group) {
      return group.kind === 'subwoofer' || group.mode === 'subwoofer';
    }) || null;
  }
  function outputHasSubwoofer(topology) {
    return !!outputSubwooferGroup(topology);
  }
  function nextSubwooferGroupId(topology) {
    var existing = {};
    outputGroups(topology).forEach(function(group) { existing[group.id] = true; });
    if (!existing.sub) return 'sub';
    var i = 2;
    while (existing['sub_' + i]) i += 1;
    return 'sub_' + i;
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
  function removeSubwooferFromTopology(topology) {
    var next = baseOutputDraft(topology);
    if (!next) return next;
    var subIds = {};
    next.speaker_groups = (next.speaker_groups || []).filter(function(group) {
      var isSub = group.kind === 'subwoofer' || group.mode === 'subwoofer';
      if (isSub) subIds[group.id] = true;
      return !isSub;
    });
    next.routing = Object.assign({}, next.routing || {}, {
      subwoofer_group_ids: (next.routing && next.routing.subwoofer_group_ids || [])
        .filter(function(id) { return !subIds[id]; })
    });
    return next;
  }
  function driverResearchRoleLabel(role) {
    return {
      woofer: 'Woofer / midbass',
      mid: 'Midrange',
      tweeter: 'Tweeter / high-frequency driver',
      subwoofer: 'Subwoofer'
    }[role] || humanRole(role);
  }
  function activeCrossoverPairs(topology) {
    var pairs = [];
    var seen = {};
    outputGroups(topology).forEach(function(group) {
      var groupPairs = group.mode === 'active_3_way'
        ? [['woofer', 'mid'], ['mid', 'tweeter']]
        : (group.mode === 'active_2_way' ? [['woofer', 'tweeter']] : []);
      groupPairs.forEach(function(pair) {
        var key = pair.join(':');
        if (!seen[key]) {
          seen[key] = true;
          pairs.push(pair);
        }
      });
    });
    return pairs;
  }
  function crossoverSettingKey(pair) {
    return String(pair[0] || '') + ':' + String(pair[1] || '');
  }
  function driverSetting(role) {
    if (!driverResearch.settings.drivers) driverResearch.settings.drivers = {};
    var drivers = driverResearch.settings.drivers;
    if (!drivers[role]) drivers[role] = {};
    return drivers[role];
  }
  function crossoverSetting(pair) {
    if (!driverResearch.settings.crossovers) driverResearch.settings.crossovers = {};
    var crossovers = driverResearch.settings.crossovers;
    var key = crossoverSettingKey(pair);
    if (!crossovers[key]) crossovers[key] = {};
    return crossovers[key];
  }
  function manualNumberValue(raw) {
    if (raw === '' || raw == null) return null;
    var value = Number(raw);
    return isFinite(value) ? value : null;
  }
  function activeRoleLabel(role) {
    return {
      full_range: 'full range',
      woofer: 'woofer',
      mid: 'midrange',
      tweeter: 'tweeter',
      subwoofer: 'subwoofer'
    }[role] || String(role || 'driver').replace(/_/g, ' ');
  }
  function uniqueRoleLabels(roles) {
    var seen = {};
    return (roles || []).map(activeRoleLabel).filter(function(label) {
      if (!label || seen[label]) return false;
      seen[label] = true;
      return true;
    });
  }
  function roleListText(roles, options) {
    options = options || {};
    var labels = uniqueRoleLabels(roles);
    if (!labels.length) return 'drivers';
    if (labels.length === 1) return labels[0];
    if (labels.length === 2) return labels[0] + (options.two || ' + ') + labels[1];
    return labels.slice(0, -1).join(', ') + (options.final || ', ') +
      labels[labels.length - 1];
  }
  function roleSentenceText(roles) {
    return roleListText(roles, {two: ' and ', final: ', and '});
  }
  function pairRoleKey(pair) {
    return (pair || []).map(String).sort().join(':');
  }
  function candidateMatchesPair(candidate, pair) {
    return candidate && Array.isArray(candidate.between_roles) &&
      candidate.between_roles.length === 2 &&
      pairRoleKey(candidate.between_roles) === pairRoleKey(pair);
  }
  function candidateFrequency(candidate) {
    var frequency = manualNumberValue(candidate && candidate.frequency_hz);
    return frequency != null && frequency > 0 ? frequency : null;
  }
  function designDraftCandidates() {
    var draftPayload = driverResearch.designDraft || {};
    var manual = draftPayload.manual_settings || {};
    var research = draftPayload.driver_research || {};
    return []
      .concat(Array.isArray(manual.crossover_candidates) ? manual.crossover_candidates : [])
      .concat(Array.isArray(research.crossover_candidates) ? research.crossover_candidates : []);
  }
  function designDraftDrivers() {
    var draftPayload = driverResearch.designDraft || {};
    var manual = draftPayload.manual_settings || {};
    var research = draftPayload.driver_research || {};
    return []
      .concat(Array.isArray(manual.drivers) ? manual.drivers : [])
      .concat(Array.isArray(research.drivers) ? research.drivers : []);
  }
  function draftCrossoverFrequency(pair) {
    var candidates = designDraftCandidates();
    for (var i = 0; i < candidates.length; i += 1) {
      if (candidateMatchesPair(candidates[i], pair)) {
        var frequency = candidateFrequency(candidates[i]);
        if (frequency != null) return frequency;
      }
    }
    return null;
  }
  function currentCrossoverFrequency(pair) {
    var crossovers = driverResearch.settings && driverResearch.settings.crossovers || {};
    var key = crossoverSettingKey(pair);
    var setting = crossovers[key] || {};
    if (Object.prototype.hasOwnProperty.call(setting, 'frequency_hz')) {
      return candidateFrequency(setting);
    }
    return draftCrossoverFrequency(pair);
  }
  function driverForRole(role) {
    var setting = driverResearch.settings && driverResearch.settings.drivers &&
      driverResearch.settings.drivers[role] || {};
    var drivers = designDraftDrivers();
    var draftDriver = {};
    for (var i = 0; i < drivers.length; i += 1) {
      if (drivers[i] && String(drivers[i].role || '') === String(role)) {
        draftDriver = drivers[i];
        break;
      }
    }
    return Object.assign({}, draftDriver, setting, {
      model: String(driverResearch.inputs[role] || setting.model || draftDriver.model || '').trim()
    });
  }
  function driverHasInfo(role) {
    var driver = driverForRole(role);
    return !!(driver.model ||
      driver.sensitivity_db_2v83_1m != null ||
      driver.nominal_impedance_ohm != null ||
      driver.recommended_highpass_hz != null ||
      driver.recommended_lowpass_hz != null ||
      driver.do_not_test_below_hz != null ||
      driver.gain_offset_db != null ||
      driver.notes);
  }
  function driverHasSafetyNotes(role) {
    var driver = driverForRole(role);
    return !!(driver.recommended_highpass_hz != null ||
      driver.recommended_lowpass_hz != null ||
      driver.do_not_test_below_hz != null ||
      driver.gain_offset_db != null ||
      driver.notes);
  }
  function driverSafetyNoteRoles(topology) {
    return driverResearchRoles(topology).filter(driverHasSafetyNotes);
  }
  function workingCrossoverSummary(topology) {
    var pairs = activeCrossoverPairs(topology);
    if (!pairs.length) {
      return {
        ready: true,
        text: 'no active crossover point is needed for this layout'
      };
    }
    var entries = [];
    var missing = [];
    pairs.forEach(function(pair) {
      var frequency = currentCrossoverFrequency(pair);
      if (frequency == null) {
        missing.push(pair);
        return;
      }
      entries.push({
        pair: pair,
        label: activeRoleLabel(pair[0]) + '/' + activeRoleLabel(pair[1]),
        frequency: frequency
      });
    });
    if (!entries.length) {
      return {
        ready: false,
        text: 'Add crossover points before previewing the active crossover.'
      };
    }
    var text = entries.length === 1 && pairs.length === 1
      ? 'crossover ' + fmtFreq(entries[0].frequency)
      : 'Crossovers: ' + entries.map(function(entry) {
        return entry.label + ' ' + fmtFreq(entry.frequency);
      }).join(', ');
    if (missing.length) {
      text += '. Add the remaining crossover point before previewing the active crossover.';
    }
    return {ready: !missing.length, text: text};
  }
  function workingSetupSummary(topology) {
    if (!topology || !outputGroups(topology).length) {
      return 'Choose a speaker layout to start the working setup. No filters are active yet.';
    }
    var roles = outputRoleSummary(topology);
    var crossover = workingCrossoverSummary(topology);
    var text = 'Working setup: ' + roleListText(roles);
    if (crossover.text.indexOf('Crossovers:') === 0 ||
        crossover.text.charAt(crossover.text.length - 1) === '.') {
      text += '. ' + crossover.text;
    } else {
      text += ', ' + crossover.text + '.';
    }
    if (crossover.ready) text += ' No filters are active yet.';
    return text;
  }
  function driverResearchHasPreviewInputs(topology) {
    if (!topology || !outputGroups(topology).length) return false;
    var rolesReady = driverResearchRoles(topology).every(driverHasInfo);
    var pairs = activeCrossoverPairs(topology);
    var crossoversReady = pairs.length > 0 && pairs.every(function(pair) {
      return currentCrossoverFrequency(pair) != null;
    });
    return rolesReady && crossoversReady;
  }
  function driverResearchMissingPreviewMessage(topology) {
    if (!topology || !outputGroups(topology).length) {
      return 'Choose and save a speaker layout before previewing the active crossover.';
    }
    var missingDrivers = driverResearchRoles(topology).filter(function(role) {
      return !driverHasInfo(role);
    });
    if (missingDrivers.length) {
      return 'Add driver info for ' + roleSentenceText(missingDrivers) +
        ' before previewing the active crossover.';
    }
    return 'Add crossover points before previewing the active crossover.';
  }
  function driverResearchPromptReady(topology) {
    if (!topology || !outputGroups(topology).length) return false;
    return driverResearchRoles(topology).every(function(role) {
      return !!String(driverResearch.inputs[role] || '').trim();
    });
  }
  function setManualDriverField(role, field, value) {
    driverSetting(role)[field] = value;
    driverResearch.error = '';
    driverResearch.dirty = true;
  }
  function setManualCrossoverField(pairKey, field, value) {
    if (!driverResearch.settings.crossovers[pairKey]) {
      driverResearch.settings.crossovers[pairKey] = {};
    }
    driverResearch.settings.crossovers[pairKey][field] = value;
    driverResearch.error = '';
    driverResearch.dirty = true;
  }
  function manualSettingsPayload(topology) {
    var drivers = driverResearchRoles(topology).map(function(role) {
      var setting = driverSetting(role);
      var out = {
        role: role,
        model: (driverResearch.inputs[role] || '').trim()
      };
      [
        'sensitivity_db_2v83_1m',
        'nominal_impedance_ohm',
        'recommended_highpass_hz',
        'recommended_lowpass_hz',
        'do_not_test_below_hz',
        'gain_offset_db'
      ].forEach(function(field) {
        var value = manualNumberValue(setting[field]);
        if (value != null) out[field] = value;
      });
      if ((setting.notes || '').trim()) out.notes = String(setting.notes).trim();
      return out;
    }).filter(function(driver) {
      return driver.model ||
        driver.sensitivity_db_2v83_1m != null ||
        driver.nominal_impedance_ohm != null ||
        driver.recommended_highpass_hz != null ||
        driver.recommended_lowpass_hz != null ||
        driver.do_not_test_below_hz != null ||
        driver.gain_offset_db != null ||
        driver.notes;
    });
    var candidates = activeCrossoverPairs(topology).map(function(pair) {
      var setting = crossoverSetting(pair);
      var frequency = manualNumberValue(setting.frequency_hz);
      if (frequency == null) return null;
      return {
        between_roles: pair,
        frequency_hz: frequency,
        filter_type: setting.filter_type || 'Linkwitz-Riley',
        slope_db_per_octave: manualNumberValue(setting.slope_db_per_octave) || 24,
        confidence: 'medium',
        rationale: 'Operator-entered crossover setting.'
      };
    }).filter(Boolean);
    return drivers.length || candidates.length
      ? {drivers: drivers, crossover_candidates: candidates}
      : null;
  }
  // Mirror jasper/active_speaker/crossover_preview.py:_CONFIDENCE_RANK so the
  // form selects the same candidate the preview will.
  var CANDIDATE_CONFIDENCE_RANK = {high: 3, medium: 2, low: 1, unknown: 0};
  function candidateConfidenceRank(candidate) {
    return CANDIDATE_CONFIDENCE_RANK[
      String((candidate && candidate.confidence) || 'unknown')
    ] || 0;
  }
  function proposeSensitivityTrims(driversByRole) {
    // Propose a starting level trim from the sensitivity gap so a hotter
    // compression/horn driver is never left at full level relative to the
    // woofer. The operator reviews/confirms the value; the server enforces the
    // same fail-safe (baseline_profile.py:_derive_corrections). Only fill when
    // the field is empty — never clobber an operator/research-supplied trim.
    var sensitivities = {};
    Object.keys(driversByRole).forEach(function(role) {
      var sens = manualNumberValue(driversByRole[role].sensitivity_db_2v83_1m);
      if (sens != null) sensitivities[role] = sens;
    });
    var roles = Object.keys(sensitivities);
    if (roles.length < 2) return;
    var reference = Math.min.apply(null, roles.map(function(role) {
      return sensitivities[role];
    }));
    roles.forEach(function(role) {
      var setting = driverSetting(role);
      if (manualNumberValue(setting.gain_offset_db) != null) return;  // keep explicit
      var trim = Math.round((reference - sensitivities[role]) * 10) / 10;  // <= 0
      if (trim < 0) setting.gain_offset_db = trim;
    });
  }
  function applyDriverResearchToManualSettings(payload) {
    if (!payload || typeof payload !== 'object') return;
    var driversByRole = {};
    (Array.isArray(payload.drivers) ? payload.drivers : []).forEach(function(driver) {
      if (!driver || !driver.role) return;
      var role = String(driver.role);
      driversByRole[role] = driver;
      if (driver.model && !driverResearch.inputs[role]) {
        driverResearch.inputs[role] = String(driver.model);
      }
      [
        'sensitivity_db_2v83_1m',
        'nominal_impedance_ohm',
        'recommended_highpass_hz',
        'recommended_lowpass_hz',
        'do_not_test_below_hz',
        'gain_offset_db'
      ].forEach(function(field) {
        if (driver[field] != null) driverSetting(role)[field] = driver[field];
      });
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
    });
    proposeSensitivityTrims(driversByRole);
  }
  function driverResearchPrompt(topology) {
    var roles = driverResearchRoles(topology);
    var lines = roles.map(function(role) {
      var name = (driverResearch.inputs[role] || '').trim();
      return '- ' + role + ': ' + (name || '[user has not entered a model yet]');
    });
    var notes = (driverResearch.inputs.notes || '').trim();
    return [
      'You are helping configure a safe active crossover for a JTS Raspberry Pi speaker.',
      '',
      'Driver list:',
      lines.join('\n'),
      notes ? '\nUser notes:\n' + notes : '',
      '',
      'Research manufacturer datasheets and reputable measurements. Prefer primary manufacturer data.',
      'Do not invent missing facts. Use null when a value is unknown. Include source URLs for every non-obvious claim.',
      'Focus on safe starting points, not final tuning. Assume JTS will start test signals extremely quiet and the human must approve any listening result.',
      'Keep each driver notes field concise: safety-relevant details only, 2048 characters or fewer; do not paste a full research report.',
      '',
      'Return only JSON with this shape:',
      '{',
      '  "artifact_schema_version": 1,',
      '  "kind": "jts_active_crossover_driver_research",',
      '  "drivers": [',
      '    {',
      '      "role": "full_range|woofer|mid|tweeter",',
      '      "model": "string",',
      '      "manufacturer": "string|null",',
      '      "nominal_impedance_ohm": 8,',
      '      "sensitivity_db_2v83_1m": 90,',
      '      "usable_frequency_range_hz": [80, 5000],',
      '      "recommended_highpass_hz": 80,',
      '      "recommended_lowpass_hz": 2200,',
      '      "do_not_test_below_hz": 1200,',
      '      "gain_offset_db": -6,',
      '      "notes": "safety-relevant summary, <= 2048 chars, not a full report",',
      '      "sources": ["https://..."]',
      '    }',
      '  ],',
      '  "crossover_candidates": [',
      '    {',
      '      "between_roles": ["woofer", "tweeter"],',
      '      "frequency_hz": 1800,',
      '      "filter_type": "Linkwitz-Riley",',
      '      "slope_db_per_octave": 24,',
      '      "confidence": "low|medium|high",',
      '      "rationale": "why this is a safe starting point",',
      '      "warnings": ["short warnings"]',
      '    }',
      '  ],',
      '  "human_review": {',
      '    "must_verify_wiring": true,',
      '    "must_start_quiet": true,',
      '    "needs_measurement_before_final": true',
      '  }',
      '}'
    ].filter(Boolean).join('\n');
  }
  function summarizeDriverResearchPayload(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('Driver research must be a JSON object.');
    }
    if (payload.kind !== 'jts_active_crossover_driver_research') {
      throw new Error('Driver research kind must be jts_active_crossover_driver_research.');
    }
    if (Number(payload.artifact_schema_version) !== 1) {
      throw new Error('Driver research artifact_schema_version must be 1.');
    }
    var drivers = Array.isArray(payload.drivers) ? payload.drivers : [];
    var candidates = Array.isArray(payload.crossover_candidates) ? payload.crossover_candidates : [];
    if (!drivers.length) throw new Error('Driver research must include at least one driver.');
    drivers.forEach(function(driver, index) {
      if (!driver || driver.notes == null || driver.notes === '') return;
      var role = driver.role || 'driver ' + (index + 1);
      if (typeof driver.notes !== 'string') {
        throw new Error('Driver research notes for ' + role + ' must be a string.');
      }
      var normalized = driver.notes.trim().split(/\s+/).filter(Boolean).join(' ');
      if (normalized.length > DRIVER_RESEARCH_NOTE_MAX_CHARS) {
        throw new Error(
          'Driver research notes for ' + role + ' must be <= ' +
          DRIVER_RESEARCH_NOTE_MAX_CHARS + ' chars.'
        );
      }
    });
    return {
      driverCount: drivers.length,
      candidateCount: candidates.length,
      roles: drivers.map(function(driver) { return driver.role || 'unknown'; })
        .filter(function(role, index, arr) { return arr.indexOf(role) === index; }),
      warnings: candidates.reduce(function(out, candidate) {
        return out.concat(Array.isArray(candidate.warnings) ? candidate.warnings : []);
      }, []).slice(0, 4)
    };
  }
  function driverResearchDraftSaved() {
    var draftPayload = driverResearch.designDraft || {};
    var savedStatus = draftPayload.status || '';
    return savedStatus === 'ready_for_review' && !driverResearch.dirty;
  }
  function driverResearchCanPreparePreview() {
    var draftPayload = driverResearch.designDraft || {};
    var savedStatus = draftPayload.status || '';
    return savedStatus && savedStatus !== 'not_saved' && savedStatus !== 'unreadable' &&
      !driverResearch.dirty && driverResearchHasPreviewInputs(currentOutputTopology());
  }
  function crossoverPreviewReadyForProtectedStaging(payload) {
    payload = payload || {};
    var permissions = payload.permissions || {};
    return payload.kind === 'jts_active_speaker_crossover_preview' &&
      payload.status === 'ready_for_protected_staging' &&
      permissions.may_prepare_protected_startup_config === true;
  }
  function driverResearchStepSatisfied() {
    var draftPayload = driverResearch.designDraft || {};
    var savedStatus = draftPayload.status || '';
    return savedStatus && savedStatus !== 'not_saved' && savedStatus !== 'unreadable' &&
      !driverResearch.dirty;
  }
  function driverResearchFlowComplete(topology) {
    if (!activeCommissionGroup(topology)) return driverResearchStepSatisfied();
    return driverResearchStepSatisfied() &&
      crossoverPreviewReadyForProtectedStaging(crossoverPreview.payload);
  }
  function driverResearchWorkingStatusLabel(status) {
    if (driverResearch.dirty) return 'editing';
    if (driverResearchHasPreviewInputs(currentOutputTopology())) return 'ready to preview';
    if (status === 'blocked') return 'needs speaker layout';
    if (status === 'unreadable') return 'needs review';
    if (status === 'needs_research') return 'needs crossover info';
    return 'working setup';
  }
  function driverResearchWorkingStatusClass(status) {
    if (!driverResearch.dirty && driverResearchHasPreviewInputs(currentOutputTopology())) {
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
    driverResearch.settings = {drivers: {}, crossovers: {}};
    var manual = payload.manual_settings || {};
    (Array.isArray(manual.drivers) ? manual.drivers : []).forEach(function(driver) {
      if (!driver || !driver.role) return;
      var role = String(driver.role);
      if (driver.model && !driverResearch.inputs[role]) {
        driverResearch.inputs[role] = String(driver.model);
      }
      driverResearch.settings.drivers[role] = Object.assign(
        {},
        driverResearch.settings.drivers[role] || {},
        driver
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
        driverResearch.error = '';
      } catch (e) {
        driverResearch.parsed = null;
        driverResearch.error = e.message;
      }
    } else {
      driverResearch.importText = '';
      driverResearch.parsed = null;
      driverResearch.error = '';
    }
    driverResearch.dirty = false;
    driverResearch.promptCopied = false;
    driverResearch.promptSelected = false;
  }
  async function fetchDesignDraft() {
    var resp = await fetch('./active-speaker/design-draft', {cache: 'no-store'});
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'speaker design draft failed');
    ingestDesignDraft(payload);
    return payload;
  }
  function ingestCrossoverPreview(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    crossoverPreview.payload = payload;
    crossoverPreview.preparing = false;
    crossoverPreview.error = '';
  }
  async function fetchCrossoverPreview() {
    var resp = await fetch('./active-speaker/crossover-preview', {cache: 'no-store'});
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'crossover preview failed');
    ingestCrossoverPreview(payload);
    return payload;
  }
  function toneSummary(tone) {
    var frequency = Number(tone.frequency_hz);
    var level = Number(tone.level_dbfs);
    var duration = Number(tone.duration_ms);
    if (!isFinite(frequency) || !isFinite(level) || !isFinite(duration)) {
      return 'unknown';
    }
    return Math.round(frequency) + ' Hz at ' + level.toFixed(1) + ' dBFS for ' +
      Math.round(duration) + ' ms';
  }
  function outputChannelGuardReady(channel) {
    var statusValue = channel && channel.protection_status || 'unknown';
    return !channel || !channel.protection_required ||
      statusValue === 'present' ||
      statusValue === 'software_guard_requested';
  }
  function outputRoleStatusText(channel) {
    if (!channel || channel.physical_output_index == null) return 'No DAC output assigned yet.';
    if (!channel.identity_verified) {
      return 'Confirm this wire before testing the driver.';
    }
    if (!outputChannelGuardReady(channel)) {
      return 'Confirmed. JTS will add the tweeter guard before any sound starts.';
    }
    if (channel.protection_required) {
      return channel.protection_status === 'present' ?
        'Confirmed. Extra protection noted; tests still start very quiet.' :
        'Confirmed. Continue to Test each driver; JTS will start it very quiet.';
    }
    return 'Confirmed. Continue to Test each driver when ready.';
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
  function outputStartupLoaded() {
    var startup = activeSpeaker.startupLoad || {};
    var state = startup.state || {};
    return state.status === 'loaded';
  }
  function quietTestStartupReady(load) {
    load = load || activeSpeaker.startupLoad || {};
    var state = load.state || {};
    return state.status === 'loaded' &&
      !!state.rollback_available &&
      state.current_config_matches_loaded !== false;
  }
  function quietTestStagedReady(staged) {
    staged = staged || activeSpeaker.stagedConfig || {};
    return staged.status === 'staged';
  }
  function activeOutputGroups(topology) {
    return outputGroups(topology).filter(function(group) {
      return group && (group.mode === 'active_2_way' || group.mode === 'active_3_way');
    });
  }
  function measurementSummary() {
    return activeSpeaker.measurements && activeSpeaker.measurements.summary || {};
  }
  function driverChecksComplete() {
    var summary = measurementSummary();
    return summary.driver_checks_complete === true ||
      summary.driver_measurements_complete === true;
  }
  function summedValidationComplete() {
    return measurementSummary().summed_validation_complete === true;
  }
  function baselineProfileApplied() {
    return activeSpeaker.baselineProfile && activeSpeaker.baselineProfile.status === 'applied';
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
      driverChecksComplete: driverChecksComplete(),
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
    return state === 'done' || state === 'active' || state === 'todo' ? state : '';
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
    return (outputStepOverride || defaultOutputStep()) === step;
  }
  function outputStepCanOpen(step, topology) {
    if (outputStepState(step, topology) !== 'todo') return true;
    // Dirty output remaps are saved from the map card itself.
    return step === 'map' && outputTopology.dirty && outputStepOverride === 'map';
  }
  function openOutputStep(step) {
    outputStepOverride = step;
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
  function renderDriverResearchStepFooter(topology) {
    if (outputTopology.dirty) {
      return '<button type="button" class="btn btn--primary" disabled>Save layout first</button>';
    }
    if (driverResearch.saving) {
      return '<button type="button" class="btn btn--primary" disabled>Saving</button>';
    }
    if (driverResearch.dirty || !driverResearchStepSatisfied()) {
      return '<button type="button" class="btn btn--primary" data-act="save-driver-design">Save values</button>';
    }
    if (activeCommissionGroup(topology) &&
        !crossoverPreviewReadyForProtectedStaging(crossoverPreview.payload)) {
      return '<button type="button" class="btn btn--primary" data-act="prepare-crossover-preview"' +
        (driverResearchHasPreviewInputs(topology) ? '' : ' disabled') +
        '>Preview crossover</button>';
    }
    return renderOutputStepButton('research', 'Continue', true);
  }
  function renderOutputMapStepFooter() {
    if (outputTopology.dirty) {
      return '<button type="button" class="btn btn--primary" data-act="save-output-topology">Save</button>';
    }
    if (!outputIdentityComplete()) {
      return '<button type="button" class="btn btn--primary" disabled>Confirm outputs</button>';
    }
    return renderOutputStepButton('map', 'Continue', true);
  }
  function outputTemplateKindFromAxes(layout, speakerMode) {
    if (layout !== 'mono' && layout !== 'stereo') return '';
    if (speakerMode !== 'passive' &&
        speakerMode !== 'active_2way' &&
        speakerMode !== 'active_3way') {
      return '';
    }
    return layout + '_' + speakerMode;
  }
  function outputTemplateIsActive(template) {
    return !!(template && template.id && template.id.indexOf('_active_') >= 0);
  }
  function outputTemplateActiveOutputNeed(template, hasSubwoofer) {
    return outputTemplateIsActive(template)
      ? Number(template.minOutputs || 0) + (hasSubwoofer ? 1 : 0)
      : 0;
  }
  function outputTemplateUnavailableReason(template, topology, hasSubwoofer) {
    if (!template) return 'Choose a supported speaker layout.';
    var mismatch = outputHardwareMismatch(topology);
    if (mismatch) {
      return mismatch.message + ' Reconnect the saved hardware or refresh after the attached hardware is stable.';
    }
    var hardware = outputHardware(topology);
    var physicalCount = Number(hardware && hardware.physical_output_count) || 0;
    if (physicalCount < template.minOutputs) {
      return template.label + ' needs at least ' + template.minOutputs +
        ' physical output' + (template.minOutputs === 1 ? '.' : 's.');
    }
    if (!outputTemplateIsActive(template)) return '';
    var route = outputActiveRoute() || {};
    var routeCount = Number(route.transport_channel_count) || 0;
    var needed = outputTemplateActiveOutputNeed(template, hasSubwoofer);
    if (routeCount > 0 && needed > routeCount) {
      return 'This install can test and apply up to ' + routeCount +
        ' active outputs right now; ' + template.label + ' needs ' + needed + '.';
    }
    if (hasSubwoofer && route.subwoofer_supported !== true) {
      return 'Subwoofer active profiles are not available on this install yet.';
    }
    return '';
  }
  function outputTemplateAxesForTopology(topology) {
    var mainGroups = outputGroups(topology).filter(function(group) {
      return group.kind !== 'subwoofer' && group.mode !== 'subwoofer';
    });
    if (!mainGroups.length) {
      return {
        layout: outputTemplateDraftAxes.layout || '',
        speakerMode: outputTemplateDraftAxes.speakerMode || ''
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
    // Dead-end: a layout is drafted but a LOCAL subwoofer can't be added here —
    // no spare physical output (the Apple-dongle case) or the active route is
    // not subwoofer-capable. Point the household at a wireless sub instead.
    var wirelessSubCta = hasLayout && !hasSub && (nextOutput == null || addIssue);
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
        (wirelessSubCta
          ? '<a class="btn btn--ghost btn--compact" href="/rooms/">' +
            escapeHtml('Wireless sub options') + '</a>'
          : '') +
      '</div>' +
    '</div>';
  }
  // Crossover (bass-management corner) control for the routed local subwoofer.
  // Fc persists onto the sub channel's crossover_fc_hz via the same topology save
  // POST the add/remove button uses (the household saves the draft to apply it).
  // Slope/filter mirror the emitter's fixed Linkwitz-Riley 24 dB/oct and are shown
  // read-only so the vocabulary matches the active-crossover card without exposing
  // a knob the backend ignores.
  function renderSubwooferCrossoverControl(topology) {
    var fc = subwooferCrossoverFcHz(topology);
    return '<div class="driver-settings driver-settings--crossovers">' +
      '<div class="driver-settings__row driver-settings__row--crossover">' +
        '<div class="driver-settings__pair">' +
          '<strong>' + escapeHtml('Subwoofer / mains') + '</strong>' +
          '<span>Bass-management crossover</span>' +
        '</div>' +
        '<label class="driver-research__field">' +
          '<span>Crossover point</span>' +
          '<input type="number" inputmode="numeric" min="' + escapeHtml(String(SUB_CROSSOVER_HZ_LO)) +
            '" max="' + escapeHtml(String(SUB_CROSSOVER_HZ_HI)) + '" step="1" ' +
            'data-sub-crossover-fc value="' +
            escapeHtml(fc == null ? '' : String(Math.round(Number(fc)))) +
            '" placeholder="' + escapeHtml(String(Math.round(DEFAULT_SUB_CROSSOVER_HZ))) + '"></label>' +
        '<label class="driver-research__field">' +
          '<span>Slope</span>' +
          '<input type="text" value="24 dB/oct" readonly aria-readonly="true"></label>' +
        '<label class="driver-research__field">' +
          '<span>Filter</span>' +
          '<input type="text" value="Linkwitz-Riley" readonly aria-readonly="true"></label>' +
      '</div>' +
      '<p class="setting-row__hint">' + escapeHtml(
        'Bass below ' + Math.round(Number(fc)) + ' Hz goes to the subwoofer; the mains get a ' +
        'matching high-pass at the same point. Save the draft to apply.'
      ) + '</p>' +
    '</div>';
  }
  function renderDriverResearchSummary(options) {
    options = options || {};
    var saved = driverResearch.designDraft || {};
    var savedStatus = saved.status || '';
    var topology = currentOutputTopology();
    var safetyRoles = driverSafetyNoteRoles(topology);
    var savedHtml =
      '<div class="driver-research__summary driver-research__summary--saved">' +
        '<span class="status-pill' + driverResearchWorkingStatusClass(savedStatus) + '">' +
          escapeHtml(driverResearchWorkingStatusLabel(savedStatus)) + '</span>' +
        '<p class="setting-row__hint">' + escapeHtml(workingSetupSummary(topology)) + '</p>' +
        (safetyRoles.length ? '<p class="setting-row__hint">' + escapeHtml(
          'Driver safety notes captured for ' + roleSentenceText(safetyRoles) + '.'
        ) + '</p>' : '') +
      '</div>';
    if (driverResearch.error) {
      return savedHtml +
        '<p class="setting-row__hint driver-research__error">' +
        escapeHtml(driverResearch.error) + '</p>';
    }
    if (options.savedOnly) return savedHtml;
    if (!driverResearch.parsed) {
      return savedHtml +
        '<p class="setting-row__hint">Paste JSON from the assistant to sanity-check the shape. JTS will not apply it automatically.</p>';
    }
    var summary = driverResearch.parsed;
    return savedHtml + '<div class="driver-research__summary">' +
      '<span class="status-pill status-pill--ready">import ready</span>' +
      '<p class="setting-row__hint">' + escapeHtml(
        'Imported driver notes for ' + roleSentenceText(summary.roles) +
        '. Review them before updating the working setup.'
      ) + '</p>' +
      (summary.warnings.length ? '<div class="driver-research__notes">' +
        '<p class="setting-row__title">Review notes</p>' +
        '<ul>' + summary.warnings.map(function(warning) {
          return '<li>' + escapeHtml(String(warning)) + '</li>';
        }).join('') + '</ul></div>' : '') +
    '</div>';
  }
  function renderManualDriverSettings(topology) {
    var roles = driverResearchRoles(topology);
    return '<div class="driver-settings">' + roles.map(function(role) {
      var setting = driverSetting(role);
      return '<div class="driver-settings__row">' +
        '<label class="driver-research__field">' +
          '<span>' + escapeHtml(driverResearchRoleLabel(role)) + '</span>' +
          '<input type="text" data-driver-field="' + escapeHtml(role) + '" value="' +
            escapeHtml(driverResearch.inputs[role] || '') + '" placeholder="Manufacturer and model">' +
        '</label>' +
        '<label class="driver-research__field">' +
          '<span>Sensitivity</span>' +
          '<input type="number" inputmode="decimal" data-manual-driver="' + escapeHtml(role) + '" ' +
            'data-manual-field="sensitivity_db_2v83_1m" value="' +
            escapeHtml(setting.sensitivity_db_2v83_1m == null ? '' : String(setting.sensitivity_db_2v83_1m)) +
            '" placeholder="dB">' +
        '</label>' +
        '<label class="driver-research__field">' +
          '<span>Safe low limit</span>' +
          '<input type="number" inputmode="numeric" min="1" data-manual-driver="' + escapeHtml(role) + '" ' +
            'data-manual-field="do_not_test_below_hz" value="' +
            escapeHtml(setting.do_not_test_below_hz == null ? '' : String(setting.do_not_test_below_hz)) +
            '" placeholder="Hz">' +
        '</label>' +
        '<label class="driver-research__field">' +
          '<span>Level trim</span>' +
          '<input type="number" inputmode="decimal" data-manual-driver="' + escapeHtml(role) + '" ' +
            'data-manual-field="gain_offset_db" value="' +
            escapeHtml(setting.gain_offset_db == null ? '' : String(setting.gain_offset_db)) +
            '" placeholder="dB">' +
        '</label>' +
      '</div>';
    }).join('') + '</div>';
  }
  function renderManualCrossoverSettings(topology) {
    var pairs = activeCrossoverPairs(topology);
    if (!pairs.length) {
      return '<p class="setting-row__hint">This speaker layout does not need an active crossover point.</p>';
    }
    return '<div class="driver-settings driver-settings--crossovers">' + pairs.map(function(pair) {
      var setting = crossoverSetting(pair);
      var key = crossoverSettingKey(pair);
      return '<div class="driver-settings__row driver-settings__row--crossover">' +
        '<div class="driver-settings__pair">' +
          '<strong>' + escapeHtml(humanRole(pair[0]) + ' / ' + humanRole(pair[1])) + '</strong>' +
          '<span>Starting crossover</span>' +
        '</div>' +
        '<label class="driver-research__field">' +
          '<span>Crossover point</span>' +
          '<input type="number" inputmode="numeric" min="1" data-manual-crossover="' + escapeHtml(key) + '" ' +
            'data-manual-field="frequency_hz" value="' +
            escapeHtml(setting.frequency_hz == null ? '' : String(setting.frequency_hz)) +
            '" placeholder="Hz">' +
        '</label>' +
        '<label class="driver-research__field">' +
          '<span>Slope</span>' +
          '<input type="number" inputmode="numeric" min="6" step="6" data-manual-crossover="' + escapeHtml(key) + '" ' +
            'data-manual-field="slope_db_per_octave" value="' +
            escapeHtml(setting.slope_db_per_octave == null ? '24' : String(setting.slope_db_per_octave)) +
            '" placeholder="24">' +
        '</label>' +
        '<label class="driver-research__field">' +
          '<span>Filter</span>' +
          '<select data-manual-crossover="' + escapeHtml(key) + '" data-manual-field="filter_type">' +
            ['Linkwitz-Riley', 'Butterworth'].map(function(value) {
              return '<option value="' + escapeHtml(value) + '"' +
                ((setting.filter_type || 'Linkwitz-Riley') === value ? ' selected' : '') +
                '>' + escapeHtml(value) + '</option>';
            }).join('') +
          '</select>' +
        '</label>' +
      '</div>';
    }).join('') + '</div>';
  }
  function renderDriverResearchAiHelper(topology) {
    var promptReady = driverResearchPromptReady(topology);
    var promptSelected = driverResearch.promptSelected && !driverResearch.promptCopied;
    var promptClass = 'driver-research__textarea' +
      (promptSelected ? ' driver-research__textarea--compact' : ' driver-research__textarea--hidden');
    var promptButtonLabel = driverResearch.promptCopied ? 'Copied' :
      (promptSelected ? 'Selected' : 'Copy prompt');
    return '<details class="driver-research__ai" open>' +
      '<summary>AI helper</summary>' +
      '<div class="driver-research__grid driver-research__grid--ai">' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<div><p class="setting-row__title">Research prompt</p>' +
              '<p class="setting-row__hint">Enter driver models first, then copy the prompt.</p></div>' +
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
            '<p class="setting-row__title">Paste JSON result</p>' +
            '<div class="driver-research__actions">' +
              '<button type="button" class="btn btn--ghost" data-act="parse-driver-research">Use values</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="driver-research-import" class="driver-research__textarea driver-research__textarea--compact" data-driver-import ' +
            'rows="4" placeholder="{...}" aria-label="Driver research JSON result">' +
            escapeHtml(driverResearch.importText || '') + '</textarea>' +
          '<div id="driver-research-import-summary">' + renderDriverResearchSummary() + '</div>' +
        '</div>' +
      '</div>' +
    '</details>';
  }
  function renderDriverResearchCard(topology) {
    var saveDisabled = driverResearch.saving || outputTopology.dirty ||
      !currentOutputTopology();
    return '<div class="output-card output-card--driver-research">' +
      '<div class="output-card__head"><div><p class="output-card__title">Working setup</p>' +
        '<p class="setting-row__hint">Enter the values JTS should use for the no-audio crossover preview.</p></div></div>' +
      renderDriverResearchAiHelper(topology) +
      '<div class="driver-research__section">' +
        '<p class="setting-row__title">Drivers</p>' +
        renderManualDriverSettings(topology) +
      '</div>' +
      '<div class="driver-research__section">' +
        '<p class="setting-row__title">Crossover points</p>' +
        renderManualCrossoverSettings(topology) +
      '</div>' +
      '<label class="driver-research__field driver-research__field--wide">' +
        '<span>Build notes</span>' +
        '<textarea rows="3" data-driver-field="notes" placeholder="Waveguide, baffle, enclosure, amplifier, measurement constraints">' +
          escapeHtml(driverResearch.inputs.notes || '') + '</textarea>' +
      '</label>' +
      '<div class="driver-research__actions driver-research__actions--save">' +
        '<button type="button" class="btn btn--ghost" data-act="save-driver-design"' +
          (saveDisabled ? ' disabled' : '') + '>' +
          escapeHtml(driverResearch.saving ? 'Saving' : 'Save values') +
        '</button>' +
      '</div>' +
      '<div class="driver-research__saved-summary">' + renderDriverResearchSummary({savedOnly: true}) + '</div>' +
    '</div>';
  }
  function previewStatusClass(value) {
    if (value === 'preview ready' || value === 'ready_for_protected_staging') {
      return ' status-pill--ready';
    }
    if (value === 'stale' || value === 'unreadable') return ' status-pill--blocked';
    return '';
  }
  function crossoverPreviewReadyCount(payload) {
    var summary = payload && payload.summary || {};
    var ready = Number(summary.ready_crossover_count || 0);
    if (ready > 0) return ready;
    var count = 0;
    (Array.isArray(payload && payload.groups) ? payload.groups : []).forEach(function(group) {
      (Array.isArray(group.crossovers) ? group.crossovers : []).forEach(function(crossover) {
        if (crossover.status === 'ready_for_review') count += 1;
      });
    });
    return count;
  }
  function crossoverPreviewDisplayStatus(payload) {
    payload = payload || {};
    var raw = payload.status || 'not_prepared';
    if (crossoverPreviewReadyCount(payload) > 0) return 'preview ready';
    if (raw === 'ready_for_protected_staging') return 'preview ready';
    if (raw === 'blocked') return 'not ready yet';
    if (raw === 'stale') return 'needs refresh';
    if (raw === 'not_applicable') return 'not needed';
    return 'not prepared';
  }
  function crossoverPreviewReviewIssues(issues) {
    return (Array.isArray(issues) ? issues : []).filter(function(issue) {
      return issue && issue.severity === 'warning';
    });
  }
  function renderIssueList(issues, maxItems) {
    issues = Array.isArray(issues) ? issues : [];
    if (!issues.length) return '';
    return '<ul class="active-speaker-issues">' + issues.slice(0, maxItems || 5).map(function(issue) {
      var severity = issue && issue.severity === 'warning' ? 'warning' : 'blocker';
      return '<li class="active-speaker-issue active-speaker-issue--' + escapeHtml(severity) + '">' +
        escapeHtml((issue && (issue.message || issue.code)) || 'review required') +
      '</li>';
    }).join('') + '</ul>';
  }
  function renderPreviewIssues(issues) {
    return renderIssueList(issues, 5);
  }
  function summedTestRetryHint(issues) {
    issues = Array.isArray(issues) ? issues : [];
    if (!issues.length) return '';
    var codes = issues.map(function(issue) {
      return String(issue && issue.code || '');
    });
    if (codes.indexOf('tone_backend_failed') >= 0) {
      return 'JTS could not prepare the combined test audio. Retry after the setup finishes; if it fails again, open System status.';
    }
    if (codes.indexOf('summed_commission_load_failed') >= 0 ||
        codes.indexOf('safe_session_not_armed') >= 0) {
      return 'JTS could not open the quiet combined-test path. Press Play combined test to retry.';
    }
    if (codes.indexOf('summed_test_artifact_missing') >= 0 ||
        codes.indexOf('summed_test_playback_incomplete') >= 0) {
      return 'The combined test did not finish. Press Play combined test to retry.';
    }
    if (codes.indexOf('summed_test_output_mismatch') >= 0) {
      return 'The last combined test did not match the saved speaker outputs. Press Play combined test to retry; if it fails again, re-check Confirm outputs.';
    }
    return 'The last combined test did not play. Press Play combined test to try again.';
  }
  function renderCrossoverPreviewRows(payload) {
    var groups = Array.isArray(payload.groups) ? payload.groups : [];
    var rows = [];
    groups.forEach(function(group) {
      (Array.isArray(group.crossovers) ? group.crossovers : []).forEach(function(crossover) {
        var roles = Array.isArray(crossover.between_roles) ? crossover.between_roles : [];
        var filter = (Array.isArray(crossover.filters) && crossover.filters[0]) || {};
        var label = (group.label || group.group_id || 'Speaker') + ': ' + roles.join(' / ');
        var detail = crossover.proposed_frequency_hz ?
          fmtFreq(crossover.proposed_frequency_hz) + ', ' +
          (filter.filter_type || 'filter') + ', ' +
          String(filter.slope_db_per_octave || 24) + ' dB/oct' :
          'needs research';
        rows.push('<div><dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(detail) + '</dd></div>');
      });
    });
    if (!rows.length) {
      rows.push('<div><dt>Preview</dt><dd>No active crossover candidate prepared yet.</dd></div>');
    }
    return '<dl class="active-speaker-facts output-facts">' + rows.join('') + '</dl>';
  }
  function renderCrossoverPreviewCard() {
    var payload = crossoverPreview.payload || {};
    var label = crossoverPreviewDisplayStatus(payload);
    var summary = payload.summary || {};
    var readyCount = crossoverPreviewReadyCount(payload);
    var warningIssues = crossoverPreviewReviewIssues(payload.issues);
    var laterSafetyCount = Math.max(0, Number(summary.blocker_count || 0));
    var topology = currentOutputTopology();
    var hasPreviewInputs = driverResearchHasPreviewInputs(topology);
    var canPrepare = hasPreviewInputs && !outputTopology.dirty && !driverResearch.saving;
    var disabled = crossoverPreview.preparing || !canPrepare;
    var hint = canPrepare ?
      'Updates the working setup, then builds a no-audio crossover preview.' :
      (driverResearch.saving
        ? 'Working setup is updating before the preview.'
        : (outputTopology.dirty
        ? 'Save the speaker layout before preparing a crossover preview.'
        : driverResearchMissingPreviewMessage(topology)));
    if (crossoverPreview.error) hint = crossoverPreview.error;
    return '<div class="output-card output-card--crossover-preview">' +
      '<div class="output-card__head"><div><p class="output-card__title">Crossover preview</p>' +
        '<p class="setting-row__hint">' + escapeHtml(hint) + '</p></div>' +
        '<span class="status-pill' + previewStatusClass(label) + '">' + escapeHtml(label) + '</span></div>' +
      renderCrossoverPreviewRows(payload) +
      '<p class="setting-row__hint">' + escapeHtml(
        (readyCount > 0 ? 'Ready to preview ' + String(readyCount) +
        ' crossover split' + (readyCount === 1 ? '' : 's') + '. ' :
        'Needs crossover info. ') +
        String(warningIssues.length) + ' review note' +
        (warningIssues.length === 1 ? '' : 's') + '. No filters are active yet.' +
        (laterSafetyCount ? ' JTS still checks the setup before any sound.' : '')
      ) + '</p>' +
      renderPreviewIssues(warningIssues) +
      '<button type="button" class="btn btn--ghost" data-act="prepare-crossover-preview"' +
        (disabled ? ' disabled' : '') + '>' +
        escapeHtml(crossoverPreview.preparing ? 'Preparing' : 'Preview crossover') +
      '</button>' +
    '</div>';
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
    var evaluation = outputEvaluation(topology);
    var layoutStatusValue = outputTopology.dirty ? 'draft' : 'layout ready';
    // Active 2/3-way groups commission through the protected crossover/limiter
    // graph; passive/full-range groups use the normal listening path.
    var safetyActive = !!activeCommissionGroup(topology);
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
        'Add driver and crossover values',
        outputStepHint('research', 'Set driver names, trims, and crossover points.'),
        topology,
        renderDriverResearchCard(topology) +
          renderCrossoverPreviewCard(),
        renderDriverResearchStepFooter(topology)
      ) +
      renderOutputStepCard(
        'map',
        'Confirm outputs',
        outputStepHint('map', 'Assign DAC channels, then play each driver quietly.'),
        topology,
        renderOutputStageCard(topology) +
          renderOutputGroupsCard(topology) +
          renderOutputIdentityCard(),
        renderOutputMapStepFooter()
      ) +
      renderOutputStepCard(
        'safety',
        'Test each driver',
        outputStepHint('safety', safetyActive
          ? 'Start one driver at a time through the real crossover and limiter.'
          : 'Per-driver audible commissioning is for active crossover layouts.'),
        topology,
        safetyActive ? renderCommissionCard() : renderActiveCommissionOnlyCard(),
        ''
      ) +
      renderOutputStepCard(
        'profile',
        'Validate and apply',
        outputStepHint('profile', 'Test the combined speaker, then save and apply.'),
        topology,
        renderSummedValidationCard(topology) +
        renderBaselineProfileCard(),
        ''
      ) +
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
    var mismatchCard = mismatch ? (
      '<div class="output-card output-card--hardware">' +
        '<div class="output-card__head">' +
          '<div><p class="output-card__title">Hardware mismatch</p>' +
          '<p class="setting-row__hint">' + escapeHtml(mismatch.message) + '</p></div>' +
          '<span class="status-pill status-pill--blocked">blocked</span>' +
        '</div>' +
        '<p class="setting-row__hint">Reconnect the saved hardware or reconfigure the speaker layout after the attached hardware is stable. JTS keeps the saved topology intact.</p>' +
      '</div>'
    ) : '';
    return mismatchCard + savedCard;
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
        var model = (driverResearch.inputs && driverResearch.inputs[channel.role]) || '';
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
    if (tonePlaying || toneStarting) {
      return '<button type="button" class="btn btn--danger btn--compact output-role__action" ' +
        'data-act="commission-abort">' +
        escapeHtml(tonePlaying ? 'Stop' : 'Starting') + '</button>';
    }
    return '<button type="button" class="btn btn--ghost btn--compact output-role__action" ' +
      'data-act="commission-step" data-identity-audition="true" ' +
      'data-role="' + escapeHtml(role) + '"' +
      (disabled ? ' disabled' : '') + '>Play</button>';
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
    return '<div class="output-card output-card--groups">' +
      '<div class="output-card__head"><div><p class="output-card__title">DAC output assignments</p>' +
        '<p class="setting-row__hint">Assign each driver to one DAC channel. Play starts quiet and ramps.</p></div>' +
        '<span class="status-pill' + (outputTopology.dirty ? '' : ' status-pill--ready') + '">' +
          escapeHtml(outputTopology.dirty ? 'draft' : 'saved') + '</span></div>' +
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
        var model = (driverResearch.inputs && driverResearch.inputs[channel.role]) || '';
        var hardwareLabel = (group.label || group.id) + ' · ' + humanRole(channel.role) +
          (model ? ' · ' + model : '');
        return '<div class="output-role">' +
          '<div class="output-role__text">' +
            '<span>' + escapeHtml(label) + '</span>' +
            '<strong>' + escapeHtml(hardwareLabel) + '</strong>' +
            '<small>' + escapeHtml(outputRoleStatusText(channel)) + '</small>' +
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
    var targets = Array.isArray(report.targets) ? report.targets : [];
    var rows = targets.length ? targets.map(function(target) {
      return '<li class="output-identity-row">' +
        '<span>' + escapeHtml(target.speaker_label || target.speaker_group_id || 'Speaker') +
          ' · ' + escapeHtml(humanRole(target.role)) + '</span>' +
        '<strong>' + escapeHtml(target.identity_verified ? 'Confirmed' :
          (target.assigned ? 'Needs confirmation' : 'Unassigned')) + '</strong>' +
      '</li>';
    }).join('') : '<li class="output-identity-row"><span>No channels configured</span><strong>Draft</strong></li>';
    return '<div class="output-card output-card--identity">' +
      '<div class="output-card__head"><div><p class="output-card__title">Confirmation progress</p>' +
        '<p class="setting-row__hint">Play a quiet ramp if needed, then confirm each DAC output.</p></div>' +
        '<span class="status-pill' + (unverified === 0 && assigned > 0 ? ' status-pill--ready' : '') + '">' +
          escapeHtml(verified + '/' + assigned + ' confirmed') + '</span></div>' +
      (outputTopology.dirty ? '<p class="setting-row__hint">Save the draft before changing confirmed outputs.</p>' : '') +
      '<ul class="output-identity-list">' + rows + '</ul>' +
      '<p class="setting-row__hint">' + escapeHtml(
        unverified > 0 ? 'Confirm the assigned outputs above to continue.' :
          (report.next_step || 'Outputs are confirmed. Continue when you are ready.')
      ) + '</p>' +
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
  function driverCheckRolesForGroup(group) {
    if (!group || !Array.isArray(group.channels)) return [];
    return group.channels.map(function(channel) {
      return channel && channel.role;
    }).filter(function(role) {
      return role && driverMeasurementCaptured(group.id, role);
    });
  }
  function driverMeasurementCounts() {
    var summary = measurementSummary();
    return {
      captured: Number(summary.captured_driver_count || 0),
      required: Number(summary.required_driver_count || 0)
    };
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
        'Choose a careful level. You can adjust it while the test audio plays.');
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
  function renderSummedValidationCard(topology) {
    var groups = activeOutputGroups(topology);
    if (!groups.length) return '';
    var canRecord = driverChecksComplete();
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
      var combinedControlsLocked = combinedStarting || combinedStopping || combinedSaving;
      var combinedPlaybackActive = combinedStarting || combinedPlaying || combinedStopping;
      var testButton;
      if (combinedPlaying) {
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
      if (combinedPlaying) recordEnabled = true;
      var summedTestId =
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
          'Test each driver first, then test the combined speaker.'));
      var latestTestIssues = latestTest && Array.isArray(latestTest.issues)
        ? latestTest.issues : [];
      var retryHint = !hasAudibleTest ?
        (groupView && groupView.failure_message || summedTestRetryHint(latestTestIssues)) :
        '';
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
          live: combinedPlaying
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
          'Test each driver first, then validate the combined crossover.') + '</p></div>' +
        '<span class="status-pill' + (summedValidationComplete() ? ' status-pill--ready' : '') + '">' +
          escapeHtml(summedValidationComplete() ? 'ready' : (revalidationNeedsCombined ? 'recheck' : (canRecord ? 'next' : 'after driver checks'))) + '</span></div>' +
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
    var badge = summary.provisional ? ' status-pill' : ' status-pill status-pill--ready';
    return '<div class="active-speaker-level-match">' +
      '<div class="output-card__head"><div>' +
        '<p class="setting-row__title">Driver levels</p>' +
        '<p class="setting-row__hint">' + escapeHtml(summary.note) + '</p></div>' +
        '<span class="' + badge + '">' +
          escapeHtml(summary.provisional ? 'estimate' : 'measured') + '</span></div>' +
      '<dl class="active-speaker-facts">' + rows + '</dl>' +
      (summary.provisional ?
        '<p class="setting-row__hint">' + escapeHtml(summary.guidance) + '</p>' : '') +
    '</div>';
  }
  function renderBaselineProfileCard() {
    var profile = activeSpeaker.baselineProfile || {};
    var statusValue = profile.status || 'not_saved';
    var config = profile.config || {};
    var permissions = profile.permissions || {};
    var applied = statusValue === 'applied';
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
    var actions = (applied || applyBlocked) ? '' :
      '<div class="active-speaker-actions active-speaker-profile-actions">' +
        '<button type="button" class="btn btn--primary' +
          '" data-act="save-apply-baseline-profile"' +
          ((busy || !canFinish) ? ' disabled' : '') + '>' +
          escapeHtml(actionLabel) + '</button>' +
      '</div>';
    return '<div class="output-card output-card--baseline-profile">' +
      '<div class="output-card__head"><div><p class="output-card__title">Active speaker profile</p>' +
        '<p class="setting-row__hint">Your active speaker profile, built from the checked crossover and driver checks.</p></div>' +
        '<span class="status-pill' + (applied || readyToApply ? ' status-pill--ready' : '') + '">' +
          escapeHtml(applied ? 'active' : (readyToApply ? 'saved' : (applyBlocked ? 'blocked' : (revalidating ? 'recheck' : 'not saved')))) + '</span></div>' +
      body +
      renderLevelMatchSummary(profile) +
      (issueRows && mayCompile ? '<ul class="active-speaker-issues active-speaker-issues--warning">' + issueRows + '</ul>' : '') +
      actions +
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
  function freqToSlider(freq, min, max) {
    var lmin = Math.log(min), lmax = Math.log(max);
    return Math.round((Math.log(clamp(freq, min, max)) - lmin) / (lmax - lmin) * 1000);
  }
  function sliderToFreq(pos, min, max) {
    var lmin = Math.log(min), lmax = Math.log(max);
    return Math.exp(lmin + clamp(pos, 0, 1000) / 1000 * (lmax - lmin));
  }
  function bandRow(band, index) {
    var open = !allCollapsed && index === activeBand;
    var type = band.type || 'Peaking';
    var shelf = type === 'Lowshelf' || type === 'Highshelf';
    var gainless = GAINLESS_TYPES.indexOf(type) >= 0;
    var body = '';
    if (open) {
      // Gain hidden for cut/notch (no gain term); Width hidden for shelves
      // (CamillaDSP fixes their slope at 6 dB/oct — the control would be inert).
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
        '<button type="button" class="segmented__btn" data-mode="simple" aria-pressed="' + (mode === 'simple' ? 'true' : 'false') + '">Simple</button>' +
        '<button type="button" class="segmented__btn" data-mode="peq" aria-pressed="' + (mode === 'peq' ? 'true' : 'false') + '">PEQ</button>' +
      '</div></section>';

    var bandsContent;
    if (mode === 'simple') {
      var cols = (simpleBands.length ? simpleBands : []).map(function(slot, i) {
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
    var activeCount = mode === 'simple'
      ? Object.keys(draft.simple_eq).filter(function(k) {
        return Math.abs(draft.simple_eq[k]) >= ACTIVE_GAIN_EPSILON_DB;
      }).length
      : draft.parametric_bands.filter(function(b) { return b.enabled !== false; }).length;
    var bandsSection = '<section class="bands-section"><div class="row-between">' +
      '<h2 class="eyebrow">Bands</h2>' +
      '<div class="bands-meta"><span id="active-count">' + activeCount + ' active</span>' +
      (mode === 'peq' ? '<button type="button" class="text-button text-button--muted" data-act="toggle-collapse">' +
        (allCollapsed ? 'Expand all' : 'Collapse all') + '</button>' : '') +
      '</div></div>' + renderSubwooferCrossoverCallout() + bandsContent + '</section>';

    el('view-body').innerHTML = '<div>' + modeSection + bandsSection +
      '<section class="draft-footer">' + footerHtml() + '</section></div>';
  }
  // Called-out, NON-editable band showing the system-managed bass-management
  // high-pass a routed local subwoofer applies to the mains. The household must
  // SEE that a subwoofer high-pass at N Hz shapes the mains; it is edited via the
  // subwoofer card (output setup), never in this PEQ list — so it has no Type or
  // Delete controls. Returns '' when no local sub is routed. Pure-helper decision
  // (does a sub exist? what Fc? the synthetic band) lives in active-speaker-ui.js.
  function renderSubwooferCrossoverCallout() {
    var band = subwooferCrossoverBand(currentOutputTopology());
    if (!band) return '';
    return '<div class="bands-card bands-card--system">' +
      '<div class="band-row band-row--system" data-open="false">' +
        '<div class="band-row__header band-row__header--system">' +
          '<span class="band-row__title">' +
            '<span class="band-dot band-dot--system">' + ico('wave') + '</span>' +
            '<span><p class="band-row__name">' + escapeHtml(String(band.label)) + '</p>' +
              '<p class="band-row__meta">' + escapeHtml(
                String(band.type) + ' · ' + Math.round(Number(band.freq_hz)) + ' Hz · system-managed'
              ) + '</p></span>' +
          '</span>' +
          '<span class="status-pill">' + escapeHtml('locked') + '</span>' +
        '</div>' +
        '<p class="setting-row__hint">' + escapeHtml(
          String(band.detail) + '. Change it on the subwoofer card under speaker setup.'
        ) + '</p>' +
      '</div>' +
    '</div>';
  }
  function footerHtml() {
    if (naming) {
      var isRename = nameMode === 'rename';
      return '<div class="naming-card">' +
        '<label class="eyebrow">' + (isRename ? 'Rename profile' : 'Name your profile') + '</label>' +
        '<input type="text" id="name-input" maxlength="48" autocomplete="off" value="' + escapeHtml(nameDraft) + '">' +
        '<div class="btn-row">' +
          '<button type="button" class="btn btn--primary" data-act="finalize-name">' +
            (isRename ? 'Rename' : 'Save profile') + '</button>' +
          '<button type="button" class="btn btn--ghost" data-act="cancel-name">Cancel</button>' +
        '</div></div>';
    }
    var dirty = draftModified();
    if (editing.kind === 'user') {
      return '<div class="btn-row">' +
          '<button type="button" class="btn btn--primary" data-act="overwrite" data-dirty-action' + (dirty ? '' : ' disabled') + '>Overwrite</button>' +
          '<button type="button" class="btn btn--ghost" data-act="begin-name">Save as new</button></div>' +
        '<div class="btn-row">' +
          '<button type="button" class="btn btn--ghost" data-act="begin-rename">Rename</button>' +
          '<button type="button" class="btn btn--ghost" data-act="reset-draft" data-dirty-action' +
            (dirty ? '' : ' disabled') + '>Reset draft</button></div>';
    }
    if (editing.kind === 'preset') {
      return '<div class="btn-row">' +
          '<button type="button" class="btn btn--primary" data-act="begin-name">Save as new</button>' +
          '<button type="button" class="btn btn--ghost" data-act="reset-draft" data-dirty-action' + (dirty ? '' : ' disabled') + '>Reset draft</button></div>';
    }
    return '<div class="btn-row">' +
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
          status(payload.message || 'Sound EQ is unavailable for this speaker setup.', true);
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
    if (view === 'off') {
      return applyProfile(Object.assign(normalizeProfile(applied), {enabled: false}), options.okMsg, seq);
    }
    if (view === 'saved') {
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
        if (sourceSeq === liveSourceSeq) status(payload.message || 'Sound EQ is unavailable for this speaker setup.', true);
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
      if (payload.profile_library) library = payload.profile_library;
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
    var prev = soundSettings;
    soundSettings = Object.assign({}, soundSettings, patch);
    try {
      var resp = await fetch('./settings', {method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify(soundSettings)});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'settings failed');
      ingestState(payload);
      if (payload.warning) status(payload.warning, true);
      else if (payload.volume_warning) status(payload.volume_warning, true);
      return true;
    } catch (e) {
      soundSettings = prev;
      status('Could not save sound settings: ' + e.message, true);
      render();
      return false;
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
      var resp = await fetch('./volume-floor/audition', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({volume_floor_db: value})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'tone failed');
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
    var floor = -50;
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
      volumeFloorDraftDb = null;
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
      var resp = await fetch('./volume-floor/stop', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({reason: options.reason || 'stop'}),
        keepalive: !!options.keepalive
      });
      if (!options.quiet) {
        var payload = await resp.json();
        if (!resp.ok) throw new Error(payload.error || 'stop failed');
        status('Volume-floor tone stopped.');
      }
    } catch (e) {
      if (!options.quiet) status('Could not stop volume-floor tone: ' + e.message, true);
    }
  }

  function ingestState(payload) {
    limits = Object.assign({}, LIMIT_DEFAULTS, payload.limits || {});
    simpleBands = limits.simple_bands || [];
    if (payload.curves) { curvesById = {}; payload.curves.forEach(function(c) { curvesById[c.id] = c; }); }
    if (payload.profile_library) library = payload.profile_library;
    if (payload.dsp_write_epoch) dspWriteEpoch = payload.dsp_write_epoch;
    if (payload.sound_settings) soundSettings = payload.sound_settings;
    applied = normalizeProfile(payload.profile || {});
  }

  // ---- tab + edit transitions ----------------------------------------
  function setView(v) {
    view = v;
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
    selectedId = id;
    render();
    requestLiveSource({immediate: true});
  }
  function newDraft() {
    draft = FLAT(); editing = {kind: 'new'}; mode = 'simple'; activeBand = 0; naming = false;
    view = 'draft'; status(''); render(); requestLiveSource({immediate: true});
  }
  function editEntry(id) {
    var entry = entryById(id);
    if (!entry) return;
    draft = normalizeProfile(entry.profile);
    editing = {kind: entry.kind === 'custom' ? 'user' : 'preset', id: entry.id, name: entry.name};
    mode = draft.parametric_bands.length ? 'peq' : 'simple';
    activeBand = 0; naming = false; view = 'draft';
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
    var n = mode === 'simple'
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
  if (!followerMode) {
    ['off', 'saved', 'draft'].forEach(function(v) {
      el('tab-' + v).addEventListener('click', function() { if (view !== v) setView(v); });
    });
  }
  el('back').addEventListener('click', function(e) { e.preventDefault(); window.location.href = '/'; });

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
    else if (act === 'toggle-band') { activeBand = (activeBand === index && !allCollapsed) ? -1 : index; allCollapsed = false; renderDraft(); renderLiveGraph(); }
    else if (act === 'toggle-collapse') { allCollapsed = !allCollapsed; renderDraft(); }
    else if (act === 'begin-name') { naming = true; nameMode = 'save'; nameDraft = defaultName(); renderDraft(); focusNameInput(); }
    else if (act === 'begin-rename') { naming = true; nameMode = 'rename'; nameDraft = editing.name || ''; renderDraft(); focusNameInput(); }
    else if (act === 'cancel-name') { naming = false; renderDraft(); }
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
    else if (act === 'commission-step') {
      startCommissionAutoRamp(t.getAttribute('data-role') || '', {
        confirm: false,
        identityAudition: t.getAttribute('data-identity-audition') === 'true'
      });
    }
    else if (act === 'commission-ack') { commissionAck(t.getAttribute('data-outcome') || ''); }
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
        activeBand = bi;
        onDraftChanged(true);
      }
    }
  });
  el('view-body').addEventListener('input', function(ev) {
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-field')) {
      var driverField = ev.target.getAttribute('data-driver-field');
      driverResearch.inputs[driverField] = ev.target.value;
      driverResearch.error = '';
      driverResearch.dirty = true;
      driverResearch.promptCopied = false;
      driverResearch.promptSelected = false;
      updateDriverResearchPromptPreview();
      updateDriverResearchPromptButton();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-import')) {
      driverResearch.importText = ev.target.value;
      driverResearch.error = '';
      driverResearch.parsed = null;
      driverResearch.dirty = true;
      updateDriverResearchImportSummary();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-manual-driver')) {
      setManualDriverField(
        ev.target.getAttribute('data-manual-driver') || '',
        ev.target.getAttribute('data-manual-field') || '',
        ev.target.value
      );
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-manual-crossover')) {
      setManualCrossoverField(
        ev.target.getAttribute('data-manual-crossover') || '',
        ev.target.getAttribute('data-manual-field') || '',
        ev.target.value
      );
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
    if (field) {
      draft.simple_eq[field] = clamp(ev.target.value, -limits.simple_gain_db, limits.simple_gain_db);
      var readout = el('view-body').querySelector('[data-readout-field="' + field + '"]');
      if (readout) readout.textContent = fmtDb(draft.simple_eq[field]);
      positionThumb(ev.target);
      refreshActiveCount();
      refreshDraftActionState();
      schedulePreview(); requestLiveSource({immediate: false});
    } else if (range) {
      var row = ev.target.closest('.band-row');
      var bi = Number(row.getAttribute('data-index'));
      var band = draft.parametric_bands[bi];
      if (!band) return;
      activeBand = bi;
      if (range === 'freq') band.freq_hz = sliderToFreq(ev.target.value, limits.min_freq_hz, limits.max_freq_hz);
      if (range === 'gain') band.gain_db = clamp(ev.target.value, -limits.advanced_gain_db, limits.advanced_gain_db);
      if (range === 'q') band.q = clamp(ev.target.value, limits.min_q, bandQMax(band.type));
      var ro = row.querySelector('[data-readout="' + range + '"]');
      if (ro) ro.textContent = range === 'freq' ? fmtFreq(band.freq_hz) : (range === 'gain' ? fmtDb(band.gain_db) + ' dB' : fmtQ(band.q));
      positionThumb(ev.target);
      refreshDraftActionState();
      schedulePreview(); requestLiveSource({immediate: false});
    }
  });
  el('view-body').addEventListener('input', function(ev) {
    if (ev.target.id === 'name-input') { nameDraft = ev.target.value; return; }
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
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-manual-crossover')) {
      setManualCrossoverField(
        ev.target.getAttribute('data-manual-crossover') || '',
        ev.target.getAttribute('data-manual-field') || '',
        ev.target.value
      );
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
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-sub-crossover-fc')) {
      // Commit on change (not every keystroke) — setOutputDraft re-renders.
      setSubwooferCrossoverFc(ev.target.value);
      return;
    }
    if (ev.target.id === 'set-match-loudness') saveSettings({match_loudness: ev.target.checked});
    else if (ev.target.id === 'set-headroom') saveSettings({headroom_trim_db: Number(ev.target.value)});
    else if (ev.target.id === 'set-volume-floor') {
      var floor = Number(ev.target.value);
      setVolumeFloorDraft(floor);
      if (volumeFloorTone.active) scheduleVolumeFloorToneUpdate(volumeFloorValue(), {immediate: true});
    }
  });
  el('view-body').addEventListener('toggle', function(ev) {
    if (ev.target && ev.target.matches && ev.target.matches('[data-active-speaker-setup]')) {
      activeSpeakerSetupOpen = !!ev.target.open;
      return;
    }
    if (ev.target && ev.target.classList && ev.target.classList.contains('output-step') &&
        ev.target.open) {
      var step = ev.target.getAttribute('data-output-step') || outputStepOverride;
      var topology = currentOutputTopology();
      if (!outputStepCanOpen(step, topology)) {
        ev.target.open = false;
        outputStepOverride = defaultOutputStep();
        status('Finish the current card before opening ' + outputStepTitle(step) + '.', true);
        render();
        return;
      }
      outputStepOverride = step;
      el('view-body').querySelectorAll('.output-step[open]').forEach(function(stepEl) {
        if (stepEl !== ev.target) stepEl.open = false;
      });
    }
  }, true);
  el('view-body').addEventListener('keydown', function(ev) {
    if (ev.target.id !== 'name-input') return;
    if (ev.key === 'Enter') { ev.preventDefault(); finalizeName(); }
    else if (ev.key === 'Escape') { ev.preventDefault(); naming = false; renderDraft(); }
  });

  function switchMode(next) {
    if (next === mode) return;
    if (next === 'simple') {
      // Snap to the simple template, copying nearest gain by log-frequency.
      var newSimple = zeroSimple();
      (simpleBands || []).forEach(function(slot) {
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
      draft.parametric_bands = (simpleBands || []).filter(function(s) {
        return Math.abs(draft.simple_eq[s.field] || 0) >= ACTIVE_GAIN_EPSILON_DB;
      }).map(function(s) {
        // Simple EQ shelves ignore Q in the backend, but carrying a stable
        // PEQ-side default keeps converted bands predictable if the type changes.
        return {enabled: true, type: s.type, freq_hz: s.freq_hz,
                gain_db: draft.simple_eq[s.field], q: 1.0};
      });
      draft.simple_eq = zeroSimple();
      activeBand = 0;
    }
    mode = next;
    onDraftChanged(true);
  }
  function addBand() {
    if (draft.parametric_bands.length >= limits.max_parametric_bands) {
      status('Advanced EQ is limited to ' + limits.max_parametric_bands + ' bands.', true);
      return;
    }
    draft.parametric_bands.push({enabled: true, type: 'Peaking', freq_hz: 1000, gain_db: 0, q: 1});
    activeBand = draft.parametric_bands.length - 1;
    onDraftChanged(true);
  }
  function delBand(index) {
    draft.parametric_bands.splice(index, 1);
    activeBand = Math.max(0, Math.min(activeBand, draft.parametric_bands.length - 1));
    onDraftChanged(true);
  }
  function resetDraft() {
    draft = sourceProfile();
    if (editing.kind !== 'new') draft = withIdentity(draft, editing.id, editing.name);
    mode = draft.parametric_bands.length ? 'peq' : 'simple';
    activeBand = 0; naming = false;
    onDraftChanged(true);
  }
  function defaultName() {
    var d = new Date();
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return 'Profile · ' + months[d.getMonth()] + ' ' + d.getDate();
  }
  function focusNameInput() { var n = el('name-input'); if (n) { n.focus(); n.select(); } }
  async function finalizeName() {
    naming = false;
    if (nameMode === 'rename' && editing.kind === 'user') {
      var newName = (nameDraft || '').trim() || editing.name || defaultName();
      var rp = await profileMutate('./profiles/rename', {id: editing.id, name: newName});
      if (rp && rp.profile_entry) {
        if (selectedId === editing.id) selectedId = rp.profile_entry.id;
        editing = {kind: 'user', id: rp.profile_entry.id, name: rp.profile_entry.name};
        status('Renamed to ' + rp.profile_entry.name + '.');
      }
      render();
      return;
    }
    var name = (nameDraft || '').trim() || defaultName();
    var payload = await profileMutate('./profiles/save', {id: null, name: name, profile: draft});
    if (payload && payload.profile_entry) {
      var entry = payload.profile_entry;
      library = payload.profile_library || library;
      selectedId = entry.id; view = 'saved';
      await requestLiveSource({okMsg: 'Saved ' + entry.name + '.', immediate: true});
      render();
    } else { render(); }
  }
  async function overwrite() {
    if (editing.kind !== 'user') return;
    var payload = await profileMutate('./profiles/save', {id: editing.id, name: editing.name, profile: draft});
    if (payload && payload.profile_entry) {
      var entry = payload.profile_entry;
      selectedId = entry.id; view = 'saved';
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
      if (selectedId === id) {
        selectedId = fallbackSavedId();
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
    outputTopology.revision = payload && payload.topology_revision || null;
    outputTopology.error = '';
    outputTopology.dirty = false;
    outputTopology.saving = false;
    outputTopology.resetting = false;
    outputTopology.loading = false;
    outputTopology.identitySaving = '';
    outputTopology.protectionSaving = '';
    if (outputGroups(topology).length) outputTemplateDraftAxes = {layout: '', speakerMode: ''};
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
    } catch (e) {
      patchActiveSpeaker({commissionBusy: '', commissionError: String(e.message || e)});
      render();
      return {ok: false, error: String(e.message || e)};
    }
    if (payload && payload.measurements) {
      patchActiveSpeaker({measurements: payload.measurements});
    }
    await refreshCommissionState();
    await refreshCommissioningView();
    if (showBusy) patchActiveSpeaker({commissionBusy: ''});
    render();
    return {ok: true, payload: payload};
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
  async function commissionAck(outcome) {
    if (!outcome) return;
    stopCommissionAutoRamp('');
    var result = await postCommission('./active-speaker/commission-ramp-ack',
      {outcome: outcome}, 'Recording');
    var confirmed = !!(result && result.payload &&
      result.payload.status === 'confirmed');
    var summary = result && result.payload && result.payload.measurements &&
      result.payload.measurements.summary || null;
    if (outcome === 'heard_correct_driver' && confirmed) {
      if (summary && (summary.driver_checks_complete === true ||
          summary.driver_measurements_complete === true)) {
        outputStepOverride = 'profile';
        status('Driver checks are saved. Continue with the combined crossover check.');
      } else {
        status('Driver check saved. Continue with the next driver.');
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
    outputStepOverride = 'map';
    status('Check the DAC channel assignments, save, then confirm the wiring again.');
    render();
  }
  function backToCrossoverConfiguration() {
    outputStepOverride = 'research';
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
    runCommissionAutoRamp(group.id, role, token);
  }
  function setOutputDraft(next) {
    outputTopology.draft = next;
    if (outputGroups(next).length) outputTemplateDraftAxes = {layout: '', speakerMode: ''};
    outputTopology.dirty = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    driverResearch.dirty = true;
    crossoverPreview.payload = null;
    crossoverPreview.error = '';
    patchActiveSpeaker({stagedConfig: null});
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
    outputStepOverride = 'map';
    setOutputDraft(next);
    status('Channel assignment updated. Save before confirming the wiring.');
  }
  function outputChannel(role, index) {
    var tweeter = role === 'tweeter';
    return {
      role: role,
      physical_output_index: index,
      identity_verified: false,
      startup_muted: true,
      protection_required: tweeter,
      protection_status: tweeter ? 'required_missing' : 'not_required'
    };
  }
  function baseOutputDraft(source) {
    var topology = source || currentOutputTopology();
    if (!topology) return null;
    var next = clone(topology);
    next.status = 'draft';
    delete next.evaluation;
    if (next.safety) next.safety.sound_tests_allowed = false;
    return next;
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
    outputTemplateDraftAxes = {layout: '', speakerMode: ''};
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
    outputTemplateDraftAxes = {layout: layout || '', speakerMode: speakerMode || ''};
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
    button.textContent = driverResearch.promptCopied ? 'Copied' :
      (driverResearch.promptSelected ? 'Selected' : 'Copy prompt');
  }
  function updateDriverResearchImportSummary() {
    var summary = el('driver-research-import-summary');
    if (summary) summary.innerHTML = renderDriverResearchSummary();
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
      status('Add driver models before copying the research prompt.', true);
      return;
    }
    var copied = await copyTextToClipboard(prompt.value, prompt);
    driverResearch.promptCopied = copied;
    driverResearch.promptSelected = !copied;
    render();
    updateDriverResearchPromptButton();
    if (!copied) {
      var fallbackPrompt = el('driver-research-prompt');
      if (fallbackPrompt) {
        fallbackPrompt.focus();
        fallbackPrompt.select();
        fallbackPrompt.setSelectionRange(0, fallbackPrompt.value.length);
      }
    }
    status(copied ? 'Copied driver research prompt.' :
      'Copy was blocked by the browser. Prompt text is selected.', !copied);
  }
  function parseDriverResearchImport() {
    try {
      var payload = JSON.parse(driverResearch.importText || '');
      driverResearch.parsed = summarizeDriverResearchPayload(payload);
      applyDriverResearchToManualSettings(payload);
      driverResearch.error = '';
      driverResearch.dirty = true;
      driverResearch.promptCopied = false;
      driverResearch.promptSelected = false;
      status('Imported driver research. Review the visible values before updating the working setup.');
    } catch (e) {
      driverResearch.parsed = null;
      driverResearch.error = e.message;
      status('Imported JSON needs review: ' + e.message, true);
    }
    render();
  }
  async function saveDriverResearchDraft(options) {
    options = options || {};
    if (!driverResearch.dirty && driverResearchStepSatisfied() && options.nextStep) {
      outputStepOverride = options.nextStep;
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
    var manualPayload = manualSettingsPayload(currentOutputTopology());
    var researchPayload = null;
    var importWarning = '';
    if ((driverResearch.importText || '').trim()) {
      try {
        researchPayload = JSON.parse(driverResearch.importText);
        driverResearch.parsed = summarizeDriverResearchPayload(researchPayload);
        driverResearch.error = '';
      } catch (e) {
        driverResearch.parsed = null;
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
    driverResearch.error = '';
    render();
    try {
      var resp = await fetch('./active-speaker/design-draft', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          operator_inputs: driverResearch.inputs,
          manual_settings: manualPayload,
          driver_research: researchPayload
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker design draft save failed');
      ingestDesignDraft(payload, {force: true});
      crossoverPreview.payload = null;
      crossoverPreview.error = '';
      if (options.nextStep) outputStepOverride = options.nextStep;
      if (!options.forPreview) {
        status(importWarning
          ? 'Working setup updated from visible fields. Imported JSON was not saved.'
          : 'Working setup updated. No filters are active and no sound was played.');
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
    if (!driverResearchHasPreviewInputs(currentOutputTopology())) {
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
      status('Crossover preview ready. No filters are active and no sound was played.');
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
        outputStepOverride = 'layout';
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
        outputStepOverride = 'map';
        status('Save the speaker layout before confirming outputs.', true);
        render();
        return;
      }
      if (!outputIdentityComplete()) {
        var report = outputIdentityReport();
        var assigned = Number(report && report.assigned_channel_count || 0);
        outputStepOverride = 'map';
        status(assigned > 0 ?
          'Confirm every assigned output before continuing.' :
          'Save a speaker layout with assigned outputs before continuing.', true);
        render();
        return;
      }
      openOutputStep('safety');
      status('Outputs are confirmed. Continue with driver checks.');
      return;
    }
    if (step === 'safety') {
      if (driverChecksComplete()) {
        openOutputStep('profile');
        status('Driver checks are saved. Continue with the combined crossover check.');
        return;
      }
      outputStepOverride = 'safety';
      status('Test each active driver before saving the active profile.');
      render();
      return;
    }
    if (step === 'profile') {
      outputStepOverride = 'profile';
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
    patchActiveSpeaker({stagedConfig: null});
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
      if (options.nextStep) outputStepOverride = options.nextStep;
      status('Saved speaker layout. No sound was played.');
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
      'Reset speaker setup? This clears the saved active crossover layout and returns this speaker to a detected passive setup. No test tone will play.',
      {danger: true}
    );
    if (!ok) return;
    stopCommissionAutoRamp('');
    outputTopology.resetting = true;
    outputTopology.error = '';
    patchActiveSpeaker({
      stagedConfig: null,
      startupLoad: null,
      commission: null,
      commissioningView: null,
      measurements: null,
      baselineProfile: null,
      error: '',
      commissionBusy: '',
      commissionError: ''
    });
    render();
    try {
      var resp = await fetch('./output-topology/reset', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker setup reset failed');
      ingestOutputTopology(payload);
      outputStepOverride = 'layout';
      var cleanupWarning = resetCleanupWarning(payload);
      if (cleanupWarning) {
        outputTopology.error = cleanupWarning;
        status(cleanupWarning, true);
      } else {
        status('Reset speaker setup to the detected passive layout. No sound was played.');
      }
    } catch (e) {
      outputTopology.resetting = false;
      outputTopology.error = e.message;
      status('Could not reset speaker setup: ' + e.message, true);
    }
    render();
  }
  function resetCleanupWarning(payload) {
    var reset = payload && payload.active_speaker_reset || {};
    if (reset.status !== 'partial') return '';
    var errors = Array.isArray(reset.errors) ? reset.errors : [];
    var ids = [];
    errors.forEach(function(error) {
      if (error && error.id) ids.push(String(error.id));
    });
    var count = errors.length || ids.length || 1;
    var msg = 'Reset speaker setup, but JTS could not clear ' + count +
      ' active-speaker setup artifact' + (count === 1 ? '' : 's');
    if (ids.length) msg += ': ' + ids.join(', ');
    msg += '. Reset again or check logs before continuing.';
    return msg;
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
      : 'Mark "' + label + '" as not confirmed?';
    if (commissionAutoRamp.running || commissionPendingStep()) {
      stopCommissionAutoRamp('');
      var abortResult = await postCommission('./active-speaker/commission-ramp-abort', {}, 'Re-muting');
      if (!abortResult || !abortResult.ok) return;
    }
    if (!await jtsConfirm(message, {danger: false})) {
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
      status((verified ? 'Confirmed output: ' : 'Cleared output confirmation: ') + label + '.');
    } catch (e) {
      outputTopology.identitySaving = '';
      outputTopology.error = e.message;
      status('Could not update channel identity: ' + e.message, true);
    }
    render();
  }
  function sleepMs(ms) {
    return new Promise(function(resolve) {
      window.setTimeout(resolve, Math.max(0, Number(ms) || 0));
    });
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
        combinedTestLevelDbfs: pending.levelDbfs,
        levelDbfs: activeSpeaker.levelDbfs
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
      'Play a looped spoken combined test for "' + label +
        '" at ' + fmtDb(combinedTestLevelDbfs()) +
        '? JTS uses the prepared crossover and keeps the test level bounded.',
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
    patchActiveSpeaker({
      loading: false, action: 'Starting combined test',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs,
      combinedTestLevelDbfs: requestedLevel
    });
    render();
    summedTestRequest.armTimer = window.setTimeout(function() {
      if (summedTestRequest.token !== requestToken ||
          activeSpeaker.action !== 'Starting combined test') {
        return;
      }
      patchActiveSpeaker({
        loading: false,
        action: 'Playing combined test',
        error: '',
        levelDbfs: activeSpeaker.levelDbfs,
        combinedTestLevelDbfs: requestedLevel
      });
      render();
    }, SUMMED_TEST_STOP_ARM_MS);
    try {
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
      if (summedTestRequest.token !== requestToken) return;
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
        levelDbfs: activeSpeaker.levelDbfs,
        combinedTestLevelDbfs: isFinite(appliedLevel) ? appliedLevel : requestedLevel
      });
      await refreshCommissioningView();
      var playback = payload.playback || {};
      var emitted = playbackConfirmable(playback);
      if (playback.stop_reason !== 'operator_confirmed') {
        status(playback.status === 'stopped' ?
          'Combined speaker test stopped.' : (emitted ?
          'Combined speaker test played. Record what you heard.' :
          'Combined speaker test did not play. Review the message in this card.'),
          playback.status !== 'stopped' && !emitted);
      }
      if (summedTestRequest.current &&
          summedTestRequest.current.token === requestToken) {
        summedTestRequest.current = null;
      }
    } catch (e) {
      if (summedTestRequest.token !== requestToken) return;
      clearSummedTestArmTimer();
      if (summedTestRequest.current &&
          summedTestRequest.current.token === requestToken) {
        summedTestRequest.current = null;
      }
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not start the combined speaker test: ' + e.message, true);
    }
    render();
  }
  async function stopSummedTest(options) {
    options = options || {};
    var requestToken = summedTestRequest.token;
    var payload = null;
    clearSummedTestArmTimer();
    clearSummedTestLevelTimer();
    summedTestLevelUpdate.pending = null;
    patchActiveSpeaker({
      loading: false, action: 'Stopping combined test',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/summed-test/stop', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({reason: options.reason || 'operator_stop'}),
        keepalive: !!options.keepalive
      });
      payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'combined speaker stop failed');
      if (summedTestRequest.token !== requestToken) return payload;
      patchActiveSpeaker({
        loading: false,
        action: '',
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      await refreshCommissioningView();
      if (!options.quiet) {
        status(payload.status === 'idle' ?
          'No combined speaker test is playing.' :
          'Combined speaker test stopped.');
      }
    } catch (e) {
      if (summedTestRequest.token !== requestToken) return payload;
      patchActiveSpeaker({
        loading: false,
        action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not stop the combined speaker test: ' + e.message, true);
      render();
      throw e;
    }
    render();
    return payload;
  }
  async function recordSummedValidation(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var outcome = button.getAttribute('data-outcome') || '';
    var summedTestId = button.getAttribute('data-summed-test-id') || '';
    if (!groupId || !outcome) {
      status('Choose a speaker group and validation result before saving the combined check.', true);
      return;
    }
    if (!summedTestId && activeSpeaker.action === 'Playing combined test') {
      try {
        summedTestId = await finishPlayingSummedTestForValidation(groupId);
      } catch (e) {
        status('Could not finish the combined speaker test: ' + e.message, true);
        return;
      }
    }
    if (!summedTestId) {
      status('Run the combined speaker test first, then record what you heard.', true);
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Saving combined check',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var groupView = commissioningGroupView(groupId);
      var action = commissioningGroupAction(groupView, 'record_combined_result');
      var body = Object.assign({}, action && action.body || {}, {
        speaker_group_id: groupId,
        outcome: outcome,
        summed_test_id: action && action.body && action.body.summed_test_id || summedTestId,
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
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      await refreshCommissioningView();
      try {
        patchActiveSpeaker({baselineProfile: await fetchActiveSpeakerBaselineProfile()});
      } catch (profileError) {
        patchActiveSpeaker({baselineProfile: activeSpeaker.baselineProfile});
      }
      if (summedValidationComplete()) outputStepOverride = 'profile';
      status('Combined crossover check saved. Save and apply the active profile when ready.');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not save combined crossover check: ' + e.message, true);
    }
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
      status('Test each driver and save the combined crossover check before saving the active profile.', true);
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
      status('Test each driver and save the combined crossover check before saving the active profile.', true);
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Finishing active profile',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/baseline-profile/save-and-apply', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'active profile save/apply failed');
      patchActiveSpeaker({
        loading: false, action: '',
        baselineProfile: payload.profile || payload,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      await refreshCommissioningView();
      status(payload.status === 'applied' ?
        'Active speaker profile saved and applied.' :
        'Active speaker profile was not applied; review the message in this card.',
        payload.status !== 'applied');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not save and apply active profile: ' + e.message, true);
    }
    render();
  }
  async function fetchActiveSpeakerStartupLoad() {
    var resp = await fetch('./active-speaker/startup-load', {cache: 'no-store'});
    if (!resp.ok) throw new Error('startup load status failed');
    return await resp.json();
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
      selectedId = findIdFor(applied);
      // Open on Off when no EQ is effectively applied — bypassed (enabled
      // false) OR flat (no active filters). Open on Saved with the applied
      // profile marked active otherwise. filter_count is the backend's
      // authoritative signal (len(build_sound_filters); 0 when disabled/flat).
      if (payload.filter_count > 0) {
        view = 'saved';
      } else {
        view = 'off';
      }
      render();
      refreshOutputTopology({silent: true});
    } catch (e) {
      status('Could not load sound profile: ' + e.message, true);
    }
  }
  // Follower boot: no content-EQ /state fetch (the leader owns the program
  // domain). Paint the local active-speaker shell, then load its hardware state.
  function loadFollowerActive() {
    render();
    refreshOutputTopology({silent: true});
  }
  window.addEventListener('pagehide', function() {
    if (activeSpeaker.action === 'Starting combined test' ||
        activeSpeaker.action === 'Playing combined test') {
      stopSummedTest({keepalive: true, quiet: true, reason: 'pagehide'});
    }
    if (volumeFloorTone.active || volumeFloorTone.inFlight) {
      stopVolumeFloorTone({keepalive: true, quiet: true, reason: 'pagehide'});
    }
  });
  if (followerMode) loadFollowerActive();
  else loadState();
})();
