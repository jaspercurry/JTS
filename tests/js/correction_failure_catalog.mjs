// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Print the browser's closed room-correction failure vocabulary and its
// supported envelope schema version as JSON, so
// tests/test_web_correction_setup.py can compare both against the server's
// values (jasper/correction/failures.py, jasper/correction/envelope.py) by
// VALUE. The two sides are duplicated deliberately — a malformed or
// half-deployed server must not smuggle arbitrary diagnostics into a block
// the browser treats as homeowner-safe, and an envelope at an unsupported
// schema version must not be silently rendered — which is exactly why they
// need a parity check.
//
//   node tests/js/correction_failure_catalog.mjs

import { loadEsm, repoPath } from "./_loader.mjs";

// api.js reaches for the shared CSRF header helper by absolute URL, which
// Node cannot resolve; the catalogue does not use it.
const api = await loadEsm(repoPath("deploy/assets/correction/js/api.js"), {
  stripImports: true,
  guardNoImports: true,
  prelude: "const jsonHeaders = () => ({});\n",
});

console.log(JSON.stringify({
  KNOWN_FAILURES: api.KNOWN_FAILURES,
  SUPPORTED_ENVELOPE_SCHEMA: api.SUPPORTED_ENVELOPE_SCHEMA,
}));
