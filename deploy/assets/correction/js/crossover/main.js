// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { getJSON, postJSON } from '/assets/shared/js/http.js';
import { jtsConfirm } from '/assets/shared/js/dialog.js';
import { renderCloud, redrawCloudChart } from './cloud.js';

const els = {
  verdict: document.getElementById('crossover-verdict'),
  applied: document.getElementById('crossover-applied'),
  startOver: document.getElementById('crossover-start-over'),
  steps: document.getElementById('crossover-steps'),
  nudges: document.getElementById('crossover-nudges'),
  review: document.getElementById('crossover-review'),
  reviewBody: document.getElementById('crossover-review-body'),
  cloud: document.getElementById('crossover-cloud'),
  cloudEyebrow: document.getElementById('crossover-cloud-eyebrow'),
  cloudTitle: document.getElementById('crossover-cloud-title'),
  cloudBasis: document.getElementById('crossover-cloud-basis'),
  cloudProvenance: document.getElementById('crossover-cloud-provenance'),
  cloudChart: document.getElementById('crossover-cloud-chart'),
  cloudGeometry: document.getElementById('crossover-cloud-geometry'),
  cloudCallouts: document.getElementById('crossover-cloud-callouts'),
  cloudPending: document.getElementById('crossover-cloud-pending'),
  legendMeasure: document.getElementById('crossover-chart-legend-measure'),
  legendVerify: document.getElementById('crossover-chart-legend-verify'),
  legendPredicted: document.getElementById('crossover-chart-legend-predicted'),
  legendCorridor: document.getElementById('crossover-chart-legend-corridor'),
  legendExcluded: document.getElementById('crossover-chart-legend-excluded'),
  action: document.getElementById('crossover-action'),
  capture: document.getElementById('crossover-capture'),
  walk: document.getElementById('crossover-walk'),
  walkProgress: document.getElementById('crossover-walk-progress'),
  walkHeadline: document.getElementById('crossover-walk-headline'),
  walkDetail: document.getElementById('crossover-walk-detail'),
  walkAction: document.getElementById('crossover-walk-action'),
  captureStatus: document.getElementById('crossover-capture-status'),
  captureStop: document.getElementById('crossover-capture-stop'),
  status: document.getElementById('capture-status'),
};

let envelope = null;
let busy = false;
let stopInFlight = false;
let refreshInFlight = null;
let refreshQueued = false;
let renderEpoch = 0;
let pollTimer = null;
let lastPollDelayMs = null;

const POLL_MS = 1500;
const RETRY_MS = 5000;
// While the tab is hidden (screen off), poll far less often instead of
// stopping outright — a stopped poller can't auto-advance the wizard when the
// measurement finishes. Normal cadence resumes on visibilitychange (and on
// the next render() call after that).
const HIDDEN_POLL_MS = 10000;
const CAPTURE_STOPPABLE = new Set(['starting', 'awaiting_capture']);
// Wind-down: in flight, but the captures are over and the session is draining
// its own work. A walkthrough has nothing to say here — the status line
// narrates it instead.
const CAPTURE_WINDING_DOWN = new Set(['stopping']);
const CAPTURE_IN_FLIGHT = new Set([...CAPTURE_STOPPABLE, ...CAPTURE_WINDING_DOWN]);
function publicCrossoverUrl(value) {
  const raw = String(value || '');
  const pathname = typeof window !== 'undefined' && window.location
    ? String(window.location.pathname || '')
    : '';
  if (pathname.indexOf('/sound/crossover/') === 0 &&
      raw.indexOf('/correction/crossover') === 0) {
    return '/sound/crossover' + raw.slice('/correction/crossover'.length);
  }
  return raw;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'disabled') node.disabled = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children) node.append(child);
  return node;
}

// `action` (optional, issue #1820/#1821) renders the refusal's own resolution
// control beside the message. It exists because a SESSION-OPEN refusal never
// reaches the envelope: the envelope renders from a persisted `failure`, and the
// pre-flight refuses before any state is written, on purpose. Without this the
// household read "review the limits in speaker setup" as flat text and
// had to go find the control themselves — one navigation plus one click, for a
// refusal whose exact remedy the server already named. The server sends it in
// the 400 body (`next_action`), from the same registry entry the hard-stop
// screen would have read. `render()` never touches this element, so the control
// survives the refresh that follows a failed action.
function setStatus(message, tone = '', action = null) {
  els.status.dataset.tone = tone;
  const href = action && action.href ? publicCrossoverUrl(action.href) : '';
  if (!href) {
    els.status.textContent = message || '';
    return;
  }
  els.status.replaceChildren(
    document.createTextNode(message || ''),
    el('a', {
      class: 'btn btn--primary capture-status__action',
      href,
      text: action.label || 'Continue',
    }),
  );
}

function renderSteps(steps) {
  const rows = (Array.isArray(steps) ? steps : []).map((step) => {
    const item = el('li', {class: `wizard-step ${step.status || 'pending'}`});
    item.append(
      el('span', {class: 'wizard-step__dot', 'aria-hidden': 'true'}),
      el('span', {class: 'wizard-step__label', text: step.label || step.id || 'Step'}),
    );
    return item;
  });
  els.steps.replaceChildren(...rows);
}

// Durable "a crossover is applied" signal, separate from the per-run step
// stepper above (crossover_envelope.py's `_applied_chip` / `applied` field):
// a manual/automatic crossover can be applied while the CURRENT measurement
// run is still mid-way, or hasn't started at all. `state === "none"` keeps
// the chip hidden via the native `hidden` attribute (app.css's
// `[hidden] { display: none !important; }`).
function renderApplied(applied) {
  const state = applied && applied.state ? String(applied.state) : 'none';
  els.applied.hidden = state === 'none';
  els.applied.textContent = state === 'none' ? '' : (applied.label || '');
  els.applied.dataset.state = state;
  const isApplied = state === 'manual' || state === 'automatic' || state === 'applied';
  els.applied.className = isApplied ? 'badge badge--ok' : 'badge badge--idle';
}

