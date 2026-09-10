// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The wired round's walkthrough (#2881): a human completes a GATED measurement
// at this browser alone, with no CLI posting the release. Every screen state is
// pinned here in the order a round meets them — opening, pending, the release
// POST, capturing, the screen taking the control back, wind-down, terminal —
// then the two holds this panel must NOT serve: one a driver releases, and a
// capture session, whose phone owns the tap.

import strict from "node:assert/strict";
import { crossoverMainModule } from "./_dom.mjs";

// The reported count is DERIVED, never typed: a hand-bumped literal drifts from
// what ran, and the Python bridge only checks it is >= 1.
let passed = 0;
const counted = (fn) => (...args) => { const out = fn(...args); passed += 1; return out; };
const assert = new Proxy(strict, {
  apply: (target, _this, args) => counted(target)(...args),
  get: (target, prop) => {
    const value = Reflect.get(target, prop);
    return typeof value === "function" ? counted(value.bind(target)) : value;
  },
});

globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

let nextEnvelope = null;
const posted = [];
// This file pins the release-hold state machine, not the walk's per-
// position picture/units logic (which has its own
// tests/js/crossover_position_diagram_test.mjs and
// tests/js/crossover_units_test.mjs) -- those stub through unchanged.
const { elements, render } = await crossoverMainModule({
  extraStubs: {
    getJSON: async () => nextEnvelope,
    postJSON: async (path, body) => {
      posted.push({ path, body });
      return { ok: true, released: { index: body && body.index } };
    },
    renderCloud: () => {},
    redrawCloudChart: () => {},
  },
});

const walk = () => elements.get("crossover-walk");
const walkAction = () => elements.get("crossover-walk-action");
const captureStatus = () => elements.get("crossover-capture-status");

function assertWiredStatus() {
  const text = captureStatus().textContent;
  assert.ok(text.length > 0, "a live session must never show a blank status");
}

// The words are the CAPTURE PLAN's, not this page's: the gate copies the
// entry's own screen bag onto the hold, so a household reading the browser and
// a household reading a capture page are given the same sentence.
const PROMPT = {
  progress: "Measurement 3 of 9",
  title: "Turn the microphone to +7° (7° RIGHT of the design axis).",
  body: "Keep it 1 m from the speaker and pointed at it.",
};
const PENDING = {
  index: 3,
  attempt: 1,
  degrees: 7,
  role: "onax",
  prompt: PROMPT,
  hand_released: true,
  action: {
    id: "crossover_v2_position_ready",
    label: "Microphone is at +7°",
    endpoint: "/sound/speaker/crossover/v2/position-ready",
    body: { index: 3, attempt: 1, degrees: 7 },
  },
};
// The SAME hold shape on an externally positioned walk: the arm's driver
// releases it, so no browser control may.
const DRIVEN_PENDING = { ...PENDING, hand_released: false };

function envelope(capture, extra = {}) {
  return {
    verdict_text: "",
    steps: [],
    nudges: [],
    capture,
    next_action: null,
    alternate_actions: [],
    ...extra,
  };
}

// -- state: a wired session that has not begun holding yet ------------------ //
// The panel stays down (there is nothing to say), and the status line names
// the instrument rather than a link that will never be created.
render(envelope({ status: "starting", source: "wired" }));
assert.equal(walk().hidden, true);
assertWiredStatus();

// -- state: PENDING — the hold a human releases ----------------------------- //
render(envelope({
  status: "awaiting_capture",
  source: "wired",
  position_pending: PENDING,
}));
assert.equal(walk().hidden, false);
assert.equal(elements.get("crossover-walk-progress").textContent, PROMPT.progress);
assert.equal(elements.get("crossover-walk-headline").textContent, PROMPT.title);
assert.equal(elements.get("crossover-walk-detail").textContent, PROMPT.body);
assert.equal(elements.get("crossover-walk-detail").hidden, false);
const release = walkAction().children[0];
assert.equal(release.tag, "button");
// The SERVER's label, not one composed here — the copy is the gate's, so this
// pins where the words come from rather than what they say.
assert.equal(release.textContent, PENDING.action.label);
assert.equal(release.disabled, false);
// While HOLDING the status differs from the capturing sentence — the two
// moments must not read alike.
const holdingStatus = captureStatus().textContent;
assertWiredStatus();

