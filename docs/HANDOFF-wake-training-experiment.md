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
(~85-105 positive Jarvis utterances across two sessions: 3 distances
× 3 conditions, plus ~30-40 hard negatives in Session B). The
browser recorder captures the three production legs (`:9876` AEC ON,
`:9877` chip-direct, `:9878` DTLN) plus opt-in `raw0` (`:9879`) for
future cheap-mic portability. It can also opt into corpus-only cheap
USB mic + reference legs (`:9880`/`:9881`/`:9882`) for testing whether
the software-AEC path can lower the hardware entry point; those legs
are not production wake inputs. Build an offline test harness and
scoring runner around that corpus. Train per-leg specialized Jarvis
models (one for raw, one for AEC ON, one for DTLN) using
`livekit-wakeword` + PR #69 (vendored), with Piper
synthetic positives + OpenSLR-28 RIR augmentation + 5-20 dB SNR noise
mixing. Deploy via the existing OR-gate fusion. Validate at every
decision point with both metrics AND human listening (five explicit
checkpoints).

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

**Per-leg specialized models.** Three trained models matching the
three production wake-detection legs:

- `jarvis_jts_raw_v1.onnx` — trained on chip-direct audio (`:9877`
  chip-direct leg — chip BF+NS+AGC+HPF, no software AEC)
- `jarvis_jts_aec_v1.onnx` — trained on AEC3-processed audio (`:9876`
  AEC ON leg, current BEST_A production config)
- `jarvis_jts_dtln_v1.onnx` — trained on DTLN-aec-processed audio
  (`:9878` DTLN leg)

Each leg's training distribution is matched to its deployment
distribution (the "matched-conditions training" principle from Wu
2020). The chain is treated as a fixed snapshot of current production
during training; if chain ablation later wins, retraining is cheap
(~$5-15 per leg per Modal run).

**A 4th `raw0` leg is captured during Phase 0b** from chip channel
2 (truly raw — no chip OR software DSP). It is NOT consumed by
production wake detection — it exists purely as training data for
two future use cases: (a) testing whether iteration-N's model
generalizes to no-chip mic hardware without retraining, (b) producing
a `jarvis_jts_raw0_v1` model later if we ever ship JTS on cheaper mic
hardware. Iteration 1 captures it but doesn't train on it; the value
compounds across future iterations.

**Optional cheap-USB corpus legs** (`ref`, `usb_raw`, `usb_webrtc`,
`usb_dtln`) are also available during Phase 0b. These are corpus-only
comparison legs for the hardware-cost question: can a $10
single-channel USB mic plus software AEC get close enough to the XVF
chain? They are not production wake-detection inputs. USB DTLN is
optional and high-resource-risk on the 1 GB Pi; use it when the extra
comparison point is worth the neural-engine cost.

**Fusion: existing OR-gate.** The triple-stream architecture shipped
in PR #253 is unchanged. Each leg scores against its own specialized
model; any leg firing above threshold triggers the session. Shared
0.7 s refractory.

**Eval surface: gold corpus (Phase 0 deliverable).** Not the
production wake_events corpus (which has unknown conditions). The
Phase 0b corpus is recorded deliberately with known distance +
condition labels across two sessions. Each clip captures the
configured production wake legs, plus raw0 and/or USB/reference legs
when the session opts in; see §5 Phase 0b for the current scope.

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

**Phase 0b — Gold corpus capture (~60-90 min Jasper recording time
across two sessions).** **Tooling shipped.** Browser-based recorder
at http://jts.local/wake-corpus/, exposed via socket-activated
`jasper-web` (no daemon resident when nobody's looking). PRs landed:
- PR #303 (data engineering CLIs: extract, enroll, noise capture)
- PR #306 (score, review)
- PR #307/309/312/313 (recorder hardening + nginx integration + home-
  page card)
- PR #315 (live mic-level meter + ambient condition + trash icon)
- PR #323 (raw mic 0 4th leg + sessions management UX)

**What the recorder captures.** Up to **eight legs** per utterance,
written into per-leg quadrant directories at
`/var/lib/jasper/enrollment_positives/aec_<leg>_<condition>/`:

| Leg | Source | Signal path |
|---|---|---|
| `on` | UDP `:9876` | chip ch1 (chip BF+NS+AGC+HPF) → SW AEC3 + NS=low + AGC1 |
| `off` | UDP `:9877` | chip ch1 — **no software processing** |
| `dtln` | UDP `:9878` | chip ch1 → SW DTLN-aec |
| `raw0` | UDP `:9879` | chip ch2 — **truly raw, no chip OR software DSP** (gated by per-session toggle) |
| `ref` | UDP `:9880` | 16 kHz mono speaker reference frame that SW AEC consumes (corpus-only; opt-in; playback selector lists it last) |
| `usb_raw` | UDP `:9881` | cheap USB mic mono capture, no software processing (corpus-only; opt-in) |
| `usb_webrtc` | UDP `:9882` | cheap USB mic → SW AEC3 + same NS/AGC settings as the production AEC chain (corpus-only; opt-in) |
| `usb_dtln` | UDP `:9883` | cheap USB mic → SW DTLN-aec (corpus-only; opt-in, high resource risk) |

The 4th `raw0` leg (PR #323) is the future-proofing layer — it
captures a no-chip baseline from the XVF. The USB/reference opt-in
legs (added 2026-05-26) go one step further: they record a real cheap
USB mic in parallel with the exact reference frame the bridge fed into
WebRTC. These are for testing and offline analysis, not for iteration
1 production wake detection. **Always opt into raw0 in iteration 1.**
Opt into USB/reference when the cheap mic is connected. If the bridge
is not already emitting the requested optional legs, the recorder will
offer to enable the matching bridge flags, restart
`jasper-aec-bridge`, and only then begin the session.
The recorder labels WebRTC legs as **WebRTC AEC3** so they are not
confused with raw or DTLN outputs. The `usb_raw` leg is JTS-unprocessed
except for resampling to 16 kHz; the UI warns when the USB mic's own
ALSA hardware Auto Gain Control is enabled because that can create
pumping or top-end artifacts before JTS ever sees the samples.

**DTLN policy.** The existing `dtln` leg is still the first neural-AEC
comparison path. Keep it optional on the Pi: `JASPER_AEC_DTLN_ENABLED=1`
turns on XVF DTLN inference in the bridge, and the recorder has a
per-session XVF DTLN checkbox for choosing whether to subscribe to
that leg. Cheap-USB DTLN is a separate experiment: the bridge only
runs the second neural engine when `JASPER_AEC_CORPUS_USB_ENABLED=1`
and `JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1`; the recorder's USB DTLN
checkbox subscribes to `usb_dtln` and records its `ref` + `usb_raw`
companion legs. Treat USB DTLN as high resource risk on a 1 GB Pi.

**Recording protocol — Jasper (~60-90 min total, across two
sessions on different days):**
- **Conditions (now 3, not 2):** quiet (no music), **ambient** (AC /
  fridge / HVAC cycling, no music), music (defined playlist).
  Ambient added 2026-05-25 in PR #315 — realistic-home third state
  that sits between quiet and music acoustically.
- **Distances (unchanged):** ~1 m near, ~2 m mid, ~3-4 m far.
- **Cell grid: 3 × 3 = 9 cells.** Aim ~7-9 utterances per cell in
  Session A (training) → ~63-81 total. Aim ~2-3 per cell in Session
  B (held-out) → ~20-25 total.
- Voice condition: normal speaking volume, slightly lower on the
  normal side, slightly faster than average.
- Distance markers (tape on floor) for repeatability.
- Music: defined playlist (one pop track, one classical, one podcast
  clip) consistent across music-condition takes.
- **Captured across TWO recording sessions on DIFFERENT DAYS.**
  Session A is the training + baseline corpus. Session B is the
  held-out test corpus (NEVER touches training). Slight ambient
  variation + slight mic position shift between sessions is the
  point; we want the test corpus to be in-distribution but not
  identically-captured.
- Both sessions: **opt into raw mic 0** via the recorder's "Also
  capture raw mic 0" checkbox at session start. Per-session
  property — every clip in the session inherits it.
- Cheap-USB comparison sessions: plug in the USB mic, enable bridge
  corpus legs, then check "Also capture USB mic + reference" at
  session start. This adds `ref`, `usb_raw`, and `usb_webrtc` WAVs
  per clip and makes the clip-row playback selector show all recorded
  legs.

**Bridge env for USB/reference corpus sessions:**

Preferred path: check the USB/reference and/or USB DTLN boxes in the
recorder. If the bridge outputs are disabled, accept the recorder's
enable-and-restart prompt. The prompt writes
`/var/lib/jasper/wake_corpus_bridge.env`, which
`jasper-aec-bridge.service` sources after `/etc/jasper/jasper.env`.
If the bridge cannot restart with the requested optional outputs
(for example, the USB mic is missing), the recorder rolls that env
file back and restarts the bridge with the prior config.

Equivalent manual env:

```sh
JASPER_AEC_CORPUS_REF_ENABLED=1
JASPER_AEC_CORPUS_USB_ENABLED=1
JASPER_AEC_USB_MIC_DEVICE="USB PnP Sound Device"
# Optional only if PortAudio's default-rate probe is wrong:
# JASPER_AEC_USB_MIC_RATE=44100
```

USB DTLN adds:

```sh
JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1
```

Ports default to `:9880` (`ref`), `:9881` (`usb_raw`), and `:9882`
(`usb_webrtc`). The recorder can hide those ports with
`JASPER_WAKE_CORPUS_USB=0` if the bridge is running without them.
The bridge opens the USB mic at its PortAudio default sample rate
(the test mic reported 44.1 kHz) and resamples to the corpus contract
of 16 kHz mono before UDP emission.

**Reference-quality follow-up.** The current `ref` leg is intentionally
the exact 16 kHz mono frame the live AEC consumes. A future
`ref_fullband` archival leg could preserve the original full-band
speaker reference before downsampling, but that changes the bridge,
recorder, and audit surface. Track it as a post-corpus follow-up rather
than changing the main recording protocol tonight.

**Sessions management UX (PR #323).** The recorder's top-of-page
Sessions card lists every recorded session (member, timestamp,
clip count, condition breakdown, leg indicators). Each row has
Load (resume) + Delete (hard-remove WAVs + JSON) buttons. Cleanup
of pre-raw0 sessions = one click each before starting fresh.

Plus **~15 min hard-negative recording in Session B**: Jasper
records similar-sounding words/phrases that should NOT trigger:
- "Travis", "service", "savings", "Charlie", "jarvey", "harvest",
  "garbage", "jealous" — single-word utterances, ~3 reps each
- Short phrases: "Travis is here", "good service", etc.
- ~30-40 negative utterances total
- Condition variety where it's practical — these are hard negatives,
  not the primary eval set

These go into training as targeted negatives alongside the synthetic
phoneme-substitution adversarial set that livekit-wakeword already
generates via CMUDict.

**No multi-speaker data in iteration 1.** Brittany isn't available
to record. Future iteration (v2) when she is — adds a regularization
probe + potentially trained samples.

Total: ~85-105 Jasper Jarvis (Session A train + Session B held-out)
+ ~30-40 negative utterances. Single speaker, two sessions, ~60-90
min total recording across the two sessions.

**Fetching the corpus to laptop for offline processing.** The
recorder writes everything under `/var/lib/jasper/enrollment_positives/`.
Pull with rsync:

```sh
rsync -avz --progress \
  pi@jts.local:/var/lib/jasper/enrollment_positives/ \
  ./data/enrollment_positives/
```

Then `scripts/_extract_wake_corpus.py` (from PR #303) handles
quadrant-split + the conversion to per-leg training arrays.

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
3. Add background noise at an initial 5-20 dB SNR range. Real
   deployment SNR measurement was deferred to iteration 2 once the
   speaker is in daily use; adjust if iteration 1 evidence demands
   wider. NOT below 0 dB — the training distribution should bracket
   realistic deployment SNRs, not extend into the
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

**Data-engineering CLIs (PR #303): merged 2026-05-25.** Extract /
enroll / noise-capture.

**Browser-based wake-corpus recorder: SHIPPED 2026-05-25.**
Available at http://jts.local/wake-corpus/. PRs landed in sequence:
- PR #303 — data engineering CLIs (extract / enroll / noise)
- PR #306 — scoring + review packages
- PR #307 / #309 — recorder hardening (CSRF, idempotent stop)
- PR #312 — socket-activated nginx integration (http://jts.local/wake-corpus/)
- PR #313 — home-page card + back-to-home nav
- PR #315 — live mic-level SSE meter + ambient condition + trash icon
- PR #318 — trash button CSS overflow fix
- **PR #323 — raw mic 0 4th leg (chip ch2, `:9879`) + sessions
  management UX (list / load / delete) + per-session
  include_raw_mic_0 toggle**
- 2026-05-26 follow-up — corpus-only cheap USB mic + reference legs
  (`:9880`-`:9882`), session-level leg metadata, and all-leg playback
  selector
- 2026-05-27 polish — playback labels now distinguish WebRTC AEC3
  from raw/DTLN legs, Reference is listed last, and the UI surfaces
  the USB raw/no-JTS-AGC contract plus USB hardware AGC status

Recorder UX status:
- ✅ One-click record, click-again-stop, spacebar hotkey
- ✅ Live mic-level meter (SSE, ~12 Hz when recording, ~2 Hz idle)
- ✅ 3 conditions: quiet / ambient / music
- ✅ 3 distances: near / mid / far
- ✅ Per-session raw-mic-0 toggle (4 legs vs 3)
- ✅ Per-session USB/ref toggle for corpus-only cheap-mic experiments
  (`ref`, `usb_raw`, `usb_webrtc`)
- ✅ Sessions card: list all sessions, Load (resume), Delete (with
  confirm)
- ✅ Per-cell counts matrix + recorded-clips list with HTML5 audio
  playback selector for every WAV recorded on a clip
- ✅ Playback selector labels WebRTC paths as WebRTC AEC3 and puts the
  speaker Reference leg last
- ✅ USB raw operator note + ALSA hardware Auto Gain Control warning
  before recording cheap-mic sessions
- ✅ jasper-voice start/stop wired (refuses start while recording —
  would EADDRINUSE the UDP ports)

Recording-day audit tooling:
- ✅ `bash scripts/audit-wake-corpus.sh data/enrollment_positives
  --expect-raw0` validates post-rsync session metadata, raw0 leg
  presence, condition × distance coverage, and WAV format/RMS.
- ✅ `--expect-leg ref --expect-leg usb_raw --expect-leg usb_webrtc`
  and `--expect-leg usb_dtln`
  validates USB/reference opt-in sessions after rsync.

**Phase −1 (pre-foundation verifications): in progress.**
- −1a (LLM session routing): investigation results in PR cover
  letter / next session message.
- −1b (Tang 2020 verification): DONE 2026-05-25. Findings folded
  into Phase 1c. See v4 changelog entry.

**Phase 0a (offline harness): not started.** Gated on Phase −1.

**Phase 0b (gold corpus capture): tooling READY, recording PENDING.**
First two recording sessions scheduled for Jasper's next studio
morning. Cleanup of pre-raw0 corpus is one-click per old session
via the new Sessions card.

**Phase 0c (baseline): pending Phase 0a + 0b.**

---

## Changelog

- **2026-05-27 (v9):** Wake-corpus recording-day polish:
  - Playback labels now say WebRTC AEC3 for the WebRTC AEC paths and
    keep the speaker Reference leg last in the clip selector.
  - UI surfaces that USB raw is hardware-captured/resampled with no
    JTS software AGC before saving.
  - UI warns when the cheap USB mic's ALSA Auto Gain Control is
    enabled, because that can explain pumping or top-end artifacts in
    USB raw clips.
  - `ref_fullband` archival reference capture is recorded as a
    follow-up; the current `ref` leg remains the exact 16 kHz mono AEC
    input frame.
- **2026-05-26 (v8):** Recorder-managed bridge-output enable flow:
  - `jasper-aec-bridge.service` now sources optional recorder-owned
    `/var/lib/jasper/wake_corpus_bridge.env` after
    `/etc/jasper/jasper.env`.
  - Beginning a session with checked XVF DTLN, USB/reference, or USB
    DTLN legs now verifies the bridge is emitting those outputs. If
    not, the UI offers to enable the required flags and restart the
    bridge before creating the session, so checked boxes cannot silently
    produce missing WAV legs.
  - Failed bridge restarts roll back the recorder-owned env file and
    restart the bridge with the prior config.
- **2026-05-26 (v7):** Cheap-USB/ref corpus comparison path:
  - Added corpus-only bridge outputs for `ref` (`:9880`), `usb_raw`
    (`:9881`), and `usb_webrtc` (`:9882`), gated by
    `JASPER_AEC_CORPUS_REF_ENABLED` / `JASPER_AEC_CORPUS_USB_ENABLED`.
  - Added optional `usb_dtln` (`:9883`), gated by
    `JASPER_AEC_CORPUS_USB_DTLN_ENABLED`, plus session checkboxes for
    XVF DTLN and USB DTLN capture.
  - Recorder now persists a session-level `enabled_legs` list instead
    of relying only on the raw0 boolean; legacy metadata still reads
    correctly.
  - Clip playback now has a leg selector so Jasper can compare XVF
    WebRTC/raw/DTLN/raw0, USB raw/WebRTC/DTLN, and reference WAVs per
    utterance.
  - Audit script accepts repeated `--expect-leg` flags for USB/ref
    sessions while preserving `--expect-raw0`.
- **2026-05-25 (v6):** Recording-day prep fixes:
  - Corrected stale TL;DR / architecture prose that still described
    the older 60-utterance, 2-condition, AEC-reference-capture plan.
    At the time, Phase 0b truth was 3 conditions, two sessions, raw0
    opt-in, and no AEC reference capture in iteration 1. v7 re-added
    reference capture only as a corpus-only cheap-USB experiment aid.
  - Added the local `scripts/audit-wake-corpus.sh` post-rsync audit
    for session metadata, raw0 presence, per-cell coverage, and WAV
    format/RMS sanity.
  - Recorder production path now keeps raw0 in the combined
    `jasper-web` port map; clip sequence numbers are monotonic across
    deletes to avoid filename reuse.
- **2026-05-25 (v5):** Capture tooling shipped end-to-end. Phase 0b
  rewritten:
  - Recorder is now the browser UI at http://jts.local/wake-corpus/,
    NOT a `jasper-wake-enroll --capture-ref` CLI extension. PRs #303
    → #323 are the lineage; full list in §12.
  - 4th leg added: raw mic 0 (chip ch2, UDP `:9879`) — truly raw,
    no chip OR software DSP, captures what a cheap USB mic would
    deliver. Per-session opt-in via recorder checkbox. Stored under
    `aec_raw0_<condition>/`. Iteration 1 captures but does not
    train on it; value compounds for future iterations (cheap-mic
    portability test + `jarvis_jts_raw0_v1` model later).
  - Conditions extended from 2 to 3: quiet / **ambient** / music.
    Ambient is the realistic-home third state (AC, fridge, HVAC —
    no music). Cell grid is now 3 × 3 = 9 cells (was 3 × 2 = 6).
  - Recording protocol numbers updated: ~7-9 per cell in Session A
    (~63-81 train), ~2-3 per cell in Session B (~20-25 held-out).
    Total recording time unchanged at ~60-90 min across two sessions.
  - Sessions management UX documented: list / load / delete per
    session via the recorder's top-of-page card.
  - AEC reference signal capture (originally Phase 0b's gate on
    Phase 2 offline chain ablation) DROPPED from the core training
    plan — Phase 1 trains against a fixed chain at current production
    BEST_A, and Phase 2 is itself gated on Phase 1 leaving gaps. v7
    later restored reference capture as a corpus-only cheap-USB
    comparison aid, not as a Phase 2 pre-spec.
  - Architecture §4: per-leg models updated to call out 3 production
    models trained on `:9876` / `:9877` / `:9878`. The `raw0`
    capture is explicitly for future use, not iteration 1 training.
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

Last verified: 2026-05-27 (v9 — wake-corpus polish + USB AGC warning verified)
