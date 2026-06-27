// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Capture-page orchestration (build step 3). Browser-only — the pure, testable
// pieces live in fragment.js / render.js / crypto.js / relay-client.js; this
// module wires them to the DOM, the microphone, and the relay.
//
// One screen, one tap (plan §5): the Start tap BOTH starts the local recording
// AND drops the `armed` flag the Pi (polling the relay) uses to play the
// stimulus. The page and the Pi never talk directly — only through the relay.
//
// Deferred to later build steps (clearly seamed here, not yet implemented):
//   - step 6: realized-constraints verify (refuse/degrade per kind) + alignment;
//   - step 7: Screen Wake Lock during capture + visibilitychange abort-and-cue.
// Those refine onStart(); the transport happy path is what ships in step 3.

import { RELAY_BASE } from "./config.js";
import { parseFragment, recordWindowMs } from "./fragment.js";
import { renderScreen } from "./render.js";
import { RelayClient } from "./relay-client.js";
import { importContentKey, encryptWav } from "./crypto.js";
import { constraintDecision, verifyRealizedConstraints } from "./constraints.js";
import { createMonoRecorder, float32ToWavBlob } from "./measurement-audio.js";

function setStatus(message, kind = "info") {
  const el = document.getElementById("status");
  if (el) {
    el.textContent = message;
    el.dataset.kind = kind;
  }
}

async function blobToBytes(blob) {
  return new Uint8Array(await blob.arrayBuffer());
}

// The whole capture leg, behind the single Start tap.
async function onStart(ctx) {
  const { spec, client, contentKeyB64 } = ctx;
  let recorder = null;
  try {
    setStatus("Starting microphone…", "info");
    // getUserMedia must be inside this user gesture (iOS). EC/AGC/NS are forced
    // off by measurement-audio's mono constraints.
    recorder = await createMonoRecorder({ sampleRate: spec.sample_rate_hz || 48000 });

    // Measurement validity is loud (step 6, §9): verify the REALIZED constraints
    // — WebKit has historically ignored echoCancellation:false. Decide per the
    // spec's per-kind policy before wasting the user's time recording.
    const track = recorder.stream.getAudioTracks ? recorder.stream.getAudioTracks()[0] : null;
    const settings = track && track.getSettings ? track.getSettings() : {};
    const decision = constraintDecision(verifyRealizedConstraints(settings, spec), spec);
    if (decision.action === "refuse") {
      await recorder.close();
      recorder = null;
      setStatus(
        `This phone can't run a clean measurement (${decision.reason}). ` +
          "Try a different phone, or use a calibrated USB mic on the speaker.",
        "error",
      );
      return;
    }

    recorder.start();
    setStatus(
      decision.degraded
        ? `Measuring at lower confidence — ${decision.reason}. Keep the screen on.`
        : "Recording — keep the screen on and stay quiet.",
      "recording",
    );

    // Drop `armed` so the Pi plays the stimulus inside our window. `degraded`
    // rides along so the Pi can mark a capability-fallback capture lower-confidence.
    await client.postEvent({ armed: true, degraded: decision.degraded });

    // Record the full window (pre-roll + stimulus + post-roll).
    await new Promise((r) => setTimeout(r, recordWindowMs(spec)));
    const samples = await recorder.stop({ timeoutMs: 5000 });
    await recorder.close();
    recorder = null;

    setStatus("Encrypting and uploading…", "info");
    const wavBytes = await blobToBytes(
      float32ToWavBlob(samples, spec.sample_rate_hz || 48000),
    );
    const key = await importContentKey(contentKeyB64);
    const { blob, plaintextLen, sha256 } = await encryptWav(key, wavBytes);
    await client.putBlob(blob, plaintextLen, sha256);

    setStatus("Done — your speaker is analyzing the measurement.", "done");
  } catch (err) {
    if (recorder) {
      try {
        await recorder.close();
      } catch {
        /* already closed */
      }
    }
    setStatus(
      `Measurement failed: ${err && err.message ? err.message : err}. Tap Start to try again.`,
      "error",
    );
  }
}

async function boot() {
  const screenEl = document.getElementById("screen");
  let handle;
  try {
    handle = parseFragment(globalThis.location ? globalThis.location.hash : "");
  } catch (err) {
    setStatus(err.message, "error");
    return;
  }

  const client = new RelayClient({
    baseUrl: RELAY_BASE,
    sessionId: handle.sessionId,
    uploadToken: handle.uploadToken,
  });

  let spec;
  try {
    setStatus("Connecting to your speaker…", "info");
    spec = await client.fetchSpec();
  } catch (err) {
    setStatus(
      "Can't reach the measurement relay. New measurements need an internet " +
        "connection; any correction already applied to your speaker still works.",
      "error",
    );
    return;
  }

  const ctx = { spec, client, contentKeyB64: handle.contentKeyB64 };
  renderScreen(screenEl, spec, {
    handlers: {
      begin_capture: () => onStart(ctx),
      retry: () => onStart(ctx),
    },
  });
  setStatus("Ready. Stand at your listening position and tap Start.", "info");
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}

export { boot, onStart };
