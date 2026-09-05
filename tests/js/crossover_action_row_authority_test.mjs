// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// renderActionRow() is the SOLE authority for the action row across every
// call-site (render(), stopCapture()'s finally, and both of runAction()'s
// capture touch-points). This harness pins what each of those four shapes
// leaves in the row: nothing while a capture is in flight, the envelope's
// own next_action once it is terminal, and a show_during_capture action
// throughout a hold.

import assert from "node:assert/strict";
import { aliasGlobals, loadEsm, repoPath } from "./_loader.mjs";
import { CROSSOVER_IDS, element, installFixedDocument } from "./_dom.mjs";

const elements = installFixedDocument(CROSSOVER_IDS);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

let nextEnvelope = null;
let postResponse = { status: "ok" };
globalThis.__getJSON = async () => nextEnvelope;
globalThis.__postJSON = async () => postResponse;
// PR-7's before/after visualization (./cloud.js) is out of scope for this
// harness — it only pins the action-row authority — so a no-op stands in,
globalThis.__renderCloud = () => {};
globalThis.__redrawCloudChart = () => {};

const { render, runAction, stopCapture } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/main.js"),
  {
    rewrite: [[/^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm, ""]],
    prelude: aliasGlobals([
      "getJSON", "postJSON", "renderCloud", "redrawCloudChart",
    ]),
    truncateBefore: "\nrefresh().catch((error) => {",
    exportNames: ["render", "runAction", "stopCapture"],
  },
);

const nextAction = {
  id: "restart_session",
  label: "Continue",
  endpoint: "/sound/speaker/crossover/v2/session",
  body: {},
  enabled: true,
};

function actionRowChildren() { return elements.get("crossover-action").children; }
let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

// --- (a) capture in flight: the capture session is the primary; action row empty --
render({
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  capture: { status: "awaiting_capture" },
  next_action: nextAction,
  alternate_actions: [],
});
check(actionRowChildren().length === 0, "(a) capture in flight: action row is empty");

// --- (b) capture terminal: the first next_action is the primary -------------
render({
  verdict_text: "Stopped",
  steps: [],
  nudges: [],
  capture: { status: "stopped", error: "Measurement stopped safely." },
  next_action: nextAction,
  alternate_actions: [],
});
check(actionRowChildren().length === 1, "(b) capture terminal: one action rendered");
check(
  String(actionRowChildren()[0].className).includes("btn--primary"),
  "(b) capture terminal: the rendered action is primary",
);
check(
  actionRowChildren()[0].textContent === "Continue",
  "(b) capture terminal: renders the envelope's next_action",
);

// --- (c) action completes and its own response started a capture ------------
// runAction()'s optimistic hide (using response.capture) and its finally
// (skipped when captureStarted) must together leave the row empty throughout
// — not just after the eventual refresh().
render({
  verdict_text: "Ready",
  steps: [],
  nudges: [],
  capture: null,
  next_action: nextAction,
  alternate_actions: [],
});
postResponse = {
  capture: { status: "awaiting_capture" },
};
nextEnvelope = {
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  capture: { status: "awaiting_capture" },
  next_action: nextAction,
  alternate_actions: [],
};
await runAction({ ...nextAction }, element("continue-button"));
check(
  actionRowChildren().length === 0,
  "(c) action started a capture: action row stays empty after completion",
);

// --- (d) action completes with no capture: the fresh next_action shows ------
// This is also the exact historical bug shape: the action's own response
// carries no capture (captureStarted === false), but by the time the finally
// block runs, the server's envelope (fetched by the awaited refresh())
// already reports the SAME capture as active — from an earlier action, a
// concurrent poll, or the phone side racing ahead. The pre-fix finally
// called renderActions(envelope.next_action, ...) unconditionally, so this
// exact combination reproduced the two-primary-buttons bug even though this
// particular action never itself started anything.
postResponse = { status: "ok" };
nextEnvelope = {
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  capture: { status: "awaiting_capture" },
  next_action: nextAction,
  alternate_actions: [],
};
await runAction(
  { endpoint: "/sound/speaker/crossover/some-other-step", body: {} },
  element("other-button"),
);
check(
  actionRowChildren().length === 0,
  "(d1) no capture from this action, but envelope reports one active: action row stays empty",
);

