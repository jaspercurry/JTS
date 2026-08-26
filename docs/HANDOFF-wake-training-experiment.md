# HANDOFF — Wake-word training experiment

> **Status: tooling shipped, training not run.** The corpus capture and
> scoring surface is built and in use; the gold-corpus recording and every
> training phase are pending. Read this before working on wake-word
> reliability, training-data collection, or wake testing methodology.
>
> The decisions behind the plan are ADRs:
> [0129](adr/0129-wake-models-are-trained-per-leg-against-the-chain-they-run-on.md)
> (per-leg models trained against the chain each leg runs on, fused by the
> OR-gate — and what was rejected to get there),
> [0130](adr/0130-nothing-gets-veto-power-upstream-of-the-wake-or-gate.md)
> (nothing gates frames upstream of the OR-gate),
> [0131](adr/0131-the-wake-training-experiment-commits-its-revert-bars-in-advance.md)
> (the ship and revert bars are fixed in advance). The May 2026 offline
> pilots that shaped the leg choices are
> [historical/wake-corpus-pilots-2026-05.md](historical/wake-corpus-pilots-2026-05.md).

## The problem

The speaker does not reliably wake on "Jarvis" from across the room (3+ m),
with or without music. Production runs the community `jarvis_v2` model:
accuracy 0.631, **recall 0.262**, 0.177 false fires/hour. Recall is the
dominant failure surface, and it is not recoverable by chain tuning — a
2026-05-20 sweep found 14 of 20 far-field utterances stuck at confidence
0.001 across every AEC config tested. See ADR-0129 for the evidence and the
literature.

Conditions that matter: **distance** (near ~1 m / mid ~2 m / far ~3–4 m, a
10×10 room corner to corner), **music state** (quiet vs JTS's own music
playing, handled by AEC), **voice** (normal to slightly quiet, slightly fast,
single speaker). Conditions that explicitly do not: whisper, yell, and
simultaneous multi-speaker wake.

**AEC maps to music, raw maps to quiet.** With music playing, the AEC leg has
something to cancel and should win; with no music it has nothing to cancel
and its artifacts are pure cost, so the chip-direct raw leg is cleanest. The
OR-gate fuses both so neither leg has to be good everywhere. (A prior version
of this doc had that mapping reversed — the methodology below exists to catch
exactly that class of error.)

## The wake legs

| Leg | Port | Source | Production? |
|---|---|---|---|
| AEC ON | `:9876` | WebRTC AEC3, BEST_A config; also the session carrier | yes |
| chip-direct | `:9877` | XVF on-chip BF+NS+AGC+HPF, no software AEC | yes |
| DTLN | `:9878` | DTLN-aec | yes |
| `raw0` | `:9879` | chip channel 2, no chip *or* software DSP | corpus only |
| `ref` | `:9880` | speaker reference | corpus only |
| `usb_raw` / `usb_webrtc` / `usb_dtln` | `:9881`/`:9882` | cheap USB mic legs | corpus only |
| `chip_aec_150` / `chip_aec_210` | `:9887`/`:9888` | XVF hardware AEC, fixed gated beams | hardware-conditional |

The chip-AEC beams graduated from corpus-only to production legs inside the
`xvf_chip_aec` input profile, which `auto` selects only when the detected XVF
mic profile has a validated chip beam plan. `raw0` is captured but never
consumed by production wake detection: it is training data for whether a
model generalizes to no-chip mic hardware, and for a possible `raw0` model if
JTS ever ships on cheaper mics. The USB legs answer the hardware-cost
question (can a $10 mic plus software AEC get close to the XVF chain?); USB
DTLN is high-resource-risk on the 1 GB Pi — select it only when the extra
comparison point is worth the neural-engine cost.

The recorder's `chip_aec_comparison_v1` profile captures hardware AEC,
software AEC3, raw, and optional USB/DTLN **on the same utterance**, feeding
the chip from outputd's direct final-output fanout. Entering the profile
restarts outputd / aec-init / bridge into a reversible corpus state; exiting
removes those overrides.

## What is built

- **Browser recorder** at `http://jts.local/wake-corpus/`
  (`jasper-wake-corpus-web`, socket-activated behind nginx): one-click
  record with spacebar hotkey, live SSE mic-level meter, 3 conditions ×
  3 distances, per-session leg selection (raw0, USB/ref, AEC3 sweep, chip-AEC
  profile), a sessions card (list / load / delete), a per-cell counts matrix,
  and per-clip playback across every WAV recorded for that clip. Corpus
  test-mode transitions apply the selected legs before session creation and
  unwind them on exit; a graceful exit does not auto-reload metadata, and
  crash recovery reattaches only when the active-session marker survives.
