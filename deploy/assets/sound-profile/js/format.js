// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — number, unit and driver-role text formatting.
//
// Leaf helpers: no page records, no DOM.

import { fmtFreq } from '/assets/shared/js/frequency-scale.js';

function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, Number(v) || 0)); }
function clone(o) { return JSON.parse(JSON.stringify(o || {})); }
function fmtDb(v) { v = Number(v) || 0; return (v > 0 ? '+' : '') + v.toFixed(1); }
function fmtFreqShort(v) {
  v = Number(v) || 0;
  return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k' : String(Math.round(v));
}
function fmtQ(v) { return 'Q ' + (Number(v) || 0).toFixed(1); }

function ico(name, cls) {
  return '<svg class="' + (cls || 'ico') + '" aria-hidden="true"><use href="#icon-' + name + '"></use></svg>';
}

function fmtTrim(v) { v = Number(v) || 0; return v > 0 ? '−' + v.toFixed(1) + ' dB' : 'Off'; }

function manualNumberValue(raw) {
  if (raw === '' || raw == null) return null;
  if (typeof raw === 'boolean') return null;
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
function joinListText(items, options) {
  options = options || {};
  if (!items.length) return '';
  if (items.length === 1) return items[0];
  if (items.length === 2) return items[0] + (options.two || ' + ') + items[1];
  return items.slice(0, -1).join(', ') + (options.final || ', ') +
    items[items.length - 1];
}
function roleListText(roles, options) {
  var labels = uniqueRoleLabels(roles);
  if (!labels.length) return 'drivers';
  return joinListText(labels, options);
}
function roleSentenceText(roles) {
  return roleListText(roles, {two: ' and ', final: ', and '});
}

function sleepMs(ms) {
  return new Promise(function(resolve) {
    window.setTimeout(resolve, Math.max(0, Number(ms) || 0));
  });
}

export {
  activeRoleLabel,
  clamp,
  clone,
  fmtDb,
  fmtFreq,
  fmtFreqShort,
  fmtQ,
  fmtTrim,
  ico,
  joinListText,
  manualNumberValue,
  roleListText,
  roleSentenceText,
  sleepMs,
};