function renderNudges(nudges, expertDetails, findings) {
  const rows = (Array.isArray(nudges) ? nudges : []).map((nudge) =>
    el('p', {
      class: `wizard-nudge ${nudge.severity === 'warn' ? 'warn' : 'info'}`,
      text: nudge.text || '',
    }),
  );
  // What the measurement LEARNED about this speaker
  // (crossover_envelope_v2._finding_notes — WO-1's read half). Quiet `info`
  // register, one line each, because a finding is something to know rather
  // than a problem to solve — the same styling the aged-resume history note
  // uses. Below the nudges (which are about THIS screen's state) and above
  // the expert numbers (which are folded away). The server composes the
  // sentence, dates it when it is not today's, and sends nothing at all when
  // nothing was banked, so there is no empty-state to render here.
  (Array.isArray(findings) ? findings : []).forEach((finding) => {
    const text = finding && finding.text ? String(finding.text) : '';
    if (text) rows.push(el('p', {class: 'wizard-nudge info', text}));
  });
  // Optional collapsed expert numbers (the verify_fail screen's tracking
  // evidence — crossover_envelope_v2._verify_expert_details, #1605). Reuses the
  // same <details> disclosure style as the candidate provenance so the primary
  // copy stays short with the numbers folded away.
  const details = Array.isArray(expertDetails) ? expertDetails : [];
  if (details.length) {
    rows.push(el('details', {class: 'candidate-provenance'}, [
      el('summary', {text: 'Expert details'}),
      el('p', {class: 'measurement-row__meta', text: `${details.join('; ')}.`}),
    ]));
  }
  els.nudges.replaceChildren(...rows);
}

// Gauge fix (2026-07-24): plain-language text for
// crossover_envelope_v2._candidate_review_payload's "linearization_outcome"
// enum (the values LinearizationState.outcome can hold). Mirrors the existing
// polarity enum-to-text mapping just above in this file; an
// unrecognized/empty value renders nothing (e.g. "" means linearization was
// never evaluated this attempt) — which is why a new Python outcome has to be
// added here too, or the round goes quiet on this screen.
const LINEARIZATION_OUTCOME_TEXT = {
  fitted: 'driver linearization: fitted',
  fitted_single_branch:
    'driver linearization: fitted (one full-range branch, no inter-driver trim)',
  trim_rejected:
    'driver linearization: filters fitted, re-solved trim rejected (used the measured trim)',
  ineligible_mic_tier: 'driver linearization: skipped — needs a reference-tier mic',
  ineligible_repeats: 'driver linearization: skipped — not enough repeat measurements',
  fit_failed: 'driver linearization: skipped — fit engine error',
};

// The one octave-band reason code this module reads (#2638): the fit engine's
// verdict that an octave sits outside the driver's own radiating band, where
// the per-octave residual is the crossover's rolloff rather than anything the
// driver did. The vocabulary is Python's
// (jasper.active_speaker.linearization_envelope.ReasonCode.OUT_OF_BAND); a
// browser module cannot import it, so this literal is a second copy — and
// tests/test_crossover_envelope_v2.py compares the two, exactly as it already
// does for the declared-polarity objective list. Renderer use is at the
// octave rows in renderCandidateReview().
const OCTAVE_REASON_OUT_OF_BAND = 'envelope_out_of_band';

// Audit item 4i: the second reason code this module reads, and the same
// second-copy-pinned-by-test contract as the one above
// (jasper.active_speaker.linearization_envelope.ReasonCode.LIMITED_BY_CLASS_PRIOR).
// An undeclared driver_class resolves to the "unknown" class prior — the most
// conservative row in _CLASS_PRIOR_FULL_TO_HZ — and banked this code with no
// household surface ever naming the /sound/setup/ field that lifts it. Gated
// on driver_class === 'unknown' at the render site below, never on the reason
// code alone: the same code fires for an ALREADY-declared class's own real
// prior, where redeclaring it is not an action the household has left to take.
const OCTAVE_REASON_LIMITED_BY_CLASS_PRIOR = 'envelope_limited_by_class_prior';

// The remedy pointer for an undeclared-class-prior band — the established
// remedy/next-action shape ({id, label, href}) refusal_copy.py's safety-limits
// deep-link rows use (REASON_PROGRAM_PROFILE_MISSING /
// _NOT_CONFIRMED), not a new one. No fragment: unlike
// "#confirm-safety-limits", /sound/setup/ renders no anchor for driver_class,
// so a deep link would land on nothing — the bare page, which opens on its
// own first unfinished step, IS the action, the same "no fragment" contract
// REASON_PROGRAM_PROFILE_MISSING uses for the identical reason. The
// Technical details paragraph is plain text with no links today, so only
// `href` reaches the screen below — `id`/`label` are kept anyway because the
// task is to MIRROR the shape, not a subset of it.
const CLASS_PRIOR_REMEDY = {
  id: 'declare_driver_class',
  label: "Declare this driver's technology class",
  href: '/sound/setup/',
};

