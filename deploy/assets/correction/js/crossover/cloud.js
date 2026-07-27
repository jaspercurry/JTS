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

// The last successfully rendered chart draw, so a window resize can redraw
// without waiting for the next poll (mirrors the room page's
// scheduleChartRedraw pattern, deploy/assets/correction/js/main.js).
let lastChart = null;

function chartPayloadFor(cloud, cloudChart) {
  const verify = (cloud && cloud[PHASE_CLOUD_VERIFY]) || null;
  const chartVerify = (cloudChart && cloudChart[PHASE_CLOUD_VERIFY]) || null;
  const chartMeasure = (cloudChart && cloudChart[PHASE_CLOUD_MEASURE]) || null;
  const verifyCurve = (chartVerify && chartVerify.curve) || null;
  const measureCurve = (chartMeasure && chartMeasure.curve) || null;
  if (!verifyCurve && !measureCurve) return null;

  // Excluded intervals come from VERIFY's own carve-out disclosure (the
  // current, graded truth — the same reason _flatness_details_lines and the
  // geometry guidance are read from PHASE_CLOUD_VERIFY, never
  // PHASE_CLOUD_MEASURE, throughout this flow). Absent until plan PR-6b
  // lands `carve_outs`, so this degrades to no hatching, never an error.
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
    // The corridor is drawn against VERIFY's own reference — the "how flat
    // did the corrected speaker end up" line the flatness gauge already
    // grades against, not MEASURE's (which exists to be out of spec).
    referenceDb: verify ? verify.reference_db : null,
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

  renderCallouts(els.cloudCallouts, verify);

  lastChart = { canvas: els.cloudChart, payload };
  drawCloudChart(els.cloudChart, payload);
}

// Redraws the last-rendered chart against its already-known payload — for a
// window resize/orientation change, where the data hasn't changed but the
// canvas's laid-out size has (see main.js's resize listener).
export function redrawCloudChart() {
  if (lastChart) drawCloudChart(lastChart.canvas, lastChart.payload);
}
