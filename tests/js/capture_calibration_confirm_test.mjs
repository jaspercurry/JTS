// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Unit harness for the one-tap household-mic confirm screen's pure helpers
// (capture-page/js/main.js's validDefaultSetupHint/calibrationModelLabel —
// Wave-2 CaptureSpec.default_setup_calibration, #1540). The DOM-driven
// render + fallback behavior (renderCalibrationConfirm wired into
// renderCalibration) is pinned by string assertions in
// tests/test_capture_page_js.py, matching the house convention the rest of
// main.js's UI copy/branching is tested with; this harness covers the two
// pure decision functions in isolation.
//
// Mirrors capture_plan_loop_test.mjs's strip-and-inject approach: main.js's
// module-level imports are stripped and replaced with no-op stubs (unused by
// these two functions, but required so the module evaluates at all — the
// trailing `if (typeof document !== "undefined" ...)` boot-on-load guard is
// a no-op here since this harness never sets `globalThis.document`).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const raw = readFileSync(resolve(here, "../../capture-page/js/main.js"), "utf8");
const withoutImports = raw
  .replace(
    /^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']+["'];\s*/gm,
    "",
  )
  .replace(/^import\s+[^;\n]+\s+from\s+["'][^"']+["'];\s*/gm, "")
  .replace(
    /^const PAGE_VERSION_URL = .*;$/m,
    'const PAGE_VERSION_URL = new URL("https://capture.test/version.json");',
  );
if (/^import\s/m.test(withoutImports)) {
  throw new Error("unhandled import in main.js — update the harness strip rule");
}

const injected = `
const acceptedAcknowledgement = () => null;
const createMonoRecorder = async () => { throw new Error("unused"); };
const delayMs = async () => {};
const safeReturnUrl = () => "";
const rmsToDbfs = () => -120;
const verifyRealizedConstraints = () => ({ clean: true, dirtyFlags: [] });
const constraintDecision = () => ({ action: "proceed", degraded: false, reason: "" });
const acquireWakeLock = async () => ({ release: async () => {} });
const watchVisibilityAbort = () => () => {};
const buildAmbientStatsEvent = () => ({});
const importContentKey = async () => ({});
const encryptWav = async () => ({ blob: new Uint8Array(), plaintextLen: 0, sha256: "" });
const float32ToWavBlob = () => ({ async arrayBuffer() { return new Uint8Array().buffer; } });
const withinUploadCap = () => true;
`;

const dataUrl =
  "data:text/javascript;base64," +
  Buffer.from(injected + withoutImports, "utf8").toString("base64");
const { validDefaultSetupHint, calibrationModelLabel } = await import(dataUrl);

let passed = 0;
function ok() {
  passed += 1;
}

// ---- validDefaultSetupHint ---------------------------------------------------

function testValidSerialHintIsAccepted() {
  const spec = {
    default_setup: {
      calibration: {
        mode: "serial",
        model: "minidsp_umik2",
        serial_display: "8494",
        calibration_id: "vendor:minidsp_umik2:abc123",
      },
    },
  };
  const hint = validDefaultSetupHint(spec);
  assert.ok(hint);
  assert.equal(hint.mode, "serial");
  assert.equal(hint.model, "minidsp_umik2");
  ok();
}

function testValidUploadHintIsAccepted() {
  const spec = {
    default_setup: {
      calibration: {
        mode: "upload",
        model: "other",
        serial_display: "",
        calibration_id: "content:deadbeef",
      },
    },
  };
  assert.ok(validDefaultSetupHint(spec));
  ok();
}

function testMissingDefaultSetupIsNull() {
  assert.equal(validDefaultSetupHint({}), null);
  assert.equal(validDefaultSetupHint({ default_setup: {} }), null);
  assert.equal(validDefaultSetupHint(null), null);
  ok();
}

function testUnknownModeIsRejected() {
  const spec = {
    default_setup: {
      calibration: { mode: "none", model: "", serial_display: "", calibration_id: "x" },
    },
  };
  assert.equal(validDefaultSetupHint(spec), null);
  ok();
}

function testMissingCalibrationIdIsRejected() {
  const spec = {
    default_setup: {
      calibration: { mode: "serial", model: "minidsp_umik2", serial_display: "8494", calibration_id: "" },
    },
  };
  assert.equal(validDefaultSetupHint(spec), null);
  ok();
}

// ---- calibrationModelLabel ---------------------------------------------------

function testResolvesFriendlyLabelFromSpecModels() {
  const spec = {
    calibration_models: [
      { key: "minidsp_umik2", label: "miniDSP UMIK-2", aliases: [] },
      { key: "other", label: "Other calibrated mic", aliases: [] },
    ],
  };
  assert.equal(calibrationModelLabel(spec, "minidsp_umik2"), "miniDSP UMIK-2");
  ok();
}

function testFallsBackToRawKeyWhenNotFound() {
  const spec = { calibration_models: [] };
  assert.equal(calibrationModelLabel(spec, "unknown_model_key"), "unknown_model_key");
  ok();
}

function testFallsBackToGenericLabelWhenKeyIsEmpty() {
  assert.equal(calibrationModelLabel({}, ""), "microphone");
  assert.equal(calibrationModelLabel({}, null), "microphone");
  ok();
}

const tests = [
  testValidSerialHintIsAccepted,
  testValidUploadHintIsAccepted,
  testMissingDefaultSetupIsNull,
  testUnknownModeIsRejected,
  testMissingCalibrationIdIsRejected,
  testResolvesFriendlyLabelFromSpecModels,
  testFallsBackToRawKeyWhenNotFound,
  testFallsBackToGenericLabelWhenKeyIsEmpty,
];

let failure = null;
for (const t of tests) {
  try {
    t();
  } catch (e) {
    failure = { test: t.name, error: String(e && e.stack ? e.stack : e) };
    break;
  }
}

if (failure) {
  console.error(failure.error);
  console.log(JSON.stringify({ ok: false, ...failure }));
  process.exit(1);
} else {
  console.log(JSON.stringify({ ok: true, passed }));
}
