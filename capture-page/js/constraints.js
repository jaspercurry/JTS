// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Realized-constraints verification + per-kind decision (phone-mic relay step 6).
//
// Measurement validity is LOUD, not silent (plan §9). Asking for
// echoCancellation/autoGainControl/noiseSuppression = false is NOT enough:
// WebKit has historically IGNORED `echoCancellation:false`, and AGC/NS silently
// left on does not corrupt the file in a way that looks wrong — it quietly
// flattens the level/spectral differences the measurement exists to find. So
// after getUserMedia we read the *realized* settings (`track.getSettings()`) and
// decide per the spec's per-kind policy:
//
//   - clean            -> proceed.
//   - clean_capture="warn"            -> proceed, labeled.
//   - clean_capture="refuse",
//       allow_capability_fallback     -> DEGRADE: proceed but labeled
//                                        lower-confidence (some iOS builds simply
//                                        cannot honor the flags — never dead-end
//                                        the iPhone, §9).
//   - clean_capture="refuse",
//       no fallback                   -> REFUSE: do not record; explain.
//
// Pure + dependency-free so it is unit-testable (tests/js/capture_constraints_test.mjs).

const PROCESSING_FLAGS = ["echoCancellation", "autoGainControl", "noiseSuppression"];

// Compare what getUserMedia actually gave us against the spec.
export function verifyRealizedConstraints(settings, spec, capturedChannelCount = null) {
  const realized = settings && typeof settings === "object" ? settings : {};
  const wanted = (spec && spec.constraints) || {};

  // Processing flags we asked to be OFF that came back ON.
  const dirtyFlags = PROCESSING_FLAGS.filter(
    (flag) => wanted[flag] === false && realized[flag] === true,
  );

  // Sample rate, when the browser reports it. Channel width is checked against
  // the recorder's normalized output when available: a USB mic may expose a
  // multi-channel source track even though createMonoRecorder deterministically
  // captures channel 0 into mono evidence.
  const wantRate = spec && spec.sample_rate_hz;
  const sampleRateOk =
    !wantRate || !realized.sampleRate || realized.sampleRate === wantRate;
  const wantChannels = (spec && spec.channels) || 1;
  const normalizedChannels =
    Number.isInteger(capturedChannelCount) && capturedChannelCount > 0
      ? capturedChannelCount
      : null;
  const checkedChannelCount = normalizedChannels || realized.channelCount;
  const channelsOk =
    !checkedChannelCount || checkedChannelCount === wantChannels;

  return {
    settings: realized,
    sourceChannelCount: realized.channelCount || null,
    capturedChannelCount: normalizedChannels,
    dirtyFlags,
    sampleRateOk,
    channelsOk,
    clean: dirtyFlags.length === 0 && sampleRateOk && channelsOk,
  };
}

function describe(realized) {
  const parts = [];
  if (realized.dirtyFlags.length) {
    parts.push(`this phone kept ${realized.dirtyFlags.join(", ")} on`);
  }
  if (!realized.sampleRateOk) parts.push("the sample rate is wrong");
  if (!realized.channelsOk) parts.push("the channel count is wrong");
  return parts.join("; ") || "the microphone is not in a clean measurement mode";
}

// Decide what to do given the realized check + the spec's per-kind validity.
// Returns { action: "proceed"|"degrade"|"refuse", degraded: bool, reason }.
export function constraintDecision(realized, spec) {
  if (realized.clean) return { action: "proceed", degraded: false, reason: "" };

  const validity = (spec && spec.validity) || {};
  const policy = validity.clean_capture || "refuse";
  const reason = describe(realized);

  if (policy === "warn") {
    return { action: "proceed", degraded: true, reason };
  }
  // policy === "refuse"
  if (validity.allow_capability_fallback) {
    // The device cannot do a clean capture — degrade gracefully + labeled,
    // never a dead-end refuse (§9). The Pi marks the result lower-confidence.
    return { action: "degrade", degraded: true, reason };
  }
  return { action: "refuse", degraded: false, reason };
}
