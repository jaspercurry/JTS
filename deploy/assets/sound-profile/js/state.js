// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — the page's shared records and its boot island.
//
// The /sound/eq/, /sound/speaker/ and /sound/output/ views share these records
// by reference: callers mutate their properties, never the bindings, so this
// module stays the one owner of each. `pageData` reads the JSON island at
// evaluation time, which is safe because the page loads main.js as a deferred
// module script.

var ACTIVE_GAIN_EPSILON_DB = 0.05;

var outputTopology = {
  loading: false, saving: false, resetting: false, repinning: false,
  payload: null, draft: null,
  identity: null, clockDomain: null, activeRoute: null,
  observedHardware: null,
  hardwareAdoption: null,
  hardwareMismatch: null,
  hardwareRepin: null,
  revision: null,
  identitySaving: '', protectionSaving: '',
  error: '', dirty: false, touched: false
};

var driverResearch = {
  inputs: {
    full_range: '', woofer: '', mid: '', tweeter: '', subwoofer: '', notes: '',
    target_models: {}
  },
  settings: {drivers: {}, crossovers: {}},
  importText: '',
  importedPayload: null,
  parsed: null,
  designDraft: null,
  error: '',
  dirty: false,
  safetyDirty: false,
  editedDriverTargets: {},
  saving: false,
  promptCopy: {copied: false, selected: false},
  researchRequest: null
};
var crossoverPreview = {payload: null, preparing: false, error: ''};

// The EQ editor's record (/sound/eq/). resetEqEditor() serves the exits that
// discard the naming UI outright — newDraft, editEntry, resetDraft,
// cancel-name and Escape — so naming/nameMode/nameDraft go back as one unit.
// finalizeName() deliberately clears `naming` alone and must not call it: it
// reads nameMode and nameDraft afterwards to choose save vs rename.
var eqEditor = {
  view: 'off',            // off | saved | draft
  mode: 'simple',         // simple | peq
  selectedId: null,       // selected library id on the Saved tab
  editing: {kind: 'new'}, // new | {kind:'user',id,name} | {kind:'preset',id,name}
  activeBand: 0,
  naming: false,
  nameMode: 'save',       // 'save' (new/copy) | 'rename'
  nameDraft: '',
  library: [],            // [{id,name,kind,editable,description,profile,...}]
  simpleBands: [],        // [{key,field,label,freq_hz,type}] from /state
  curvesById: {},
  // The loaded graph's EQ refusal ({reason_code, message}) from /state, or null
  // when it can host EQ (or nothing probed it). Whole-page state, not a status
  // line: only a payload that stops refusing clears it, never an editor exit,
  // so resetEqEditor() leaves it alone.
  carrierBlock: null
};

function resetEqEditor() {
  eqEditor.naming = false;
  eqEditor.nameMode = 'save';
  eqEditor.nameDraft = '';
}

// The /sound/output/ wizard's record: the saved sound settings it edits plus
// the in-flight picks of its own steps.
var outputPage = {
  // volume_floor_db is absent until /state carries it: savedVolumeFloorDb()
  // then falls back to volumeFloorDefault() (backend-owned) rather than this
  // module keeping a second copy of the default.
  soundSettings: {headroom_trim_db: 0, match_loudness: false},
  blocked: false,          // ./settings: the graph refused to carry EQ
  i2sHat: null,
  volumeFloorDraftDb: null,
  stepOverride: '',
  templateDraftAxes: {layout: '', speakerMode: ''}
};

// Clears templateDraftAxes only — the layout wizard's in-flight axis pick,
// dropped when a saved topology supersedes it. `stepOverride` is not part of
// that draft: which step is open is cleared by the re-pin path alone.
function resetOutputTemplateDraft() {
  outputPage.templateDraftAxes = {layout: '', speakerMode: ''};
}

function el(id) { return document.getElementById(id); }
// The crossover filters and slopes this page may OFFER, served on the island
// by jasper/web/sound_setup.py:_sound_page_island and owned by the compiler
// (jasper/active_speaker/profile.py's SUPPORTED_CROSSOVER_TYPES /
// SUPPORTED_LR_ORDERS, spelled by staging). Deliberately NOT re-stated here:
// a literal list would be a second answer to "what can JTS build", and the
// editor would go on offering a filter or slope the compiler refuses several
// screens later. An island that carries none leaves the pickers empty and
// blocks the save with a named reason rather than guessing a vocabulary.
function crossoverVocabularyFromIsland(raw) {
  var island = raw && typeof raw === 'object' ? raw : {};
  var filterTypes = Array.isArray(island.filter_types) ? island.filter_types : [];
  var slopes = Array.isArray(island.slopes_db_per_octave) ?
    island.slopes_db_per_octave : [];
  return {
    filterTypes: filterTypes.map(String),
    slopes: slopes.map(Number).filter(function(value) {
      return isFinite(value) && value > 0;
    }),
    defaultFilterType: island.default_filter_type == null ?
      '' : String(island.default_filter_type),
    defaultSlope: Number(island.default_slope_db_per_octave) || null
  };
}
// nginx selects one renderer mode on the same backend. EQ owns profiles and
// Match Loudness; Speaker owns the layout, drivers and local commissioning;
// Output owns the I2S HAT and volume shaping.
var pageData = (function() {
  var node = document.getElementById('sound-page-data');
  var text = node && node.textContent ? node.textContent.trim() : '';
  var legacyFollowerIsland = false;
  // Keep the standalone follower harness compatible with the old island.
  if (!text) {
    node = document.getElementById('sound-follower-data');
    text = node && node.textContent ? node.textContent.trim() : '';
    legacyFollowerIsland = !!text;
  }
  if (!text) {
    return {mode: 'eq', follower: false, crossoverVocabulary: crossoverVocabularyFromIsland(null)};
  }
  try {
    var parsed = JSON.parse(text);
    var mode = parsed.mode === 'speaker' || parsed.mode === 'output' ? parsed.mode : 'eq';
    return {
      mode: legacyFollowerIsland ? 'speaker' : mode,
      follower: parsed.follower === true,
      crossoverVocabulary: crossoverVocabularyFromIsland(parsed.crossover_vocabulary)
    };
  } catch (e) {
    // Split pages without EQ chrome must stay on the local-speaker side if the
    // tiny island is damaged; attempting EQ would dereference absent tabs.
    return {mode: 'speaker', follower: true, crossoverVocabulary: crossoverVocabularyFromIsland(null)};
  }
})();
var pageMode = pageData.mode;
var followerMode = pageData.follower;
var crossoverVocabulary = pageData.crossoverVocabulary;

export {
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
  resetOutputTemplateDraft,
};
