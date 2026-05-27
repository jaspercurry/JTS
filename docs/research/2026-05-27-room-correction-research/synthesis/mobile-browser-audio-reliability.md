# Mobile Browser Audio Reliability - Synthesis

> **Status: research synthesis.** Distilled from
> [`../raw/mobile-browser-audio-reliability.md`](../raw/mobile-browser-audio-reliability.md)
> on 2026-05-27. This is not current operational truth; use it to
> guide implementation and verification work.

## Bottom Line

The browser can be a viable capture path for an opt-in
room-correction wizard, but only if JTS treats the browser as an
untrusted transport. The report's strongest conclusion is that JTS
must verify the captured signal itself, not trust `getUserMedia`
constraints, device labels, or browser promises.

The right architecture remains:

- Browser UI asks for microphone access, captures PCM, and uploads the
  recorded sweep or probe data.
- The Pi is the source of truth for deconvolution, signal-quality
  checks, correction decisions, bundle persistence, and rollback.
- Every correction bundle stores browser, constraint, device,
  calibration, and smoke-test metadata.
- A correction must be blocked, downgraded, or explicitly marked
  degraded when the capture path fails verification.

## What The Report Supports Strongly

- `echoCancellation`, `autoGainControl`, `noiseSuppression`,
  `sampleRate`, and `channelCount` are best-effort hints. JTS should
  request the cleanest path, then verify what happened.
- iOS, Android, Chrome, Safari, and Firefox differ enough that product
  support must be empirical. "Works on my phone" is not a platform
  guarantee.
- Safari cannot expose native iOS `AVAudioSession.Mode.measurement`
  to web code. A native app can do better than Safari when the web path
  is processed or unstable.
- External calibrated USB mics are the preferred user path. Built-in
  phone mics may be useful as a degraded fallback, especially for bass,
  but should not be treated as equivalent.
- WebRTC is the wrong transport for measurement because it may apply
  lossy compression and voice-processing assumptions. AudioWorklet
  plus lossless upload is the safer baseline.
- `deviceId` should not be treated as permanently stable. Use it for a
  session, persist labels and privacy-safe hashes where useful, and
  re-verify on every run.

## Device And Platform Nuance Not To Lose

The report is more nuanced than "use Safari" or "use Chrome":

- iOS Safari can capture audio, but the underlying CoreAudio mode is
  not publicly guaranteed. `echoCancellation:false` is necessary but
  not proof of measurement mode.
- Recent WebKit history matters: avoid `echoCancellation:{exact:false}`
  because the report cites a 2025 WebKit bug where that exact syntax
  fails. Prefer plain `echoCancellation:false`.
- iPhone 6s and newer hardware commonly runs at 48 kHz. Asking for
  unusual sample rates invites hidden resampling or failure.
- Android USB mic enumeration is inconsistent across OEMs. Some paths
  expose only a generic input or route a USB mic as the "main mic."
- UMIK-1 is well-known and fixed at 48 kHz. UMIK-2 is more capable but
  the report flags a miniDSP firmware caveat around non-48 kHz LF
  behavior that should be re-checked before JTS recommends higher
  sample rates.
- iMM-6C is attractive for phone use because it is cheap, calibrated,
  and USB-C, but JTS still needs real device tests on iOS and Android.
- Bluetooth headsets, AirPods, CarPlay, and in-app iOS browsers are
  hazardous capture contexts. The wizard should detect and refuse
  them when possible.

## Recommended Smoke Test Contract

The smoke test should be a first-class measurement step, not a warning
banner. It should run before the long sweep and produce a persisted
result.

Recommended pre-capture checks:

- Secure context is present.
- AudioWorklet is available.
- Browser/OS version is known enough to warn about risky combinations.
- The user has chosen a mic or the page has selected a likely external
  input after permission unlocks labels.

Recommended post-permission checks:

- Persist requested constraints, `getSettings()`, capabilities, user
  agent, AudioContext sample rate, and device label.