// The release POSTs the endpoint the SERVER named, with the server's own body
// — the index is checked against what is actually pending, so a control that
// minted its own would release a position the microphone never reached.
nextEnvelope = envelope({ status: "awaiting_capture", source: "wired" });
await release.click();
assert.deepEqual(posted, [{
  path: "/sound/speaker/crossover/v2/position-ready",
  body: { index: 3, attempt: 1, degrees: 7 },
}]);

// -- state: CAPTURING — released, tone playing ------------------------------ //
// The gate drops `position_pending` the moment it admits the begin, so the
// spot's identity has to survive that or the household loses their place for
// the whole sweep. No button: there is nothing to press, and a live one would
// double-fire into a 409.
render(envelope({ status: "awaiting_capture", source: "wired" }));
assert.equal(walk().hidden, false);
assert.equal(elements.get("crossover-walk-progress").textContent, PROMPT.progress);
assert.equal(elements.get("crossover-walk-headline").textContent, PROMPT.title);
// A paragraph, NOT a button: the structural claim, so a re-word of the note
// does not fail this while a resurrected dead button would.
assert.equal(walkAction().children.length, 1);
assert.equal(walkAction().children[0].tag, "p");
assert.ok(walkAction().children[0].textContent.length > 0);
assertWiredStatus();
// Holding and capturing are different moments and must not read alike — the
// status line is the only thing that distinguishes them once the panel's own
// prompt is retained across the release.
assert.notEqual(captureStatus().textContent, holdingStatus);

// -- state: a POSE BATCH — one release, N configs --------------------------- //
// The defect this pins: configs 2..N of a batch are granted WITHOUT a hold of
// their own, so no second `position_pending` is ever published and the retained
// prompt froze the progress line at config 1 for the whole batch. The gate now
// publishes the entry it is executing; the pose has not changed, so only the
// progress line follows it.
const batchCurrent = (ordinal) => ({
  index: 2 + ordinal,
  attempt: 1,
  prompt: {...PROMPT, progress: `Config ${ordinal} of 3 — keep the mic still.`},
  batch: {start: 3, size: 3, ordinal},
});
render(envelope({
  status: "awaiting_capture",
  source: "wired",
  position_current: batchCurrent(2),
}));
assert.equal(walk().hidden, false);
assert.equal(
  elements.get("crossover-walk-progress").textContent,
  batchCurrent(2).prompt.progress,
);
// The pose is the same spot for every config in the batch, so the headline the
// household is holding the microphone against must not move under them.
assert.equal(elements.get("crossover-walk-headline").textContent, PROMPT.title);
assert.equal(walkAction().children[0].tag, "p");

render(envelope({
  status: "awaiting_capture",
  source: "wired",
  position_current: batchCurrent(3),
}));
assert.equal(
  elements.get("crossover-walk-progress").textContent,
  batchCurrent(3).prompt.progress,
);

// -- the NEXT pose: its own hold, its own button, no leftover progress ------- //
// The gate drops the executed entry when a fresh hold opens, so the batch's
// last config must not still be the line above the new pose's release button.
const NEXT_PROMPT = {
  progress: "Measurement 6 of 9",
  title: "Turn the microphone to -7° (7° LEFT of the design axis).",
  body: PROMPT.body,
};
const NEXT_PENDING = {
  ...PENDING,
  index: 6,
  degrees: -7,
  prompt: NEXT_PROMPT,
  action: {...PENDING.action, body: {index: 6, attempt: 1, degrees: -7}},
};
render(envelope({
  status: "awaiting_capture",
  source: "wired",
  position_pending: NEXT_PENDING,
}));
assert.equal(
  elements.get("crossover-walk-progress").textContent, NEXT_PROMPT.progress,
);
assert.equal(elements.get("crossover-walk-headline").textContent, NEXT_PROMPT.title);
assert.equal(walkAction().children[0].tag, "button");

// -- a fresh hold beside a STALE executing entry ---------------------------- //
// The gate never publishes both, so a poll carrying both straddled a release.
// The hold wins — its own progress line and its own button — and a repeat poll
// must not re-key that button under a finger already on it.
const straddled = {
  status: "awaiting_capture",
  source: "wired",
  position_pending: NEXT_PENDING,
  position_current: batchCurrent(3),
};
render(envelope(straddled));
assert.equal(
  elements.get("crossover-walk-progress").textContent, NEXT_PROMPT.progress,
);
const heldRelease = walkAction().children[0];
assert.equal(heldRelease.tag, "button");
assert.equal(heldRelease.textContent, NEXT_PENDING.action.label);
render(envelope(straddled));
assert.equal(walkAction().children[0], heldRelease);

