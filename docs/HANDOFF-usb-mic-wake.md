# Handoff: Cheap USB Mic Wake/AEC Follow-Up

This is the parking-lot note for the cheap USB mic path while the
current workstream stays focused on dialing in the ReSpeaker XVF3800
AEC3 parameters. Come back here after the XVF sweep is settled.

## Current State

- The wake-corpus recorder can capture `usb_raw`, `usb_webrtc`, and
  `ref` alongside the XVF legs.
- `usb_raw` is 16 kHz mono int16, resampled from the USB mic's native
  capture rate. JTS does not apply software AGC before saving it.
- `usb_webrtc` runs the cheap USB mic through the same WebRTC AEC3
  binding/config family as the XVF `on` leg, using the shared speaker
  reference stream.
- Pilot clips suggest `usb_raw` can sound good to a human, while
  `usb_webrtc` can underperform both by ear and by wake score.

## Leading Hypotheses

1. **Delay/alignment mismatch.** The USB mic is an independent capture
   clock and queue. The bridge currently feeds USB AEC the same
   freshest `ref` frame used by the XVF path. If USB has a different
   fixed delay, jitter, or drift, AEC3 may chase the wrong echo.
2. **Mic-side nonlinear processing.** Cheap USB mics often have device
   AGC, limiting, high-pass filtering, or noise cleanup. These can make
   speech sound present while making speaker echo less like a linear
   copy of the reference.
3. **Acoustic path difference.** The USB mic may sit in a position with
   more reverberant or reflected speaker energy than the XVF array.
4. **Level mismatch / clipping margin.** Prior clips showed USB paths
   hotter than XVF paths. Near-ceiling samples can hurt both AEC and
   wake scoring.
5. **Wake-model mismatch.** Better human intelligibility is not the same
   as preserving the edge/transient pattern `jarvis_v2` currently needs.

## First Measurements

Run these on same-session clips that include `ref`, `off` or `raw0`,
`usb_raw`, and `usb_webrtc`.

- Estimate `ref` to `off` / `raw0` lag and `ref` to `usb_raw` lag with
  normalized cross-correlation and GCC-PHAT.
- Report best lag in milliseconds, correlation strength, and
  clip-to-clip stability.
- Compute band-limited coherence or correlation in speech-relevant
  bands, especially mid-band and high-band onset windows.
- Compare lag/coherence results against wake scores and residual music
  metrics for `usb_webrtc`.
- Check USB raw for near-clipping, flat-tops, and sudden envelope jumps
  that would indicate device AGC/limiter behavior.

## First Experiments

1. **Offline USB ref-delay sweep.** Replay existing `usb_raw` + `ref`
   through AEC3 with the reference shifted across a bounded range
   such as -250 ms to +250 ms. Score/listen to see whether any fixed
   offset materially improves USB AEC.
2. **Live USB ref-delay knob, only if the sweep is promising.** Add a
   corpus-only `JASPER_AEC_USB_REF_DELAY_MS` ring-buffer offset for the
   `usb_webrtc` path. Keep default at 0.
3. **Hardware processing check.** Confirm the USB mic's hardware AGC
   and capture gain state before each test session. Record the state in
   session notes or metadata if this becomes a serious tuning branch.
4. **Same-utterance comparison.** Use the corpus UI with music + far
   distance, and compare `usb_raw`, delayed `usb_webrtc`, XVF `off`,
   and the best XVF AEC candidate.

## Guardrails

- Treat USB legs as corpus-only until a USB-specific chain and wake
  model beat the current XVF path in held-out testing.
- Do not let USB experiments increase always-on production CPU/RAM cost.
- Prefer offline delay sweeps before adding live bridge complexity.
- Keep the reference capture as-is unless the measurement proves the
  reference itself is inadequate.

Last verified: 2026-05-27.
