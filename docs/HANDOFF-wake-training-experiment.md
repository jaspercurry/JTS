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
browser recorder captures the production software/chip-direct legs
(`:9876` primary/session carrier, `:9877` chip-direct, `:9878` DTLN)
plus opt-in `raw0` (`:9879`) for future cheap-mic portability. It can
also opt into cheap USB mic + reference legs (`:9880`/`:9881`/`:9882`)
and the XVF chip-AEC beams (`:9887`/`:9888`) for testing hardware AEC
against software AEC on the same utterance. As of 2026-05-31,
`chip_aec_150` / `chip_aec_210` are no longer corpus-only: they are
default-OFF, 6-channel-firmware-gated production wake legs when chip-AEC
mode is enabled. Build an offline test harness and
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

**Optional chip-AEC comparison legs** are available during Phase 0b
through the recorder's `chip_aec_comparison_v1` profile. This profile
puts the XVF3800 into the lab-proven ASR fixed-beam hardware-AEC mode,
feeds the chip from outputd's direct final-output fanout, and captures
both `chip_aec_150` / `chip_aec_210` alongside `raw0`,
`xvf_raw0_webrtc_aec3`, `ref`, `usb_raw`, and `usb_webrtc`. The profile
is corpus-only: it exists to compare hardware AEC, software AEC3, raw,
USB, and optional DTLN on the same utterance before deciding what
belongs in production fusion.

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

**What the recorder captures.** Up to **fifteen legs** per utterance
across the standard, AEC3-sweep, and chip-AEC comparison profiles,
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
| `usb_webrtc` | UDP `:9882` | cheap USB mic → SW AEC3 (corpus-only; opt-in). Outside sweep mode this is the chosen USB edge-combo profile at `stream_delay_ms=80`. In USB AEC3 sweep sessions this is the 40 ms edge-combo delay-hint member. |
| `usb_dtln` | UDP `:9883` | cheap USB mic → SW DTLN-aec (corpus-only; opt-in, high resource risk) |
| `aec3_variant_1` | UDP `:9884` | parallel SW AEC3 slot 1. The input source is explicit metadata: legacy/manual source is `xvf`; new recorder AEC3-sweep sessions default to `usb`. Code default is edge-combo tuning with `JASPER_AEC_STREAM_DELAY_MS=80`. |
| `aec3_variant_2` | UDP `:9885` | parallel SW AEC3 slot 2. Same source rule as slot 1. Code default is edge-combo tuning with `JASPER_AEC_STREAM_DELAY_MS=120`. |
| `aec3_variant_3` | UDP `:9886` | parallel SW AEC3 slot 3. Same source rule as slot 1. Code default is edge-combo tuning with `JASPER_AEC_STREAM_DELAY_MS=160`. |
| `chip_aec_150` | UDP `:9887` | XVF3800 on-chip AEC, category-7 ASR fixed gated beam at `150°` (default-OFF production wake leg when chip-AEC mode is enabled; also a corpus comparison leg) |
| `chip_aec_210` | UDP `:9888` | XVF3800 on-chip AEC, category-7 ASR fixed gated beam at `210°` (paired default-OFF production wake leg; keep because orientation shifts can swap the winner) |
| `xvf_raw0_webrtc_aec3` | UDP `:9889` | chip ch2 raw0 → SW WebRTC AEC3 using the same outputd final-output reference as chip AEC |
| `xvf_raw0_dtln` | UDP `:9890` | chip ch2 raw0 → SW DTLN-aec (optional, high resource risk) |

