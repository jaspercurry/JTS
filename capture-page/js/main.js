// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Capture-page orchestration (build step 3). Browser-only — the pure, testable
// pieces live in fragment.js / render.js / crypto.js / relay-client.js; this
// module wires them to the DOM, the microphone, and the relay.
//
// One screen, one tap (plan §5): the Start tap records passive room noise, starts
// the local sweep recording, and drops the `armed` flag the Pi polls before it
// plays the stimulus. The page and the Pi never talk directly — only through the
// relay.

import { RELAY_BASE } from "./config.js";
import { parseFragment, withinUploadCap } from "./fragment.js";
import { renderScreen } from "./render.js";
import { RelayClient } from "./relay-client.js";
import { importContentKey, encryptWav } from "./crypto.js";
import { constraintDecision, verifyRealizedConstraints } from "./constraints.js";
import { safeReturnUrl } from "./return-url.js";
import { acquireWakeLock, watchVisibilityAbort } from "./wakelock.js";
import {
  createMonoRecorder,
  delayMs,
  float32ToWavBlob,
  rmsToDbfs,
} from "./measurement-audio.js?v=20260630-1";

// The input the household picked (empty = OS default, which is usually the USB-C
// measurement mic when one is plugged into the phone). Set by the mic picker.
let selectedDeviceId = "";
let setupInputs = [];
let setupState = {
  total_positions: 5,
  calibration: { mode: "none" },
};

function setStatus(message, kind = "info") {
  const el = document.getElementById("status");
  if (el) {
    el.textContent = message;
    el.dataset.kind = kind;
  }
}

function setupValidationToken() {
  const cryptoObj = globalThis.crypto || {};
  if (typeof cryptoObj.randomUUID === "function") return cryptoObj.randomUUID();
  const random = Math.random().toString(36).slice(2);
  return `setup-${Date.now()}-${random}`;
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

async function waitForSetupValidation(ctx, token) {
  const pollMs = Math.max(100, Math.min(1000, Number(ctx.spec.progress_poll_ms) || 250));
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const status = await ctx.client.fetchPhoneStatus();
    const event = (status && status.host_event) || {};
    if (String(event.setup_token || "") === token) {
      if (event.phase === "setup_validated") return;
      if (event.phase === "setup_validation_failed") {
        throw new Error(event.error || "speaker could not validate that calibration");
      }
    }
    await delayMs(pollMs);
  }
  throw new Error("speaker did not validate the calibration before the timeout");
}

async function validateSetupBeforeContinue(ctx) {
  const calibration = setupState.calibration || {};
  if (!ctx.spec.setup_validation || calibration.mode === "none") return;
  const token = setupValidationToken();
  setStatus("Checking calibration on the speaker…", "info");
  await ctx.client.postEvent({
    setup_validate: true,
    setup_token: token,
    setup: setupState,
  });
  await waitForSetupValidation(ctx, token);
}

async function blobToBytes(blob) {
  return new Uint8Array(await blob.arrayBuffer());
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "for") node.htmlFor = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value !== false && value !== null && value !== undefined) {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(label, onClick, secondary = false) {
  return el(
    "button",
    {
      type: "button",
      class: secondary ? "cap-button cap-button--secondary" : "cap-button",
      onclick: onClick,
      text: label,
    },
  );
}

function linkButton(label, href) {
  return el("a", {
    class: "cap-button",
    href,
    text: label,
  });
}

function setScreen(screenEl, children) {
  screenEl.replaceChildren(...children);
}

function renderCaptureComplete(ctx) {
  const returnUrl = safeReturnUrl(ctx.spec);
  const children = [
    el("h1", { class: "cap-heading", text: "Measurement uploaded" }),
    el("p", {
      class: "cap-note",
      text: "Your speaker is analyzing the recording. Return to the local speaker page to continue.",
    }),
  ];
  if (returnUrl) {
    children.push(linkButton("Back to speaker", returnUrl));
  } else {
    children.push(el("p", {
      class: "cap-note",
      text: "You can now return to the speaker page on your local network.",
    }));
  }
  setScreen(ctx.screenEl, children);
}

async function enumerateAudioInputs() {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav || !nav.mediaDevices || !nav.mediaDevices.enumerateDevices) return [];
  try {
    return (await nav.mediaDevices.enumerateDevices())
      .filter((d) => d.kind === "audioinput");
  } catch {
    return [];
  }
}

async function requestMicPermissionForSetup(spec) {
  const recorder = await createMonoRecorder({
    sampleRate: spec.sample_rate_hz || 48000,
    deviceId: selectedDeviceId,
  });
  await recorder.close();
  return enumerateAudioInputs();
}

function renderIntro(screenEl, ctx) {
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Room measurement" }),
    el("ol", { class: "cap-steps" }, [
      el("li", { text: "Allow microphone access on this phone." }),
      el("li", { text: "Choose the microphone and calibration." }),
      el("li", { text: "Pick how many listening positions to measure." }),
      el("li", { text: "Stay quiet while JTS records noise and plays each sweep." }),
    ]),
    button("Continue", () => renderPermission(screenEl, ctx)),
  ]);
  setStatus("Ready to set up the phone microphone.", "info");
}

