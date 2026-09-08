// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// strictImports: an import outside `sources` must fail loudly, not vanish
// via stripImports. Driven by test_js_loader.py.
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildFunction } from "./_loader.mjs";

const dir = mkdtempSync(join(tmpdir(), "loader-strict-"));
const entry = join(dir, "entry.mjs");
writeFileSync(join(dir, "dep.mjs"), "export const x = 1;\n");
writeFileSync(entry, 'import { x } from "./dep.mjs";\nconsole.log(x);\n');

assert.throws(
  () => buildFunction(entry, { stripImports: true, strictImports: true }),
  /unhandled import \.\/dep\.mjs in .*entry\.mjs/,
);
assert.doesNotThrow(() => buildFunction(entry, { stripImports: true }));

console.log(JSON.stringify({ ok: true }));