// The ordinary (non-buggy) shape of (d): no capture anywhere. The action row
// must NOT stay stuck hidden — the fresh next_action renders normally.
nextEnvelope = {
  verdict_text: "Ready for the next step",
  steps: [],
  nudges: [],
  capture: null,
  next_action: nextAction,
  alternate_actions: [],
};
await runAction(
  { endpoint: "/sound/speaker/crossover/some-other-step", body: {} },
  element("other-button-2"),
);
check(
  actionRowChildren().length === 1,
  "(d2) no capture anywhere: the fresh next_action renders",
);
check(
  String(actionRowChildren()[0].className).includes("btn--primary"),
  "(d2) no capture anywhere: the rendered action is primary",
);

// --- stopCapture()'s finally also routes through the single authority -------
nextEnvelope = {
  verdict_text: "Stopped",
  steps: [],
  nudges: [],
  capture: { status: "stopped", error: "Measurement stopped safely." },
  next_action: nextAction,
  alternate_actions: [],
};
render({
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  capture: { status: "awaiting_capture" },
  next_action: null,
  alternate_actions: [],
});
postResponse = { capture: { status: "stopping" } };
await stopCapture();
check(
  actionRowChildren().length === 1 && actionRowChildren()[0].textContent === "Continue",
  "stopCapture finally: renders the post-stop envelope's next_action via the shared authority",
);

// --- (e) a show_during_capture PRIMARY renders alone during a live capture -----
// W6.10 blocker #2's general mechanism: a next_action marked show_during_capture
// renders as the SINGLE primary even while the capture is in flight (the
// gate that otherwise suppresses next_action beside a live phone link, so a
// second capture can't be started, has an explicit escape hatch for the one
// action that legitimately needs to stay reachable throughout a hold) — the
// misleading "Open phone capture" link/QR is suppressed, and any populated
// candidate_review still renders. Historically exercised by the v2 crossover
// review_apply screen's Apply action (removed by the 2026-07-20 owner
// ruling — apply is now automatic); the mechanism itself is still live today
// via verify_fail's Undo/Re-measure alternates (scenario (f) below), so this
// scenario keeps exercising it directly with a generic fixture rather than a
// dead endpoint.
const holdPrimaryAction = {
  id: "hold_primary_action",
  label: "Primary action during hold",
  endpoint: "/sound/speaker/crossover/v2/some-primary-action",
  body: { fingerprint: "fp-1" },
  show_during_capture: true,
};
render({
  verdict_text: "Something to review while the phone holds",
  steps: [],
  nudges: [],
  capture: { status: "awaiting_capture" },
  next_action: holdPrimaryAction,
  alternate_actions: [],
  candidate_review: {
    trims: [{ role: "woofer", attenuation_db: -2.5 }],
    delay: { role: "woofer", delay_ms: 0.25 },
    polarity: "invert",
    confidence: 0.8,
    fingerprint: "fp-1",
  },
});
check(
  actionRowChildren().length === 1
    && String(actionRowChildren()[0].className).includes("btn--primary")
    && actionRowChildren()[0].textContent === "Primary action during hold",
  "(e) show_during_capture: the primary renders during the hold",
);
check(
  !elements.get("crossover-review").hidden,
  "(e) show_during_capture: the candidate card is shown",
);

// --- (f) verify_fail during a live capture: Undo + Re-measure show, Try again gated -
// W6.12 P0-adjacent fix: right after a failed VERIFY capture the capture object
// can still be transitioning ("stopping" while the walk drains) for a real
// window before it settles. Before this
// fix the capture gate blanket-cleared EVERY alternate action during that
// window, so the household saw NO buttons at all on the verify_fail screen
// and had no obvious reason to guess "hit Stop" to make them reappear.
// The way back and verify_remeasure carry show_during_capture (the same
// escape hatch (e) uses for Apply); verify_retry ("Try again") deliberately
// does not, since it starts a brand-new session and racing the one still
// tearing down is exactly what the gate exists to prevent.
const verifyRetryAction = {
  id: "verify_retry",
  label: "Try again",
  endpoint: "/sound/speaker/crossover/v2/verify",
  body: {},
};
const wayBackAction = {
  id: "republish_previous",
  label: "Go back to the previous tuning",
  endpoint: "/sound/speaker/crossover/v2/republish",
  body: { fingerprint: "fp-previous" },
  show_during_capture: true,
};
const verifyRemeasureAction = {
  id: "verify_remeasure",
  label: "Re-measure",
  endpoint: "/sound/speaker/crossover/v2/session",
  body: {},
  expert: true,
  show_during_capture: true,
};
render({
  verdict_text: "That measurement didn't check out.",
  steps: [],
  nudges: [{ code: "verify_out_of_tolerance", severity: "warn", text: "x" }],
  capture: { status: "stopping" },
  next_action: verifyRetryAction,
  alternate_actions: [wayBackAction, verifyRemeasureAction],
});
const fLabels = actionRowChildren().map((child) => child.textContent);
check(
  actionRowChildren().length === 2,
  "(f) verify_fail during a live capture: exactly the way back + Re-measure render",
);
check(
  fLabels.includes("Go back to the previous tuning"),
  "(f) verify_fail during a live capture: the way back renders",
);
check(
  fLabels.includes("Re-measure"),
  "(f) verify_fail during a live capture: Re-measure renders",
);
check(
  !fLabels.includes("Try again"),
  "(f) verify_fail during a live capture: Try again stays gated until Stop",
);

