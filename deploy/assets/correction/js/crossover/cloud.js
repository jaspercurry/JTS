// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Before/after visualization + anomaly callouts (flat-linearization plan
// PR-7). Renders the compact `env.cloud` verdict (geometry guidance,
// provenance, spec bands, carve-out disclosure — all server-owned copy, see
// jasper/web/correction_crossover_v2.py's `_compact_cloud_status` and
// jasper/active_speaker/crossover_v2_flow.py's `carve_outs_by_band`) plus
// `env.cloud_chart`'s decimated curves. This module never invents copy or a
// spec-facing number of its own — it reads server strings verbatim into
// text nodes (via `textContent`, never `innerHTML`) and turns already-
// disclosed numbers (frequencies, reference_db, tolerance_db) into screen
// coordinates. Callout markup is built with plain `document.createElement` +
// `textContent` (mirrors main.js's own local `el()` helper's shape) rather
// than the shared `h()` builder — every string here is untrusted-safe by the
// same `textContent`-only argument either way, and staying off `h()` keeps
// this module's only import a same-directory relative one (`./chart.js`).
import { drawCloudChart } from './chart.js';

const PHASE_CLOUD_MEASURE = 'cloud_measure';
const PHASE_CLOUD_VERIFY = 'cloud_verify';

// The plain-language, hardware-blind caption shown while only the pre-
// correction curve exists. A client literal (not server-owned copy): it is
// a transient UI-state message, not a spec claim, the same category as
// main.js's own 'Working…' / 'Stopping safely…' status strings — no
// measured number, no promise about timing.
const VERIFY_PENDING_TEXT =
  'The after-correction curve appears once the second measurement pass finishes.';

// The last chart draw ATTEMPTED (not necessarily successfully rendered —
// review N-2), so a window resize can redraw without waiting for the next
// poll (mirrors the room page's scheduleChartRedraw pattern,
// deploy/assets/correction/js/main.js). `drawCloudChart` can return `false`
// on the very first render after the section is unhidden, if the browser
// has not yet laid out the canvas (0-size `getBoundingClientRect()`); unlike
// the room page's drawChart() caller, nothing here inspects that return
// value to retry synchronously — the next poll (≤1.5 s later) calls
// renderCloud() again with the same or newer payload and draws normally.
// This is a real, working self-heal, not the same mechanism as the room
// page's retry, and is exactly the first-unhide path the HW product smoke
// (docs/flat-linearization-productization-plan.md's PR-7 section) should
// eyeball on real hardware.
let lastChart = null;

function chartPayloadFor(cloud, cloudChart) {
  const measure = (cloud && cloud[PHASE_CLOUD_MEASURE]) || null;
  const verify = (cloud && cloud[PHASE_CLOUD_VERIFY]) || null;
  const chartMeasure = (cloudChart && cloudChart[PHASE_CLOUD_MEASURE]) || null;
  const chartVerify = (cloudChart && cloudChart[PHASE_CLOUD_VERIFY]) || null;
  const measureCurve = (chartMeasure && chartMeasure.curve) || null;
  const verifyCurve = (chartVerify && chartVerify.curve) || null;
  if (!measureCurve && !verifyCurve) return null;

  // Excluded intervals come from VERIFY's own carve-out disclosure (the
  // current, graded truth — the same reason _flatness_details_lines and the
  // geometry guidance are read from PHASE_CLOUD_VERIFY, never
  // PHASE_CLOUD_MEASURE, throughout this flow).
  const excludedIntervals = [];
  const carveOuts = Array.isArray(verify && verify.carve_outs) ? verify.carve_outs : [];
  carveOuts.forEach((band) => {
    const intervals = Array.isArray(band && band.intervals) ? band.intervals : [];
    intervals.forEach((interval) => {
      excludedIntervals.push({
        f_lo_hz: interval && interval.f_lo_hz,
        f_hi_hz: interval && interval.f_hi_hz,
      });
    });
  });

  return {
    measureCurve,
    verifyCurve,
    // Review B-1: each curve is plotted relative to its OWN reference —
    // linearization is cut-only, so VERIFY's reference is always at or
    // below MEASURE's, and a single shared reference displaced the whole
    // "Before" curve by a level change the spec never grades. Both
    // reference_db values already ride the compact block (every phase entry
    // carries its own — jasper.web.correction_crossover_v2's
    // _compact_cloud_status), so no new server data is needed.
    measureReferenceDb: measure ? measure.reference_db : null,
    verifyReferenceDb: verify ? verify.reference_db : null,
    // Spec bands (and therefore the corridor) are VERIFY's own — the
    // current, graded truth; MEASURE exists to be out of spec, so it never
    // gets a corridor.
    specBands: (verify && verify.spec_bands) || [],
    excludedIntervals,
  };
}

