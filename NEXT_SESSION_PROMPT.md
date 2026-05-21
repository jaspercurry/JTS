# Prompt for next session — dual-stream OR-gate wake-word detection

Paste the section below into a fresh Claude Code session in this repo:

---

I want to build the **dual-stream OR-gate** wake-word path for the JTS speaker — option C in `docs/HANDOFF-aec.md`'s open-work-streams roadmap. This is the empirically-highest-value remaining AEC improvement based on our existing test-1 data, and the user (me, Jasper) has explicitly chosen it over the livekit-wakeword path (which we explored on 2026-05-21 and found marginal).

**Read these first to ground yourself**:
- `docs/HANDOFF-aec.md` — especially:
  - The TL;DR callouts at the top (current production state: NS=low + AGC1 enabled + MIC_GAIN_DB=6)
  - "Open work streams — 2026-05-21 roadmap" → option **C** (dual-stream wake with reference-coherence gating)
  - "Wake-rate forensic test methodology — 2026-05-20" (how we measure)
- Memory note `project_aec_wake_rate_forensic_methodology.md` (cross-leg apples-to-apples scoring)
- Memory note `feedback_aec_keep_bridge_architecture.md` (no topology rearchitect; engine-internal changes preferred)

## The empirical case for this path

From `logs/wake-rate/2026-05-20/test-1/` (aligned A/B forensic data):

| | AEC ON unique fires | Both fire | AEC OFF unique fires | Union |
|---|---|---|---|---|
| Test 1 (NS=mod, MIC_GAIN_DB=6) | 3 | 1 | 4 | **8/20 (40%)** |

vs. each single leg at 20-25% — a real +15-point gain by ORing the two streams. The two AEC legs catch *mostly disjoint* sets of Jarvises; the union captures the best of both.

Production today runs ONE stream into `jasper-voice` — the bridge's UDP output (AEC ON). The chip-direct mic (AEC OFF) is available at the chip's USB capture but isn't currently routed to wake-word detection.

## What to build

A **dual-stream wake-word listener** with reference-coherence gating:

1. **Two parallel wake-word streams.** `jasper-voice`'s wake loop ingests both:
   - AEC ON: the existing UDP socket at `127.0.0.1:9876` (no change)
   - AEC OFF: a new tap on the chip-direct mic (ch 1 with SHF_BYPASS=1, same as the bridge's input). Could be a second PortAudio capture, or a second UDP output from the bridge itself.
2. **Score both with the same wake-word model** (current production `jarvis_v2.onnx`). Each chunk gets two scores; fire if either is ≥ threshold.
3. **Reference-coherence gate to suppress music-driven false fires.** When the AEC OFF leg fires, check the AEC adaptive filter's correlation between mic and ref at that moment. If it's high (above some threshold, TBD), the trigger is likely caused by self-talk via speakers — suppress. If low, it's user speech — accept. Prior art: Sonos [US 11,769,505](https://patents.google.com/patent/US11769505), Amazon [US 12,361,942](https://patents.google.com/patent/US12361942).
4. **Conservative refractory window** between dual-stream fires to prevent double-trigger races (one leg fires, then the other 100ms later — should count as one wake).

## What I want from you in this session

**Lay out a concrete implementation plan**, not jumping straight to code. Step-by-step, breaking into commits/PRs:

1. **Investigate first** — read `jasper/voice_daemon.py` and `jasper/wake.py` to understand the current wake-word ingestion path. How does the wake loop consume the UDP mic? Is the model architecture pluggable to take a second stream?
2. **Investigate AEC adaptive filter state exposure** — read `jasper_aec3/src/aec3_binding.cpp`. Does WebRTC AEC3 expose a "near-end probability" or "echo-residual confidence" we can query per-frame? If not, what's the cheapest proxy? (ERLE estimate? Reference RMS-vs-mic RMS ratio at fire time?)
3. **Architecture proposal** — small design doc (in a markdown file or as a long PR description) covering:
   - How to deliver the AEC OFF stream to `jasper-voice` (preferred: bridge emits a second UDP stream; alternative: jasper-voice opens its own PortAudio capture)
   - Wake-loop changes (two parallel `openwakeword.Model` instances? One model on two streams?)
   - Coherence gate implementation
   - Refractory logic
   - Telemetry: per-leg fire counts in `/state`
4. **Stage the implementation** as small PRs in order:
   - Bridge emits second UDP (raw chip mic, no AEC, same 16 kHz mono format)
   - Wake loop consumes both UDP streams
   - Dual-model scoring + simple OR-gate (no coherence yet)
   - Test capture & measure: does this match the predicted ~40% wake rate on our test-1 baseline?
   - Add coherence gate
   - Measure FP rate (the prerequisite we kept deferring — capture 5-10 min of music-only audio, count false fires per leg)

Ask clarifying questions before writing code. Sweet spots to nail down before committing:
- Should the bridge emit ONE socket with both streams interleaved, or TWO sockets?
- Should the dual-stream logic live in `jasper-voice` (current) or in a new arbitration daemon?
- Does WebRTC AEC3's binding expose enough internal state for a coherence gate, or do we need to compute mic↔ref correlation ourselves at the bridge?

## Constraints (per user memory + feedback notes)

- **PR flow required**: every change goes through a feature branch + PR. No direct push to main even for one-line fixes.
- **Canonical deploy is `bash scripts/deploy-to-pi.sh`**: rsync + install.sh + restart, never hand-rolled.
- **JTS is a production speaker**: the dual-stream logic must not break wake-word recovery on failure (one stream stalling shouldn't deafen the other).
- **No topology rearchitect**: AEC bridge stays, wake-word model stays. We're adding a stream, not changing the audio pipeline.
- **Silent failure is unacceptable**: every new code path that could block wake detection MUST have an audible cue via `jasper/cues/registry.py`.

## Knobs already on the production Pi (do not change without measurement)

- `JASPER_AEC_NS_ENABLED=1`, `JASPER_AEC_NS_LEVEL=low`
- `JASPER_AEC_AGC1_ENABLED=1`, `JASPER_AEC_AGC1_TARGET_DBFS=9`, `JASPER_AEC_AGC1_MAX_GAIN_DB=18`
- `JASPER_AEC_AGC2=0` (no-op on Trixie's libwebrtc; documented as such)
- `JASPER_AEC_MIC_GAIN_DB=6` — reverted to this on 2026-05-21 after testing 0 hurt detection more than it helped
- Bridge log line on healthy startup: `engine=aec3 ns=on/low agc1=on(target=9,max=18dB) agc2=off frame=320 rate=16000`

## What I don't want to do in this session

- **Don't train a new wake-word model.** livekit-wakeword path explored 2026-05-21, parked, captured in HANDOFF-aec.md option A.
- **Don't revisit chip-AEC.** Option D in the roadmap; previously tested and rejected. Future option if dual-stream + remaining knobs aren't enough.
- **Don't refactor `jasper-voice`'s wake loop wholesale.** Surgical changes only — add a second stream, add a gate, OR detections. Keep existing architecture intact.
- **Don't propose vendoring a newer libwebrtc** (option E). Deeper AEC3 tuning is a different conversation.

When you have the architecture proposal, present it to me. I'll review before any code lands.

---

End of prompt.

## Notes for me (Jasper) before starting the new session

- This worktree should be on `main` with all the AEC PRs merged (#150, #154, #157, #158, #160, #161, #163, #167, #169). If not, `git pull` first.
- After the dual-stream work lands, this NEXT_SESSION_PROMPT.md should be deleted in the same PR (per the pattern of the previous one).
