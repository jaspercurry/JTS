# JTS Calibration Wizard v2 — Unified Spec

**Source:** external research report shared with the project on 2026-05-22.
Preserved here verbatim because [`HANDOFF-mic-quality-v2.md`](../HANDOFF-mic-quality-v2.md)
references it heavily. Treat as input to product decisions, not as a finalized
plan — the handoff doc has Jasper's chosen sequencing.

---

**Supersedes:** previous "rely on XVF3800, build XMOS-focused wizard" memo.
**Driver of change:** Jasper's signal chain uses XVF3800 as mic-only with a parallel external DAC for high-fidelity playback. That breaks the hardware-AEC story because XMOS never sees a clock-synchronous loopback. Combined with the strategic priority of "works with any mic the user owns," the right architecture is **software AEC + a calibration wizard that auto-tunes it**, with hardware DSP as an optional future "accelerator" mode.

---

## What changed and why

### Previous recommendation, retracted
The previous memo recommended disabling software AEC and trusting XVF3800 hardware DSP. That was correct *given the implicit assumption that XMOS could do AEC end-to-end*, which assumed the XVF3800 was also the DAC. In Jasper's actual chain — XVF3800 as mic only, separate DAC for music quality — the XMOS chip has no reliable loopback reference, so its hardware AEC is materially weaker than the spec sheets imply. I should have caught that in the first pass and didn't.

### The product principle I underweighted
"Works with any mic" is a real differentiator, not a nice-to-have. Locking the project to a specific $80 microphone array gates adoption and reduces remix surface for the OSS community. If a software pipeline + auto-calibration wizard can get to 80% of XMOS quality, the openness premium is worth the 20% gap. Especially when the Pi 5 has CPU headroom going unused.

### What this changes architecturally
The wizard is no longer a polishing layer on top of XMOS — it's now the primary mechanism that makes the system work at all. The XMOS path becomes a toggle: "users with the XVF3800 hardware can enable hardware-accelerated AEC mode" as an opt-in down the line.

---

## The three forces that shape the design

1. **Clock drift between independent ADC and DAC clocks.** This is the Google research's key insight, and it bites harder in Jasper's setup than it would on an integrated speaker. USB mic + I2S/USB DAC on independent crystals will drift several ppm/sec. AEC3's linear filter loses convergence as drift accumulates; the residual suppressor over-engages; sibilants tear. The wizard must measure drift and either compensate it in the bridge or pick an AEC engine that tolerates it.

2. **AEC3 residual suppressor brittleness on sibilants.** The original diagnostic — hf_CV +0.286 in 3–7 kHz with identical static spectrum — points at the residual suppressor gating HF bins frame-by-frame. The RS knobs in libwebrtc 1.3 (already in Trixie) can soften this, but hand-tuning is brittle across rooms. The wizard must measure HF survival in double-talk and tune RS automatically.

3. **Generic wake-word models don't match the real pipeline.** Even with great AEC, a wake-word model trained on idealized data plus synthetic noise augmentation will underperform versus one trained on the *user's* voice through the *user's* mic and AEC. Wake-word personalization is an independent win, not a fallback for poor AEC.

The wizard addresses all three in a single onboarding session.

---

## The wizard, in three phases

Total time: 2–3 minutes end-to-end. Designed to be skippable but heavily encouraged on first run.

### Phase A — Acoustic measurement (~30 s)

Pure-listen + emit-then-listen, no user speech yet.

1. **Noise floor (5 s silent):** RMS in dBFS, presence of tonal interferers (HVAC, fridge), 3-bin spectral classification (quiet / moderate / noisy).
2. **Acoustic path (10 s):** Play exponential sine sweep (ESS) at typical TTS level. Capture, deconvolve to impulse response. Derive: round-trip delay (ms → frame units), reverb tail (T60 estimate), early-reflection density.
3. **Clock drift (15 s):** Play continuous reference tone or a series of correlated ticks while capturing. Cross-correlate every 1 s window, fit a line to delay-vs-time, report drift rate in ppm. Threshold: <5 ppm acceptable for AEC3, >5 ppm strongly favors DTLN-aec or in-bridge adaptive resampling.

Outputs of Phase A drive every subsequent decision.

### Phase B — AEC configuration (~30 s)

Driven by the Phase A measurements. No user speech required.

#### B.1: Engine selection

The wizard picks AEC3 vs. DTLN-aec automatically:

