// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — the page's shared records and its boot island.
//
// The /sound/eq/, /sound/speaker/ and /sound/output/ views share these records
// by reference:
// callers mutate their properties, never the bindings, so this module stays
// the one owner of each. `pageData` reads the JSON island at evaluation time,
// which is safe because the page loads main.js as a deferred module script.

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
  followerMode,
  outputTopology,
  pageMode,
};
