# Research: Mic-driven active-crossover calibration & the shared audio core

> **Status: research artifact.** Snapshot from 2026-06-19 produced by two
> multi-agent research/design workflows plus a live JTS3 probe. Preserved
> for traceability and source links. **Current operational/design truth
> lives in the canonical handoff** (see "Where the canonical truth lives"
> at the bottom) — this file is the archaeology behind it, not the live
> spec. Specific facts (file paths, "what's built" snapshots) will drift.

## Why this exists

The maintainer wants to programmatically dial in an **active crossover**
(bi-/tri-amped speaker, per-driver DAC channels, CamillaDSP filters) using
the **phone-mic measurement flow already built for room correction**, and
to do it as a **productized, layered** feature anyone can use. This is the
research that scoped it. Two questions drove it:

1. The lab speaker (JTS3) "sounds shrill / horn far too powerful vs the
   woofer." Is that just **level matching** between drivers, or something
   more?
2. Do we even need a **calibrated mic**, or can a plain **uncalibrated
   iPhone mic** do the level-matching job?

## Bottom line (the two adjudicated questions)

**1. "Is it just level matching?" — Half right.**

| What you hear | Region | Most likely cause | Fixed by level trim alone? |
|---|---|---|---|
| Shouty / forward / shrill | ~1–5 kHz presence | Tweeter run too hot | **Yes** |
| Nasal / honky | ~300 Hz–2 kHz | Missing baffle-step compensation, woofer breakup, or tweeter crossed too low | **No** — needs EQ / different Fc |
| Hollow / thin at crossover | Fc ± ½ oct | Phase/delay/polarity misalignment (a dip, not a flat sum) | **No** — needs delay/polarity |

So broadband level matching fixes "shouty/shrill" but **not** "nasal"
(that's a midrange/baffle-step or crossover-region problem). An
LLM-designed crossover from datasheet specs will not have modeled the
specific baffle, so a baffle-step rise sails through as real coloration.

**2. "Calibrated mic vs iPhone?" — Uncalibrated iPhone is good enough for
level matching, not for phase/FR.** Relative level is a *ratio* at one mic
position; the mic's unknown sensitivity and the room both apply to both
drivers and largely cancel **in the crossover overlap band**. Expect
±3–6 dB. Mandatory guardrails: disable AGC/EC/NS (the shared
`measurement-audio.js` already hard-codes these off), don't move the phone
between driver captures, compare in the overlap band, average several
captures. A calibrated mic (the maintainer's Dayton USB-C; reuse
`jasper/correction/calibration.py` upload path) is required for
precision FR and phase/delay/null-depth work — phase error on an
uncalibrated phone is ±20–40° at Fc.

## JTS3 live diagnosis (2026-06-19, ~17:00 ET)

**The crossover is not live.** This is the obvious reason it sounds wrong:

- Output hardware: **HiFiBerry DAC8x** (8 outputs, `status: ready`).
- Live CamillaDSP graph (`/etc/camilladsp/v1.yml` and the outputd-topology
  `outputd-cutover.yml`) is a **flat identity passthrough** — `master_gain`
  at 0 dB → `flat` → `flat`. No crossover filters, no per-driver gain, no
  EQ. `outputd-cutover.yml` self-documents: *"This flat graph maps
  full-range stereo directly to DAC outputs. It is illegal when saved
  output topology assigns any physical output to tweeter/protected role."*
- The active-speaker commission state (design draft, crossover preview,
  baseline profile, measurements, dated Jun 18) was **staged but blocked /
  never cut over**, and was reset/wiped during the probe (jts3 redeployed
  2026-06-19 12:57 on branch `claude/jts3-latest-main`; another session
  is actively churning it).
- Driver pair: woofer = **Dayton Epique E150HE-44** (83.3 dB/2.83V),
  high-frequency = **B&C DE250-8 compression driver** on a 3D-printed
  Le Cleac'h horn (108.5 dB, normalized to 2.83V/1m at 8 Ω) =
  **~25 dB sensitivity gap**. Full-range, equal-level signal to both → the
  compression driver gets midrange/presence energy it should never see,
  ~25 dB hot → "shrill, horn far too powerful." Exactly the symptom. (The
  driver research config confirms this: it explicitly says apply ~−25 dB
  attenuation on the tweeter channel *before any signal*.)

Caveats: exact current output→driver channel routing wasn't pinned (state
resetting under the probe). **Safety:** full-range into a compression
driver with no high-pass is a tweeter-damage risk at volume — do not push
level until the crossover + protective HP are live.

