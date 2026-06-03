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
// dom/format/charts/components/sections/views/api/actions/main — this is
// still one module. The editor's ~25 state vars are woven through its EQ
// math, innerHTML rendering, and the live-draft IO; splitting it into a
// shared store + eq/views/io modules is planned but MUST be exercised on
// the Pi (band-drag + live-draft → CamillaDSP) before merge. Do not
// blind-refactor it. See docs/HANDOFF-management-ui.md.
import { jtsConfirm } from "/assets/shared/js/dialog.js";
(function() {
  var LIMIT_DEFAULTS = {
    simple_gain_db: 12, advanced_gain_db: 12, max_parametric_bands: 8,
    min_freq_hz: 20, max_freq_hz: 20000, min_q: 0.2, max_q: 10,
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
    stagedConfig: null, plan: null, playback: null, error: '', levelDbfs: null
  };
  var outputTopology = {
    loading: false, saving: false, payload: null, draft: null,
    identity: null, clockDomain: null, identitySaving: '', protectionSaving: '',
    readiness: null, readinessChecking: '', readinessError: '',
    readinessPlayback: null, readinessPlaybackChecking: '',
    error: '', dirty: false, touched: false
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
  function specActive(s) { return Math.abs(Number(s.gain_db || 0)) >= 0.05; }
  function responseDb(spec, freq) {
    var f = Math.max(Number(freq) || 0, 1e-6);
    var c = Math.max(Number(spec.freq_hz || spec.freq || 1000), 1e-6);
    var gain = Number(spec.gain_db || 0);
    var type = spec.type || spec.biquad_type || 'Peaking';
    var x = Math.log(f / c) / Math.log(2);
    if (type === 'Lowshelf') return gain / (1 + Math.exp(3 * x));
    if (type === 'Highshelf') return gain / (1 + Math.exp(-3 * x));
    var q = Math.max(Number(spec.q || 1), 1e-3);
    return gain / (1 + Math.pow(x / (1 / q), 2));
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
      return {preview: [], components: {curve: [], simple: [], advanced: []}, off: true};
    }
    var all = curveSpecs(profile).concat(simpleSpecs(profile), advancedSpecs(profile));
    var preview = pointsFor(all, freqs, false);
    return {
      preview: preview,
      components: {
        curve: pointsFor(curveSpecs(profile), freqs, true),
        simple: pointsFor(simpleSpecs(profile), freqs, true),
        advanced: (profile.parametric_bands || []).map(function(b, i) {
          return {index: i, enabled: b.enabled !== false,
                  preview: pointsFor(advancedSpecs({parametric_bands: [b]}), freqs, true)};
        })
      }
    };
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
  function drawBandMarkers() {
    if (view !== 'draft' || mode !== 'peq') return '';
    var expandedBand = expandedPeqBandIndex();
    var html = '';
    (draft.parametric_bands || []).forEach(function(b, i) {
      if (!b || b.enabled === false) return;
      var sel = i === expandedBand ? ' selected' : '';
      var cx = gx(clamp(b.freq_hz, 20, 20000)), cy = gy(clamp(b.gain_db, MINDB, MAXDB));
      if ((b.type || 'Peaking') === 'Peaking' && i === expandedBand) {
        var q = Math.max(Number(b.q || 1), 0.2);
        var lo = gx(clamp(b.freq_hz / Math.pow(2, 1 / q), 20, 20000));
        var hi = gx(clamp(b.freq_hz * Math.pow(2, 1 / q), 20, 20000));
        html += '<rect class="band-width" x="' + Math.min(lo, hi).toFixed(1) +
                '" y="' + padT + '" width="' + Math.abs(hi - lo).toFixed(1) +
                '" height="' + (H - padB - padT) + '"></rect>';
      }
      html += '<line class="band-marker" x1="' + cx.toFixed(1) + '" x2="' + cx.toFixed(1) +
              '" y1="' + padT + '" y2="' + (H - padB) + '"></line>';
      html += '<circle class="band-dot' + sel + '" cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
              '" r="' + (sel ? 4.5 : 3.5) + '"></circle>';
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
    var comp = payload.components || {};
    if (enabled) {
      html += drawArea(payload.preview || []);
      var expandedBand = expandedPeqBandIndex();
      var bandComponent = (comp.advanced || []).find(function(item) {
        return item.index === expandedBand;
      });
      if (bandComponent) html += drawPath(bandComponent.preview || [], 'component selected');
    }
    var curvePts = enabled
      ? (payload.preview || [])
      : [{freq_hz: 20, db: 0}, {freq_hz: 20000, db: 0}];
    html += drawPath(curvePts, 'curve');
    html += drawBandMarkers();
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
    if (!profile) { renderGraph({preview: [], components: {}}, false); return; }
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
    n += profile.parametric_bands.filter(function(b) { return b.enabled !== false && Math.abs(b.gain_db) >= 0.05; }).length;
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
    var body = renderActiveSpeakerStatus();
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
          '</div>' +
          '<div class="active-speaker-status">' + body + '</div>' +
        '</div>' +
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
        (outputTopology.loading ? ' disabled' : '') + '>' + (topology ? 'Reload' : 'Load') + '</button>' +
      '<button type="button" class="btn btn--primary" data-act="save-output-topology"' +
        (saveDisabled ? ' disabled' : '') + '>' + (outputTopology.saving ? 'Saving' : 'Save') + '</button>' +
    '</div>';
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
      renderOutputSetupTemplates(topology) +
      renderOutputHardwareCard(topology, statusValue) +
      renderOutputStageCard(topology) +
      renderOutputGroupsCard(topology) +
      renderOutputIdentityCard() +
      renderOutputReadinessCard() +
      renderOutputSafetyCard(topology, statusValue) +
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
        var protection = channel.protection_required
          ? ' · protection ' + (channel.protection_status || 'unknown') : '';
        var label = channel.human_output_label ||
          (channel.physical_output_index == null ? 'No output assigned' : 'Output ' + (Number(channel.physical_output_index) + 1));
        var target = identityTargetFor(group.id, channel.role) || {};
        var targetId = target.id || (group.id + ':' + channel.role);
        var busy = outputTopology.identitySaving === targetId;
        var protectionBusy = outputTopology.protectionSaving === targetId;
        var readinessBusy = outputTopology.readinessChecking === targetId;
        var action = channel.identity_verified ? 'Clear' : 'Mark verified';
        var protectionPresent = channel.protection_status === 'present';
        var protectionAction = protectionPresent ? 'Clear protection' : 'Mark protection';
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
            (channel.protection_required ? '<button type="button" class="btn btn--ghost output-role__action" ' +
              'data-act="mark-output-protection" ' +
              'data-group-id="' + escapeHtml(group.id) + '" ' +
              'data-role="' + escapeHtml(channel.role) + '" ' +
              'data-present="' + (protectionPresent ? 'false' : 'true') + '" ' +
              'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
              (disabled ? ' disabled' : '') + '>' +
              escapeHtml(protectionBusy ? 'Saving' : protectionAction) + '</button>' : '') +
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
    var disabled = !readiness || !readiness.preconditions_passed ||
      outputTopology.readinessPlaybackChecking;
    var attrs = 'data-group-id="' + escapeHtml(target.speaker_group_id || '') + '" ' +
      'data-role="' + escapeHtml(target.role || '') + '"';
    var artifactLabel = outputTopology.readinessPlaybackChecking === 'artifact' ?
      'Verifying' : 'Verify artifact';
    var playLabel = outputTopology.readinessPlaybackChecking === 'audio' ?
      'Playing' : 'Play low-level test';
    return '<div class="active-speaker-actions">' +
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
      ['Safe playback', safe.playback_allowed ? 'Allowed' : 'Not allowed yet'],
      ['Safety session', session.status || 'Not armed'],
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
      (session.status === 'armed' ? renderActiveSpeakerLevel() : '') +
      renderActiveSpeakerActions(ok, session) +
      renderActiveSpeakerPlan(activeSpeaker.plan) +
      renderActiveSpeakerPlayback(activeSpeaker.playback) +
      '<p class="setting-row__hint">' + escapeHtml(safe.warning || 'Playback remains disabled until the safe tone path is implemented.') + '</p>' +
    '</div>';
  }
  function activeSpeakerLevelConfig() {
    var raw = activeSpeaker.targets && activeSpeaker.targets.calibration_level &&
      activeSpeaker.targets.calibration_level.test_signal || {};
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
  function renderActiveSpeakerLevel() {
    var cfg = activeSpeakerLevelConfig();
    var contract = activeSpeaker.plan && activeSpeaker.plan.calibration_level ||
      activeSpeaker.targets && activeSpeaker.targets.calibration_level || {};
    var meter = contract.mic_meter || {};
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
      '<div class="active-speaker-meter">' +
        '<span class="active-speaker-meter__label">Quiet</span>' +
        '<span class="active-speaker-meter__label">Usable</span>' +
        '<span class="active-speaker-meter__label">High</span>' +
      '</div>' +
      '<div class="row-between active-speaker-level__meter">' +
        '<span class="status-pill' + toneClass + '">' + escapeHtml(label) + '</span>' +
        '<span class="setting-row__hint">JTS caps this at ' + escapeHtml(fmtDbfs(cfg.max)) + '.</span>' +
      '</div>' +
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
      ['Load gate', load.load_gate || 'not implemented']
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
    var body = '';
    if (open) {
      body = '<div class="band-row__body">' +
        '<div class="range-row"><span class="range-row__label">Type</span>' +
          '<div class="segmented" data-band="' + index + '">' +
            typeBtn('Lowshelf', 'Low', band.type) + typeBtn('Peaking', 'Peak', band.type) +
            typeBtn('Highshelf', 'High', band.type) + '</div></div>' +
        rangeRow('Freq', band.freq_hz, limits.min_freq_hz, limits.max_freq_hz,
          {kind: 'freq', log: true, variant: 'thumb', step: 1, format: function(v) { return fmtFreq(v); }}) +
        rangeRow('Gain', band.gain_db, -limits.advanced_gain_db, limits.advanced_gain_db,
          {kind: 'gain', step: 0.1, format: function(v) { return fmtDb(v) + ' dB'; }}) +
        rangeRow('Width', band.q, limits.min_q, limits.max_q,
          {kind: 'q', step: 0.1, format: function(v) { return fmtQ(v); }}) +
        '<button type="button" class="band-row__delete" data-act="del-band" data-index="' + index + '">' +
          ico('trash') + 'Delete band</button>' +
      '</div>';
    }
    return '<div class="band-row" data-index="' + index + '" data-open="' + (open ? 'true' : 'false') + '">' +
      '<button type="button" class="band-row__header" data-act="toggle-band" data-index="' + index + '">' +
        '<span class="band-row__title">' +
          '<span class="band-dot' + (open ? ' band-dot--active' : '') + '">' + (index + 1) + '</span>' +
          '<span><p class="band-row__name">Band ' + (index + 1) + '</p>' +
            '<p class="band-row__meta">' + escapeHtml(band.type) + ' · ' + Math.round(band.freq_hz) +
            ' Hz · ' + band.gain_db.toFixed(1) + ' dB · Q ' + band.q.toFixed(1) + '</p></span>' +
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
    else if (act === 'save-output-topology') { saveOutputTopology(); }
    else if (act === 'mark-output-identity') { updateOutputChannelIdentity(t); }
    else if (act === 'mark-output-protection') { updateOutputChannelProtection(t); }
    else if (act === 'check-output-readiness') { checkOutputPlaybackReadiness(t); }
    else if (act === 'play-output-readiness-tone') { playOutputReadinessTone(t); }
    else if (act === 'stage-active-config') { stageActiveSpeakerConfig(); }
    else if (act === 'arm-active-speaker') { activeSpeakerPost('./active-speaker/arm', 'Arming'); }
    else if (act === 'stop-active-speaker') { activeSpeakerPost('./active-speaker/stop', 'Stopping'); }
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
      if (draft.parametric_bands[bi]) { draft.parametric_bands[bi].type = typeBtn.getAttribute('data-band-type'); activeBand = bi; onDraftChanged(true); }
    }
  });
  el('view-body').addEventListener('input', function(ev) {
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
      if (range === 'q') band.q = clamp(ev.target.value, limits.min_q, limits.max_q);
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
    if (ev.target.id === 'set-match-loudness') saveSettings({match_loudness: ev.target.checked});
    else if (ev.target.id === 'set-headroom') saveSettings({headroom_trim_db: Number(ev.target.value)});
  });
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
  function baseOutputDraft() {
    var topology = currentOutputTopology();
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
  async function saveOutputTopology() {
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
    var present = button.getAttribute('data-present') !== 'false';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var message = present
      ? 'Mark compression-driver protection present for "' + label + '"? Only do this after the physical protection path is installed and inspected.'
      : 'Clear compression-driver protection evidence for "' + label + '"?';
    if (!await jtsConfirm(message, {danger: present})) return;

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
          protection_present: present
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'channel protection update failed');
      ingestOutputTopology(payload);
      outputTopology.protectionSaving = '';
      status((present ? 'Marked protection present: ' : 'Cleared protection: ') + label + '.');
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
          role: role,
          level_dbfs: activeSpeakerLevelConfig().value
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
    var audio = button.getAttribute('data-audio') === 'true';
    if (audio && !await jtsConfirm(
      'Play a short low-level channel test? Keep the physical Stop control available and stop immediately if anything sounds wrong.',
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
          level_dbfs: activeSpeakerLevelConfig().value,
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
        plan: result.plan || activeSpeaker.plan,
        playback: result.playback || activeSpeaker.playback,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
      status(audio ? 'Played low-level channel test.' : 'Verified channel test artifact. No sound was played.');
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
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: payload,
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
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
        plan: activeSpeaker.plan,
        playback: activeSpeaker.playback,
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      };
      status('Could not stage protected config: ' + e.message, true);
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
      var targetsResp = await fetch('./active-speaker/tone-targets', {cache: 'no-store'});
      if (!targetsResp.ok) throw new Error('tone targets failed');
      var nextTargets = await targetsResp.json();
      activeSpeaker = {
        loading: false, action: '',
        payload: await envResp.json(),
        session: await sessionResp.json(),
        targets: nextTargets,
        stagedConfig: await stagedResp.json(),
        plan: null,
        playback: null,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs == null && nextTargets.calibration_level ?
          (nextTargets.calibration_level.test_signal || {}).default_level_dbfs :
          activeSpeaker.levelDbfs
      };
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '', payload: null, session: null, targets: null,
        stagedConfig: activeSpeaker.stagedConfig,
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
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: await resp.json(),
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
        plan: null,
        playback: null,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      };
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
      plan: activeSpeaker.plan,
      playback: activeSpeaker.playback,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
    render();
    try {
      var payload = Object.assign({}, target || {}, {
        level_dbfs: activeSpeakerLevelConfig().value
      });
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
      plan: activeSpeaker.plan,
      playback: activeSpeaker.playback,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    };
    render();
    try {
      var payload = {
        side: target.side || '',
        driver_role: target.driver_role || '',
        level_dbfs: activeSpeakerLevelConfig().value
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
        plan: result.plan || activeSpeaker.plan,
        playback: playback,
        error: '',
        levelDbfs: playback && playback.tone && isFinite(Number(playback.tone.level_dbfs)) ?
          Number(playback.tone.level_dbfs) : activeSpeaker.levelDbfs
      };
    } catch (e) {
      activeSpeaker = {
        loading: false, action: '',
        payload: activeSpeaker.payload,
        session: activeSpeaker.session,
        targets: activeSpeaker.targets,
        stagedConfig: activeSpeaker.stagedConfig,
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
