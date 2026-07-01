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
import { parseFragment, recordWindowMs, withinUploadCap } from "./fragment.js";
import { renderScreen } from "./render.js";
import { RelayClient } from "./relay-client.js";
import { importContentKey, encryptWav } from "./crypto.js";
import { constraintDecision, verifyRealizedConstraints } from "./constraints.js";
import { acquireWakeLock, watchVisibilityAbort } from "./wakelock.js";
import { createMonoRecorder, float32ToWavBlob } from "./measurement-audio.js?v=20260630-1";

// The input the household picked (empty = OS default, which is usually the USB-C
// measurement mic when one is plugged into the phone). Set by the mic picker.
let selectedDeviceId = "";

function setStatus(message, kind = "info") {
  const el = document.getElementById("status");
  if (el) {
    el.textContent = message;
    el.dataset.kind = kind;
  }
}

function captureFailureMessage(err) {
  const message = err && err.message ? String(err.message) : String(err);
  if (message === "not_found") {
    return (
      "This one-time capture link has expired. Return to the speaker page " +
      "and create a new phone capture link."
    );
  }
  return `Measurement failed: ${message}. Tap Start to try again.`;
}

async function blobToBytes(blob) {
  return new Uint8Array(await blob.arrayBuffer());
}

// The whole capture leg, behind the single Start tap.
async function onStart(ctx) {
  const { spec, client, contentKeyB64 } = ctx;
  let recorder = null;
  let wakeLock = null;
  let disposeWatch = () => {};
  let aborted = false;

  // Abort path (step 7): if the page is backgrounded mid-capture, stop and tell
  // the Pi (which plays the audible cue) — never upload garbage.
  const abort = async (reason) => {
    if (aborted) return;
    aborted = true;
    setStatus(
      reason === "backgrounded"
        ? "Measurement stopped — the screen must stay on this page. Tap Start to try again."
        : `Measurement stopped — ${reason}. Tap Start to try again.`,
      "error",
    );
    try {
      await client.postEvent({ aborted: true, abort_reason: reason });
    } catch {
      /* the Pi also times out if it never hears the abort */
    }
    if (recorder) {
      try {
        await recorder.close();
      } catch {
        /* already closed */
      }
      recorder = null;
    }
  };

  try {
    setStatus("Starting microphone…", "info");
    // getUserMedia must be inside this user gesture (iOS). EC/AGC/NS are forced
    // off by measurement-audio's mono constraints.
    recorder = await createMonoRecorder({
      sampleRate: spec.sample_rate_hz || 48000,
      deviceId: selectedDeviceId,
    });

    // Measurement validity is loud (step 6, §9): verify the REALIZED constraints
    // — WebKit has historically ignored echoCancellation:false. Decide per the
    // spec's per-kind policy before wasting the user's time recording.
    const track = recorder.stream.getAudioTracks ? recorder.stream.getAudioTracks()[0] : null;
    const settings = track && track.getSettings ? track.getSettings() : {};
    const decision = constraintDecision(verifyRealizedConstraints(settings, spec), spec);
    // Which mic actually recorded (track.label is the device name, available once
    // permission is granted). The Pi uses this to decide whether a loaded vendor
    // calibration applies — the phone built-in mic ⇒ refuse it, a USB measurement
    // mic ⇒ apply it. It rides the opaque `armed` event below, not the E2E WAV.
    const captureDevice = {
      label: (track && track.label) || "",
      device_id: settings.deviceId || "",
    };
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
    // Hold the screen on for the capture; if it backgrounds anyway, abort+cue.
    wakeLock = await acquireWakeLock();
    disposeWatch = watchVisibilityAbort(
      typeof document !== "undefined" ? document : null,
      (reason) => {
        void abort(reason);
      },
    );
    setStatus(
      decision.degraded
        ? `Measuring at lower confidence — ${decision.reason}. Keep the screen on.`
        : "Recording — keep the screen on and stay quiet.",
      "recording",
    );

    // Drop `armed` so the Pi plays the stimulus inside our window. `degraded`
    // rides along so the Pi can mark a capability-fallback capture lower-confidence.
    await client.postEvent({ armed: true, degraded: decision.degraded, device: captureDevice });

    // Record the full window (pre-roll + stimulus + post-roll).
    await new Promise((r) => setTimeout(r, recordWindowMs(spec)));
    if (aborted) return;
    const samples = await recorder.stop({ timeoutMs: 5000 });
    if (aborted) return;
    await recorder.close();
    recorder = null;

    setStatus("Encrypting and uploading…", "info");
    const wavBytes = await blobToBytes(
      float32ToWavBlob(samples, spec.sample_rate_hz || 48000),
    );
    const key = await importContentKey(contentKeyB64);
    const { blob, plaintextLen, sha256 } = await encryptWav(key, wavBytes);
    // Page half of the dual size cap (§8): fail loud locally rather than after a
    // wasted upload that the Worker would 413.
    if (!withinUploadCap(blob.length, spec)) {
      setStatus(
        "This recording is too large to upload. Try a shorter measurement.",
        "error",
      );
      return;
    }
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
    if (!aborted) {
      setStatus(captureFailureMessage(err), "error");
    }
  } finally {
    disposeWatch();
    if (wakeLock) await wakeLock.release();
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
  void buildMicPicker(screenEl);
  setStatus("Ready. Stand at your listening position and tap Start.", "info");
}

// Best-effort input picker for a USB-C measurement mic plugged into the phone.
// Progressive enhancement: it appears only when the browser exposes ≥2 labeled
// audio inputs (Android Chrome typically does). It stays hidden when labels are
// gated behind mic permission (notably iOS Safari pre-permission) or there is one
// input — there the OS default is used, which is the USB mic when one is plugged
// in. Either way the actually-used device is reported in the `armed` event, so the
// Pi's device-aware calibration gate works with or without this picker.
async function buildMicPicker(beforeEl) {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav || !nav.mediaDevices || !nav.mediaDevices.enumerateDevices) return;
  let devices;
  try {
    devices = await nav.mediaDevices.enumerateDevices();
  } catch {
    return; // enumerate blocked/unsupported — fall back to the OS default input
  }
  const inputs = devices.filter((d) => d.kind === "audioinput" && d.label);
  if (inputs.length < 2) return; // nothing useful to choose; keep the OS default
  const wrap = document.createElement("label");
  wrap.className = "mic-picker";
  wrap.append("Microphone: ");
  const select = document.createElement("select");
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "Automatic (recommended)";
  select.appendChild(auto);
  for (const d of inputs) {
    const opt = document.createElement("option");
    opt.value = d.deviceId;
    opt.textContent = d.label; // browser-provided → textContent, never innerHTML
    select.appendChild(opt);
  }
  select.value = selectedDeviceId;
  select.addEventListener("change", () => {
    selectedDeviceId = select.value;
  });
  wrap.appendChild(select);
  if (beforeEl && beforeEl.parentNode) {
    beforeEl.parentNode.insertBefore(wrap, beforeEl);
  }
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}

export { boot, onStart };
