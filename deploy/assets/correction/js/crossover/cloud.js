// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Before/after visualization + anomaly callouts (flat-linearization plan
// PR-7). Renders the compact `env.cloud` verdict (geometry guidance,
// provenance, spec bands, carve-out disclosure — all server-owned copy, see
// jasper/active_speaker/crossover_envelope_v2.py's `compact_cloud_status`
// and
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
const TIER_EXPRESS = 'express';

// The plain-language, hardware-blind caption shown while only the pre-
// correction curve exists. A client literal (not server-owned copy): it is
// a transient UI-state message, not a spec claim, the same category as
// main.js's own 'Working…' / 'Stopping safely…' status strings — no
// measured number, no promise about timing.
const VERIFY_PENDING_TEXT =
  'The after-correction curve appears once the second measurement pass finishes.';

// Express (M=1, flow-simplification §1.3) has NO post-apply cloud, ever —
// unlike full mid-session, there is no second pass coming. Reusing
// VERIFY_PENDING_TEXT here would promise a curve that will never appear
// (an honesty bug this module must not have): distinguished by `env.tier`,
// which the envelope copies through from the durable state
// (crossover_envelope_v2.py's own "tier" key).
const EXPRESS_NO_AFTER_CURVE_TEXT =
  'This quick tune confirms the result at the mark only — there is no ' +
  'after-correction curve for this measurement. Run a Full measurement to ' +
  'see one.';

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
// (docs/historical/linearization-campaign-2026-07.md's PR-7 section) should
// eyeball on real hardware.
let lastChart = null;

// B1 fix (adversarial review of PR #1780): which compact cloud-phase block
// carries the household-facing honesty-instrument surface (spec bands,
// carve-outs, provenance, geometry guidance) — VERIFY for Full (the
// current, graded truth), MEASURE for Express (the ONLY cloud it ever
// produces; M=1 never closes a CLOUD-VERIFY group, permanently, not "not
// yet"). `compact_cloud_status`
// (jasper.active_speaker.crossover_envelope_v2)
// already projects the identical shape onto every phase entry, so this is
// a read-side selection, not new server data.
// `prediction` (two-stage commission D3.1) forces the pre-apply cloud, and it
// is the REVIEW screen's marker — the envelope sends that key on no other
// screen, so this stays a data test rather than the `env.screen` switch PR-T2
// is explicitly not allowed to add.
//
// The review interlude renders BEFORE anything is applied, so a CLOUD-VERIFY
// entry does not exist yet at Full and never will at Express. Falling through
// to the Full branch there would leave the screen with no spec bands, hence no
// tolerance corridor — and the corridor is what makes the predicted curve
// legible as passing or missing at all, on the one screen whose purpose is
// that verdict. D3.1 names the source outright: "the per-band verdict from
// `env.cloud.cloud_measure.spec_bands`".
//
// This does NOT relax the standing rule that the pre-apply cloud is never
// rendered as "how flat your speaker is" (the reason Full reads CLOUD-VERIFY
// everywhere else): on this screen the pre-apply cloud is the measured
// evidence being reviewed, framed as exactly that, and the corrected claim
// belongs to stage 2's own cloud once it has been walked.
function specSourceFor(cloud, tier, prediction) {
  const measure = (cloud && cloud[PHASE_CLOUD_MEASURE]) || null;
  const verify = (cloud && cloud[PHASE_CLOUD_VERIFY]) || null;
  if (prediction) return measure;
  return tier === TIER_EXPRESS ? measure : verify;
}

