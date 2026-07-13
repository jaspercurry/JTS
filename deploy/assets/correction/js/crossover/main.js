// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { getJSON, postJSON } from '/assets/shared/js/http.js';

const els = {
  verdict: document.getElementById('crossover-verdict'),
  steps: document.getElementById('crossover-steps'),
  nudges: document.getElementById('crossover-nudges'),
  action: document.getElementById('crossover-action'),
  relay: document.getElementById('crossover-relay'),
  relayStatus: document.getElementById('crossover-relay-status'),
  relayLink: document.getElementById('crossover-relay-link'),
  status: document.getElementById('capture-status'),
};

let envelope = null;
let busy = false;
let refreshInFlight = null;
let refreshQueued = false;
let renderEpoch = 0;
let pollTimer = null;

const POLL_MS = 1500;
const RETRY_MS = 5000;

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
    const button = el('button', {
      class: className,
      type: 'button',
      disabled: busy || action.enabled === false,
      text: action.label || 'Continue',
    });
    button.addEventListener('click', () => runAction(action, button));
    els.action.append(button);
  });
}

function renderRelay(relay) {
  const active = relay && ['starting', 'awaiting_phone'].includes(relay.status);
  els.relay.classList.toggle('hidden', !active);
  els.relayLink.classList.add('hidden');
  if (!active) {
    if (relay && relay.status === 'failed') {
      setStatus(relay.error || 'Phone capture failed. Retry this step.', 'bad');
    } else if (relay && relay.status === 'complete') {
      setStatus('Phone capture complete.', 'ok');
    }
    return;
  }
  if (relay.tap_link) {
    els.relayLink.href = relay.tap_link;
    els.relayLink.classList.remove('hidden');
    els.relayStatus.textContent = 'Open the trusted capture page and follow its one next step.';
  } else {
    els.relayStatus.textContent = 'Creating the phone capture link…';
  }
}

function render(env) {
  envelope = env;
  els.verdict.textContent = env.verdict_text || '';
  renderSteps(env.steps);
  renderNudges(env.nudges);
  renderRelay(env.relay);
  const relayActive = env.relay && ['starting', 'awaiting_phone'].includes(env.relay.status);
  renderActions(
    relayActive ? null : env.next_action,
    relayActive ? [] : env.alternate_actions,
  );
  schedulePoll(relayActive ? POLL_MS : null);
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
    const issues = error && error.body && Array.isArray(error.body.issues)
      ? error.body.issues : [];
    const candidateChanged = error && error.status === 409 && issues.some(
      (issue) => issue && issue.code === 'baseline_candidate_fingerprint_mismatch'
    );
    if (candidateChanged) {
      setStatus('The crossover candidate changed. Refreshing the review…', 'bad');
      try {
        await refresh();
        setStatus('Crossover review refreshed. Review the current candidate.', '');
      } catch (refreshError) {
        setStatus(
          refreshError && refreshError.message
            ? refreshError.message : String(refreshError),
          'bad'
        );
      }
    } else {
      setStatus(error && error.message ? error.message : String(error), 'bad');
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
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (
    delayMs === null ||
    (typeof document !== 'undefined' && document.visibilityState === 'hidden')
  ) return;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    refresh().catch((error) => {
      setStatus(error.message, 'bad');
      schedulePoll(RETRY_MS);
    });
  }, delayMs);
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
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      schedulePoll(null);
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
