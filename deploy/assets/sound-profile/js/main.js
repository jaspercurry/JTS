// Sound profile — parametric EQ editor.
//
// Static ES module served from /assets/sound-profile/js/ (revalidated by
// nginx, same delivery model as /system/). The EQ editor and profile library
// remain in this module because their live-draft and band-drag behavior is
// hardware-validated. The active-speaker setup code is split into
// active-speaker-views.js and active-speaker-actions.js, with state/API
// primitives in store.js and api.js.
//
// FOLLOW-UP (deferred, hardware-gated): split the remaining EQ editor/profile
// library code only after on-device verification of band-drag and live-draft →
// CamillaDSP behavior. Do not blind-refactor it. See docs/HANDOFF-management-ui.md.
import { jtsConfirm } from "/assets/shared/js/dialog.js";
import { escapeHtml } from "/assets/shared/js/escape.js";
import { magnitudeDb, GAINLESS_TYPES } from "/assets/sound-profile/js/eq-math.js";
import { createSoundProfileApi } from "./api.js";
import { createActiveSpeakerStore } from "./store.js";
import { createActiveSpeakerViews } from "./active-speaker-views.js";
import { createActiveSpeakerActions } from "./active-speaker-actions.js";

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

  var activeStore = createActiveSpeakerStore();
  var activeApi = createSoundProfileApi();
  var activeViews = createActiveSpeakerViews({
    store: activeStore, api: activeApi, escapeHtml: escapeHtml, ico: ico,
    fmtDb: fmtDb, fmtFreq: fmtFreq, fmtDbfs: fmtDbfs, clamp: clamp, clone: clone,
    status: status, render: function() { render(); }
  });
  var activeActions = createActiveSpeakerActions({
    store: activeStore, api: activeApi, views: activeViews, el: el, jsonHeaders: jsonHeaders,
    clone: clone, clamp: clamp, fmtDbfs: fmtDbfs, status: status, render: function() { render(); },
    jtsConfirm: jtsConfirm
  });

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
      activeViews.renderActiveSpeakerSetup() +
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
      renderSoundSettings() + activeViews.renderActiveSpeakerSetup() + '</div>';
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
    else if (act === 'refresh-active-speaker') { activeActions.refreshActiveSpeakerStatus(); }
    else if (act === 'refresh-output-topology') { activeActions.refreshOutputTopology(); }
    else if (act === 'output-template-axis') {
      activeActions.setOutputTemplateAxis(
        t.getAttribute('data-axis') || '',
        t.getAttribute('data-value') || ''
      );
    }
    else if (act === 'toggle-output-subwoofer') { activeActions.toggleOutputSubwoofer(t.getAttribute('data-mode') || 'add'); }
    else if (act === 'output-step-next') { activeActions.advanceOutputStep(t.getAttribute('data-step') || ''); }
    else if (act === 'save-output-topology') { activeActions.saveOutputTopology(); }
    else if (act === 'copy-driver-research-prompt') { activeActions.copyDriverResearchPrompt(); }
    else if (act === 'parse-driver-research') { activeActions.parseDriverResearchImport(); }
    else if (act === 'save-driver-design') { activeActions.saveDriverResearchDraft(); }
    else if (act === 'prepare-crossover-preview') { activeActions.prepareCrossoverPreview(); }
    else if (act === 'mark-output-identity') { activeActions.updateOutputChannelIdentity(t); }
    else if (act === 'check-output-readiness') { activeActions.checkOutputPlaybackReadiness(t); }
    else if (act === 'play-output-readiness-tone') { activeActions.playOutputReadinessTone(t); }
    else if (act === 'active-floor-result') { activeActions.recordFloorAudioResult(t); }
    else if (act === 'stage-active-config') { activeActions.stageActiveSpeakerConfig(); }
    else if (act === 'check-active-path-safety') { activeActions.checkActivePathSafety(); }
    else if (act === 'load-active-startup') { activeActions.loadActiveStartupConfig(); }
    else if (act === 'rollback-active-startup') { activeActions.rollbackActiveStartupConfig(); }
    else if (act === 'arm-active-speaker') { activeActions.activeSpeakerPost('./active-speaker/arm', 'Starting quiet test mode'); }
    else if (act === 'stop-active-speaker') { activeActions.activeSpeakerPost('./active-speaker/stop', 'Stopping'); }
    else if (act === 'active-level') {
      activeActions.updateActiveSpeakerLevel(t.getAttribute('data-level-action') || 'set');
    }
    else if (act === 'active-mic-observation') {
      activeActions.recordActiveSpeakerMicObservation();
    }
    else if (act === 'active-auto-level') {
      activeActions.applyActiveSpeakerAutoLevel();
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
      activeActions.handleDriverResearchFieldInput(driverField, ev.target.value);
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-import')) {
      activeActions.handleDriverResearchImportInput(ev.target.value);
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
      activeActions.handleActiveSpeakerLevelInput(ev.target.value);
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
      activeActions.updateActiveSpeakerLevel('set', Number(ev.target.value));
      return;
    }
    if (ev.target.id === 'active-speaker-mic-clipping') {
      activeActions.setActiveSpeakerMicClipping(ev.target.checked);
      return;
    }
    if (ev.target.id === 'set-match-loudness') saveSettings({match_loudness: ev.target.checked});
    else if (ev.target.id === 'set-headroom') saveSettings({headroom_trim_db: Number(ev.target.value)});
  });
  el('view-body').addEventListener('toggle', function(ev) {
    activeActions.handleOutputStepToggle(ev.target);
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
      activeActions.refreshOutputTopology({silent: true});
    } catch (e) {
      status('Could not load sound profile: ' + e.message, true);
    }
  }
  loadState();
})();
