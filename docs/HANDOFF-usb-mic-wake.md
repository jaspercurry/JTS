# Cheap USB mic wake/AEC — parked follow-up

The parking-lot note for the cheap USB mic path. **Parked, not
abandoned:** a generic USB mic matters for open-source accessibility and
BOM reduction, and this is the next hardware-accessibility track once the
XVF profile and corpus instrumentation are stable. Production input
selection is profile-first — `auto` resolves to the XVF chip-AEC profile
only when the detected mic profile has a validated chip beam plan, and
falls back otherwise.

## Current state

- **USB legs are corpus-only.** Production support for a generic USB mic
  is not shipped: the bridge still imports XVF-specific profile
  constants for the primary production capture path. Phase 2 in
  [HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)
  is the intended route to a first-class USB production profile.
- The recorder captures `usb_raw`, `usb_webrtc` and `ref` alongside the
  XVF legs; the chip-AEC comparison profile enables `usb_raw` and
  `usb_webrtc` by default so the cheap mic stays in the same-utterance
  comparison set. `usb_dtln` is an optional resource-sensitive toggle.
- `usb_raw` is 16 kHz mono int16 resampled from the mic's native rate,
  with no JTS software AGC applied before saving.
- `usb_webrtc` runs the USB mic through the same WebRTC AEC3
  binding/config family as the XVF `on` leg against the shared speaker
  reference. Its production corpus tuning is edge-combo at
  `stream_delay_ms=80` (`usb_webrtc/aec3_edge_combo_80` in the bridge).
- Recorder-created AEC3 sweep sessions set
  `JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb` (`jasper/aec_sweep.py`), so
  the `aec3_variant_1..3` slots are fed from `usb_raw` while the XVF `on`
  leg stays available as the same-utterance reference. That sweep is
  pilot-only: `usb_webrtc` drops to 40 ms and the variant slots take 80 /
  120 / 160 ms.
- Pilot clips suggest `usb_raw` can sound good to a human while
  `usb_webrtc` underperforms both by ear and by wake score. The measured
  USB session numbers are in
  [historical/wake-corpus-pilots-2026-05.md](historical/wake-corpus-pilots-2026-05.md);
  the read is that the USB stack is useful corpus evidence and not close
  to a production path.

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

1. **Offline USB delay probe.** Replay existing `usb_raw` + `ref`
   through AEC3 with bounded reference offsets and stream-delay hints.
   The first quick probe suggested timing matters, but offline AEC3
   without live pre-roll/state did not clearly beat the saved live
   output, so treat it as directional evidence only.
2. **USB AEC3 + DTLN corpus mode.** Already run once (2026-05-28):
   `usb_raw`, `usb_webrtc` at edge-combo 80 ms, `usb_dtln`, `ref` and
   XVF control legs in the same utterance. Keep collecting USB legs in
   the gold corpus, but do not let USB tuning block XVF model training.
3. **Hardware processing check.** Confirm the USB mic's hardware AGC
   and capture gain state before each test session. Record the state in
   session notes or metadata if this becomes a serious tuning branch.
4. **Same-utterance comparison.** Use the corpus UI with USB/reference
   and USB DTLN enabled, and compare `usb_raw`, `usb_webrtc`,
   `usb_dtln`, XVF `off`, and the best XVF AEC candidate. Re-enable
   the USB AEC3 sweep only for bounded pilot runs, not for the main
   corpus.

## Guardrails

- Treat USB legs as corpus-only until a USB-specific chain and wake
  model beat the current XVF path in held-out testing.
- Do not let USB experiments increase always-on production CPU/RAM cost.
- Do not start by introducing a broad `MicProfile` Protocol. Add one
  concrete second profile and only factor common shape after real XVF-vs
  USB differences are visible in code.
- Do not repurpose `_usb_mic_thread` in place for production without
  preserving wake-corpus semantics; the recorder depends on the current
  `usb_*` leg meanings and ports.
- Prefer offline delay sweeps before adding live bridge complexity.
- Keep the reference capture as-is unless the measurement proves the
  reference itself is inadequate.
- Do not promote waveform-mixed USB outputs without hard-negative
  validation (ADR-0136).

Last verified: 2026-08-26 (USB leg tokens rechecked against
`jasper/wake_legs.py`; the sweep-source env var against
`jasper/aec_sweep.py`; the edge-combo-80 corpus tuning and
`_usb_mic_thread` against `jasper/cli/aec_bridge.py`. USB legs remain
corpus-only and USB production support remains a deliberate Phase 2
follow-up, not abandoned work. The 2026-05-28 session numbers moved to
historical/wake-corpus-pilots-2026-05.md, which already held them.)