This validates the layered product floor: **L0 = the crossover is actually
applied, fail-closed** (today it isn't), then **L1 = level-match the
drivers**.

## Codebase reuse & gap analysis (verified against code)

**Reusable plumbing (high confidence):**
`deploy/assets/shared/js/measurement-audio.js` (mono 48k, AGC/EC/NS off);
`jasper/correction/` — `sweep.py` (Novak ESS), `deconv.py` (FFT IR),
`analysis.py` (octave smoothing / log resample / band normalize),
`calibration.py` (mic-cal lookup/upload), `coordinator.py`
(`measurement_window`: pauses renderers + voice; correction/balance/sync
globally mutually exclusive), `confidence.py`, `acoustic_quality.py`,
`browser_audio.py`, `autolevel.py`, `session.py` (state-machine pattern),
durable evidence bundles.

**The trap — coded but DISCONNECTED (called only by tests, no production
caller):**
- `jasper/active_speaker/driver_acoustics.py` — `analyze_driver_capture`,
  `analyze_summed_crossover` (fully written, never invoked in prod).
- `jasper/active_speaker/commissioning_capture.py` —
  `record_driver_acoustic_capture` (bridge exists, only tests call it).
- `jasper/active_speaker/measurement.py` — *accepts* an acoustic verdict
  dict but nothing computes it.
- `DriverSpec.sensitivity_db` is **stored but never read to set gain**;
  per-driver gain comes only from a caller-supplied `corrections` dict,
  default 0 dB. No datasheet-sensitivity→trim path exists.

**Scoped out:** the LLM `jasper/calibration_agent/` is explicitly limited
to room-correction PEQ; its prompt forbids active-speaker work → **crossover
calibration must be deterministic, not LLM-driven.**

**Net-new:** the per-driver sweep→measure→trim endpoint + UI; the trim
algorithm (overlap-band dB delta → gain); trim persistence + re-freeze; a
`measurement_mode` (magnitude_only vs phase_aware) so an uncalibrated
measurement can never authorize a phase/delay decision; crossover-tuned
confidence thresholds. Integration point = active-speaker commissioning
**"Stage 6"** (per-driver sweep + acoustic verdict), today subjective
("what did you hear?").

## Effort (indicative)

Phase 0 spike ~1 day (≈150-line CLI: play band-limited ESS per role →
capture via existing pipeline → print proposed trim). Phase 1 MVP no-mic
level match ~2–3 wk. Phase 2 persist/re-freeze ~1 wk. Phase 3 blend /
null-depth (calibrated tier) ~3–4 wk. DSP is highly reusable; the
crossover **workflow** (orchestration + algorithm + UI + persistence) is
substantially net-new.

## Layered product vision (maintainer)

- **L0** — crossover + protective HP applied, fail-closed. (Foundational.)
- **L1** — uncalibrated phone-mic woofer↔tweeter level match. "Anyone."
- **L2** — calibrated mic (Dayton USB-C) for FR / phase / null-depth.
- Multi-volume: see the multi-volume verdict in the canonical doc — driver
  level matching is a fixed, level-independent trim (one number), whereas
  perceived tonal change with volume is *loudness compensation*, a separate
  optional feature. Don't conflate.

The deeper goal: consolidate the audio subsystem into a **shared
measurement/calibration core** that room correction, active-crossover
calibration, AND pair/leader-follower balance all build on — clean
separation of concerns, resilient, performant. (`balance-sync-calibration.md`
already says "reuse the phone/browser mic model from balance/correction
work.") The input-side `HANDOFF-audio-capability-platform.md` is the
adjacent sibling (mic/AEC/DAC hardware capability), not the same thing.

## Methodology

Two background Workflow runs on 2026-06-19:
- `active-crossover-calibration-assessment` (10 agents) — codebase
  understanding, external acoustics research, adversarial verification,
  staff synthesis. Answered the two questions above.
- `audio-core-platform-and-refactor-design` (7 agents) — shared-core
  consolidation map, volume/multi-volume architecture, layered-product +
  refactor design. Feeds the canonical doc.

External sources surveyed: REW measurement/timing docs, miniDSP+UMIK
guides, Sonos Trueplay / Dirac / Genelec GLM / Audyssey tiering,
diyAudio/Audioholics crossover-integration threads, ISO 226 / Fletcher-
Munson equal-loudness, Linkwitz/Toole/Vanderkooy. (Per-source links are in
the workflow transcripts under the session's `subagents/workflows/`.)

## Where the canonical truth lives

The shipped/operational design (target shared-core architecture, layered
product spec, multi-volume verdict, safe refactor roadmap, decision
points) is the **canonical handoff**:
[HANDOFF-audio-measurement-core.md](../../HANDOFF-audio-measurement-core.md).
Backing safety/DSP contracts remain canonical
in [HANDOFF-active-speaker-dsp.md](../../HANDOFF-active-speaker-dsp.md),
[HANDOFF-correction.md](../../HANDOFF-correction.md), and
[active-crossover-information-design.md](../../active-crossover-information-design.md).

Last researched: 2026-06-19