- **Session sidecars** carry `metadata_schema_version=2` plus an
  `audio_context` snapshot — production profile intent and runtime truth,
  XVF3800 mic identity and firmware channel state, selected-leg details from
  `jasper/wake_legs.py`, DAC/reference env, and validation-artifact status —
  so lab corpus can later be grouped by production profile. Clips carry
  `selected_legs` and the same snapshot beside `capture_health`.
- **CLIs:** `jasper-wake-enroll`, `jasper-wake-score` (scoring runner over a
  pre-recorded corpus), `jasper-wake-review` (listening-review package
  builder), plus noise capture and extraction.
- **Audit:** `bash scripts/audit-wake-corpus.sh data/enrollment_positives
  --expect-raw0` validates post-rsync session metadata, leg presence,
  condition × distance coverage, and WAV format/RMS; `--expect-leg <name>`
  extends it to any opt-in leg. It reads the shared leg registry, prints
  profile / mic / validation summaries when present, and still accepts older
  sessions without those fields.
- **Offline training helpers** live in the top-level `wake_training/`
  package, which must stay side-effect-light: importing it may not load
  models, touch audio hardware, or mutate Pi runtime state.
- **Production wake corpus:** `jasper/wake_events.py` keeps per-event WAVs in
  SQLite alongside the funnel, pruning oldest-first under a **128 MiB** audio
  cap (`DEFAULT_MAX_AUDIO_BYTES`, roughly 600 files at that cap).

`scripts/_waveform_fusion_experiment.py` generates offline AEC3+DTLN waveform
mixes across delay/weight grids to test whether any mix beats same-pair score
fusion. It is research evidence, not a production path — score/decision
fusion remains the production-shaped architecture.

## What is not built

- **Phase 0a — the offline AEC harness.** The scoring and review halves
  shipped; the `(raw_mic_pcm, ref_pcm, aec_config) → processed_pcm`
  extraction from `jasper-aec-bridge`'s engine internals — the piece that
  would let the harness reprocess raw captures through arbitrary AEC configs
  offline — has not been built.
- **Phase 0b — the gold corpus.** Tooling is ready, recording is pending. The
  May 2026 tuning pilots are useful analysis data but are **not** the clean
  training/held-out split (see the historical appendix for why). The real
  milestone remains a fresh Session A, then a separate Session B recorded as
  held-out plus hard negatives and kept held out.
- **Phase 0c — the baseline**, which needs 0a and 0b.
- **Phase 1 — training.** Nothing in `jasper/wake_models.py` is a trained
  model yet; `jarvis_v2` remains `DEFAULT_KEY`.

## The plan

| Phase | What | Gate |
|---|---|---|
| 0a | Offline harness: the AEC `process()` extraction + scoring runner | — |
| 0b | Gold corpus: Session A (training split) and Session B (held-out + hard negatives), ~85–105 positives across 3 distances × 3 conditions plus ~30–40 hard negatives | Checkpoint 1 |
| 0c | Baseline every leg + `jarvis_v2` on the gold corpus | — |
| 1a | Fork livekit-wakeword, vendor the custom-positives PR, prove the pipeline end-to-end on a throwaway phrase before spending real data on it | — |
| 1b | ~25k synthetic "Jarvis" positives (multi-speaker, tempo and speaker-blend variation), generated once and reused across all legs | Checkpoint 2 |
| 1c | Augmentation: RIR convolution (OpenSLR-28 as-is — it is already a mixed real+simulated corpus) + 5–20 dB SNR noise + leg-specific processing | Checkpoint 3 |
| 1d | Train per-leg models, plus a union arm for comparison | — |
| 1e | Score every arm on the held-out split | Checkpoint 4 + ADR-0131's bars |
| 1f | Deploy the winner behind a feature flag: registry entries in `jasper/wake_models.py`, per-leg loading in `WakeLoop` | Checkpoint 5 |
| 2 | *Optional* chain ablation (NS level, AGC1 vs AGC2, suppressor knobs) using the new models as the instrument | Only if Phase 1 leaves a recall gap |
| 3 | Pi deployment: one PR per shippable change, each with before/after metrics and a listening package; soft rollout behind the flag with per-leg funnel telemetry watched | — |

Rough cost: ~3–4 weeks of engineering, ~$50–100 of cloud compute, ~1–1.5
hours of listening across all five checkpoints.

**Scope of iteration 1** is deliberately narrow: one speaker, one recording
day's voice characteristics, one room, one chain config — the minimum viable
matched-conditions training. Iteration 2+ folds in real-usage utterances
passively collected from the production wake-events corpus, a second
household member, more rooms and mic positions, and a different chain config
if Phase 2 finds one wins.

## Methodology

1. **One lever at a time**, everything else at production defaults.
2. **The gold corpus is the fixed instrument** — not the production
   wake-events corpus (unknown conditions), not fresh per-experiment
   recordings (no comparability).
3. **Offline first, Pi last.** The Pi only sees candidates that cleared the
   offline bar.
4. **Metrics rank, ears select** (ADR-0131).
5. **Verify the instrument before trusting it** — validate synthetic
   positives, RIRs and the SNR range by listening *before* paying for
   training.
