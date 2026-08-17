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
// **Deviation frame, not absolute dB (review B-1, 2026-07-27).** A fit moves
// VERIFY's own reference level away from MEASURE's — below it on a cut-only
// fit, and since the boost ruling (#2106) a permitted lift can move it the
// other way, which is why this frame is stated per curve rather than assuming
// a direction (premise trued up by #2603's sweep; the rule below never
// depended on the sign). Plotting both curves in absolute dB
// against ONE shared reference (VERIFY's) displaced the entire "Before" curve
// by a level change the spec never grades, under a corridor labeled "Spec
// tolerance" that was not testing what it claimed to. Each curve is instead
// plotted as `magnitude_db - that phase's OWN reference_db` — the exact
// subtraction `jasper.active_speaker.flat_spec.evaluate_flat_spec` already
// documents (`deviation_db = spec_smoothed_db - reference_db`), re-run here
// for display only, never a new derivation. The corridor becomes
// `0 ± tolerance_db` per band in this frame — the same window for both
// curves now that each is relative to its own reference — with a 0 dB
// emphasis line marking "on this phase's own reference" (mirrors
// deploy/assets/correction/js/main.js's drawChart() 0 dB line).
//
// **Domain restricted twice over (review B-2, measured on the real S0
// corpus — see gradedFrequencyRange()/deviationDomain() for the numbers).**
// The decimated curve extends well outside the chart's displayed [F_MIN_HZ,
// F_MAX_HZ] window (0.71-23,953 Hz measured) — folding those off-screen bins
// into the y-domain inflated it to a 72.7 dB span pre-fix and collapsed the
// tolerance corridor to a few-pixel sliver (6.9% of plot height). Restricting
// to the displayed range alone still was not enough: the worst DISPLAYED
// deviation sits at 19,969 Hz, inside `flat_spec.BEST_EFFORT_ABOVE_HZ`'s
// documented "best-effort, disclosed, never specced" region — a driver's
// natural top-octave rolloff, not a defect, and not something the spec ever
// grades. The y-domain is therefore bounded to the spec's own GRADED
// frequency range (derived from `specBands`, e.g. 250-16000 Hz — never a
// hardcoded constant), which measured 21.0% corridor height on the same
// corpus (23.9 dB span) against 9.9% for the full display range. Every
// point in the wider DISPLAY range is still drawn at full resolution —
// including the ungraded rolloff — it simply does not get a vote in how
// tall the plot is, and is naturally clipped by the canvas clip below if it
// exceeds the graded-range-derived bound.
//
// Precedent: deploy/assets/correction/js/main.js's drawChart() (the room
// page's frequency-response canvas) — same log-frequency axis, margin
// layout, and devicePixelRatio scaling. Colors differ on purpose: that chart
// hardcodes hex; this one reads CSS custom properties via getComputedStyle
// so the chart tracks the shared design tokens (crossover.css's
// `.crossover-page` block) instead of a second, driftable color list.

const F_MIN_HZ = 20;
const F_MAX_HZ = 20000;
const DEVIATION_PAD_DB = 3;
// The predicted curve's dash pattern, in CSS px (pre-DPR — the context is
// already scaled). Long enough to read as a deliberate dash at 240 px tall
// rather than as a dotted or broken line.
const PREDICTED_DASH = [6, 4];
const GRID_FREQS_HZ = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];

function chartColor(canvas, name, fallback) {
  const value = getComputedStyle(canvas).getPropertyValue(name).trim();
  return value || fallback;
}

// Every {f, dev} point of `curve` that falls inside the drawn frequency
// range [F_MIN_HZ, F_MAX_HZ] and has a finite deviation from `referenceDb`.
// Filtering here (rather than only clipping at draw time) is what fixes the
// domain computation in review B-2 — an out-of-range bin cannot inflate the
// y-axis if it never becomes a point in the first place.
function inRangeDeviations(curve, referenceDb) {
  if (!curve || !Array.isArray(curve.freqs_hz) || !Array.isArray(curve.magnitude_db)) return [];
  if (typeof referenceDb !== 'number' || !Number.isFinite(referenceDb)) return [];
  const freqs = curve.freqs_hz;
  const mags = curve.magnitude_db;
  const n = Math.min(freqs.length, mags.length);
  const points = [];
  for (let i = 0; i < n; i += 1) {
    const f = freqs[i];
    const db = mags[i];
    if (!Number.isFinite(f) || !Number.isFinite(db)) continue;
    if (f < F_MIN_HZ || f > F_MAX_HZ) continue;
    points.push({ f, dev: db - referenceDb });
  }
  return points;
}

