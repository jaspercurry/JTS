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

import { readJsonIsland } from '../../shared/js/dom.js';

const ACTIVE_GAIN_EPSILON_DB = 0.05;

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
};

function el(id) { return document.getElementById(id); }
var pageData = (function() {
  var id = (el('sound-page-data') || {}).textContent?.trim() ?
    'sound-page-data' : 'sound-follower-data';
  // Damaged split-page islands must not select absent EQ tabs.
  var fallback = (el(id) || {}).textContent?.trim() ?
    {mode: 'speaker', follower: true} : {mode: 'eq', follower: false};
  var parsed = readJsonIsland(id, fallback);
  var mode = parsed.mode === 'speaker' || parsed.mode === 'output' ? parsed.mode : 'eq';
  return {
    mode: id === 'sound-follower-data' && (el(id) || {}).textContent?.trim() ? 'speaker' : mode,
    follower: parsed.follower === true,
  };
})();
var pageMode = pageData.mode;
var followerMode = pageData.follower;

export {
  ACTIVE_GAIN_EPSILON_DB,
  el,
  eqEditor,
  followerMode,
  outputPage,
  pageMode,
  readJsonIsland,
  resetEqEditor,
};
