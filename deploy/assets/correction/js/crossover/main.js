// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { getJSON, postJSON } from '/assets/shared/js/http.js';
import { jtsConfirm } from '/assets/shared/js/dialog.js';
import { renderCloud, redrawCloudChart } from './cloud.js';
import { positionDiagram, positionCaption } from './position-diagram.js';
import { UNIT_IMPERIAL, UNIT_METRIC, currentUnits, formatDistances, setUnits } from './units.js';

const els = {
  verdict: document.getElementById('crossover-verdict'),
  applied: document.getElementById('crossover-applied'),
  startOver: document.getElementById('crossover-start-over'),
  steps: document.getElementById('crossover-steps'),
  nudges: document.getElementById('crossover-nudges'),
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
  walkUnitsImperial: document.getElementById('crossover-units-imperial'),
  walkUnitsMetric: document.getElementById('crossover-units-metric'),
  walkProgress: document.getElementById('crossover-walk-progress'),
  walkDiagram: document.getElementById('crossover-walk-diagram'),
  walkCaption: document.getElementById('crossover-walk-caption'),
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
const CAPTURE_STOPPABLE = new Set(['awaiting_join', 'starting', 'awaiting_capture']);
// Wind-down: in flight, but the captures are over and the session is draining
// its own work. A walkthrough has nothing to say here — the status line
// narrates it instead.
const CAPTURE_WINDING_DOWN = new Set(['stopping']);
const CAPTURE_IN_FLIGHT = new Set([...CAPTURE_STOPPABLE, ...CAPTURE_WINDING_DOWN]);

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
  const href = (action && action.href) || '';
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
  const details = Array.isArray(expertDetails) ? expertDetails : [];
  if (details.length) {
    rows.push(el('details', {class: 'candidate-provenance'}, [
      el('summary', {text: 'Expert details'}),
      el('p', {class: 'measurement-row__meta', text: `${details.join('; ')}.`}),
    ]));
  }
  els.nudges.replaceChildren(...rows);
}