function renderPermission(screenEl, ctx) {
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Microphone permission" }),
    el("p", {
      class: "cap-note",
      text: "Your browser will ask to use the microphone. Tap Allow so JTS can list available inputs and record the sweep.",
    }),
    button("Allow microphone", async () => {
      try {
        setStatus("Opening microphone…", "info");
        const inputs = await requestMicPermissionForSetup(ctx.spec);
        renderMicChoice(screenEl, ctx, inputs);
      } catch (err) {
        setStatus(captureFailureMessage(err), "error");
      }
    }),
  ]);
}

function renderMicChoice(screenEl, ctx, inputs) {
  setupInputs = Array.isArray(inputs) ? inputs : setupInputs;
  const select = el("select", { id: "phone-mic-select" });
  select.appendChild(el("option", { value: "", text: "Automatic / phone default" }));
  for (const input of setupInputs) {
    if (!input.label) continue;
    select.appendChild(el("option", {
      value: input.deviceId,
      text: input.label,
    }));
  }
  select.value = selectedDeviceId;
  select.addEventListener("change", () => {
    selectedDeviceId = select.value;
  });
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Choose microphone" }),
    el("p", {
      class: "cap-note",
      text: "If a USB-C measurement mic is plugged into the phone, choose it here. Otherwise leave Automatic.",
    }),
    el("label", { class: "cap-field" }, [
      el("span", { text: "Microphone" }),
      select,
    ]),
    el("div", { class: "cap-actions" }, [
      button("Continue", () => renderCalibration(screenEl, ctx)),
      button("Back", () => renderPermission(screenEl, ctx), true),
    ]),
  ]);
  setStatus("Microphone permission granted.", "done");
}

function renderCalibration(screenEl, ctx) {
  const calibrationModels = Array.isArray(ctx.spec.calibration_models)
    ? ctx.spec.calibration_models.filter((model) => (
        model &&
        typeof model.key === "string" &&
        model.key &&
        typeof model.label === "string" &&
        model.label
      ))
    : [];
  const mode = el("select", { id: "calibration-mode" }, [
    el("option", { value: "none", text: "No calibration / phone built-in mic" }),
    el("option", { value: "serial", text: "Known measurement mic serial" }),
    el("option", { value: "upload", text: "Upload calibration file" }),
  ]);
  const details = el("div");
  const renderDetails = () => {
    details.replaceChildren();
    if (mode.value === "serial") {
      const serial = el("input", {
        id: "calibration-serial",
        type: "text",
        autocomplete: "off",
        placeholder: "Serial number",
      });
      const model = el("select", { id: "calibration-model" });
      for (const option of calibrationModels) {
        model.appendChild(el("option", { value: option.key, text: option.label }));
      }
      details.append(
        el("label", { class: "cap-field" }, [el("span", { text: "Mic model" }), model]),
        el("label", { class: "cap-field" }, [el("span", { text: "Serial number" }), serial]),
      );
    } else if (mode.value === "upload") {
      const file = el("input", {
        id: "calibration-file",
        type: "file",
        accept: ".txt,.cal,.frd,.csv,.omm,text/plain",
      });
      details.append(el("label", { class: "cap-field" }, [
        el("span", { text: "Calibration file" }),
        file,
      ]));
    }
  };
  mode.addEventListener("change", renderDetails);
  renderDetails();

  const saveAndContinue = async () => {
    if (mode.value === "serial") {
      const modelEl = document.getElementById("calibration-model");
      if (!modelEl || !modelEl.value) {
        setStatus("No supported measurement-mic models were provided by the speaker.", "error");
        return;
      }
      setupState.calibration = {
        mode: "serial",
        model: modelEl.value,
        serial: document.getElementById("calibration-serial").value.trim(),
      };
      if (!setupState.calibration.serial) {
        setStatus("Enter the microphone serial number.", "error");
        return;
      }
    } else if (mode.value === "upload") {
      const file = document.getElementById("calibration-file").files[0];
      if (!file) {
        setStatus("Choose a calibration file.", "error");
        return;
      }
      setupState.calibration = {
        mode: "upload",
        filename: file.name,
        content: await file.text(),
      };
    } else {
      setupState.calibration = { mode: "none" };
    }
    try {
      await validateSetupBeforeContinue(ctx);
    } catch (err) {
      setStatus(err && err.message ? String(err.message) : String(err), "error");
      return;
    }
    renderPositionCount(screenEl, ctx);
  };

  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Calibration" }),
    el("p", {
      class: "cap-note",
      text: "Calibration is applied after recording on the speaker. It is fine to continue without it when using the phone mic.",
    }),
    el("label", { class: "cap-field" }, [
      el("span", { text: "Calibration source" }),
      mode,
    ]),
    details,
    el("div", { class: "cap-actions" }, [
      button("Continue", saveAndContinue),
      button("Back", () => renderMicChoice(screenEl, ctx, setupInputs), true),
    ]),
  ]);
  setStatus("Choose the calibration that matches the microphone.", "info");
}