// The measured-crossover candidate the household reviews before applying
// (crossover_envelope_v2._candidate_review_payload — trims / delay / polarity,
// derived from the conductor's _candidate_summary). W6.10 blocker #2: the prior
// renderer expected a retained_crossover_regions/drivers shape the conductor
// never builds, so #crossover-review-body rendered empty; this consumes exactly
// the shape the envelope now sends.
function renderCandidateReview(review) {
  const trims = review && Array.isArray(review.trims) ? review.trims : [];
  const hasDelay = Boolean(review && review.delay);
  const hasPolarity = Boolean(review && review.polarity);
  const visible = Boolean(review && (trims.length || hasDelay || hasPolarity));
  els.review.hidden = !visible;
  if (!visible) {
    els.reviewBody.replaceChildren();
    return;
  }

  // WHERE the alignment came from, not just what it is (#2607 S3, extended to
  // the DELAY row by #2617). The measurement can decline to decide either half:
  // when the capture's SNR is below the law an alignment decision is held to,
  // the flow commits the polarity the PRESET declares and a delay this capture
  // did not supply — the one the speaker already plays, or none. The page must
  // not call either "measured", the one word a household reads as "we checked".
  //
  // A SET, not one string: that refusal has four commitments, differing in the
  // DELAY they commit and agreeing exactly here. The fourth (#2662) commits an
  // explicit bench PRESCRIPTION, which the refusal does not touch because it
  // never came from this capture — and its polarity is the declaration too,
  // so it words the same way UNLESS that round also pinned the polarity, which
  // the `polarityPinned` bit below takes ahead of this list. Mirrors
  // `program_analysis.ALIGNMENT_DECLARED_POLARITY_OBJECTIVES`; a browser module
  // cannot import a Python constant, so `tests/test_crossover_envelope_v2.py`
  // fails when the two lists disagree and
  // `tests/js/crossover_polarity_provenance_test.mjs` covers every member here.
  const declaredByDesign = [
    'declared_committed_after_low_snr',
    'applied_alignment_held_after_low_snr',
    'no_delay_committed_after_unreadable_apply',
    'explicit_prescription_held_after_low_snr',
  ].includes(review.alignment_objective);

  // The OTHER way a polarity is not a measured result: the round PINNED it.
  // A separate bit and not a fifth member of the list above, because the
  // objective cannot carry this — a pinned round commits the very same
  // `explicit_prescription_committed` an unpinned prescription does — and
  // because that list is also read for commitments whose ANCHOR is withdrawn,
  // which a pinned round's is not. Nor can it be inferred from
  // `polarity_agrees_with_sum === null`: a seed-committed arm reports null too,
  // and ITS polarity is a measurement (the correlation's).
  //
  // Checked BEFORE `declaredByDesign` because the two overlap on exactly one
  // arm — a pinned prescription on a capture the SNR verdict refused — and
  // there the pin is what actually shipped, so "as designed" would name the
  // wrong author. Python owns the bit (`CrossoverCandidate.polarity_pinned` →
  // `analysis_json` → `_candidate_summary` → `_candidate_review_payload`);
  // `tests/js/crossover_polarity_provenance_test.mjs` drives this renderer with
  // a pinned payload, and `tests/test_crossover_envelope_v2.py` pins the
  // round-trip that carries it here.
  const polarityPinned = review.polarity_pinned === true;

  // The SAME rule one level up: a crossover an operator pinned is not a corner
  // this round measured, and no shipped path ranks one topology against
  // another, so it must never be worded as a result. Python owns the bit
  // (`CrossoverV2Session._topology_prescription` → `_candidate_summary` →
  // `_candidate_review_payload`); `tests/test_crossover_envelope_v2.py` pins
  // the round-trip that carries it here, and
  // `tests/js/crossover_topology_provenance_test.mjs` drives this renderer
  // with a pinned payload.
  const crossoverPinned = review.crossover_pinned === true;
  const crossover = review.crossover;
  const hasCrossover = crossover && typeof crossover.fc_hz === 'number';

  const rows = [];
  if (hasCrossover) {
    // First, because it is the topology every row below sits inside: the
    // trims, the delay and the polarity are all decisions made AT this corner.
    const slope = typeof crossover.slope_db_per_octave === 'number'
      ? `, ${Number(crossover.slope_db_per_octave).toFixed(0)} dB/octave`
      : '';
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {class: 'measurement-row__title', text: 'Crossover'}),
        el('p', {
          class: 'measurement-row__meta',
          text: `${Number(crossover.fc_hz).toFixed(0)} Hz${slope}` +
            (crossoverPinned ? ' (pinned for this round)' : ''),
        }),
      ]),
    ]));
  }
  trims.forEach((trim) => {
    // The SAME rule as the two rows above, one per driver: a level this round
    // was told to hold is not a level it measured. Python owns the bit
    // (`DriverPrescription.pinned_trim_db` → `_candidate_summary` →
    // `_candidate_review_payload`);
    // `tests/js/crossover_trim_provenance_test.mjs` drives this renderer with a
    // pinned payload, and `tests/test_crossover_envelope_v2.py` pins the
    // round-trip that carries it here.
    const trimPinned = trim.pinned === true;
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {class: 'measurement-row__title', text: `${trim.role} level`}),
        el('p', {
          class: 'measurement-row__meta',
          text: `${Number(trim.attenuation_db).toFixed(1)} dB` +
            (trimPinned ? ' (pinned for this round)' : ''),
        }),
      ]),
    ]));
  });
  if (hasDelay) {
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {class: 'measurement-row__title', text: 'Alignment delay'}),
        el('p', {
          class: 'measurement-row__meta',
          text: `${Number(review.delay.delay_ms).toFixed(3)} ms on the ` +
            `${review.delay.role}` +
            (declaredByDesign ? ' — kept as set, not measured this time' : ''),
        }),
      ]),
    ]));
  }
  if (hasPolarity) {
    const polarityBase = review.polarity === 'invert' ? 'Inverted' : 'Kept as set';
    const polarityText = polarityPinned
      ? `${polarityBase} (pinned for this round)`
      : declaredByDesign
        ? 'As designed — this measurement could not check it'
        : review.polarity === 'invert'
          ? 'Inverted (measured)'
          : 'Kept as set';
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {class: 'measurement-row__title', text: 'Polarity'}),
        el('p', {class: 'measurement-row__meta', text: polarityText}),
      ]),
    ]));
  }
  // Alignment confidence, predicted ripple, and the candidate fingerprint are
  // support/provenance detail, not primary copy a household member needs to
  // judge the candidate — collapse them behind a disclosure so the
  // plain-language rows stay first (also reused, unchanged, on the RESULT
  // screen's own expert disclosure).
  const details = [];
  if (typeof review.confidence === 'number') {
    details.push(`alignment confidence ${review.confidence.toFixed(2)}`);
  }
  if (typeof review.ripple_db === 'number') {
    details.push(`predicted ripple ${review.ripple_db.toFixed(1)} dB`);
  }
  if (review.fingerprint) details.push(`candidate ${review.fingerprint}`);
  // "This correction costs N dB of maximum level" (PR-L5), reaching a screen
  // for the first time (two-stage commission D3.2). Read from the `{db, basis}`
  // COMPOUND and nothing else: the same disclosure also exists as sibling
  // `headroom_cost_db` + `headroom_cost_basis` scalars on
  // /state.crossover_v2.candidate, and the never-render-bare property is a
  // property of THIS payload, not of that one.
  //
  // The basis is inseparable from the number. The charge's derivation changed
  // under #1808 and the stamp is deliberately not re-derived on load, so a
  // candidate persisted before that amendment discloses ~22.5 dB where the
  // same correction now costs ~5 — an order of magnitude, on the one screen
  // whose purpose is honesty. An `unknown` basis therefore gets a sentence
  // saying so rather than a figure presented as current.
  //
  // A SET, not an equality, and that is what #2758 taught: the widened
  // evaluation grid minted `realized_peak_full_domain`, and an equality
  // against the one older name would have told the household that every FRESH
  // correction was "measured a way JTS no longer uses". Both realized-peak
  // eras are a measured charge for the emitted chain and read plainly here;
  // only the sum-of-positives era (which reaches this payload as `unknown`)
  // gets the caveat. A NEW era must be added here deliberately — which is the
  // point of listing them.
  const headroom = review.headroom_cost;
  const MEASURED_HEADROOM_BASES = ['realized_peak', 'realized_peak_full_domain'];
  if (headroom && typeof headroom.db === 'number') {
    details.push(
      MEASURED_HEADROOM_BASES.includes(headroom.basis)
        ? `costs ${headroom.db.toFixed(1)} dB of maximum volume`
        : `costs ${headroom.db.toFixed(1)} dB of maximum volume, measured a ` +
          'way JTS no longer uses — re-measure for a current figure',
    );
  }
  // Gauge fix (2026-07-24): the linearization run/skip outcome — the
  // failure mode this kills is linearization silently not running while
  // every other screen looks the same.
  const outcomeText = LINEARIZATION_OUTCOME_TEXT[review.linearization_outcome];
  if (outcomeText) details.push(outcomeText);
  // Gauge fix (2026-07-24): per-role top-octave residuals (achieved minus fit
  // target, dB) — the number that says "the top octave is 9 dB down and
  // nothing corrected it." Uncorrected regions show their natural response
  // here, never a pass/fail.
  //
  // Relabelled by the flat-linearization plan's PR-5: these are per-driver
  // FIT DIAGNOSTICS from the single design-axis capture, not the spec
  // measurement. The spec claim on this same screen comes from the spatial
  // cloud (the "flatness ..." expert lines, built server-side by
  // crossover_envelope_v2._flatness_details_lines). The earlier wording
  // "measured vs fit target" led with "measured", which reads as the
  // measurement — the frame the two constructions must not share.
  //
  // #2638: an octave past the driver's own band is NOT a deficit and its
  // number is not a performance figure. The residual runs to 20 kHz, so
  // where the crossover target dives at 24 dB/oct against a measurement
  // floor that stays put, the subtraction returns a large POSITIVE number —
  // "+23.0 dB" on a healthy 2026-08-16 candidate whose largest filter gain
  // anywhere was +2.5 dB, which read on this very line as a runaway boost.
  // So those octaves are NAMED and not numbered: nothing is hidden, and no
  // stopband arithmetic is presented as passband performance. The verdict is
  // the fit engine's (server-side `reason`), never re-derived here from the
  // frequency — this module knows one code, and Python pins that literal
  // against the ReasonCode enum
  // (tests/test_crossover_envelope_v2.py::
  //  test_the_browser_and_python_agree_on_the_out_of_band_octave_code).
  // Every other reason code describes a band the driver DOES radiate, where
  // the number is a real residual, and renders unchanged — except
  // LIMITED_BY_CLASS_PRIOR on an undeclared class, which gains one remedy
  // sentence alongside its (unchanged) number; see below.
  const octaveRows = Array.isArray(review.linearization_octaves) ?
    review.linearization_octaves : [];
  const octaveLabel = (band) => `${Math.round(Number(band.hz) / 1000)}k`;
  octaveRows.forEach((row) => {
    const bands = Array.isArray(row && row.bands) ? row.bands : [];
    if (!bands.length) return;
    const radiated = bands.filter(
      (band) => band.reason !== OCTAVE_REASON_OUT_OF_BAND);
    const outOfBand = bands.filter(
      (band) => band.reason === OCTAVE_REASON_OUT_OF_BAND);
    if (radiated.length) {
      const parts = radiated.map((band) =>
        `${octaveLabel(band)} ${Number(band.delta_db).toFixed(1)} dB`);
      details.push(
        `${row.role} fit residual vs target (design-axis capture, not the ` +
        `spatial measurement): ${parts.join(', ')}`);
    }
    if (outOfBand.length) {
      details.push(
        `${row.role} ${outOfBand.map(octaveLabel).join(', ')}: outside this ` +
        'driver’s band — not corrected');
    }
    // Audit item 4i: an undeclared driver_class capped correction on this
    // row. Gated on driver_class === 'unknown', never on the reason code
    // alone — the same code fires for an ALREADY-declared class's own real
    // prior, where naming /sound/setup/ again would be a false remedy. One
    // sentence, not per-band: the row already named which octaves and their
    // numbers above.
    const classPriorLimited = bands.some(
      (band) => band.reason === OCTAVE_REASON_LIMITED_BY_CLASS_PRIOR);
    if (classPriorLimited && row.driver_class === 'unknown') {
      details.push(
        `${row.role}: this driver's technology class is not declared, so ` +
        'correction above this range is capped conservatively — declare it ' +
        `at ${CLASS_PRIOR_REMEDY.href} for a less conservative limit`);
    }
  });
  if (details.length) {
    rows.push(el('details', {class: 'candidate-provenance'}, [
      el('summary', {text: 'Technical details'}),
      el('p', {class: 'measurement-row__meta', text: `${details.join('; ')}.`}),
    ]));
  }
  els.reviewBody.replaceChildren(
    el('div', {class: 'measurement-list'}, rows),
  );
}