| Condition | Pick | Rationale |
|---|---|---|
| Clock drift < 5 ppm AND Pi CPU headroom < 30% | **AEC3** | AEC3 ~5% CPU; DTLN-aec 256-unit ~20%. Don't burn cycles you don't have. |
| Clock drift > 10 ppm OR pronounced non-linearity in IR | **DTLN-aec** | Neural net is robust to drift and speaker distortion; AEC3 will tear. |
| In between | **AEC3 with adaptive resampling** | Bridge resamples reference continuously based on measured delay drift. |
| User explicitly requests low-latency | **AEC3** | DTLN-aec adds ~10 ms latency from STFT framing. |

The choice is exposed in a "Detected acoustic profile" summary screen so a curious user can see why a particular engine was picked. Override toggle available.

#### B.2: AEC3 configuration (if selected)

Bridge calls `webrtc::AudioProcessingBuilder().SetEchoControlFactory(std::make_unique<EchoCanceller3Factory>(cfg)).Create()` against Trixie's already-installed libwebrtc 1.3. The knobs are *already there*; the call site exposes them. Configuration is driven by Phase A:

- `delay_agnostic_mode`: enabled if measured delay variance > 5 ms
- `extended_filter`: enabled if T60 > 250 ms
- `suppressor.use_subband_nearend_detection = true` always (cheap insurance against sibilant tearing)
- `suppressor.dominant_nearend_detection.snr_threshold`: 30 (default) → 10–15 if Phase B.3 verification fails
- `suppressor.dominant_nearend_detection.hold_duration`: 50 → 100
- `suppressor.high_bands_suppression.max_gain_during_echo = 1.0f`
- NS level: kLow (noisy room → kModerate; kHigh almost never)
- AGC: gain_controller1 with target -12 dBFS

#### B.3: AEC validation

Two short tests, no user speech:

- **ERLE measurement (5 s):** Play TTS, capture, measure echo return loss enhancement during far-end-only. Target > 25 dB. If < 15 dB: chain is broken, surface a diagnostic ("can't detect TTS playback in mic — check audio routing").
- **Sibilant survival proxy (5 s):** Play a TTS sample heavy on /s/ and /ʃ/, capture, measure hf_CV under playback. If hf_CV delta > 0.15 vs. no-playback baseline: re-tune RS knobs (lower `dominant_nearend_detection.snr_threshold`, retry once).

#### B.4: DTLN-aec configuration (if selected)

Much simpler. Pick model size:

| Pi 5 RAM headroom | Model size |
|---|---|
| > 600 MB free | 512 units (best quality) |
| 400–600 MB | 256 units (good balance) |
| < 400 MB | 128 units (degraded but real-time) |

Native TFLite via XNNPACK delegate. No further tuning needed; that's the whole point of going neural.

### Phase C — Wake-word personalization (~60–90 s)

Both AEC paths feed into the same wake-word personalization flow. This is the synthesis of Jasper's third research report on wake-word personalization.

#### C.1: Engine selection — livekit-wakeword by default

Default to **livekit-wakeword** for new installs. Published metrics: ~60× lower AUT, ~100× lower false-positive rate, 86.1% vs. 68.6% recall on the LiveKit benchmark. Same `(16, 96)` frozen embedding front-end as openWakeWord, so the runtime cost is identical and the swap is cheap.

Keep openWakeWord as a fallback toggle for users who want the larger pretrained model library (Hey Jarvis V2, etc.).

#### C.2: Tier-1 personalization — on-Pi verifier (~30 s)

The fast path. Sufficient for ~80% of the personalization win.

- **Capture (30 s of user time):** Prompt user to say "Jarvis" / "Jasper" 5–8 times in varied conditions:
  - 2× normal close
  - 2× normal medium distance
  - 2× with music playing (forces AEC active)
  - 1× faster, 1× quieter
- **Train (milliseconds):** Fit a small verifier head (logistic regression for openWakeWord; small attention head for livekit-wakeword) on the captured (16, 96) embeddings.
- **Threshold (5 s):** Measure verifier scores on captured positives and on 30 s of recent recorded ambient (treated as negatives). Set threshold for target FAR (default: <1 false trigger per 24 h, configurable).
- **Output:** Per-user `.pkl` (openWakeWord) or small `.onnx` (livekit-wakeword) on disk; activated on next wake-word window.

This is the demo-ready feature. Wizard completes here for most users.

#### C.3: Tier-2 personalization — cloud retrain (~5 min wait, optional)

The marquee feature, gated behind "Train deeper model — takes 5 minutes, sends recordings to our training server, costs nothing."