- Verify echo cancellation and noise suppression are false when the
  browser reports them.
- Treat `autoGainControl` as fail or warn depending on browser support;
  undefined is not the same as known-disabled.
- Confirm sample rate. 48 kHz is preferred; 44.1 kHz can be accepted
  with a resampling note; unusual rates should warn or fail.
- Detect Bluetooth or built-in fallback by label and sample rate.

Recommended signal checks:

- 1 kHz probe tone at conservative level.
- Peak headroom check; block on clipping.
- RMS sanity check relative to expected level.
- THD or harmonic distortion sanity check where feasible.
- 3 seconds of silence for noise floor and SNR.
- DC offset check.
- Dropout/glitch detection from AudioWorklet callback behavior and
  sample discontinuities.
- Pink-noise or short sweep frequency-response sanity check against the
  loaded mic calibration.
- Continuous clipping/dropout monitoring during the real sweep.

The report proposes a score model with penalties for EC/NS/AGC,
sample-rate mismatch, built-in mic, missing calibration, and individual
smoke-test failures. The exact weights are policy, but the structure is
right: a low score blocks deployment, medium score warns/downgrades,
high score may auto-apply within the selected correction strategy.

## Bundle Fields JTS Should Persist

Minimum useful schema groups:

- `client`: user agent, parsed OS/browser, secure context,
  AudioWorklet support.
- `constraints_requested`: the exact constraints JTS asked for.
- `track_settings_actual`: the browser-reported actual settings.
- `track_capabilities` and `supported_constraints`.
- `audio_context`: sample rate, base latency, state.
- `device`: raw label, normalized match, session device id/hash,
  category (`calibrated_external`, `unknown_external`, `builtin`,
  `bluetooth`).
- `calibration`: file source, filename, checksum, mic orientation and
  sensitivity if known.
- `smoke_tests`: every pass/warn/fail value, not just a final score.
- `capture`: sweep settings, expected and actual samples, callback
  count, underruns, WAV checksum.
- `decision`: deploy/warn/block and any user overrides.

This lines up with JTS's existing bundle strategy: future LLM behavior
should explain deterministic facts, not invent reasons after the fact.

## Recommended Fallback Ladder

1. External calibrated mic in supported browser.
2. Desktop bridge / REW import for power users or failed phones.
3. Native companion app when browser reliability becomes the limiting
   factor.
4. Built-in phone mic degraded mode, explicitly opt-in, with upper-band
   restrictions.
5. Manual measurement import (`.frd`, REW export, impulse WAV).

The report's important framing is that native app and REW import are
not failures of the product. They are honest escape hatches for users
whose browser stack cannot provide measurement-grade capture.

## Implementation Implications For JTS

- Extend the current browser audio path panel into a guided "mic
  confidence" panel rather than a raw device picker.
- Keep the first screen simple: detected mic, calibration status,
  capture confidence, and a single recommended next action.
- Block or downgrade correction when calibration is missing, processing
  is detected, clipping occurs, or SNR is inadequate.
- Treat built-in mode as "approximate bass/tonal correction" rather
  than a full-range path.
- Add a power-user export of the capture bundle so support/debugging can
  reproduce the browser failure.
- Use the same smoke-test schema for `/correction/` and future active
  speaker commissioning flows.

## Open Questions To Verify

- Current Safari 26.x and iOS/iPadOS behavior with USB-C UMIK-1,
  UMIK-2, and iMM-6C.
- Android Chrome behavior on current Pixel, Samsung, and a budget OEM
  phone.
- Whether UMIK-2 firmware still has the sample-rate-dependent high-pass
  issue; default to 48 kHz until proven otherwise.
- Actual JTS clock drift between browser capture and Pi playback with
  acoustic timing references.
- Whether MediaRecorder PCM/ALAC can be a safe fallback if
  AudioWorklet fails on some iOS releases.

Last synthesized: 2026-05-27