// Wraps a rendered action `control` (button/link/form) in the shared
// `.measurement-row` title/meta shape when the action carries a
// `description` — a one-line claim, so far only the microphone_check
// screen's tier chooser (flow-simplification PR-U3, crossover_envelope_v2.py's
// `_tier_choice_actions`). Every other action on every other screen (Try
// again, Re-measure, Continue, ...) has no `description` and this
// returns `control` untouched — no other screen's markup changes.
//
// S2 fix (adversarial review of PR #1780): the row's own title already
// carries the tier's full name, so the control need not repeat it — a
// "Quick tune" title above a "Quick tune" button read as a duplicated
// label. Shortening the control to "Start" removes the duplication without
// losing information (the title + one-line description say everything the
// control's own text would have).
//
// The badge sits in its own `.measurement-row__head` flex row with the
// title (gap 0.6rem), mirroring wake_setup.py's `.wake-row__head` convention
// (deploy/assets/wake/wake.css) rather than the earlier zero-gap inline
// child. Reuses the existing `.measurement-row`/`.measurement-row__title`/
// `.measurement-row__meta` classes (the candidate-review rows already use
// them) and the shared `.badge` pill (app.css) for "Recommended"; only
// `.measurement-row__head` (crossover.css) is new, scoped to this page like
// its other single-page visuals.
function wrapChoice(action, control) {
  if (!action.description) return control;
  control.textContent = 'Start';
  const head = el('div', {class: 'measurement-row__head'}, [
    el('p', {class: 'measurement-row__title', text: action.label || 'Continue'}),
    ...(action.recommended
      ? [el('span', {class: 'badge badge--ok', text: 'Recommended'})] : []),
  ]);
  return el('div', {class: 'measurement-row'}, [
    el('div', {}, [
      head,
      el('p', {class: 'measurement-row__meta', text: action.description}),
    ]),
    control,
  ]);
}

