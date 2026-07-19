// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { getJSON, postJSON } from '/assets/shared/js/http.js';
import { renderRelayQr } from '/assets/shared/js/qr.js';
import { jtsConfirm } from '/assets/shared/js/dialog.js';

const els = {
  verdict: document.getElementById('crossover-verdict'),
  applied: document.getElementById('crossover-applied'),
  startOver: document.getElementById('crossover-start-over'),
  steps: document.getElementById('crossover-steps'),
  nudges: document.getElementById('crossover-nudges'),
  review: document.getElementById('crossover-review'),
  reviewBody: document.getElementById('crossover-review-body'),
  action: document.getElementById('crossover-action'),
  relay: document.getElementById('crossover-relay'),
  relayStatus: document.getElementById('crossover-relay-status'),
  relayLink: document.getElementById('crossover-relay-link'),
  relayQr: document.getElementById('crossover-relay-qr'),
  relayStop: document.getElementById('crossover-relay-stop'),
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
// While the tab is hidden (phone in hand, screen off), poll far less often
// instead of stopping outright — a stopped poller can't auto-advance the
// wizard when the phone finishes its side. Normal cadence resumes on
// visibilitychange (and on the next render() call after that).
const HIDDEN_POLL_MS = 10000;
const RELAY_STOPPABLE = new Set(['starting', 'awaiting_phone']);
const RELAY_IN_FLIGHT = new Set([
  ...RELAY_STOPPABLE,
  'finishing',
  'committing',
  'stopping',
]);

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

function setStatus(message, tone = '') {
  els.status.textContent = message || '';
  els.status.dataset.tone = tone;
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
}

function renderNudges(nudges) {
  const rows = (Array.isArray(nudges) ? nudges : []).map((nudge) =>
    el('p', {
      class: `wizard-nudge ${nudge.severity === 'warn' ? 'warn' : 'info'}`,
      text: nudge.text || '',
    }),
  );
  els.nudges.replaceChildren(...rows);
}

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

  const rows = [];
  trims.forEach((trim) => {
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {class: 'measurement-row__title', text: `${trim.role} level`}),
        el('p', {
          class: 'measurement-row__meta',
          text: `${Number(trim.attenuation_db).toFixed(1)} dB`,
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
            `${review.delay.role}`,
        }),
      ]),
    ]));
  }
  if (hasPolarity) {
    const polarityText = review.polarity === 'invert'
      ? 'Inverted (measured)'
      : 'Kept as set';
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {class: 'measurement-row__title', text: 'Polarity'}),
        el('p', {class: 'measurement-row__meta', text: polarityText}),
      ]),
    ]));
  }
  // Alignment confidence + the candidate fingerprint are support/provenance
  // detail, not primary copy a household member needs to judge the candidate —
  // collapse them behind a disclosure so the plain-language rows stay first.
  const details = [];
  if (typeof review.confidence === 'number') {
    details.push(`alignment confidence ${review.confidence.toFixed(2)}`);
  }
  if (review.fingerprint) details.push(`candidate ${review.fingerprint}`);
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

function renderActions(primary, alternates = []) {
  els.action.replaceChildren();
  const actions = [primary, ...(Array.isArray(alternates) ? alternates : [])]
    .filter(Boolean);
  actions.forEach((action, index) => {
    const className = index === 0 ? 'btn btn--primary' : 'btn btn--ghost';
    if (action.href) {
      els.action.append(el('a', {
        class: className,
        href: action.href,
        text: action.label || 'Continue',
      }));
      return;
    }
    const fields = Array.isArray(action.fields) ? action.fields : [];
    if (!fields.length) {
      const button = el('button', {
        class: className,
        type: 'button',
        disabled: busy || action.enabled === false,
        text: action.label || 'Continue',
      });
      button.addEventListener('click', () => runAction(action, button));
      els.action.append(button);
      return;
    }
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
    els.action.append(form);
  });
}

