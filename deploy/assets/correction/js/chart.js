// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// chart.js — the result canvas. A dumb renderer: it draws the curves the
// Pi already smoothed and the segments the Pi already classified, and
// derives no improvement verdict of its own.

var canvas = document.getElementById('chart');
var chartShowFilter = document.getElementById('chart-show-filter');

function filterEffectCurve(measured, predicted) {
  if (
    !measured || !predicted ||
    !measured.freqs_hz || !measured.magnitude_db ||
    !predicted.magnitude_db ||
    measured.magnitude_db.length !== predicted.magnitude_db.length
  ) {
    return null;
  }
  return {
    freqs_hz: measured.freqs_hz,
    magnitude_db: measured.magnitude_db.map(function (value, idx) {
      return Number(predicted.magnitude_db[idx] || 0) - Number(value || 0);
    })
  };
}

export function drawChart(curves, fillSegments) {
  curves = curves || {};
  var measured = curves.measured || null;
  var target = curves.target || null;
  var predicted = curves.predicted || null;
  var verify = curves.verify || null;
  var dpr = window.devicePixelRatio || 1;
  var rect = canvas.getBoundingClientRect();
  // Defensive: a hidden canvas (display:none ancestor) reports
  // 0×0. Drawing into it silently produces an empty chart. Bail
  // and log — caller is responsible for re-invoking after the
  // canvas becomes visible.
  if (rect.width < 10 || rect.height < 10) {
    console.warn('drawChart skipped — canvas not laid out yet ' +
      '(' + rect.width + '×' + rect.height + ')');
    return;
  }
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  var c = canvas.getContext('2d');
  c.scale(dpr, dpr);
  c.clearRect(0, 0, rect.width, rect.height);

  // Margins
  var ml = 40, mr = 10, mt = 10, mb = 22;
  var W = rect.width - ml - mr;
  var H = rect.height - mt - mb;

  var fMin = 20, fMax = 20000;
  var dbMin = -20, dbMax = 20;

  function fx(f) { return ml + W * (Math.log2(f / fMin) / Math.log2(fMax / fMin)); }
  function fy(db) { return mt + H * (1 - (db - dbMin) / (dbMax - dbMin)); }

  // Grid
  c.strokeStyle = '#e6e6e6'; c.fillStyle = '#888';
  c.font = '11px sans-serif'; c.lineWidth = 1;
  [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach(function (f) {
    var x = fx(f);
    c.beginPath(); c.moveTo(x, mt); c.lineTo(x, mt + H); c.stroke();
    var label = f >= 1000 ? (f / 1000) + 'k' : '' + f;
    c.fillText(label, x - 8, mt + H + 14);
  });
  [-20, -10, 0, 10, 20].forEach(function (db) {
    var y = fy(db);
    c.beginPath(); c.moveTo(ml, y); c.lineTo(ml + W, y); c.stroke();
    c.fillText(db + ' dB', 2, y + 3);
  });
  // 0 dB emphasis
  c.strokeStyle = '#bbb';
  c.beginPath(); c.moveTo(ml, fy(0)); c.lineTo(ml + W, fy(0)); c.stroke();

  function drawCurve(curve, color, dashed, width) {
    if (!curve || !curve.freqs_hz) return;
    c.strokeStyle = color;
    c.lineWidth = width || 2;
    if (dashed) c.setLineDash([4, 4]); else c.setLineDash([]);
    c.beginPath();
    var first = true;
    for (var i = 0; i < curve.freqs_hz.length; i++) {
      var x = fx(curve.freqs_hz[i]);
      var y = fy(curve.magnitude_db[i]);
      if (first) { c.moveTo(x, y); first = false; }
      else c.lineTo(x, y);
    }
    c.stroke();
    c.setLineDash([]);
  }

  // Honest before/after fill: shade the area between the
  // pre-correction measured curve and the post-correction verify
  // curve, green where the correction moved toward the target
  // (improved), amber where it moved away (regressed). The segment
  // classification + grid indices come from the Pi envelope; this only
  // renders them against the exact server-smoothed curves.
  function drawBeforeAfterFill(segments, beforeCurve, afterCurve) {
    if (
      !segments || !segments.length ||
      !beforeCurve || !beforeCurve.freqs_hz || !beforeCurve.magnitude_db ||
      !afterCurve || !afterCurve.magnitude_db ||
      beforeCurve.freqs_hz.length !== beforeCurve.magnitude_db.length ||
      beforeCurve.freqs_hz.length !== afterCurve.magnitude_db.length
    ) return;
    var freqs = beforeCurve.freqs_hz;
    var beforeDb = beforeCurve.magnitude_db;
    var afterDb = afterCurve.magnitude_db;
    var n = freqs.length;
    segments.forEach(function (seg) {
      var lo = Number(seg.i_lo);
      var hi = Number(seg.i_hi);
      if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi < lo) return;
      lo = Math.max(0, lo);
      hi = Math.min(n - 1, hi);
      c.fillStyle = seg.tone === 'improved'
        ? 'rgba(29, 185, 84, 0.22)'   // green — moved toward target
        : 'rgba(214, 130, 0, 0.22)';  // amber — moved away
      c.beginPath();
      var first = true;
      for (var i = lo; i <= hi; i++) {
        var x = fx(freqs[i]);
        var y = fy(afterDb[i]);
        if (first) { c.moveTo(x, y); first = false; }
        else c.lineTo(x, y);
      }
      for (var j = hi; j >= lo; j--) {
        c.lineTo(fx(freqs[j]), fy(beforeDb[j]));
      }
      c.closePath();
      c.fill();
    });
  }

  // Measured before/after fill (green=improved, amber=regressed),
  // under the curves so both edges stay visible. The improved/
  // regressed verdict + grid indices are Pi-computed; we only fill
  // between the server-smoothed before/after curves within each
  // server-classified segment. Render only when a verify exists.
  if (verify && fillSegments && fillSegments.length) {
    drawBeforeAfterFill(fillSegments, measured, verify);
  }

  drawCurve(target, '#888', true, 2);
  drawCurve(measured, '#d44', false, 2);
  drawCurve(predicted, '#1db954', false, 2);
  if (chartShowFilter && chartShowFilter.checked) {
    drawCurve(
      filterEffectCurve(measured, predicted),
      '#2b7bb9',
      true,
      1.6,
    );
  }
  // Phase 2: post-correction verify pass overlay (purple dashed).
  drawCurve(verify, '#a050d0', true, 2);
  return true;
}