function maxToleranceDb(specBands) {
  let max = 0;
  (specBands || []).forEach((band) => {
    const tolerance = Number(band && band.tolerance_db);
    if (Number.isFinite(tolerance)) max = Math.max(max, Math.abs(tolerance));
  });
  return max;
}

// The union of the spec's own graded bands (e.g. 250-16000 Hz), derived from
// the already-disclosed `specBands` rather than a hardcoded constant — a
// future spec-table change (the plan doc's own "S0-contingent, not final"
// caveat) needs no chart edit. `[null, null]` when there are no bands yet
// (no verify has closed): the caller falls back to the full displayed range.
function gradedFrequencyRange(specBands) {
  let lo = null;
  let hi = null;
  (specBands || []).forEach((band) => {
    const bandLo = Number(band && band.f_lo_hz);
    const bandHi = Number(band && band.f_hi_hz);
    if (Number.isFinite(bandLo)) lo = lo === null ? bandLo : Math.min(lo, bandLo);
    if (Number.isFinite(bandHi)) hi = hi === null ? bandHi : Math.max(hi, bandHi);
  });
  return [lo, hi];
}

// A small symmetric window around 0 dB: floored to contain the largest spec
// tolerance (so the corridor is never a sub-pixel sliver) and widened to
// contain whatever the GRADED-range data actually shows (an out-of-spec
// deviation, or a deep identified null).
//
// **Bounded to the spec-graded frequency range, not the full displayed one
// (review B-2, refined).** The decimated curve's worst deviation on the real
// S0 corpus sits at 19,969 Hz — inside `flat_spec.BEST_EFFORT_ABOVE_HZ`'s
// documented "best-effort, disclosed, never specced" region (a driver's
// natural top-octave rolloff, not a defect). Letting an ungraded extreme like
// that set the y-scale is the same failure the fix corrects, one band
// further out: measured on that corpus, scaling to the FULL display range
// gives a 50.6 dB span (corridor 9.9% of plot height); scaling to the
// spec-graded range alone gives 23.9 dB (corridor 21.0%) — the ungraded
// rolloff is still fully DRAWN (see `inRangeDeviations`'s wider range below),
// it just does not get a vote in how tall the plot is. Falls back to the
// full display-range bound (`points` unfiltered) when there are no graded
// bands yet — nothing to protect the legibility of.
function deviationDomain(pointSets, toleranceBound, gradedRange) {
  const [gradedLo, gradedHi] = gradedRange;
  const bounded = gradedLo === null || gradedHi === null
    ? pointSets
    : pointSets.map((points) => points.filter((p) => p.f >= gradedLo && p.f <= gradedHi));
  let bound = toleranceBound;
  bounded.forEach((points) => {
    points.forEach((p) => {
      bound = Math.max(bound, Math.abs(p.dev));
    });
  });
  bound += DEVIATION_PAD_DB;
  return [-bound, bound];
}

