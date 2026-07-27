// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Before/after cloud chart (flat-linearization plan PR-7). Pure canvas
// renderer — every number it draws (curves, corridor, excluded intervals)
// comes from the server; this module computes screen geometry only and
// never derives a spec verdict of its own (see docs/flat-linearization-
// productization-plan.md's PR-7 section: "reuse … never derive a spec-facing
// number privately").
//
// Precedent: deploy/assets/correction/js/main.js's drawChart() (the room
// page's frequency-response canvas) — same log-frequency axis, margin
// layout, and devicePixelRatio scaling. Colors differ on purpose: that chart
// hardcodes hex; this one reads CSS custom properties via getComputedStyle
// so the chart tracks the shared design tokens (crossover.css's
// `.crossover-page` block) instead of a second, driftable color list.

const F_MIN_HZ = 20;
const F_MAX_HZ = 20000;
const DB_PAD = 3;
const GRID_FREQS_HZ = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];

function chartColor(canvas, name, fallback) {
  const value = getComputedStyle(canvas).getPropertyValue(name).trim();
  return value || fallback;
}

function dbDomain(values) {
  const finite = values.filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (!finite.length) return null;
  const lo = Math.min(...finite) - DB_PAD;
  const hi = Math.max(...finite) + DB_PAD;
  return lo < hi ? [lo, hi] : [lo - 1, hi + 1];
}

function curveValues(curve) {
  if (!curve || !Array.isArray(curve.magnitude_db)) return [];
  return curve.magnitude_db;
}

function corridorValues(referenceDb, specBands) {
  if (typeof referenceDb !== 'number' || !Number.isFinite(referenceDb)) return [];
  const out = [];
  (specBands || []).forEach((band) => {
    const tolerance = Number(band && band.tolerance_db);
    if (Number.isFinite(tolerance)) {
      out.push(referenceDb + tolerance, referenceDb - tolerance);
    }
  });
  return out;
}

