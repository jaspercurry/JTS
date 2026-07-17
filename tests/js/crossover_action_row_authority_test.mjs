// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// 2026-07-16 hardware-confirmed bug: runAction()'s finally re-rendered
// envelope.next_action with no relay gate, so a primary action button could
// appear beside the "Open phone capture" relay link (jasper/web/
// correction_crossover_flow.py hardcodes that link as btn--primary and
// unhides it while a relay is in flight) — two green buttons, no way to
// tell which one to press. The fix made renderActionRow() the sole
// authority for the action row across every call-site (render(), stopRelay()'s
// finally, and both of runAction()'s relay touch-points); this harness pins
// the invariant it restores: at most one primary control (the action row's
// button/link, or the relay link) is visible at any time, across all four
// call-site shapes.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function classList() {
  const values = new Set();
  return {
    add(...names) { names.forEach((name) => values.add(name)); },
    remove(...names) { names.forEach((name) => values.delete(name)); },
    contains(name) { return values.has(name); },
    toggle(name, force) {
      if (force) values.add(name); else values.delete(name);
    },
  };
}

function element(id = "") {
  return {
    id,
    children: [],
    classList: classList(),
    dataset: {},
    disabled: false,
    textContent: "",
    href: "",
    addEventListener() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) { this[key] = String(value); },
  };
}

const ids = [
  "crossover-verdict",
  "crossover-applied",
  "crossover-start-over",
  "crossover-steps",
  "crossover-nudges",
  "crossover-review",
  "crossover-review-body",
  "crossover-action",
  "crossover-relay",
  "crossover-relay-status",
  "crossover-relay-link",
  "crossover-relay-qr",
  "crossover-relay-stop",
  "capture-status",
];
const elements = new Map(ids.map((id) => [id, element(id)]));
globalThis.document = {
  visibilityState: "visible",
  addEventListener() {},
  createElement: (tag) => element(tag),
  getElementById: (id) => elements.get(id),
};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};

let nextEnvelope = null;
let postResponse = { status: "ok" };
globalThis.__getJSON = async () => nextEnvelope;
globalThis.__postJSON = async () => postResponse;
globalThis.__renderRelayQr = () => {};

const here = dirname(fileURLToPath(import.meta.url));
let source = readFileSync(
  resolve(here, "../../deploy/assets/correction/js/crossover/main.js"),
  "utf8",
);
source = source.replace(
  /^import\s+\{[^}]+\}\s+from\s+["'][^"']+["'];\s*\n?/gm,
  "",
);
source =
  "const getJSON = globalThis.__getJSON; const postJSON = globalThis.__postJSON; " +
  "const renderRelayQr = globalThis.__renderRelayQr;\n" + source;
const bootStart = source.lastIndexOf("\nrefresh().catch((error) => {");
if (bootStart < 0) throw new Error("crossover module boot call not found");
source = source.slice(0, bootStart).concat(
  "\nexport { render, runAction, stopRelay };\n",
);
const dataUrl =
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { render, runAction, stopRelay } = await import(dataUrl);

const nextAction = {
  id: "level_match",
  label: "Continue",
  endpoint: "/correction/crossover/level-match",
  body: {},
  enabled: true,
};

function actionRowChildren() { return elements.get("crossover-action").children; }
function relayLinkVisible() {
  // The page's only hiding mechanism is the native `hidden` attribute
  // (app.css defines `[hidden] { display: none !important; }` and no
  // `.hidden` class rule) — assert the property the CSS actually implements,
  // not a classList token the stylesheet never wired up.
  return !elements.get("crossover-relay-link").hidden;
}

// Only one of {action-row primary, relay link} may be visible at once — the
// invariant the whole fix exists to restore.
function assertSinglePrimary(label) {
  const primaries = actionRowChildren().filter(
    (child) => String(child.className || "").includes("btn--primary"),
  );
  assert.ok(
    !(primaries.length > 0 && relayLinkVisible()),
    `${label}: action-row primary and relay link must not both be visible`,
  );
}

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

// --- (a) relay in flight: the relay link is the primary; action row empty --
render({
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  relay: { status: "awaiting_phone", tap_link: "https://capture.test/#s=a" },
  next_action: nextAction,
  alternate_actions: [],
});
check(actionRowChildren().length === 0, "(a) relay in flight: action row is empty");
check(relayLinkVisible(), "(a) relay in flight: relay link is visible");
assertSinglePrimary("(a) relay in flight");

