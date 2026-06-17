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
  activeCommissionGroup,
  activeSpeakerStepState,
  commissionCardState,
  commissionFloorLabel,
  defaultActiveSpeakerStep,
  humanMode,
  humanRole,
  outputStatusClass,
  outputStepTitle,
  playbackResultMessage
} from "/assets/sound-profile/js/active-speaker-ui.js";
import { magnitudeDb, GAINLESS_TYPES } from "/assets/sound-profile/js/eq-math.js";
(function() {
  var LIMIT_DEFAULTS = {
    simple_gain_db: 12, advanced_gain_db: 12, max_parametric_bands: 8,
    min_freq_hz: 20, max_freq_hz: 20000, min_q: 0.2, max_q: 10, cut_max_q: 1.4,
    simple_bands: [], headroom_trim_max_db: 12
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
  var soundSettings = {headroom_trim_db: 0, match_loudness: false};  // global output settings
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
    commission: null, commissionBusy: '', commissionError: ''
  };
  var outputAudibleRamp = {
    running: false,
    token: 0,
    targetKey: '',
    lastPlaybackId: '',
    pulseCount: 0,
    levelDbfs: null,
    message: ''
  };
  var OUTPUT_RAMP_LISTEN_MS = 1200;
  var OUTPUT_RAMP_NEXT_PULSE_MS = 350;
  var outputTopology = {
    loading: false, saving: false, payload: null, draft: null,
    identity: null, clockDomain: null, activeRoute: null,
    identitySaving: '', protectionSaving: '',
    readiness: null, readinessChecking: '', readinessError: '',
    readinessPlayback: null, readinessPlaybackChecking: '',
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
    saving: false
  };
  var crossoverPreview = {payload: null, preparing: false, error: ''};
  var ZERO_DETENT_DB = 0.1;

  function el(id) { return document.getElementById(id); }
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
    renderTabs();
    renderLiveGraph();
    if (view === 'off') renderOff();
    else if (view === 'saved') renderSaved();
    else renderDraft();
    status(statusText, statusErr);
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
  function renderSoundSettings() {
    var ml = soundSettings.match_loudness ? ' checked' : '';
    var trim = Number(soundSettings.headroom_trim_db) || 0;
    var trimMax = Number(limits.headroom_trim_max_db) || 12;  // backend clamps authoritatively
    return '<section class="sound-settings">' +
      '<div class="setting-row">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Match loudness</p>' +
          '<p class="setting-row__hint">Level-match profiles so switching compares tone, not volume.</p>' +
        '</div>' +
        '<label class="toggle"><input type="checkbox" id="set-match-loudness"' + ml +
          ' aria-label="Match loudness"><span class="track"></span></label>' +
      '</div>' +
      '<details class="advanced"' + (trim > 0 ? ' open' : '') + '>' +
        '<summary>Advanced</summary>' +
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
      outputTopology.readinessChecking || outputTopology.readinessPlaybackChecking ||
      outputTopology.dirty || outputTopology.touched;
    return '<section class="active-speaker-setup">' +
      '<details class="advanced" data-active-speaker-setup' + (open ? ' open' : '') + '>' +
        '<summary>Advanced speaker setup</summary>' +
        renderOutputTopologySetup() +
        renderCommissionCard() +
      '</details>' +
    '</section>';
  }
  // Protected single-audio-path driver commissioning (the Stage-5 ramp). Shown
  // only when an active 2/3-way speaker group exists. Arming is silent; a step
  // makes ONE driver audible at a low level through the production crossover.
  function renderCommissionCard() {
    var group = activeCommissionGroup(currentOutputTopology());
    var c = commissionCardState(activeSpeaker.commission, group);
    if (!c.available) return '';
    var busy = activeSpeaker.commissionBusy;
    var dis = busy ? ' disabled' : '';
    var roleLabel = function(r) { return escapeHtml(humanRole(r)); };
    var statusRows =
      '<div class="commission-status">' +
      '<div><span class="commission-status__k">Armed driver</span>' +
        '<span class="commission-status__v">' +
        (c.armed ? roleLabel(c.armedRole) + ' (' +
          (c.armedGainDb == null ? '—'
            : (Number(c.armedGainDb) <= -120 ? 'silent floor'
              : escapeHtml(String(c.armedGainDb)) + ' dB')) + ')'
          : 'none — silent') + '</span></div>' +
      '<div><span class="commission-status__k">By-ear</span>' +
        '<span class="commission-status__v">' +
        escapeHtml(commissionFloorLabel(c.floorStatus)) + '</span></div>' +
      '<div><span class="commission-status__k">Confirmed</span>' +
        '<span class="commission-status__v">' +
        (c.confirmedRoles.length ? c.confirmedRoles.map(roleLabel).join(', ') : 'none') +
        '</span></div>' +
      '</div>';

    var buttons = [];
    if (c.canArm) {
      var roles = activeCommissionRoles(group);
      buttons = roles.map(function(role) {
        return '<button type="button" class="btn btn--ghost" data-act="commission-arm" ' +
          'data-role="' + escapeHtml(role) + '"' + dis + '>Arm ' + roleLabel(role) +
          ' (silent)</button>';
      });
    }
    if (c.canStep) {
      buttons.push('<button type="button" class="btn btn--primary" ' +
        'data-act="commission-step" data-role="' + escapeHtml(c.armedRole || '') + '"' +
        dis + '>Make audible (step)</button>');
    }
    if (c.canAck) {
      buttons.push('<button type="button" class="btn btn--primary" ' +
        'data-act="commission-ack" data-outcome="heard_correct_driver"' + dis +
        '>I hear ' + roleLabel(c.pendingRole) + '</button>');
      buttons.push('<button type="button" class="btn btn--ghost" ' +
        'data-act="commission-ack" data-outcome="silent"' + dis + '>Too quiet — louder</button>');
      buttons.push('<button type="button" class="btn btn--ghost" ' +
        'data-act="commission-ack" data-outcome="too_loud"' + dis + '>Too loud</button>');
      buttons.push('<button type="button" class="btn btn--ghost" ' +
        'data-act="commission-ack" data-outcome="heard_wrong_driver"' + dis +
        '>Wrong driver</button>');
    }
    if (c.canRemute) {
      buttons.push('<button type="button" class="btn btn--danger" ' +
        'data-act="commission-abort"' + dis + '>Stop / re-mute</button>');
    }

    var note = activeSpeaker.commissionError ?
      '<p class="commission-card__error">' + escapeHtml(activeSpeaker.commissionError) + '</p>' :
      (c.armed ?
        '<p class="setting-row__hint">Amps on at low gain only. Each step is very quiet; confirm by ear before going louder.</p>' :
        '<p class="setting-row__hint">Keep amps OFF until a driver is armed and you are ready to listen. Arming is silent.</p>');

    return '<div class="commission-card">' +
      '<h4 class="commission-card__title">Protected driver commissioning</h4>' +
      '<p class="commission-card__lead">Test one driver at a time through the real ' +
        'crossover/limiter graph — woofer first, then tweeter.</p>' +
      statusRows + note +
      (busy ? '<p class="setting-row__hint">' + escapeHtml(busy) + '…</p>' : '') +
      '<div class="active-speaker-actions commission-card__actions">' +
        buttons.join('') + '</div>' +
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
    var drivers = outputRoleSummary(topology).map(function(role) {
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
  function applyDriverResearchToManualSettings(payload) {
    if (!payload || typeof payload !== 'object') return;
    (Array.isArray(payload.drivers) ? payload.drivers : []).forEach(function(driver) {
      if (!driver || !driver.role) return;
      var role = String(driver.role);
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
    (Array.isArray(payload.crossover_candidates) ? payload.crossover_candidates : [])
      .forEach(function(candidate) {
        if (!candidate || !Array.isArray(candidate.between_roles) ||
            candidate.between_roles.length !== 2) return;
        var pair = candidate.between_roles.map(String);
        var setting = crossoverSetting(pair);
        if (candidate.frequency_hz != null) setting.frequency_hz = candidate.frequency_hz;
        if (candidate.filter_type) setting.filter_type = String(candidate.filter_type);
        if (candidate.slope_db_per_octave != null) {
          setting.slope_db_per_octave = candidate.slope_db_per_octave;
        }
      });
  }
  function driverResearchPrompt(topology) {
    var roles = outputRoleSummary(topology);
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
      '',
      'Return only JSON with this shape:',
      '{',
      '  "artifact_schema_version": 1,',
      '  "kind": "jts_active_crossover_driver_research",',
      '  "drivers": [',
      '    {',
      '      "role": "full_range|woofer|mid|tweeter|subwoofer",',
      '      "model": "string",',
      '      "manufacturer": "string|null",',
      '      "nominal_impedance_ohm": 8,',
      '      "sensitivity_db_2v83_1m": 90,',
      '      "usable_frequency_range_hz": [80, 5000],',
      '      "recommended_highpass_hz": 80,',
      '      "recommended_lowpass_hz": 2200,',
      '      "do_not_test_below_hz": 1200,',
      '      "gain_offset_db": -6,',
      '      "notes": "short safety-relevant notes",',
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
    var summary = draftPayload.summary || {};
    return savedStatus && savedStatus !== 'not_saved' && savedStatus !== 'unreadable' &&
      (Number(summary.driver_count || 0) + Number(summary.manual_driver_count || 0)) > 0 &&
      (Number(summary.crossover_candidate_count || 0) +
        Number(summary.manual_crossover_candidate_count || 0)) > 0 &&
      !driverResearch.dirty;
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
  function driverResearchSavedStatusLabel(status, summary) {
    summary = summary || {};
    if (status === 'ready_for_review') return 'saved settings';
    if (status === 'needs_research') return 'saved partial settings';
    if (status === 'blocked' && Number(summary.driver_count || 0) > 0) {
      return 'saved driver info';
    }
    if (status === 'blocked') return 'saved: needs layout';
    if (status === 'unreadable') return 'saved draft unreadable';
    return 'saved draft';
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
      return 'Confirmed. Continue to Test each driver; JTS will start it very quiet.';
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
          '<p class="setting-row__hint">Build the speaker layout, add crossover info, confirm DAC outputs, test each driver, then validate and apply the profile.</p>' +
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
  function quietTestControlsOpen() {
    var session = activeSpeaker.session || {};
    return session.status === 'armed';
  }
  function activeSpeakerAudibleTestReady() {
    var readiness = outputTopology.readiness || {};
    return readiness.preconditions_passed === true &&
      readiness.playback_allowed === true &&
      !!activeSpeakerSelectedReadinessTarget();
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
  function driverMeasurementsComplete() {
    return measurementSummary().driver_measurements_complete === true;
  }
  function summedValidationComplete() {
    return measurementSummary().summed_validation_complete === true;
  }
  function baselineProfileApplied() {
    return activeSpeaker.baselineProfile && activeSpeaker.baselineProfile.status === 'applied';
  }
  function outputStepContext(topology) {
    return {
      hasLayout: outputGroups(topology).length > 0,
      dirty: outputTopology.dirty,
      driverResearchSatisfied: driverResearchStepSatisfied(),
      outputIdentityComplete: outputIdentityComplete(),
      driverMeasurementsComplete: driverMeasurementsComplete(),
      baselineProfileApplied: baselineProfileApplied()
    };
  }
  function outputStepState(step, topology) {
    return activeSpeakerStepState(step, outputStepContext(topology));
  }
  function defaultOutputStep() {
    return defaultActiveSpeakerStep(outputStepContext(currentOutputTopology()));
  }
  function outputStepIsOpen(step, topology) {
    return (outputStepOverride || defaultOutputStep()) === step;
  }
  function outputStepCanOpen(step, topology) {
    return outputStepState(step, topology) !== 'todo';
  }
  function openOutputStep(step) {
    outputStepOverride = step;
    render();
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
  function renderOutputStepButton(step, label, primary) {
    return '<button type="button" class="btn ' + escapeHtml(primary ? 'btn--primary' : 'btn--ghost') +
      '" data-act="output-step-next" data-step="' + escapeHtml(step) + '">' +
      escapeHtml(label) + '</button>';
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
        '<p class="setting-row__hint">Choose what you are wiring. No sound plays here.</p></div></div>' +
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
        '<p class="setting-row__hint">Optional. This composes with any mono or stereo layout instead of duplicating templates.</p></div>' +
        '<span class="status-pill' + (hasSub ? ' status-pill--ready' : '') + '">' + escapeHtml(hasSub ? 'added' : 'optional') + '</span></div>' +
      '<p class="setting-row__hint">' + escapeHtml(hint) + '</p>' +
      '<button type="button" class="btn btn--ghost" data-act="toggle-output-subwoofer" data-mode="' +
        escapeHtml(hasSub ? 'remove' : 'add') + '"' + (disabled ? ' disabled' : '') + '>' +
        escapeHtml(hasSub ? 'Remove subwoofer' : 'Add subwoofer') + '</button>' +
    '</div>';
  }
  function renderDriverResearchSummary(options) {
    options = options || {};
    var saved = driverResearch.designDraft || {};
    var savedStatus = saved.status || '';
    var savedSummary = saved.summary || {};
    var savedHtml = savedStatus && savedStatus !== 'not_saved' ? (
      '<div class="driver-research__summary driver-research__summary--saved">' +
        '<span class="status-pill' + (savedStatus === 'ready_for_review' ? ' status-pill--ready' : '') + '">' +
          escapeHtml(driverResearchSavedStatusLabel(savedStatus, savedSummary)) + '</span>' +
        '<p class="setting-row__hint">' + escapeHtml(
          String(Number(savedSummary.driver_count || 0) + Number(savedSummary.manual_driver_count || 0)) +
          ' saved driver' +
          ((Number(savedSummary.driver_count || 0) + Number(savedSummary.manual_driver_count || 0)) === 1 ? '' : 's') +
          ', ' + String(
            Number(savedSummary.crossover_candidate_count || 0) +
            Number(savedSummary.manual_crossover_candidate_count || 0)
          ) +
          ' crossover setting' +
          ((Number(savedSummary.crossover_candidate_count || 0) +
            Number(savedSummary.manual_crossover_candidate_count || 0)) === 1 ? '' : 's') +
          '. No filters are applied.'
        ) + '</p>' +
      '</div>'
    ) : '';
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
      '<span class="status-pill status-pill--ready">import parsed</span>' +
      '<p class="setting-row__hint">' + escapeHtml(
        summary.driverCount + ' driver' + (summary.driverCount === 1 ? '' : 's') +
        ', ' + summary.candidateCount + ' crossover candidate' + (summary.candidateCount === 1 ? '' : 's') +
        '. Roles: ' + summary.roles.join(', ')
      ) + '</p>' +
      (summary.warnings.length ? '<div class="driver-research__notes">' +
        '<p class="setting-row__title">Review notes</p>' +
        '<ul>' + summary.warnings.map(function(warning) {
          return '<li>' + escapeHtml(String(warning)) + '</li>';
        }).join('') + '</ul></div>' : '') +
    '</div>';
  }
  function renderManualDriverSettings(topology) {
    var roles = outputRoleSummary(topology);
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
    return '<details class="driver-research__ai">' +
      '<summary>Use AI to fill these settings</summary>' +
      '<div class="driver-research__grid driver-research__grid--ai">' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<p class="setting-row__title">Research prompt</p>' +
            '<button type="button" class="btn btn--ghost" data-act="copy-driver-research-prompt">Copy prompt</button>' +
          '</div>' +
          '<textarea id="driver-research-prompt" class="driver-research__textarea" readonly rows="10" ' +
            'aria-label="Driver research prompt">' +
            escapeHtml(driverResearchPrompt(topology)) + '</textarea>' +
        '</div>' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<p class="setting-row__title">Paste JSON result</p>' +
            '<div class="driver-research__actions">' +
              '<button type="button" class="btn btn--ghost" data-act="parse-driver-research">Use imported values</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="driver-research-import" class="driver-research__textarea" data-driver-import ' +
            'rows="7" placeholder="{...}" aria-label="Driver research JSON result">' +
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
      '<div class="output-card__head"><div><p class="output-card__title">Crossover settings</p>' +
        '<p class="setting-row__hint">Enter the values JTS should use for the no-audio crossover preview. The AI helper below can fill these in for review.</p></div></div>' +
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
        '<button type="button" class="btn btn--primary" data-act="save-driver-design"' +
          (saveDisabled ? ' disabled' : '') + '>' +
          escapeHtml(driverResearch.saving ? 'Saving' : 'Save crossover settings') +
        '</button>' +
      '</div>' +
      '<div class="driver-research__saved-summary">' + renderDriverResearchSummary({savedOnly: true}) + '</div>' +
      renderDriverResearchAiHelper(topology) +
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
    var hasSavedResearch = driverResearchCanPreparePreview();
    var canPrepare = hasSavedResearch && !outputTopology.dirty;
    var disabled = crossoverPreview.preparing || !canPrepare;
    var hint = canPrepare ?
      'Builds the crossover plan from your saved settings. No sound plays.' :
      (outputTopology.dirty
        ? 'Save the speaker layout before preparing a crossover preview.'
        : (hasSavedResearch
          ? 'Save the latest crossover setting edits before preparing a preview.'
          : 'Save crossover settings before preparing a preview.'));
    if (crossoverPreview.error) hint = crossoverPreview.error;
    return '<div class="output-card output-card--crossover-preview">' +
      '<div class="output-card__head"><div><p class="output-card__title">Crossover preview</p>' +
        '<p class="setting-row__hint">' + escapeHtml(hint) + '</p></div>' +
        '<span class="status-pill' + previewStatusClass(label) + '">' + escapeHtml(label) + '</span></div>' +
      renderCrossoverPreviewRows(payload) +
      '<p class="setting-row__hint">' + escapeHtml(
        String(readyCount) + ' crossover candidate' + (readyCount === 1 ? '' : 's') +
        ', ' + String(warningIssues.length) + ' review note' +
        (warningIssues.length === 1 ? '' : 's') + '. No filters are applied.' +
        (laterSafetyCount ? ' JTS still checks the setup before any sound.' : '')
      ) + '</p>' +
      renderPreviewIssues(warningIssues) +
      '<button type="button" class="btn btn--primary" data-act="prepare-crossover-preview"' +
        (disabled ? ' disabled' : '') + '>' +
        escapeHtml(crossoverPreview.preparing ? 'Preparing' : 'Prepare crossover preview') +
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
    var layoutStatusValue = outputTopology.dirty ? 'draft' : 'saved draft';
    return '<div class="output-layout">' +
      renderOutputStepCard(
        'layout',
        'Choose speaker layout',
        'Pick mono or stereo, active or passive, then optionally add a subwoofer.',
        topology,
        renderOutputSetupTemplates(topology) +
          renderOutputSubwooferCard(topology) +
          renderOutputHardwareCard(topology, layoutStatusValue),
        renderOutputHardwareRefresh() +
          renderOutputStepButton('layout',
          outputTopology.dirty ? 'Save and continue' : 'Next: add crossover info',
          true)
      ) +
      renderOutputStepCard(
        'research',
        'Add driver and crossover info',
        'Enter the starting crossover settings. The AI helper is optional.',
        topology,
        renderDriverResearchCard(topology) +
          renderCrossoverPreviewCard(),
        renderOutputStepButton('research', 'Next: confirm outputs', true)
      ) +
      renderOutputStepCard(
        'map',
        'Confirm outputs',
        'Make sure each DAC output goes to the driver shown here.',
        topology,
        renderOutputStageCard(topology) +
          renderOutputGroupsCard(topology) +
          renderOutputIdentityCard(),
          renderOutputStepButton('map', 'Next: test each driver', true)
      ) +
      renderOutputStepCard(
        'safety',
        'Test each driver',
        'Choose one driver at a time, start very quiet, and record what happened.',
        topology,
        renderOutputReadinessCard() +
        renderDriverMeasurementProgressCard(topology),
        ''
      ) +
      renderOutputStepCard(
        'profile',
        'Validate and apply',
        'Test the combined speaker, save the active profile, then apply it if this hardware supports it.',
        topology,
        renderSummedValidationCard(topology) +
        renderBaselineProfileCard(),
        ''
      ) +
    '</div>';
  }
  function renderOutputHardwareCard(topology, statusValue) {
    var hardware = outputHardware(topology) || {};
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
    return '<div class="output-card output-card--hardware">' +
      '<div class="output-card__head">' +
        '<div><p class="output-card__title">' + escapeHtml(hardware.device_label || 'Unknown output device') + '</p>' +
        '<p class="setting-row__hint">Detected output hardware</p></div>' +
        '<span class="status-pill' + outputStatusClass(statusValue) + '">' + escapeHtml(statusValue) + '</span>' +
      '</div>' +
      '<dl class="active-speaker-facts output-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
    '</div>';
  }
  function outputGroupPoint(group, index, total) {
    var pos = group.position || {};
    var hasPos = isFinite(Number(pos.x)) || isFinite(Number(pos.y));
    var x = hasPos ? Number(pos.x || 0) : (group.kind === 'left' ? -0.65 : (group.kind === 'right' ? 0.65 : 0));
    var y = hasPos ? Number(pos.y || 0) : (group.kind === 'subwoofer' ? -0.72 : 0.45);
    if (!hasPos && group.kind === 'mono' && total > 1) x = (index - (total - 1) / 2) * 0.55;
    return {
      x: 120 + clamp(x, -1.2, 1.2) * 70,
      y: 92 - clamp(y, -1.0, 1.0) * 52
    };
  }
  function outputGroupInitial(group) {
    if (group.kind === 'left') return 'L';
    if (group.kind === 'right') return 'R';
    if (group.kind === 'subwoofer') return 'S';
    return 'M';
  }
  function renderOutputStageCard(topology) {
    var groups = outputGroups(topology);
    var assigned = outputAssignedMap(topology);
    var outputs = outputHardware(topology) && Array.isArray(outputHardware(topology).outputs)
      ? outputHardware(topology).outputs : [];
    var markers = groups.map(function(group, index) {
      var p = outputGroupPoint(group, index, groups.length);
      return '<g class="output-stage__speaker" data-kind="' + escapeHtml(group.kind || '') + '">' +
        '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="18"></circle>' +
        '<text x="' + p.x.toFixed(1) + '" y="' + (p.y + 4).toFixed(1) + '">' +
          escapeHtml(outputGroupInitial(group)) + '</text>' +
      '</g>';
    }).join('');
    var lane = outputs.length ? outputs.map(function(output) {
      var hit = assigned[String(output.index)];
      return '<span class="output-chip' + (hit ? ' output-chip--assigned' : '') + '">' +
        escapeHtml(output.human_label || ('Output ' + (Number(output.index) + 1))) +
        (hit ? '<small>' + escapeHtml(hit.group + ' · ' + humanRole(hit.role)) + '</small>' : '<small>Unassigned</small>') +
      '</span>';
    }).join('') : '<p class="setting-row__hint">No physical outputs detected.</p>';
    return '<div class="output-card output-card--stage">' +
      '<div class="output-card__head"><div><p class="output-card__title">Speaker layout</p>' +
        '<p class="setting-row__hint">Top-down sketch for routing context only.</p></div></div>' +
      '<svg class="output-stage" viewBox="0 0 240 150" role="img" aria-label="Speaker output layout">' +
        '<rect x="16" y="18" width="208" height="114" rx="8"></rect>' +
        '<path d="M60 112 C88 92 152 92 180 112"></path>' +
        '<text x="120" y="122" class="output-stage__seat">Listening area</text>' +
        (markers || '<text x="120" y="78" class="output-stage__empty">No groups yet</text>') +
      '</svg>' +
      '<div class="output-lane">' + lane + '</div>' +
    '</div>';
  }
  function renderOutputGroupsCard(topology) {
    var assignments = [];
    outputGroups(topology).forEach(function(group) {
      (Array.isArray(group.channels) ? group.channels : []).forEach(function(channel) {
        if (channel.physical_output_index == null) return;
        assignments.push({group: group, channel: channel});
      });
    });
    assignments.sort(function(a, b) {
      return Number(a.channel.physical_output_index) - Number(b.channel.physical_output_index);
    });
    if (!assignments.length) {
      return '<div class="output-card output-card--groups">' +
        '<p class="output-card__title">DAC output assignments</p>' +
        '<p class="setting-row__hint">Choose a speaker layout first. JTS keeps it as a draft until you confirm the wires.</p>' +
      '</div>';
    }
    return '<div class="output-card output-card--groups">' +
      '<div class="output-card__head"><div><p class="output-card__title">DAC output assignments</p>' +
        '<p class="setting-row__hint">Confirm which physical driver is connected to each DAC output. No sound plays here.</p></div></div>' +
      '<div class="output-roles output-roles--flat">' + assignments.map(function(item) {
        var group = item.group;
        var channel = item.channel;
        var label = channel.human_output_label ||
          (channel.physical_output_index == null ? 'No output assigned' : 'Output ' + (Number(channel.physical_output_index) + 1));
        var target = identityTargetFor(group.id, channel.role) || {};
        var targetId = target.id || (group.id + ':' + channel.role);
        var busy = outputTopology.identitySaving === targetId;
        var readinessBusy = outputTopology.readinessChecking === targetId;
        var disabled = outputTopology.dirty || busy || readinessBusy ||
          channel.physical_output_index == null;
        var model = (driverResearch.inputs && driverResearch.inputs[channel.role]) || '';
        var hardwareLabel = (group.label || group.id) + ' · ' + humanRole(channel.role) +
          (model ? ' · ' + model : '');
        return '<div class="output-role">' +
          '<div class="output-role__text">' +
            '<span>' + escapeHtml(label) + '</span>' +
            '<strong>' + escapeHtml(hardwareLabel) + '</strong>' +
            '<small>' + escapeHtml(outputRoleStatusText(channel)) + '</small>' +
          '</div>' +
          '<div class="output-role__actions">' +
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
        '<p class="setting-row__hint">Confirm each DAC output after you check the wiring. No sound plays here.</p></div>' +
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
  function outputTargetSignature(raw) {
    raw = raw || {};
    var output = raw.output_index != null ? raw.output_index : raw.physical_output_index;
    output = output == null ? null : Number(output);
    return {
      speaker_group_id: raw.speaker_group_id || raw.groupId || null,
      role: String(raw.driver_role || raw.role || '').trim().toLowerCase() || null,
      output_index: isFinite(output) && output >= 0 ? output : null
    };
  }
  function outputTargetKey(raw) {
    var sig = outputTargetSignature(raw);
    return [
      sig.speaker_group_id || '',
      sig.role || '',
      sig.output_index == null ? '' : String(sig.output_index)
    ].join(':');
  }
  function outputSameTarget(a, b) {
    var left = outputTargetSignature(a);
    var right = outputTargetSignature(b);
    return !!left && !!right &&
      (left.speaker_group_id || null) === (right.speaker_group_id || null) &&
      (left.role || null) === (right.role || null) &&
      (left.output_index == null ? null : Number(left.output_index)) ===
      (right.output_index == null ? null : Number(right.output_index));
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
  function outputAudibleRampMatches(readiness) {
    return outputAudibleRamp.running &&
      outputAudibleRamp.targetKey &&
      outputAudibleRamp.targetKey === outputTargetKey(readiness && readiness.target);
  }
  function outputLatestAudiblePlayback(readiness) {
    var playback = outputTopology.readinessPlayback || null;
    if (!playbackConfirmable(playback)) return null;
    var target = readiness && readiness.target || null;
    if (!target) return playback;
    return outputSameTarget(playback.target, target) ? playback : null;
  }
  function stopOutputAudibleRamp(message) {
    outputAudibleRamp = Object.assign({}, outputAudibleRamp, {
      running: false,
      token: outputAudibleRamp.token + 1,
      message: message || ''
    });
    outputTopology.readinessPlaybackChecking = '';
  }
  function outputRampProgressPct(level) {
    var cfg = activeSpeakerLevelConfig();
    var value = Number(level);
    if (!isFinite(value)) value = cfg.value;
    return clamp((value - cfg.min) / Math.max(1, cfg.max - cfg.min) * 100, 0, 100);
  }
  function outputFloorAudioPendingForPlayback(playback) {
    var session = activeSpeaker.session || {};
    var quiet = session.quiet_start || {};
    var pendingId = quiet.pending_playback_id || null;
    var playbackId = playback && playback.playback_id || null;
    return session.status === 'armed' &&
      quiet.status === 'floor_pending_operator' &&
      pendingId && playbackId && pendingId === playbackId &&
      playbackConfirmable(playback);
  }
  function outputResultLabel(outcome) {
    outcome = String(outcome || '').trim().toLowerCase();
    if (outcome === 'heard_correct_driver') return 'heard selected driver';
    if (outcome === 'silent') return 'not heard';
    if (outcome === 'heard_wrong_driver') return 'wrong driver';
    if (outcome === 'too_loud') return 'too loud';
    if (outcome === 'blend_ok') return 'blend sounds right';
    if (outcome === 'needs_adjustment') return 'needs adjustment';
    if (outcome === 'polarity_or_delay_problem') return 'sounds hollow or thin';
    return 'not recorded';
  }
  function measurementTargetId(groupId, role) {
    return String(groupId || '') + ':' + String(role || '').trim().toLowerCase();
  }
  function latestDriverMeasurement(groupId, role) {
    var latest = measurementSummary().latest_driver_measurements || {};
    return latest[measurementTargetId(groupId, role)] || null;
  }
  function driverMeasurementCaptured(groupId, role) {
    var latest = latestDriverMeasurement(groupId, role);
    return latest && latest.captured === true;
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
  function driverTargetLabel(group, channel) {
    var output = channel && channel.human_output_label ||
      (channel && channel.physical_output_index != null ?
        'DAC output ' + (Number(channel.physical_output_index) + 1) : 'unassigned output');
    return (group.label || group.id || 'Speaker') + ' · ' +
      humanRole(channel && channel.role) + ' · ' + output;
  }
  function renderDriverMeasurementProgressCard(topology) {
    var targets = [];
    activeOutputGroups(topology).forEach(function(group) {
      (Array.isArray(group.channels) ? group.channels : []).forEach(function(channel) {
        targets.push({group: group, channel: channel});
      });
    });
    if (!targets.length) return '';
    var summary = measurementSummary();
    var captured = Number(summary.captured_driver_count || 0);
    var required = Number(summary.required_driver_count || targets.length);
    var rows = targets.map(function(target) {
      var group = target.group;
      var channel = target.channel;
      var latest = latestDriverMeasurement(group.id, channel.role);
      var measured = latest && latest.captured === true;
      var note = measured ? 'Measured' :
        (latest && latest.outcome ?
          'Last check: ' + outputResultLabel(latest.outcome) :
          'Needs one successful driver test');
      return '<div class="active-speaker-progress__row">' +
        '<div><strong>' + escapeHtml(driverTargetLabel(group, channel)) + '</strong>' +
          '<span>' + escapeHtml(note) + '</span></div>' +
        '<span class="status-pill' + (measured ? ' status-pill--ready' : '') + '">' +
          escapeHtml(measured ? 'measured' : 'remaining') + '</span>' +
      '</div>';
    }).join('');
    return '<div class="output-card output-card--measurements">' +
      '<div class="output-card__head"><div><p class="output-card__title">Driver checks</p>' +
        '<p class="setting-row__hint">Each active driver needs one successful quiet test before the combined-speaker check.</p></div>' +
        '<span class="status-pill' + (captured >= required && required > 0 ? ' status-pill--ready' : '') + '">' +
          escapeHtml(captured + '/' + required) + '</span></div>' +
      '<div class="active-speaker-progress">' + rows + '</div>' +
    '</div>';
  }
  function renderSummedValidationCard(topology) {
    var groups = activeOutputGroups(topology);
    if (!groups.length) return '';
    var canRecord = driverMeasurementsComplete();
    var rows = groups.map(function(group) {
      var latest = latestSummedValidation(group.id);
      var latestTest = latestSummedTest(group.id);
      var ok = latest && latest.validated === true;
      var hasAudibleTest = latestTest && latestTest.captured === true &&
        latestTest.audio_emitted === true && !playbackHasBlocker(latestTest);
      var statusText = ok ? 'validated' :
        (hasAudibleTest ? 'ready to record' : 'not tested');
      var testButton = '<button type="button" class="btn btn--primary" ' +
        'data-act="prepare-summed-test" data-group-id="' + escapeHtml(group.id) + '"' +
        ' data-label="' + escapeHtml(group.label || group.id || 'speaker') + '"' +
        (canRecord ? '' : ' disabled') + '>Play combined test</button>';
      var buttons = [
        ['blend_ok', 'Blend sounds right', 'btn--primary'],
        ['needs_adjustment', 'Needs adjustment', 'btn--ghost'],
        ['polarity_or_delay_problem', 'Sounds hollow or thin', 'btn--ghost'],
        ['too_loud', 'Too loud', 'btn--danger']
      ].map(function(item) {
        return '<button type="button" class="btn ' + escapeHtml(item[2]) +
          '" data-act="record-summed-validation" data-group-id="' + escapeHtml(group.id) +
          '" data-summed-test-id="' + escapeHtml(
            latestTest && (latestTest.summed_test_id || latestTest.playback_id) || ''
          ) +
          '" data-outcome="' + escapeHtml(item[0]) + '"' +
          (hasAudibleTest ? '' : ' disabled') + '>' + escapeHtml(item[1]) + '</button>';
      }).join('');
      var hint = hasAudibleTest ?
        'After the combined test, record whether the drivers blend.' :
        (canRecord ?
          'Run the combined speaker test first. It uses the saved crossover setup and starts at the quiet test level.' :
          'Measure each driver first, then test the combined speaker.');
      return '<div class="active-speaker-validation__group">' +
        '<div class="row-between">' +
          '<div><p class="setting-row__title">' + escapeHtml(group.label || group.id || 'Speaker') + '</p>' +
          '<p class="setting-row__hint">' + escapeHtml(hint) + '</p></div>' +
          '<span class="status-pill' + (ok ? ' status-pill--ready' : '') + '">' +
            escapeHtml(statusText) + '</span>' +
        '</div>' +
        '<div class="active-speaker-actions">' + testButton + buttons + '</div>' +
      '</div>';
    }).join('');
    return '<div class="output-card output-card--summed-validation">' +
      '<div class="output-card__head"><div><p class="output-card__title">Combined crossover check</p>' +
        '<p class="setting-row__hint">' + escapeHtml(canRecord ?
          'Use the same quiet-start pattern and your listening check for the combined speaker.' :
          'Test each driver first, then validate the combined crossover.') + '</p></div>' +
        '<span class="status-pill' + (summedValidationComplete() ? ' status-pill--ready' : '') + '">' +
          escapeHtml(summedValidationComplete() ? 'ready' : (canRecord ? 'next' : 'after driver checks')) + '</span></div>' +
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
  function renderBaselineProfileCard() {
    var profile = activeSpeaker.baselineProfile || {};
    var statusValue = profile.status || 'not_saved';
    var config = profile.config || {};
    var permissions = profile.permissions || {};
    var applied = statusValue === 'applied';
    var readyToApply = permissions.may_apply === true;
    var mayCompile = summedValidationComplete();
    var applyBlocked = baselineProfileApplyBlocked(profile);
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
        '<p class="setting-row__hint">The active profile was saved for review, but this hardware path cannot be applied from this page yet.</p>' :
      (readyToApply ?
        '<p class="setting-row__hint">Your active speaker profile is saved. Apply it to start using it.</p>' :
        '<p class="setting-row__hint">' + escapeHtml(mayCompile ?
          'Save the measured crossover as your active speaker profile. No sound plays.' :
          'Finish the combined crossover check before saving the active profile.') + '</p>'));
    var actions = applied ? '' :
      '<div class="active-speaker-actions active-speaker-profile-actions">' +
        '<button type="button" class="btn btn--primary" data-act="compile-baseline-profile"' +
          (mayCompile ? '' : ' disabled') + '>' + escapeHtml(
            readyToApply || applyBlocked ? 'Rebuild profile' : 'Save active profile'
          ) + '</button>' +
        '<button type="button" class="btn btn--danger" data-act="apply-baseline-profile"' +
          (readyToApply ? '' : ' disabled') + '>Apply active profile</button>' +
      '</div>';
    return '<div class="output-card output-card--baseline-profile">' +
      '<div class="output-card__head"><div><p class="output-card__title">Active speaker profile</p>' +
        '<p class="setting-row__hint">Your active speaker profile, built from the saved crossover and driver checks.</p></div>' +
        '<span class="status-pill' + (applied || readyToApply ? ' status-pill--ready' : '') + '">' +
          escapeHtml(applied ? 'active' : (readyToApply ? 'saved' : (applyBlocked ? 'saved for review' : 'not saved'))) + '</span></div>' +
      body +
      (issueRows && mayCompile ? '<ul class="active-speaker-issues active-speaker-issues--warning">' + issueRows + '</ul>' : '') +
      actions +
    '</div>';
  }
  function quietStartTargetLabel(target) {
    target = target || {};
    var pieces = [];
    if (target.speaker_group_id) pieces.push(String(target.speaker_group_id));
    if (target.role) pieces.push(humanRole(target.role).toLowerCase());
    if (target.output_index != null && isFinite(Number(target.output_index))) {
      pieces.push('output ' + (Number(target.output_index) + 1));
    }
    return pieces.join(' ');
  }
  function quietStartLabel(session) {
    var quiet = session && session.quiet_start || {};
    if (session && session.status !== 'armed') return 'Getting ready';
    if (quiet.status === 'floor_confirmed' && quiet.floor_audio_confirmed) {
      var targetLabel = quietStartTargetLabel(quiet.current_target);
      return targetLabel ? 'Heard ' + targetLabel : 'Heard at the starting level';
    }
    if (quiet.status === 'floor_pending_operator') {
      var pendingTargetLabel = quietStartTargetLabel(quiet.current_target);
      return pendingTargetLabel ? 'Waiting on what you heard for ' + pendingTargetLabel : 'Waiting on what you heard';
    }
    return 'Ready to start';
  }
  function readinessTargetLockReason(readiness) {
    var target = readiness && readiness.target || {};
    var audible = readiness && readiness.audible_test || {};
    if (target.role === 'tweeter') {
      return audible.target_role_allowed === false ?
        'Choose this driver again so JTS can start it quiet.' : '';
    }
    if (audible.target_role_allowed === false) {
      return 'This driver cannot be tested from here yet. Choose a woofer, mid, or subwoofer driver to continue.';
    }
    return '';
  }
  function friendlySetupReason(raw) {
    var text = String(raw || '').trim();
    if (!text) return 'One setup item needs to be finished.';
    var lower = text.toLowerCase();
    if (lower.indexOf('audible driver tests are not wired') >= 0 ||
        lower.indexOf('audible driver tests are not enabled') >= 0 ||
        lower.indexOf('require explicit') >= 0 ||
        lower.indexOf('lab backend') >= 0 ||
        lower.indexOf('audio_backend_not_enabled') >= 0) {
      return 'Driver tests are not available on this install yet.';
    }
    if (lower.indexOf('device or resource busy') >= 0 ||
        lower.indexOf('aplay failed') >= 0 ||
        lower.indexOf('tone playback backend failed') >= 0) {
      return 'The speaker audio device was busy. Stop other playback, then try the quiet driver test again.';
    }
    if ((lower.indexOf('safe test') >= 0 && lower.indexOf('not open') >= 0) ||
        lower.indexOf('safe_session_not_armed') >= 0 ||
        lower.indexOf('safe session must be armed') >= 0) {
      return 'JTS needs to prepare this driver again. Choose the driver, then press Start.';
    }
    if (lower.indexOf('rollback_target_available') >= 0 ||
        lower.indexOf('current_config_missing') >= 0 ||
        lower.indexOf('current_config_unreadable') >= 0 ||
        lower.indexOf('current config path') >= 0) {
      return 'JTS could not find the current audio profile it needs to restore after testing. Refresh the page, then choose the driver again.';
    }
    if (lower.indexOf('rollback_target_restore_limited') >= 0 ||
        lower.indexOf('volume_limit_missing') >= 0 ||
        lower.indexOf('volume_limit_positive') >= 0 ||
        lower.indexOf('unknown_custom_camilla_config') >= 0) {
      return 'The current sound profile cannot be used for testing. Save a normal JTS sound profile, then choose the driver again.';
    }
    if (lower.indexOf('route_verified') >= 0 ||
        lower.indexOf('protected_by_active_baseline') >= 0 ||
        lower.indexOf('bypass_disabled') >= 0 ||
        lower.indexOf('path-safety evidence was not provided') >= 0 ||
        lower.indexOf('staged protected candidate') >= 0 ||
        lower.indexOf('active_startup_candidate') >= 0) {
      return 'JTS could not get the test ready. Save the speaker layout and crossover settings, then choose the driver again.';
    }
    if (lower.indexOf('protection') >= 0 || lower.indexOf('high_frequency') >= 0 ||
        lower.indexOf('high-frequency') >= 0) {
      return 'JTS needs to save the quiet limit for that high-frequency driver before any tone can play. Save the crossover settings, then choose the driver again.';
    }
    if (lower.indexOf('path_safety') >= 0 || lower.indexOf('safety evidence') >= 0 ||
        lower.indexOf('evidence') >= 0 || lower.indexOf('protected path') >= 0) {
      return 'JTS could not get this driver test ready. Choose the driver again to retry. No sound was played.';
    }
    if (lower.indexOf('crossover_preview') >= 0 || lower.indexOf('crossover preview') >= 0) {
      return 'Save the crossover settings, then choose this driver again. No sound was played.';
    }
    if (lower.indexOf('startup') >= 0 || lower.indexOf('staged') >= 0 ||
        lower.indexOf('camilla') >= 0) {
      return 'JTS could not get the test ready. No sound was played.';
    }
    if (lower.indexOf('floor') >= 0 || lower.indexOf('calibration') >= 0) {
      return 'Choose the driver again so JTS can restart this test from the quietest level.';
    }
    if (lower.indexOf('target requires speaker_group_id') >= 0 ||
        lower.indexOf('target requires') >= 0) {
      return 'Choose the driver you want to test first.';
    }
    if (lower.indexOf('physical output') >= 0 ||
        lower.indexOf('output identity') >= 0 ||
        lower.indexOf('unverified') >= 0) {
      return 'Confirm which DAC output goes to each driver, then choose the driver again.';
    }
    // Never echo raw backend codes to the user. A snake_case identifier we did
    // not map above collapses to one calm, actionable sentence; already-readable
    // backend copy (a normal sentence) passes through unchanged.
    if (text.indexOf('_') >= 0) {
      return 'One setup step still needs finishing. Choose the driver again to continue.';
    }
    return text;
  }
  function friendlySetupIssue(issue) {
    issue = issue || {};
    return friendlySetupReason(issue.message || issue.label || issue.code);
  }
  function readinessBlockedReasons(readiness) {
    var reasons = [];
    var gates = Array.isArray(readiness.required_gates) ? readiness.required_gates : [];
    var issues = Array.isArray(readiness.issues) ? readiness.issues : [];
    gates.forEach(function(gate) {
      if (!gate || gate.passed) return;
      reasons.push(friendlySetupReason(gate.message || gate.label || gate.id));
    });
    issues.forEach(function(issue) {
      if (!issue) return;
      reasons.push(friendlySetupIssue(issue));
    });
    var lockReason = readinessTargetLockReason(readiness);
    if (lockReason) {
      reasons.unshift(lockReason);
    }
    return reasons.filter(function(reason, index, arr) {
      return reason && arr.indexOf(reason) === index;
    }).slice(0, 6);
  }
  function renderOutputReadinessSummary(readiness) {
    var target = readiness.target || {};
    var level = readiness.calibration_level && readiness.calibration_level.test_signal || {};
    var rows = [
      ['DAC output', target.physical_output_index == null ? 'unknown' : 'Output ' + (Number(target.physical_output_index) + 1)],
      ['Test level', level.requested_level_dbfs == null ? fmtDbfs(activeSpeakerLevelConfig().value) : fmtDbfs(level.requested_level_dbfs)]
    ];
    return '<dl class="active-speaker-facts output-readiness-summary">' + rows.map(function(row) {
      return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
    }).join('') + '</dl>';
  }
  function renderQuietTestTargetChoices() {
    if (outputTopology.dirty || !outputIdentityComplete()) return '';
    var topology = currentOutputTopology();
    var groups = outputGroups(topology);
    var buttons = [];
    groups.forEach(function(group) {
      (Array.isArray(group.channels) ? group.channels : []).forEach(function(channel) {
        if (!channel.identity_verified || channel.physical_output_index == null) return;
        var label = channel.human_output_label ||
          ('Output ' + (Number(channel.physical_output_index) + 1));
        var targetLabel = (group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label;
        var measured = driverMeasurementCaptured(group.id, channel.role);
        buttons.push('<button type="button" class="btn btn--ghost" data-act="check-output-readiness" ' +
          'data-group-id="' + escapeHtml(group.id) + '" ' +
          'data-role="' + escapeHtml(channel.role) + '" ' +
          'data-output-index="' + escapeHtml(channel.physical_output_index == null ? '' : String(channel.physical_output_index)) + '" ' +
          'data-speaker-label="' + escapeHtml(group.label || group.id) + '" ' +
          'data-label="' + escapeHtml(targetLabel) + '"' +
          (measured ? ' disabled' : '') + '>' +
          escapeHtml(measured ? '✓ ' + humanRole(channel.role) + ' confirmed' : 'Test ' + humanRole(channel.role)) +
          ' · ' + escapeHtml(label) + '</button>');
      });
    });
    if (!buttons.length) return '';
    return '<div class="active-speaker-actions active-speaker-actions--targets">' +
      buttons.join('') +
    '</div>';
  }
  function renderOutputReadinessBlockers(readiness) {
    var reasons = readinessBlockedReasons(readiness);
    if (!reasons.length) {
      if (readiness && readiness.preconditions_passed && readiness.playback_allowed !== true) {
        return '<p class="setting-row__hint">This install cannot play driver tests from this page yet.</p>';
      }
      return '<p class="setting-row__hint">Ready to start at the quietest test level. Press Stop immediately if anything sounds wrong.</p>';
    }
    return '<div class="output-readiness-blockers">' +
      '<p class="setting-row__title">How to continue</p>' +
      '<ul class="active-speaker-issues active-speaker-issues--warning">' + reasons.map(function(reason) {
        return '<li>' + escapeHtml(reason) + '</li>';
      }).join('') + '</ul>' +
    '</div>';
  }
  function renderOutputReadinessCard() {
    if (outputTopology.dirty) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Choose first driver</p>' +
        '<p class="setting-row__hint">Save the speaker layout before choosing a driver for the first test.</p></div>' +
        '<span class="status-pill">draft</span></div>' +
      '</div>';
    }
    if (outputTopology.readinessChecking) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Getting the test ready</p>' +
        '<p class="setting-row__hint">JTS is checking the saved setup for the selected driver. No sound is playing.</p></div>' +
        '<span class="status-pill">preparing</span></div>' +
      '</div>';
    }
    if (outputTopology.readinessError) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Finish setup before testing</p>' +
        '<p class="setting-row__hint">No sound played. Use the message below, then choose the driver again.</p></div>' +
        '<span class="status-pill">not ready yet</span></div>' +
        '<p class="setting-row__hint">' + escapeHtml(outputTopology.readinessError) + '</p>' +
        renderQuietTestTargetChoices() +
      '</div>';
    }
    var readiness = outputTopology.readiness;
    if (!readiness) {
      var choices = renderQuietTestTargetChoices();
      var counts = driverMeasurementCounts();
      var hasProgress = counts.captured > 0;
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">' +
          escapeHtml(hasProgress ? 'Choose next driver' : 'Choose first driver') + '</p>' +
        '<p class="setting-row__hint">' + escapeHtml(choices ?
          (hasProgress ?
            'The completed driver is checked off. Choose the next driver to hear.' :
            'Choose the driver you want to hear first. JTS will prepare the quiet test before any sound can play.') :
          'Confirm outputs first, then choose one driver to test.') + '</p></div>' +
        '<span class="status-pill">' + escapeHtml(choices ? 'next' : 'confirm outputs') + '</span></div>' +
        choices +
      '</div>';
    }
    var target = readiness.target || {};
    var audioEnabled = readiness.playback_allowed === true;
    var statusValue = readiness.preconditions_passed ?
      (audioEnabled ? 'ready to test' : 'not available') :
      'not ready yet';
    var nextStep = readiness.preconditions_passed && !audioEnabled ?
      'This install cannot play driver tests from this page yet.' :
      friendlySetupReason(readiness.next_step || 'No sound was played.');
    var showAudibleControls = activeSpeakerAudibleTestReady();
    return '<div class="output-card output-card--readiness">' +
      '<div class="output-card__head"><div><p class="output-card__title">Listen for this driver</p>' +
        '<p class="setting-row__hint">' + escapeHtml(target.label || 'Selected channel') + '</p></div>' +
        '<span class="status-pill' + (readiness.preconditions_passed && audioEnabled ? ' status-pill--ready' : '') + '">' +
          escapeHtml(statusValue) + '</span></div>' +
      renderOutputReadinessSummary(readiness) +
      renderOutputReadinessBlockers(readiness) +
      (showAudibleControls ? renderActiveSpeakerLevel() :
        '<p class="setting-row__hint">' + escapeHtml(nextStep) + '</p>') +
      renderOutputReadinessActions(readiness) +
      renderOutputReadinessPlayback(outputTopology.readinessPlayback) +
    '</div>';
  }
  function renderOutputReadinessActions(readiness) {
    var target = readiness && readiness.target || {};
    var lockReason = readinessTargetLockReason(readiness);
    var baseDisabled = !readiness || !readiness.preconditions_passed || !!lockReason ||
      outputTopology.readinessPlaybackChecking;
    var audioEnabled = readiness && readiness.playback_allowed === true;
    var rampRunning = outputAudibleRampMatches(readiness);
    var pendingPlayback = outputFloorAudioPendingForPlayback(outputTopology.readinessPlayback) ?
      outputTopology.readinessPlayback : null;
    var latestPlayback = outputLatestAudiblePlayback(readiness);
    var playbackId = pendingPlayback && pendingPlayback.playback_id ||
      latestPlayback && latestPlayback.playback_id ||
      (rampRunning ? outputAudibleRamp.lastPlaybackId : '') ||
      '';
    var resultMode = rampRunning || !!pendingPlayback || !!latestPlayback;
    var audioDisabled = baseDisabled || rampRunning;
    var primaryDisabled = resultMode ? !playbackId : audioDisabled;
    var resultDisabled = !playbackId;
    var stopDisabled = !rampRunning;
    var attrs = 'data-group-id="' + escapeHtml(target.speaker_group_id || '') + '" ' +
      'data-role="' + escapeHtml(target.role || '') + '" ' +
      'data-label="' + escapeHtml(target.label || '') + '"';
    var hints = [];
    if (lockReason) hints.push(lockReason);
    if (readiness && readiness.preconditions_passed && !audioEnabled) {
      hints.push('Driver tests are not available on this install yet.');
    }
    var primaryButton = resultMode ?
      '<button type="button" class="btn btn--primary" data-act="active-floor-result" ' +
        'data-outcome="heard_correct_driver" data-playback-id="' + escapeHtml(playbackId) + '"' +
        (primaryDisabled ? ' disabled' : '') + '>I hear this driver</button>' :
      '<button type="button" class="btn btn--primary" data-act="play-output-readiness-tone" ' +
        attrs + ' data-audio="true"' + (primaryDisabled ? ' disabled' : '') +
        '>Start quiet test</button>';
    return hints.map(function(hint) {
      return '<p class="setting-row__hint">' + escapeHtml(hint) + '</p>';
    }).join('') +
      '<div class="active-speaker-actions active-speaker-actions--driver-test">' +
      (audioEnabled ? primaryButton +
        '<button type="button" class="btn btn--ghost" data-act="active-floor-result" ' +
        'data-outcome="heard_wrong_driver" data-playback-id="' + escapeHtml(playbackId) + '"' +
        (resultDisabled ? ' disabled' : '') + '>Wrong driver</button>' +
        '<button type="button" class="btn btn--danger" data-act="stop-active-speaker"' +
        (stopDisabled ? ' disabled' : '') + '>Stop</button>' :
        '<span class="setting-row__hint">Driver tests are not available on this install yet.</span>') +
    '</div>';
  }
  function renderOutputReadinessPlayback(playback) {
    if (!playback) return '';
    var emitted = playbackConfirmable(playback);
    var message = playbackResultMessage(playback, '', friendlySetupReason);
    if (emitted) return '';
    return message ? '<p class="setting-row__hint">No sound played. ' +
      escapeHtml(message) + '</p>' : '';
  }
  function activeSpeakerLevelConfig() {
    var contract = activeSpeaker.calibrationLevel || {};
    var raw = contract.test_signal || {};
    var min = Number(raw.min_level_dbfs);
    var max = Number(raw.max_level_dbfs);
    var step = Number(raw.step_db);
    var def = Number(raw.default_level_dbfs);
    if (!isFinite(min)) min = -80;
    if (!isFinite(max)) max = -45;
    if (!isFinite(step) || step <= 0) step = 1;
    if (!isFinite(def)) def = min;
    var value = Number(activeSpeaker.levelDbfs);
    if (!isFinite(value)) value = def;
    value = clamp(value, min, max);
    return {min: min, max: max, step: step, def: def, value: value};
  }
  function activeSpeakerSelectedReadinessTarget() {
    var target = outputTopology.readiness && outputTopology.readiness.target || null;
    if (!target || !target.speaker_group_id || !target.role) return null;
    return target;
  }
  function renderActiveSpeakerLevel() {
    var cfg = activeSpeakerLevelConfig();
    var contract = activeSpeaker.calibrationLevel || {};
    var issues = Array.isArray(contract.issues) ? contract.issues : [];
    var readiness = outputTopology.readiness || {};
    var target = readiness.target || {};
    var running = outputAudibleRampMatches(readiness);
    var level = running && isFinite(Number(outputAudibleRamp.levelDbfs)) ?
      Number(outputAudibleRamp.levelDbfs) : cfg.value;
    var levelPct = outputRampProgressPct(level);
    var role = humanRole(target.role || 'driver').toLowerCase();
    var hint = running ?
      ('Playing short ' + role + ' pulses. Press “I hear this driver” as soon as you hear it.') :
      ('JTS starts very quiet and automatically tries a little louder until you hear it or the safe limit is reached.');
    return '<div class="active-speaker-level">' +
      '<div class="row-between active-speaker-level__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Test progress</p>' +
          '<p class="setting-row__hint">Listen only for ' + escapeHtml(target.label || role) + '.</p>' +
        '</div>' +
        '<span class="active-speaker-level__readout" id="active-speaker-level-readout">' +
          escapeHtml(fmtDbfs(level)) + '</span>' +
      '</div>' +
      '<div class="active-speaker-level__bar' + (running ? ' active-speaker-level__bar--running' : '') + '">' +
        '<span aria-hidden="true" style="width:' + clamp(levelPct, 0, 100).toFixed(1) + '%"></span>' +
      '</div>' +
      '<div class="active-speaker-meter">' +
        '<span class="active-speaker-meter__label">Very quiet</span>' +
        '<span class="active-speaker-meter__label">Ramping</span>' +
        '<span class="active-speaker-meter__label">Safe limit</span>' +
      '</div>' +
      '<p class="setting-row__hint">' + escapeHtml(outputAudibleRamp.message || hint) + '</p>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.slice(0, 3).map(function(issue) {
        return '<li>' + escapeHtml(activeSpeakerLevelIssueMessage(issue)) + '</li>';
      }).join('') + '</ul>' : '') +
    '</div>';
  }
  function activeSpeakerLevelIssueMessage(issue) {
    var code = issue && issue.code || '';
    if (code === 'upward_step_limited') {
      return 'JTS moved to the next safe volume step. Try this level before raising it again.';
    }
    if (code === 'audible_ramp_step_limited') {
      return 'JTS moved to the next safe volume step. Try this level before raising it again.';
    }
    if (code === 'level_clamped') {
      return 'Volume was kept inside the safe test range.';
    }
    if (code === 'floor_required') {
      return 'Start this driver at the quietest level first.';
    }
    return 'JTS adjusted the test volume to stay inside the safe range.';
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
      '</div></div>' + bandsContent + '</section>';

    el('view-body').innerHTML = '<div>' + modeSection + bandsSection +
      '<section class="draft-footer">' + footerHtml() + '</section></div>';
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
        if (payload.dsp_write_epoch) dspWriteEpoch = payload.dsp_write_epoch;
        if (payload.live_status === 'live') status('Listening to this draft live.');
        else if (payload.live_status === 'stale') status('Speaker DSP changed — move a control again to hear this draft.');
        else status('Live preview unavailable on this CamillaDSP connection.', true);
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
      ingestState(payload);
      if (sourceSeq === liveSourceSeq) status(okMsg || '');
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

  // Global sound settings (match-loudness, headroom). Optimistic: the controls
  // already show the user's input, so on success we just ingest (audio is
  // re-applied server-side); on failure we revert and re-render.
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
    } catch (e) {
      soundSettings = prev;
      status('Could not save sound settings: ' + e.message, true);
      render();
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
  ['off', 'saved', 'draft'].forEach(function(v) {
    el('tab-' + v).addEventListener('click', function() { if (view !== v) setView(v); });
  });
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
    else if (act === 'copy-driver-research-prompt') { copyDriverResearchPrompt(); }
    else if (act === 'parse-driver-research') { parseDriverResearchImport(); }
    else if (act === 'save-driver-design') { saveDriverResearchDraft(); }
    else if (act === 'prepare-crossover-preview') { prepareCrossoverPreview(); }
    else if (act === 'mark-output-identity') { updateOutputChannelIdentity(t); }
    else if (act === 'check-output-readiness') { checkOutputPlaybackReadiness(t); }
    else if (act === 'play-output-readiness-tone') { playOutputReadinessTone(t); }
    else if (act === 'active-floor-result') { recordFloorAudioResult(t); }
    else if (act === 'prepare-summed-test') { prepareSummedTest(t); }
    else if (act === 'record-summed-validation') { recordSummedValidation(t); }
    else if (act === 'compile-baseline-profile') { compileBaselineProfile(); }
    else if (act === 'apply-baseline-profile') { applyBaselineProfile(); }
    else if (act === 'stop-active-speaker') { stopActiveSpeakerTest(); }
    else if (act === 'commission-arm') { commissionArm(t.getAttribute('data-role') || ''); }
    else if (act === 'commission-step') { commissionStep(t.getAttribute('data-role') || ''); }
    else if (act === 'commission-ack') { commissionAck(t.getAttribute('data-outcome') || ''); }
    else if (act === 'commission-abort') { commissionAbort(); }
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
      updateDriverResearchPromptPreview();
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
    if (ev.target.id === 'set-match-loudness') saveSettings({match_loudness: ev.target.checked});
    else if (ev.target.id === 'set-headroom') saveSettings({headroom_trim_db: Number(ev.target.value)});
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
    outputTopology.error = '';
    outputTopology.dirty = false;
    outputTopology.saving = false;
    outputTopology.loading = false;
    outputTopology.identitySaving = '';
    outputTopology.protectionSaving = '';
    outputTopology.readiness = null;
    outputTopology.readinessChecking = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
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
  async function postCommission(url, body, busyLabel) {
    patchActiveSpeaker({commissionBusy: busyLabel, commissionError: ''});
    render();
    try {
      var resp = await fetch(url, {
        method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify(body || {})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error((payload && payload.error) || 'request failed');
    } catch (e) {
      patchActiveSpeaker({commissionBusy: '', commissionError: String(e.message || e)});
      render();
      return;
    }
    await refreshCommissionState();
    patchActiveSpeaker({commissionBusy: ''});
    render();
  }
  async function commissionArm(role) {
    var group = activeCommissionGroup(currentOutputTopology());
    if (!group || !role) return;
    await postCommission('./active-speaker/commission-load',
      {group: group.id, role: role}, 'Arming ' + humanRole(role));
  }
  async function commissionStep(role) {
    var group = activeCommissionGroup(currentOutputTopology());
    if (!group || !role) return;
    var ok = await jtsConfirm('Make the ' + humanRole(role) + ' audible? Amps should be ' +
      'on at LOW gain — JTS will play it very quietly through the crossover.',
      {danger: true});
    if (!ok) return;
    await postCommission('./active-speaker/commission-ramp-step',
      {group: group.id, role: role}, 'Stepping ' + humanRole(role));
  }
  async function commissionAck(outcome) {
    if (!outcome) return;
    await postCommission('./active-speaker/commission-ramp-ack',
      {outcome: outcome}, 'Recording');
  }
  async function commissionAbort() {
    await postCommission('./active-speaker/commission-ramp-abort', {}, 'Re-muting');
  }
  function setOutputDraft(next) {
    outputTopology.draft = next;
    if (outputGroups(next).length) outputTemplateDraftAxes = {layout: '', speakerMode: ''};
    outputTopology.dirty = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    outputTopology.readiness = null;
    outputTopology.readinessChecking = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    driverResearch.dirty = true;
    crossoverPreview.payload = null;
    crossoverPreview.error = '';
    patchActiveSpeaker({stagedConfig: null});
    render();
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
  function updateDriverResearchImportSummary() {
    var summary = el('driver-research-import-summary');
    if (summary) summary.innerHTML = renderDriverResearchSummary();
  }
  async function copyDriverResearchPrompt() {
    var prompt = el('driver-research-prompt');
    if (!prompt) return;
    var copied = false;
    try {
      await navigator.clipboard.writeText(prompt.value);
      copied = true;
    } catch (e) {
      try {
        prompt.select();
        copied = document.execCommand('copy');
      } catch (fallbackError) {
        copied = false;
      }
    }
    status(copied ? 'Copied driver research prompt.' :
      'Could not copy automatically. Select the prompt text and copy it manually.', !copied);
  }
  function parseDriverResearchImport() {
    try {
      var payload = JSON.parse(driverResearch.importText || '');
      driverResearch.parsed = summarizeDriverResearchPayload(payload);
      applyDriverResearchToManualSettings(payload);
      driverResearch.error = '';
      driverResearch.dirty = true;
      status('Imported driver research. Review the visible crossover settings before saving.');
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
        'Crossover settings are already saved. Continue with output mapping.' :
        'Driver details are optional for now. Continue with output mapping.');
      render();
      return true;
    }
    if (outputTopology.dirty) {
      status('Save the speaker layout before saving crossover settings.', true);
      return false;
    }
    if (!currentOutputTopology()) {
      status('Load output hardware before saving a speaker design draft.', true);
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
      status(importWarning
        ? 'Saved visible crossover settings. Imported JSON was not saved.'
        : 'Saved crossover settings. No filters were applied and no sound was played.');
      render();
      return true;
    } catch (e) {
      driverResearch.saving = false;
      driverResearch.error = e.message;
      status('Could not save speaker design draft: ' + e.message, true);
      render();
      return false;
    }
  }
  async function prepareCrossoverPreview() {
    if (outputTopology.dirty) {
      status('Save the speaker layout before preparing the crossover preview.', true);
      return false;
    }
    if (!driverResearchCanPreparePreview()) {
      status('Save crossover settings before preparing the preview.', true);
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
      status('Crossover preview ready. No filters were applied and no sound was played.');
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
      if (!await saveDriverResearchDraft({nextStep: 'map'})) return;
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
      if (driverMeasurementsComplete()) {
        openOutputStep('profile');
        status('Driver checks are saved. Continue with the combined crossover check.');
        return;
      }
      outputStepOverride = 'safety';
      status('Measure each active driver before saving the active profile.');
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
        body: JSON.stringify({output_topology: outputTopology.draft})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker layout save failed');
      ingestOutputTopology(payload);
      if (options.nextStep) outputStepOverride = options.nextStep;
      status('Saved speaker layout. No sound was played.');
    } catch (e) {
      outputTopology.saving = false;
      outputTopology.error = e.message;
      status('Could not save speaker layout: ' + e.message, true);
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
      ? 'Confirm that "' + label + '" is wired to the driver shown here? No sound will play.'
      : 'Mark "' + label + '" as not confirmed?';
    if (!await jtsConfirm(message, {danger: false})) return;

    outputTopology.identitySaving = groupId + ':' + role;
    outputTopology.error = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
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
      status((verified ? 'Confirmed output: ' : 'Cleared output confirmation: ') + label + '.');
    } catch (e) {
      outputTopology.identitySaving = '';
      outputTopology.error = e.message;
      status('Could not update channel identity: ' + e.message, true);
    }
    render();
  }
  function syncPreparedOutputTopology(payload) {
    if (!payload || !payload.output_topology) return;
    outputTopology.payload = clone(payload.output_topology);
    outputTopology.draft = clone(payload.output_topology);
    outputTopology.identity = payload.channel_identity || outputTopology.identity || null;
    outputTopology.clockDomain = payload.clock_domain || outputTopology.clockDomain || null;
    outputTopology.activeRoute = payload.active_playback_route || outputTopology.activeRoute || null;
    outputTopology.error = '';
    outputTopology.dirty = false;
    outputTopology.saving = false;
    outputTopology.loading = false;
    outputTopology.identitySaving = '';
    outputTopology.protectionSaving = '';
  }
  function outputReadinessFromPreparedDriver(payload, button) {
    payload = payload || {};
    var preparedTarget = payload.target || {};
    var toneBackend = payload.tone_backend || {};
    var audioEnabled = toneBackend.audio_enabled === true;
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var outputIndex = Number(button.getAttribute('data-output-index'));
    var target = {
      speaker_group_id: preparedTarget.speaker_group_id || groupId,
      speaker_label: preparedTarget.speaker_label || button.getAttribute('data-speaker-label') || groupId,
      role: preparedTarget.role || preparedTarget.driver_role || role,
      physical_output_index: preparedTarget.physical_output_index != null ?
        preparedTarget.physical_output_index : (isFinite(outputIndex) ? outputIndex : null),
      label: preparedTarget.label || button.getAttribute('data-label') || (groupId + ' ' + role)
    };
    return {
      artifact_schema_version: 1,
      kind: 'jts_active_speaker_playback_readiness',
      status: 'preconditions_passed',
      preconditions_passed: true,
      playback_allowed: audioEnabled,
      would_play: false,
      tone_playback_implemented: audioEnabled,
      target: target,
      tone_backend: toneBackend,
      calibration_level: payload.calibration_level || activeSpeaker.calibrationLevel || {},
      safe_session: payload.session || activeSpeaker.session || {},
      startup_load: (payload.startup_load && payload.startup_load.load) ||
        (activeSpeaker.startupLoad || {}).state ||
        activeSpeaker.startupLoad ||
        {},
      required_gates: [],
      issues: [],
      next_step: audioEnabled ?
        (payload.message ||
          'Start at the quietest level. Press Stop immediately if anything sounds wrong.') :
        'This install cannot play driver tests from this page yet.'
    };
  }
  function refreshSelectedOutputReadiness(nextStep) {
    if (!outputTopology.readiness || !outputTopology.readiness.target) return;
    outputTopology.readiness = Object.assign({}, outputTopology.readiness, {
      calibration_level: activeSpeaker.calibrationLevel || outputTopology.readiness.calibration_level || {},
      safe_session: activeSpeaker.session || outputTopology.readiness.safe_session || {},
      startup_load: (activeSpeaker.startupLoad || {}).state ||
        activeSpeaker.startupLoad ||
        outputTopology.readiness.startup_load ||
        {},
      next_step: nextStep || outputTopology.readiness.next_step ||
        'Start at the quietest level. Press Stop immediately if anything sounds wrong.'
    });
  }
  function setupFailureMessage(payload, fallback) {
    payload = payload || {};
    var candidates = [];
    if (payload.error) candidates.push(payload.error);
    if (payload.message) candidates.push(payload.message);
    var report = payload.report || {};
    if (report.message) candidates.push(report.message);
    if (payload.load && payload.load.message) candidates.push(payload.load.message);
    [payload.issues, report.issues, payload.blockers].forEach(function(items) {
      if (!Array.isArray(items)) return;
      items.forEach(function(issue) {
        if (!issue) return;
        candidates.push(issue.message || issue.label || issue.code);
      });
    });
    var friendly = candidates.map(friendlySetupReason).filter(Boolean);
    return friendly[0] || fallback ||
      'JTS could not get this driver test ready. No sound was played.';
  }
  async function checkOutputPlaybackReadiness(button) {
    if (outputTopology.dirty) {
      status('Save the speaker layout before choosing a driver to test.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var targetId = groupId + ':' + role;
    outputTopology.readinessChecking = targetId;
    outputTopology.protectionSaving = '';
    outputTopology.error = '';
    outputTopology.readinessError = '';
    outputTopology.readiness = null;
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    outputTopology.touched = true;
    activeSpeaker.stagedConfig = null;
    status('Getting ' + label + ' ready. No sound will play yet.');
    render();
    try {
      var resp = await fetch('./active-speaker/prepare-driver-test', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          speaker_group_id: groupId,
          role: role
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(setupFailureMessage(
        payload,
        'JTS could not prepare this driver for testing. No sound was played.'
      ));
      syncPreparedOutputTopology(payload);
      if (!payload.ready) {
        throw new Error(setupFailureMessage(
          payload,
          'Finish the setup item shown here, then choose this driver again.'
        ));
      }
      var nextLevel = payload.calibration_level || activeSpeaker.calibrationLevel;
      patchActiveSpeaker({
        loading: false,
        action: '',
        error: '',
        session: payload.session || activeSpeaker.session,
        calibrationLevel: nextLevel,
        stagedConfig: payload.staged_config || activeSpeaker.stagedConfig,
        startupLoad: payload.startup_load || activeSpeaker.startupLoad,
        levelDbfs: nextLevel && nextLevel.test_signal ?
          Number(nextLevel.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs
      });
      outputTopology.readiness = outputReadinessFromPreparedDriver(payload, button);
      outputTopology.readinessChecking = '';
      status(outputTopology.readiness.playback_allowed === true ?
        ('Ready to test ' + label + '. Start at the quietest level.') :
        ('Driver tests are not available on this install yet.'));
    } catch (e) {
      outputTopology.protectionSaving = '';
      outputTopology.readinessChecking = '';
      outputTopology.readinessError = e.message;
      patchActiveSpeaker({
        loading: false,
        action: '',
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('No sound played. ' + e.message);
    }
    render();
  }
  function outputRampTargetFromButton(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    return {groupId: groupId, role: role, label: label};
  }
  function sleepMs(ms) {
    return new Promise(function(resolve) {
      window.setTimeout(resolve, Math.max(0, Number(ms) || 0));
    });
  }
  async function postOutputReadinessTone(target) {
    var resp = await fetch('./active-speaker/play-tone', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({
        speaker_group_id: target.groupId,
        role: target.role,
        audio: true
      })
    });
    var result = await resp.json();
    if (!resp.ok) throw new Error(result.error || 'channel test failed');
    return result;
  }
  function applyOutputToneResult(result, target, token) {
    if (token !== outputAudibleRamp.token) return null;
    outputTopology.readinessPlayback = result.playback || null;
    outputTopology.readinessPlaybackChecking = '';
    var playback = result.playback || {};
    var emitted = playbackConfirmable(playback);
    var tone = playback.tone || {};
    var level = Number(tone.level_dbfs);
    if (emitted && isFinite(level)) {
      outputAudibleRamp = Object.assign({}, outputAudibleRamp, {
        pulseCount: outputAudibleRamp.pulseCount + 1,
        lastPlaybackId: playback.playback_id || outputAudibleRamp.lastPlaybackId,
        levelDbfs: level,
        message: 'Listening for ' + target.label + '. Press “I hear this driver” as soon as you hear it.'
      });
    }
    var playbackMessage = playbackResultMessage(playback, undefined, friendlySetupReason);
    patchActiveSpeaker({
      loading: false, action: '',
      session: result.session || activeSpeaker.session,
      error: '',
      levelDbfs: isFinite(level) ? level : activeSpeaker.levelDbfs
    });
    refreshSelectedOutputReadiness(emitted ?
      'If you hear the selected driver, confirm it. If you hear nothing, JTS will try a little louder.' :
      playbackMessage);
    status(emitted ?
      'Playing quiet test pulses. Press Stop if anything sounds wrong.' :
      'No sound played. ' + playbackMessage,
      !emitted);
    return playback;
  }
  async function recordFloorAudioOutcome(outcome, playbackId, options) {
    options = options || {};
    if (!outcome || !playbackId) {
      status('JTS lost track of that test. Choose the driver again to retry.', true);
      return null;
    }
    var target = outputTopology.readiness && outputTopology.readiness.target || null;
    if (!options.silentAutoRetry) {
      stopOutputAudibleRamp(outcome === 'heard_wrong_driver' ?
        'Stopped. Check the DAC output mapping before trying again.' :
        '');
      patchActiveSpeaker({
        loading: false, action: 'Saving what you heard',
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      render();
    }
    var result = null;
    try {
      var resp = await fetch('./active-speaker/floor-audio-result', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          outcome: outcome,
          playback_id: playbackId
        })
      });
      result = await resp.json();
      if (!resp.ok) throw new Error(result.error || 'driver-test result failed');
      var measurementPayload = null;
      var measurementWarning = '';
      if (!options.silentAutoRetry && target && target.speaker_group_id && target.role) {
        try {
          measurementPayload = await postDriverMeasurement(target, outcome, playbackId);
          patchActiveSpeaker({measurements: measurementPayload});
        } catch (measurementErr) {
          measurementWarning = measurementErr.message || 'driver check save failed';
        }
      }
      patchActiveSpeaker({
        loading: false, action: '',
        session: result,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      if (!options.silentAutoRetry && target && target.speaker_group_id && target.role) {
        refreshSelectedOutputReadiness(outcome === 'heard_correct_driver' ?
          'That driver is confirmed. Continue with the next driver.' :
          'No sound is playing. Check the wiring or choose the driver again when ready.');
        outputTopology.readinessError = '';
      }
      if (options.silentAutoRetry) return result;
      var latest = target && measurementPayload ?
        (measurementPayload.summary || {}).latest_driver_measurements || {} : {};
      var measured = target && latest[measurementTargetId(target.speaker_group_id, target.role)] &&
        latest[measurementTargetId(target.speaker_group_id, target.role)].captured === true;
      if (measured && driverMeasurementsComplete()) {
        outputStepOverride = 'profile';
        outputTopology.readiness = null;
        outputTopology.readinessPlayback = null;
      } else if (measured && outcome === 'heard_correct_driver') {
        outputTopology.readiness = null;
        outputTopology.readinessPlayback = null;
      }
      status(measurementWarning ?
        'Driver result saved, but the driver check was not saved: ' + measurementWarning :
        (measured ?
          (driverMeasurementsComplete() ?
            'Both drivers are confirmed. Continue with the combined crossover check.' :
            'Driver confirmed. Choose the next driver.') :
          (outcome === 'heard_wrong_driver' ?
            'Stopped. Check the output mapping before trying again.' :
            'Saved. Choose the next driver when you are ready.')),
        outcome === 'heard_wrong_driver');
      return result;
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not record driver-test result: ' + e.message, true);
      if (options.silentAutoRetry) stopOutputAudibleRamp('Stopped because JTS could not record the last pulse.');
      return null;
    } finally {
      if (!options.silentAutoRetry) render();
    }
  }
  async function advanceOutputAudibleRampLevel(target, token) {
    var body = {
      action: 'auto_step',
      speaker_group_id: target.groupId,
      role: target.role
    };
    var resp = await fetch('./active-speaker/calibration-level', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(body)
    });
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'calibration level update failed');
    if (token !== outputAudibleRamp.token) return null;
    var accepted = payload && payload.test_signal ?
      Number(payload.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs;
    patchActiveSpeaker({
      loading: false,
      action: '',
      calibrationLevel: payload,
      error: '',
      levelDbfs: isFinite(accepted) ? accepted : activeSpeaker.levelDbfs
    });
    if (isFinite(accepted)) {
      outputAudibleRamp = Object.assign({}, outputAudibleRamp, {
        levelDbfs: accepted
      });
    }
    return payload;
  }
  function autoLevelReachedCap(payload) {
    var decision = payload && payload.auto_level || {};
    var action = String(decision.action || '').toLowerCase();
    var statusValue = String(decision.status || '').toLowerCase();
    return action !== 'raise' ||
      statusValue === 'maxed' ||
      Number(payload && payload.applied_delta_db || 0) <= 0;
  }
  async function runOutputAudibleRamp(target, token) {
    while (outputAudibleRamp.running && token === outputAudibleRamp.token) {
      outputTopology.readinessPlaybackChecking = 'audio';
      outputAudibleRamp = Object.assign({}, outputAudibleRamp, {
        message: outputAudibleRamp.pulseCount ?
          'Trying another short pulse a little louder.' :
          'Starting with the quietest short pulse.'
      });
      render();
      try {
        var result = await postOutputReadinessTone(target);
        var playback = applyOutputToneResult(result, target, token);
        render();
        if (!playbackConfirmable(playback)) {
          stopOutputAudibleRamp('No sound played. JTS could not start the driver test.');
          render();
          return;
        }
        await sleepMs(OUTPUT_RAMP_LISTEN_MS);
        if (!outputAudibleRamp.running || token !== outputAudibleRamp.token) return;
        if (outputFloorAudioPendingForPlayback(playback)) {
          await recordFloorAudioOutcome('silent', playback.playback_id, {
            silentAutoRetry: true
          });
        }
        if (!outputAudibleRamp.running || token !== outputAudibleRamp.token) return;
        var levelPayload = await advanceOutputAudibleRampLevel(target, token);
        if (!levelPayload || autoLevelReachedCap(levelPayload)) {
          stopOutputAudibleRamp(
            'Reached the safe test limit. If you still heard nothing, check amp power, wiring, and the DAC output mapping before trying again.'
          );
          status('Reached the safe test limit. Check wiring and amp power before trying again.', true);
          render();
          return;
        }
        await sleepMs(OUTPUT_RAMP_NEXT_PULSE_MS);
      } catch (e) {
        stopOutputAudibleRamp('Stopped. ' + e.message);
        outputTopology.readinessPlaybackChecking = '';
        status('Could not run the driver test: ' + e.message, true);
        render();
        return;
      }
    }
  }
  async function playOutputReadinessTone(button) {
    var target = outputRampTargetFromButton(button);
    var readiness = outputTopology.readiness || {};
    var audio = button.getAttribute('data-audio') === 'true';
    if (audio && readiness.playback_allowed !== true) {
      var backend = readiness.tone_backend || {};
      var message = playbackResultMessage(
        {
          status: 'blocked',
          issues: Array.isArray(backend.issues) && backend.issues.length ?
            backend.issues :
            [{code: 'audio_backend_not_enabled'}]
        },
        'Driver tests are not available on this install yet.',
        friendlySetupReason
      );
      refreshSelectedOutputReadiness(message);
      outputTopology.readinessError = '';
      outputTopology.touched = true;
      status('No sound played. ' + message, true);
      render();
      return;
    }
    if (!audio) return;
    var token = outputAudibleRamp.token + 1;
    outputAudibleRamp = {
      running: true,
      token: token,
      targetKey: outputTargetKey(readiness.target || target),
      lastPlaybackId: '',
      pulseCount: 0,
      levelDbfs: activeSpeaker.levelDbfs,
      message: 'Starting with the quietest short pulse.'
    };
    outputTopology.readinessPlaybackChecking = 'audio';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.touched = true;
    status('Starting quiet driver test. Press Stop if anything sounds wrong.');
    render();
    runOutputAudibleRamp(target, token);
  }
  async function recordFloorAudioResult(button) {
    var outcome = button.getAttribute('data-outcome') || '';
    var playbackId = button.getAttribute('data-playback-id') || '';
    await recordFloorAudioOutcome(outcome, playbackId);
  }
  function currentMicObservationPayload() {
    return {};
  }
  async function postDriverMeasurement(target, outcome, playbackId) {
    var body = Object.assign({
      speaker_group_id: target.speaker_group_id,
      role: target.role,
      outcome: outcome,
      playback_id: playbackId,
      test_level_dbfs: activeSpeaker.levelDbfs
    }, currentMicObservationPayload());
    var resp = await fetch('./active-speaker/driver-measurement', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(body)
    });
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'driver check save failed');
    return payload;
  }
  async function prepareSummedTest(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var label = button.getAttribute('data-label') || groupId || 'speaker';
    if (!groupId) {
      status('Choose the speaker group to test.', true);
      return;
    }
    if (!await jtsConfirm(
      'Play a short quiet combined test for "' + label +
        '"? JTS uses the saved crossover and the same bounded test level.',
      {danger: true}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Starting combined test',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/summed-test', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          speaker_group_id: groupId,
          audio: true,
          duration_ms: 500
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'combined speaker test failed');
      patchActiveSpeaker({
        loading: false,
        action: '',
        session: payload.session || activeSpeaker.session,
        measurements: payload.measurements || activeSpeaker.measurements,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      var playback = payload.playback || {};
      var emitted = playbackConfirmable(playback);
      status(emitted ?
        'Combined speaker test played. Record what you heard.' :
        'Combined speaker test did not play. Review the message in this card.',
        !emitted);
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not start the combined speaker test: ' + e.message, true);
    }
    render();
  }
  async function recordSummedValidation(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var outcome = button.getAttribute('data-outcome') || '';
    var summedTestId = button.getAttribute('data-summed-test-id') || '';
    if (!groupId || !outcome) {
      status('Choose a speaker group and validation result before saving the combined check.', true);
      return;
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
      var body = Object.assign({
        speaker_group_id: groupId,
        outcome: outcome,
        summed_test_id: summedTestId,
        polarity: outcome === 'polarity_or_delay_problem' ? 'needs_review' : 'normal'
      }, currentMicObservationPayload());
      var resp = await fetch('./active-speaker/summed-validation', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
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
      if (summedValidationComplete()) outputStepOverride = 'profile';
      status(outcome === 'blend_ok' ?
        'Combined crossover check saved.' :
        'Combined crossover result saved; adjust the crossover before applying a profile.');
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
  async function compileBaselineProfile() {
    if (!summedValidationComplete()) {
      status('Measure each driver and save the combined crossover check before saving the active profile.', true);
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Saving active profile',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/baseline-profile', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'active profile save failed');
      patchActiveSpeaker({
        loading: false, action: '',
        baselineProfile: payload,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(payload.permissions && payload.permissions.may_apply ?
        'Active profile saved. Apply it when you are ready.' :
        (baselineProfileApplyBlocked(payload) ?
          'Active profile saved for review. This hardware path cannot be applied from here yet.' :
          'Active profile could not be saved yet; review the message in this card.'),
        !(payload.permissions && payload.permissions.may_apply) &&
          !baselineProfileApplyBlocked(payload));
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not save active profile: ' + e.message, true);
    }
    render();
  }
  async function applyBaselineProfile() {
    var profile = activeSpeaker.baselineProfile || {};
    var config = profile.config || {};
    if (!(profile.permissions || {}).may_apply) {
      status('Save a ready active profile before applying it.', true);
      return;
    }
    if (!await jtsConfirm(
      'Apply the active speaker profile "' + (config.basename || 'active speaker baseline') +
        '"? This makes it your normal speaker profile.',
      {danger: true}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Applying active profile',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/baseline-profile/apply', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'active profile apply failed');
      patchActiveSpeaker({
        loading: false, action: '',
        baselineProfile: payload.profile || payload,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(payload.status === 'applied' ?
        'Active speaker profile applied.' :
        'Active speaker profile was not applied; review the message in this card.',
        payload.status !== 'applied');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not apply active profile: ' + e.message, true);
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
  async function stopActiveSpeakerTest() {
    stopOutputAudibleRamp('Stopped. No test tone is playing.');
    outputTopology.readinessPlaybackChecking = '';
    patchActiveSpeaker({
      loading: false,
      action: 'Stopping',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/stop', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      if (!resp.ok) throw new Error('stop failed');
      var nextSession = await resp.json();
      var nextLevel = nextSession.calibration_level || activeSpeaker.calibrationLevel;
      var levelResp = await fetch('./active-speaker/calibration-level', {cache: 'no-store'});
      if (levelResp.ok) nextLevel = await levelResp.json();
      patchActiveSpeaker({
        loading: false,
        action: '',
        session: nextSession,
        calibrationLevel: nextLevel,
        error: '',
        levelDbfs: nextLevel && nextLevel.test_signal ?
          Number(nextLevel.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs
      });
      refreshSelectedOutputReadiness('Stopped. Choose Start quiet test when you are ready to try again.');
      status('Stopped. No test tone is playing.');
    } catch (e) {
      patchActiveSpeaker({
        loading: false,
        action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not stop active speaker test: ' + e.message, true);
    }
    render();
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
  loadState();
})();
