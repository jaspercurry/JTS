# Handoff: Cheap USB Mic Wake/AEC Follow-Up

This is the parking-lot note for the cheap USB mic path. The XVF AEC3
edge-family sweep is settled enough that the wake-corpus recorder can
now run USB-fed AEC3 sweep variants, but USB production work remains
separate from the main XVF wake path until the delay/alignment questions
below are answered.

**Current priority (2026-06-04): not the next active build.** The
generic USB mic path remains strategically important for open-source
accessibility and BOM reduction, but the active product path now uses
profile-first input selection: `auto` resolves to the XVF chip-AEC
profile on 6-channel XVF3800 hardware and falls back when that profile
is unavailable. Do not treat this file as an abandoned thread; treat it
as the next hardware-accessibility track after the current XVF profile
and corpus instrumentation are stable.

## Current State

- The wake-corpus recorder can capture `usb_raw`, `usb_webrtc`, and
  `ref` alongside the XVF legs. The 2026-05-29 chip-AEC comparison
  profile enables `usb_raw` and `usb_webrtc` by default so the cheap mic
  stays in the same-utterance comparison set while XVF chip AEC is under
  evaluation.
- New recorder-created AEC3 sweep sessions set
  `JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb`, so the stable
  `aec3_variant_1`-`aec3_variant_3` slots are fed from `usb_raw`
  while the XVF `on` leg remains available as the same-utterance
  reference.
- Outside sweep mode, `usb_webrtc` is the chosen USB WebRTC AEC3
  profile: edge-combo tuning with `stream_delay_ms=80`.
- The built-in USB sweep remains a pilot-only stream-delay comparison:
  when enabled, `usb_webrtc` is edge-combo WebRTC AEC3 at 40 ms, and
  the variant slots use the same tuning at 80, 120, and 160 ms.
- `usb_raw` is 16 kHz mono int16, resampled from the USB mic's native
  capture rate. JTS does not apply software AGC before saving it.
- `usb_webrtc` runs the cheap USB mic through the same WebRTC AEC3
  binding/config family as the XVF `on` leg, using the shared speaker
  reference stream. The current corpus profile is edge-combo tuning at
  an 80 ms delay hint.
- Pilot clips suggest `usb_raw` can sound good to a human, while
  `usb_webrtc` can underperform both by ear and by wake score.
- Latest same-utterance USB AEC3 + DTLN session
  (`20260528T184424Z-d205`) showed the USB stack is useful as corpus
  evidence but not ready as the main production path: `usb_webrtc` hit
  11/27, `usb_dtln` hit 2/27, and `usb_raw + usb_webrtc + usb_dtln`
  unioned to 13/27. A separate offline waveform mix of
  `usb_webrtc + usb_dtln` reached 14/27 on that session, but added only
  one clip over the full original-leg union.
- Production support for a generic USB mic is **not shipped**. The
  current bridge still imports XVF-specific profile constants for the
  primary production capture path, while the USB legs remain
  corpus-only experiment streams. Phase 2 in
  `HANDOFF-mic-fusion-architecture.md` is the intended path to make a
  USB mic a first-class production profile.

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
2. **USB AEC3 + DTLN corpus mode.** The first pass completed
   2026-05-28: `usb_raw`, `usb_webrtc` at edge-combo 80 ms,
   `usb_dtln`, `ref`, and XVF control legs in the same utterance.
   The 2026-05-29 chip-AEC comparison profile keeps `usb_raw` and
   `usb_webrtc` in the main comparison set; `usb_dtln` remains an
   optional resource-sensitive checkbox. Keep collecting USB legs in
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
  validation; the first mix result is interesting but not decisive.

Last verified: 2026-06-05 (re-read against the profile-first input
policy: USB legs remain corpus-only; pluggable USB production support
remains a deliberate Phase 2 follow-up after the XVF profile stabilizes,
not abandoned work).
