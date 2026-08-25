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

sections.updateUsbLatency(refs, {
  selected_mode: "medium",
  applied_mode: "medium",
  effective_mode: null,
  live_buffer_ms: 42.7,
  state: "recovery",
  detail: "Recovery buffer active; latency will fall after timing stabilizes.",
});
assert.equal(refs.preference.textContent, "Medium");
assert.equal(refs.effective.textContent, "Adjusting");
assert.equal(refs.live.textContent, "42.7 ms");
assert.match(refs.status.textContent, /Recovery buffer active/);
assert.ok(refs.buttons.every((item) => item.el.attrs["aria-pressed"] === "false"));

sections.updateUsbLatency(refs, {
  selected_mode: "low",
  applied_mode: "low",
  effective_mode: "high",
  live_buffer_ms: 53.3,
  state: "fallback",
  detail: "Low is selected, but the host timing check failed. This USB session is using the stable 53.3 ms buffer.",
});
assert.match(refs.status.textContent, /host timing check failed/);
assert.equal(refs.effective.textContent, "High · stable fallback");
assert.equal(refs.buttons[0].el.attrs["aria-pressed"], "false");
assert.equal(refs.buttons[2].el.attrs["aria-pressed"], "true");

sections.updateUsbLatency(refs, {
  selected_mode: "low",
  applied_mode: "low",
  effective_mode: null,
  live_buffer_ms: 53.3,
  state: "idle",
  detail: "Low is preferred. It will be used when USB audio starts.",
});
assert.equal(refs.preference.textContent, "Low");
assert.equal(refs.effective.textContent, "Not active");
assert.ok(refs.buttons.every((item) => item.el.attrs["aria-pressed"] === "false"));

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