function renderActions(primary, alternates = []) {
  els.action.replaceChildren();
  const actions = [primary, ...(Array.isArray(alternates) ? alternates : [])]
    .filter(Boolean);
  actions.forEach((action, index) => {
    const className = index === 0 ? 'btn btn--primary' : 'btn btn--ghost';
    let control;
    if (action.href && !action.endpoint) {
      control = el('a', {
        class: className,
        href: action.href,
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
    els.action.append(control);
  });
}

// The prompt the walk is standing on, kept across the poll that follows a
// release. The gate clears `position_pending` the moment the capture is
// admitted, so without this the panel would drop from "Measurement 3 of 9 —
// turn the microphone to +7°" to a bare status line for the whole 25 s the
// tone plays, and the household would lose their place mid-round. Cleared
// whenever the session stops being in flight (renderWalk's own !active arm),
// so it can never describe a session that is over.
let walkPrompt = null;
let walkGeometry = null;
let lastWalkKey = null;

// A stable serialization of exactly what renderWalk builds — the same
// tap-preserving discipline as actionRowKey. renderWalk runs on every 1.5 s
// poll, and rebuilding the release button under a finger that is already
// down is how hardware round 4 lost taps on the action row.
function walkKey(prompt, pending, yielded, progress) {
  return JSON.stringify({prompt, pending: pending || null, yielded, busy, progress});
}

// See ADR-0296: the server owns each mover’s actions.
function renderWalkDiagram(pending) {
  const degrees = pending ? pending.degrees : 0;
  const verticalDeg = pending ? pending.vertical_deg : 0;
  const show = Boolean(pending) && (degrees || verticalDeg);
  els.walkDiagram.hidden = !show;
  els.walkCaption.hidden = !show;
  if (!show) {
    els.walkDiagram.replaceChildren();
    return;
  }
  els.walkDiagram.replaceChildren(positionDiagram(degrees, verticalDeg));
  els.walkCaption.textContent = positionCaption(degrees, verticalDeg);
}

// Re-applies the units preference to the currently displayed prompt without
// waiting for the next 1.5 s poll -- the diagram itself is unit-agnostic
// (degrees/vertical_deg, not text), so only the two prose lines need it.
function refreshUnitsDisplay() {
  if (!walkPrompt) return;
  els.walkHeadline.textContent = formatDistances(walkPrompt.title || '');
  els.walkDetail.textContent = formatDistances(walkPrompt.body || '');
}

function setUnitsButtons(unit) {
  const metric = unit === UNIT_METRIC;
  els.walkUnitsMetric.setAttribute('aria-pressed', String(metric));
  els.walkUnitsImperial.setAttribute('aria-pressed', String(!metric));
}

function renderWalk(capture, {active, yielded}) {
  const walking = Boolean(active && !CAPTURE_WINDING_DOWN.has(capture.status));
  const held = walking ? (capture.join || capture.position_pending) : null;
  const pending = held && held.mover === 'human' ? held : null;
  // The entry the gate is EXECUTING, and the only thing that moves during a
  // pose batch: configs 2..N are granted under the first config's release, so
  // no second hold is published and the retained prompt would otherwise freeze
  // the progress line for the whole batch. An open hold outranks it: the gate
  // never publishes both, so a `current` beside one is a stale leftover.
  const current = walking && !pending ? capture.position_current : null;
  if (pending && pending.prompt) walkPrompt = pending.prompt;
  if (pending) walkGeometry = {degrees: pending.degrees, vertical_deg: pending.vertical_deg};
  if (!walking) { walkPrompt = null; walkGeometry = null; }
  const show = Boolean(walking && walkPrompt && !yielded);
  const progress = show
    ? ((current && current.prompt.progress) || walkPrompt.progress || '')
    : '';
  const key = walkKey(show ? walkPrompt : null, show ? pending : null, yielded, progress);
  if (key === lastWalkKey) return;
  lastWalkKey = key;
  els.walk.hidden = !show;
  if (!show) {
    els.walkAction.replaceChildren();
    return;
  }
  els.walkProgress.textContent = progress;
  els.walkHeadline.textContent = formatDistances(walkPrompt.title || '');
  els.walkDetail.textContent = formatDistances(walkPrompt.body || '');
  els.walkDetail.hidden = !walkPrompt.body;
  renderWalkDiagram(walkGeometry);
  if (pending && pending.actions?.length) {
    els.walkAction.replaceChildren(...pending.actions.map((action, index) => {
      const button = el('button', {
        class: index === 0 ? 'btn btn--primary' : 'btn btn--ghost',
        type: 'button',
        disabled: busy,
        text: action.label,
      });
      button.addEventListener('click', () => runAction(action, button));
      return button;
    }));
  } else {
    els.walkAction.replaceChildren(
      el('p', {
        class: 'measurement-row__meta',
        text: 'Recording this spot — keep still until the tone stops.',
      }),
    );
  }
}

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
  const awaitingReader = Boolean(
    !suppressConnectAffordance &&
    (capture.join || capture.position_pending)?.mover === 'human',
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
  const showPrimary = !captureActive
    || (env.next_action && env.next_action.show_during_capture);
  const alternates = Array.isArray(env.alternate_actions) ? env.alternate_actions : [];
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

function screenOwnsLiveControl(env) {
  return Boolean(env && env.next_action && env.next_action.show_during_capture);
}

function render(env) {
  envelope = env;
  els.verdict.textContent = env.verdict_text || '';
  renderApplied(env.applied);
  renderSteps(env.steps);
  renderNudges(env.nudges, env.expert_details, env.findings);
  if (env.screen === 'awaiting_plan' || env.screen === 'finished') {
    renderCloud(els, {});
  } else {
    renderCloud(els, env);
  }
  renderCapture(env.capture, {
    suppressConnectAffordance: screenOwnsLiveControl(env),
  });
  renderActionRow(env);
  schedulePoll(captureIsActive(env.capture) || env.screen === 'awaiting_plan' ? POLL_MS : null);
}

async function stopCapture() {
  if (stopInFlight) return;
  busy = true;
  stopInFlight = true;
  renderEpoch += 1;
  els.captureStop.disabled = true;
  setStatus('Stopping safely…');
  try {
    const response = await postJSON('/sound/speaker/crossover/capture-cancel', {});
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
    const response = await postJSON('/sound/speaker/crossover/reset', {});
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
    const response = await postJSON(action.endpoint, action.body || {});
    captureStarted = captureIsActive(response && response.capture);
    if (captureStarted) {
      renderCapture(response.capture);
      // The response's capture hasn't landed in `envelope` yet (that happens
      // inside refresh() below) — hide the action row immediately against
      // the capture we just started rather than waiting a round trip.
      renderActionRow({capture: response.capture, next_action: null, alternate_actions: []});
      schedulePoll(POLL_MS);
    }
    setStatus(captureStarted ? 'Measurement started.' : 'Updated.', 'ok');
    await refresh();
  } catch (error) {
    const failureMessage = error && error.message ? error.message : String(error);
    const refusalAction = error && error.body && error.body.next_action
      ? error.body.next_action : null;
    setStatus(failureMessage, 'bad', refusalAction);
    try {
      await refresh();
      setStatus(failureMessage, 'bad', refusalAction);
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
    const env = await getJSON('/sound/speaker/crossover/envelope');
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
  // Page-local units preference (#3629, #1941 Q2): a static toggle, wired
  // once here like Start Over / Stop above -- unlike the walk's own action
  // button, it is never rebuilt by a render pass.
  setUnitsButtons(currentUnits());
  els.walkUnitsImperial.addEventListener('click', () => {
    setUnits(UNIT_IMPERIAL);
    setUnitsButtons(UNIT_IMPERIAL);
    refreshUnitsDisplay();
  });
  els.walkUnitsMetric.addEventListener('click', () => {
    setUnits(UNIT_METRIC);
    setUnitsButtons(UNIT_METRIC);
    refreshUnitsDisplay();
  });
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
// poll. Debounced at 150 ms (review S-4), so a
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