6. **Spectrograms as standard output.** Every review package ships PNG
   spectrograms beside the WAVs; the vocal-tear artifact has a visual
   signature (an `hf_CV` jump in the 3–7 kHz band) that is faster to see than
   to hear.
7. **Don't relitigate what is settled.** Reopening a rejected path takes new
   evidence — a paper, a measurement, an upstream fix.

**Checkpoints** (all async — a package is handed off, a verdict comes back):

| # | After | Listening for | Time |
|---|---|---|---|
| 1 | 0b | "this gold corpus is what I sound like at these conditions" | ~5 min |
| 2 | 1b | "the synthetic 'Jarvis' actually sounds like Jarvis" | ~15 min |
| 3 | 1c | "the augmented samples sound realistic for my room and chain" | ~10 min |
| 4 | 1e | "the new model's wins are on real speech, not artifacts" | ~20–30 min |
| 5 | 1f | final sanity before Pi deployment | ~5 min |

**Review package shape**, produced by the harness:

```
review-<phase>-<timestamp>/
  README.md                 — what to listen for; no metric claims
  pairs/<clip-id>/          — input.wav, processed.wav, spectrogram.png,
                              context.txt (condition labels, no scores)
  blind/                    — randomized filenames + rate.csv (1-5, no metrics)
  metrics-after-listening/  — don't open until done
  YOUR_VERDICT.md           — template
```

## Not doing

Rejected paths, with the rationale in ADR-0129 (per-leg training and what it
displaced) and ADR-0130 (upstream gating). In short: no more AEC tuning
sweeps; no PipeWire migration; no Silero VAD upstream of wake; no AGC2
enablement this sprint (it is a no-op in the binding today — a one-line C++
fix would activate it, but the expected gain is 1–3 fires in yell-music,
which is not a condition this product has); no whisper or yell optimization;
no openWakeWord training pipeline; no swapping `jarvis_v2` for another
community model; no multi-speaker simultaneous wake; no on-Pi training.

## Open questions

**Still open for iteration 1:**

- **Per-leg inference cost on the Pi** — three detector instances instead of
  one. Expected small (~5 % of one A76 core each, estimated) but unmeasured;
  measure in Phase 1f before committing.
- **DTLN's value once per-leg training lands.** DTLN currently fills gaps
  where AEC3 struggles, at ~75 MB of Pi RAM. If a trained AEC model closes
  those gaps, DTLN may stop earning its slot. Measure after Phase 1e.
- **LLM session audio routing** — whether the realtime session should read
  the DTLN stream rather than the AEC stream, since DTLN's learned mask does
  not produce the vocal-tear artifact a binary frame gate does. Cheap to
  test; changes the urgency of the whole plan if session quality is also
  affected.

**Deferred to iteration 2:** measuring the real deployment SNR distribution
(the current 5–20 dB augmentation range is literature-default territory, and
the existing wake-events corpus is mostly bench captures from one office, not
representative); multi-speaker coverage, added first as a regularization
probe; and real-usage wake attempts harvested from the production corpus once
the speaker is in daily use.

## Cross-references

- [`HANDOFF-aec.md`](HANDOFF-aec.md) — the AEC engine and its architecture
  decisions, the BEST_A tune, software vs chip AEC.
- [`HANDOFF-mic-quality-v2.md`](HANDOFF-mic-quality-v2.md) — the empirical
  findings record through 2026-05-23: sweep results, the `hf_CV` diagnosis,
  the triple-stream architecture, per-leg measurement methodology.
- [`HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md) — wake detection,
  the SQLite schema, per-event audio capture, funnel stages.
- [`HANDOFF-vad-experiments.md`](HANDOFF-vad-experiments.md) — the VAD A/B
  matrix and why server-side VAD is out.
- [`HANDOFF-xvf3800.md`](HANDOFF-xvf3800.md) — the chip-side mic reference:
  firmware variants, mixer state, the multi-channel firmware requirement.
- [`audio-paths.md`](audio-paths.md) — required reading before any
  audio-routing change.
- `experiments/aec3-v2-deep-tune-spike/README.md` — the BEST_A development
  log.

Last verified: 2026-08-26 (spine trim: the leg inventory, the shipped
tooling, the CLI entry points and the training-package location re-read
against `pyproject.toml`, `jasper/wake_legs.py`, `jasper/wake_models.py`,
`jasper/wake_events.py`, `wake_training/` and `scripts/audit-wake-corpus.sh`.
The training package is confirmed **top-level `wake_training/`**, not under
`jasper/`, and the production wake-event WAV cap is confirmed **128 MiB**.
`jarvis_v2` is confirmed still `DEFAULT_KEY` — no trained model is
registered. The per-PR recorder chronology and the May tuning-pilot results
moved to the historical appendix rather than being carried forward as
status.)
