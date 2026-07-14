// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Behavioral harness for the browser orchestration path: a host Stop arrives
// after the phone arms, onStart returns without uploading, and its finally
// block still closes the recorder that owns the mic graph.

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

const recorder = {
  capturedChannelCount: 1,
  starts: 0,
  stops: 0,
  closes: 0,
  graphLive: true,
  stream: {
    getAudioTracks() {
      return [{
        label: "Test microphone",
        getSettings() {
          return {
            autoGainControl: false,
            channelCount: 1,
            echoCancellation: false,
            noiseSuppression: false,
            sampleRate: 48000,
          };
        },
      }];
    },
  },
  start() {
    this.starts += 1;
  },
  async stop() {
    this.stops += 1;
    return new Float32Array([0]);
  },
  async close() {
    this.closes += 1;
    this.graphLive = false;
  },
};
let uploads = 0;
const injected = `
const acceptedAcknowledgement = () => null;
const createMonoRecorder = async () => globalThis.__recorder;
const delayMs = async () => {};
const rmsToDbfs = (rms) => Number(rms) > 0 ? 20 * Math.log10(Number(rms)) : -120;
const verifyRealizedConstraints = (settings, spec, capturedChannelCount) => ({
  settings,
  sourceChannelCount: settings.channelCount || null,
  capturedChannelCount,
  dirtyFlags: [],
  sampleRateOk: true,
  channelsOk: true,
  clean: true,
});
const constraintDecision = () => ({ action: "proceed", degraded: false, reason: "" });
const acquireWakeLock = async () => ({ release: async () => { globalThis.__wakeReleased = true; } });
const watchVisibilityAbort = () => () => {};
`;
globalThis.__recorder = recorder;
globalThis.__wakeReleased = false;
const statusEl = { dataset: {}, textContent: "" };
globalThis.document = {
  getElementById() {
    return statusEl;
  },
};
const dataUrl =
  "data:text/javascript;base64," +
  Buffer.from(injected + withoutImports, "utf8").toString("base64");
const { onStart } = await import(dataUrl);

const posted = [];
await onStart({
  spec: {
    kind: "crossover_sweep",
    sample_rate_hz: 48000,
    channels: 1,
    constraints: {
      autoGainControl: false,
      echoCancellation: false,
      noiseSuppression: false,
    },
    validity: { clean_capture: "refuse" },
    run_token: "run-test",
  },
  contentKeyB64: "unused-after-stop",
  captureRefs: {},
  client: {
    async postEvent(event) {
      posted.push(event);
    },
    async fetchPhoneStatus() {
      return {
        host_event: {
          phase: "sweep_cancelled",
        },
      };
    },
    async putBlob() {
      uploads += 1;
    },
  },
});

assert.equal(posted.length, 1, "phone arms exactly once");
assert.equal(posted[0].armed, true);
assert.equal(recorder.starts, 2, "ambient and sweep recording both started");
assert.equal(recorder.closes, 1, "host Stop closes the recorder");
assert.equal(recorder.graphLive, false, "the microphone graph is no longer live");
assert.equal(globalThis.__wakeReleased, true, "host Stop releases the wake lock");
assert.equal(uploads, 0, "host Stop prevents a late upload");
assert.equal(statusEl.dataset.kind, "info", "host Stop stays expected control flow");

console.log(JSON.stringify({ ok: true, passed: 8 }));