// --- (b) relay terminal: the first next_action is the primary -------------
render({
  verdict_text: "Stopped",
  steps: [],
  nudges: [],
  relay: { status: "stopped", error: "Measurement stopped safely." },
  next_action: nextAction,
  alternate_actions: [],
});
check(actionRowChildren().length === 1, "(b) relay terminal: one action rendered");
check(
  String(actionRowChildren()[0].className).includes("btn--primary"),
  "(b) relay terminal: the rendered action is primary",
);
check(
  actionRowChildren()[0].textContent === "Continue",
  "(b) relay terminal: renders the envelope's next_action",
);
check(!relayLinkVisible(), "(b) relay terminal: relay link is hidden");
assertSinglePrimary("(b) relay terminal");

// --- (c) action completes and its own response started a relay ------------
// runAction()'s optimistic hide (using response.relay) and its finally
// (skipped when relayStarted) must together leave the row empty throughout
// — not just after the eventual refresh().
render({
  verdict_text: "Ready",
  steps: [],
  nudges: [],
  relay: null,
  next_action: nextAction,
  alternate_actions: [],
});
postResponse = {
  relay: { status: "awaiting_phone", tap_link: "https://capture.test/#s=c" },
};
nextEnvelope = {
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  relay: { status: "awaiting_phone", tap_link: "https://capture.test/#s=c" },
  next_action: nextAction,
  alternate_actions: [],
};
await runAction({ ...nextAction }, element("continue-button"));
check(
  actionRowChildren().length === 0,
  "(c) action started a relay: action row stays empty after completion",
);
check(relayLinkVisible(), "(c) action started a relay: relay link is visible");
assertSinglePrimary("(c) action started a relay");

// --- (d) action completes with no relay: the fresh next_action shows ------
// This is also the exact historical bug shape: the action's own response
// carries no relay (relayStarted === false), but by the time the finally
// block runs, the server's envelope (fetched by the awaited refresh())
// already reports the SAME relay as active — from an earlier action, a
// concurrent poll, or the phone side racing ahead. The pre-fix finally
// called renderActions(envelope.next_action, ...) unconditionally, so this
// exact combination reproduced the two-primary-buttons bug even though this
// particular action never itself started anything.
postResponse = { status: "ok" };
nextEnvelope = {
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  relay: { status: "awaiting_phone", tap_link: "https://capture.test/#s=d1" },
  next_action: nextAction,
  alternate_actions: [],
};
await runAction(
  { endpoint: "/correction/crossover/some-other-step", body: {} },
  element("other-button"),
);
check(
  actionRowChildren().length === 0,
  "(d1) no relay from this action, but envelope reports one active: action row stays empty",
);
check(relayLinkVisible(), "(d1) no relay from this action, envelope active: relay link visible");
assertSinglePrimary("(d1) no relay from this action, envelope active");

// The ordinary (non-buggy) shape of (d): no relay anywhere. The action row
// must NOT stay stuck hidden — the fresh next_action renders normally.
nextEnvelope = {
  verdict_text: "Ready for the next step",
  steps: [],
  nudges: [],
  relay: null,
  next_action: nextAction,
  alternate_actions: [],
};
await runAction(
  { endpoint: "/correction/crossover/some-other-step", body: {} },
  element("other-button-2"),
);
check(
  actionRowChildren().length === 1,
  "(d2) no relay anywhere: the fresh next_action renders",
);
check(
  String(actionRowChildren()[0].className).includes("btn--primary"),
  "(d2) no relay anywhere: the rendered action is primary",
);
check(!relayLinkVisible(), "(d2) no relay anywhere: relay link stays hidden");
assertSinglePrimary("(d2) no relay anywhere");

// --- stopRelay()'s finally also routes through the single authority -------
nextEnvelope = {
  verdict_text: "Stopped",
  steps: [],
  nudges: [],
  relay: { status: "stopped", error: "Measurement stopped safely." },
  next_action: nextAction,
  alternate_actions: [],
};
render({
  verdict_text: "Awaiting phone",
  steps: [],
  nudges: [],
  relay: { status: "awaiting_phone", tap_link: "https://capture.test/#s=stop" },
  next_action: null,
  alternate_actions: [],
});
postResponse = { relay: { status: "stopping" } };
await stopRelay();
check(
  actionRowChildren().length === 1 && actionRowChildren()[0].textContent === "Continue",
  "stopRelay finally: renders the post-stop envelope's next_action via the shared authority",
);
check(!relayLinkVisible(), "stopRelay finally: relay link is hidden once terminal");
assertSinglePrimary("stopRelay finally");

console.log(JSON.stringify({ ok: true, passed }));