// Renders the before/after overlay into `canvas` from a plain-data payload:
//   measureCurve / verifyCurve: {freqs_hz: number[], magnitude_db: number[]} | null
//   referenceDb: number | null — the flat-spec reference level (VERIFY's own)
//   specBands: [{f_lo_hz, f_hi_hz, tolerance_db}] — draws the tolerance
//     corridor as one shaded step per band, centered on referenceDb
//   excludedIntervals: [{f_lo_hz, f_hi_hz}] — dimmed vertical strips
//     (identified interference nulls / the position-disagreement screen)
// Returns false (and draws nothing) when the canvas has no laid-out size yet
// or neither curve has data — mirrors the room page's drawChart() contract
// so callers can retry after layout the same way.
export function drawCloudChart(canvas, payload) {
  const measureCurve = payload && payload.measureCurve;
  const verifyCurve = payload && payload.verifyCurve;
  const referenceDb = payload ? payload.referenceDb : null;
  const specBands = (payload && payload.specBands) || [];
  const excludedIntervals = (payload && payload.excludedIntervals) || [];

  if (!measureCurve && !verifyCurve) return false;

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return false;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const c = canvas.getContext('2d');
  c.setTransform(1, 0, 0, 1, 0, 0);
  c.scale(dpr, dpr);
  c.clearRect(0, 0, rect.width, rect.height);

  const ml = 44;
  const mr = 10;
  const mt = 10;
  const mb = 22;
  const W = rect.width - ml - mr;
  const H = rect.height - mt - mb;

  const domain = dbDomain([
    ...curveValues(measureCurve),
    ...curveValues(verifyCurve),
    ...corridorValues(referenceDb, specBands),
  ]);
  if (!domain) return false;
  const [dbMin, dbMax] = domain;

  function fx(f) {
    return ml + W * (Math.log2(f / F_MIN_HZ) / Math.log2(F_MAX_HZ / F_MIN_HZ));
  }
  function fy(db) {
    return mt + H * (1 - (db - dbMin) / (dbMax - dbMin));
  }
  function clampX(f) {
    return Math.max(ml, Math.min(ml + W, fx(f)));
  }

  const gridColor = chartColor(canvas, '--border-strong', '#ccc');
  const textColor = chartColor(canvas, '--muted', '#888');
  const measureColor = chartColor(canvas, '--crossover-chart-measure', '#c0392b');
  const verifyColor = chartColor(canvas, '--crossover-chart-verify', '#2e8b57');
  const corridorColor = chartColor(canvas, '--crossover-chart-corridor', '#4a90a4');
  const excludedColor = chartColor(canvas, '--crossover-chart-excluded', '#888');

  // Grid + axis labels.
  c.strokeStyle = gridColor;
  c.fillStyle = textColor;
  c.font = '11px sans-serif';
  c.lineWidth = 1;
  GRID_FREQS_HZ.forEach((f) => {
    const x = fx(f);
    c.beginPath();
    c.moveTo(x, mt);
    c.lineTo(x, mt + H);
    c.stroke();
    const label = f >= 1000 ? `${f / 1000}k` : `${f}`;
    c.fillText(label, x - 8, mt + H + 14);
  });
  const dbStep = Math.max(5, Math.round((dbMax - dbMin) / 4 / 5) * 5);
  for (let db = Math.ceil(dbMin / dbStep) * dbStep; db <= dbMax; db += dbStep) {
    const y = fy(db);
    c.beginPath();
    c.moveTo(ml, y);
    c.lineTo(ml + W, y);
    c.stroke();
    c.fillText(`${db} dB`, 2, y + 3);
  }

  // Excluded intervals — dimmed vertical strips, drawn first so the curves
  // and corridor stay legible on top of them.
  c.save();
  c.globalAlpha = 0.16;
  c.fillStyle = excludedColor;
  excludedIntervals.forEach((interval) => {
    const lo = Number(interval && interval.f_lo_hz);
    const hi = Number(interval && interval.f_hi_hz);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return;
    const x0 = clampX(lo);
    const x1 = clampX(hi);
    if (x1 <= x0) return;
    c.fillRect(x0, mt, x1 - x0, H);
  });
  c.restore();

  // Spec tolerance corridor — one shaded step per band, centered on the
  // verify reference level (the current, graded truth; see cloud.js).
  if (typeof referenceDb === 'number' && Number.isFinite(referenceDb)) {
    c.save();
    c.globalAlpha = 0.16;
    c.fillStyle = corridorColor;
    specBands.forEach((band) => {
      const lo = Number(band && band.f_lo_hz);
      const hi = Number(band && band.f_hi_hz);
      const tolerance = Number(band && band.tolerance_db);
      if (!Number.isFinite(lo) || !Number.isFinite(hi) || !Number.isFinite(tolerance)) return;
      const x0 = clampX(lo);
      const x1 = clampX(hi);
      if (x1 <= x0) return;
      const yTop = fy(referenceDb + tolerance);
      const yBottom = fy(referenceDb - tolerance);
      c.fillRect(x0, yTop, x1 - x0, yBottom - yTop);
    });
    c.restore();
  }

  function drawCurve(curve, color) {
    if (!curve || !Array.isArray(curve.freqs_hz) || !Array.isArray(curve.magnitude_db)) return;
    const freqs = curve.freqs_hz;
    const mags = curve.magnitude_db;
    const n = Math.min(freqs.length, mags.length);
    if (!n) return;
    c.strokeStyle = color;
    c.lineWidth = 2;
    c.beginPath();
    let first = true;
    for (let i = 0; i < n; i += 1) {
      const f = freqs[i];
      const db = mags[i];
      if (!Number.isFinite(f) || !Number.isFinite(db) || f <= 0) continue;
      const x = fx(f);
      const y = fy(db);
      if (first) {
        c.moveTo(x, y);
        first = false;
      } else {
        c.lineTo(x, y);
      }
    }
    c.stroke();
  }

  drawCurve(measureCurve, measureColor);
  drawCurve(verifyCurve, verifyColor);
  return true;
}