// -- state: the SCREEN takes the control back ------------------------------- //
// The closing screen's Save / Record-again are `show_during_capture` primaries.
// One primary at a time: the walkthrough stands down rather than competing.
// Labels come off the fixtures, so a re-word of the server's copy does not
// need editing here — what is pinned is WHICH action reached the row.
const CLOSING_SAVE = {
  id: "crossover_v2_complete",
  label: "Save this measurement",
  endpoint: "/sound/speaker/crossover/v2/complete",
  body: {},
  show_during_capture: true,
};
const CLOSING_RETAKE = {
  id: "crossover_v2_retake",
  label: "Record the last spot again",
  endpoint: "/sound/speaker/crossover/v2/retake",
  body: {},
  show_during_capture: true,
};
const CLOSING_ACTIONS = {
  next_action: CLOSING_SAVE,
  alternate_actions: [CLOSING_RETAKE],
};
const actionLabels = () =>
  elements.get("crossover-action").children.map((node) => node.textContent);

render(envelope({ status: "awaiting_capture", source: "wired" }, CLOSING_ACTIONS));
assert.equal(walk().hidden, true);
assert.equal(walkAction().children.length, 0);
assert.deepEqual(actionLabels(), [CLOSING_SAVE.label, CLOSING_RETAKE.label]);
assertWiredStatus();

// -- REGRESSION: a retake from the closing screen must not vanish ----------- //
// The defect this pins: tapping Record-again puts the walk back at that slot
// and the gate publishes a fresh hand-released hold — but the group stays
// un-confirmed, so the screen is STILL `closing`. While the closing screen
// mints its own `show_during_capture` primary, `yielded` is true and the panel
// hides, so the retake's prompt rendered NOWHERE and the hold ran out its
// 600 s budget under a Save button. The server now withholds the pair while a
// hold is open; this is the JS half of that contract.
render(envelope({
  status: "awaiting_capture",
  source: "wired",
  position_pending: PENDING,
}));  // closing + held: no screen primary, so the walk owns the screen
assert.equal(walk().hidden, false);
assert.deepEqual(actionLabels(), []);
const retakeRelease = walkAction().children[0];
assert.equal(retakeRelease.tag, "button");
assert.equal(retakeRelease.textContent, PENDING.action.label);

// …the release still posts the server's own body, and the screen then returns
// to the closing pair once the walk finishes the re-recorded spot.
posted.length = 0;
nextEnvelope = envelope({ status: "awaiting_capture", source: "wired" });
await retakeRelease.click();
assert.deepEqual(posted, [{
  path: "/sound/speaker/crossover/v2/position-ready",
  body: PENDING.action.body,
}]);
render(envelope({ status: "awaiting_capture", source: "wired" }, CLOSING_ACTIONS));
assert.equal(walk().hidden, true);
assert.deepEqual(actionLabels(), [CLOSING_SAVE.label, CLOSING_RETAKE.label]);

// -- state: WIND-DOWN — the captures are over ------------------------------- //
// A retained prompt must not outlive the walk it described.
render(envelope({
  status: "stopping",
  source: "wired",
  position_pending: PENDING,
}));
assert.equal(walk().hidden, true);
assert.equal(
  captureStatus().textContent,
  "Stopping playback and restoring the speaker safely…",
);

// -- state: TERMINAL -------------------------------------------------------- //
render(envelope({ status: "complete", source: "wired" }));
assert.equal(elements.get("crossover-capture").hidden, true);
assert.equal(walk().hidden, true);

// -- a hold NOBODY here releases -------------------------------------------- //
// The arm's own walk is gated identically and rides the identical payload, so
// the discriminator has to be the hold's `hand_released`, not the transport.
// A release control here could free a position the arm has not reached.
render(envelope({
  status: "awaiting_capture",
  source: "wired",
  position_pending: DRIVEN_PENDING,
}));
assert.equal(walk().hidden, true);
assert.equal(walkAction().children.length, 0);
assertWiredStatus();

// -- a hold nobody at this browser releases is not narrated as a wait ------- //
render(envelope({
  status: "awaiting_capture",
  source: "wired",
  position_pending: DRIVEN_PENDING,
}));
assertWiredStatus();

console.log(JSON.stringify({ ok: true, passed }));
