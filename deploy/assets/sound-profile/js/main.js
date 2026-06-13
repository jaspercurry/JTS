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
// dom/format/charts/components/sections/views/api/actions/main — this view/
// state/IO logic is still one module. The pure RBJ biquad math lives in the
// sibling eq-math.js (shared with the node parity check). The editor's ~25
// state vars are woven through its rendering and the live-draft IO; splitting
// the rest into a shared store + views/io modules is planned but MUST be
// exercised on the Pi (band-drag + live-draft → CamillaDSP) before merge.
// Do not blind-refactor it. See docs/HANDOFF-management-ui.md.
import { jtsConfirm } from "/assets/shared/js/dialog.js";
import { escapeHtml } from "/assets/shared/js/escape.js";
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
    loading: false, action: '', payload: null, session: null, targets: null,
    stagedConfig: null, calibrationLevel: null,
    bringup: null, startupLoad: null, rehearsal: null, error: '', levelDbfs: null
  };
  var activeSpeakerLevelSeq = 0;
  var activeSpeakerMicObservation = {observedDbfs: '', clipping: false};
  var outputTopology = {
    loading: false, saving: false, payload: null, draft: null,
    identity: null, clockDomain: null, identitySaving: '', protectionSaving: '',
    readiness: null, readinessChecking: '', readinessError: '',
    readinessPlayback: null, readinessPlaybackChecking: '',
    error: '', dirty: false, touched: false
  };
  var outputStepOverride = '';
  var activeSpeakerSetupOpen = false;
  var outputTemplateDraftAxes = {layout: '', speakerMode: ''};
  var driverResearch = {
    inputs: {full_range: '', woofer: '', mid: '', tweeter: '', subwoofer: '', notes: ''},
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
    var open = activeSpeakerSetupOpen || activeSpeaker.loading || activeSpeaker.payload ||
      activeSpeaker.session || activeSpeaker.stagedConfig ||
      activeSpeaker.error || outputTopology.loading || outputTopology.saving ||
      outputTopology.identitySaving || outputTopology.protectionSaving || outputTopology.error ||
      outputTopology.readinessChecking || outputTopology.readinessPlaybackChecking ||
      outputTopology.dirty || outputTopology.touched;
    return '<section class="active-speaker-setup">' +
      '<details class="advanced" data-active-speaker-setup' + (open ? ' open' : '') + '>' +
        '<summary>Advanced speaker setup</summary>' +
        renderOutputTopologySetup() +
      '</details>' +
    '</section>';
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
  function outputStatusClass(statusValue) {
    if (statusValue === 'verified' || statusValue === 'valid' ||
        statusValue === 'ready' || statusValue === 'preview ready') {
      return ' status-pill--ready';
    }
    if (statusValue === 'blocked') return ' status-pill--blocked';
    return ' status-pill--planned';
  }
  function humanMode(modeValue) {
    return {
      full_range_passive: 'Passive/full range',
      active_2_way: 'Active 2-way',
      active_3_way: 'Active 3-way',
      subwoofer: 'Subwoofer'
    }[modeValue] || modeValue || 'Unknown';
  }
  function humanRole(role) {
    return {
      full_range: 'Full range',
      woofer: 'Woofer',
      mid: 'Mid',
      tweeter: 'Tweeter',
      subwoofer: 'Subwoofer'
    }[role] || role || 'Channel';
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
      Number(summary.driver_count || 0) > 0 &&
      Number(summary.crossover_candidate_count || 0) > 0 &&
      !driverResearch.dirty;
  }
  function driverResearchStepSatisfied() {
    var draftPayload = driverResearch.designDraft || {};
    var savedStatus = draftPayload.status || '';
    return savedStatus && savedStatus !== 'not_saved' && savedStatus !== 'unreadable' &&
      !driverResearch.dirty;
  }
  function driverResearchSavedStatusLabel(status, summary) {
    summary = summary || {};
    if (status === 'ready_for_review') return 'saved design draft';
    if (status === 'needs_research') return 'research skipped';
    if (status === 'blocked' && Number(summary.driver_count || 0) > 0) {
      return 'saved driver research';
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
      return 'Confirm this wire before choosing it for a quiet test.';
    }
    if (!outputChannelGuardReady(channel)) {
      return 'Confirmed. Continue to First quiet test; JTS will start it at the quietest level.';
    }
    if (channel.protection_required) {
      return channel.protection_status === 'present' ?
        'Confirmed. Extra protection noted; quiet tests still start low.' :
        'Confirmed. Continue to First quiet test; JTS will start it at the quietest level.';
    }
    return 'Confirmed. Continue to First quiet test when ready.';
  }
  function renderOutputTopologySetup() {
    return '<div class="setting-row setting-row--stack output-setup">' +
      '<div class="output-setup__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Active crossover setup</p>' +
          '<p class="setting-row__hint">Build the speaker layout, add driver info, confirm DAC outputs, then start with one quiet driver test.</p>' +
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
  function outputStepState(step, topology) {
    var groups = outputGroups(topology);
    var hasLayout = groups.length > 0;
    if (step === 'layout') return hasLayout && !outputTopology.dirty ? 'done' : 'active';
    if (step === 'research') return hasLayout && !outputTopology.dirty ?
      (driverResearchStepSatisfied() ? 'done' : 'active') : 'todo';
    if (step === 'map') return outputIdentityComplete() ? 'done' :
      (hasLayout && !outputTopology.dirty ? 'active' : 'todo');
    if (step === 'safety') return outputStartupLoaded() ? 'done' :
      (outputIdentityComplete() ? 'active' : 'todo');
    return 'todo';
  }
  function defaultOutputStep() {
    var topology = currentOutputTopology();
    if (!topology || !outputGroups(topology).length || outputTopology.dirty) return 'layout';
    if (!driverResearchStepSatisfied()) return 'research';
    if (!outputIdentityComplete()) return 'map';
    return 'safety';
  }
  function outputStepIsOpen(step, topology) {
    return (outputStepOverride || defaultOutputStep()) === step;
  }
  function outputStepTitle(step) {
    return {
      layout: 'Choose speaker layout',
      research: 'Add driver info',
      map: 'Confirm outputs',
      safety: 'First quiet test'
    }[step] || 'this card';
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
    var layout = axis === 'layout' ? value : axes.layout;
    var speakerMode = axis === 'speaker-mode' ? value : axes.speakerMode;
    if (layout && speakerMode) {
      var template = outputTemplateDefinition(outputTemplateKindFromAxes(layout, speakerMode));
      return !template || count < template.minOutputs;
    }
    if (axis === 'layout') return count < (value === 'stereo' ? 2 : 1);
    var mono = outputTemplateDefinition(outputTemplateKindFromAxes('mono', value));
    var stereo = outputTemplateDefinition(outputTemplateKindFromAxes('stereo', value));
    var minOutputs = Math.min(
      mono ? mono.minOutputs : Infinity,
      stereo ? stereo.minOutputs : Infinity
    );
    return !isFinite(minOutputs) || count < minOutputs;
  }
  function outputTemplateAxisButton(axis, value, label, hint, selected, disabled) {
    return '<button type="button" class="output-template-option" data-act="output-template-axis" ' +
      'data-axis="' + escapeHtml(axis) + '" data-value="' + escapeHtml(value) + '" ' +
      'aria-pressed="' + (selected ? 'true' : 'false') + '"' +
      (disabled ? ' disabled' : '') + '>' +
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
        : 'Adds one subwoofer group on ' + (nextOutputLabel || ('DAC output ' + (Number(nextOutput) + 1)))));
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
  function renderDriverResearchSummary() {
    var saved = driverResearch.designDraft || {};
    var savedStatus = saved.status || '';
    var savedSummary = saved.summary || {};
    var savedHtml = savedStatus && savedStatus !== 'not_saved' ? (
      '<div class="driver-research__summary driver-research__summary--saved">' +
        '<span class="status-pill' + (savedStatus === 'ready_for_review' ? ' status-pill--ready' : '') + '">' +
          escapeHtml(driverResearchSavedStatusLabel(savedStatus, savedSummary)) + '</span>' +
        '<p class="setting-row__hint">' + escapeHtml(
          String(savedSummary.driver_count || 0) + ' saved driver' +
          (Number(savedSummary.driver_count || 0) === 1 ? '' : 's') +
          ', ' + String(savedSummary.crossover_candidate_count || 0) +
          ' crossover candidate' +
          (Number(savedSummary.crossover_candidate_count || 0) === 1 ? '' : 's') +
          '. No filters are applied.'
        ) + '</p>' +
      '</div>'
    ) : '';
    if (driverResearch.error) {
      return savedHtml +
        '<p class="setting-row__hint driver-research__error">' +
        escapeHtml(driverResearch.error) + '</p>';
    }
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
  function renderDriverResearchCard(topology) {
    var roles = outputRoleSummary(topology);
    var saveDisabled = driverResearch.saving || outputTopology.dirty ||
      !currentOutputTopology();
    var fields = roles.map(function(role) {
      return '<label class="driver-research__field">' +
        '<span>' + escapeHtml(driverResearchRoleLabel(role)) + '</span>' +
        '<input type="text" data-driver-field="' + escapeHtml(role) + '" value="' +
          escapeHtml(driverResearch.inputs[role] || '') + '" placeholder="Manufacturer and model">' +
      '</label>';
    }).join('');
    return '<div class="output-card output-card--driver-research">' +
      '<div class="output-card__head"><div><p class="output-card__title">Driver info helper</p>' +
        '<p class="setting-row__hint">Optional. Copy the prompt, paste JSON back, then save before previewing crossover ideas.</p></div></div>' +
      '<div class="driver-research__grid">' +
        '<div class="driver-research__fields">' +
          fields +
          '<label class="driver-research__field driver-research__field--wide">' +
            '<span>Build notes</span>' +
            '<textarea rows="3" data-driver-field="notes" placeholder="Waveguide, baffle, enclosure, amplifier, measurement constraints">' +
              escapeHtml(driverResearch.inputs.notes || '') + '</textarea>' +
          '</label>' +
        '</div>' +
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
              '<button type="button" class="btn btn--ghost" data-act="parse-driver-research">Check JSON</button>' +
              '<button type="button" class="btn btn--primary" data-act="save-driver-design"' +
                (saveDisabled ? ' disabled' : '') + '>' +
                escapeHtml(driverResearch.saving ? 'Saving' : 'Save driver info') +
              '</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="driver-research-import" class="driver-research__textarea" data-driver-import ' +
            'rows="7" placeholder="{...}" aria-label="Driver research JSON result">' +
            escapeHtml(driverResearch.importText || '') + '</textarea>' +
          '<div id="driver-research-summary">' + renderDriverResearchSummary() + '</div>' +
        '</div>' +
      '</div>' +
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
    if (raw === 'blocked') return 'not ready yet';
    return raw.replace(/_/g, ' ');
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
    var draftStatus = (driverResearch.designDraft || {}).status || '';
    var hint = canPrepare ?
      (draftStatus === 'blocked'
        ? 'Builds a no-audio preview from saved research. Wiring and quiet test checks happen before any sound.'
        : 'Builds bounded filter intent from the saved draft. No YAML, no Camilla load, no sound.') :
      (outputTopology.dirty
        ? 'Save the speaker layout before preparing a crossover preview.'
        : (hasSavedResearch
          ? 'Save the latest driver research edits before preparing a crossover preview.'
          : 'Save driver research before preparing a crossover preview.'));
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
          outputTopology.dirty ? 'Save and continue' : 'Next: add driver info',
          true)
      ) +
      renderOutputStepCard(
        'research',
        'Add driver info',
        'Optional helper: collect driver facts and preview crossover ideas.',
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
        renderOutputStepButton('map', 'Next: first quiet test', true)
      ) +
      renderOutputStepCard(
        'safety',
        'First quiet test',
        'Start with one driver at the quietest test level, then raise toward audible in bounded steps.',
        topology,
        renderActiveSpeakerStatus() +
        renderOutputReadinessCard(),
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
      ? (clock && clock.composite_clock_supported ? 'supported' : 'needs attention')
      : (clock && clock.multi_device_aggregate_supported ? 'supported' : 'not enabled');
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
    var groups = outputGroups(topology);
    if (!groups.length) {
      return '<div class="output-card output-card--groups">' +
        '<p class="output-card__title">Speaker groups</p>' +
        '<p class="setting-row__hint">Choose a setup template above. JTS keeps it as a draft until you verify channels safely.</p>' +
      '</div>';
    }
    return '<div class="output-card output-card--groups">' +
      '<div class="output-card__head"><div><p class="output-card__title">Speaker groups</p>' +
        '<p class="setting-row__hint">Driver roles and assigned DAC outputs.</p></div></div>' +
      '<div class="output-groups">' + groups.map(renderOutputGroup).join('') + '</div>' +
    '</div>';
  }
  function renderOutputGroup(group) {
    var channels = Array.isArray(group.channels) ? group.channels : [];
    return '<div class="output-group">' +
      '<div class="output-group__head">' +
        '<div><p class="output-group__title">' + escapeHtml(group.label || group.id) + '</p>' +
        '<p class="setting-row__hint">' + escapeHtml(humanMode(group.mode)) + '</p></div>' +
        '<span class="output-group__badge">' + escapeHtml(group.kind || 'speaker') + '</span>' +
      '</div>' +
      '<div class="output-roles">' + channels.map(function(channel) {
        var label = channel.human_output_label ||
          (channel.physical_output_index == null ? 'No output assigned' : 'Output ' + (Number(channel.physical_output_index) + 1));
        var target = identityTargetFor(group.id, channel.role) || {};
        var targetId = target.id || (group.id + ':' + channel.role);
        var busy = outputTopology.identitySaving === targetId;
        var readinessBusy = outputTopology.readinessChecking === targetId;
        var disabled = outputTopology.dirty || busy || readinessBusy ||
          channel.physical_output_index == null;
        return '<div class="output-role">' +
          '<div class="output-role__text">' +
            '<span>' + escapeHtml(humanRole(channel.role)) + '</span>' +
            '<strong>' + escapeHtml(label) + '</strong>' +
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
  function outputCurrentLevelAtFloor() {
    var level = activeSpeakerLevelConfig();
    return Math.abs(Number(level.value) - Number(level.min)) < 0.001;
  }
  function outputTargetSignature(raw) {
    raw = raw || {};
    var output = raw.output_index != null ? raw.output_index : raw.physical_output_index;
    output = output == null ? null : Number(output);
    return {
      speaker_group_id: raw.speaker_group_id || null,
      role: String(raw.driver_role || raw.role || '').trim().toLowerCase() || null,
      output_index: isFinite(output) && output >= 0 ? output : null
    };
  }
  function outputSameTarget(a, b) {
    return !!a && !!b &&
      (a.speaker_group_id || null) === (b.speaker_group_id || null) &&
      (a.role || null) === (b.role || null) &&
      (a.output_index == null ? null : Number(a.output_index)) ===
        (b.output_index == null ? null : Number(b.output_index));
  }
  function outputFloorAudioConfirmedForReadiness(readiness) {
    var session = activeSpeaker.session || {};
    var quiet = session.quiet_start || {};
    return session.status === 'armed' &&
      quiet.status === 'floor_confirmed' &&
      quiet.floor_audio_confirmed === true &&
      outputSameTarget(quiet.current_target, outputTargetSignature(readiness && readiness.target));
  }
  function outputFloorAudioPendingForPlayback(playback) {
    var session = activeSpeaker.session || {};
    var quiet = session.quiet_start || {};
    var pendingId = quiet.pending_playback_id || null;
    var playbackId = playback && playback.playback_id || null;
    return session.status === 'armed' &&
      quiet.status === 'floor_pending_operator' &&
      pendingId && playbackId && pendingId === playbackId &&
      playback.audio_emitted === true;
  }
  function renderOutputFloorAudioResultActions(playback) {
    var session = activeSpeaker.session || {};
    var quiet = session.quiet_start || {};
    var lastResult = quiet.last_operator_result || null;
    var playbackId = playback && playback.playback_id || '';
    if (!outputFloorAudioPendingForPlayback(playback)) {
      if (!lastResult || !lastResult.outcome || lastResult.playback_id !== playbackId) return '';
      return '<p class="setting-row__hint">Last quiet-test result: ' +
        escapeHtml(String(lastResult.outcome).replace(/_/g, ' ')) + '</p>';
    }
    var buttons = [
      ['heard_correct_driver', 'Heard correct driver', 'btn--primary'],
      ['heard_wrong_driver', 'Wrong driver', 'btn--ghost'],
      ['silent', 'Silent', 'btn--ghost'],
      ['too_loud', 'Too loud', 'btn--danger']
    ];
    return '<div class="output-floor-result">' +
      '<p class="setting-row__title">What did you hear?</p>' +
      '<p class="setting-row__hint">Confirm the selected driver only if it was clearly the one making sound. Press Stop if anything feels wrong.</p>' +
      '<div class="active-speaker-actions">' + buttons.map(function(item) {
        return '<button type="button" class="btn ' + escapeHtml(item[2]) +
          '" data-act="active-floor-result" data-outcome="' + escapeHtml(item[0]) +
          '" data-playback-id="' + escapeHtml(playbackId) + '">' +
          escapeHtml(item[1]) + '</button>';
      }).join('') + '</div>' +
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
    if (session && session.status !== 'armed') return 'Not armed';
    if (quiet.status === 'floor_confirmed' && quiet.floor_audio_confirmed) {
      var targetLabel = quietStartTargetLabel(quiet.current_target);
      return targetLabel ? 'Quiet test confirmed for ' + targetLabel : 'Quiet test confirmed';
    }
    if (quiet.status === 'floor_pending_operator') {
      var pendingTargetLabel = quietStartTargetLabel(quiet.current_target);
      return pendingTargetLabel ? 'Waiting on what you heard for ' + pendingTargetLabel : 'Waiting on what you heard';
    }
    return 'Start quiet';
  }
  function readinessTargetLockReason(readiness) {
    var target = readiness && readiness.target || {};
    var audible = readiness && readiness.audible_test || {};
    if (target.role === 'tweeter') {
      return audible.target_role_allowed === false ?
        'Choose this driver again so JTS can start it quiet.' : '';
    }
    if (audible.target_role_allowed === false) {
      return 'Audible tests are limited to woofer, mid, and subwoofer targets in this slice.';
    }
    return '';
  }
  function friendlySetupReason(raw) {
    var text = String(raw || '').trim();
    if (!text) return 'One setup item needs attention.';
    var lower = text.toLowerCase();
    if (lower.indexOf('route_verified') >= 0 ||
        lower.indexOf('protected_by_active_baseline') >= 0 ||
        lower.indexOf('bypass_disabled') >= 0 ||
        lower.indexOf('path-safety evidence was not provided') >= 0 ||
        lower.indexOf('staged protected candidate') >= 0 ||
        lower.indexOf('active_startup_candidate') >= 0) {
      return 'Set up quiet test mode so JTS can route playback through the protected DSP path. No sound will play.';
    }
    if (lower.indexOf('protection') >= 0 || lower.indexOf('high_frequency') >= 0 ||
        lower.indexOf('high-frequency') >= 0) {
      return 'Choose one confirmed driver so JTS can start it quiet.';
    }
    if (lower.indexOf('path_safety') >= 0 || lower.indexOf('safety evidence') >= 0 ||
        lower.indexOf('evidence') >= 0 || lower.indexOf('protected path') >= 0) {
      return 'Continue preparing the quiet test setup.';
    }
    if (lower.indexOf('startup') >= 0 || lower.indexOf('staged') >= 0 ||
        lower.indexOf('camilla') >= 0) {
      return 'Load the quiet test setup before testing.';
    }
    if (lower.indexOf('floor') >= 0 || lower.indexOf('calibration') >= 0) {
      return 'Return test volume to the quietest level before trying again.';
    }
    if (lower.indexOf('target') >= 0 || lower.indexOf('channel') >= 0) {
      return 'Choose one confirmed driver for the first quiet test.';
    }
    return text.replace(/_/g, ' ');
  }
  function friendlySetupIssue(issue) {
    issue = issue || {};
    return friendlySetupReason(issue.message || issue.label || issue.code);
  }
  function issueNeedsQuietTestSetup(issue) {
    issue = issue || {};
    var code = String(issue.code || '').toLowerCase();
    var message = String(issue.message || issue.label || '').toLowerCase();
    return code.indexOf('route_verified') >= 0 ||
      code.indexOf('protected_by_active_baseline') >= 0 ||
      code.indexOf('bypass_disabled') >= 0 ||
      code === 'path_safety_evidence_missing' ||
      code === 'active_startup_candidate_required' ||
      code === 'staged_candidate_not_ready' ||
      code === 'staged_topology_mismatch' ||
      message.indexOf('path-safety evidence was not provided') >= 0 ||
      message.indexOf('staged protected candidate') >= 0;
  }
  function activeSpeakerCanStageFromIssues(issues) {
    issues = Array.isArray(issues) ? issues : [];
    return issues.some(issueNeedsQuietTestSetup);
  }
  function friendlySetupIssueList(envIssues, sessionIssues) {
    var rows = (Array.isArray(envIssues) ? envIssues : [])
      .concat(Array.isArray(sessionIssues) ? sessionIssues : []);
    var out = [];
    rows.forEach(function(issue) {
      var reason = friendlySetupIssue(issue);
      if (reason && out.indexOf(reason) < 0) out.push(reason);
    });
    return out;
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
      ['Driver', target.label || target.role || 'unknown'],
      ['DAC output', target.physical_output_index == null ? 'unknown' : 'Output ' + (Number(target.physical_output_index) + 1)],
      ['Quiet test', quietStartLabel(activeSpeaker.session)],
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
        buttons.push('<button type="button" class="btn btn--ghost" data-act="check-output-readiness" ' +
          'data-group-id="' + escapeHtml(group.id) + '" ' +
          'data-role="' + escapeHtml(channel.role) + '" ' +
          'data-protection-required="' + (channel.protection_required ? 'true' : 'false') + '" ' +
          'data-protection-status="' + escapeHtml(channel.protection_status || 'unknown') + '" ' +
          'data-label="' + escapeHtml(targetLabel) + '">' +
          'Choose ' + escapeHtml(humanRole(channel.role)) +
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
      return '<p class="setting-row__hint">' + escapeHtml(readiness.playback_allowed ?
        'This driver is ready for a quiet test.' :
        'JTS can preview the test signal, but this install is not enabled to play it yet.') + '</p>';
    }
    return '<div class="output-readiness-blockers">' +
      '<p class="setting-row__title">What to do next</p>' +
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
        '<div class="output-card__head"><div><p class="output-card__title">Selected driver</p>' +
        '<p class="setting-row__hint">Preparing the selected driver. No sound will play.</p></div>' +
        '<span class="status-pill">preparing</span></div>' +
      '</div>';
    }
    if (outputTopology.readinessError) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Selected driver</p>' +
        '<p class="setting-row__hint">JTS could not prepare that driver. The saved speaker layout is still available.</p></div>' +
        '<span class="status-pill status-pill--blocked">check failed</span></div>' +
        '<p class="setting-row__hint">' + escapeHtml(outputTopology.readinessError) + '</p>' +
      '</div>';
    }
    var readiness = outputTopology.readiness;
    if (!readiness) {
      var choices = renderQuietTestTargetChoices();
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Choose first driver</p>' +
        '<p class="setting-row__hint">' + escapeHtml(choices ?
          'Choose one confirmed driver. JTS will prepare it, but no sound plays yet.' :
          'Confirm outputs first, then choose one driver for the first quiet test.') + '</p></div>' +
        '<span class="status-pill">' + escapeHtml(choices ? 'next' : 'confirm outputs') + '</span></div>' +
        choices +
      '</div>';
    }
    var target = readiness.target || {};
    var statusValue = readiness.preconditions_passed ? 'ready' : 'needs one step';
    return '<div class="output-card output-card--readiness">' +
      '<div class="output-card__head"><div><p class="output-card__title">Selected driver</p>' +
        '<p class="setting-row__hint">' + escapeHtml(target.label || 'Selected channel') + '</p></div>' +
        '<span class="status-pill' + (readiness.preconditions_passed ? ' status-pill--ready' : '') + '">' +
          escapeHtml(statusValue) + '</span></div>' +
      renderOutputReadinessSummary(readiness) +
      renderOutputReadinessBlockers(readiness) +
      '<p class="setting-row__hint">' + escapeHtml(readiness.next_step || 'No sound was played.') + '</p>' +
      renderOutputReadinessActions(readiness) +
      renderOutputReadinessPlayback(outputTopology.readinessPlayback) +
    '</div>';
  }
  function renderOutputReadinessActions(readiness) {
    var target = readiness && readiness.target || {};
    var lockReason = readinessTargetLockReason(readiness);
    var atFloor = outputCurrentLevelAtFloor();
    var floorConfirmed = outputFloorAudioConfirmedForReadiness(readiness);
    var disabled = !readiness || !readiness.preconditions_passed || !!lockReason ||
      (!atFloor && !floorConfirmed) || outputTopology.readinessPlaybackChecking;
    var attrs = 'data-group-id="' + escapeHtml(target.speaker_group_id || '') + '" ' +
      'data-role="' + escapeHtml(target.role || '') + '" ' +
      'data-label="' + escapeHtml(target.label || '') + '"';
    var artifactLabel = outputTopology.readinessPlaybackChecking === 'artifact' ?
      'Preparing' : 'Preview test signal';
    var roleLabel = humanRole(target.role || 'channel').toLowerCase();
    var playLabel = outputTopology.readinessPlaybackChecking === 'audio' ?
      'Playing' : 'Start quiet ' + roleLabel + ' test';
    var hints = [];
    if (lockReason) hints.push(lockReason);
    if (readiness && readiness.preconditions_passed && !atFloor && !floorConfirmed) {
      hints.push('Return test volume to the quietest level before starting this driver.');
    }
    if (readiness && readiness.preconditions_passed && floorConfirmed && !atFloor) {
      hints.push('This driver was heard at the quietest level. Raised tests stay bounded by the level limit.');
    }
    return hints.map(function(hint) {
      return '<p class="setting-row__hint">' + escapeHtml(hint) + '</p>';
    }).join('') +
      '<div class="active-speaker-actions">' +
      '<button type="button" class="btn btn--ghost" data-act="play-output-readiness-tone" ' +
        attrs + ' data-audio="false"' + (disabled ? ' disabled' : '') + '>' +
        escapeHtml(artifactLabel) + '</button>' +
      (readiness && readiness.playback_allowed ? '<button type="button" class="btn btn--danger" ' +
        'data-act="play-output-readiness-tone" ' + attrs + ' data-audio="true"' +
        (disabled ? ' disabled' : '') + '>' + escapeHtml(playLabel) + '</button>' : '') +
    '</div>';
  }
  function renderOutputReadinessPlayback(playback) {
    if (!playback) return '';
    var artifact = playback.artifact || {};
    var target = playback.target || {};
    var rows = [
      ['Test status', playback.status || 'unknown'],
      ['Backend', playback.backend || 'none'],
      ['Target', target.label || target.driver_role || target.role || 'unknown'],
      ['Tone', toneSummary(playback.tone || {})],
      ['Artifact', artifact.wav_basename || 'none'],
      ['Sound played', playback.audio_emitted ? 'Yes' : 'No']
    ];
    var issues = Array.isArray(playback.issues) ? playback.issues.slice(0, 4) : [];
    return '<div class="active-speaker-plan output-readiness-playback">' +
      '<p class="setting-row__title">Driver test result</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Playback: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      renderOutputFloorAudioResultActions(playback) +
    '</div>';
  }
  function renderActiveSpeakerStatus() {
    if (activeSpeaker.loading) {
      return '<div class="output-card output-card--active-status">' +
        '<div class="output-card__head"><div><p class="output-card__title">Prepare first quiet test</p>' +
        '<p class="setting-row__hint">Checking the saved setup. No sound will play.</p></div>' +
        '<span class="status-pill">checking</span></div>' +
      '</div>';
    }
    if (activeSpeaker.error && !activeSpeaker.payload) {
      return '<div class="output-card output-card--active-status active-speaker-status__stack">' +
        '<div class="row-between active-speaker-status__head">' +
          '<div><p class="output-card__title">Prepare first quiet test</p>' +
          '<p class="setting-row__hint">JTS could not check the saved setup. No sound was played.</p></div>' +
          '<span class="status-pill status-pill--blocked">check failed</span>' +
        '</div>' +
        '<p class="setting-row__hint">' + escapeHtml(activeSpeaker.error) + '</p>' +
        '<div class="active-speaker-actions"><button type="button" class="btn btn--primary" data-act="refresh-active-speaker">Try again</button></div>' +
      '</div>';
    }
    if (!activeSpeaker.payload) {
      return '<div class="output-card output-card--active-status active-speaker-status__stack">' +
        '<div class="output-card__head"><div><p class="output-card__title">Prepare first quiet test</p>' +
          '<p class="setting-row__hint">JTS checks the saved setup, keeps test volume at its quietest setting, and will not play sound yet.</p></div>' +
        '<span class="status-pill">next</span></div>' +
        '<div class="active-speaker-actions">' +
          '<button type="button" class="btn btn--primary" data-act="refresh-active-speaker">Prepare first quiet test</button>' +
        '</div>' +
      '</div>';
    }
    var p = activeSpeaker.payload || {};
    var safe = p.safe_playback || {};
    var session = activeSpeaker.session || {};
    var staged = activeSpeaker.stagedConfig || {};
    var startup = activeSpeaker.startupLoad || {};
    var startupState = startup.state || {};
    var ok = !!p.ok_to_load_active_config;
    var envIssues = Array.isArray(p.issues) ? p.issues.slice(0, 4) : [];
    var sessionIssues = Array.isArray(session.issues) ? session.issues.slice(0, 4) : [];
    var stagedReady = staged.status === 'staged';
    var startupReady = startupState.status === 'loaded' &&
      !!startupState.rollback_available &&
      !!startupState.current_config_matches_loaded;
    var armed = session.status === 'armed';
    var canStageFromIssues = activeSpeakerCanStageFromIssues(envIssues);
    var statusLabel = armed ? 'ready' :
      (startupReady ? 'step 3 of 3' : (stagedReady ? 'step 2 of 3' : ((ok || canStageFromIssues) ? 'next' : 'needs one step')));
    var nextCopy = armed ?
      'Quiet test mode is ready. Choose one confirmed driver when you are ready to start quietly.' :
      (startupReady ?
        'One more setup step opens the quiet test controls. No sound plays yet.' :
        (stagedReady ?
          'Continue setup to load the quiet test DSP. No sound plays yet.' :
          ((ok || canStageFromIssues) ?
            'JTS can set up quiet test mode now. This does not play sound.' :
            'JTS needs one setup item fixed before quiet test mode.')));
    return '<div class="output-card output-card--active-status active-speaker-status__stack">' +
      '<div class="output-card__head"><div><p class="output-card__title">Prepare first quiet test</p>' +
        '<p class="setting-row__hint">' + escapeHtml(nextCopy) + '</p></div>' +
        '<span class="status-pill' + (armed ? ' status-pill--ready' : '') + '">' +
          escapeHtml(statusLabel) + '</span></div>' +
      (activeSpeaker.error ? '<p class="setting-row__hint">' + escapeHtml(activeSpeaker.error) + '</p>' : '') +
      renderActiveSpeakerIssues(envIssues, sessionIssues) +
      renderActiveSpeakerActions(ok, session, envIssues) +
      ((armed && activeSpeakerSelectedReadinessTarget()) ? renderActiveSpeakerLevel() : '') +
      '<p class="setting-row__hint">' + escapeHtml(safe.warning || 'No sound plays until you explicitly start a quiet test.') + '</p>' +
    '</div>';
  }
  function activeSpeakerLevelConfig() {
    var contract = activeSpeaker.calibrationLevel ||
      (activeSpeaker.targets && activeSpeaker.targets.calibration_level) || {};
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
  function activeSpeakerMicRecommendation(code) {
    return {
      start_at_minimum: 'Start at the quietest level.',
      raise_slowly: 'Use Raise toward audible if the selected driver is still too quiet.',
      hold_level: 'Hold this level for the next check.',
      lower_level: 'Lower the level before continuing.',
      stop_or_lower: 'Press Stop or return to the quietest level; clipping resets the test volume.'
    }[code] || 'Record a mic reading before treating this as guided testing.';
  }
  function activeSpeakerSelectedReadinessTarget() {
    var target = outputTopology.readiness && outputTopology.readiness.target || null;
    if (!target || !target.speaker_group_id || !target.role) return null;
    return target;
  }
  function activeSpeakerTargetLabel(target) {
    if (!target) return 'Choose a driver for the first quiet test first';
    return target.label || [
      target.speaker_label || target.speaker_group_id || 'Speaker',
      humanRole(target.role || target.driver_role || 'channel'),
      target.physical_output_index == null && target.output_index == null ? '' :
        'Output ' + (Number(target.physical_output_index != null ?
          target.physical_output_index : target.output_index) + 1)
    ].filter(Boolean).join(' · ');
  }
  function activeSpeakerAutoLevelLabel(autoLevel) {
    if (!autoLevel || !autoLevel.kind) return 'Choose a driver, play a quiet test, then adjust.';
    return autoLevel.reason || (autoLevel.status || 'hold').replace(/_/g, ' ');
  }
  function renderActiveSpeakerLevel() {
    var cfg = activeSpeakerLevelConfig();
    var contract = activeSpeaker.calibrationLevel ||
      activeSpeaker.targets && activeSpeaker.targets.calibration_level || {};
    var meter = contract.mic_meter || {};
    var guard = contract.software_gain_guard || {};
    var issues = Array.isArray(contract.issues) ? contract.issues : [];
    var rampStep = Number(guard.audible_ramp_step_db) ||
      Number(guard.upward_step_limit_db) || cfg.step;
    var label = {
      unmeasured: 'Mic unmeasured',
      too_quiet: 'Too quiet',
      low: 'Low',
      usable: 'Usable',
      too_loud: 'Too loud',
      clipping: 'Clipping'
    }[meter.status] || 'Mic unmeasured';
    var toneClass = meter.tone === 'danger' ? ' status-pill--blocked' :
      (meter.tone === 'ok' ? ' status-pill--ready' : '');
    var observedInput = activeSpeakerMicObservation.observedDbfs;
    if (!observedInput && meter.observed_dbfs != null) {
      observedInput = String(meter.observed_dbfs);
    }
    var clippingChecked = activeSpeakerMicObservation.clipping ||
      meter.status === 'clipping';
    var selectedTarget = activeSpeakerSelectedReadinessTarget();
    var autoLevel = contract.auto_level || {};
    var autoBusy = activeSpeaker.action === 'Raising test level';
    var autoDisabled = !selectedTarget || autoBusy;
    var levelPct = (cfg.value - cfg.min) / Math.max(1, cfg.max - cfg.min) * 100;
    return '<div class="active-speaker-level">' +
      '<div class="row-between active-speaker-level__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Test volume</p>' +
          '<p class="setting-row__hint">For the selected driver only. Normal listening volume is untouched.</p>' +
        '</div>' +
        '<span class="active-speaker-level__readout" id="active-speaker-level-readout">' +
          escapeHtml(fmtDbfs(cfg.value)) + '</span>' +
      '</div>' +
      '<div class="active-speaker-level__bar" aria-hidden="true">' +
        '<span style="width:' + clamp(levelPct, 0, 100).toFixed(1) + '%"></span>' +
      '</div>' +
      '<div class="active-speaker-meter">' +
        '<span class="active-speaker-meter__label">Quiet</span>' +
        '<span class="active-speaker-meter__label">Usable</span>' +
        '<span class="active-speaker-meter__label">High</span>' +
      '</div>' +
      '<div class="row-between active-speaker-level__meter">' +
        '<span class="status-pill' + toneClass + '">' + escapeHtml(label) + '</span>' +
        '<span class="setting-row__hint">JTS caps this at ' + escapeHtml(fmtDbfs(cfg.max)) +
          ' and raises by at most ' + escapeHtml(fmtDb(rampStep)) + ' dB each time.</span>' +
      '</div>' +
      '<dl class="active-speaker-facts active-speaker-level__target">' +
        '<div><dt>Selected driver</dt><dd>' + escapeHtml(activeSpeakerTargetLabel(selectedTarget)) + '</dd></div>' +
        '<div><dt>Next action</dt><dd>' + escapeHtml(activeSpeakerAutoLevelLabel(autoLevel)) + '</dd></div>' +
      '</dl>' +
      '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="active-level" data-level-action="reset">Back to quiet</button>' +
        '<button type="button" class="btn btn--primary" data-act="active-auto-level"' +
          (autoDisabled ? ' disabled' : '') + '>' +
          escapeHtml(autoBusy ? 'Raising' : 'Raise toward audible') + '</button>' +
      '</div>' +
      '<div class="active-speaker-mic-observation">' +
        '<div class="active-speaker-mic-observation__fields">' +
          '<label for="active-speaker-mic-dbfs">Mic reading dBFS' +
            '<input type="number" id="active-speaker-mic-dbfs" inputmode="decimal" ' +
              'min="-120" max="0" step="0.1" value="' + escapeHtml(observedInput) + '" ' +
              'placeholder="-35.0"></label>' +
          '<label class="active-speaker-mic-observation__check">' +
            '<input type="checkbox" id="active-speaker-mic-clipping"' +
              (clippingChecked ? ' checked' : '') + '> Clipping observed</label>' +
          '<button type="button" class="btn btn--ghost" data-act="active-mic-observation">Record reading</button>' +
        '</div>' +
        '<p class="setting-row__hint">' + escapeHtml(activeSpeakerMicRecommendation(meter.recommendation)) + '</p>' +
        '<p class="setting-row__hint">The mic reading helps JTS decide whether to hold, lower, or raise the next quiet test.</p>' +
      '</div>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.slice(0, 3).map(function(issue) {
        return '<li>' + escapeHtml('Test volume: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
    '</div>';
  }
  function renderActiveSpeakerIssues(envIssues, sessionIssues) {
    var rows = friendlySetupIssueList(envIssues, sessionIssues);
    if (!rows.length) return '';
    return '<div class="active-speaker-note">' +
      '<p class="setting-row__title">What needs attention</p>' +
      '<ul class="active-speaker-issues active-speaker-issues--warning">' + rows.slice(0, 3).map(function(reason) {
      return '<li>' + escapeHtml(reason) + '</li>';
    }).join('') + '</ul></div>';
  }
  function renderActiveSpeakerActions(ok, session, envIssues) {
    var busy = !!activeSpeaker.action;
    var state = session || {};
    var staged = activeSpeaker.stagedConfig || {};
    var startup = activeSpeaker.startupLoad || {};
    var startupState = startup.state || {};
    var stagedReady = staged.status === 'staged';
    var startupReady = startupState.status === 'loaded' &&
      !!startupState.rollback_available &&
      !!startupState.current_config_matches_loaded;
    var stageDisabled = outputTopology.dirty ? ' disabled' : '';
    if (busy) {
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" disabled>' + escapeHtml(activeSpeaker.action) + '</button>' +
        '<span class="setting-row__hint">No sound plays in this step.</span>' +
      '</div>';
    }
    if (state.status === 'armed') {
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--danger" data-act="stop-active-speaker">Stop</button>' +
        '<button type="button" class="btn btn--ghost" data-act="rollback-active-startup">Exit quiet mode</button>' +
        '<span class="setting-row__hint">Quiet test mode is ready. Choose a driver below, then start at the quietest level.</span>' +
      '</div>';
    }
    if (!stagedReady) {
      if (!ok && !activeSpeakerCanStageFromIssues(envIssues)) {
        return '<div class="active-speaker-actions">' +
          '<button type="button" class="btn btn--primary" data-act="refresh-active-speaker">Check again</button>' +
        '</div>';
      }
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--primary" data-act="stage-active-config"' + stageDisabled + '>Set up quiet test mode</button>' +
        '<span class="setting-row__hint">Step 1 of 3: build the quiet test setup. No sound will play.</span>' +
      '</div>';
    }
    if (!startupReady) {
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--primary" data-act="load-active-startup">Continue setup</button>' +
        '<span class="setting-row__hint">Step 2 of 3: load the quiet test setup. No sound will play.</span>' +
      '</div>';
    }
    if (!ok) {
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--primary" data-act="refresh-active-speaker">Check again</button>' +
      '</div>';
    }
    return '<div class="active-speaker-actions">' +
      '<button type="button" class="btn btn--primary" data-act="arm-active-speaker">Open test controls</button>' +
      '<button type="button" class="btn btn--ghost" data-act="rollback-active-startup">Exit quiet mode</button>' +
      '<span class="setting-row__hint">Step 3 of 3: open controls at the quietest setting. It does not play sound by itself.</span>' +
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
    else if (act === 'refresh-active-speaker') { refreshActiveSpeakerStatus(); }
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
    else if (act === 'stage-active-config') { stageActiveSpeakerConfig(); }
    else if (act === 'load-active-startup') { loadActiveStartupConfig(); }
    else if (act === 'rollback-active-startup') { rollbackActiveStartupConfig(); }
    else if (act === 'arm-active-speaker') { activeSpeakerPost('./active-speaker/arm', 'Starting quiet test mode'); }
    else if (act === 'stop-active-speaker') { activeSpeakerPost('./active-speaker/stop', 'Stopping'); }
    else if (act === 'active-level') {
      updateActiveSpeakerLevel(t.getAttribute('data-level-action') || 'set');
    }
    else if (act === 'active-mic-observation') {
      recordActiveSpeakerMicObservation();
    }
    else if (act === 'active-auto-level') {
      applyActiveSpeakerAutoLevel();
    }
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
    if (ev.target.id === 'active-speaker-level') {
      var cfg = activeSpeakerLevelConfig();
      var levelPatch = {levelDbfs: clamp(ev.target.value, cfg.min, cfg.max)};
      patchActiveSpeaker(levelPatch);
      var levelReadout = el('active-speaker-level-readout');
      if (levelReadout) levelReadout.textContent = fmtDbfs(activeSpeaker.levelDbfs);
      return;
    }
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
    if (ev.target.id === 'active-speaker-level') {
      updateActiveSpeakerLevel('set', Number(ev.target.value));
      return;
    }
    if (ev.target.id === 'active-speaker-mic-clipping') {
      activeSpeakerMicObservation.clipping = ev.target.checked;
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
    } catch (e) {
      outputTopology.loading = false;
      outputTopology.error = e.message;
    }
    render();
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
    var summary = el('driver-research-summary');
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
      driverResearch.error = '';
      driverResearch.dirty = true;
      status('Driver research JSON parsed. Review it before using any values.');
    } catch (e) {
      driverResearch.parsed = null;
      driverResearch.error = e.message;
      status('Driver research JSON needs review: ' + e.message, true);
    }
    render();
  }
  async function saveDriverResearchDraft(options) {
    options = options || {};
    if (!driverResearch.dirty && driverResearchStepSatisfied() && options.nextStep) {
      outputStepOverride = options.nextStep;
      status(driverResearchCanPreparePreview() ?
        'Driver research is already saved. Continue with output mapping.' :
        'Driver research is skipped for now. Continue with output mapping.');
      render();
      return true;
    }
    if (outputTopology.dirty) {
      status('Save the speaker layout before saving driver research.', true);
      return false;
    }
    if (!currentOutputTopology()) {
      status('Load output hardware before saving a speaker design draft.', true);
      return false;
    }
    var researchPayload = null;
    if ((driverResearch.importText || '').trim()) {
      try {
        researchPayload = JSON.parse(driverResearch.importText);
        driverResearch.parsed = summarizeDriverResearchPayload(researchPayload);
        driverResearch.error = '';
      } catch (e) {
        driverResearch.parsed = null;
        driverResearch.error = e.message;
        status('Driver research JSON needs review: ' + e.message, true);
        render();
        return false;
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
          driver_research: researchPayload
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker design draft save failed');
      ingestDesignDraft(payload, {force: true});
      crossoverPreview.payload = null;
      crossoverPreview.error = '';
      if (options.nextStep) outputStepOverride = options.nextStep;
      status(payload.status === 'blocked' && payload.driver_research ?
        'Saved driver research. No filters were applied and no sound was played.' :
        'Saved speaker design draft. No filters were applied and no sound was played.');
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
      status('Save driver research before preparing the crossover preview.', true);
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
      status('Prepared crossover preview. No YAML was emitted, no filters were applied, and no sound was played.');
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
      status('Outputs are confirmed. Continue with the first quiet test.');
      return;
    }
    if (step === 'safety') {
      outputStepOverride = 'safety';
      status('Use this card to start quiet, listen to one driver, and raise only in bounded steps.');
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
  async function saveOutputChannelProtectionState(groupId, role, nextStatus) {
    var resp = await fetch('./active-speaker/channel-protection', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({
        speaker_group_id: groupId,
        role: role,
        protection_status: nextStatus
      })
    });
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'channel quiet test setup update failed');
    return payload;
  }
  async function fetchOutputPlaybackReadiness(groupId, role) {
    var resp = await fetch('./active-speaker/playback-readiness', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({
        speaker_group_id: groupId,
        role: role
      })
    });
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'playback readiness failed');
    return payload;
  }
  async function checkOutputPlaybackReadiness(button) {
    if (outputTopology.dirty) {
      status('Save the speaker layout before choosing the first quiet test channel.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var protectionRequired = button.getAttribute('data-protection-required') === 'true';
    var protectionStatus = button.getAttribute('data-protection-status') || 'unknown';
    var targetId = groupId + ':' + role;
    var needsSoftwareGuard = protectionRequired &&
      protectionStatus !== 'present' &&
      protectionStatus !== 'software_guard_requested';
    outputTopology.readinessChecking = targetId;
    outputTopology.protectionSaving = needsSoftwareGuard ? targetId : '';
    outputTopology.error = '';
    outputTopology.readinessError = '';
    outputTopology.readiness = null;
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    outputTopology.touched = true;
    activeSpeaker.stagedConfig = null;
    if (needsSoftwareGuard) {
      status('Preparing ' + label + ' for a quiet first test. No sound will play.');
    }
    render();
    try {
      if (needsSoftwareGuard) {
        ingestOutputTopology(await saveOutputChannelProtectionState(
          groupId,
          role,
          'software_guard_requested'
        ));
        outputTopology.protectionSaving = '';
      }
      outputTopology.readiness = await fetchOutputPlaybackReadiness(groupId, role);
      outputTopology.readinessChecking = '';
      status('Selected ' + label + ' for the first quiet test. No sound was played.');
    } catch (e) {
      outputTopology.protectionSaving = '';
      outputTopology.readinessChecking = '';
      outputTopology.readinessError = e.message;
      status('Could not prepare that driver for a quiet test: ' + e.message, true);
    }
    render();
  }
  async function playOutputReadinessTone(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var audio = button.getAttribute('data-audio') === 'true';
    if (audio && !await jtsConfirm(
      'Play one short quiet ' + humanRole(role).toLowerCase() + ' test on "' +
        label + '"? JTS will use the quiet test setup, bounded test level, and Stop gate for this target.',
      {danger: true}
    )) {
      return;
    }
    outputTopology.readinessPlaybackChecking = audio ? 'audio' : 'artifact';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.touched = true;
    render();
    try {
      var resp = await fetch('./active-speaker/play-tone', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          speaker_group_id: groupId,
          role: role,
          audio: audio
        })
      });
      var result = await resp.json();
      if (!resp.ok) throw new Error(result.error || 'channel test failed');
      outputTopology.readinessPlayback = result.playback || null;
      outputTopology.readinessPlaybackChecking = '';
      patchActiveSpeaker({
        loading: false, action: '',
        session: result.session || activeSpeaker.session,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(audio ? 'Played quiet channel test.' : 'Verified channel test artifact. No sound was played.');
    } catch (e) {
      outputTopology.readinessPlaybackChecking = '';
      outputTopology.readinessError = e.message;
      status('Could not run channel test: ' + e.message, true);
    }
    render();
  }
  async function recordFloorAudioResult(button) {
    var outcome = button.getAttribute('data-outcome') || '';
    var playbackId = button.getAttribute('data-playback-id') || '';
    if (!outcome || !playbackId) {
      status('Quiet-test result is missing playback evidence.', true);
      return;
    }
    var target = outputTopology.readiness && outputTopology.readiness.target || null;
    patchActiveSpeaker({
      loading: false, action: 'Recording what you heard',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/floor-audio-result', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          outcome: outcome,
          playback_id: playbackId
        })
      });
      var result = await resp.json();
      if (!resp.ok) throw new Error(result.error || 'quiet-test result failed');
      patchActiveSpeaker({
        loading: false, action: '',
        session: result,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      if (target && target.speaker_group_id && target.role) {
        try {
          outputTopology.readiness = await fetchOutputPlaybackReadiness(
            target.speaker_group_id,
            target.role
          );
          outputTopology.readinessError = '';
        } catch (refreshErr) {
          outputTopology.readinessError = refreshErr.message;
        }
      }
      await refreshActiveSpeakerRehearsal();
      var quiet = result.quiet_start || {};
      status(quiet.floor_audio_confirmed ?
        'Quiet test confirmed for this driver.' :
        'Quiet test was not confirmed; return to the quietest setting before continuing.');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not record quiet-test result: ' + e.message, true);
    }
    render();
  }
  async function stageActiveSpeakerConfig() {
    if (outputTopology.dirty) {
      status('Save the speaker layout before preparing quiet test mode.', true);
      return;
    }
    if (!await jtsConfirm(
      'Prepare the first quiet test from the saved speaker layout? JTS will build the quiet test setup, but it will not load CamillaDSP or play sound yet.',
      {danger: false}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Preparing quiet test mode',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/stage-config', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'protected config staging failed');
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      patchActiveSpeaker({
        loading: false, action: '',
        stagedConfig: payload,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(payload.status === 'staged' ?
        'Quiet test mode is prepared. Continue when you are ready; no sound was played.' :
        'Quiet test mode needs one more setup step before it can continue.',
        payload.status !== 'staged');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not prepare quiet test mode: ' + e.message, true);
    }
    render();
  }
  async function updateActiveSpeakerLevel(action, requestedLevel) {
    var seq = activeSpeakerLevelSeq += 1;
    var cfg = activeSpeakerLevelConfig();
    var body = {
      action: action || 'set'
    };
    if (requestedLevel != null) body.level_dbfs = requestedLevel;
    patchActiveSpeaker({
      loading: false, action: 'Updating level',
      error: '',
      levelDbfs: cfg.value
    });
    render();
    try {
      var resp = await fetch('./active-speaker/calibration-level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'calibration level update failed');
      if (seq !== activeSpeakerLevelSeq) return;
      var accepted = payload && payload.test_signal ?
        Number(payload.test_signal.requested_level_dbfs) : cfg.value;
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      if (seq !== activeSpeakerLevelSeq) return;
      patchActiveSpeaker({
        loading: false, action: '',
        calibrationLevel: payload,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : cfg.value
      });
      status(payload.issues && payload.issues.length ?
        'Test volume changed within the safety limits.' :
        'Test volume updated.');
    } catch (e) {
      if (seq !== activeSpeakerLevelSeq) return;
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not update test volume: ' + e.message, true);
    }
    render();
  }
  async function recordActiveSpeakerMicObservation() {
    var input = el('active-speaker-mic-dbfs');
    var clippingInput = el('active-speaker-mic-clipping');
    var raw = input ? String(input.value || '').trim() : '';
    var clipping = !!(clippingInput && clippingInput.checked);
    var observed = raw === '' ? null : Number(raw);
    if (raw !== '' && !isFinite(observed)) {
      status('Enter the observed mic level as dBFS, for example -35.0.', true);
      return;
    }
    if (raw === '' && !clipping) {
      status('Enter a mic reading or mark clipping before recording an observation.', true);
      return;
    }
    activeSpeakerLevelSeq += 1;
    activeSpeakerMicObservation = {observedDbfs: raw, clipping: clipping};
    patchActiveSpeaker({
      loading: false, action: 'Recording mic reading',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var body = {action: 'observe', mic_clipping: clipping};
      if (observed != null) body.observed_mic_dbfs = observed;
      var resp = await fetch('./active-speaker/calibration-level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'mic observation update failed');
      var meter = payload.mic_meter || {};
      var accepted = payload && payload.test_signal ?
        Number(payload.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs;
      var bringupResp = await fetch('./active-speaker/bringup-preflight', {cache: 'no-store'});
      if (!bringupResp.ok) throw new Error('bring-up preflight failed');
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      activeSpeakerMicObservation = {
        observedDbfs: meter.observed_dbfs != null ? String(meter.observed_dbfs) : raw,
        clipping: meter.status === 'clipping'
      };
      patchActiveSpeaker({
        loading: false, action: '',
        calibrationLevel: payload,
        bringup: await bringupResp.json(),
        startupLoad: startupLoad,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : activeSpeaker.levelDbfs
      });
      await refreshActiveSpeakerRehearsal();
      status(meter.status === 'clipping' ?
        'Mic clipping recorded; calibration level reset to the floor.' :
        'Mic observation recorded.');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not record mic observation: ' + e.message, true);
    }
    render();
  }
  async function applyActiveSpeakerAutoLevel() {
    var target = activeSpeakerSelectedReadinessTarget();
    if (!target) {
      status('Choose one confirmed driver for the first quiet test before applying a guided level step.', true);
      return;
    }
    var input = el('active-speaker-mic-dbfs');
    var clippingInput = el('active-speaker-mic-clipping');
    var raw = input ? String(input.value || '').trim() : '';
    var observed = raw === '' ? null : Number(raw);
    var clipping = !!(clippingInput && clippingInput.checked);
    if (raw !== '' && !isFinite(observed)) {
      status('Enter the observed mic level as dBFS, for example -35.0.', true);
      return;
    }
    activeSpeakerLevelSeq += 1;
    activeSpeakerMicObservation = {observedDbfs: raw, clipping: clipping};
    patchActiveSpeaker({
      loading: false, action: 'Raising test level',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    render();
    try {
      var body = {
        action: 'auto_step',
        speaker_group_id: target.speaker_group_id,
        role: target.role,
        mic_clipping: clipping
      };
      if (observed != null) body.observed_mic_dbfs = observed;
      var resp = await fetch('./active-speaker/calibration-level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'test volume step failed');
      var accepted = payload && payload.test_signal ?
        Number(payload.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs;
      var meter = payload.mic_meter || {};
      var decision = payload.auto_level || {};
      activeSpeakerMicObservation = {
        observedDbfs: meter.observed_dbfs != null ? String(meter.observed_dbfs) : raw,
        clipping: meter.status === 'clipping'
      };
      var startupLoad = activeSpeaker.startupLoad;
      var refreshWarning = '';
      try {
        startupLoad = await fetchActiveSpeakerStartupLoad();
      } catch (refreshErr) {
        refreshWarning = refreshErr.message || 'startup status refresh failed';
      }
      try {
        outputTopology.readiness = await fetchOutputPlaybackReadiness(
          target.speaker_group_id,
          target.role
        );
        outputTopology.readinessError = '';
      } catch (refreshErr2) {
        outputTopology.readinessError = refreshErr2.message;
        refreshWarning = refreshWarning || refreshErr2.message;
      }
      patchActiveSpeaker({
        loading: false, action: '',
        calibrationLevel: payload,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : activeSpeaker.levelDbfs
      });
      await refreshActiveSpeakerRehearsal();
      status('Test volume: ' + (decision.reason || decision.status || 'level held') +
        (refreshWarning ? ' Refresh warning: ' + refreshWarning + '.' : '.'));
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not raise test volume: ' + e.message, true);
    }
    render();
  }
  async function fetchActiveSpeakerStartupLoad() {
    var resp = await fetch('./active-speaker/startup-load', {cache: 'no-store'});
    if (!resp.ok) throw new Error('startup load status failed');
    return await resp.json();
  }
  async function fetchActiveSpeakerCommissioningRehearsal() {
    var resp = await fetch('./active-speaker/commissioning-rehearsal', {cache: 'no-store'});
    if (!resp.ok) throw new Error('commissioning rehearsal failed');
    return await resp.json();
  }
  async function refreshActiveSpeakerRehearsal() {
    try {
      patchActiveSpeaker({rehearsal: await fetchActiveSpeakerCommissioningRehearsal()});
    } catch (e) {
      patchActiveSpeaker({rehearsal: activeSpeaker.rehearsal || null});
    }
  }
  async function fetchActiveSpeakerEnvironment() {
    var resp = await fetch('./active-speaker/environment', {cache: 'no-store'});
    if (!resp.ok) throw new Error('environment probe failed');
    return await resp.json();
  }
  async function checkActivePathSafety() {
    patchActiveSpeaker({
      loading: false, action: 'Checking quiet test setup',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/check-path-safety', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'quiet-test setup check failed');
      var ready = payload.report && payload.report.ok_to_load_active_config;
      var environment = await fetchActiveSpeakerEnvironment();
      patchActiveSpeaker({
        loading: false, action: '',
        payload: environment,
        startupLoad: payload.startup_load || activeSpeaker.startupLoad,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(ready ?
        'Quiet test setup check passed. No sound was played.' :
        'Quiet test setup needs one more step. No sound was played.',
        !ready);
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not check quiet test setup: ' + e.message, true);
    }
    render();
  }
  async function loadActiveStartupConfig() {
    if (!await jtsConfirm(
      'Continue preparing the first quiet test? This reloads the quiet test setup but does not play sound or change the test level.',
      {danger: false}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Loading quiet test mode',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var pathResp = await fetch('./active-speaker/check-path-safety', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var pathPayload = await pathResp.json();
      if (!pathResp.ok) throw new Error(pathPayload.error || 'quiet-test path check failed');
      var pathReport = pathPayload.report || {};
      if (pathPayload.startup_load) {
        activeSpeaker.startupLoad = pathPayload.startup_load;
      }
      if (pathReport.load_gate && pathReport.load_gate !== 'ready') {
        activeSpeaker = {
          loading: false, action: '',
          payload: activeSpeaker.payload,
          session: activeSpeaker.session,
          targets: activeSpeaker.targets,
          stagedConfig: activeSpeaker.stagedConfig,
          calibrationLevel: activeSpeaker.calibrationLevel,
          bringup: activeSpeaker.bringup,
          startupLoad: pathPayload.startup_load || activeSpeaker.startupLoad,
          rehearsal: activeSpeaker.rehearsal,
          error: '',
          levelDbfs: activeSpeaker.levelDbfs
        };
        status(pathReport.message ||
          'Quiet test mode needs one more setup step before it can continue.', true);
        render();
        return;
      }
      var resp = await fetch('./active-speaker/load-startup-config', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'startup load failed');
      patchActiveSpeaker({
        loading: false, action: '',
        startupLoad: {
          state: payload.load || {},
          preflight: payload.preflight || {}
        },
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      var loaded = payload.load && payload.load.status === 'loaded';
      status(loaded ?
        'Quiet test mode is loaded. No sound was played.' :
        'Quiet test mode needs one more setup step before it can continue.',
        !loaded);
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not load quiet test mode: ' + e.message, true);
    }
    render();
  }
  async function rollbackActiveStartupConfig() {
    if (!await jtsConfirm(
      'Exit quiet test mode and restore the previous DSP setup? This reloads CamillaDSP but does not play sound.',
      {danger: false}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Exiting quiet test mode',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/rollback-startup-config', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'startup rollback failed');
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      patchActiveSpeaker({
        loading: false, action: '',
        startupLoad: startupLoad.state ? startupLoad : {
          state: payload.rollback || {},
          preflight: activeSpeaker.startupLoad && activeSpeaker.startupLoad.preflight || {}
        },
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      var rolledBack = payload.rollback && payload.rollback.status === 'rolled_back';
      status(rolledBack ?
        'Exited quiet test mode. No sound was played.' :
        'Could not exit quiet test mode yet; review the setup state.',
        !rolledBack);
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not exit quiet test mode: ' + e.message, true);
    }
    render();
  }
  async function refreshActiveSpeakerStatus() {
    patchActiveSpeaker({
      loading: true, action: '',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    var probes = [
      ['payload', function() { return fetch('./active-speaker/environment', {cache: 'no-store'}); },
        'environment probe failed'],
      ['session', function() { return fetch('./active-speaker/safe-playback', {cache: 'no-store'}); },
        'safe playback status failed'],
      ['stagedConfig', function() { return fetch('./active-speaker/staged-config', {cache: 'no-store'}); },
        'staged config status failed'],
      ['calibrationLevel', function() { return fetch('./active-speaker/calibration-level', {cache: 'no-store'}); },
        'calibration level status failed'],
      ['bringup', function() { return fetch('./active-speaker/bringup-preflight', {cache: 'no-store'}); },
        'bring-up preflight failed'],
      ['startupLoad', function() { return fetch('./active-speaker/startup-load', {cache: 'no-store'}); },
        'startup load status failed'],
      ['rehearsal', function() { return fetch('./active-speaker/commissioning-rehearsal', {cache: 'no-store'}); },
        'commissioning rehearsal failed'],
      ['targets', function() { return fetch('./active-speaker/tone-targets', {cache: 'no-store'}); },
        'tone targets failed']
    ];
    var results = await Promise.allSettled(probes.map(function(probe) {
      return probe[1]().then(async function(resp) {
        if (!resp.ok) throw new Error(probe[2]);
        return await resp.json();
      });
    }));
    var patch = {loading: false, action: '', error: ''};
    var errors = [];
    results.forEach(function(result, index) {
      var key = probes[index][0];
      if (result.status === 'fulfilled') {
        patch[key] = result.value;
        return;
      }
      errors.push(result.reason && result.reason.message || probes[index][2]);
    });
    var nextLevel = patch.calibrationLevel || activeSpeaker.calibrationLevel;
    if (nextLevel && nextLevel.test_signal) {
      patch.levelDbfs = Number(nextLevel.test_signal.requested_level_dbfs);
    }
    if (errors.length) patch.error = 'Partial refresh: ' + errors.join('; ');
    patchActiveSpeaker(patch);
    render();
  }
  async function activeSpeakerPost(path, actionLabel) {
    patchActiveSpeaker({
      loading: false, action: actionLabel,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch(path, {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      if (!resp.ok) throw new Error(actionLabel + ' failed');
      var nextSession = await resp.json();
      var nextLevel = nextSession.calibration_level || activeSpeaker.calibrationLevel;
      if (path.indexOf('/stop') >= 0 &&
          !(nextLevel && nextLevel.status === 'reset_failed')) {
        var levelResp = await fetch('./active-speaker/calibration-level', {cache: 'no-store'});
        if (levelResp.ok) nextLevel = await levelResp.json();
      }
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      patchActiveSpeaker({
        loading: false, action: '',
        session: nextSession,
        calibrationLevel: nextLevel,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: nextLevel && nextLevel.test_signal ?
          Number(nextLevel.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs
      });
      await refreshActiveSpeakerRehearsal();
      outputTopology.readiness = null;
      outputTopology.readinessChecking = '';
      outputTopology.readinessError = '';
      outputTopology.readinessPlayback = null;
      outputTopology.readinessPlaybackChecking = '';
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
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
