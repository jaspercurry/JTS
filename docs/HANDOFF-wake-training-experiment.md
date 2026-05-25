# HANDOFF — Wake-word training experiment

> **This is the active workstream as of 2026-05-25.** Created after
> a deliberate reset following ~4 weeks of churn on AEC tuning, NS
> sweeps, VAD A/B tests, and corpus design that didn't yield product
> gains. The previous tuning paths hit diminishing returns; this doc
> captures the new path forward and the rationale for choosing it.
>
> **Read this first** before working on wake-word reliability, training
> data collection, or testing methodology. Cross-references to the
> prior investigation docs are in §11.
>
> The previous active doc, `HANDOFF-mic-quality-v2.md`, stays as the
> empirical-findings record (AEC tuning sweeps, BEST_A spike, triple-
> stream architecture, the `hf_CV` diagnosis). This doc is the
> forward-looking plan; that doc is the history.

---

## TL;DR

**The product problem.** The speaker doesn't reliably wake on "Jarvis"
when Jasper is across the room (3+ meters), with or without music
playing. Current production model `jarvis_v2` (fwartner community
model) has a published recall of 26% and observed wake rate roughly
matches that.

**The plan in one paragraph.** Capture a known-conditions gold corpus
(60 utterances: 3 distances × 2 music states × 10 reps, with raw mic
+ AEC reference signal). Build an offline test harness that processes
this corpus through any AEC config and scores it with any wake-word
model. Train per-leg specialized Jarvis models (one for raw, one for
AEC ON, one for DTLN) using `livekit-wakeword` + PR #69 (vendored),
with Piper synthetic positives + OpenSLR-28 RIR augmentation + 5-20
dB SNR noise mixing. Deploy via the existing OR-gate fusion. Validate
at every decision point with both metrics AND human listening (five
explicit checkpoints).

**What this is NOT.** Another AEC tuning sweep, a PipeWire migration,
a Silero VAD experiment, or a chase after "the right" base model.
Those have been tried; the yield curve is flat. Wake-word retraining
matched to the actual audio chain is the highest-leverage remaining
lever per the far-field KWS literature.

**What success looks like.** Wake rate ≥80% on a HELD-OUT slice of
the gold corpus at the "far + music" condition (the hardest cell),
with no FA regression on the existing wake_events production corpus,
validated by both peak scores AND Jasper's ear on representative clips.

**What failure looks like — defined in advance so we honor it.** If
ANY of the following hold after Phase 1e, the per-leg trained models
do NOT deploy and we revert to jarvis_v2 + existing OR-gate:
- Recall on the held-out gold corpus (any cell) drops below 60%
- False-positive rate on the production wake_events corpus exceeds
  0.5/hour (current baseline ~0.18/hour for jarvis_v2)
- Listening checkpoint 4 reveals that new-model wins are dominated by
  artifact-matching (e.g. model fires on TV noise that contains
  Jarvis-similar phonemes the augmentation set used)

These are pre-commitments. If we hit them, we revert and either go
back to design or accept that wake-word retraining isn't the right
lever for our setup.

**This is iteration 1 of several, not the final answer.** The scope
here is "Jasper-only, single recording day's voice characteristics,
single room, single chain config" — the minimum viable matched-
conditions training. Future iterations (v2+) are explicitly planned
to fold in:
- Real-usage utterances passively collected from production wake_events
- Multi-household-member coverage (when Brittany is available to record)
- More acoustic conditions (different rooms, different mic positions,
  different music genres at higher diversity)
- Possibly different chain configs if Phase 2 reveals one wins

Retraining cost is ~$5-15 per leg per Modal run, so iteration is cheap
once the pipeline is built. The first model is meant to materially
beat jarvis_v2; later models are meant to make that gap bigger.

---

## 1. The problem we're solving