- Capture additional ~140 utterances over 3–5 minutes (longer enrollment script per Claude's wake-word research)
- Upload to Modal endpoint running livekit-wakeword's training pipeline
- ~30–45 min cloud training on RTX 4090 Community ($0.14–0.34) or Modal A10 ($0.55–0.82)
- ONNX model (<200 KB) returned, atomically swapped on Pi
- Notify user via in-app or push: "Your custom Jarvis model is ready"

Privacy-conscious users get strong Tier-1 performance without ever uploading audio. Tier-2 is opt-in.

#### C.4: Speaker ID (post-wake) — Picovoice Eagle

Per Jasper's wake-word research, Picovoice Eagle adds household-member attribution after wake-word fires. 0.18% EER, 4.5 MB model, comfortably real-time on Pi 5. Free tier sufficient. Enrolled during the wake-word capture phase using the same audio.

This isn't required for wake-word personalization to work, but it's a small additional ask (~10 s per household member) and unlocks per-user routing downstream.

---

## Key engineering decisions

### Software AEC engine: AEC3 vs. DTLN-aec

Both are viable; the wizard picks. AEC3 is the lighter default; DTLN-aec is the more forgiving fallback when the measurement-derived profile says AEC3 will struggle.

The strongest argument *for* keeping AEC3 in the wizard's toolkit: it's already integrated, the diagnostic shows the linear filter is mostly working (static spectrum is identical between AEC-on and AEC-off), and the RS knobs needed to fix sibilant tearing are already in Trixie's libwebrtc 1.3. The bridge change is small.

The strongest argument *for* DTLN-aec: in Jasper's specific drift-prone signal chain, AEC3 will be fighting a losing battle no matter how well-tuned. DTLN-aec sidesteps the entire convergence problem because there's no adaptive filter to diverge.

A wizard that picks between them at install time is strictly better than committing to one upfront.

### Wake-word engine: livekit-wakeword default, openWakeWord fallback

Per Jasper's third research report. livekit-wakeword's published numbers are materially better and the architecture is forward-compatible.

### Local vs. cloud training: both, tiered

Tier 1 on-Pi verifier ≈ 80% of the personalization win in ~30 s. Tier 2 cloud retrain ≈ the remaining 20% in ~5 min wait + ~$0.50 cost. Make both available; default Tier 1; gate Tier 2 behind explicit opt-in.

### Telemetry: log everything from day one

Per Jasper's wake-word research's "log everything" principle. Save during normal operation:
- Raw mic stream
- AEC output stream
- Playback reference stream
- Per-channel wake-word scores
- Detection labels (true/false from user feedback: tap to confirm or reject)

This is the data scaffold that makes future fusion or reference-aware models possible without re-collecting. Local-only by default; opt-in upload for federated improvement.

### Calibration cadence: more than once

The wizard isn't a one-time event. Re-run it when:
- User moves the device (manual button or accelerometer-detected)
- Detection rate degrades below threshold over a 7-day window
- New AEC engine or wake-word model is released
- Background acoustic fingerprint shifts significantly (e.g., new furniture, season change)

Surface "Re-calibrate" prominently in the dip.co companion UI.

---

## Open questions

### Q1: Clock-drift compensation in the bridge

If the wizard's Phase A measures > 5 ppm drift, and the user has selected AEC3 (e.g., for CPU reasons), the bridge needs continuous adaptive resampling of the reference to match the captured stream's effective sample rate. PipeWire's echo-cancel module is known weak on this; a custom bridge can do better.

The reference implementation is to maintain a long ring buffer for both streams, cross-correlate every ~250 ms to measure current offset, and adjust a high-quality resampler's ratio in fine increments. Soxr or SRC are both options. Realistic effort: 2–3 days of Claude Code work + audio framework debugging.

**Or:** if Phase A consistently shows > 5 ppm drift in normal Pi 5 + USB mic + I2S DAC setups, just default to DTLN-aec for those configurations and skip this work. Worth running the measurement on a few different mic/DAC combos before committing.

### Q2: Fusion across AEC and raw channels

Current dual-channel approach (run wake-word on both AEC output and raw mic, OR the scores) is the right starting point but not the endpoint. The 2025 multichannel-attention literature (cited in Jasper's wake-word research) argues for score fusion or reference-aware modeling. Defer to post-launch; the telemetry capture above keeps the door open.

### Q3: How honest can the wizard be about its own quality estimate?

After Phase B and Phase C, the wizard knows: ERLE, sibilant survival, wake-word recall on personalized model, FPPH. It could surface a single composite "voice quality score" so users know whether to expect great or merely OK performance. Risk: over-promising on the score. Suggestion: be conservative ("Voice setup complete. Performance should be good in moderate-noise environments. We'll learn and improve as you use it.") rather than precise.

### Q4: livekit-wakeword maturity

It's young (5 GitHub stars at time of writing, v0.2.1 from May 2026). If it materializes as the right primary engine over the next quarter, fine. If it stalls upstream, having openWakeWord as the fallback path means JTS isn't blocked. Don't burn the bridge.

---

## Recommended path

### Stage 1 — Disable AEC3, validate baseline (1 day)
Before building the wizard, confirm the current chain works at all without AEC3. Set `top.echo_canceller.enabled = false`, keep HPF + light NS + AGC. Re-run the 5-clip AB test. This is your floor.

### Stage 2 — Bridge upgrade for proper AEC3 configuration (1–2 days)
Plumb `SetEchoControlFactory(EchoCanceller3Factory(cfg))` against Trixie's libwebrtc 1.3. Expose RS knobs via TOML config (no UI yet). Manually re-tune for Jasper's specific environment to validate that the proposed RS settings actually fix sibilant tearing. This is your test of "if the wizard sets these knobs, will it work?"

### Stage 3 — DTLN-aec integration spike (2–3 days)
Wire DTLN-aec as a parallel option in the bridge. Run side-by-side comparison with tuned AEC3 on Jasper's clip set. Measure: hf_CV delta, ERLE, CPU usage, latency. Decide whether the wizard's "pick best engine" decision has a clear winner in typical Pi 5 + USB mic setups, or genuinely depends on conditions.

### Stage 4 — Wizard skeleton (3–5 days)
Build Phase A (acoustic measurement) and Phase B (AEC configuration) as a FastAPI + WebSocket service on the Pi. Browser-served wizard UI (mDNS discovery from a phone or laptop). No wake-word personalization yet — just AEC setup.

Ship this and use it yourself for a week. Validate that the measurements are stable, the engine selection is sensible, and the AEC configuration actually produces good audio.

### Stage 5 — Wake-word personalization (3–5 days)
Add Phase C (livekit-wakeword Tier 1 on-Pi verifier) to the wizard. Skip Tier 2 cloud retrain initially — get the local flow rock-solid first.

### Stage 6 — Cloud retrain pipeline (1 week)
Modal endpoint, S3 model artifact storage, atomic model swap on Pi. Wire it into the wizard as Tier 2.

### Stage 7 — Speaker ID + telemetry (3–5 days)
Picovoice Eagle for household attribution. Local telemetry logging. Re-calibration triggers.

**Realistic ship-to-YouTube timeline:** Stages 1–4 in ~2 weeks of focused work. That's a demo-ready video segment. Stages 5–7 over the following 2–4 weeks layer in the wake-word personalization story.

---

## What I was wrong about

Two things, explicitly:

1. **I treated XVF3800 as if it could do hardware AEC in your actual signal chain.** It can't, because you use it as a mic only and route playback through a different DAC. The previous memo's recommendation to "trust the XMOS hardware AEC" was based on a misread of your hardware. I should have caught that the first time.

2. **I underweighted "open hardware compatibility" as a strategic priority.** Locking the project to specific hardware is a real adoption cost for an OSS YouTube-driven project. A software AEC + wizard architecture that works with any USB mic is materially more valuable for community growth, even if it costs some absolute quality.

The previous memo's other observations stand (RS knobs are in Trixie's libwebrtc 1.3, the call site is the blocker, hand-tuning is brittle across rooms, DTLN-aec is the strongest ML alternative). What changes is the architectural endpoint: software-AEC-first with hardware as optional accelerator, not the other way around.

---

## What I'm uncertain about

- **Whether AEC3 with adaptive resampling can actually keep up with > 5 ppm drift.** The bridge work is more research than coding; might end up validating "just use DTLN-aec when drift is bad" as the simpler answer.
- **Whether DTLN-aec performs well enough on Jasper's specific TTS-induced echo profile.** The model was trained on telephony and teleconferencing data, not far-field smart-speaker echo. Worth a real-data spike.
- **Whether livekit-wakeword is durable enough to default to.** Two months old, small repo. If it grows, great. If it stalls, openWakeWord remains the safe fallback.
- **Whether the wizard's automated AEC3 RS-knob tuning is robust enough that Google's "brittle across environments" critique doesn't apply.** The argument is that measurement replaces guessing — but measurement quality depends on the test signals being representative of the user's actual use case. Edge environments (very small room, very large room, hardwood floor) may need additional handling.
- **Whether a per-user wake-word model trained on 5–8 utterances generalizes to the user's voice across day-to-day variation (sick, tired, just woke up).** Tier 2 cloud retrain with ~150 utterances probably solves this; Tier 1 might not. Worth empirical testing.

---

## Bottom line

Software AEC + wake-word personalization, surfaced through a single onboarding wizard that auto-measures the user's environment and configures both layers, is the right primary architecture. It respects your "any mic should work" principle, uses the Pi 5's spare compute productively, and makes per-environment brittleness a non-issue by replacing hand-tuning with measurement. Hardware DSP becomes an optional future accelerator mode rather than a required dependency. The wizard is the product moment that makes this work — and it's also the YouTube-ready demo.