// One callout card: `disclosure` (plain language, always shown) plus an
// optional collapsed `expert` line (the τ/r register — mirrors the review
// screen's own `.candidate-provenance` <details>, imported nowhere: it is
// the SAME class name, styled once in crossover.css). Every string lands via
// `textContent`, never `innerHTML` — untrusted-safe by construction, no
// escaping helper needed.
function buildCallout(disclosure, expert) {
  const wrap = document.createElement('div');
  wrap.className = 'crossover-callout';
  const headline = document.createElement('p');
  headline.textContent = disclosure;
  wrap.appendChild(headline);
  if (expert) {
    const details = document.createElement('details');
    details.className = 'candidate-provenance';
    const summary = document.createElement('summary');
    summary.textContent = 'Technical details';
    details.appendChild(summary);
    const expertLine = document.createElement('p');
    expertLine.className = 'measurement-row__meta';
    expertLine.textContent = expert;
    details.appendChild(expertLine);
    wrap.appendChild(details);
  }
  return wrap;
}

function renderCallouts(container, verify) {
  const carveOuts = Array.isArray(verify && verify.carve_outs) ? verify.carve_outs : [];
  const rows = [];
  carveOuts.forEach((band) => {
    const disclosure = band && band.disclosure;
    if (!disclosure) return; // nothing carved in this band — nothing to say
    rows.push(buildCallout(disclosure, band && band.expert));
  });
  container.replaceChildren(...rows);
}

// Legend + "still measuring" caption, shown progressively (review S-5): a
// session that has only walked the pre-correction cloud has no verify curve,
// no spec bands, and no carve-outs yet, so a legend advertising all four
// series (and a chart with three of them simply missing) would read as
// broken rather than in-progress. Each swatch is shown only once its own
// series is actually on the canvas.
function updateLegend(els, payload) {
  const hasMeasure = Boolean(payload.measureCurve);
  const hasVerify = Boolean(payload.verifyCurve);
  const hasCorridor = payload.specBands.length > 0;
  const hasExcluded = payload.excludedIntervals.length > 0;
  els.legendMeasure.hidden = !hasMeasure;
  els.legendVerify.hidden = !hasVerify;
  els.legendCorridor.hidden = !hasCorridor;
  els.legendExcluded.hidden = !hasExcluded;
  els.cloudPending.hidden = hasVerify;
  if (!hasVerify) els.cloudPending.textContent = VERIFY_PENDING_TEXT;
}

// Renders the section into `els` (the caller's DOM refs — see main.js) from
// one envelope. Hides the whole section when neither cloud phase has a
// curve yet (nothing measured); every sub-piece degrades independently
// otherwise (a stale/absent geometry_guidance or provenance_note just
// renders nothing, never a placeholder).
export function renderCloud(els, env) {
  const cloud = env && env.cloud;
  const cloudChart = env && env.cloud_chart;
  const verify = (cloud && cloud[PHASE_CLOUD_VERIFY]) || null;
  const payload = chartPayloadFor(cloud, cloudChart);

  const visible = Boolean(payload);
  els.cloud.hidden = !visible;
  if (!visible) {
    lastChart = null;
    return;
  }

  const provenance = (verify && verify.provenance_note) || '';
  els.cloudProvenance.textContent = provenance;
  els.cloudProvenance.hidden = !provenance;

  const guidance = (verify && verify.geometry_guidance) || '';
  els.cloudGeometry.textContent = guidance;
  els.cloudGeometry.hidden = !guidance;

  updateLegend(els, payload);
  renderCallouts(els.cloudCallouts, verify);

  lastChart = { canvas: els.cloudChart, payload };
  drawCloudChart(els.cloudChart, payload);
}

// Redraws the last-attempted chart against its already-known payload — for a
// window resize/orientation change, where the data hasn't changed but the
// canvas's laid-out size has (see main.js's debounced resize listener).
export function redrawCloudChart() {
  if (lastChart) drawCloudChart(lastChart.canvas, lastChart.payload);
}
