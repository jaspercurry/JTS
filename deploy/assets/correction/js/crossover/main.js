// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { getJSON, postJSON } from '/assets/shared/js/http.js';
import { renderRelayQr } from '/assets/shared/js/qr.js';

const els = {
  verdict: document.getElementById('crossover-verdict'),
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

function renderNudges(nudges) {
  const rows = (Array.isArray(nudges) ? nudges : []).map((nudge) =>
    el('p', {
      class: `wizard-nudge ${nudge.severity === 'warn' ? 'warn' : 'info'}`,
      text: nudge.text || '',
    }),
  );
  els.nudges.replaceChildren(...rows);
}

function renderCandidateReview(review) {
  const regions = review && Array.isArray(review.retained_crossover_regions)
    ? review.retained_crossover_regions : [];
  const drivers = review && Array.isArray(review.drivers) ? review.drivers : [];
  const visible = Boolean(review && regions.length && drivers.length);
  els.review.classList.toggle('hidden', !visible);
  if (!visible) {
    els.reviewBody.replaceChildren();
    return;
  }

  const rows = [];
  regions.forEach((region) => {
    const polarity = `${region.lower_role}: ${region.lower_polarity}; ` +
      `${region.upper_role}: ${region.upper_polarity}`;
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {
          class: 'measurement-row__title',
          text: `${region.lower_role} / ${region.upper_role}`,
        }),
        el('p', {
          class: 'measurement-row__meta',
          text: `${Number(region.fc_hz).toLocaleString()} Hz · ` +
            `${region.filter_family} order ${region.order} · ${polarity}`,
        }),
      ]),
    ]));
  });
  drivers.forEach((driver) => {
    rows.push(el('div', {class: 'measurement-row'}, [
      el('div', {}, [
        el('p', {class: 'measurement-row__title', text: driver.role}),
        el('p', {
          class: 'measurement-row__meta',
          text: `${Number(driver.attenuation_db).toFixed(1)} dB attenuation · ` +
            `${Number(driver.delay_ms).toFixed(3)} ms delay · ${driver.polarity}`,
        }),
      ]),
    ]));
  });
  // Raw content hashes and the algorithm id/version are provenance for
  // support/debugging, not primary copy a household member needs to judge
  // the candidate — collapse them behind a disclosure so the plain-language
  // region/driver rows above stay the first thing read.
  const evidence = review.evidence || {};
  const isolated = evidence.isolated_artifact || {};
  const summed = evidence.summed_artifact || {};
  rows.push(el('details', {class: 'candidate-provenance'}, [
    el('summary', {text: 'Technical details'}),
    el('p', {
      class: 'measurement-row__meta',
      text: `Evidence ${isolated.fingerprint || 'unavailable'} (drivers), ` +
        `${summed.fingerprint || 'unavailable'} (combined); ` +
        `${evidence.algorithm_id || 'unknown'} v${evidence.algorithm_version || '?'}.`,
    }),
  ]));
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

function renderRelay(relay) {
  const active = relay && RELAY_IN_FLIGHT.has(relay.status);
  const stoppable = relay && RELAY_STOPPABLE.has(relay.status);
  els.relay.classList.toggle('hidden', !active);
  els.relayLink.classList.add('hidden');
  // Cleared by default alongside the link; repopulated below only in the
  // one branch that has a tap_link to encode.
  renderRelayQr(els.relayQr, null);
  els.relayStop.classList.toggle('hidden', !stoppable);
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
  if (relay.tap_link) {
    els.relayLink.href = relay.tap_link;
    els.relayLink.classList.remove('hidden');
    els.relayStatus.textContent = 'Open the trusted capture page and follow its one next step.';
    renderRelayQr(els.relayQr, relay.tap_link);
  } else {
    els.relayStatus.textContent = 'Creating the phone capture link…';
  }
}

function render(env) {
  envelope = env;
  els.verdict.textContent = env.verdict_text || '';
  renderSteps(env.steps);
  renderNudges(env.nudges);
  renderCandidateReview(env.candidate_review);
  renderRelay(env.relay);
  const relayActive = env.relay && RELAY_IN_FLIGHT.has(env.relay.status);
  renderActions(
    relayActive ? null : env.next_action,
    relayActive ? [] : env.alternate_actions,
  );
  schedulePoll(relayActive ? POLL_MS : null);
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
    if (envelope) {
      const relayActive = envelope.relay && RELAY_IN_FLIGHT.has(envelope.relay.status);
      renderActions(
        relayActive ? null : envelope.next_action,
        relayActive ? [] : envelope.alternate_actions,
      );
    }
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
      renderActions(null);
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
    if (!relayStarted && envelope) {
      renderActions(envelope.next_action, envelope.alternate_actions);
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