The 4th `raw0` leg (PR #323) is the future-proofing layer — it
captures a no-chip baseline from the XVF. The USB/reference opt-in
legs (added 2026-05-26) go one step further: they record a real cheap
USB mic in parallel with the exact reference frame the bridge fed into
WebRTC. These are for testing and offline analysis, not for iteration
1 production wake detection. **Always opt into raw0 in iteration 1.**
Opt into USB/reference when the cheap mic is connected. If the bridge
is not already emitting the requested optional legs, the recorder will
offer to enable the matching corpus outputs, restart the affected
daemons (`jasper-outputd`, `jasper-aec-init`, and/or
`jasper-aec-bridge`), and only then begin the session.
The recorder labels WebRTC legs as **WebRTC AEC3** so they are not
confused with raw or DTLN outputs. The `usb_raw` leg is JTS-unprocessed
except for resampling to 16 kHz, which matches the wake/AEC model
contract and keeps the corpus legs directly comparable.

**Chip-AEC comparison profile.** The recorder's default new-session
profile is `chip_aec_comparison_v1` while this workstream is deciding
whether chip AEC belongs in the corpus. It captures:
`chip_aec_150`, `chip_aec_210`, `raw0`, `xvf_raw0_webrtc_aec3`, `ref`,
`usb_raw`, and `usb_webrtc`, with optional `xvf_raw0_dtln` and
`usb_dtln`. Internally the profile writes
`JASPER_OUTPUTD_REFERENCE_UDP_TARGET` and `JASPER_OUTPUTD_CHIP_REF_PCM`
so outputd fans the exact final speaker buffer both to the XVF3800
USB-IN reference and to the bridge's software-AEC reference tap.
`jasper-aec-init` applies the volatile chip profile
(`SHF_BYPASS=0`, ASR fixed gated `150°/210°`, `AEC_AECEMPHASISONOFF=2`)
only while the recorder-owned env file requests it; exiting corpus test
mode removes those overrides and returns the chip to production bypass.

**AEC3 sweep policy.** The recorder has a corpus-only **USB AEC3
sweep** checkbox for pilot tuning before the gold corpus is recorded.
When selected, `jasper-aec-bridge` runs three additional warmed WebRTC
AEC3 instances in parallel with the baseline XVF `on` leg. As of
2026-05-28, new recorder-created sweep sessions set
`JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb`: each utterance records the
XVF AEC3 reference (`on`), USB raw/reference, USB baseline WebRTC AEC3
(`usb_webrtc`, currently edge-combo at 40 ms inside sweep mode), and the three USB-fed
variant slots (`aec3_variant_1`-`aec3_variant_3`, currently the same
edge-combo tuning at 80/120/160 ms). Older/manual sessions without the
source flag remain XVF-fed for backward compatibility. Keep this mode
quarantined as pilot data: it is for Jasper listening + offline
analysis, not Session A/B training/eval. The three sweep legs are
stable machine-readable slots; their display labels and env overrides
may be changed at runtime with
`/var/lib/jasper/aec3_sweep_variants.json` and
`jasper-aec-sweep-config apply <file> --restart-bridge`, avoiding a
full deploy for knob changes. The exact effective variant metadata,
input source, and config hash are written into the session sidecar.
Use AEC3 sweep separately from DTLN to protect the 1 GB Pi resource
budget and keep listening comparisons readable.

**2026-05-28 AEC3 fusion tuning guidance.** Judge sweep pilots by
fusion value — union hits and unique saves — not only by the best
single-leg hit count. Marginal far+music tests now show the Edge
family can beat BEST_A as an added wake leg, but not as a safe
unconditional production replacement yet. "Edge" here means relaxed
high-frequency suppression, slower suppressor attack, and faster
dominant-near-end detection. Edge NS-off disables WebRTC NS on top of
that; Edge NS+AGC1-off also disables AGC1.

Current XVF AEC3 candidates before the next pilot:
- **Two AEC3 wake legs:** use BEST_A + Edge NS-off as the practical
  level-stable pair. BEST_A + Edge NS+AGC1-off is the strict
  threshold-0.5 wake-only union winner so far, but that variant is
  roughly 7-8 dB quieter and should stay diagnostic until speech
  quality and level behavior are better understood.
- **Three AEC3 wake legs:** use BEST_A + Edge + Edge NS-off. In the
  latest edge-family test this reached the same 38/46 AEC union as
  the AGC-off triple while avoiding the AGC-off level drop.

Plain BEST_A + plain Edge is stability-biased but too overlapping if
only two AEC3 legs are available. The broader lesson is that NS and
AGC can both rescue and damage the wake-word edge: NS-off variants
preserved high-frequency / onset evidence on some clips, while
regressing others hard. Keep raw/raw0 and DTLN in the research set
because they still provide complementary saves that extra AEC3
variants do not always cover.

**DTLN policy.** The existing `dtln` leg is still the first neural-AEC
comparison path. Keep it optional on the Pi: `JASPER_AEC_DTLN_ENABLED=1`
turns on XVF DTLN inference in the bridge, and the recorder has a
per-session XVF DTLN checkbox for choosing whether to subscribe to
that leg. In chip-AEC comparison mode, `xvf_raw0_dtln` is the cleaner
"DTLN on the same raw XVF element" experiment and is controlled by
`JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED`. Cheap-USB DTLN is a separate
experiment: the bridge only runs the USB neural engine when
`JASPER_AEC_CORPUS_USB_ENABLED=1` and
`JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1`; the recorder's USB DTLN
checkbox subscribes to `usb_dtln` and records its `ref` + `usb_raw`
companion legs. Treat XVF raw0 DTLN and USB DTLN as high resource risk
on a 1 GB Pi; use them only when their comparison value is worth the
neural-engine cost.

**2026-05-28 DTLN finding.** Offline DTLN-256 on the latest
far+music marginal session (`20260528T141727Z-28bf`, 46 clips, captured
ref) did not beat tuned AEC3 as a single leg, but it remains
complementary enough to keep as a serious fusion/research candidate.
At threshold 0.5, the existing XVF AEC3 sweep union was 38/46, while
`dtln_off` (XVF off/raw chip ch1) hit 29/46, `dtln_raw0` (XVF raw0/ch2)
hit 28/46, and `dtln_usb_raw` (USB raw) hit 27/46. The three DTLN
projections unioned to 36/46; XVF AEC3 + XVF DTLN reached 41/46; and
existing everything + DTLN reached 42/46.

The DTLN-only saves over all existing recorded legs were clip 17
(`dtln_off=0.986`) and clip 28 (`dtln_raw0=0.761`). Clip 1 was missed
by AEC and saved by `dtln_raw0=0.996`, but USB raw had already caught
it, so it was not a save over everything. On the USB side, existing
USB legs unioned to 25/46; USB existing + `dtln_usb_raw` reached
32/46, with USB DTLN saves on clips 8, 10, 11, 16, 19, 22, and 23.

Caution before treating DTLN as a production fusion leg: the DTLN
outputs are much quieter than the main AEC legs. Median RMS was about
-38.5 dBFS for `dtln_off`, -40.7 dBFS for `dtln_raw0`, and -31.9 dBFS
for `dtln_usb_raw`, versus about -29 dBFS for the main AEC legs. That
points to level normalization or per-leg threshold calibration before
production fusion decisions. A quick fixed make-up gain sweep on the
same DTLN WAVs (+3/+6/+9/+12/+15 dB) showed that level alone does not
explain the remaining misses: `dtln_off` only improved from 29/46 to
30/46 at +6 or +15 dB, `dtln_raw0` stayed 28/46 through +9 dB and then
fell to 27/46, and `dtln_usb_raw` generally regressed or clipped. If
DTLN becomes a production fusion leg, prefer per-leg threshold
calibration and careful normalization over a blind gain boost.

**2026-05-28 DTLN noise-suppression finding.** As an offline-only
follow-up, ran upstream DTLN noise suppression (`breizhn/DTLN`
`model_1.onnx` + `model_2.onnx`) on the same
`20260528T141727Z-28bf` 46-clip far+music session. This is the DTLN
speech-enhancement model, not DTLN-aec: it takes one noisy input and
does not consume the speaker reference. Tested it as (a) a raw-path
cleaner, (b) a post-filter on existing AEC3/USB legs, and (c) a
post-filter on the DTLN-aec outputs from the previous pass.

At threshold 0.5, DTLN-NS did not beat the existing legs as a general
path: `ns_off` hit 20/46, `ns_raw0` 21/46, `ns_usb_raw` 18/46, and the
three raw-input NS legs unioned to 29/46. Applying NS after the XVF
AEC3 variants also regressed individual recall (`ns_on` 23/46,
`ns_edge`-family variants 23/46, 21/46, and 25/46; post-filter union
29/46 versus the original AEC3 union of 38/46). Applying NS after
DTLN-aec produced `ns_dtln_off` 21/46, `ns_dtln_raw0` 28/46, and
`ns_dtln_usb_raw` 24/46, union 34/46 versus the pre-NS DTLN-aec union
of 36/46.

The one meaningful new datapoint: existing recorded legs + DTLN-aec
unioned to 42/46, and adding all DTLN-NS variants raised that to
43/46 by saving clip 25 via `ns_dtln_raw0=0.883`. DTLN-NS also saved
clips 17 and 28 over the originally recorded legs, but DTLN-aec had
already saved those. Net read: DTLN-NS is worth keeping as an offline
research/post-filter candidate for rare marginal cases, but this run
does not justify adding it to the live Pi corpus/test-mode matrix or
the production fusion set yet.

**2026-05-28 waveform-fusion offline finding.** A speculative
waveform-level fusion pass tested whether same-utterance AEC3 and
DTLN outputs could be aligned and mixed into a single "super waveform"
that scores better than either source. The harness lives at
`scripts/_waveform_fusion_experiment.py`; it is laptop/offline only.
It loads a local `enrollment_positives` session, pairs legs such as
`on + dtln` and `usb_webrtc + usb_dtln`, generates delay/weight-swept
mixes, optionally scores them with openWakeWord, and compares each mix
against same-pair max-score/OR fusion. Outputs are written under
`captures/waveform-fusion/<session>/` (gitignored).

First pass on session `20260528T184424Z-d205` (27 far+music clips,
medium-quiet music for clips 1-20 and lower music for clips 21-27)
was promising but not architecture-changing:
- Original all-leg fusion hit 22/27.
- Best XVF waveform mix alone hit 20/27: `on + dtln`, RMS-matched,
  `dtln` delayed +10 ms, 50/50 weight.
- Original all legs + that best XVF mix reached 23/27 by newly saving
  clip 15.
- Best USB waveform mix alone hit 14/27: `usb_webrtc + usb_dtln`,
  native levels, `usb_dtln` delayed +20 ms, 50/50 weight. It beat
  USB pair fusion (11/27) but also only added clip 15 over the full
  original-leg set.

Interpretation: waveform fusion can create model-useful evidence, but
it also loses some clips that score-level fusion keeps. Treat it as an
offline research candidate or possible extra experimental leg, not as
a replacement for per-leg model scoring. Before considering real-time
use, it must beat score fusion across multiple sessions and hard
negatives, and Jasper should listen to both saves and losses for
artifact luck. The production-shaped architecture remains separate
legs, per-leg threshold/model calibration, and score/decision fusion.

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
- USB AEC3 sweep sessions: check "USB AEC3 sweep". The UI also keeps
  USB mic + reference selected because the variant engines are fed
  from the USB mic in this mode. Leave XVF/USB DTLN off during this
  sweep unless explicitly doing a neural-AEC side test; the Pi budget
  and listening matrix get messy fast.

**2026-05-29 pre-corpus runbook.** The tuning pilots answered enough to
stop broad AEC3 knob chasing, and the chip-AEC carve-out produced a
major positive result (details in
[`CHIP-AEC-EXPERIMENT.md`](CHIP-AEC-EXPERIMENT.md)). The earlier
`plug:jasper_capture` feeder path created misleading timing/drift
problems; direct source fanout to both the external DAC and XVF3800
USB-IN held about `~1 ppm` over 15 minutes and let the chip AEC produce
useful cancellation. The best lab output is category-7 ASR fixed gated
beams at `150°/210°`, with `AEC_AECEMPHASISONOFF=2`; the `150°` beam
was the standout listening/metric winner.

**1. Chip-AEC Option D — positive lab result, recorder pilot-ready.**
The recorder now has enough integration for intentional chip-AEC corpus
capture. The path is not the old feeder harness; it is direct source
fanout from outputd to both the physical DAC and the XVF3800 USB-IN
reference, then category-7 ASR fixed-beam capture on `150°` and `210°`.
Use the recorder's `chip_aec_comparison_v1` profile, not ad-hoc lab
WAVs, for any training/evaluation clips.

Decision:
- Include `chip_aec_150` and `chip_aec_210` in the next pilot corpus
  so mic orientation changes cannot silently erase the better beam.
- Include `xvf_raw0_webrtc_aec3`, `usb_raw`, and `usb_webrtc` in the
  same clips so software AEC3 vs chip AEC is comparable on one utterance.
- Treat `xvf_raw0_dtln` and `usb_dtln` as optional high-resource
  comparison legs; enable them for a short soak before relying on them
  during a long session.
- Do not record training clips from ad-hoc lab harness WAVs. They are
  useful for tuning, but not a clean corpus surface.

After any chip-AEC lab gate, verify production services recovered before
opening the wake-corpus page. On 2026-05-29, `jasper-aec-bridge`,
`jasper-voice`, and `SHF_BYPASS=1` were restored after the lab sweeps.

**2. Recorder known-good state (~10 min).** Start from production mode:
`jasper-voice` running, recorder-owned optional bridge outputs off, no
loaded/active session marker unless intentionally resuming. Then open
`http://jts.local/wake-corpus/`, begin a fresh session, and select only
the legs intended for the corpus:
- Raw mic 0: **on**.
- Chip-AEC comparison profile: **on** for the next pilot/gold-corpus
  candidate run. This forces raw0, outputd reference fanout, chip AEC
  `150°/210°`, XVF raw0 WebRTC AEC3, and USB raw/WebRTC AEC3.
- XVF DTLN (`dtln`): **off by default** in chip-profile runs unless we
  explicitly want the legacy ch1 neural comparison.
- XVF raw0 DTLN: **optional**; include for a short soak if the Pi remains
  stable.
- USB DTLN: **optional**; include it only if the Pi remains stable after
  a short corpus-mode soak.
- USB AEC3 sweep: **off** for the real corpus. Sweep sessions are pilot
  data, not Session A/B training or held-out data.

Use the page's corpus test-mode transition to apply the selected
optional outputs. Do not hand-edit `/var/lib/jasper/wake_corpus_bridge.env`
unless diagnosing a failure. Once test mode is entered, verify the page
shows the expected recorded legs and that the mic-level meter moves
green during a short spoken test.

**3. Fresh Session A recording (~45-60 min).** Start from a new session;
delete or ignore prior pilot sessions for training. With the chip profile
enabled, capture the comparison set (`chip_aec_150`, `chip_aec_210`,
`raw0`, `xvf_raw0_webrtc_aec3`, `ref`, `usb_raw`, `usb_webrtc`, and
optionally `xvf_raw0_dtln` / `usb_dtln`).
Record across the full 3 × 3 grid: quiet / ambient / music by near /
mid / far. The marginal far+music/air-conditioner cases matter, but the
model also needs easy cells so it learns the deployment distribution
rather than one corner case. Aim 7-9 utterances per cell (~63-81 total).

Immediately after Session A:
- rsync `/var/lib/jasper/enrollment_positives/` to the laptop;
- run `scripts/audit-wake-corpus.sh` with `--expect-raw0` and the
  expected optional legs;
- run wake-score/fusion analysis;
- listen to a small review queue before declaring Session A usable for
  training.

**4. Session B is held out forever.** Record Session B later, ideally a
different day or after a real break plus mic-position adjustment. Capture
~20-25 Jarvis positives across the same 3 × 3 grid and 30-40 hard
negatives. Never train on Session B.

**Bridge env for USB/reference corpus sessions:**

Preferred path: choose the optional corpus legs first, then use the
recorder's **Enter corpus test mode & begin session** button. That
single transition stops `jasper-voice`, writes the selected
recorder-owned bridge-output overrides to
`/var/lib/jasper/wake_corpus_bridge.env`, which
`jasper-aec-bridge.service` sources after `/etc/jasper/jasper.env`.
For chip-AEC comparison sessions the same env file is also sourced by
`jasper-outputd.service` and `jasper-aec-init.service`, because outputd
must fan out the direct chip reference and aec-init must put the XVF3800
into the volatile chip profile before recording.
If the bridge cannot restart with the requested optional outputs
(for example, the USB mic is missing), the recorder rolls that env
file back and restarts the bridge with the prior config. The selected
checkboxes are the desired test-mode state: stale optional outputs from
an earlier session are removed unless they are selected again.

When testing is done, use the wake-corpus page's **Exit corpus test
mode** button. It removes recorder-owned corpus output overrides from
`/var/lib/jasper/wake_corpus_bridge.env`, restarts the affected audio
daemons, and starts `jasper-voice`. This is intentionally a
recorder-page lifecycle, not a `jasper-doctor` warning: corpus outputs
are on while the operator is testing, off when they are not. DTLN
cleanup falls back to the reconciler's production wake-leg intent
instead of forcing the production DTLN leg off.

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

AEC3 sweep adds:

```sh
JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1
# New recorder sweep sessions use the cheap USB mic as the variant input.
JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb
# Required when source=usb:
JASPER_AEC_CORPUS_REF_ENABLED=1
JASPER_AEC_CORPUS_USB_ENABLED=1
```

Chip-AEC comparison adds:

```sh
JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1
JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED=1
JASPER_AEC_REF_SOURCE=outputd_udp
JASPER_AEC_OUTPUTD_REF_UDP_HOST=127.0.0.1
JASPER_AEC_OUTPUTD_REF_UDP_PORT=9891
JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0
JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891
JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE=16000
JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES=320
JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES=1280
# Optional DTLN on XVF raw0:
JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED=1
```

Ports default to `:9880` (`ref`), `:9881` (`usb_raw`), and `:9882`
(`usb_webrtc`), plus `:9884`-`:9886` for AEC3 sweep variants and
`:9887`-`:9890` for chip-AEC comparison legs. The
recorder can hide those ports with
`JASPER_WAKE_CORPUS_USB=0` if the bridge is running without them.
The bridge opens the USB mic at its PortAudio default sample rate
(the test mic reported 44.1 kHz) and resamples to the corpus contract
of 16 kHz mono before UDP emission.

**Capture-health metadata.** `jasper-aec-bridge` writes a low-cost
JSON counter snapshot to `/run/jasper/aec_bridge_stats.json`:
monotonic queue-drop counts, UDP send-drop counts, packet counts per
leg, and reference-starvation count for the current bridge process.
The recorder snapshots that file at clip start/stop and stores the
delta under each clip's `capture_health` metadata. Status meanings:
`clean` = no known provenance damage, `warning` = usable but review
the note (for example stale reference reuse or duration skew),
`compromised` = upstream drops/restart/no packets, and `unknown` =
bridge stats were unavailable. The audit script treats compromised
clips as failures and warning/unknown clips as review warnings.
For AEC3 sweep clips, `capture_health.aec3_sweep_source` records
whether variant legs inherit XVF mic queue-drop counters or USB mic
queue-drop counters; all AEC/DTLN variants still inherit reference
drop/starvation counters.

**Production-profile metadata.** As of 2026-06-01, new wake-corpus
session sidecars include `metadata_schema_version=2` and an additive
`audio_context` snapshot. That context records production AEC intent
from `/var/lib/jasper/aec_mode.env`, reconciler-applied runtime env
from `/etc/jasper/jasper.env`, the classified production audio profile
from `jasper/audio_profile_state.py`, XVF3800 mic identity/firmware
channel facts, selected corpus profile/legs with `jasper/wake_legs.py`
kind + wake-input semantics, outputd/DAC/reference env, and optional
validation-artifact status from
`/var/lib/jasper/audio_validation/latest.json` when a future validation
stream writes it. Each new clip also stores its session `selected_legs`
and the same `audio_context` snapshot beside `capture_health`, so a clip
copied out of band still carries enough profile truth to interpret it.
Old sessions without these fields remain valid; loaders and the audit
script treat missing `audio_context` as historical metadata, not a
failure.

**Reference-quality follow-up.** The current `ref` leg is intentionally
the exact 16 kHz mono frame the live AEC consumes. A future
`ref_fullband` archival leg could preserve the original full-band
speaker reference before downsampling, but that changes the bridge,
recorder, and audit surface. Track it as a post-corpus follow-up rather
than changing the main recording protocol tonight.

**Sessions management UX (PR #323, refined 2026-05-27).** The
recorder's collapsible Sessions card sits below the new-session setup
and lists every recorded session (name/member, timestamp, clip count,
condition breakdown, leg indicators). Each row has Load (resume) +
Delete (hard-remove WAVs + JSON) buttons. Load makes the selected
session the in-memory recording target but does not enter corpus test
mode; the page then offers to enter test mode using that loaded
session's saved leg selection. The active append target is tracked by
a narrow `.active_session.json` marker, not by "newest recent metadata";
exiting corpus test mode or pressing Unload clears the marker and
returns the page to a fresh new-session form. Cleanup of pre-raw0
sessions = one click each before starting fresh.

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
  from raw/DTLN legs and Reference is listed last
- 2026-05-27 tuning pilot — corpus-only AEC3 sweep mode adds three
  same-utterance WebRTC AEC3 variants on `:9884`-`:9886`
- 2026-05-27 evening tuning pass — AEC3 sweep variants retargeted to
  the HF-preservation 2×2: `aec3_hf_relaxed`,
  `aec3_hf_mask_upstream`, and `aec3_hf_wide_open`
- 2026-05-27 late tuning pass — AEC3 sweep variants retargeted to
  edge-preservation under far+music: `aec3_hf_relaxed`,
  `aec3_nearend_fast`, and `aec3_slow_attack`
- 2026-05-27 edge-combo pass — AEC3 sweep variants retargeted to
  test the combined promising direction: `aec3_hf_relaxed`,
  `aec3_edge_combo`, and `aec3_slow_attack`
- 2026-05-27 DND isolation pass — AEC3 sweep variants retargeted to
  isolate DND effects: `aec3_hf_slow_only`, `aec3_edge_combo`, and
  `aec3_gentle_dnd`
- 2026-05-28 BEST_A ablation pass — session
  `20260528T140258Z-35c4`, 21 far+music marginal clips, config hash
  `5782b4d229e8`. Compared BEST_A against NS-off, AGC1-off, and
  NS+AGC1-off. All four AEC3 variants landed at 16/21 individually,
  XVF raw landed at 17/21, AEC-only union was 17/21, and AEC +
  XVF raw/raw0 union was 21/21. NS-off rescued clips such as #7 but
  regressed clips such as #14, so it is useful as a fusion direction
  rather than an obvious single-chain replacement.
- 2026-05-28 edge-family fusion pass — session
  `20260528T141727Z-28bf`, 46 far+music marginal clips, config hash
  `97980f2b1971`. Compared BEST_A, Edge, Edge NS-off, and Edge
  NS+AGC1-off. Individual hits were 30/46, 32/46, 32/46, and 33/46;
  AEC-only union was 38/46. The practical three-leg set is BEST_A +
  Edge + Edge NS-off (38/46 at threshold 0.5). For two AEC3 legs,
  BEST_A + Edge NS-off is the current level-stable fusion pick; BEST_A
  + Edge NS+AGC1-off maximizes strict 0.5 union but is quieter.
- 2026-05-28 DTLN/USB side finding — offline DTLN-256 on session
  `20260528T131605Z-9708` did not dethrone AEC3, but it produced
  complementary saves. XVF AEC union was 25/64, XVF AEC + XVF DTLN
  was 28/64, USB existing legs union was 15/64, and USB existing +
  USB DTLN was 19/64. Keep DTLN as a comparison / possible fusion
  leg, but do not mix it into AEC3 sweep sessions on the 1 GB Pi.
- 2026-05-28 latest DTLN-256 pass — session
  `20260528T141727Z-28bf`, 46 far+music marginal clips, using the
  captured ref. Tested `dtln_off` from XVF off/raw chip ch1,
  `dtln_raw0` from XVF raw0/ch2, and `dtln_usb_raw` from USB raw. At
  threshold 0.5: existing XVF AEC3 sweep union was 38/46; `dtln_off`
  was 29/46; `dtln_raw0` was 28/46; `dtln_usb_raw` was 27/46; all
  DTLN projections unioned to 36/46; XVF AEC3 + XVF DTLN unioned to
  41/46; and existing everything + DTLN unioned to 42/46. DTLN-only
  saves over all existing recorded legs were clips 17 (`dtln_off=0.986`)
  and 28 (`dtln_raw0=0.761`); clip 1 was AEC-missed and saved by
  `dtln_raw0=0.996`, but USB raw had already caught it. USB existing
  union was 25/46; USB existing + USB DTLN reached 32/46, with DTLN
  saves on clips 8, 10, 11, 16, 19, 22, and 23. DTLN outputs were
  much quieter than main AEC legs (median RMS about -38.5 dBFS
  `dtln_off`, -40.7 dBFS `dtln_raw0`, -31.9 dBFS `dtln_usb_raw` vs
  about -29 dBFS main AEC legs), so normalize levels or calibrate
  per-leg thresholds before treating DTLN as production fusion.
- 2026-05-28 DTLN noise-suppression offline pass — same session,
  upstream DTLN-NS (`breizhn/DTLN`) model. DTLN-NS did not improve
  the main candidate legs overall: raw-input NS union was 29/46,
  XVF AEC3 post-filter NS union was 29/46 versus original AEC3 union
  38/46, and DTLN-aec→NS union was 34/46 versus DTLN-aec union 36/46.
  It did add one new save over existing recorded legs + DTLN-aec:
  clip 25 via `ns_dtln_raw0=0.883`, raising the everything+DTLN+NS
  union to 43/46. Keep as offline research, not live test-mode scope.

Recorder UX status:
- ✅ One-click record, click-again-stop, spacebar hotkey
- ✅ Live mic-level meter (SSE, ~12 Hz when recording, ~2 Hz idle)
- ✅ 3 conditions: quiet / ambient / music
- ✅ 3 distances: near / mid / far
- ✅ Per-session raw-mic-0 toggle (4 legs vs 3)
- ✅ Per-session USB/ref toggle for corpus-only cheap-mic experiments
  (`ref`, `usb_raw`, `usb_webrtc`)
- ✅ Per-session USB AEC3 sweep toggle for pilot tuning: XVF AEC3
  reference + USB raw/reference + USB WebRTC baseline + three
  runtime-labeled USB-fed AEC3 variant slots (`aec3_variant_1`-
  `aec3_variant_3`), with effective labels/source/config/hash stored
  in the session sidecar. Legacy/manual XVF-fed sweep sessions still
  load via `aec3_sweep_source=xvf`.
- ✅ Per-session chip-AEC comparison profile:
  `chip_aec_150`, `chip_aec_210`, `raw0`, `xvf_raw0_webrtc_aec3`,
  `ref`, `usb_raw`, `usb_webrtc`, and optional `xvf_raw0_dtln` /
  `usb_dtln`. Entering the profile restarts outputd/aec-init/bridge
  into the reversible corpus state; exiting removes those overrides.
- ✅ Sessions card: list all sessions, Load (resume), Delete (with
  confirm); collapsible and below new-session setup
- ✅ Per-cell counts matrix + recorded-clips list with HTML5 audio
  playback selector for every WAV recorded on a clip
- ✅ Playback selector labels WebRTC paths as WebRTC AEC3 and puts the
  speaker Reference leg last
- ✅ Corpus test-mode transition wired: selected optional legs are
  applied before session creation; exiting disables recorder-owned
  bridge outputs, restarts `jasper-voice`, and unloads the session
  after clearing stale systemd start-limit state for the intentional
  bridge restart
- ✅ Loaded sessions show as loaded (not newly active) and can enter
  corpus test mode using their saved leg set
- ✅ Recent metadata is not auto-loaded after a graceful exit; crash
  recovery only reattaches when the active-session marker remains

Recording-day audit tooling:
- ✅ `bash scripts/audit-wake-corpus.sh data/enrollment_positives
  --expect-raw0` validates post-rsync session metadata, raw0 leg
  presence, condition × distance coverage, and WAV format/RMS.
- ✅ `--expect-leg ref --expect-leg usb_raw --expect-leg usb_webrtc`
  and `--expect-leg usb_dtln`
  validates USB/reference opt-in sessions after rsync.
- ✅ Chip-profile sessions can be audited by expecting the explicit
  recorded legs (`chip_aec_150`, `chip_aec_210`,
  `xvf_raw0_webrtc_aec3`, `ref`, `usb_raw`, `usb_webrtc`, plus any
  selected DTLN legs).
- ✅ `scripts/_waveform_fusion_experiment.py` generates offline
  AEC3+DTLN waveform mixes across delay/weight grids and checks whether
  any mix beats same-pair score fusion. Treat it as research evidence,
  not a production path.

**Phase −1 (pre-foundation verifications): in progress.**
- −1a (LLM session routing): investigation results in PR cover
  letter / next session message.
- −1b (Tang 2020 verification): DONE 2026-05-25. Findings folded
  into Phase 1c. See v4 changelog entry.

**Phase 0a (offline harness): not started.** Gated on Phase −1.

**Phase 0b (gold corpus capture): tooling READY, recording PENDING.**
Multiple 2026-05-27/28 tuning pilots are recorded and useful as
analysis data, but they should not be treated as the clean training /
held-out split. The next real milestone is a fresh Session A recording
using the 2026-05-29 production plan above, followed by a separate
Session B held-out + hard-negative recording. Cleanup of pre-raw0
corpus is one-click per old session via the Sessions card.
Recorder-managed corpus bridge outputs can be enabled/disabled through
the page's corpus test-mode transition, and per-clip metadata records
capture-health deltas from the bridge where available. New session
metadata also records `audio_context` so the lab corpus can be grouped
by production profile, mic firmware/channel state, selected leg kinds,
and DAC/reference validation status once that validation stream exists.

**Phase 0c (baseline): pending Phase 0a + 0b.**

---

## Changelog

- **2026-06-01 (v31):** Corpus / onboarding reuse metadata:
  - New wake-corpus sidecars write `metadata_schema_version=2` plus
    `audio_context` with production profile intent/runtime truth,
    XVF3800 mic identity and firmware channel state, selected-leg
    details from `jasper/wake_legs.py`, DAC/reference env, and optional
    validation-artifact status.
  - New clips carry `selected_legs` and the same `audio_context`
    snapshot beside existing `capture_health`.
  - `scripts/audit-wake-corpus.sh` now understands the chip-AEC
    comparison legs through the shared leg registry and prints profile /
    mic / validation summaries when present, while accepting older
    sessions without these fields.
- **2026-05-29 (v28):** Chip-AEC lab result folded in:
  - Updated the pre-corpus runbook from "partial ch0 positive" to
    "positive lab result, recorder pilot-ready."
  - Captured the direct-source-fanout finding: the old feeder path was
    the timing/drift problem; direct DAC + XVF3800 USB-IN fanout held
    about `~1 ppm` over 15 minutes.
  - Recorded the current chip-AEC candidate leg for future corpus
    design: category-7 ASR output, fixed gated `150°/210°` beams,
    `AEC_AECEMPHASISONOFF=2`, with `150°` as the standout beam.
  - Added the recorder chip-AEC comparison profile: outputd direct
    reference fanout, volatile chip profile via `jasper-aec-init`,
    bridge UDP legs for chip `150°/210°`, XVF raw0 WebRTC AEC3, and
    optional XVF raw0 DTLN.
- **2026-05-28 (v26):** Waveform-fusion and next-recording plan:
  - Documented the offline `scripts/_waveform_fusion_experiment.py`
    harness and first `20260528T184424Z-d205` result: best XVF
    `on + dtln` waveform mix hit 20/27 and added clip 15 over the
    full original-leg union, but score/decision fusion remains the
    production-shaped architecture.
  - Added the 2026-05-29 pre-corpus runbook: run a bounded chip-AEC
    Option D gate first, restore production, put the wake-corpus page
    into a known-good state, record a fresh Session A with XVF + raw0 +
    USB/reference comparison legs, then keep Session B held out forever.
- **2026-05-28 (v25):** USB-fed AEC3 stream-delay sweep mode:
  - New recorder-created AEC3 sweep sessions set
    `JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb`, automatically include
    USB/reference companion legs, and keep the XVF `on` leg as the
    same-utterance AEC3 reference.
  - Current built-in USB sweep compares one utterance across edge-combo
    USB AEC3 delay hints: `usb_webrtc=40 ms`, `aec3_variant_1=80 ms`,
    `aec3_variant_2=120 ms`, and `aec3_variant_3=160 ms`.
  - Clarified stable variant slots now carry explicit input-source
    metadata; old/manual sessions without the source flag remain
    XVF-fed for backward compatibility.
  - Capture-health notes now distinguish XVF mic queue drops from USB
    mic queue drops for AEC3 variant legs.
- **2026-05-28 (v23):** Latest DTLN-256 analysis folded in:
  - Added the `20260528T141727Z-28bf` offline DTLN pass against 46
    far+music marginal clips using captured reference audio.
  - Documented `dtln_off`, `dtln_raw0`, and `dtln_usb_raw` threshold
    0.5 hit counts, union values, DTLN-only saves, USB-side saves,
    and the level-normalization / per-leg-threshold caution.
  - Added the fixed make-up gain sweep result: DTLN's lower RMS is
    real, but blind positive gain did not materially improve wake
    recall on this session.
  - Added the DTLN noise-suppression offline result: one new
    everything+DTLN save, but broad regressions versus tuned AEC3 and
    DTLN-aec mean it stays offline research only.
- **2026-05-28 (v22):** XVF AEC3 fusion tuning pilot documented:
  - Added the current fusion-first interpretation of the AEC3 sweep:
    optimize union hits and unique saves, not just top single-leg hit
    count.
  - Recorded the 2026-05-28 BEST_A ablation, edge-family, and
    DTLN/USB side-test findings with session IDs, config hashes, hit
    counts, and current two-/three-leg AEC3 recommendations.
- **2026-05-27 (v19):** Corpus test-mode bridge restart safety:
  - The recorder clears stale `jasper-aec-bridge` systemd start-limit
    counters before an intentional corpus-output restart, so rapid
    deploy/test-mode toggles do not accidentally trip the critical
    daemon `StartLimitAction=reboot` ladder.
  - Mixed state (`jasper-voice` running while recorder-owned bridge
    outputs remain on) labels the loaded-session action as resuming
    recording instead of starting a new corpus session.
- **2026-05-27 (v15):** AEC3 same-utterance sweep:
  - Added corpus-only AEC3 sweep mode. When selected, the bridge runs
    three additional warmed WebRTC AEC3 instances in parallel with the
    baseline `on` leg and emits them on `:9884`-`:9886`.
  - First-pass variants are NS off, residual `default_gain=0.8`, and
    relaxed high-frequency suppression. Treat sessions with these legs
    as pilot tuning data, not Session A/B train/eval data.
- **2026-05-27 (v14):** USB note removal:
  - Removed the visible USB raw / hardware Auto Gain Control status note
    from the recorder page. The backend diagnostic endpoint remains, but
    the page no longer polls it or shifts layout while the operator is
    setting up a recording session.
- **2026-05-27 (v13):** Fresh-state session UX:
  - Added an explicit active-session marker for crash recovery so
    recent historical metadata no longer makes a new page visit look
    like a loaded session.
  - Exiting corpus test mode now unloads the session after returning
    bridge outputs and `jasper-voice` to production mode. A separate
    Unload button clears a loaded session without deleting WAVs.
- **2026-05-27 (v12):** Loaded-session UX:
  - Clarified Load semantics: loading a session selects it as the
    recording target but does not stop `jasper-voice` or enable corpus
    bridge outputs.
  - Sessions card moved below setup, made collapsible, and loaded
    sessions now offer a separate enter-test-mode action using saved
    leg selections.
- **2026-05-27 (v11):** Corpus test-mode UX:
  - Replaced separate voice/start-stop and return-to-production controls
    with a single recorder-page mode transition. The operator selects
    optional legs first, enters corpus test mode to stop `jasper-voice`
    and apply those bridge outputs, then exits test mode to disable
    recorder-owned outputs and restart `jasper-voice`.
- **2026-05-27 (v10):** Capture-health + corpus bridge lifecycle:
  - `jasper-aec-bridge` emits monotonic per-leg packet/drop counters
    to `/run/jasper/aec_bridge_stats.json`; the recorder diffs those
    counters at clip start/stop and stores `capture_health` in session
    metadata.
  - Wake-corpus audit surfaces compromised capture health as a failure
    and warning/unknown capture health as review warnings.
  - Wake-corpus page now has a recorder-owned corpus test-mode flow:
    optional corpus legs are selected before entry, entry stops
    `jasper-voice` and applies the desired bridge outputs, and exit
    removes recorder-owned overrides before starting `jasper-voice`.
- **2026-05-27 (v9):** Wake-corpus recording-day polish:
  - Playback labels now say WebRTC AEC3 for the WebRTC AEC paths and
    keep the speaker Reference leg last in the clip selector.
  - UI labels clarify that USB capture is paired with the 16 kHz
    reference leg consumed by AEC.
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

Last verified: 2026-06-01 (v31 — wake-corpus metadata schema and audit
contract rechecked against `jasper/web/wake_corpus_setup.py`,
`jasper/wake_legs.py`, and `scripts/_audit_wake_corpus.py`; validation
artifact production remains a future stream.)