function renderActions(primary, alternates = []) {
  els.action.replaceChildren();
  const actions = [primary, ...(Array.isArray(alternates) ? alternates : [])]
    .filter(Boolean);
  // S1 fix (adversarial review of PR #1780): choice cards (tier-chooser
  // actions, carrying `description`) collect separately from plain actions
  // and render inside a dedicated `.tier-choices` grid — one column, equal
  // width, flush-left at every breakpoint — rather than `#crossover-action`'s
  // own `flex; justify-content: flex-end` row, which sized each card to its
  // own content and right-aligned them, so the shorter (often the
  // Recommended) card rendered narrower and visibly indented next to the
  // other. Every other screen's plain actions are unaffected: with no
  // `description` present, `choices` stays empty and this collapses to the
  // original per-action append.
  const choices = [];
  actions.forEach((action, index) => {
    // Choice cards are equal-weight peers — the household picks ONE of two
    // legitimate options, not a primary path plus a subordinate escape
    // hatch — so both render as `btn--primary` and the Recommended badge is
    // the ONLY visual differentiator (S1). Every other action keeps the
    // existing first-action-is-primary convention.
    const className = action.description
      ? 'btn btn--primary'
      : (index === 0 ? 'btn btn--primary' : 'btn btn--ghost');
    let control;
    // `endpoint` WINS over `href` when an action carries both (#2641). An
    // action that can be performed is a button; `href` is then only a
    // presentation hint naming where the household ends up, for a client that
    // cannot POST. This branch used to test `href` FIRST, which is what made
    // "Keep current sound" a link: the server minted a decision, the client
    // rendered a navigation, and the click reloaded the page back onto the
    // same decision screen forever. Reversing the order is also what makes
    // "every minted action is machine-actionable" true for THIS driver, not
    // only for the ones that read the envelope directly.
    if (action.href && !action.endpoint) {
      control = el('a', {
        class: className,
        href: publicCrossoverUrl(action.href),
        text: action.label || 'Continue',
      });
    } else {
      const fields = Array.isArray(action.fields) ? action.fields : [];
      if (!fields.length) {
        const button = el('button', {
          class: className,
          type: 'button',
          disabled: busy || action.enabled === false,
          text: action.label || 'Continue',
        });
        button.addEventListener('click', () => runAction(action, button));
        control = button;
      } else {
        const form = el('form', {class: 'action-form'});
        const inputs = [];
        fields.forEach((field, fieldIndex) => {
          const inputId = `crossover-action-${index}-${fieldIndex}`;
          const inputAttrs = {
            id: inputId,
            type: field.type || 'text',
            name: field.name || '',
            step: field.step || 'any',
          };
          if (field.required) inputAttrs.required = '';
          const input = el('input', inputAttrs);
          inputs.push({field, input});
          form.append(el('div', {class: 'field'}, [
            el('label', {
              for: inputId,
              text: field.label || field.name || 'Value',
            }),
            input,
          ]));
        });
        const button = el('button', {
          class: className,
          type: 'submit',
          disabled: busy || action.enabled === false,
          text: action.label || 'Continue',
        });
        form.append(button);
        form.addEventListener('submit', (event) => {
          event.preventDefault();
          if (!form.reportValidity()) return;
          const body = {...(action.body || {})};
          inputs.forEach(({field, input}) => {
            body[field.name] = field.type === 'number'
              ? Number(input.value) : input.value;
          });
          runAction({...action, body}, button);
        });
        control = form;
      }
    }
    if (action.description) {
      choices.push(wrapChoice(action, control));
    } else {
      els.action.append(control);
    }
  });
  if (choices.length) {
    els.action.append(el('div', {class: 'tier-choices'}, choices));
  }
}

