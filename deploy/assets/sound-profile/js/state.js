// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

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

function el(id) { return document.getElementById(id); }
function status(msg, isErr) {
  var node = el('status');
  if (node) {
    node.textContent = msg || '';
    node.className = 'status-line' + (isErr ? ' status-line--err' : '');
  }
}
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
  pageMode,
  resetEqEditor,
  status,
};