// `suppressConnectAffordance` keeps the relay ACTIVE (so polling continues and
// Stop stays wired) but hides the "Open phone capture" link + QR — used on the
// review screen, where the phone is already connected and parked in the
// "waiting for apply" hold, so a "scan to connect" prompt beside the Apply
// button would be a misleading second primary (W6.10 blocker #2).
function renderRelay(relay, {suppressConnectAffordance = false} = {}) {
  const active = relay && RELAY_IN_FLIGHT.has(relay.status);
  const stoppable = relay && RELAY_STOPPABLE.has(relay.status);
  els.relay.hidden = !active;
  els.relayLink.hidden = true;
  // Cleared by default alongside the link; repopulated below only in the
  // one branch that has a tap_link to encode.
  renderRelayQr(els.relayQr, null);
  els.relayStop.hidden = !stoppable;
  els.relayStop.disabled = stopInFlight;
  if (!active) {
    if (relay && relay.status === 'failed') {
      setStatus(relay.error || 'Phone capture failed. Retry this step.', 'bad');
    } else if (relay && relay.status === 'stopped') {
      setStatus(relay.error || 'Measurement stopped safely.', 'ok');
    } else if (relay && relay.status === 'complete') {
      setStatus('Phone capture complete.', 'ok');
    }
    return;
  }
  if (relay.status === 'stopping') {
    els.relayStatus.textContent = 'Stopping playback and restoring the speaker safely…';
    return;
  }
  if (relay.status === 'committing') {
    els.relayStatus.textContent = 'Saving the verified measurement…';
    return;
  }
  if (relay.status === 'finishing') {
    els.relayStatus.textContent = 'The phone is finishing and uploading the measurement…';
    return;
  }
  if (suppressConnectAffordance) {
    // Phone connected and holding for apply — keep the section (Stop stays
    // wired, polling continues) but do not re-advertise a connect link/QR.
    els.relayStatus.textContent = 'Your phone is connected — review and apply below.';
    return;
  }
  if (relay.tap_link) {
    els.relayLink.href = relay.tap_link;
    els.relayLink.hidden = false;
    els.relayStatus.textContent = 'Open the trusted capture page and follow its one next step.';
    renderRelayQr(els.relayQr, relay.tap_link);
  } else {
    els.relayStatus.textContent = 'Creating the phone capture link…';
  }
}

