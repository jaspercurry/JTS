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
  selected: { textContent: "" },
  applied: { textContent: "" },
  live: { textContent: "" },
  status: { textContent: "" },
  buttons: [button("low"), button("medium"), button("high")],
};

sections.updateUsbLatency(refs, {
  selected_mode: "medium",
  applied_mode: "medium",
  live_buffer_ms: 53.3,
  state: "recovery",
  detail: "Recovery buffer active; latency will fall after timing stabilizes.",
});
assert.equal(refs.selected.textContent, "Medium");
assert.equal(refs.applied.textContent, "Medium");
assert.equal(refs.live.textContent, "53.3 ms");
assert.match(refs.status.textContent, /Recovery buffer active/);
assert.equal(refs.buttons[1].el.attrs["aria-pressed"], "true");

sections.updateUsbLatency(refs, {
  selected_mode: "low",
  applied_mode: "low",
  live_buffer_ms: 53.3,
  state: "fallback",
  detail: "Low is selected, but the host timing check failed. This USB session is using the stable 53.3 ms buffer.",
});
assert.match(refs.status.textContent, /host timing check failed/);

const quietConsole = { error() {} };
const actions = await new AsyncFunction(
  "fetch", "jsonHeaders", "console",
  `${moduleBody(actionsPath)}\nreturn { setLatencyMode };`,
)(
  async () => ({
    ok: false,
    status: 502,
    async json() { return { error: "fan-in restart failed" }; },
  }),
  () => ({ "Content-Type": "application/json" }),
  quietConsole,
);

await actions.setLatencyMode({ latency: refs }, "high");
assert.match(refs.status.textContent, /Could not apply: fan-in restart failed/);
assert.ok(refs.buttons.every((item) => item.el.disabled === false));
