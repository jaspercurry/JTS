// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const [sectionsPath, actionsPath] = process.argv.slice(2);
if (!sectionsPath || !actionsPath) {
  throw new Error("usage: node system_latency_control_test.mjs <sections.js> <actions.js>");
}

function moduleBody(path) {
  return readFileSync(path, "utf8")
    .replace(/^import[\s\S]*?;\n/gm, "")
    .replace(/^export /gm, "");
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const sections = await new AsyncFunction(
  `${moduleBody(sectionsPath)}\nreturn { updateUsbLatency };`,
)();

function button(mode) {
  return {
    mode,
    el: {
      attrs: {}, dataset: {}, disabled: false,
      setAttribute(name, value) { this.attrs[name] = value; },
    },
  };
}

const refs = {
  preference: { textContent: "" },
  effective: { textContent: "" },
  live: { textContent: "" },
  status: { textContent: "" },
  buttons: [button("low"), button("medium"), button("high")],
};

for (const [state, selected, effective, label, pending] of [
  ["recovery", "medium", null, "Adjusting", true],
  ["fallback", "low", "high", "High · stable fallback", true],
  ["idle", "low", null, "Not active", true],
  ["starting", "low", "high", "Starting", true],
  ["applying", "medium", null, "Starting", true],
  ["error", "low", "high", "High", true],
  ["unavailable", "low", null, "unknown", true],
  ["applied", "low", "low", "Low", false],
  ["applied", "high", "high", "High", false],
]) {
  sections.updateUsbLatency(refs, {
    selected_mode: selected, effective_mode: effective,
    live_buffer_ms: 42.7, state, detail: "Live status from the speaker",
  });
  assert.equal(refs.effective.textContent, label);
  assert.equal(refs.live.textContent, "42.7 ms");
  assert.equal(refs.status.textContent, "Live status from the speaker");
  for (const item of refs.buttons) {
    const chosen = item.mode === selected;
    assert.equal(item.el.attrs["aria-pressed"], String(chosen));
    assert.equal(item.el.dataset.latencyPending, String(chosen && pending));
    assert.equal(item.el.disabled, false);
  }
}

const quietConsole = { error() {} };
// postJSON's failure contract: a non-2xx throws an Error carrying the
// server's JSON verdict on .body/.status, with .message = body.error.
const actions = await new AsyncFunction(
  "postJSON", "console",
  `${moduleBody(actionsPath)}\nreturn { setLatencyMode };`,
)(
  async () => {
    const err = new Error("fan-in restart failed");
    err.status = 502;
    err.body = { error: "fan-in restart failed" };
    throw err;
  },
  quietConsole,
);

await actions.setLatencyMode({ latency: refs }, "high");
assert.match(refs.status.textContent, /Could not apply: fan-in restart failed/);
assert.ok(refs.buttons.every((item) => item.el.disabled === false));
