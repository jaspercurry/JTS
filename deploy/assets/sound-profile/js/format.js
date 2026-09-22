// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

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

export {
  clamp,
  clone,
  fmtDb,
  fmtFreq,
  fmtFreqShort,
  fmtQ,
  fmtTrim,
  ico,
};