// The prompt the walk is standing on, kept across the poll that follows a
// release. The gate clears `position_pending` the moment the capture is
// admitted, so without this the panel would drop from "Measurement 3 of 9 —
// turn the microphone to +7°" to a bare status line for the whole 25 s the
// tone plays, and the household would lose their place mid-round. Cleared
// whenever the session stops being in flight (renderWalk's own !active arm),
// so it can never describe a session that is over.
let walkPrompt = null;
let lastWalkKey = null;

// A stable serialization of exactly what renderWalk builds — the same
// tap-preserving discipline as actionRowKey. renderWalk runs on every 1.5 s
// poll, and rebuilding the release button under a finger that is already
// down is how hardware round 4 lost taps on the action row.
function walkKey(prompt, pending, yielded) {
  return JSON.stringify({prompt, pending: pending || null, yielded, busy});
}

// The walkthrough: which spot, in the capture plan's own words, and the
// control that says the microphone is there (#2881). Reads
// `capture.position_pending` — the hold the position gate publishes, which until
// now only `jasper-angle-capture serve` ever rendered.
//
// Transport-agnostic by construction: NOTHING here tests which source the
// session opened on. What it tests is `hand_released` — the server's own
// answer to "is a person expected to release this hold?" — so the panel
// follows the hold rather than the transport. That is also what keeps the
// arm's rig off this screen (ADR-0188 §4): an externally positioned walk is
// gated too, but its driver POSTs the release, and a browser button beside it
// could free a position the arm has not reached yet.
//
// `yielded` steps the panel aside when the SCREEN has minted its own control
// for this session (the closing screen's Save / Record-again, the review
// screen's Apply) — one primary at a time, the rule the action row's capture
// gate already holds.
function renderWalk(capture, {active, yielded}) {
  const walking = Boolean(active && !CAPTURE_WINDING_DOWN.has(capture.status));
  const held = walking ? capture.position_pending : null;
  const pending = held && held.hand_released ? held : null;
  if (pending && pending.prompt) walkPrompt = pending.prompt;
  if (!walking) walkPrompt = null;
  const show = Boolean(walking && walkPrompt && !yielded);
  const key = walkKey(show ? walkPrompt : null, show ? pending : null, yielded);
  if (key === lastWalkKey) return;
  lastWalkKey = key;
  els.walk.hidden = !show;
  if (!show) {
    els.walkAction.replaceChildren();
    return;
  }
  els.walkProgress.textContent = walkPrompt.progress || '';
  els.walkHeadline.textContent = walkPrompt.title || '';
  els.walkDetail.textContent = walkPrompt.body || '';
  els.walkDetail.hidden = !walkPrompt.body;
  // Two states, and the difference is whose move it is. Holding: the server
  // named the release action, so render it. Not holding: the tone is playing
  // on the spot named above, and there is nothing to press — say that rather
  // than leave a dead button the household can double-fire into a 409.
  if (pending && pending.action && pending.action.endpoint) {
    const button = el('button', {
      class: 'btn btn--primary',
      type: 'button',
      disabled: busy,
      text: pending.action.label || 'The microphone is in place',
    });
    button.addEventListener('click', () => runAction(pending.action, button));
    els.walkAction.replaceChildren(button);
  } else {
    els.walkAction.replaceChildren(
      el('p', {
        class: 'measurement-row__meta',
        text: 'Recording this spot — keep still until the tone stops.',
      }),
    );
  }
}

// `suppressConnectAffordance` keeps the capture ACTIVE (so polling continues
// and Stop stays wired) but yields the walkthrough to the screen that owns the
// live control — used on the review screen, where a second live prompt beside
// the Apply button would be a misleading second primary (W6.10 blocker #2).
function renderCapture(capture, {suppressConnectAffordance = false} = {}) {
  const active = capture && CAPTURE_IN_FLIGHT.has(capture.status);
  const stoppable = capture && CAPTURE_STOPPABLE.has(capture.status);
  els.capture.hidden = !active;
  els.captureStop.hidden = !stoppable;
  els.captureStop.disabled = stopInFlight;
  // Ahead of the status branches below, all of which return early: the walk is
  // a property of the SESSION, not of the branch that happens to be describing
  // it, and it has to be torn down on the terminal ones too.
  renderWalk(capture, {active, yielded: suppressConnectAffordance});
  if (!active) {
    if (capture && capture.status === 'failed') {
      setStatus(capture.error || 'Capture failed. Retry this step.', 'bad');
    } else if (capture && capture.status === 'stopped') {
      setStatus(capture.error || 'Measurement stopped safely.', 'ok');
    } else if (capture && capture.status === 'complete') {
      setStatus('Capture complete.', 'ok');
    }
    return;
  }
  if (capture.status === 'stopping') {
    els.captureStatus.textContent = 'Stopping playback and restoring the speaker safely…';
    return;
  }
  // Which of the two sentences is keyed off the same `hand_released` the
  // panel is: a hold nobody at this browser releases must not be narrated as
  // if it waited on the reader.
  const awaitingReader = Boolean(
    !suppressConnectAffordance &&
    capture.position_pending && capture.position_pending.hand_released,
  );
  els.captureStatus.textContent = awaitingReader
    ? 'The tone plays as soon as you confirm the microphone is in place.'
    : 'Measuring on the microphone plugged into the speaker.';
}

function captureIsActive(capture) {
  return Boolean(capture && CAPTURE_IN_FLIGHT.has(capture.status));
}

// The last action row this function actually rendered, as a stable
// serialization of everything the row's appearance depends on (see
// actionRowKey below). null before the first render.
let lastActionRowKey = null;

// A stable, order-preserving serialization of exactly what the action-row
// builder below would build from these inputs — the fields the DOM
// actually depends on, nothing else (no envelope fields like verdict_text/
// steps that render() already updates through their own, non-destructive
// setters).
function actionRowKey(primary, alternates) {
  return JSON.stringify({primary: primary || null, alternates, busy});
}

