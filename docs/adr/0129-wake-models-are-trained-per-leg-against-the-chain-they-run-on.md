# ADR-0129: Wake models are trained per leg, against the chain that leg runs on

- **Date:** 2026-08-26
- **Status:** Accepted (design ratified 2026-05-25; training not yet run —
  recorded here when HANDOFF-wake-training-experiment.md was trimmed to its
  operational spine)

## Context

Production wakes on a community `jarvis_v2` model whose published recall is
0.262, and the observed wake rate matches. A 2026-05-20 sweep found 14 of 20
far-field utterances sitting at confidence 0.001 across *every* AEC config
tested — the model produces no signal at all on those, so no amount of chain
tuning can recover them.

Four weeks of AEC engine swaps, NS and AGC sweeps, VAD A/B tests and
suppressor-knob tuning preceded this. Each yielded something real and small,
and the yield curve went flat: the vocal-tear artifact in the AEC leg is
structural to single-reference linear AEC plus nonlinear post-suppression,
not a tuning failure. The one architectural change that paid — roughly +15
percentage points, the best to date — was fusing three differently-processed
microphone legs with an OR-gate.

The far-field KWS literature is unambiguous about where the remaining leverage
is. Wu et al. 2020 (Interspeech, arXiv:2005.03633) measure FRR at 1 FA/hr
rising 1.41 % at 0.25 m → 1.64 % at 1 m → **6.33 % at 3 m** on a pooled-data
CNN baseline, and show a far-field-aware model cutting the 3 m figure to
4.11 % *without* degrading close-talk (1.41 % → 1.21 %). Tang et al. 2020
(arXiv:2006.02774) is the matched-simulation companion: hybrid ISM+SRT
simulation with air absorption and frequency-dependent materials narrows the
simulated-vs-measured RIR gap by ~36 %, though measured RIRs still win when
available. (It recommends **no** real/simulated mixing ratio; an earlier
version of the plan cited it for one, which was an over-interpretation.)

## Decision

**Train one specialized wake model per production leg, each on data matched
to that leg's own deployment distribution, and fuse them with the existing
OR-gate.**

- The runtime stays openWakeWord's `Model` loader. ONNX from either
  openWakeWord or livekit-wakeword training drops in — both share the frozen
  `(16, 96)` Google speech-embedding front-end — so swapping models needs no
  Pi-side code change and `WakeWordDetector` is untouched.
- Training uses **livekit-wakeword with its unmerged custom-positives PR
  vendored**: focal loss, embedding mixup, three-phase training, checkpoint
  averaging, a Conv-Attention head, and a lockfile. openWakeWord's own
  training notebooks are broken upstream.
- Each leg's training set is augmented toward *its* signal: synthetic
  positives, RIR convolution, and noise mixing in a 5–20 dB SNR band (never
  below 0 dB — the distribution should bracket realistic deployment, not
  extend into the impossible), plus leg-specific processing — music leakage
  mixed into the raw leg, the offline AEC chain applied to the AEC leg, DTLN
  applied to the DTLN leg.
- The chain is treated as a **fixed snapshot** during training. If a later
  ablation shows a different chain wins, retraining is cheap.

## Consequences

- **Fusion stays an OR over independent legs.** Each leg scores against its
  own model; any leg over threshold triggers, under one shared refractory.
  That is what lets each leg be bad at something — AEC has something to
  cancel only when music is playing; raw is cleanest when it is not.
- Per-leg models mean **three detector instances loaded at once** instead of
  one shared model, and that cost is unmeasured. It is expected to be small,
  but it is measured before deployment, not assumed.
- The corpus captures more legs than production consumes — a truly-raw chip
  channel, optional cheap-USB legs, a reference leg. These are training and
  comparison data for questions that pay off later (does a model generalize
  to no-chip mic hardware? can a $10 USB mic plus software AEC get close?),
  not production inputs.
- **Rejected:** more AEC tuning sweeps (flat yield, structural artifact); a
  PipeWire migration (the ALSA-only policy holds; the dsnoop tap already
  gives the fan-out); swapping `jarvis_v2` for another community model
  (community models are baselines, not the answer); on-Pi training (all
  training is off-Pi; the Pi does inference only); whisper and yell
  optimization (not the use case — the problem is far-field attenuation and
  reverb, whose spectral content is ordinary speech, not the fundamentally
  different spectrum of whispered voice).
