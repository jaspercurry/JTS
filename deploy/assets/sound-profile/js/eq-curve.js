// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — filter-spec response math and EQ graph geometry.
//
// Turns a profile's filter specs into curve points, and maps Hz/dB onto the
// SVG viewBox coordinates the editor draws in.

import {
  GAINLESS_TYPES,
  magnitudeDb
} from "/assets/sound-profile/js/eq-math.js";
import { clamp } from "/assets/sound-profile/js/format.js";
import { ACTIVE_GAIN_EPSILON_DB } from "/assets/sound-profile/js/state.js";

function bandType(s) { return s.type || s.biquad_type || 'Peaking'; }
function isGainless(s) { return GAINLESS_TYPES.indexOf(bandType(s)) >= 0; }

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

function freqToSlider(freq, min, max) {
  var lmin = Math.log(min), lmax = Math.log(max);
  return Math.round((Math.log(clamp(freq, min, max)) - lmin) / (lmax - lmin) * 1000);
}
function sliderToFreq(pos, min, max) {
  var lmin = Math.log(min), lmax = Math.log(max);
  return Math.exp(lmin + clamp(pos, 0, 1000) / 1000 * (lmax - lmin));
}

export {
  H,
  MAXDB,
  MINDB,
  W,
  advancedSpecs,
  drawArea,
  drawPath,
  freqToSlider,
  gx,
  gy,
  padB,
  padL,
  padR,
  padT,
  pointsFor,
  sliderToFreq,
  specActive,
  summedDbAt,
};