// --- (g) click-swallowing: an unchanged envelope must not replace the row --
// W6.12: renderActions() used to call els.action.replaceChildren() on EVERY
// render(), tearing the row down and rebuilding it even when nothing about
// it had changed — every ~1.5s poll ran through this unconditionally.
// Hardware round 4 lost 4 taps this way: a poll landed between pointerdown
// and click and replaced the button the tap was headed for out from under
// it. A click dispatched against the SAME node across two identical-content
// renders (exactly what a repeated poll response looks like — same fields,
// a fresh object each time) must still land.
const clickAction = {
  id: "restart_session",
  label: "Continue",
  endpoint: "/sound/speaker/crossover/v2/session",
  body: {},
  enabled: true,
};
const clickEnvelope = () => ({
  verdict_text: "Ready",
  steps: [],
  nudges: [],
  capture: null,
  next_action: clickAction,
  alternate_actions: [],
});
render(clickEnvelope());
const survivingButton = actionRowChildren()[0];
check(Boolean(survivingButton), "(g) click-swallowing: a button rendered");

// A poll landing with an unchanged envelope — a fresh object, identical
// content.
render(clickEnvelope());
check(
  actionRowChildren()[0] === survivingButton,
  "(g) click-swallowing: the SAME node survives an identical-content re-render",
);

postResponse = { status: "ok" };
nextEnvelope = clickEnvelope();
const clickResult = survivingButton.click();
check(
  survivingButton.disabled === true,
  "(g) click-swallowing: the click on the surviving node landed synchronously",
);
await clickResult;
// runAction's own finally re-renders once busy clears — by then the action
// row is legitimately allowed to rebuild (busy is part of the key); the
// fresh button coming out re-enabled proves the click ran to completion
// rather than getting stuck disabled or throwing.
check(
  actionRowChildren()[0].disabled === false,
  "(g) click-swallowing: runAction ran to completion and the row re-enabled",
);

