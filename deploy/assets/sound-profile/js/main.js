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
  var activeSpeaker = {
    loading: false, action: '', payload: null, session: null, targets: null,
    stagedConfig: null, calibrationLevel: null, plan: null, playback: null,
    bringup: null, startupLoad: null, rehearsal: null, error: '', levelDbfs: null
  };
  var activeSpeakerMicObservation = {observedDbfs: '', clipping: false};
  var outputTopology = {
    loading: false, saving: false, payload: null, draft: null,
    identity: null, clockDomain: null, identitySaving: '', protectionSaving: '',
    readiness: null, readinessChecking: '', readinessError: '',
    readinessPlayback: null, readinessPlaybackChecking: '',
    error: '', dirty: false, touched: false
  };
  var outputStepOverride = '';
  var driverResearch = {
    inputs: {woofer: '', mid: '', tweeter: '', subwoofer: '', notes: ''},
    importText: '',
    parsed: null,
    error: ''
  };
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
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function(ch) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
    });
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
    return isGainless(s) || Math.abs(Number(s.gain_db || 0)) >= 0.05;
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
    Object.keys(profile.simple_eq).forEach(function(k) { if (Math.abs(profile.simple_eq[k]) >= 0.05) n += 1; });
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
    var open = activeSpeaker.loading || activeSpeaker.payload ||
      activeSpeaker.session || activeSpeaker.stagedConfig ||
      activeSpeaker.plan || activeSpeaker.playback ||
      activeSpeaker.error || outputTopology.loading || outputTopology.saving ||
      outputTopology.identitySaving || outputTopology.protectionSaving || outputTopology.error ||
      outputTopology.readinessChecking || outputTopology.readinessPlaybackChecking ||
      outputTopology.dirty || outputTopology.touched;
    return '<section class="active-speaker-setup">' +
      '<details class="advanced"' + (open ? ' open' : '') + '>' +
        '<summary>Advanced speaker setup</summary>' +
        '<div class="setting-row setting-row--stack">' +
          '<div class="setting-row__text">' +
            '<p class="setting-row__title">Active crossover commissioning</p>' +
            '<p class="setting-row__hint">For speakers with separate woofer, mid, or tweeter amplifier channels. ' +
              'Environment checks and staging will not play tones, reload CamillaDSP, or load active crossover configs.</p>' +
          '</div></div>' +
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
    if (statusValue === 'verified' || statusValue === 'valid') return ' status-pill--ready';
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
      tweeter: 'Tweeter / compression driver / horn',
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
  function humanProtectionStatus(value) {
    return {
      not_required: 'not needed',
      required_missing: 'not set',
      present: 'physical protection',
      software_guard_requested: 'software guard requested',
      unknown: 'unknown'
    }[value] || value || 'unknown';
  }
  function renderOutputTopologySetup() {
    return '<div class="setting-row setting-row--stack output-setup">' +
      '<div class="output-setup__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Output setup</p>' +
          '<p class="setting-row__hint">Map DAC outputs to speakers and driver roles before any active crossover work. ' +
            'Saving this map does not play sound or reload CamillaDSP.</p>' +
        '</div></div>' +
      renderOutputSetupState() +
      renderOutputTopologyBody() +
    '</div>';
  }
  function outputSetupStatusLabel() {
    var topology = currentOutputTopology();
    if (outputTopology.loading && !topology) return 'Loading';
    if (!topology) return 'Not loaded';
    if (outputTopology.dirty) return 'Unsaved draft';
    var evaluation = outputEvaluation(topology);
    return (evaluation.status || topology.status || 'Saved').replace(/_/g, ' ');
  }
  function renderOutputSetupState() {
    return '<div class="output-setup__state">' +
      '<span class="status-pill">' + escapeHtml(outputSetupStatusLabel()) + '</span>' +
      renderOutputTopologyActions() +
    '</div>';
  }
  function renderOutputTopologyActions() {
    var topology = currentOutputTopology();
    var saveDisabled = !outputTopology.draft || !outputTopology.dirty ||
      outputTopology.saving || outputTopology.loading;
    return '<div class="output-setup__actions">' +
      '<button type="button" class="btn btn--ghost" data-act="refresh-output-topology"' +
        (outputTopology.loading ? ' disabled' : '') + '>' + (topology ? 'Reload hardware' : 'Load hardware') + '</button>' +
      '<button type="button" class="btn btn--primary" data-act="save-output-topology"' +
        (saveDisabled ? ' disabled' : '') + '>' + (outputTopology.saving ? 'Saving' : 'Save output map') + '</button>' +
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
      (driverResearch.parsed ? 'done' : 'active') : 'todo';
    if (step === 'map') return outputIdentityComplete() ? 'done' :
      (hasLayout && !outputTopology.dirty ? 'active' : 'todo');
    if (step === 'safety') return outputStartupLoaded() ? 'done' :
      (outputIdentityComplete() ? 'active' : 'todo');
    return 'todo';
  }
  function defaultOutputStep(topology) {
    if (!topology || !outputGroups(topology).length || outputTopology.dirty) return 'layout';
    if (!driverResearch.parsed) return 'research';
    if (!outputIdentityComplete()) return 'map';
    return 'safety';
  }
  function outputStepIsOpen(step, topology) {
    return (outputStepOverride || defaultOutputStep(topology)) === step;
  }
  function openOutputStep(step) {
    outputStepOverride = step;
    render();
  }
  function renderOutputStepCard(step, title, hint, topology, bodyHtml, footerHtml) {
    var state = outputStepState(step, topology);
    var open = outputStepIsOpen(step, topology);
    var stateLabel = open ? 'Now' : (state === 'done' ? 'Done' : 'Next');
    return '<details class="output-step output-step--' + escapeHtml(state) + '"' +
      ' data-output-step="' + escapeHtml(step) + '"' +
      (open ? ' open' : '') + '>' +
      '<summary class="output-step__summary">' +
        '<span class="output-step__marker">' + escapeHtml(stateLabel) + '</span>' +
        '<span class="output-step__text"><strong>' + escapeHtml(title) + '</strong>' +
          '<span>' + escapeHtml(hint) + '</span></span>' +
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
  function outputSetupTemplateButton(template, count) {
    var disabled = count < template.minOutputs;
    return '<button type="button" class="output-template" data-act="output-template" ' +
      'data-template="' + escapeHtml(template.id) + '"' + (disabled ? ' disabled' : '') + '>' +
      '<strong>' + escapeHtml(template.label) + '</strong>' +
      '<span>' + escapeHtml(template.hint) + '</span>' +
      '<small>' + escapeHtml(String(template.minOutputs) + '+ output' + (template.minOutputs === 1 ? '' : 's')) + '</small>' +
    '</button>';
  }
  function outputTemplateList() {
    return [
      'mono_passive',
      'mono_active_2way',
      'mono_active_3way',
      'stereo_passive',
      'stereo_active_2way',
      'stereo_active_3way'
    ].map(outputTemplateDefinition).filter(Boolean);
  }
  function renderOutputSetupTemplates(topology) {
    var hardware = outputHardware(topology);
    var count = Number(hardware && hardware.physical_output_count) || 0;
    var templates = outputTemplateList();
    return '<div class="output-card output-card--templates">' +
      '<div class="output-card__head"><div><p class="output-card__title">Setup template</p>' +
        '<p class="setting-row__hint">Choose the speaker layout you are wiring. This only edits the saved draft map.</p></div></div>' +
      '<div class="output-template-grid">' + templates.map(function(template) {
        return outputSetupTemplateButton(template, count);
      }).join('') + '</div>' +
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
    if (driverResearch.error) {
      return '<p class="setting-row__hint driver-research__error">' + escapeHtml(driverResearch.error) + '</p>';
    }
    if (!driverResearch.parsed) {
      return '<p class="setting-row__hint">Paste JSON from the assistant to sanity-check the shape. JTS will not apply it automatically.</p>';
    }
    var summary = driverResearch.parsed;
    return '<div class="driver-research__summary">' +
      '<span class="status-pill status-pill--ready">import parsed</span>' +
      '<p class="setting-row__hint">' + escapeHtml(
        summary.driverCount + ' driver' + (summary.driverCount === 1 ? '' : 's') +
        ', ' + summary.candidateCount + ' crossover candidate' + (summary.candidateCount === 1 ? '' : 's') +
        '. Roles: ' + summary.roles.join(', ')
      ) + '</p>' +
      (summary.warnings.length ? '<ul class="active-speaker-issues">' + summary.warnings.map(function(warning) {
        return '<li>' + escapeHtml(String(warning)) + '</li>';
      }).join('') + '</ul>' : '') +
    '</div>';
  }
  function renderDriverResearchCard(topology) {
    var roles = outputRoleSummary(topology);
    var fields = roles.map(function(role) {
      return '<label class="driver-research__field">' +
        '<span>' + escapeHtml(driverResearchRoleLabel(role)) + '</span>' +
        '<input type="text" data-driver-field="' + escapeHtml(role) + '" value="' +
          escapeHtml(driverResearch.inputs[role] || '') + '" placeholder="Manufacturer and model">' +
      '</label>';
    }).join('');
    return '<div class="output-card output-card--driver-research">' +
      '<div class="output-card__head"><div><p class="output-card__title">Driver research helper</p>' +
        '<p class="setting-row__hint">Generate a precise prompt for an external assistant, then paste bounded JSON back for review.</p></div></div>' +
      '<div class="driver-research__grid">' +
        '<div class="driver-research__fields">' +
          fields +
          '<label class="driver-research__field driver-research__field--wide">' +
            '<span>Build notes</span>' +
            '<textarea rows="3" data-driver-field="notes" placeholder="Horn, enclosure, intended use, amplifier, measurement constraints">' +
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
            '<button type="button" class="btn btn--ghost" data-act="parse-driver-research">Check JSON</button>' +
          '</div>' +
          '<textarea id="driver-research-import" class="driver-research__textarea" data-driver-import ' +
            'rows="7" placeholder="{...}" aria-label="Driver research JSON result">' +
            escapeHtml(driverResearch.importText || '') + '</textarea>' +
          '<div id="driver-research-summary">' + renderDriverResearchSummary() + '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  }
  function renderOutputTopologyBody() {
    if (outputTopology.loading && !currentOutputTopology()) {
      return '<p class="setting-row__hint">Loading output topology…</p>';
    }
    if (outputTopology.error) {
      return '<div class="output-error">' +
        '<span class="status-pill status-pill--blocked">Output setup unavailable</span>' +
        '<p class="setting-row__hint">' + escapeHtml(outputTopology.error) + '</p>' +
      '</div>';
    }
    var topology = currentOutputTopology();
    if (!topology) {
      return '<div class="output-empty">' +
        '<p class="setting-row__hint">Load detected hardware to start a speaker output map.</p>' +
      '</div>';
    }
    var evaluation = outputEvaluation(topology);
    var statusValue = outputTopology.dirty ? 'draft' : (evaluation.status || topology.status || 'draft');
    return '<div class="output-layout">' +
      renderOutputStepCard(
        'layout',
        'Choose speaker layout',
        'Pick mono or stereo, active or passive, then optionally add a subwoofer.',
        topology,
        renderOutputHardwareCard(topology, statusValue) +
          renderOutputSetupTemplates(topology) +
          renderOutputSubwooferCard(topology),
        renderOutputStepButton('layout',
          outputTopology.dirty ? 'Save and continue' : 'Next: research drivers',
          true)
      ) +
      renderOutputStepCard(
        'research',
        'Research drivers',
        'Generate a precise external-assistant prompt, then paste bounded JSON back for review.',
        topology,
        renderDriverResearchCard(topology),
        renderOutputStepButton('research', 'Next: map outputs', true)
      ) +
      renderOutputStepCard(
        'map',
        'Map and verify outputs',
        'Review speaker groups, DAC lanes, and physical verification evidence.',
        topology,
        renderOutputStageCard(topology) +
          renderOutputGroupsCard(topology) +
          renderOutputIdentityCard(),
        renderOutputStepButton('map', 'Next: safety checks', true)
      ) +
      renderOutputStepCard(
        'safety',
        'Stage, load, and start quiet',
        'Check environment, stage protected startup, then use bounded readiness and level controls.',
        topology,
        renderOutputCommissioningRehearsal() +
        '<div class="output-card output-card--active-status">' +
          '<div class="output-card__head"><div><p class="output-card__title">Environment and safe-session state</p>' +
          '<p class="setting-row__hint">These controls do not play sound unless the explicit lab backend is enabled and readiness passes.</p></div></div>' +
          renderOutputBringupSequence() +
          renderActiveSpeakerStatus() +
        '</div>' +
        renderOutputReadinessCard() +
        renderOutputSafetyCard(topology, statusValue),
        ''
      ) +
    '</div>';
  }
  function renderOutputHardwareCard(topology, statusValue) {
    var hardware = outputHardware(topology) || {};
    var clock = outputClockDomainReport();
    var rows = [
      ['Device', hardware.device_id || 'unknown'],
      ['Outputs', String(hardware.physical_output_count || 0) + ' physical'],
      ['Route', hardware.route || 'default'],
      ['Clock domain', clock && clock.clock_domain_label ||
        hardware.clock_domain_label || 'Single output device clock'],
      ['Multi-DAC aggregate', clock && clock.multi_device_aggregate_supported ? 'supported' : 'not enabled'],
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
        var protectionStatus = channel.protection_status || 'unknown';
        var protection = channel.protection_required
          ? ' · guard ' + humanProtectionStatus(protectionStatus) : '';
        var label = channel.human_output_label ||
          (channel.physical_output_index == null ? 'No output assigned' : 'Output ' + (Number(channel.physical_output_index) + 1));
        var target = identityTargetFor(group.id, channel.role) || {};
        var targetId = target.id || (group.id + ':' + channel.role);
        var busy = outputTopology.identitySaving === targetId;
        var protectionBusy = outputTopology.protectionSaving === targetId;
        var readinessBusy = outputTopology.readinessChecking === targetId;
        var action = channel.identity_verified ? 'Clear' : 'Mark verified';
        var protectionPresent = protectionStatus === 'present';
        var softwareGuard = protectionStatus === 'software_guard_requested';
        var disabled = outputTopology.dirty || busy || protectionBusy ||
          readinessBusy || channel.physical_output_index == null;
        return '<div class="output-role">' +
          '<div class="output-role__text">' +
            '<span>' + escapeHtml(humanRole(channel.role)) + '</span>' +
            '<strong>' + escapeHtml(label) + '</strong>' +
            '<small>' + escapeHtml((channel.identity_verified ? 'identity verified' : 'identity unverified') + protection) + '</small>' +
          '</div>' +
          '<div class="output-role__actions">' +
            '<button type="button" class="btn btn--ghost output-role__action" ' +
              'data-act="mark-output-identity" ' +
              'data-group-id="' + escapeHtml(group.id) + '" ' +
              'data-role="' + escapeHtml(channel.role) + '" ' +
              'data-verified="' + (channel.identity_verified ? 'false' : 'true') + '" ' +
              'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
              (disabled ? ' disabled' : '') + '>' +
              escapeHtml(busy ? 'Saving' : action) + '</button>' +
            (channel.protection_required ? (
              (protectionPresent || softwareGuard ? '<button type="button" class="btn btn--ghost output-role__action" ' +
                'data-act="mark-output-protection" ' +
                'data-group-id="' + escapeHtml(group.id) + '" ' +
                'data-role="' + escapeHtml(channel.role) + '" ' +
                'data-status="required_missing" ' +
                'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
                (disabled ? ' disabled' : '') + '>' +
                escapeHtml(protectionBusy ? 'Saving' : 'Clear guard') + '</button>' : '') +
              (!protectionPresent ? '<button type="button" class="btn btn--ghost output-role__action" ' +
                'data-act="mark-output-protection" ' +
                'data-group-id="' + escapeHtml(group.id) + '" ' +
                'data-role="' + escapeHtml(channel.role) + '" ' +
                'data-status="present" ' +
                'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
                (disabled ? ' disabled' : '') + '>' +
                escapeHtml(protectionBusy ? 'Saving' : 'Hardware protected') + '</button>' : '') +
              (!softwareGuard ? '<button type="button" class="btn btn--ghost output-role__action" ' +
                'data-act="mark-output-protection" ' +
                'data-group-id="' + escapeHtml(group.id) + '" ' +
                'data-role="' + escapeHtml(channel.role) + '" ' +
                'data-status="software_guard_requested" ' +
                'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
                (disabled ? ' disabled' : '') + '>' +
                escapeHtml(protectionBusy ? 'Saving' : 'Use software guard') + '</button>' : '')
            ) : '') +
            '<button type="button" class="btn btn--ghost output-role__action" ' +
              'data-act="check-output-readiness" ' +
              'data-group-id="' + escapeHtml(group.id) + '" ' +
              'data-role="' + escapeHtml(channel.role) + '" ' +
              'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
              (disabled ? ' disabled' : '') + '>' +
              escapeHtml(readinessBusy ? 'Checking' : 'Check readiness') + '</button>' +
          '</div>' +
        '</div>';
      }).join('') + '</div>' +
    '</div>';
  }
  function renderOutputIdentityCard() {
    if (outputTopology.dirty) {
      return '<div class="output-card output-card--identity">' +
        '<div class="output-card__head"><div><p class="output-card__title">Channel identity</p>' +
        '<p class="setting-row__hint">Save this output setup draft before recording physical verification evidence.</p></div>' +
        '<span class="status-pill">draft</span></div>' +
        '<p class="setting-row__hint">JTS will re-run backend validation after save, then you can mark assigned channels as physically verified.</p>' +
      '</div>';
    }
    var report = outputIdentityReport();
    if (!report) {
      return '<div class="output-card output-card--identity">' +
        '<div class="output-card__head"><div><p class="output-card__title">Channel identity</p>' +
        '<p class="setting-row__hint">Load or save the output setup to see verification progress.</p></div></div>' +
      '</div>';
    }
    var assigned = Number(report.assigned_channel_count || 0);
    var verified = Number(report.verified_channel_count || 0);
    var unverified = Number(report.unverified_channel_count || 0);
    var targets = Array.isArray(report.targets) ? report.targets : [];
    var rows = targets.length ? targets.map(function(target) {
      var blockers = Array.isArray(target.sound_test_blockers) ? target.sound_test_blockers : [];
      return '<li class="output-identity-row">' +
        '<span>' + escapeHtml(target.speaker_label || target.speaker_group_id || 'Speaker') +
          ' · ' + escapeHtml(humanRole(target.role)) + '</span>' +
        '<strong>' + escapeHtml(target.identity_verified ? 'Verified' :
          (target.assigned ? 'Needs check' : 'Unassigned')) + '</strong>' +
        (blockers.length ? '<small>' + escapeHtml(blockers.slice(0, 2).join(', ')) + '</small>' : '') +
      '</li>';
    }).join('') : '<li class="output-identity-row"><span>No channels configured</span><strong>Draft</strong></li>';
    return '<div class="output-card output-card--identity">' +
      '<div class="output-card__head"><div><p class="output-card__title">Channel identity</p>' +
        '<p class="setting-row__hint">Physical verification is operator evidence. It does not authorize playback by itself.</p></div>' +
        '<span class="status-pill' + outputStatusClass(report.status || 'draft') + '">' +
          escapeHtml(verified + '/' + assigned + ' verified') + '</span></div>' +
      (outputTopology.dirty ? '<p class="setting-row__hint">Save the draft before changing identity evidence.</p>' : '') +
      '<ul class="output-identity-list">' + rows + '</ul>' +
      '<p class="setting-row__hint">' + escapeHtml(report.next_step || 'Verify assigned channels before sound tests.') + '</p>' +
      (unverified > 0 ? '<p class="setting-row__hint">Use this only after wiring inspection, dummy-load/DMM checks, or a future low-level channel test confirms the driver.</p>' : '') +
    '</div>';
  }
  function sequenceStep(label, done, active, blocked, detail) {
    var state = done ? 'done' : (blocked ? 'blocked' : (active ? 'active' : 'todo'));
    var marker = done ? 'Done' : (blocked ? 'Blocked' : (active ? 'Now' : 'Next'));
    return '<li class="output-sequence__item output-sequence__item--' + escapeHtml(state) + '">' +
      '<span class="output-sequence__marker">' + escapeHtml(marker) + '</span>' +
      '<span class="output-sequence__text"><strong>' + escapeHtml(label) + '</strong>' +
        '<small>' + escapeHtml(detail || '') + '</small></span>' +
    '</li>';
  }
  function renderOutputBringupSequence() {
    var env = activeSpeaker.payload || {};
    var staged = activeSpeaker.stagedConfig || {};
    var startup = activeSpeaker.startupLoad || {};
    var startupState = startup.state || {};
    var startupPreflight = startup.preflight || {};
    var pathSafety = startupPreflight.path_safety || {};
    var session = activeSpeaker.session || {};
    var readiness = outputTopology.readiness || {};
    var envChecked = !!activeSpeaker.payload;
    var envReady = !!env.ok_to_load_active_config;
    var stagedReady = staged.status === 'staged';
    var pathReady = pathSafety.load_gate === 'ready';
    var startupReady = startupState.status === 'loaded' &&
      !!startupState.rollback_available &&
      !!startupState.current_config_matches_loaded;
    var armed = session.status === 'armed';
    var readinessChecked = !!outputTopology.readiness;
    var readinessReady = !!readiness.preconditions_passed;
    var atFloor = outputCurrentLevelAtFloor();
    var floorConfirmed = outputFloorAudioConfirmedForReadiness(readiness);
    var artifactReady = !!(outputTopology.readinessPlayback && outputTopology.readinessPlayback.artifact);
    var rows = [
      sequenceStep(
        'Check environment',
        envReady,
        !envChecked,
        envChecked && !envReady,
        envReady ? 'Output path and config are load-gate ready.' :
          (envChecked ? 'Resolve environment blockers before loading DSP.' : 'Run Check environment first.')
      ),
      sequenceStep(
        'Stage protected startup',
        stagedReady,
        envReady && !stagedReady,
        false,
        stagedReady ? 'Muted, limited startup config is staged.' : 'Build a protected startup config for review.'
      ),
      sequenceStep(
        'Check protected path',
        pathReady,
        stagedReady && !pathReady,
        startupPreflight.status === 'blocked',
        pathReady ? 'Path-safety evidence is bound to this startup load.' : 'Verify renderer/cue paths before loading.'
      ),
      sequenceStep(
        'Load protected startup',
        startupReady,
        pathReady && !startupReady,
        startupState.status === 'blocked',
        startupReady ? 'CamillaDSP is on the protected graph with rollback.' : 'Reloads DSP only; it does not play sound.'
      ),
      sequenceStep(
        'Arm safe session',
        armed,
        startupReady && !armed,
        false,
        armed ? 'Stop is available and the session is time-bounded.' : 'Arming records safety state only.'
      ),
      sequenceStep(
        'Select target and check readiness',
        readinessReady,
        armed && !readinessChecked,
        readinessChecked && !readinessReady,
        readinessChecked ? (readiness.next_step || 'Review target readiness below.') : 'Use Check readiness on a saved output lane.'
      ),
      sequenceStep(
        'Start at the floor',
        atFloor || floorConfirmed,
        readinessReady && !atFloor && !floorConfirmed,
        false,
        floorConfirmed ? 'Floor audio is confirmed for this target.' :
          (atFloor ? 'Calibration level is at the quiet floor.' : 'Reset or lower test level before preparing tone.')
      ),
      sequenceStep(
        'Verify artifact before audio',
        artifactReady && (atFloor || floorConfirmed),
        readinessReady && (atFloor || floorConfirmed) && !artifactReady,
        false,
        'Artifact-only verification remains the default; audible playback needs explicit lab enablement.'
      ),
      sequenceStep(
        'Confirm floor audio',
        floorConfirmed,
        readinessReady && atFloor && !floorConfirmed,
        false,
        floorConfirmed ? 'Raised tests are unlocked for this target/session.' : 'First audible test must succeed at the floor.'
      ),
      sequenceStep(
        'Raise slowly',
        floorConfirmed && !atFloor,
        floorConfirmed && atFloor,
        false,
        'After floor audio succeeds, raised audible tests remain bounded by 1 dB steps.'
      )
    ];
    return '<div class="output-sequence">' +
      '<p class="setting-row__title">Safe bring-up sequence</p>' +
      '<ol class="output-sequence__list">' + rows.join('') + '</ol>' +
    '</div>';
  }
  function renderOutputCommissioningRehearsal() {
    var rehearsal = activeSpeaker.rehearsal || {};
    var steps = Array.isArray(rehearsal.steps) ? rehearsal.steps : [];
    var statusValue = rehearsal.status || 'not_checked';
    if (!steps.length) {
      return '<div class="output-card output-card--rehearsal">' +
        '<div class="output-card__head"><div><p class="output-card__title">Commissioning rehearsal</p>' +
        '<p class="setting-row__hint">Refresh active-speaker status to rehearse the durable safety sequence without sound.</p></div>' +
        '<span class="status-pill">not checked</span></div>' +
      '</div>';
    }
    return '<div class="output-card output-card--rehearsal">' +
      '<div class="output-card__head"><div><p class="output-card__title">Commissioning rehearsal</p>' +
        '<p class="setting-row__hint">' + escapeHtml(rehearsal.next_step || 'No sound is played by this rehearsal.') + '</p></div>' +
        '<span class="status-pill' + outputStatusClass(statusValue === 'blocked' ? 'blocked' : 'valid') + '">' +
          escapeHtml(statusValue.replace(/_/g, ' ')) + '</span></div>' +
      '<ol class="output-sequence__list">' + steps.map(function(step) {
        var stepStatus = step.status || 'pending';
        return '<li class="output-sequence__item output-sequence__item--' + escapeHtml(stepStatus) + '">' +
          '<span>' + escapeHtml(stepStatus.replace(/_/g, ' ')) + '</span>' +
          '<strong>' + escapeHtml(step.label || step.id || 'Step') + '</strong>' +
          '<p>' + escapeHtml(step.message || '') + '</p>' +
        '</li>';
      }).join('') + '</ol>' +
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
      return targetLabel ? 'Floor confirmed for ' + targetLabel : 'Floor confirmed for last target';
    }
    return 'Floor required';
  }
  function readinessTargetLockReason(readiness) {
    var target = readiness && readiness.target || {};
    var audible = readiness && readiness.audible_test || {};
    if (target.role === 'tweeter') {
      return 'Tweeter and horn audible tests are intentionally locked in this slice.';
    }
    if (audible.target_role_allowed === false) {
      return 'Audible tests are limited to woofer, mid, and subwoofer targets in this slice.';
    }
    return '';
  }
  function readinessBlockedReasons(readiness) {
    var reasons = [];
    var gates = Array.isArray(readiness.required_gates) ? readiness.required_gates : [];
    var issues = Array.isArray(readiness.issues) ? readiness.issues : [];
    gates.forEach(function(gate) {
      if (!gate || gate.passed) return;
      reasons.push(gate.message || gate.label || gate.id || 'Readiness gate is blocked.');
    });
    issues.forEach(function(issue) {
      if (!issue) return;
      reasons.push(issue.message || issue.code || 'Readiness issue requires review.');
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
    var startup = readiness.startup_load || {};
    var backend = readiness.tone_backend || {};
    var level = readiness.calibration_level && readiness.calibration_level.test_signal || {};
    var audible = readiness.audible_test || {};
    var rows = [
      ['Target', target.label || target.role || 'unknown'],
      ['DAC output', target.physical_output_index == null ? 'unknown' : 'Output ' + (Number(target.physical_output_index) + 1)],
      ['Role policy', audible.target_role_allowed === false ? 'Locked in this slice' : 'Eligible after gates'],
      ['Backend', (backend.audio_enabled ? 'audible lab backend' : 'artifact-only') + (backend.test_pcm ? ' · ' + backend.test_pcm : '')],
      ['Rollback', startup.rollback_available ? 'available' : 'not ready'],
      ['Test level', level.requested_level_dbfs == null ? fmtDbfs(activeSpeakerLevelConfig().value) : fmtDbfs(level.requested_level_dbfs)]
    ];
    return '<dl class="active-speaker-facts output-readiness-summary">' + rows.map(function(row) {
      return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
    }).join('') + '</dl>';
  }
  function renderOutputReadinessBlockers(readiness) {
    var reasons = readinessBlockedReasons(readiness);
    if (!reasons.length) {
      return '<p class="setting-row__hint">' + escapeHtml(readiness.playback_allowed ?
        'No blocking readiness reasons; the audible lab backend is enabled for this target.' :
        'No blocking readiness reasons for artifact verification. Audible playback still requires explicit backend enablement.') + '</p>';
    }
    return '<div class="output-readiness-blockers">' +
      '<p class="setting-row__title">Why sound is blocked</p>' +
      '<ul class="active-speaker-issues">' + reasons.map(function(reason) {
        return '<li>' + escapeHtml(reason) + '</li>';
      }).join('') + '</ul>' +
    '</div>';
  }
  function renderOutputCompressionDriverReadiness(readiness) {
    var horn = readiness && readiness.compression_driver;
    if (!horn || !horn.applies) return '';
    var mic = horn.microphone || {};
    var gates = Array.isArray(horn.required_gates) ? horn.required_gates : [];
    var statusLabel = {
      guided_ready_no_audio: 'Guided evidence ready',
      manual_ready_no_audio: 'Manual evidence ready',
      blocked: 'Blocked'
    }[horn.status] || horn.status || 'Unknown';
    var rows = [
      ['Audio allowed', horn.audio_allowed ? 'Yes' : 'No'],
      ['Protection path', horn.protection_mode || 'unknown'],
      ['Manual floor test', horn.manual_floor_test_candidate ? 'candidate' : 'blocked'],
      ['Guided floor test', horn.guided_floor_test_candidate ? 'candidate' : 'blocked'],
      ['Mic status', mic.status || 'unknown'],
      ['Mic reading', mic.observed_dbfs == null ? 'none' : fmtDbfs(Number(mic.observed_dbfs))]
    ];
    return '<div class="active-speaker-plan output-horn-readiness">' +
      '<div class="row-between active-speaker-level__head">' +
        '<div><p class="setting-row__title">Horn bring-up readiness</p>' +
        '<p class="setting-row__hint">No horn audio is enabled here; this only summarizes future bring-up evidence.</p></div>' +
        '<span class="status-pill' + (horn.status === 'blocked' ? ' status-pill--blocked' : ' status-pill--ready') + '">' +
          escapeHtml(statusLabel) + '</span></div>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      '<ul class="output-safety-list">' + gates.slice(0, 10).map(function(gate) {
        return '<li class="output-safety-list__item output-safety-list__item--' + escapeHtml(gate.passed ? 'info' : 'blocker') + '">' +
          '<span>' + escapeHtml(gate.label || gate.id || 'gate') + '</span>' +
          '<p>' + escapeHtml(gate.message || (gate.passed ? 'Passed' : 'Blocked')) + '</p>' +
        '</li>';
      }).join('') + '</ul>' +
      '<p class="setting-row__hint">' + escapeHtml(horn.next_step || 'Horn audio remains disabled.') + '</p>' +
    '</div>';
  }
  function renderOutputReadinessCard() {
    if (outputTopology.dirty) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">Save the output setup before checking a channel.</p></div>' +
        '<span class="status-pill">draft</span></div>' +
      '</div>';
    }
    if (outputTopology.readinessChecking) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">Checking the selected saved channel. No sound will play.</p></div>' +
        '<span class="status-pill">checking</span></div>' +
      '</div>';
    }
    if (outputTopology.readinessError) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">The last readiness check failed. The saved output setup is still available.</p></div>' +
        '<span class="status-pill status-pill--blocked">check failed</span></div>' +
        '<p class="setting-row__hint">' + escapeHtml(outputTopology.readinessError) + '</p>' +
      '</div>';
    }
    var readiness = outputTopology.readiness;
    if (!readiness) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">Choose Check readiness on one saved channel to see the no-audio safety checklist.</p></div>' +
        '<span class="status-pill">not checked</span></div>' +
      '</div>';
    }
    var target = readiness.target || {};
    var gates = Array.isArray(readiness.required_gates) ? readiness.required_gates : [];
    var rows = gates.map(function(gate) {
      return [
        gate.passed ? 'info' : 'blocker',
        gate.label || gate.id || 'gate',
        gate.message || (gate.passed ? 'Passed' : 'Blocked')
      ];
    });
    var statusValue = readiness.preconditions_passed ? 'Preconditions passed' : 'Blocked';
    return '<div class="output-card output-card--readiness">' +
      '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">' + escapeHtml(target.label || 'Selected channel') + '</p></div>' +
        '<span class="status-pill' + (readiness.preconditions_passed ? ' status-pill--ready' : ' status-pill--blocked') + '">' +
          escapeHtml(statusValue) + '</span></div>' +
      renderOutputReadinessSummary(readiness) +
      renderOutputReadinessBlockers(readiness) +
      renderOutputCompressionDriverReadiness(readiness) +
      '<ul class="output-safety-list">' + rows.slice(0, 10).map(function(row) {
        return '<li class="output-safety-list__item output-safety-list__item--' + escapeHtml(row[0]) + '">' +
          '<span>' + escapeHtml(row[1]) + '</span>' +
          '<p>' + escapeHtml(row[2]) + '</p>' +
        '</li>';
      }).join('') + '</ul>' +
      '<p class="setting-row__hint">' + escapeHtml(readiness.next_step || 'No audio was emitted.') + '</p>' +
      '<p class="setting-row__hint">Playback allowed: ' + escapeHtml(readiness.playback_allowed ? 'yes' : 'no') + '. This checklist does not play sound.</p>' +
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
      'Verifying' : 'Verify artifact';
    var roleLabel = humanRole(target.role || 'channel').toLowerCase();
    var playLabel = outputTopology.readinessPlaybackChecking === 'audio' ?
      'Playing' : 'Play quiet ' + roleLabel + ' test';
    var hints = [];
    if (lockReason) hints.push(lockReason + ' Artifact verification stays locked too so the UI cannot imply this target is ready for sound.');
    if (readiness && readiness.preconditions_passed && !atFloor && !floorConfirmed) {
      hints.push('Reset calibration level to the quiet floor before verifying an artifact or playing a test.');
    }
    if (readiness && readiness.preconditions_passed && floorConfirmed && !atFloor) {
      hints.push('Floor audio is confirmed for this target/session; raised tests remain bounded by the calibration level guard.');
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
      ['Playback status', playback.status || 'unknown'],
      ['Backend', playback.backend || 'none'],
      ['Target', target.label || target.driver_role || target.role || 'unknown'],
      ['Tone', toneSummary(playback.tone || {})],
      ['Artifact', artifact.wav_basename || 'none'],
      ['Audio emitted', playback.audio_emitted ? 'Yes' : 'No']
    ];
    var issues = Array.isArray(playback.issues) ? playback.issues.slice(0, 4) : [];
    return '<div class="active-speaker-plan output-readiness-playback">' +
      '<p class="setting-row__title">Channel test result</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Playback: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
    '</div>';
  }
  function renderOutputSafetyCard(topology, statusValue) {
    var evaluation = outputEvaluation(topology);
    var clock = outputClockDomainReport();
    var blockers = Array.isArray(evaluation.blockers) ? evaluation.blockers : [];
    var warnings = Array.isArray(evaluation.warnings) ? evaluation.warnings : [];
    var safety = topology.safety || evaluation.safety || {};
    var rows = [];
    if (outputTopology.dirty) {
      rows.push(['warning', 'unsaved_draft', 'Save to run backend validation on this draft.']);
    }
    blockers.forEach(function(issue) { rows.push(['blocker', issue.code, issue.message]); });
    warnings.forEach(function(issue) { rows.push(['warning', issue.code, issue.message]); });
    if (clock && clock.status && clock.status !== 'single_device_clock') {
      rows.push(['warning', 'clock_domain', clock.recommendation || 'Output clocking needs review.']);
    }
    rows.push([
      safety.sound_tests_allowed ? 'warning' : 'info',
      'sound_tests_allowed',
      safety.sound_tests_allowed ? 'Sound tests are enabled.' : 'Sound tests remain disabled for this setup surface.'
    ]);
    return '<div class="output-card output-card--safety">' +
      '<div class="output-card__head"><div><p class="output-card__title">Safety evidence</p>' +
        '<p class="setting-row__hint">Backend validation owns the final decision.</p></div>' +
        '<span class="status-pill' + outputStatusClass(statusValue) + '">' + escapeHtml(statusValue) + '</span></div>' +
      '<ul class="output-safety-list">' + rows.slice(0, 8).map(function(row) {
        return '<li class="output-safety-list__item output-safety-list__item--' + escapeHtml(row[0]) + '">' +
          '<span>' + escapeHtml(row[1]) + '</span>' +
          '<p>' + escapeHtml(row[2]) + '</p>' +
        '</li>';
      }).join('') + '</ul>' +
    '</div>';
  }
  function renderActiveSpeakerStatus() {
    if (activeSpeaker.loading) {
      return '<div class="row-between active-speaker-status__head">' +
        '<span class="status-pill">Checking environment</span>' +
        '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker" disabled>Refresh</button>' +
      '</div>';
    }
    if (activeSpeaker.error) {
      return '<div class="active-speaker-status__stack">' +
        '<div class="row-between active-speaker-status__head">' +
          '<span class="status-pill status-pill--blocked">Probe failed</span>' +
          '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker">Retry</button>' +
        '</div>' +
        '<p class="setting-row__hint">' + escapeHtml(activeSpeaker.error) + '</p>' +
      '</div>';
    }
    if (!activeSpeaker.payload) {
      return '<div class="row-between active-speaker-status__head">' +
        '<span class="status-pill status-pill--planned">Not checked</span>' +
        '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker">Check environment</button>' +
      '</div>';
    }
    var p = activeSpeaker.payload || {};
    var cfg = p.camilla_config || {};
    var alsa = p.alsa || {};
    var validation = p.camilla_validation || {};
    var safe = p.safe_playback || {};
    var session = activeSpeaker.session || {};
    var staged = activeSpeaker.stagedConfig || {};
    var ok = !!p.ok_to_load_active_config;
    var devices = Array.isArray(alsa.devices) ? alsa.devices.length : 0;
    var rows = [
      ['Camilla config', cfg.label || cfg.classification || 'Unknown'],
      ['Playback lane', (cfg.playback_device || 'Unknown') + (cfg.playback_channels ? ' · ' + cfg.playback_channels + ' ch' : '')],
      ['Volume ceiling', cfg.volume_limit_db == null ? 'Missing' : fmtDb(cfg.volume_limit_db) + ' dB'],
      ['ALSA playback devices', String(devices)],
      ['Config validation', validation.status || 'unknown'],
      ['Staged startup', staged.status || 'not staged'],
      ['Startup load', activeSpeaker.startupLoad && activeSpeaker.startupLoad.state ?
        (activeSpeaker.startupLoad.state.status || 'idle') : 'idle'],
      ['Safe playback', safe.playback_allowed ? 'Allowed' : 'Not allowed yet'],
      ['Safety session', session.status || 'Not armed'],
      ['Quiet start', quietStartLabel(session)],
      ['Calibration level', fmtDbfs(activeSpeakerLevelConfig().value)]
    ];
    var envIssues = Array.isArray(p.issues) ? p.issues.slice(0, 4) : [];
    var sessionIssues = Array.isArray(session.issues) ? session.issues.slice(0, 4) : [];
    return '<div class="active-speaker-status__stack">' +
      '<div class="row-between active-speaker-status__head">' +
        '<span class="status-pill ' + (ok ? 'status-pill--ready' : 'status-pill--blocked') + '">' +
          escapeHtml(ok ? 'Load gate ready' : 'Load gate blocked') + '</span>' +
        '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker">Refresh</button>' +
      '</div>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      renderActiveSpeakerIssues(envIssues, sessionIssues) +
      renderActiveSpeakerStagedConfig(activeSpeaker.stagedConfig) +
      renderActiveSpeakerBringup(activeSpeaker.bringup) +
      renderActiveSpeakerStartupLoad(activeSpeaker.startupLoad) +
      renderActiveSpeakerLevel() +
      renderActiveSpeakerActions(ok, session) +
      renderActiveSpeakerPlan(activeSpeaker.plan) +
      renderActiveSpeakerPlayback(activeSpeaker.playback) +
      '<p class="setting-row__hint">' + escapeHtml(safe.warning || 'Playback remains disabled until the safe tone path is implemented.') + '</p>' +
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
      start_at_minimum: 'Start at the minimum level.',
      raise_slowly: 'Raise slowly, one backend-approved step at a time.',
      hold_level: 'Hold this level for the next check.',
      lower_level: 'Lower the level before continuing.',
      stop_or_lower: 'Stop or lower; clipping resets the level to the floor.'
    }[code] || 'Record a mic reading before treating this as guided calibration.';
  }
  function renderActiveSpeakerLevel() {
    var cfg = activeSpeakerLevelConfig();
    var contract = activeSpeaker.calibrationLevel ||
      activeSpeaker.plan && activeSpeaker.plan.calibration_level ||
      activeSpeaker.targets && activeSpeaker.targets.calibration_level || {};
    var meter = contract.mic_meter || {};
    var guard = contract.software_gain_guard || {};
    var issues = Array.isArray(contract.issues) ? contract.issues : [];
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
    return '<div class="active-speaker-level">' +
      '<div class="row-between active-speaker-level__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Calibration level</p>' +
          '<p class="setting-row__hint">Test-signal level only. Normal listening volume is untouched.</p>' +
        '</div>' +
        '<span class="active-speaker-level__readout" id="active-speaker-level-readout">' +
          escapeHtml(fmtDbfs(cfg.value)) + '</span>' +
      '</div>' +
      '<input type="range" class="active-speaker-level__range" id="active-speaker-level" ' +
        'min="' + cfg.min + '" max="' + cfg.max + '" step="' + cfg.step + '" value="' + cfg.value + '" ' +
        'aria-label="Calibration test signal level">' +
      '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="active-level" data-level-action="lower">Lower</button>' +
        '<button type="button" class="btn btn--ghost" data-act="active-level" data-level-action="reset">Reset</button>' +
        '<button type="button" class="btn btn--ghost" data-act="active-level" data-level-action="raise">Raise 1 dB</button>' +
      '</div>' +
      '<div class="active-speaker-meter">' +
        '<span class="active-speaker-meter__label">Quiet</span>' +
        '<span class="active-speaker-meter__label">Usable</span>' +
        '<span class="active-speaker-meter__label">High</span>' +
      '</div>' +
      '<div class="row-between active-speaker-level__meter">' +
        '<span class="status-pill' + toneClass + '">' + escapeHtml(label) + '</span>' +
        '<span class="setting-row__hint">JTS caps this at ' + escapeHtml(fmtDbfs(cfg.max)) +
          ' and limits upward moves to ' + escapeHtml(fmtDb(Number(guard.upward_step_limit_db) || cfg.step)) + ' dB.</span>' +
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
        '<p class="setting-row__hint">This records operator-observed capture level only. It does not play sound or claim calibrated SPL.</p>' +
      '</div>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.slice(0, 3).map(function(issue) {
        return '<li>' + escapeHtml('Level guard: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
    '</div>';
  }
  function renderActiveSpeakerIssues(envIssues, sessionIssues) {
    var rows = [];
    envIssues.forEach(function(issue) {
      rows.push(['Environment', issue]);
    });
    sessionIssues.forEach(function(issue) {
      rows.push(['Session', issue]);
    });
    if (!rows.length) {
      return '<p class="setting-row__hint">No active load blockers in the read-only environment report.</p>';
    }
    return '<ul class="active-speaker-issues">' + rows.slice(0, 6).map(function(row) {
      var issue = row[1] || {};
      return '<li>' + escapeHtml(row[0] + ': ' + (issue.code || 'issue')) + '</li>';
    }).join('') + '</ul>';
  }
  function renderActiveSpeakerStagedConfig(staged) {
    if (!staged || staged.status === 'not_staged') return '';
    var cfg = staged.config || {};
    var preset = staged.preset || {};
    var load = staged.load || {};
    var rows = [
      ['Stage status', staged.status || 'unknown'],
      ['Preset', preset.name || preset.preset_id || 'unknown'],
      ['Config', cfg.basename || 'none'],
      ['Playback device', cfg.playback_device || 'missing'],
      ['Channels', cfg.playback_channels == null ? 'unknown' : String(cfg.playback_channels)],
      ['Validation', cfg.validation && cfg.validation.status || 'unknown'],
      ['Protective HP', cfg.tweeter_protective_highpass_hz ?
        String(cfg.tweeter_protective_highpass_hz) + ' Hz' : 'unknown'],
      ['Load gate', load.load_gate || 'startup load not checked']
    ];
    var issues = Array.isArray(staged.issues) ? staged.issues.slice(0, 5) : [];
    return '<div class="active-speaker-plan active-speaker-stage">' +
      '<p class="setting-row__title">Protected startup config</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Stage: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(staged.next_step || 'Staged only; no DSP graph was loaded.') + '</p>' +
    '</div>';
  }
  function modeStatusLabel(mode) {
    return {
      ready: 'Ready',
      ready_to_arm: 'Ready to arm',
      armed: 'Armed',
      ready_relative: 'Mic relative',
      ready_calibrated: 'Mic calibrated',
      blocked: 'Blocked'
    }[mode && mode.status] || (mode && mode.status) || 'Unknown';
  }
  function renderActiveSpeakerBringup(preflight) {
    if (!preflight) return '';
    var modes = preflight.modes || {};
    var manual = modes.manual_guarded_bringup || {};
    var guided = modes.guided_calibration || {};
    var mic = preflight.microphone || {};
    var guard = preflight.software_guard || {};
    var level = preflight.calibration_level || {};
    var failed = []
      .concat(Array.isArray(manual.required_gates) ? manual.required_gates : [])
      .concat(Array.isArray(guided.required_gates) ? guided.required_gates : [])
      .filter(function(gate, index, arr) {
        if (!gate || gate.passed) return false;
        var messageKey = String(gate.message || '').trim().toLowerCase();
        return arr.findIndex(function(item) {
          if (!item || item.passed) return false;
          if (item.id && gate.id && item.id === gate.id) return true;
          return messageKey && String(item.message || '').trim().toLowerCase() === messageKey;
        }) === index;
      })
      .slice(0, 5);
    var rows = [
      ['Manual guarded', modeStatusLabel(manual)],
      ['Guided calibration', modeStatusLabel(guided)],
      ['Microphone', mic.status || 'not checked'],
      ['Guard', guard.status || 'unknown'],
      ['Start level', level.at_floor ? 'At floor' : 'Reset needed']
    ];
    return '<div class="active-speaker-plan active-speaker-stage">' +
      '<p class="setting-row__title">Bring-up preflight</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (failed.length ? '<ul class="active-speaker-issues">' + failed.map(function(gate) {
        return '<li>' + escapeHtml('Preflight: ' + (gate.message || gate.id || 'gate blocked')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(preflight.next_step || 'Choose guided calibration when a mic is working; manual guarded bring-up stays available for known plans.') + '</p>' +
    '</div>';
  }
  function renderActiveSpeakerStartupLoad(startupLoad) {
    if (!startupLoad) return '';
    var state = startupLoad.state || {};
    var preflight = startupLoad.preflight || {};
    var candidate = preflight.candidate || {};
    var canLoad = !!preflight.load_allowed;
    var canRollback = !!state.rollback_available;
    var busy = !!activeSpeaker.action;
    var rows = [
      ['Load state', state.status || 'idle'],
      ['Preflight', preflight.status || 'unknown'],
      ['Candidate', candidate.basename || 'none'],
      ['Path safety', preflight.path_safety && preflight.path_safety.load_gate || 'unknown'],
      ['Rollback target', state.previous_config_path ? state.previous_config_path.split('/').pop() : 'none']
    ];
    var issues = []
      .concat(Array.isArray(preflight.issues) ? preflight.issues : [])
      .concat(Array.isArray(state.issues) ? state.issues : [])
      .filter(function(issue, index, arr) {
        if (!issue) return false;
        var code = issue.code || '';
        return arr.findIndex(function(item) {
          return item && item.code === code;
        }) === index;
      })
      .slice(0, 5);
    return '<div class="active-speaker-plan active-speaker-stage">' +
      '<p class="setting-row__title">Startup load</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Load: ' + (issue.message || issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="check-active-path-safety"' +
          (busy ? ' disabled' : '') + '>Check protected path</button>' +
        '<button type="button" class="btn btn--ghost" data-act="load-active-startup"' +
          (busy || !canLoad ? ' disabled' : '') + '>Load protected config</button>' +
        '<button type="button" class="btn btn--ghost" data-act="rollback-active-startup"' +
          (busy || !canRollback ? ' disabled' : '') + '>Rollback to prior config</button>' +
      '</div>' +
      '<p class="setting-row__hint">' + escapeHtml(preflight.next_step || 'Loading reloads CamillaDSP but does not play sound.') + '</p>' +
    '</div>';
  }
  function renderActiveSpeakerActions(ok, session) {
    var busy = !!activeSpeaker.action;
    var state = session || {};
    var targets = activeSpeaker.targets && Array.isArray(activeSpeaker.targets.targets)
      ? activeSpeaker.targets.targets : [];
    var stageDisabled = outputTopology.dirty ? ' disabled' : '';
    if (busy) {
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" disabled>' + escapeHtml(activeSpeaker.action) + '</button>' +
        '<span class="setting-row__hint">No audio is emitted by this step.</span>' +
      '</div>';
    }
    if (state.status === 'armed') {
      var targetButtons = targets.length ? targets.slice(0, 6).map(function(target) {
        var label = target.label || ((target.side || '') + ' ' + (target.driver_role || 'channel'));
        return '<button type="button" class="btn btn--ghost" data-act="prepare-active-tone" ' +
          'data-side="' + escapeHtml(target.side || '') + '" ' +
          'data-driver-role="' + escapeHtml(target.driver_role || '') + '">' +
          'Prepare ' + escapeHtml(label) + '</button>';
      }).join('') : '<span class="setting-row__hint">No preset channel targets available.</span>';
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--danger" data-act="stop-active-speaker">Stop</button>' +
        '<button type="button" class="btn btn--ghost" data-act="stage-active-config"' + stageDisabled + '>Stage protected config</button>' +
        '<span class="setting-row__hint">Armed safety session. Artifact checks are available; audible tests require saved output readiness and explicit lab enablement.</span>' +
      '</div>' +
      '<div class="active-speaker-actions active-speaker-actions--targets">' +
        targetButtons +
      '</div>';
    }
    return '<div class="active-speaker-actions">' +
      '<button type="button" class="btn btn--ghost" data-act="stage-active-config"' + stageDisabled + '>Stage protected config</button>' +
      '<button type="button" class="btn btn--ghost" data-act="arm-active-speaker"' + (ok ? '' : ' disabled') + '>Arm safe session</button>' +
      '<span class="setting-row__hint">Arming records the safety state only; it does not play sound.</span>' +
    '</div>';
  }
  function renderActiveSpeakerPlan(plan) {
    if (!plan) return '';
    var tone = plan.tone || {};
    var target = plan.target || {};
    var rows = [
      ['Plan status', plan.status || 'unknown'],
      ['Target', target.label || target.driver_role || 'unknown'],
      ['Tone', (tone.frequency_hz || '?') + ' Hz at ' + fmtDbfs(tone.level_dbfs)],
      ['Duration', String(tone.duration_ms || '?') + ' ms'],
      ['Would play', plan.would_play ? 'Yes' : 'No']
    ];
    var issues = Array.isArray(plan.issues) ? plan.issues.slice(0, 4) : [];
    return '<div class="active-speaker-plan">' +
      '<p class="setting-row__title">Prepared channel test</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Plan: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      (plan.status === 'ready' ? '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="verify-active-tone">Verify tone artifact</button>' +
        '<span class="setting-row__hint">Generates a no-audio multi-channel WAV for inspection.</span>' +
      '</div>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(plan.next_step || 'Prepared only; no sound was emitted.') + '</p>' +
    '</div>';
  }
  function renderActiveSpeakerPlayback(playback) {
    if (!playback) return '';
    var artifact = playback.artifact || {};
    var target = playback.target || {};
    var rows = [
      ['Playback status', playback.status || 'unknown'],
      ['Backend', playback.backend || 'none'],
      ['Target', target.label || target.driver_role || 'unknown'],
      ['Artifact', artifact.wav_basename || 'none'],
      ['Channels', artifact.channel_count == null ? 'unknown' : String(artifact.channel_count)],
      ['Audio emitted', playback.audio_emitted ? 'Yes' : 'No']
    ];
    var issues = Array.isArray(playback.issues) ? playback.issues.slice(0, 4) : [];
    return '<div class="active-speaker-plan">' +
      '<p class="setting-row__title">Tone artifact check</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Playback: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(playback.audio_emitted ?
        'Audio was emitted by the explicitly enabled lab backend.' :
        'No audio was emitted by this backend.') + '</p>' +
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
      ? Object.keys(draft.simple_eq).filter(function(k) { return Math.abs(draft.simple_eq[k]) >= 0.05; }).length
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
      ? Object.keys(draft.simple_eq).filter(function(k) { return Math.abs(draft.simple_eq[k]) >= 0.05; }).length
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
    else if (act === 'output-template') { setOutputTemplate(t.getAttribute('data-template') || ''); }
    else if (act === 'toggle-output-subwoofer') { toggleOutputSubwoofer(t.getAttribute('data-mode') || 'add'); }
    else if (act === 'output-step-next') { advanceOutputStep(t.getAttribute('data-step') || ''); }
    else if (act === 'save-output-topology') { saveOutputTopology(); }
    else if (act === 'copy-driver-research-prompt') { copyDriverResearchPrompt(); }
    else if (act === 'parse-driver-research') { parseDriverResearchImport(); }
    else if (act === 'mark-output-identity') { updateOutputChannelIdentity(t); }
    else if (act === 'mark-output-protection') { updateOutputChannelProtection(t); }
    else if (act === 'check-output-readiness') { checkOutputPlaybackReadiness(t); }
    else if (act === 'play-output-readiness-tone') { playOutputReadinessTone(t); }
    else if (act === 'stage-active-config') { stageActiveSpeakerConfig(); }
    else if (act === 'check-active-path-safety') { checkActivePathSafety(); }
    else if (act === 'load-active-startup') { loadActiveStartupConfig(); }
    else if (act === 'rollback-active-startup') { rollbackActiveStartupConfig(); }
    else if (act === 'arm-active-speaker') { activeSpeakerPost('./active-speaker/arm', 'Arming'); }
    else if (act === 'stop-active-speaker') { activeSpeakerPost('./active-speaker/stop', 'Stopping'); }
    else if (act === 'active-level') {
      updateActiveSpeakerLevel(t.getAttribute('data-level-action') || 'set');
    }
    else if (act === 'active-mic-observation') {
      recordActiveSpeakerMicObservation();
    }
    else if (act === 'prepare-active-tone') {
      activeSpeakerTonePlan({
        side: t.getAttribute('data-side') || '',
        driver_role: t.getAttribute('data-driver-role') || ''
      });
    }
    else if (act === 'verify-active-tone') { activeSpeakerTonePlayback(); }
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
      updateDriverResearchPromptPreview();
      return;
    }
    if (ev.target.hasAttribute && ev.target.hasAttribute('data-driver-import')) {
      driverResearch.importText = ev.target.value;
      driverResearch.error = '';
      driverResearch.parsed = null;
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
      activeSpeaker.levelDbfs = clamp(ev.target.value, cfg.min, cfg.max);
      var levelReadout = el('active-speaker-level-readout');
      if (levelReadout) levelReadout.textContent = fmtDbfs(activeSpeaker.levelDbfs);
      if (activeSpeaker.plan) {
        activeSpeaker.plan = null;
        activeSpeaker.playback = null;
        render();
      }
      return;
    }
    if (ev.target.id === 'active-speaker-mic-dbfs') {
      activeSpeakerMicObservation.observedDbfs = ev.target.value;
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
    if (ev.target && ev.target.classList && ev.target.classList.contains('output-step') &&
        ev.target.open) {
      outputStepOverride = ev.target.getAttribute('data-output-step') || outputStepOverride;
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
        return Math.abs(draft.simple_eq[s.field] || 0) >= 0.05;
      }).map(function(s) {
        return {enabled: true, type: s.type, freq_hz: s.freq_hz,
                gain_db: draft.simple_eq[s.field], q: s.type === 'Peaking' ? 1.0 : 1.0};
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
  }
  async function refreshOutputTopology(options) {
    options = options || {};
    if (!options.silent && outputTopology.dirty &&
        !await jtsConfirm('Reload output setup and lose the unsaved draft?')) {
      return;
    }
    if (!options.silent) outputTopology.touched = true;
    outputTopology.loading = true;
    outputTopology.error = '';
    if (!options.silent) render();
    try {
      var resp = await fetch('./output-topology', {cache: 'no-store'});
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'output setup failed');
      ingestOutputTopology(payload);
    } catch (e) {
      outputTopology.loading = false;
      outputTopology.error = e.message;
    }
    render();
  }
  function setOutputDraft(next) {
    outputTopology.draft = next;
    outputTopology.dirty = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    outputTopology.readiness = null;
    outputTopology.readinessChecking = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    activeSpeaker.stagedConfig = null;
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
  function baseOutputDraft(topology) {
    topology = topology || currentOutputTopology();
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
        hint: '2 amp channels: woofer/mid + tweeter',
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
        hint: '3 amp channels: woofer + mid + tweeter',
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
        hint: '4 amp channels: L/R woofer + L/R tweeter',
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
        hint: '6 amp channels: L/R woofer + mid + tweeter',
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
  async function setOutputTemplate(kind) {
    if (outputTopology.dirty &&
        !await jtsConfirm('Replace the unsaved output setup draft?')) {
      return;
    }
    var next = baseOutputDraft();
    if (!next || !next.hardware) {
      status('Load output hardware before creating a speaker map.', true);
      return;
    }
    var count = Number(next.hardware.physical_output_count) || 0;
    var template = outputTemplateDefinition(kind);
    if (!template) {
      status('Choose a supported output setup template.', true);
      return;
    }
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
    setOutputDraft(next);
    status('Output setup template is a draft. Save to validate; no sound will play.');
  }
  function toggleOutputSubwoofer(modeValue) {
    var topology = currentOutputTopology();
    if (!topology) {
      status('Load output hardware before editing the speaker map.', true);
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
      'Removed subwoofer from the output setup draft.' :
      'Added subwoofer to the output setup draft. Save before verification.');
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
      status('Driver research JSON parsed. Review it before using any values.');
    } catch (e) {
      driverResearch.parsed = null;
      driverResearch.error = e.message;
      status('Driver research JSON needs review: ' + e.message, true);
    }
    render();
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
      status('Output map is already saved. Continue with driver research or skip ahead.');
      return;
    }
    if (step === 'research') {
      if (driverResearch.importText.trim() && !driverResearch.parsed) {
        parseDriverResearchImport();
        if (!driverResearch.parsed) return;
      }
      openOutputStep('map');
      status(driverResearch.parsed ?
        'Driver research JSON parsed. Review values before using them.' :
        'Driver research is optional in this slice; continuing without applying values.');
      return;
    }
    if (step === 'map') {
      if (outputTopology.dirty) {
        outputStepOverride = 'map';
        status('Save the output map before recording or relying on physical verification.', true);
        render();
        return;
      }
      if (!outputIdentityComplete()) {
        var report = outputIdentityReport();
        var assigned = Number(report && report.assigned_channel_count || 0);
        outputStepOverride = 'map';
        status(assigned > 0 ?
          'Verify every assigned physical output before continuing to safety checks.' :
          'Save a speaker map with assigned physical outputs before continuing to safety checks.', true);
        render();
        return;
      }
      openOutputStep('safety');
      status('Output identity is complete. Continue with protected staging and readiness checks.');
      return;
    }
    if (step === 'safety') {
      outputStepOverride = 'safety';
      status('Use this card to check environment, stage the protected config, load only when allowed, and start quiet.');
      render();
      return;
    }
  }
  async function saveOutputTopology(options) {
    options = options || {};
    if (!outputTopology.draft) return;
    outputTopology.saving = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    activeSpeaker.stagedConfig = null;
    render();
    try {
      var resp = await fetch('./output-topology', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({output_topology: outputTopology.draft})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'output setup save failed');
      ingestOutputTopology(payload);
      if (options.nextStep) outputStepOverride = options.nextStep;
      status('Saved output setup. No sound was played.');
    } catch (e) {
      outputTopology.saving = false;
      outputTopology.error = e.message;
      status('Could not save output setup: ' + e.message, true);
    }
    render();
  }
  async function updateOutputChannelIdentity(button) {
    if (outputTopology.dirty) {
      status('Save the output setup before changing channel identity evidence.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var verified = button.getAttribute('data-verified') !== 'false';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var message = verified
      ? 'Mark "' + label + '" as physically verified? Only do this after wiring inspection, dummy-load/DMM checks, or a low-level channel test confirms it.'
      : 'Clear physical verification for "' + label + '"?';
    if (!await jtsConfirm(message, {danger: verified && role === 'tweeter'})) return;

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
      status((verified ? 'Marked verified: ' : 'Cleared verification: ') + label + '.');
    } catch (e) {
      outputTopology.identitySaving = '';
      outputTopology.error = e.message;
      status('Could not update channel identity: ' + e.message, true);
    }
    render();
  }
  async function updateOutputChannelProtection(button) {
    if (outputTopology.dirty) {
      status('Save the output setup before changing protection evidence.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var nextStatus = button.getAttribute('data-status') || (
      button.getAttribute('data-present') !== 'false' ? 'present' : 'required_missing'
    );
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var message = nextStatus === 'present'
      ? 'Mark physical compression-driver protection present for "' + label + '"? Only do this after the protection path is installed and inspected.'
      : (nextStatus === 'software_guard_requested'
        ? 'Use software-guarded bring-up for "' + label + '"? JTS will still block playback; this only allows a muted, high-passed, limited startup candidate to be staged for review.'
        : 'Clear compression-driver guard evidence for "' + label + '"?');
    if (!await jtsConfirm(message, {danger: nextStatus === 'present'})) return;

    outputTopology.protectionSaving = groupId + ':' + role;
    outputTopology.error = '';
    outputTopology.readinessError = '';
    outputTopology.readiness = null;
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    outputTopology.touched = true;
    activeSpeaker.stagedConfig = null;
    render();
    try {
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
      if (!resp.ok) throw new Error(payload.error || 'channel protection update failed');
      ingestOutputTopology(payload);
      outputTopology.protectionSaving = '';
      status('Set guard to ' + humanProtectionStatus(nextStatus) + ': ' + label + '.');
    } catch (e) {
      outputTopology.protectionSaving = '';
      outputTopology.error = e.message;
      status('Could not update channel protection: ' + e.message, true);
    }
    render();
  }
  async function checkOutputPlaybackReadiness(button) {
    if (outputTopology.dirty) {
      status('Save the output setup before checking playback readiness.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var targetId = groupId + ':' + role;
    outputTopology.readinessChecking = targetId;
    outputTopology.error = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    outputTopology.touched = true;
    render();
    try {
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
      outputTopology.readiness = payload;
      outputTopology.readinessChecking = '';
      status('Checked playback readiness for ' + label + '. No sound was played.');
    } catch (e) {
      outputTopology.readinessChecking = '';
      outputTopology.readinessError = e.message;
      status('Could not check playback readiness: ' + e.message, true);
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
        label + '"? This first audible slice is limited to woofer, mid, and subwoofer targets; horn/tweeter playback remains blocked.',
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
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: result.session || activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: result.plan || activeSpeaker.plan,
        playback: result.playback || activeSpeaker.playback,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
      status(audio ? 'Played quiet channel test.' : 'Verified channel test artifact. No sound was played.');
    } catch (e) {
      outputTopology.readinessPlaybackChecking = '';
      outputTopology.readinessError = e.message;
      status('Could not run channel test: ' + e.message, true);
    }
    render();
  }
  async function stageActiveSpeakerConfig() {
    if (outputTopology.dirty) {
      status('Save the output setup before staging protected config.', true);
      return;
    }
    if (!await jtsConfirm(
      'Stage a muted protected startup config from the saved output setup? This writes a candidate file only; it will not load CamillaDSP or play sound.',
      {danger: false}
    )) {
      return;
    }
    activeSpeaker = {
      loading: false, action: 'Staging protected config',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: activeSpeaker.plan,
      playback: activeSpeaker.playback,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
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
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: payload,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: startupLoad,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
      status(payload.status === 'staged' ?
        'Staged protected startup config. No DSP graph was loaded.' :
        'Protected startup config is blocked; review the staging evidence.',
        payload.status !== 'staged');
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
      status('Could not stage protected config: ' + e.message, true);
    }
    render();
  }
  async function updateActiveSpeakerLevel(action, requestedLevel) {
    var cfg = activeSpeakerLevelConfig();
    var body = {
      action: action || 'set'
    };
    if (requestedLevel != null) body.level_dbfs = requestedLevel;
    activeSpeaker = {
      loading: false, action: 'Updating level',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: null,
      playback: null,
      error: '',
      levelDbfs: cfg.value
    };
    render();
    try {
      var resp = await fetch('./active-speaker/calibration-level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'calibration level update failed');
      var accepted = payload && payload.test_signal ?
        Number(payload.test_signal.requested_level_dbfs) : cfg.value;
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: payload,
        bringup: activeSpeaker.bringup,
        startupLoad: startupLoad,
        plan: null,
        playback: null,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : cfg.value
      };
      await refreshActiveSpeakerRehearsal();
      status(payload.issues && payload.issues.length ?
        'Level raised one guarded step; larger upward move was limited.' :
        'Calibration level updated.');
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
      status('Could not update calibration level: ' + e.message, true);
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
    activeSpeakerMicObservation = {observedDbfs: raw, clipping: clipping};
    activeSpeaker = {
      loading: false, action: 'Recording mic reading',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: null,
      playback: null,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
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
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: payload,
        bringup: await bringupResp.json(),
        startupLoad: startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: null,
        playback: null,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
      status(meter.status === 'clipping' ?
        'Mic clipping recorded; calibration level reset to the floor.' :
        'Mic observation recorded.');
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
      status('Could not record mic observation: ' + e.message, true);
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
      activeSpeaker.rehearsal = await fetchActiveSpeakerCommissioningRehearsal();
    } catch (e) {
      activeSpeaker.rehearsal = activeSpeaker.rehearsal || null;
    }
  }
  async function fetchActiveSpeakerEnvironment() {
    var resp = await fetch('./active-speaker/environment', {cache: 'no-store'});
    if (!resp.ok) throw new Error('environment probe failed');
    return await resp.json();
  }
  async function checkActivePathSafety() {
    activeSpeaker = {
      loading: false, action: 'Checking protected path',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: null,
      playback: null,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
    render();
    try {
      var resp = await fetch('./active-speaker/check-path-safety', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'protected path check failed');
      var ready = payload.report && payload.report.ok_to_load_active_config;
      var environment = await fetchActiveSpeakerEnvironment();
      activeSpeaker = {
        loading: false, action: '',
        payload: environment,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: payload.startup_load || activeSpeaker.startupLoad,
        plan: null,
        playback: null,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
      status(ready ?
        'Protected path check passed. No sound was played.' :
        'Protected path check found blockers. No sound was played.',
        !ready);
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
      status('Could not check protected path: ' + e.message, true);
    }
    render();
  }
  async function loadActiveStartupConfig() {
    if (!await jtsConfirm(
      'Load the protected startup config into CamillaDSP? This reloads the DSP graph but does not play tones or change the calibration level.',
      {danger: false}
    )) {
      return;
    }
    activeSpeaker = {
      loading: false, action: 'Loading protected config',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: null,
      playback: null,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
    render();
    try {
      var resp = await fetch('./active-speaker/load-startup-config', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'startup load failed');
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: {
          state: payload.load || {},
          preflight: payload.preflight || {}
        },
        plan: null,
        playback: null,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
      var loaded = payload.load && payload.load.status === 'loaded';
      status(loaded ?
        'Protected startup config loaded. No sound was played.' :
        'Startup config load is blocked; review the load evidence.',
        !loaded);
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: null,
        playback: null,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
      status('Could not load protected config: ' + e.message, true);
    }
    render();
  }
  async function rollbackActiveStartupConfig() {
    if (!await jtsConfirm(
      'Rollback to the config that was active before the protected startup load? This reloads CamillaDSP but does not play sound.',
      {danger: false}
    )) {
      return;
    }
    activeSpeaker = {
      loading: false, action: 'Rolling back',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: null,
      playback: null,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
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
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: startupLoad.state ? startupLoad : {
          state: payload.rollback || {},
          preflight: activeSpeaker.startupLoad && activeSpeaker.startupLoad.preflight || {}
        },
        plan: null,
        playback: null,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
      var rolledBack = payload.rollback && payload.rollback.status === 'rolled_back';
      status(rolledBack ?
        'Rolled back to the prior config. No sound was played.' :
        'Startup rollback is blocked; review the load evidence.',
        !rolledBack);
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: null,
        playback: null,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
      status('Could not roll back startup config: ' + e.message, true);
    }
    render();
  }
  async function refreshActiveSpeakerStatus() {
    activeSpeaker = {
      loading: true, action: '',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: null,
      playback: null,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
    render();
    try {
      var envResp = await fetch('./active-speaker/environment', {cache: 'no-store'});
      if (!envResp.ok) throw new Error('environment probe failed');
      var sessionResp = await fetch('./active-speaker/safe-playback', {cache: 'no-store'});
      if (!sessionResp.ok) throw new Error('safe playback status failed');
      var stagedResp = await fetch('./active-speaker/staged-config', {cache: 'no-store'});
      if (!stagedResp.ok) throw new Error('staged config status failed');
      var levelResp = await fetch('./active-speaker/calibration-level', {cache: 'no-store'});
      if (!levelResp.ok) throw new Error('calibration level status failed');
      var bringupResp = await fetch('./active-speaker/bringup-preflight', {cache: 'no-store'});
      if (!bringupResp.ok) throw new Error('bring-up preflight failed');
      var startupLoadResp = await fetch('./active-speaker/startup-load', {cache: 'no-store'});
      if (!startupLoadResp.ok) throw new Error('startup load status failed');
      var rehearsalResp = await fetch('./active-speaker/commissioning-rehearsal', {cache: 'no-store'});
      if (!rehearsalResp.ok) throw new Error('commissioning rehearsal failed');
      var targetsResp = await fetch('./active-speaker/tone-targets', {cache: 'no-store'});
      if (!targetsResp.ok) throw new Error('tone targets failed');
      var nextLevel = await levelResp.json();
      var nextBringup = await bringupResp.json();
      var nextStartupLoad = await startupLoadResp.json();
      var nextRehearsal = await rehearsalResp.json();
      var nextTargets = await targetsResp.json();
      activeSpeaker = {
        loading: false, action: '',
        payload: await envResp.json(),
        session: await sessionResp.json(),
        targets: nextTargets,
        stagedConfig: await stagedResp.json(),
        calibrationLevel: nextLevel,
        bringup: nextBringup,
        startupLoad: nextStartupLoad,
        rehearsal: nextRehearsal,
        plan: null,
        playback: null,
        error: '',
        levelDbfs: nextLevel && nextLevel.test_signal ?
          Number(nextLevel.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs
      };
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '', payload: null, session: null, targets: null,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: null, playback: null, error: e.message, levelDbfs: activeSpeaker.levelDbfs
      };
    }
    render();
  }
  async function activeSpeakerPost(path, actionLabel) {
    activeSpeaker = {
      loading: false, action: actionLabel,
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: null,
      playback: null,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
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
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: nextSession,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: nextLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: startupLoad,
        plan: null,
        playback: null,
        error: '',
        levelDbfs: nextLevel && nextLevel.test_signal ?
          Number(nextLevel.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
      outputTopology.readiness = null;
      outputTopology.readinessChecking = '';
      outputTopology.readinessError = '';
      outputTopology.readinessPlayback = null;
      outputTopology.readinessPlaybackChecking = '';
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
    }
    render();
  }
  async function activeSpeakerTonePlan(target) {
    activeSpeaker = {
      loading: false, action: 'Preparing',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: activeSpeaker.plan,
      playback: activeSpeaker.playback,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
    render();
    try {
      var payload = Object.assign({}, target || {});
      var resp = await fetch('./active-speaker/tone-plan', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(payload)
      });
      if (!resp.ok) throw new Error('tone plan failed');
      var nextPlan = await resp.json();
      var returnedLevel = nextPlan && nextPlan.calibration_level &&
        nextPlan.calibration_level.test_signal ?
        Number(nextPlan.calibration_level.test_signal.requested_level_dbfs) :
        Number(nextPlan && nextPlan.tone && nextPlan.tone.level_dbfs);
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: nextPlan.calibration_level || activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: nextPlan,
        playback: null,
        error: '',
        levelDbfs: isFinite(returnedLevel) ? returnedLevel : activeSpeakerLevelConfig().value
      };
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
    }
    render();
  }
  async function activeSpeakerTonePlayback() {
    var target = activeSpeaker.plan && activeSpeaker.plan.target || {};
    activeSpeaker = {
      loading: false, action: 'Verifying',
      payload: activeSpeaker.payload,
      session: activeSpeaker.session,
      targets: activeSpeaker.targets,
      stagedConfig: activeSpeaker.stagedConfig,
      calibrationLevel: activeSpeaker.calibrationLevel,
      bringup: activeSpeaker.bringup,
      startupLoad: activeSpeaker.startupLoad,
      rehearsal: activeSpeaker.rehearsal,
      plan: activeSpeaker.plan,
      playback: activeSpeaker.playback,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
    render();
    try {
      var payload = {
        side: target.side || '',
        driver_role: target.driver_role || ''
      };
      var resp = await fetch('./active-speaker/play-tone', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(payload)
      });
      if (!resp.ok) throw new Error('tone artifact check failed');
      var result = await resp.json();
      var playback = result.playback || null;
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: result.session || activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: result.plan && result.plan.calibration_level || activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: result.plan || activeSpeaker.plan,
        playback: playback,
        error: '',
        levelDbfs: playback && playback.tone && isFinite(Number(playback.tone.level_dbfs)) ?
          Number(playback.tone.level_dbfs) : activeSpeaker.levelDbfs
      };
      await refreshActiveSpeakerRehearsal();
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        calibrationLevel: activeSpeaker.calibrationLevel,
        bringup: activeSpeaker.bringup,
        startupLoad: activeSpeaker.startupLoad,
        rehearsal: activeSpeaker.rehearsal,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
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
