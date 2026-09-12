// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0


import assert from "node:assert/strict";
import { crossoverMainModule, element } from "./_dom.mjs";

globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

let nextEnvelope = null;
let postResponse = { status: "ok" };
const { elements, render, runAction, stopCapture } = await crossoverMainModule({
  extraStubs: {
    getJSON: async () => nextEnvelope,
    postJSON: async () => postResponse,
    renderCloud: () => {},
    redrawCloudChart: () => {},
  },
  exportNames: ["render", "runAction", "stopCapture"],
});

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

render({
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  capture: { status: "awaiting_capture" },
  next_action: nextAction,
  alternate_actions: [],
});
check(actionRowChildren().length === 0, "(a) capture in flight: action row is empty");

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

});
check(
  actionRowChildren().length === 1
    && String(actionRowChildren()[0].className).includes("btn--primary")
    && actionRowChildren()[0].textContent === "Primary action during hold",
  "(e) show_during_capture: the primary renders during the hold",
);


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
check(
  actionRowChildren()[0].disabled === false,
  "(g) click-swallowing: runAction ran to completion and the row re-enabled",
);

for (const enabled of [false, true]) {
  render({next_action: {...holdPrimaryAction, enabled}});
  check(actionRowChildren()[0].disabled === !enabled, "server controls action availability");
}

for (const screen of ["awaiting_plan", "finished"]) {
  const action = screen === "finished"
    ? {id: "reset", label: "Reset", endpoint: "/test/server-reset", body: {run: "one"}}
    : null;
  nextEnvelope = {screen, verdict_text: "Server run status", next_action: action};
  render(nextEnvelope);
  check(elements.get("crossover-verdict").textContent === nextEnvelope.verdict_text,
    "the screen renders the server sentence");
  check(actionRowChildren().length === (action ? 1 : 0), "the screen renders only listed actions");
}

console.log(JSON.stringify({ ok: true, passed }));