// --- (h) tier chooser: description + Recommended badge (flow-simplification
// PR-U3, S1/S2 fixes from the adversarial review of PR #1780) render via a
// dedicated `.tier-choices` grid of `.measurement-row` cards — equal-width,
// flush-left peers, badge the only differentiator, no duplicated label — and
// every OTHER action (every scenario above — none carries `description`)
// renders as a bare button/link, unchanged. -------------------------------
const recommendedTierAction = {
  id: "start_v2_session_full",
  label: "Full measurement",
  description:
    "About 11 min — 16 measurements; re-checks the result at several spots around the mark.",
  recommended: true,
  endpoint: "/sound/speaker/crossover/v2/session",
  body: { tier: "full" },
};
const otherTierAction = {
  id: "start_v2_session_express",
  label: "Quick tune",
  description: "About 5 min — 7 measurements; confirms the result at the mark.",
  recommended: false,
  endpoint: "/sound/speaker/crossover/v2/session",
  body: { tier: "express" },
};
render({
  verdict_text: "Choose how thorough a measurement to run below.",
  steps: [],
  nudges: [],
  capture: null,
  next_action: recommendedTierAction,
  alternate_actions: [otherTierAction],
});
// S1: the action row itself holds ONE dedicated container, not two
// independently-sized/right-aligned cards.
check(
  actionRowChildren().length === 1 && actionRowChildren()[0].className === "tier-choices",
  "(h) tier chooser: the two cards render inside one dedicated .tier-choices container",
);
const tierRows = actionRowChildren()[0].children;
check(tierRows.length === 2, "(h) tier chooser: two rows render, one per tier");
const [primaryRow, otherRow] = tierRows;
check(
  primaryRow.className === "measurement-row" && otherRow.className === "measurement-row",
  "(h) tier chooser: both wrap in the shared measurement-row shape (description present)",
);
const [primaryText, primaryButton] = primaryRow.children;
const [primaryHead, primaryMeta] = primaryText.children;
// S2: title + badge sit in their own flex head row (gap 0.6rem, mirroring
// wake_setup.py's .wake-row__head), not a zero-gap child of the title itself.
check(
  primaryHead.className === "measurement-row__head",
  "(h) tier chooser: title + badge share a dedicated flex head row",
);
const [primaryTitle] = primaryHead.children;
check(
  primaryTitle.className === "measurement-row__title",
  "(h) tier chooser: the title paragraph uses the shared title class",
);
check(
  primaryTitle.textContent === "Full measurement",
  "(h) tier chooser: the title text is the action's label",
);
check(
  primaryHead.children.length === 2
    && primaryHead.children[1].className === "badge badge--ok"
    && primaryHead.children[1].textContent === "Recommended",
  "(h) tier chooser: the recommended action's head carries a Recommended badge",
);
check(
  primaryMeta.className === "measurement-row__meta" && primaryMeta.textContent === recommendedTierAction.description,
  "(h) tier chooser: the meta line is the action's one-line claims description, verbatim",
);
// S2: the button no longer repeats the row's own title.
check(
  primaryButton.textContent === "Start",
  "(h) tier chooser: the control is a short 'Start' CTA, not a duplicate of the row title",
);
// S1: both cards are equal-weight peers — the badge is the ONLY visual
// differentiator, so both buttons carry the SAME class.
check(
  String(primaryButton.className).includes("btn--primary"),
  "(h) tier chooser: the recommended action's own button is primary",
);
const [otherText, otherButton] = otherRow.children;
const [otherHead] = otherText.children;
check(
  otherHead.children.length === 1,
  "(h) tier chooser: the non-recommended action's head carries no badge",
);
check(
  String(otherButton.className).includes("btn--primary")
    && otherButton.className === primaryButton.className,
  "(h) tier chooser: the non-recommended action's button matches the recommended one's class — badge is the only differentiator",
);

// Every earlier scenario's actions had no `description` — confirm those
// still render as bare buttons (no .tier-choices/.measurement-row wrapper
// introduced by this change).
render({
  verdict_text: "Ready",
  steps: [],
  nudges: [],
  capture: null,
  next_action: nextAction,
  alternate_actions: [],
});
check(
  actionRowChildren()[0].className !== "measurement-row"
    && actionRowChildren()[0].className !== "tier-choices",
  "(h) tier chooser: an action with no description still renders as a bare control",
);

// --- (i) `enabled: false` actually disables the control -------------------
//
// The renderer's `disabled: busy || action.enabled === false` seam has shipped
// for a while but was only ever exercised with `enabled: true`. The two-stage
// review screen (work order D3/D6, issue #1806) makes it load-bearing: the
// envelope disables Apply when the prediction is ungradeable or the stage-2
// openability preflight refused, and the whole point is that a household
// CANNOT apply an unevidenced proposal. Server-side that state is pinned in
// tests/test_crossover_envelope_v2.py; this is the client half — without it,
// a renderer regression would present a live Apply button over exactly the
// proposals that must not be applyable, and every server-side test would
// still pass.
render({
  verdict_text: "Review",
  steps: [],
  nudges: [],
  capture: null,
  next_action: {
    id: "review_apply",
    label: "Apply and verify",
    endpoint: "/sound/speaker/crossover/v2/apply",
    body: {expected_candidate_fingerprint: "fp-1"},
    enabled: false,
    show_during_capture: true,
  },
  alternate_actions: [],
});
check(
  actionRowChildren()[0].disabled === true,
  "(i) a next_action with enabled:false renders a DISABLED control — the " +
  "review screen's refusal to offer an apply it cannot stand behind",
);

// ...and the same action with the flag flipped is live, so the assertion above
// is testing the flag rather than something incidental to this envelope.
render({
  verdict_text: "Review",
  steps: [],
  nudges: [],
  capture: null,
  next_action: {
    id: "review_apply",
    label: "Apply and verify",
    endpoint: "/sound/speaker/crossover/v2/apply",
    body: {expected_candidate_fingerprint: "fp-1"},
    enabled: true,
    show_during_capture: true,
  },
  alternate_actions: [],
});
check(
  actionRowChildren()[0].disabled === false,
  "(i) the same action with enabled:true is live",
);

console.log(JSON.stringify({ ok: true, passed }));