function renderPositionCount(screenEl, ctx) {
  const positions = el("select", { id: "position-count" }, [
    el("option", { value: "1", text: "1 position / quick check" }),
    el("option", { value: "3", text: "3 positions" }),
    el("option", { value: "5", text: "5 positions / recommended" }),
    el("option", { value: "7", text: "7 positions / large couch" }),
  ]);
  positions.value = String(setupState.total_positions || 5);
  positions.addEventListener("change", () => {
    setupState.total_positions = Number(positions.value) || 5;
  });
  setScreen(screenEl, [
    el("h1", { class: "cap-heading", text: "Listening positions" }),
    el("p", {
      class: "cap-note",
      text: "Five measurements across the listening area is the default. Move the phone roughly a head-width between positions.",
    }),
    el("label", { class: "cap-field" }, [
      el("span", { text: "Measurements" }),
      positions,
    ]),
    el("div", { class: "cap-actions" }, [
      button("Start measurement", () => onStart(ctx)),
      button("Back", () => renderCalibration(screenEl, ctx), true),
    ]),
  ]);
  setStatus("Ready to measure position 1.", "info");
}

function samplesRmsDbfs(samples) {
  if (!samples || !samples.length) return null;
  let sumSquares = 0;
  for (const sample of samples) sumSquares += sample * sample;
  return rmsToDbfs(Math.sqrt(sumSquares / samples.length));
}

async function captureAmbientNoise(recorder, spec) {
  const durationMs = Math.max(300, Math.min(2000, Number(spec.noise_floor_ms) || 800));
  setStatus("Measuring room noise — stay quiet.", "recording");
  recorder.start();
  await delayMs(durationMs);
  const samples = await recorder.stop({ timeoutMs: 5000 });
  return {
    duration_ms: durationMs,
    rms_dbfs: samplesRmsDbfs(samples),
  };
}

async function waitForSweepComplete(client, spec, isAborted) {
  const timeoutMs = Math.max(5000, Number(spec.duration_ms) || 20000);
  const pollMs = Math.max(100, Math.min(1000, Number(spec.progress_poll_ms) || 250));
  const deadline = Date.now() + timeoutMs;
  let lastPhase = "";
  while (Date.now() < deadline) {
    if (isAborted()) return;
    const status = await client.fetchPhoneStatus();
    const event = status && status.host_event || {};
    const phase = String(event.phase || "");
    if (phase && phase !== lastPhase) {
      lastPhase = phase;
      if (phase === "sweep_started") {
        setStatus("Tone is playing — stay quiet and keep the phone still.", "recording");
      } else if (phase === "sweep_complete") {
        setStatus("Tone finished — capturing the room tail.", "recording");
      }
    }
    if (phase === "sweep_complete") return;
    if (phase === "sweep_failed") {
      throw new Error(event.error || "speaker sweep failed");
    }
    await delayMs(pollMs);
  }
  throw new Error("speaker did not finish the sweep before the recording timeout");
}

// The whole capture leg, behind the single Start tap.
async function onStart(ctx) {
  const { spec, client, contentKeyB64 } = ctx;
  let recorder = null;
  let wakeLock = null;
  let disposeWatch = () => {};
  let aborted = false;

  // Abort path (step 7): if the page is backgrounded mid-capture, stop, surface
  // the failure visibly, and tell the Pi on its next relay poll — never upload
  // garbage.
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

    // Hold the screen on for the capture; if it backgrounds anyway, abort
    // visibly and let the Pi observe the failure through the relay.
    wakeLock = await acquireWakeLock();
    disposeWatch = watchVisibilityAbort(
      typeof document !== "undefined" ? document : null,
      (reason) => {
        void abort(reason);
      },
    );
    const noise = await captureAmbientNoise(recorder, spec);
    if (aborted) return;

    recorder.start();
    setStatus(
      decision.degraded
        ? `Recording at lower confidence — ${decision.reason}. Waiting for the speaker.`
        : "Recording — waiting for the speaker to start.",
      "recording",
    );

    // Drop `armed` so the Pi plays the stimulus inside our window. `degraded`
    // rides along so the Pi can mark a capability-fallback capture lower-confidence.
    await client.postEvent({
      armed: true,
      degraded: decision.degraded,
      device: captureDevice,
      noise_floor: noise,
      setup: setupState,
    });

    // Record until the Pi reports that the real sweep finished, then keep a
    // short tail. `duration_ms` is now the hard timeout, not the normal stop
    // condition.
    await waitForSweepComplete(client, spec, () => aborted);
    await delayMs(Math.max(0, Number(spec.post_roll_ms) || 700));
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

    renderCaptureComplete(ctx);
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

  const ctx = { spec, client, contentKeyB64: handle.contentKeyB64, screenEl };
  if (spec.kind === "room_sweep") {
    renderIntro(screenEl, ctx);
  } else {
    renderScreen(screenEl, spec, {
      handlers: {
        begin_capture: () => onStart(ctx),
        retry: () => onStart(ctx),
      },
    });
    void buildMicPicker(screenEl);
    setStatus("Ready. Stand at your listening position and tap Start.", "info");
  }
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