function relayIsActive(relay) {
  return Boolean(relay && RELAY_IN_FLIGHT.has(relay.status));
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
// call-site (render, stopRelay's finally, runAction's finally) routes
// through this so the relay-in-flight gate can't be forgotten or duplicated
// at one of them — the 2026-07-16 two-primary-buttons bug was exactly that:
// runAction's finally re-rendered envelope.next_action ungated, so a second
// primary button could appear beside the "Open phone capture" relay link.
function renderActionRow(env) {
  if (!env) return;
  const relayActive = relayIsActive(env.relay);
  // The relay gate suppresses a next_action beside a live phone link so a
  // second capture can't be started (the 2026-07-16 two-primary-buttons bug).
  // The review screen's Apply is the exception: it is the PRIMARY action while
  // the phone is parked in the "waiting for apply" hold, so the envelope marks
  // it show_during_relay and it renders through (W6.10 blocker #2).
  const showPrimary = !relayActive
    || (env.next_action && env.next_action.show_during_relay);
  const alternates = Array.isArray(env.alternate_actions) ? env.alternate_actions : [];
  // W6.12: the same show_during_relay escape hatch, per-alternate. Before
  // this the gate blanket-cleared EVERY alternate action while the relay was
  // in flight, so the verify_fail screen's Undo / Re-measure — the "get me
  // out of this" affordances — vanished behind a live relay link the
  // operator had no obvious reason to expect (they had to guess "hit Stop"
  // to make them reappear). Only alternates the envelope explicitly marks
  // show_during_relay survive the gate; every other alternate stays hidden
  // while a relay is in flight, unchanged from before.
  const shownAlternates = relayActive
    ? alternates.filter((action) => action && action.show_during_relay)
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
  // stopRelay/runAction/startOver's finally blocks, which rely on THIS
  // function re-rendering once busy flips back to false).
  const key = actionRowKey(primary, shownAlternates);
  if (key === lastActionRowKey) return;
  lastActionRowKey = key;
  renderActions(primary, shownAlternates);
}

function render(env) {
  envelope = env;
  els.verdict.textContent = env.verdict_text || '';
  renderApplied(env.applied);
  renderSteps(env.steps);
  renderNudges(env.nudges);
  renderCandidateReview(env.candidate_review);
  // On the review screen the show_during_relay primary (Apply) owns the phone —
  // keep the relay live for polling/Stop but hide its connect link/QR.
  const suppressConnectAffordance = Boolean(
    env.next_action && env.next_action.show_during_relay,
  );
  renderRelay(env.relay, {suppressConnectAffordance});
  renderActionRow(env);
  schedulePoll(relayIsActive(env.relay) ? POLL_MS : null);
}

async function stopRelay() {
  if (stopInFlight) return;
  busy = true;
  stopInFlight = true;
  renderEpoch += 1;
  els.relayStop.disabled = true;
  setStatus('Stopping safely…');
  try {
    const response = await postJSON('/correction/crossover/relay-cancel', {});
    renderRelay(response.relay);
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
    const response = await postJSON('/correction/crossover/reset', {});
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
    // pattern (stopRelay/runAction's finally) exactly: always re-render the
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
  let relayStarted = false;
  try {
    const response = await postJSON(action.endpoint, action.body || {});
    relayStarted = Boolean(response && response.relay);
    if (relayStarted) {
      renderRelay(response.relay);
      // The response's relay hasn't landed in `envelope` yet (that happens
      // inside refresh() below) — hide the action row immediately against
      // the relay we just started rather than waiting a round trip.
      renderActionRow({relay: response.relay, next_action: null, alternate_actions: []});
      schedulePoll(POLL_MS);
    }
    setStatus(response && response.relay ? 'Phone capture is ready.' : 'Updated.', 'ok');
    await refresh();
  } catch (error) {
    const failureMessage = error && error.message ? error.message : String(error);
    const issues = error && error.body && Array.isArray(error.body.issues)
      ? error.body.issues : [];
    const candidateChanged = error && error.status === 409 && issues.some(
      (issue) => issue && issue.code === 'baseline_candidate_fingerprint_mismatch'
    );
    if (candidateChanged) {
      setStatus('The crossover candidate changed. Refreshing the review…', 'bad');
    } else {
      setStatus(failureMessage, 'bad');
    }
    // A failed mutation may still have advanced durable authority: candidate
    // apply can restore exactly or retain the graph pending finalization. Keep
    // the failure visible, but always replace stale actions with the server's
    // one current state.
    try {
      await refresh();
      if (candidateChanged) {
        setStatus('Crossover review refreshed. Review the current candidate.', '');
      } else {
        setStatus(failureMessage, 'bad');
      }
    } catch (refreshError) {
      const refreshMessage = refreshError && refreshError.message
        ? refreshError.message : String(refreshError);
      setStatus(`${failureMessage} Latest state could not be refreshed: ${refreshMessage}`, 'bad');
    }
  } finally {
    busy = false;
    // If relay registration succeeded but refresh failed, keep the old action
    // hidden. Showing it beside a live phone link would permit a second run.
    // renderActionRow re-applies the relay gate against the latest known
    // envelope. The prior version of this block rendered envelope.next_action
    // directly, without that gate — the 2026-07-16 two-primary-buttons bug.
    if (!relayStarted) {
      renderActionRow(envelope);
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
    const env = await getJSON('/correction/crossover/envelope');
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
  els.relayStop.addEventListener('click', stopRelay);
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

refresh().catch((error) => {
  setStatus(error.message, 'bad');
  schedulePoll(RETRY_MS);
});