The product is a smart speaker. The user wakes it by saying "Jarvis"
(no "Hey" prefix). The wake-word detector is openWakeWord's `Model`
loading `jarvis_v2.onnx` — a community-trained model from the
[fwartner Home Assistant wake-words collection](https://github.com/fwartner/home-assistant-wakewords-collection).
The model has published metrics:

- Accuracy: 0.631
- Recall: **0.262**
- False fires/hour: 0.177

The 26% recall is the dominant failure surface. Observed wake rate in
real use is roughly consistent: the 2026-05-20 sweep found 14 of 20
utterances stayed at confidence 0.001 across every AEC config tested
— i.e. the base model literally produces no signal on those
utterances. Tuning the AEC chain can't recover what the model can't
score.

The conditions that matter for this product:

| Axis | Values | Notes |
|---|---|---|
| Distance | Near (~1 m), Mid (~2 m), Far (~3-4 m) | Jasper's 10×10 room means corner-to-corner for "far" |
| Music state | Quiet (no music), Music playing (varied content + volume) | Music is from JTS's own speaker, handled by AEC |
| Voice condition | Normal speaking volume, slightly lower on the normal side, slightly faster than average | Single speaker (Jasper). Not whispering, not yelling. |
| Speaker count | Solo (Jasper) | Brittany later if needed |

The conditions that explicitly do NOT matter for this product:

- **Whisper detection.** Jasper won't whisper to the speaker. The
  literature on "quiet voice" is mostly irrelevant.
- **Yell / shout detection.** Same — not the use case.
- **Multi-speaker simultaneous wake.** Single household device.

---

## 2. What's been tried, and what was learned

Compressed history of the past ~4 weeks. Each item links to the
canonical record. The point of this section is to NOT relitigate
these paths.

| Workstream | Outcome | Canonical record |
|---|---|---|
| AEC engine swap (WebRTC AEC3 v1.3 → vendored v2.1 + BEST_A tune) | Shipped. Production runs BEST_A. Improvements were real but small. | [`HANDOFF-aec.md`](HANDOFF-aec.md) |
| Triple-stream OR-gate (raw + AEC + DTLN, PR #253) | Shipped 2026-05-23. Recovered ~+15 percentage points wake rate over best single leg. Best architectural change to date. | [`HANDOFF-mic-quality-v2.md`](HANDOFF-mic-quality-v2.md), [`HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md) |
| NS aggressiveness sweep (low / moderate / high) | Confirmed `kLow` ≥ `kModerate` for wake rate. Aggressive NS strips HF speech consonants the model needs. Production stays on `kLow`. | [`HANDOFF-aec.md`](HANDOFF-aec.md) "Open work streams §E" |
| AGC1 + AGC1 adaptive_digital tuning | Confirmed `kAdaptiveDigital` mode with current settings. Diminishing returns. | [`HANDOFF-aec.md`](HANDOFF-aec.md) "Tuning values" |
| OpenAI server-VAD A/B (PR #283 → #301) | Every cell tested was worse than the pre-existing default (AEC + local Silero). Reverted production. | [`HANDOFF-vad-experiments.md`](HANDOFF-vad-experiments.md) |
| AGC2 (`adaptive_digital`) | Currently no-op in the binding (`adaptive_digital.enabled` never set). One-line C++ fix would activate, but expected impact is marginal (+1-3 fires in yell-music per agent estimate). Low priority. | This doc, §8 |
| AEC3 RS knob sweeps (subband_nearend_detection, nearend_tuning) | Yield curve flat. Vocal tears in AEC ON leg are structural to single-reference linear AEC + nonlinear post-suppression. Documented in `hf_CV +0.286` finding. | [`HANDOFF-mic-quality-v2.md`](HANDOFF-mic-quality-v2.md), `experiments/aec3-v2-deep-tune-spike/README.md` |
| Wake-event corpus capture (PR 3 of telemetry series) | Shipped. SQLite + per-event WAVs per fire + near-miss. Production corpus growing. | [`HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md) |
| Phase 1 data engineering (PR #303, this branch) | Extract / enroll / noise-capture CLIs. Triple-leg aware. Tested. Merged. | This branch (PR #303) |

**The honest takeaway from the churn:** the AEC chain is roughly as
good as a single-reference linear AEC can get on this hardware
topology. The vocal tear artifact in the AEC ON leg is intrinsic to
the architecture, not a tuning failure. The triple-stream fusion is
the right architectural answer. **What's left is the wake-word model
itself — it has not been seriously trained against this specific
audio chain, and the literature is clear that's the highest-leverage
remaining lever.**

---

## 3. The reframe

Two cognitive shifts that took longer than they should have to land.

**Shift 1: the real problem is far-field, not whisper.** A lot of
prior energy went into "the silent-miss case" framed implicitly as
"the model can't detect quiet speech." This was conflating two
different physical situations:

- **Whisper** (rejected as out of scope): voice is quiet by intent.
  Spectral content is fundamentally different from normal speech
  (mostly unvoiced, no glottal source). Model needs different
  training data.
- **Far-field** (the actual problem): voice is quiet *at the mic*
  due to inverse-square attenuation + room reverb. Spectral content
  is the same as near-field, just attenuated and smeared by reverb.
  Model needs distance-aware training data — not whisper data.

Far-field is the right target. Per Wu et al. 2020 Interspeech
([arXiv:2005.03633](https://arxiv.org/abs/2005.03633)) on smart-
speaker KWS: at 1 FA/hr, FRR is 1.41% at 0.25 m → 1.64% at 1 m →
**6.33% at 3 m** with a pooled-data CNN baseline. The ~4.5× FRR
increase at 3 m is the dominant problem and is closable with proper
training augmentation: their best far-field-aware model dropped 3-m
FRR from 6.33% → 4.11% **without degrading close-talk** (it actually
improved 1.41% → 1.21%).

**Shift 2: AEC is correctly mapped to music conditions, RAW to quiet
conditions.** The AEC's job is to cancel music coming from JTS's own
speaker. So:

- **Music playing:** AEC ON has something to cancel and should be
  the more useful leg. RAW gets full music leakage on top of voice.
- **No music playing:** AEC ON has nothing to cancel; the AEC
  pipeline introduces minor processing artifacts that aren't useful.
  RAW (chip-direct, only XVF on-chip BF+NS+AGC+HPF applied) is the
  cleanest signal.
- **OR-gate fuses both** so neither leg has to be perfect across all
  conditions.

A prior version of this doc had the mapping reversed (raw for music,
AEC for quiet). Jasper caught the error. The methodology must catch
these as a matter of course — see §6.

**Shift 3: the methodology has to include human listening.** Pure
metrics have misled the project in this codebase before (multiple
instances of "the data said X, the ears said Y"). Five explicit
human-in-the-loop checkpoints are now part of every phase below. See
§7.

---

## 4. The architecture we're locking in

These are not subject to further re-debate without strong new evidence:

**Runtime: openWakeWord's `Model` loader.** Drop-in compatible with
ONNX from either openWakeWord training OR livekit-wakeword training
(both share the frozen `(16, 96)` Google speech-embedding front-end).
No Pi-side code change to swap models. The existing `WakeWordDetector`
in `jasper/wake.py` is unchanged.

**Training: livekit-wakeword + PR #69 (vendored).** Per prior research
(in this conversation): livekit-wakeword's training pipeline is
materially better engineered than openWakeWord's (uv lockfile, focal
loss + embedding mixup + 3-phase training + checkpoint averaging,
Conv-Attention head). openWakeWord's training notebooks are
[currently broken](https://github.com/dscripka/openWakeWord/issues/296)
on Colab. livekit-wakeword's [PR #69](https://github.com/livekit/livekit-wakeword/pull/69)
adds custom positive samples support but sits unmerged; we vendor it.

**Per-leg specialized models.** Three trained models, one per leg of
the existing OR-gate:

- `jarvis_jts_raw_v1.onnx` — trained on chip-direct audio (RAW leg)
- `jarvis_jts_aec_v1.onnx` — trained on AEC3-processed audio (AEC ON
  leg, current BEST_A production config)
- `jarvis_jts_dtln_v1.onnx` — trained on DTLN-aec-processed audio
  (DTLN leg)

Each leg's training distribution is matched to its deployment
distribution (the "matched-conditions training" principle from Wu
2020). The chain is treated as a fixed snapshot of current production
during training; if chain ablation later wins, retraining is cheap
(~$5-15 per leg per Modal run).

**Fusion: existing OR-gate.** The triple-stream architecture shipped
in PR #253 is unchanged. Each leg scores against its own specialized
model; any leg firing above threshold triggers the session. Shared
0.7 s refractory.

**Eval surface: gold corpus (Phase 0 deliverable).** Not the
production wake_events corpus (which has unknown conditions). 60
utterances captured deliberately with known distance + music state
labels. Each captured as paired (raw mic + AEC reference) WAVs so
any AEC config can be replayed offline. See §5 Phase 0.

---

## 5. The sequenced plan

Phase 0 → Phase 1 → Phase 2 (optional) → Phase 3. Total estimated
investment: ~3-4 weeks of focused engineering + ~$50-100 in cloud
compute + ~1-2 hours of Jasper's listening time across all
checkpoints.

### Phase −1 — Pre-foundation verifications (~45 min, do first)

Two short investigations that could change the rest of the plan and
that cost almost nothing. Both should happen BEFORE Phase 0 starts.

**−1a — LLM session audio routing check (~30 min).** Read
`jasper/voice_daemon.py` for which UDP port the realtime session
reads from. If it's the AEC stream (`:9876`) and Jasper is hearing
vocal tears, a one-line config change routes it to the DTLN stream
(`:9878`) instead. DTLN uses a learned mask not a binary frame gate,
so it doesn't produce the tear artifact. This could be a free quality
win that materially changes our priority (if wake reliability AND
session quality both need work vs just wake reliability, the urgency
of Phase 1 changes).

**−1b — Tang 2020 citation verification (~10 min). DONE 2026-05-25.**
The previous version of this doc cited [Tang et al. 2020 (arXiv:2006.02774)](https://arxiv.org/abs/2006.02774)
as justification for a "50/50 real-measured + simulated RIR mix" —
**that was an over-interpretation by an earlier research agent**.
Findings:
- Tang 2020 IS about KWS specifically (verified — title is
  "A study on more realistic room simulation for far-field keyword
  spotting"). Good citation for the general "match training to
  far-field deployment" thesis.
- Tang 2020 does **NOT** recommend any real+sim mixing ratio.
  It compares three regimes separately: measured-only (the oracle),
  various simulated-only techniques, no mixing experiments.
- Tang 2020's actual finding: hybrid ISM+SRT with air absorption +
  frequency-dependent material coefficients narrows the simulated-
  vs-measured gap by ~36%. Measured RIRs still win when available.
  Implementation: [Pyroomacoustics](https://github.com/LCAV/pyroomacoustics).
- **OpenSLR-28 is already a mixed real+sim corpus, not pure real**
  (bundles real RIRs from RWCP, REVERB challenge, AIR databases +
  simulated RIRs the SLR-28 authors generated). Treat as-is or
  filter to real subset.
- **BIRD ([arXiv:2010.09930](https://arxiv.org/abs/2010.09930))** is
  fine — 100k multichannel image-method simulated RIRs, Apache 2.0.
  Newer options exist if we want them: [FAST-RIR (arXiv:2110.04057)](https://arxiv.org/abs/2110.04057)
  generates RIRs on-the-fly via neural network (no precomputed
  download), [RIR-Mega (arXiv:2510.18917)](https://arxiv.org/pdf/2510.18917)
  is a 2025 successor.

These corrections are folded into Phase 1c below.

**Note: SNR distribution measurement (originally planned as −1b)
deferred.** The existing wake_events corpus is mostly bench captures
from Jasper's office, not representative of the deployment
distribution we care about. The augmentation SNR range (5-20 dB,
NOT below 0) is sensible-default territory based on the literature
and adjustable later if iteration 1 reveals it's wrong. Real
deployment SNR distribution becomes useful information once the
speaker is actually being used in the house.

### Phase 0 — Foundation (~1 week)

Goal: enable everything downstream.

**Phase 0a — Offline test harness (~2-3 days engineering).**
Extract the AEC chain processing into a callable Python library that
takes `(raw_mic_pcm, ref_pcm, aec_config) → processed_pcm`. The chain
currently lives in `jasper-aec-bridge` as a long-running daemon;
expose its engine internals via a `process()` function that can be
called from a test script. Reuses 100% of the existing engine code,
only changes the calling convention. Plus a scoring runner that takes
(corpus directory × chain config × wake-word model) → CSV of per-clip
peak scores + aggregate metrics.

**Critical sub-deliverable: listening review packages.** The harness
emits a `review/` directory per test run with:
- Top-3 + bottom-3 clips by metric in matched A/B pairs
- Spectrogram PNGs for each clip (catches vocal-tear visual signature
  alongside metric)
- `index.html` with embedded `<audio>` tags for in-browser listening
- A `YOUR_VERDICT.md` template Jasper fills in

This is the "metrics rank, ears select" infrastructure from §7.

**Phase 0b — Gold corpus capture (~60-75 min Jasper recording time
across two sessions, + half-day tooling).** Extend
`jasper-wake-enroll` (from PR #303) with a `--capture-ref` flag that
also taps the AEC reference signal via the `pcm.jasper_capture`
dsnoop tap. Each utterance produces 3 WAVs (raw mic + AEC ON + AEC
reference) plus the optional DTLN leg if running. Without the
reference signal, we can only test NS/AGC variations offline, not
AEC variations — so capturing it is the gate on Phase 2's chain-
ablation option.

Recording protocol — **Jasper (~60-75 min total, across two
sessions on different days):**
- 80 utterances total: 3 distances (~1 m near, ~2 m mid, ~3-4 m far)
  × 2 music states (quiet, music playing varied content/volume) ×
  ~13-14 utterances per cell
- Voice condition: normal speaking volume, slightly lower on the
  normal side, slightly faster than average
- Distance markers (tape on floor) for repeatability
- Music: defined playlist (one pop track, one classical, one podcast
  clip) consistent across music-condition takes
- Per-utterance metadata logged: timing, distance, condition, any
  notes ("dog barked", "neighbor noise")
- **Captured across TWO recording sessions on DIFFERENT DAYS.**
  Session A: 60 utterances split across all cells (the training +
  baseline corpus). Session B: 20 utterances spread across all cells
  (the held-out test corpus — NEVER touches training). Slight
  ambient variation + slight mic position shift between sessions is
  the point; we want the test corpus to be in-distribution but not
  identically-captured.

Plus **~15 min hard-negative recording in Session B**: Jasper
records similar-sounding words/phrases that should NOT trigger:
- "Travis", "service", "savings", "Charlie", "jarvey", "harvest",
  "garbage", "jealous" — single-word utterances, ~3 reps each
- Short phrases: "Travis is here", "good service", etc.
- ~30-40 negative utterances total
- Same condition variety (3 distances × 2 music states) where it's
  practical, but coverage doesn't need to be exhaustive — these are
  hard negatives, not the primary eval set

These go into training as targeted negatives alongside the synthetic
phoneme-substitution adversarial set that livekit-wakeword already
generates via CMUDict.

**No multi-speaker data in iteration 1.** Brittany isn't available
to record. Future iteration (v2) when she is — adds a regularization
probe + potentially trained samples.

Total: 80 Jasper Jarvis (60 train + 20 held-out) + ~30-40 negative
utterances. Single speaker, two sessions, ~75-90 min total recording.

**Phase 0c — Baseline measurement (~half-day).** Run the gold corpus
through the harness with the current production chain + jarvis_v2.
Produces a baseline metric table: peak score per condition × leg ×
split (train/held-out), FR rate per condition, FAR on the negative
utterances. **This is the before-state every later phase is measured
against.**

Critical: the recall baseline is computed against the HELD-OUT split
(the 20 Session B Jasper utterances) — NOT the training split.
Comparing Phase 1 results to training-split baselines would inflate
apparent gains by including data the new model has seen during
training.

**Checkpoint 1 (listening):** Jasper validates the gold corpus is
fit-for-purpose. Listens to 2-3 representative clips per condition.
~5 minutes.

### Phase 1 — Wake-word foundation (~1-2 weeks + ~$30-60 compute)

Goal: train per-leg custom Jarvis models matched to current
production chain. This is the load-bearing phase.

**Phase 1a — Fork livekit-wakeword + vendor PR #69 (~1 day).**
Mechanical:
```sh
git clone https://github.com/livekit/livekit-wakeword
cd livekit-wakeword
git remote add bnovik0v https://github.com/bnovik0v/livekit-wakeword.git
git fetch bnovik0v
git checkout -b jts-main main
git merge bnovik0v/feat/custom-positive-samples  # PR #69 head: ba25fe5
```
Validate the pipeline runs end-to-end on a throwaway phrase ("hey
snowy" or similar) before investing in real data. De-risks
infrastructure before infrastructure can burn data collection.

**Phase 1b — Generate synthetic Jarvis positives (~1 hour on a free
Colab T4).** ~25,000 Piper TTS clips saying "Jarvis":
- `--max-speakers 500` from LibriTTS-R (904 available)
- `--length-scales 0.85, 1.0, 1.15` (slight tempo variation)
- `--slerp-weights 0.2, 0.35, 0.5, 0.65, 0.8` (per-batch speaker
  blending)

Re-used across all three per-leg training runs — generation cost is
one-time.

**Checkpoint 2 (listening):** Jasper validates Piper-generated Jarvis
samples actually sound like "Jarvis" pronunciations. ~15 minutes.
Spot-check ~20 clips.

**Phase 1c — Augmentation pipeline + hard negatives (~2-3 days).**
For each leg's training set:

1. Take the 25k Piper synthetic + 60 gold-corpus training-split
   utterances for that leg
2. Augment with RIR convolution: random RIR drawn from
   [OpenSLR-28](https://www.openslr.org/28/) (~325 real + simulated
   RIRs, already a mixed corpus — use as-is) plus optionally
   [BIRD](https://arxiv.org/abs/2010.09930) (100k image-method
   simulated, just for diversity if more variation is needed).
   Per Tang 2020, the highest-leverage simulated-RIR move is
   ensuring the simulator uses hybrid ISM+SRT with air absorption
   (Pyroomacoustics does this by default) — NOT specifically a
   real+sim mixing ratio. If iteration 1 is RIR-quality-limited,
   consider switching to [FAST-RIR](https://arxiv.org/abs/2110.04057)
   (on-the-fly generative simulation, no precomputed corpus).
3. Add background noise at SNR range determined by Phase −1b
   measurement (initially planned 5-20 dB, adjust if reality
   demands wider). NOT below 0 dB — the training distribution
   should bracket realistic deployment SNRs, not extend into the
   impossible. Noise sources: MUSAN noise + music + speech subsets,
   FMA for music diversity.
4. **Leg-specific augmentation:**
   - Raw leg: 30-50% of positives mixed with music leakage at -3 to
     +12 dB SNR (simulating chip-direct music+voice)
   - AEC ON leg: 30-50% of positives processed through the offline
     AEC chain with the AEC reference being the augmentation music
     (simulating production residual signature)
   - DTLN leg: 30-50% similar, processed through DTLN offline

**Hard-negative collection — already captured in Phase 0b Session
B.** ~30-40 utterances of Jasper saying similar-sounding words that
should NOT trigger ("Travis", "service", "savings", "Charlie",
"jarvey", "harvest", short phrases). These go into training as
targeted hard negatives, in addition to the synthetic CMUDict
phoneme-substitution adversarials that livekit-wakeword already
generates (`hey libby`, `hey lidocaine`-style — but for "Jarvis").

**Out of scope: actual Jarvis utterances in media (Iron Man, tech
podcasts).** If Tony Stark clearly says "Jarvis" in a movie playing
through the speaker, the wake-word model SHOULD fire on it — that's
acoustically a true positive, and trying to train against it as a
negative would teach the model to NOT fire when it hears "Jarvis,"
which defeats the entire point. Whether the speaker should ignore
wake-fires that originate from its own playback content (rather than
from the room) is a UX problem solved at a higher layer (e.g. an
"ignore wake during media playback" toggle, or context-aware
suppression), NOT a wake-word training problem. Filed in §9 as a
future-work item if it becomes a real annoyance.

**Checkpoint 3 (listening):** Jasper validates the augmented
training data sounds realistic — does the RIR sound like his actual
room? Is the noise at a plausible SNR? Does the AEC-processed
synthetic look like deployment audio? ~10 minutes spot-check.

**Phase 1d — Train models (~3 hours each on Modal L4,
~$3-8 per run, ~$15-32 total for all four).** Four training runs:
```sh
uv run livekit-wakeword run configs/jarvis_jts_raw.yaml
uv run livekit-wakeword run configs/jarvis_jts_aec.yaml
uv run livekit-wakeword run configs/jarvis_jts_dtln.yaml
uv run livekit-wakeword run configs/jarvis_jts_union.yaml
```
Each per-leg config has:
- `target_phrases: ["jarvis"]`
- `n_samples: 25000` (synthetic baseline)
- `custom_positive_samples` with `multiplier: 50` on the relevant gold
  corpus subset (60 × 50 = 3000 augmented copies — TRAINING SPLIT
  ONLY, never the 20 held-out + never Brittany)
- `model: {model_type: conv_attention, model_size: small}`
- `steps: 50000`
- `target_fp_per_hour: 0.2` (training-time target, real calibration
  happens in Phase 1e')

The **union config** (`jarvis_jts_union.yaml`) is a control: same
synthetic baseline + all three legs' processed versions of each
training-split utterance merged into one training set. Purpose: tells
us if per-leg specialization actually beats a single-model approach,
or if the architectural complexity isn't paying for itself. Decided
in Phase 1e.

**Phase 1e — A/B evaluation against baseline (~half-day).** Score
each of the four new models against:
- The HELD-OUT gold corpus split (20 Jasper utterances never seen
  during training)
- The hard-negative utterances (Travis/service/etc — should NOT
  fire)
- The existing production wake_events corpus (FP check)

Compare per-condition recall and FA rate against the Phase 0c
baseline. Two explicit comparisons matter:
1. **Per-leg models vs jarvis_v2 baseline** (the headline result)
2. **Per-leg models vs union model** (architectural question:
   specialization vs single-model)

**Phase 1e' — Real-audio calibration (~half-day).** Take whichever
model(s) Phase 1e identifies as candidates. Run them against
representative real wake_events audio (last 7 days of production
captures including TV/podcast/conversation negatives). Re-pick the
threshold to achieve target FP/hr on REAL audio, not training
synthetic. The training-time `target_fp_per_hour: 0.2` is calibrated
on the augmented synthetic distribution; the deployment threshold
needs to be calibrated on the real distribution. Discrepancies of
2-5× between training and deployment FP rates are common.

**Checkpoint 4 (listening):** Jasper listens to:
- Clips where new model fires but jarvis_v2 didn't — was it really
  his voice or an artifact?
- Clips where jarvis_v2 fired but new model didn't — what got lost?
- Hard-negative clips that fired (false positives) — what tripped
  them?
- Spectrograms for any condition where the metric improvement is
  large enough to suspect overfitting
~20-30 minutes.

**Phase 1f — Deploy as 3-leg fusion (~1 day).** Subject to the
"what failure looks like" pre-commitments in the TL;DR — if any
were tripped, REVERT to jarvis_v2 instead of deploying.

If clear to deploy: update the wake-model registry in
`jasper/wake_models.py` with the winning entries (either three per-leg
models OR one union model, decided in Phase 1e). Add per-leg model
config (probably as env vars `JASPER_WAKE_MODEL_RAW`,
`JASPER_WAKE_MODEL_AEC`, `JASPER_WAKE_MODEL_DTLN`, or just
`JASPER_WAKE_MODEL` if union won). Update the existing wake loop in
`jasper.voice_daemon.WakeLoop` to load the per-leg models. Ship as a
PR behind a feature flag.

**Checkpoint 5 (pre-deploy listening):** Final sanity. ~5 minutes.

### Phase 2 (OPTIONAL) — Chain ablation

Only if Phase 1 left meaningful gaps in specific conditions (e.g.
"new models still miss far+music at 50% recall"). Uses the new strong
per-leg models as the measurement instrument. Lever sequence:

1. NS level (`kOff` vs `kLow` vs `kModerate`)
2. AGC1 vs (AGC2 adaptive_digital after the 1-line binding fix) vs none
3. Suppressor knobs within BEST_A (`dominant_nearend_detection.snr_threshold`,
   `subband_nearend_detection`, `nearend_tuning.*`)

Each lever: hold all others at the production baseline + the new
per-leg models. Vary one parameter, score on gold corpus, listen to
representative clips (Checkpoint at each lever-decision).

**This phase is gated on Phase 1 not closing the recall gap.** If the
per-leg trained models hit ~80%+ at far+music, skip Phase 2 entirely.

### Phase 3 — Pi deployment + real-world validation (~1 week)

- Single PR per shippable change
- Each PR includes: before/after metrics on the wake_events corpus,
  before/after listening review package
- Soft rollout: feature flag enabled for one week, monitor
  `wake_events` funnel queries (recall, FPPH, `ts_speech_detected`-
  null proxy for false positives)
- Cue / dashboard surfaces telemetry per-leg so regressions are
  caught fast

---

## 6. Methodology principles

These are operational rules for every experiment, derived from prior
mistakes.

1. **One lever at a time.** When testing a config knob, hold all
   others at sensible production defaults. Composing-everything-with-
   everything produces unintelligible results.

2. **Gold corpus is the canonical eval set.** Not the production
   wake_events corpus (unknown conditions). Not new ad-hoc recordings
   per experiment (lose comparability). The gold corpus is the fixed
   measurement instrument.

3. **Offline first, Pi last.** Every config change is scored on the
   gold corpus offline first. The Pi gets only final candidates that
   already cleared the offline bar. Iteration cycle: ~minutes
   offline vs ~hours on Pi.

4. **Metrics rank, ears select.** Metrics narrow 50 candidates to 5.
   Human listening picks the winner among the 5. No deployment of a
   "metric wins but sounds worse" outcome.

5. **Verify the test instrument before trusting it.** Before any
   training run, validate that the synthetic + augmented data
   actually looks/sounds like what the model will see in deployment
   (Checkpoint 2 + 3). The Jarvis-from-Piper might sound wrong; the
   RIR might be unrealistic; the augmented SNR range might be off.
   Catch this before paying for training.

6. **Spectrograms as standard output.** Every audio review package
   includes spectrograms alongside WAV files. The vocal-tear pattern,
   for example, has a visual signature (`hf_CV` jump in the 3-7 kHz
   band) that's faster to spot than to hear. The harness produces
   PNG spectrograms for every review clip automatically.

7. **Don't relitigate what's already settled.** §2 is the "we tried
   this, here's the answer" list. Adding to it is fine; reopening
   items requires strong new evidence (a paper, a measurement, a
   community fix).

8. **Date every load-bearing claim.** Per AGENTS.md "Documentation
   paradigm". Stale claims rot silently. Re-verify when picking up
   the workstream after a gap.

---

## 7. Human-in-the-loop checkpoints

The five explicit listening / approval points referenced from §5.

| # | Where | What you're listening for | Time |
|---|---|---|---|
| 1 | Phase 0b end | "Yes, this gold corpus is what I sound like at these conditions" | ~5 min |
| 2 | Phase 1b end | "Does Piper-generated 'Jarvis' actually sound like Jarvis?" | ~15 min |
| 3 | Phase 1c end | "Do the augmented training samples sound realistic for my room + chain?" | ~10 min |
| 4 | Phase 1e end | "Are the new model's wins on real speech, not artifacts?" | ~20-30 min |
| 5 | Phase 1f end | Final sanity before Pi deployment | ~5 min |

Total Jasper time across all checkpoints: ~1-1.5 hours over the full
project. All checkpoints are async (review package handed off,
verdict returned at convenience) so they're not blocking dependencies
on long experiments.

**Review package format** (produced automatically by the harness):
```
review-<phase>-<timestamp>/
  README.md                 — what to listen for; no metric claims
  pairs/
    <clip-id>/
      input.wav             — original from gold corpus
      processed.wav         — what the model received
      spectrogram.png       — visual of processed audio
      context.txt           — condition labels (no scores)
  blind/                    — randomized filenames, no labels
    audio_xyz.wav
    rate.csv                — score 1-5 without seeing metric
  metrics.csv               — hidden behind a `metrics-after-listening/`
                              subdirectory; don't open until done
  YOUR_VERDICT.md           — template
```

The blind subdirectory exists for any "is X meaningfully better than
Y?" decision where confirmation bias would inflate the answer.

---

## 8. What we explicitly are NOT doing

A list of paths considered and rejected, with rationale. Adding to
this list is fine; removing items requires evidence.

- **No more AEC tuning sweeps.** The tunable surface is mostly
  explored (per the BEST_A spike + the agent research summarized in
  §2). The vocal tear artifact in the AEC ON leg is structural to
  single-reference linear AEC. The OR-gate is the architectural
  answer; further per-knob tuning has flat yield.
- **No PipeWire migration.** AGENTS.md "Acoustic echo cancellation"
  is explicit about the ALSA-only policy. The dsnoop tap already
  provides the multi-reader fan-out PipeWire would give. Doesn't
  apply to JTS.
- **No Silero VAD upstream of wake.** Two reasons, primary first:
  1. **It would give VAD veto power over the entire OR-gate.**
     Adding a VAD upstream changes the fusion rule from
     `WW_raw ∨ WW_aec ∨ WW_dtln` to
     `VAD ∧ (WW_raw ∨ WW_aec ∨ WW_dtln)`. If VAD misses a frame
     of real speech (which Silero does, especially in music
     conditions), NO leg can recover. The OR-gate was designed
     specifically so each leg compensates for the others' failures;
     a VAD prefix re-introduces a single-point recall bottleneck.
  2. Silero V5 removed the AANL layer; documented community
     regressions on quiet/distant speech (which is the SAME PHYSICS
     as far-field, even though Jasper isn't whispering). The two
     failure modes compound the primary reason.
  The existing downstream Silero in `voice_daemon` (sustained-
  speech gate) stays — it's gating turn-opening, not wake-firing.
- **No AGC2 enablement (in current sprint).** Currently no-op
  because `adaptive_digital.enabled` isn't set in the binding. Could
  be fixed in 1 line of C++, but expected impact is +1-3 fires in
  yell-music (not a Jasper-relevant condition). Tagged as low-
  priority backlog.
- **No whisper-detection optimization.** Jasper won't whisper.
- **No yell-detection optimization.** Jasper won't yell.
- **No openWakeWord training pipeline use.** Notebooks are broken
  upstream; community forks exist but are less mature than livekit-
  wakeword's pipeline.
- **No replacement of jarvis_v2 with another community wake-word
  model.** We train our own; community models are baselines.
- **No multi-speaker simultaneous-wake handling.** Solo household.
- **No real-time on-Pi training.** All training off-Pi on Modal /
  laptop GPU. Pi inference only.

---

## 9. Open questions and decisions pending

Recorded so they don't get lost. Add to this list as they come up.

**Resolved by being promoted to a planned phase:**
- ~~LLM session audio routing.~~ Promoted to Phase −1a.
- ~~Tang 2020 citation verification.~~ Promoted to Phase −1b.
- ~~Hard-negative collection.~~ Promoted to Phase 0b Session B
  (Jasper-recorded similar-sounding words).
- ~~Per-leg vs union training.~~ Promoted to Phase 1d as 4th
  training arm + explicit comparison in Phase 1e.

**Deferred to iteration 2 (after v1 ships):**
- **SNR distribution measurement.** Once the speaker is actually in
  daily use, the wake_events corpus will reflect real deployment
  conditions. Re-measure SNR at the mic and re-tune augmentation
  range if iteration 1's 5-20 dB range turns out wrong.
- **Multi-speaker coverage (Brittany).** When she's available to
  record, add her as a regularization probe first, then training
  data if iteration 1 showed Jasper-specific overfit.
- **Real-usage utterances from passive collection.** Iteration 1
  uses deliberate-recording gold corpus only. Once the speaker is
  in use, the wake_events corpus contains real wake attempts —
  these become high-value additional training data (subject to
  manual labeling for true positives).

**Still open for iteration 1:**
- **Per-leg model inference cost on Pi.** Three openWakeWord
  instances loaded simultaneously instead of one shared model.
  Probably fine (each is small, ~5% of one A76 core estimated), but
  measure before committing. **Open. Measure in Phase 1f.**
- **DTLN leg's actual value with per-leg training.** Currently DTLN
  fills a gap where AEC3 struggles. If the per-leg AEC model
  improves enough, is DTLN still pulling its weight? RAM cost is ~75
  MB on the Pi. **Open. Measure after Phase 1e.**
- **"Suppress wake during own-speaker playback" feature.** If
  iteration-1 deployment surfaces real annoyance with the speaker
  waking on its own media playback (e.g. Tony Stark saying "Jarvis"
  in a movie), consider a UX-layer fix: ignore wake-fires during
  active media playback, or context-aware suppression. Out of scope
  for wake-word training; would be a separate small feature.

---

## 10. Glossary

Quick reference for terms used throughout.

- **AEC** — Acoustic Echo Cancellation. The signal-processing step
  that subtracts the speaker's own output (music + TTS) from the mic
  input, leaving (ideally) just the user's voice.
- **AEC reference signal** — The audio being sent TO the speaker
  (music + TTS), used as the "what to cancel" signal for AEC.
- **AGC** — Automatic Gain Control. Normalizes audio level so quiet
  voices and loud voices both arrive at the model at similar
  amplitude.
- **BEST_A** — A specific tuned configuration of WebRTC AEC3 v2.1
  developed in the 2026-05-22 spike. See `experiments/aec3-v2-deep-tune-spike/`.
- **DTLN-aec** — Deep Temporal Latent Network for AEC. A neural-net
  alternative to WebRTC AEC3. Runs as a parallel engine in JTS's
  triple-stream architecture (PR #253).
- **Far-field** — Audio captured at distance (typically 2+ meters)
  with significant attenuation + reverb. The dominant problem for
  JTS.
- **FAR / FPPH** — False Accept Rate / False Positives Per Hour. How
  often the wake-word fires on non-wake-word audio.
- **FRR** — False Reject Rate. How often the wake-word FAILS to fire
  on a real wake-word utterance.
- **Gold corpus** — The known-conditions evaluation set captured
  deliberately for this experiment (Phase 0b). Distinct from the
  production wake_events corpus (unknown conditions).
- **`hf_CV`** — High-Frequency Coefficient of Variation. A metric
  measuring per-frame variability in the 3-7 kHz band; jumps when
  the AEC3 residual suppressor frame-gates speech consonants.
  Diagnostic for the vocal-tear artifact.
- **`jarvis_v2`** — Current production wake-word model. Community-
  trained by fwartner. Published recall 26%.
- **KWS** — Keyword Spotting. The technical term for "wake-word
  detection."
- **livekit-wakeword** — The training pipeline we're vendoring.
  Open-source, Apache 2.0. Outputs ONNX drop-in compatible with
  openWakeWord runtime.
- **NS** — Noise Suppression. Removes stationary background noise
  (HVAC, fans). JTS uses WebRTC's NS at `kLow` setting.
- **OpenSLR-28** — Open Speech and Language Resources, set 28: a
  mixed corpus of real-measured RIRs (from RWCP + REVERB + AIR
  databases, ~325 real) AND simulated RIRs (image-method, generated
  by the SLR-28 authors). Used for far-field training augmentation.
  Apache 2.0 licensed.
- **BIRD** — Bigger Impulse Response Dataset
  ([arXiv:2010.09930](https://arxiv.org/abs/2010.09930)). 100,000
  multichannel image-method simulated RIRs, 1 second each at 16 kHz,
  FLAC. Optional extra source of simulated RIRs for training
  diversity beyond OpenSLR-28.
- **FAST-RIR** — Neural-network-based generative RIR simulator
  ([arXiv:2110.04057](https://arxiv.org/abs/2110.04057)). 400× faster
  than image-method on CPU; generates RIRs on-the-fly during training
  instead of using a precomputed corpus. Useful upgrade if iteration
  1 hits RIR-diversity limits.
- **openWakeWord** — The wake-word library JTS uses at runtime. Per
  `openwakeword.Model()`. Apache 2.0. Loads ONNX files regardless of
  which training pipeline produced them.
- **OR-gate fusion** — Decision rule where the wake fires if ANY of
  the per-leg models scores above its threshold. Existing triple-
  stream architecture in `WakeLoop`.
- **PR #69** — Open PR against livekit-wakeword adding custom
  positive samples support. Has been unmerged for 5+ weeks. We
  vendor it.
- **RIR** — Room Impulse Response. A recording of how a specific
  room "rings" when you generate a brief impulse (clap, sine
  sweep). Captures reverb tail, frequency response, reflections.
  Convolving a clean voice clip with an RIR produces what that
  voice would sound like recorded in that room. Used for training
  augmentation: simulate thousands of room conditions without
  recording in each.
- **RS / Residual Suppressor** — The non-linear post-processing
  stage in WebRTC AEC3 that suppresses what the linear filter
  couldn't cancel. The source of vocal tears when it over-engages
  on speech frames.
- **SNR** — Signal-to-Noise Ratio. Voice level minus background
  noise level, in dB.
- **Triple-stream** — The current JTS wake architecture: raw mic +
  AEC ON + DTLN-aec, each scored independently, OR-gated. Shipped
  PR #253.
- **VAD** — Voice Activity Detection. Decides "is this frame
  speech?" JTS uses Silero V5 downstream of wake (for sustained-
  speech gate), and openWakeWord's internal VAD (currently
  disabled).
- **wake_events corpus** — Production telemetry corpus at
  `/var/lib/jasper/wake-events/`. Every wake fire + near-miss
  captured with per-leg WAVs. ~1250 events ring-buffered.

---

## 11. Cross-references to prior investigation

For context, history, and rationale. Don't restate; link.

- [`HANDOFF-aec.md`](HANDOFF-aec.md) — AEC engine + architecture
  decisions. Why WebRTC AEC3 over alternatives. Why no PipeWire.
  BEST_A tune. Software AEC vs chip AEC.
- [`HANDOFF-mic-quality-v2.md`](HANDOFF-mic-quality-v2.md) — The
  active empirical-findings doc through 2026-05-23. Sweep results.
  `hf_CV` diagnosis. Triple-stream architecture shipping. AEC3 BEST_A
  config decisions. Per-leg measurement methodology.
- [`HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md) — Dual-
  stream (now triple-stream) wake detection. SQLite schema. Per-
  event audio capture. Funnel stages. The corpus this doc's Phase
  0c baseline references.
- [`HANDOFF-vad-experiments.md`](HANDOFF-vad-experiments.md) — The
  failed VAD A/B test matrix from PR #283 → #301. Why Cell 0 (the
  pre-existing default) won. Why server-side VAD is out.
- [`HANDOFF-xvf3800.md`](HANDOFF-xvf3800.md) — The chip-side
  microphone reference. Firmware variants. Mixer state. Why on-chip
  AEC is disabled. The 6-channel firmware variant requirement for
  the triple-stream architecture.
- [`HANDOFF-resilience.md`](HANDOFF-resilience.md) — Why the bridge
  → voice transport is UDP, not snd-aloop. Why the persistent
  journal is needed for diagnosing watchdog resets.
- [`audio-paths.md`](audio-paths.md) — The two ALSA paths to the
  dongle (music vs TTS). Required reading before any audio-routing
  change.
- `experiments/aec3-v2-deep-tune-spike/README.md` — The BEST_A
  development log. What was tested. What didn't combine. What
  remains unreached.

---

## 12. Status + last verified

**PR #303 data engineering: merged 2026-05-25.** Extract / enroll /
noise-capture CLIs are available. All 63 tests passing.

**Phase −1 (pre-foundation verifications): in progress.**
- −1a (LLM session routing): investigation results in PR cover
  letter / next session message.
- −1b (Tang 2020 verification): DONE 2026-05-25. Findings folded
  into Phase 1c. See v4 changelog entry.

**Phase 0a (offline harness): not started.** Gated on Phase −1.

**Phase 0b (gold corpus capture): pending tooling.** Single speaker
(Jasper) across two recording sessions on different days. 60 train +
20 held-out positives + ~30-40 negative utterances. No multi-speaker
data in iteration 1.

**Phase 0c (baseline): pending Phase 0a + 0b.**

---

## Changelog

- **2026-05-25 (v1):** Initial document.
- **2026-05-25 (v2):** Major revision after methodology critique:
  - Added Phase −1 (pre-foundation verifications): LLM routing
    check, SNR distribution measurement, Tang 2020 citation
    verification.
  - Phase 0b: split into two recording sessions on different days
    (60 train + 20 held-out for Jasper) + added 24 Brittany clips
    as regularization probe.
  - Phase 0c: baseline now computed against held-out + Brittany,
    not training split.
  - Phase 1c: added explicit hard-negative collection (~20 min
    Iron Man / tech podcasts / Jarvis-similar content).
  - Phase 1d: added union model as 4th training arm.
  - Phase 1e: explicit three-way comparison (per-leg vs jarvis_v2,
    per-leg vs union, Jasper vs Brittany).
  - Phase 1e': new — real-audio threshold calibration after
    training-time calibration.
  - Phase 1f: gated on pre-committed failure criteria.
  - TL;DR: added "What failure looks like" section with explicit
    revert criteria (recall < 60%, FP > 0.5/hr, Brittany < 40%,
    artifact-matching wins).
  - §8: strengthened "no Silero upstream" rationale (VAD veto power
    over OR-gate is the primary reason, not the V5 regression).
  - §9: marked resolved-by-promotion items.
- **2026-05-25 (v4):** Phase −1b verification results folded in:
  - Tang 2020 does NOT actually recommend a 50/50 real+sim RIR mix
    (that was an over-interpretation by an earlier research agent).
    The paper compares real-only, sim-only, and various sim
    techniques — no mixing experiments. The real finding is that
    hybrid ISM+SRT with air absorption closes ~36% of the sim-vs-
    real gap.
  - OpenSLR-28 is already a mixed real+sim corpus, not pure real-
    measured as previous versions assumed. Phase 1c updated to
    "use OpenSLR-28 as-is" rather than "mix 50/50 with BIRD."
  - BIRD downgraded from "the standard simulated RIR source" to
    "optional extra source for diversity." FAST-RIR flagged as a
    more current alternative if iteration 1 hits RIR limits.
  - Glossary updated for OpenSLR-28 (corrected to "mixed corpus")
    and BIRD ("optional"); FAST-RIR added.
- **2026-05-25 (v3):** Scoped down per Jasper's feedback:
  - Added "This is iteration 1 of several" framing to TL;DR.
  - Dropped Phase −1b (SNR measurement): existing wake_events
    corpus is bench-only, not representative; deferred to iteration
    2 once the speaker is in real use. Phase −1b renumbered to
    Tang 2020 verification.
  - Dropped Brittany from Phase 0b: not available to record in
    iteration 1; moved to §9 deferred items.
  - Dropped Brittany failure criterion from TL;DR.
  - Phase 0b: replaced "find Iron Man + podcast hard negatives"
    with "Jasper records 30-40 similar-sounding words in Session B
    (Travis, service, etc)". Movies/podcasts saying actual
    "Jarvis" are acoustic true positives — out of scope for wake-
    word training, handled at UX layer if it becomes a real
    annoyance (added to §9).
  - Phase 0c + 1e: dropped Brittany comparison; held-out Jasper
    split + hard-negative utterances are the eval set.
  - Phase 1e: comparisons trimmed from three to two (per-leg vs
    jarvis_v2, per-leg vs union).
  - §9: deferred items list for iteration 2 (SNR measurement,
    Brittany, real-usage utterances, own-speaker-playback
    suppression).

Last verified: 2026-05-25