// Sole authority for what the action row shows given an envelope. Every
// call-site (render, stopCapture's finally, runAction's finally) routes
// through this so the capture-in-flight gate can't be forgotten or duplicated
// at one of them — the 2026-07-16 two-primary-buttons bug was exactly that:
// runAction's finally re-rendered envelope.next_action ungated, so a second
// primary button could appear beside the "Open phone capture" capture session.
function renderActionRow(env) {
  if (!env) return;
  const captureActive = captureIsActive(env.capture);
  // The capture gate suppresses a next_action beside a live phone link so a
  // second capture can't be started (the 2026-07-16 two-primary-buttons bug).
  // The review screen's Apply is the exception: it is the PRIMARY action while
  // the just-ended stage-1 capture is still winding down, so the envelope marks
  // it show_during_capture and it renders through (W6.10 blocker #2).
  const showPrimary = !captureActive
    || (env.next_action && env.next_action.show_during_capture);
  const alternates = Array.isArray(env.alternate_actions) ? env.alternate_actions : [];
  // Only alternates the envelope explicitly marks show_during_capture survive
  // the gate — e.g. the verify_fail screen's "Go back to the previous
  // tuning" / Re-measure "get me out of this" affordances must stay visible
  // even while a capture session is live.
  // Every other alternate stays hidden while a capture is in flight.
  const shownAlternates = captureActive
    ? alternates.filter((action) => action && action.show_during_capture)
    : alternates;
  const primary = showPrimary ? env.next_action : null;
  // W6.12: the row builder below unconditionally tears down and rebuilds
  // the row (els.action.replaceChildren()), which every ~1.5s poll
  // (render()'s own call, below) ran through even when NOTHING about the
  // row had changed — hardware round 4 lost 4 taps this way, the classic
  // "the poll fired between pointerdown and click and replaced the button
  // out from under the tap" failure mode. Skip the rebuild when the row
  // would come out byte-identical to what is already on screen; busy is
  // included in the key because it changes each button's baked-in
  // `disabled` without otherwise touching primary/alternates (see
  // stopCapture/runAction/startOver's finally blocks, which rely on THIS
  // function re-rendering once busy flips back to false).
  const key = actionRowKey(primary, shownAlternates);
  if (key === lastActionRowKey) return;
  lastActionRowKey = key;
  renderActions(primary, shownAlternates);
}

// Whether the SCREEN has minted a primary the household is meant to press
// while the session is still in flight — the review screen's Apply, and the
// closing screen's Save / Record-again on a wired round. One primary at a
// time: when this is true the capture block stops advertising a connect link
// and the walkthrough stands down, so the two never compete.
function screenOwnsLiveControl(env) {
  return Boolean(env && env.next_action && env.next_action.show_during_capture);
}

function render(env) {
  envelope = env;
  els.verdict.textContent = env.verdict_text || '';
  renderApplied(env.applied);
  renderSteps(env.steps);
  renderNudges(env.nudges, env.expert_details, env.findings);
  renderCandidateReview(env.candidate_review);
  renderCloud(els, env);
  renderCapture(env.capture, {
    suppressConnectAffordance: screenOwnsLiveControl(env),
  });
  renderActionRow(env);
  schedulePoll(captureIsActive(env.capture) ? POLL_MS : null);
}

async function stopCapture() {
  if (stopInFlight) return;
  busy = true;
  stopInFlight = true;
  renderEpoch += 1;
  els.captureStop.disabled = true;
  setStatus('Stopping safely…');
  try {
    const response = await postJSON(
      publicCrossoverUrl('/correction/crossover/capture-cancel'),
      {},
    );
    renderCapture(response.capture);
    schedulePoll(POLL_MS);
    await refresh();
  } catch (error) {
    setStatus(error && error.message ? error.message : String(error), 'bad');
  } finally {
    busy = false;
    stopInFlight = false;
    renderActionRow(envelope);
  }
}

function startOverConfirmMessage() {
  // Grouping-aware: a bonded speaker's group crossover is rebuilt from the
  // measurement evidence this clears, so it fails back to a plain solo
  // crossover on the next group re-form until re-measured (the driver setup
  // is kept either way). Solo speakers keep exactly what is playing now.
  if (envelope && envelope.grouping_member) {
    return 'This speaker is grouped. Starting the crossover calibration over ' +
      'clears your measurement progress, so this speaker will fall back to a ' +
      'plain solo crossover the next time the group re-forms, until you ' +
      'measure it again. Your driver setup is kept.';
  }
  return 'Start the crossover calibration over? This clears your measurement ' +
    'progress. Your driver setup and the crossover that’s playing now stay ' +
    'exactly as they are — you’ll just measure the crossover again.';
}

async function startOver() {
  if (busy) return;
  const ok = await jtsConfirm(startOverConfirmMessage(), {danger: true});
  if (!ok) return;
  busy = true;
  renderEpoch += 1;
  els.startOver.disabled = true;
  setStatus('Starting over…');
  try {
    const response = await postJSON(
      publicCrossoverUrl('/correction/crossover/reset'),
      {},
    );
    render(response);
    const reset = response && response.reset;
    if (reset && reset.status && reset.status !== 'cleared') {
      // Partial unlink (an errors entry): do not paint it green.
      setStatus(
        'Some measurement files could not be cleared. Check the speaker ' +
          'and try Start over again.',
        'bad',
      );
    } else {
      setStatus('Measurement progress cleared. Ready to start again.', 'ok');
    }
  } catch (error) {
    setStatus(error && error.message ? error.message : String(error), 'bad');
  } finally {
    busy = false;
    els.startOver.disabled = false;
    // render(response) above (success path) builds the action row's buttons
    // WHILE busy was still true, baking `disabled: busy` into every one of
    // them — including buttons unrelated to Start-over, like "Start
    // measurement". Nothing re-rendered after busy flipped back to false, so
    // those buttons stayed disabled until a manual reload. Match the sibling
    // pattern (stopCapture/runAction's finally) exactly: always re-render the
    // action row against the now-correct busy=false.
    renderActionRow(envelope);
  }
}

