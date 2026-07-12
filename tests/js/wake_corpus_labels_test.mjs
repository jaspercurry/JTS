// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";

const source = readFileSync(process.argv[2], "utf8");
const moduleUrl = "data:text/javascript;base64," +
  Buffer.from(source, "utf8").toString("base64");
const { createLegLabels } = await import(moduleUrl);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

for (const malformed of [undefined, null, [], "bad", 42]) {
  const { legLabel } = createLegLabels({ leg_labels: malformed });
  assert(legLabel("raw0") === "raw0", "malformed maps must fall back to leg id");
}

const configLabel = '<img src=x onerror="config()">';
const planLabel = '<svg onload="plan()">';
const runtimeLabel = '<script>runtime()</script>';
const labels = createLegLabels({
  leg_labels: { raw0: configLabel, usb_webrtc: "USB configured" },
  usb_aec3_sweep_baseline_label: "USB sweep baseline",
});
assert(labels.legLabel("raw0") === configLabel, "configured full-map lookup failed");
assert(labels.legLabel("unknown") === "unknown", "unknown leg must use raw id");
assert(labels.legLabel("raw0", {
  capture_plan: { legs: [{ token: "raw0", label: planLabel }] },
}) === planLabel, "capture-plan label must win");
assert(labels.legLabel("usb_webrtc", {
  include_aec3_sweep: true,
  aec3_sweep_source: "usb",
}) === "USB sweep baseline", "USB sweep baseline precedence failed");
labels.applyAec3SweepVariants([{ leg: "raw0", label: runtimeLabel }]);
assert(labels.legLabel("raw0") === runtimeLabel, "runtime variant mutation failed");

console.log(JSON.stringify({ ok: true }));
