// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Exercise the pure backend-state → product-language projection without
// coupling this contract test to a browser DOM implementation.

import assert from "node:assert/strict";
import { buildFunction } from "./_loader.mjs";

const modulePath = process.argv[2];
if (!modulePath) {
  throw new Error("usage: node system_optional_features_test.mjs <optional-features-card.js>");
}

const { enhancedAecPresentation } = buildFunction(modulePath, {
  rewrite: [[/^import .*;\n/gm, ""], [/^export /gm, ""]],
  guardNoImports: true,
  returns: ["enhancedAecPresentation"],
})();

const states = [
  "not_installed", "installing", "installed", "stale",
  "failed", "unavailable", "not_needed",
];
for (const state of states) {
  assert.equal(
    enhancedAecPresentation({ schema_version: 1, state }).state,
    state,
    `schema state ${state} must remain distinct`,
  );
}

const absent = enhancedAecPresentation({
  schema_version: 1,
  state: "not_installed",
  engine: "v1",
  action: { enabled: true, label: "Install enhancement" },
});
assert.equal(absent.badge, "Optional");
assert.equal(absent.installable, true);
assert.equal(absent.button, "Install enhancement");
assert.match(absent.detail, /Standard echo cancellation is already working/);

const blocked = enhancedAecPresentation({
  schema_version: 1,
  state: "not_installed",
  detail: "Installation is unavailable while another maintenance job runs.",
  action: { enabled: false, label: "Install enhancement" },
});
assert.equal(blocked.installable, false);
assert.match(blocked.detail, /another maintenance job/);

const installing = enhancedAecPresentation({
  schema_version: 1,
  state: "installing",
  action: { enabled: false, label: "Installing…" },
});
assert.equal(installing.polling, true);
assert.equal(installing.installable, false);
assert.match(installing.detail, /Music, measurements, and room correction remain available/);

const stale = enhancedAecPresentation({
  schema_version: 1,
  state: "stale",
  action: { enabled: true, label: "Update enhancement" },
});
assert.equal(stale.badge, "Update available");
assert.equal(stale.installable, true);
assert.equal(stale.button, "Update enhancement");

const failed = enhancedAecPresentation({
  schema_version: 1,
  state: "failed",
  detail: "Standard echo cancellation remains active; you can retry safely.",
  last_error: "checksum mismatch",
  action: { enabled: true, label: "Retry installation" },
});
assert.equal(failed.badge, "Install failed");
assert.match(failed.detail, /Standard echo cancellation remains active/);
assert.doesNotMatch(failed.detail, /checksum mismatch/);
assert.equal(failed.button, "Retry installation");

const chip = enhancedAecPresentation({
  schema_version: 1,
  state: "not_needed",
  engine: "chip",
});
assert.equal(chip.badge, "Not needed");
assert.equal(chip.button, "");
assert.match(chip.detail, /hardware echo cancellation/);

const malformed = enhancedAecPresentation({ state: "invented" });
assert.equal(malformed.state, "unavailable");
assert.equal(malformed.installable, false);

const missingAction = enhancedAecPresentation({
  schema_version: 1,
  state: "not_installed",
});
assert.equal(missingAction.installable, false);

const futureSchema = enhancedAecPresentation({
  schema_version: 2,
  state: "not_installed",
  action: { enabled: true, label: "Install enhancement" },
});
assert.equal(futureSchema.state, "unavailable");
assert.equal(futureSchema.installable, false);

process.stdout.write(JSON.stringify({ ok: true }));