function chartPayloadFor(cloud, cloudChart, tier, prediction) {
  const measure = (cloud && cloud[PHASE_CLOUD_MEASURE]) || null;
  const verify = (cloud && cloud[PHASE_CLOUD_VERIFY]) || null;
  const specSource = specSourceFor(cloud, tier, prediction);
  const chartMeasure = (cloudChart && cloudChart[PHASE_CLOUD_MEASURE]) || null;
  const chartVerify = (cloudChart && cloudChart[PHASE_CLOUD_VERIFY]) || null;
  const measureCurve = (chartMeasure && chartMeasure.curve) || null;
  const verifyCurve = (chartVerify && chartVerify.curve) || null;
  // The PREDICTED response (two-stage commission D3's "what we predict"). The
  // envelope sends `prediction` on the review screen only, so this module
  // needs no screen test of its own — the presence of the data IS the
  // instruction, the same rule every other series here already follows.
  const predictedCurve = (prediction && prediction.curve) || null;
  if (!measureCurve && !verifyCurve && !predictedCurve) return null;

  // Excluded intervals come from the spec source's own carve-out disclosure
  // (the current, graded truth for Full; the ONLY cloud Express ever
  // produces, framed as the before-tuning state by the envelope's own
  // expert_details — carve-outs themselves render VERBATIM here, since they
  // are a post-apply-persistent fact ("EQ cannot fill these") regardless of
  // which cloud measured them, owner decision 1).
  const excludedIntervals = [];
  const carveOuts = Array.isArray(specSource && specSource.carve_outs)
    ? specSource.carve_outs : [];
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
    predictedCurve,
    // The predicted curve's OWN reference, from the stored spec report that
    // graded it — same per-curve rule as the two below (review B-1). `null`
    // for an ungradeable prediction, which the chart then draws no points for
    // rather than borrowing another phase's reference frame.
    predictedReferenceDb: prediction ? prediction.reference_db : null,
    // Review B-1 (PR-7): each curve is plotted relative to its OWN
    // reference — a fit moves VERIFY's reference off MEASURE's (below it on
    // a cut-only fit, and either way since the boost ruling #2106), and a
    // single shared reference displaced the whole "Before" curve by a level
    // change the spec never grades. Per-curve holds whichever way it moved;
    // the old wording assumed one direction (#2603's sweep trued it up).
    // Both reference_db values already ride the compact block
    // (every phase entry carries its own), so no new server data is needed.
    measureReferenceDb: measure ? measure.reference_db : null,
    verifyReferenceDb: verify ? verify.reference_db : null,
    // Spec bands (and therefore the corridor) come from the spec source:
    // VERIFY's for Full (the current, graded truth — MEASURE exists to be
    // out of spec there, so it never gets a corridor); MEASURE's for
    // Express, drawn against the BEFORE curve — express has no after curve
    // to grade, and showing the corridor there is what makes its carve-outs
    // legible on the chart at all (B1).
    specBands: (specSource && specSource.spec_bands) || [],
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

function renderCallouts(container, specSource) {
  const carveOuts = Array.isArray(specSource && specSource.carve_outs)
    ? specSource.carve_outs : [];
  const rows = [];
  carveOuts.forEach((band) => {
    const disclosure = band && band.disclosure;
    if (!disclosure) return; // nothing carved in this band — nothing to say
    rows.push(buildCallout(disclosure, band && band.expert));
  });
  container.replaceChildren(...rows);
}

// The section's own framing (issue #2152). One section serves two screens,
// and until now it asserted the POST-VERIFY one on both: "Before and after /
// What the microphone heard".
//
// That heading was true while the pre-apply CLOUD_MEASURE existed. R15 removed
// it by design (#2106), so on the driver-only review screen the ONLY curve is
// the prediction — leaving a heading that claimed measurement directly above a
// legend reading "(not measured)". The household was asked to approve a change
// on the strength of a chart that overstated what it was.
//
// The heading now follows what is actually on the canvas. Nothing here decides
// WHAT to draw; it only stops the frame from claiming more than the contents.
// The vocabulary is the one this flow already uses — "what the microphone
// heard" for measured, "what JTS expects" for modelled (see chart.js's dashed
// stroke, the visual half of the same distinction). Every string below is
// household-facing, so it is hardware-blind by the same rule the rest of this
// page's authored copy follows.
// TWO aria-labels, because the markup has two: one on the <section> (the
// region) and one on the <canvas> (the graphic). Both were static, and fixing
// only the section would have left the chart element itself still announcing
// "before and after correction" on a screen with no before and no after.
const MEASURED_FRAMING = {
  eyebrow: 'Before and after',
  title: 'What the microphone heard',
  ariaLabel: 'Before and after measurement',
  chartAriaLabel: 'Frequency response before and after correction',
  basis: '',
};
const PREDICTED_FRAMING = {
  eyebrow: 'Predicted result',
  title: 'What JTS expects after correction',
  ariaLabel: 'Predicted frequency response after correction',
  // The canvas label carries "not measured" where the section label does not.
  // A sighted household is told the curve is a model by chart.js's dashed
  // stroke; someone reading the canvas through a screen reader gets none of
  // that, so the words have to do what the dash does.
  chartAriaLabel: 'Predicted frequency response after correction, not measured',
  // Says where the line comes from, that it is not a measurement, and when a
  // real one arrives — the last part matters, because it is what makes this a
  // step in a process rather than a hedge.
  //
  // "the measurements JTS just took", not the parts they were taken from:
  // household copy on this page is hardware-blind by rule (the page shell's
  // own `_assert_hardware_blind` guard, and the same reason the verdict text
  // says "worked out from the measurement, not measured"). Naming the parts
  // would describe the speaker by device taxonomy instead of by evidence.
  basis:
    'Worked out from the measurements JTS just took — a prediction, not a '
    + 'measurement. The microphone measures the real result right after you '
    + 'apply.',
};

function updateSectionFraming(els, payload) {
  // Prediction-only is the pre-apply review screen. The moment ANY measured
  // curve is on the canvas the measured framing is the honest one again, so
  // the post-verify chart — whose fidelity is confirmed — is untouched.
  const measured = Boolean(payload.measureCurve || payload.verifyCurve);
  const framing = measured ? MEASURED_FRAMING : PREDICTED_FRAMING;
  if (els.cloudEyebrow) els.cloudEyebrow.textContent = framing.eyebrow;
  if (els.cloudTitle) els.cloudTitle.textContent = framing.title;
  // Screen-reader users read these instead of the heading and the picture, so
  // leaving either on the measured wording would fix the claim for sighted
  // households only.
  if (els.cloud && typeof els.cloud.setAttribute === 'function') {
    els.cloud.setAttribute('aria-label', framing.ariaLabel);
  }
  if (els.cloudChart && typeof els.cloudChart.setAttribute === 'function') {
    els.cloudChart.setAttribute('aria-label', framing.chartAriaLabel);
  }
  if (els.cloudBasis) {
    els.cloudBasis.textContent = framing.basis;
    els.cloudBasis.hidden = !framing.basis;
  }
}

// Legend + "still measuring" caption, shown progressively (review S-5): a
// session that has only walked the pre-correction cloud has no verify curve,
// no spec bands, and no carve-outs yet, so a legend advertising all four
// series (and a chart with three of them simply missing) would read as
// broken rather than in-progress. Each swatch is shown only once its own
// series is actually on the canvas.
function updateLegend(els, payload, tier) {
  const hasMeasure = Boolean(payload.measureCurve);
  const hasVerify = Boolean(payload.verifyCurve);
  // The swatch tracks what is actually ON the canvas, not merely what was
  // sent: an ungradeable prediction has a curve but no reference to plot it
  // against, draws nothing, and must not advertise a series that is not there.
  const hasPredicted = Boolean(
    payload.predictedCurve && typeof payload.predictedReferenceDb === 'number',
  );
  const hasCorridor = payload.specBands.length > 0;
  const hasExcluded = payload.excludedIntervals.length > 0;
  els.legendMeasure.hidden = !hasMeasure;
  els.legendVerify.hidden = !hasVerify;
  if (els.legendPredicted) els.legendPredicted.hidden = !hasPredicted;
  els.legendCorridor.hidden = !hasCorridor;
  els.legendExcluded.hidden = !hasExcluded;
  // The "after-correction curve is still coming" caption is about the
  // POST-APPLY measurement, and on the review screen nothing has been applied
  // for it to describe — the household has not decided yet, so neither
  // "appears once the second pass finishes" nor express's "there is no after
  // curve" is a true sentence there. The predicted curve carries its own
  // meaning through the legend and the screen's verdict copy instead.
  const suppressPendingCaption = Boolean(payload.predictedCurve);
  els.cloudPending.hidden = hasVerify || suppressPendingCaption;
  if (!hasVerify && !suppressPendingCaption) {
    els.cloudPending.textContent = tier === TIER_EXPRESS
      ? EXPRESS_NO_AFTER_CURVE_TEXT
      : VERIFY_PENDING_TEXT;
  }
}

// Renders the section into `els` (the caller's DOM refs — see main.js) from
// one envelope. Hides the whole section when neither cloud phase has a
// curve yet (nothing measured); every sub-piece degrades independently
// otherwise (a stale/absent geometry_guidance or provenance_note just
// renders nothing, never a placeholder).
export function renderCloud(els, env) {
  const cloud = env && env.cloud;
  const cloudChart = env && env.cloud_chart;
  const tier = env && env.tier;
  const prediction = (env && env.prediction) || null;
  const specSource = specSourceFor(cloud, tier, prediction);
  const payload = chartPayloadFor(cloud, cloudChart, tier, prediction);

  const visible = Boolean(payload);
  els.cloud.hidden = !visible;
  if (!visible) {
    lastChart = null;
    return;
  }

  const provenance = (specSource && specSource.provenance_note) || '';
  els.cloudProvenance.textContent = provenance;
  els.cloudProvenance.hidden = !provenance;

  const guidance = (specSource && specSource.geometry_guidance) || '';
  els.cloudGeometry.textContent = guidance;
  els.cloudGeometry.hidden = !guidance;

  updateSectionFraming(els, payload);
  updateLegend(els, payload, tier);
  renderCallouts(els.cloudCallouts, specSource);

  lastChart = { canvas: els.cloudChart, payload };
  drawCloudChart(els.cloudChart, payload);
}

// Redraws the last-attempted chart against its already-known payload — for a
// window resize/orientation change, where the data hasn't changed but the
// canvas's laid-out size has (see main.js's debounced resize listener).
export function redrawCloudChart() {
  if (lastChart) drawCloudChart(lastChart.canvas, lastChart.payload);
}