// Renders the before/after overlay into `canvas` from a plain-data payload:
//   measureCurve / verifyCurve / predictedCurve: {freqs_hz, magnitude_db} | null
//   measureReferenceDb / verifyReferenceDb / predictedReferenceDb: number |
//     null — each curve's OWN flat-spec reference (evaluate_flat_spec's
//     reference_db); every curve is plotted relative to its own, never a
//     shared one (review B-1). The predicted curve's comes from the STORED
//     spec report that graded it, so an ungradeable prediction has no
//     reference, contributes no points, and is simply not drawn — the honest
//     outcome, since plotting it against another phase's reference would
//     invent the one frame the screen exists to state truthfully.
//   specBands: [{f_lo_hz, f_hi_hz, tolerance_db}] — the tolerance corridor,
//     `0 ± tolerance_db` per band in this deviation frame
//   excludedIntervals: [{f_lo_hz, f_hi_hz}] — dimmed vertical strips
//     (identified interference nulls / the position-disagreement screen)
// Returns false (and draws nothing) when the canvas has no laid-out size yet
// or neither curve has data — mirrors the room page's drawChart() contract
// so callers can retry after layout the same way.
export function drawCloudChart(canvas, payload) {
  const measureCurve = payload && payload.measureCurve;
  const verifyCurve = payload && payload.verifyCurve;
  const predictedCurve = payload && payload.predictedCurve;
  const measureReferenceDb = payload ? payload.measureReferenceDb : null;
  const verifyReferenceDb = payload ? payload.verifyReferenceDb : null;
  const predictedReferenceDb = payload ? payload.predictedReferenceDb : null;
  const specBands = (payload && payload.specBands) || [];
  const excludedIntervals = (payload && payload.excludedIntervals) || [];

  if (!measureCurve && !verifyCurve && !predictedCurve) return false;

  const measurePoints = inRangeDeviations(measureCurve, measureReferenceDb);
  const verifyPoints = inRangeDeviations(verifyCurve, verifyReferenceDb);
  const predictedPoints = inRangeDeviations(predictedCurve, predictedReferenceDb);

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

  const [dbMin, dbMax] = deviationDomain(
    // The predicted curve gets a vote in the y-domain like any other series:
    // it is drawn in this frame, so a prediction that overshoots the corridor
    // must be VISIBLE overshooting it rather than clipped flat against the
    // plot edge — that overshoot is the number the review screen's verdict
    // names in words, and the chart is where a household sees it.
    [measurePoints, verifyPoints, predictedPoints],
    maxToleranceDb(specBands),
    gradedFrequencyRange(specBands),
  );

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
  const predictedColor = chartColor(canvas, '--crossover-chart-predicted', '#d08b25');
  const corridorColor = chartColor(canvas, '--crossover-chart-corridor', '#4a90a4');
  const excludedColor = chartColor(canvas, '--crossover-chart-excluded', '#888');

  // Grid + axis labels, in relative dB (deviation from each curve's own
  // reference — see the module header).
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
  const dbStep = Math.max(1, Math.round((dbMax - dbMin) / 4));
  for (let db = Math.ceil(dbMin / dbStep) * dbStep; db <= dbMax; db += dbStep) {
    const y = fy(db);
    c.beginPath();
    c.moveTo(ml, y);
    c.lineTo(ml + W, y);
    c.stroke();
    c.fillText(`${db} dB`, 2, y + 3);
  }
  // 0 dB emphasis — "on this phase's own reference" (mirrors main.js's own
  // 0 dB line, deploy/assets/correction/js/main.js's drawChart()).
  c.strokeStyle = textColor;
  c.beginPath();
  c.moveTo(ml, fy(0));
  c.lineTo(ml + W, fy(0));
  c.stroke();

  // Everything below is bounded to the plot rectangle (review B-2, second
  // guard): inRangeDeviations() and the domain above should already keep
  // every drawn point inside it, but a clip costs nothing and protects
  // against any residual edge case (e.g. a rounding-driven off-by-one at
  // the domain boundary) painting into the axis gutter or margin.
  c.save();
  c.beginPath();
  c.rect(ml, mt, W, H);
  c.clip();

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

  // Spec tolerance corridor — 0 ± tolerance_db per band. The same window
  // now grades both curves, because each is already relative to its own
  // reference (review B-1) rather than one shared level.
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
    const yTop = fy(tolerance);
    const yBottom = fy(-tolerance);
    c.fillRect(x0, yTop, x1 - x0, yBottom - yTop);
  });
  c.restore();

  // `dash` (two-stage commission D3): the predicted curve is stroked dashed
  // because it is a MODEL, not a measurement — the one visual difference that
  // keeps "what the microphone heard" and "what JTS expects" from reading as
  // two equally-evidenced lines. Solid for every measured series, unchanged.
  function drawCurve(points, color, dash = null) {
    if (!points.length) return;
    c.strokeStyle = color;
    c.lineWidth = 2;
    c.setLineDash(dash || []);
    c.beginPath();
    let first = true;
    points.forEach(({ f, dev }) => {
      const x = fx(f);
      const y = fy(dev);
      if (first) {
        c.moveTo(x, y);
        first = false;
      } else {
        c.lineTo(x, y);
      }
    });
    c.stroke();
  }

  drawCurve(measurePoints, measureColor);
  drawCurve(verifyPoints, verifyColor);
  drawCurve(predictedPoints, predictedColor, PREDICTED_DASH);
  c.setLineDash([]); // never leak the dash into a later draw on this context
  c.restore(); // undo the clip
  return true;
}