async function runAction(action, button) {
  if (busy || !action.endpoint) return;
  busy = true;
  // An older envelope fetch may already be in flight. Invalidate its render;
  // the serialized refresh queued after this mutation is the new authority.
  renderEpoch += 1;
  button.disabled = true;
  setStatus('Working…');
  let captureStarted = false;
  try {
    const response = await postJSON(
      publicCrossoverUrl(action.endpoint),
      action.body || {},
    );
    captureStarted = Boolean(response && response.capture);
    if (captureStarted) {
      renderCapture(response.capture);
      // The response's capture hasn't landed in `envelope` yet (that happens
      // inside refresh() below) — hide the action row immediately against
      // the capture we just started rather than waiting a round trip.
      renderActionRow({capture: response.capture, next_action: null, alternate_actions: []});
      schedulePoll(POLL_MS);
    }
    setStatus(response && response.capture ? 'Measurement started.' : 'Updated.', 'ok');
    await refresh();
  } catch (error) {
    const failureMessage = error && error.message ? error.message : String(error);
    const issues = error && error.body && Array.isArray(error.body.issues)
      ? error.body.issues : [];
    const candidateChanged = error && error.status === 409 && issues.some(
      (issue) => issue && issue.code === 'baseline_candidate_fingerprint_mismatch'
    );
    // The refusal's own resolution control, when the server named one (a
    // session-open pre-flight refusal — the one class of failure that can never
    // reach the envelope's decision screen; see setStatus).
    const refusalAction = error && error.body && error.body.next_action
      ? error.body.next_action : null;
    if (candidateChanged) {
      setStatus('The crossover candidate changed. Refreshing the review…', 'bad');
    } else {
      setStatus(failureMessage, 'bad', refusalAction);
    }
    // A failed mutation may still have advanced durable authority: candidate
    // apply can restore exactly or retain the graph pending finalization. Keep
    // the failure visible, but always replace stale actions with the server's
    // one current state.
    try {
      await refresh();
      if (candidateChanged) {
        setStatus('Active speaker review refreshed. Review the current candidate.', '');
      } else {
        setStatus(failureMessage, 'bad', refusalAction);
      }
    } catch (refreshError) {
      const refreshMessage = refreshError && refreshError.message
        ? refreshError.message : String(refreshError);
      setStatus(
        `${failureMessage} Latest state could not be refreshed: ${refreshMessage}`,
        'bad',
        refusalAction,
      );
    }
  } finally {
    busy = false;
    // If capture registration succeeded but refresh failed, keep the old action
    // hidden. Showing it beside a live phone link would permit a second run.
    // renderActionRow re-applies the capture gate against the latest known
    // envelope. The prior version of this block rendered envelope.next_action
    // directly, without that gate — the 2026-07-16 two-primary-buttons bug.
    if (!captureStarted) {
      renderActionRow(envelope);
      // Same reason, same latest-known envelope: the walk's release button was
      // built while busy was still true (render() ran inside the refresh
      // above), so it carries a baked-in `disabled` that nothing else would
      // clear until the next poll — a full second and a half in which the
      // household's next spot looks refused.
      if (envelope) {
        renderWalk(envelope.capture, {
          active: captureIsActive(envelope.capture),
          yielded: screenOwnsLiveControl(envelope),
        });
      }
    }
  }
}

function schedulePoll(delayMs) {
  lastPollDelayMs = delayMs;
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (delayMs === null) return;
  const hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden';
  const effectiveDelay = hidden ? Math.max(delayMs, HIDDEN_POLL_MS) : delayMs;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    refresh().catch((error) => {
      setStatus(error.message, 'bad');
      schedulePoll(RETRY_MS);
    });
  }, effectiveDelay);
}

async function runRefreshQueue() {
  do {
    refreshQueued = false;
    const epoch = renderEpoch;
    const env = await getJSON(
      publicCrossoverUrl('/correction/crossover/envelope'),
    );
    if (epoch === renderEpoch) render(env);
  } while (refreshQueued);
}

function refresh() {
  if (refreshInFlight) {
    refreshQueued = true;
    return refreshInFlight;
  }
  refreshInFlight = runRefreshQueue().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

if (typeof document !== 'undefined') {
  els.captureStop.addEventListener('click', stopCapture);
  els.startOver.addEventListener('click', startOver);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      // Re-apply whichever cadence is already in effect — schedulePoll()
      // stretches it to HIDDEN_POLL_MS itself; a null intent (no active
      // reason to poll) stays null.
      schedulePoll(lastPollDelayMs);
      return;
    }
    refresh().catch((error) => {
      setStatus(error.message, 'bad');
      schedulePoll(RETRY_MS);
    });
  });
}

// Redraw the before/after chart on resize/orientation change — without this
// the canvas's drawing surface stays at whatever size it had on the last
// poll. Debounced at 150 ms (review S-4) — mirrors
// deploy/assets/correction/js/main.js's scheduleChartRedraw() exactly, so a
// drag-resize does not force a style recalc + canvas buffer realloc +
// ~1024-point redraw on every intermediate frame. Guarded separately from
// the `document` check above: the small per-feature test harnesses for this
// page (tests/js/crossover_*_test.mjs) stub `globalThis.document` but not
// `globalThis.window`.
if (typeof window !== 'undefined') {
  let cloudResizeTimer = null;
  function scheduleCloudChartRedraw() {
    if (cloudResizeTimer) clearTimeout(cloudResizeTimer);
    cloudResizeTimer = setTimeout(redrawCloudChart, 150);
  }
  window.addEventListener('resize', scheduleCloudChartRedraw);
  window.addEventListener('orientationchange', scheduleCloudChartRedraw);
}

refresh().catch((error) => {
  setStatus(error.message, 'bad');
  schedulePoll(RETRY_MS);
});
