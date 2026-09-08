# Active speaker tuning — layer model

This document explains the layer boundaries and fitting rationale. Start with
[tuning-operator-runbook.md](tuning-operator-runbook.md) for a tuning session.
[measurement-loop-doctrine.md](measurement-loop-doctrine.md) owns the experiment
contract; [tuning-methodology.md](tuning-methodology.md) is optional scientific
context. Code owns numerical bounds and supported measurement programs.

The former seed → linearize → crossover → verify pipeline is superseded by the
operator-led experiment loop. These layers describe different jobs, not a
mandatory order of measurements, analysis, or application. A missing optional
analysis limits a claim; it does not prevent an otherwise admissible trial.

## The five layers

| Layer | Job | Evidence |
|---|---|---|
| 1a — Driver linearization | Shape each driver's response in its useful band | Driver measurements with their gate, calibration, and pose limits |
| 1b — Crossover integration | Set crossover filters, scalar trim, delay, and polarity so drivers sum as intended | Complex solos and measured sums in a common geometry |
| 2 — Bass | Extend bass or integrate a subwoofer | Evidence suited to the specific extension or in-room integration question |
| 3 — Room correction | Reduce repeatable room deviation | Reverberant measurements across listening positions |
| 4 — Preference | Express the listener's chosen balance | Listening judgement |

The speaker layer travels with the speaker; Room correction belongs to its room
and placement. A direct-sound measurement and an in-room spatial average answer
different questions. Neither should silently stand in for the other. A flat
on-axis curve alone does not establish flat sound power or good off-axis sound.

These numbers are not signal order. Room and preference act before the driver
split in the composed graph; see [audio-paths.md](audio-paths.md). Shape belongs
in filters, level in scalar trim. Keeping those facts separate lets a comparison
show what actually changed.

## Declared driver facts and correction ownership

**Decisions 8–9 — one declared low limit.** A manufacturer's minimum crossover
frequency and any slope condition remain declared facts. Consumers derive their
bounds from that declaration; they do not store a second version of the same
limit. An estimate must remain an estimate, never a manufactured datasheet fact.
A corner at the declared low limit is allowed when its slope condition is met.
[ADR-0227](adr/0227-owner-rulings-the-prose-pass-surfaced.md) records the current
rule; the declaration and its consumers live in
[driver_protection.py](../jasper/active_speaker/driver_protection.py).

**Decision 10 — the blend belongs to the sum.** A solo driver's magnitude does
not identify the cause of a dip made by two drivers. The
[region-based adjustment contract](#the-region-based-adjustment-contract)
separates per-driver shape, summed correction, and relative alignment.

Integration and flatness are separate claims. Tracking a predicted sum over a
crossover band does not prove flatness across the speaker's full measured band.
A simulated improvement is a prediction until a suitable capture tests it.

## Layer 1a concretely

The default fitter in
[linearization_fit.py](../jasper/active_speaker/linearization_fit.py) uses
measured response and the correction envelope below to propose driver EQ. Its
selection policy is one tool. It does not define every experiment an external
prescriber may author. Missing classification, a sparse pose set, or uncertain
mic response must remain visible without becoming a universal authoring veto.

When changing delay, polarity, or crossover structure, examine whether existing
EQ compensates the old sum. Keeping that EQ can hide the structural error or
produce double correction. [ADR-0203](adr/0203-the-incumbent-tune-retires-recommissioning-is-structure-first.md) explains
the measured case; it does not impose a new fixed campaign on all speakers.

### The correction envelope

[linearization_envelope.py](../jasper/active_speaker/linearization_envelope.py)
owns the fitter's per-frequency allowance. Its terms combine microphone trust,
repeat agreement, driver class, and optional spatial exclusion and position
stability evidence. The smallest applicable allowance
limits the default fit. Read the returned reasons and the code-owned caps rather
than treating this document as a second table of numbers.

The envelope and optional diagnostics answer different questions:

- Calibration and microphone trust bound what the instrument can resolve.
- Repeat agreement reveals variation between captures, not every common error.
- A level-dependent response may be nonlinear; an inverse filter does not fix
  that mechanism.
- Interference dips and angle-dependent features may worsen elsewhere when
  boosted on one axis.
- A driver class is a prior about likely behaviour, not a measured response.

The former P3 rule 1 tied all filter eligibility to feature classification.
Current classification is optional evidence. Its result can constrain the
fitter's recommendation without making an unclassified operator trial illegal.
The doctrine owns the distinction between advice and physical limits.

Gated far-field response can resolve direct-sound shape only within its useful
band. Near-field data can help with low-frequency magnitude, but does not by
itself establish baffle radiation, a valid splice, or relative driver timing.
See the [measurement limits](active-crossover-information-design.md#measurement-validity-gating-and-the-low-frequency-floor).

### CD-horn compensation — the top-octave HF stage

A falling horn response can be consistent with constant-directivity behaviour.
That is a hypothesis to test with the mounted driver's measurements, not a
conclusion supplied by its class label. The fitter's `_hf_continuation_stage`
sizes its correction from the measured deficit and remaining allowance. Repeat
agreement, microphone confidence, and realization limits bound that default
proposal. Code owns their exact values.

A cut-domain realization attenuates the lower band so the top octave rises
relative to it. This can produce the desired relative shape without raising a
device volume ceiling. Normalization spends maximum output headroom; it is not
an equal loss of ordinary listening level. Disclose the cost and any deficit
left uncorrected by the realization limit.

Above the measured confidence ceiling, the fitter's declared-type continuation
policy holds or tapers the correction. That extension is a disclosed model
choice, not measured performance. Missing high-frequency evidence cannot become
a flatness claim.

### Anchored give-back (the trim)

A trim adjustment must use the same level frame as the trim it changes. Each
branch starts from its committed raw trim and adds its measured before/after
level change over `branch_level_bands_hz`, using the estimator that
`solve_branch_trims` and `realized_level_match` share. A common shift then
normalizes the pair to non-positive trims.

A correction's own core-band delta is not interchangeable with this crossover
level-band delta. Mixing those frames can leave the branches unequal even when
the arithmetic appears to restore their levels. A branch can legitimately end
hotter than before, while still within the non-positive clamps.

Ripple optimization has a different objective from equal band-average levels.
A ripple-polished base therefore needs its disclosed offset from the band-level
anchor. The implementation bounds that choice and can fall back to the anchor;
read its result rather than assuming any polished trim is the raw level solve.

## The region-based adjustment contract

There are three different ways to address a crossover-region feature:

1. Per-driver EQ changes the driver's own useful-band response. Its solo fit
   cannot decide how two branches interfere.
2. Summed blend correction acts on the measured combined response. The current
   [blend_correction.py](../jasper/active_speaker/crossover_v2/blend_correction.py)
   fitter proposes cuts within its own headroom and realization policy.
3. Relative trim, delay, polarity, and crossover choices change how the drivers
   combine. A dip caused by cancellation may call for one of these changes
   rather than an inverse magnitude filter.

A result should name the part it changed and distinguish predicted from
measured effect. Failure of one blend-correction trial does not prove the
feature cannot be changed by another parameter or measurement geometry.

## The prescriber seam

The external LLM chooses the question, optional analysis, and candidate. The
operator controls placement, starts sound-producing measurement, and decides
whether to continue or adopt. Code parses values, composes the graph, enforces
physical bounds, and reports results. The current
[runbook](tuning-operator-runbook.md) and command help own that route.

Apply confirms the graph write and runtime readback. A new acoustic capture is
a separate experiment; an earlier optional analysis is not its permission slip.
There is no prescribed number of rounds or compulsory classifier pass.

## Measurement geometry and composition

[measurement_programs.py](../jasper/active_speaker/measurement_programs.py)
owns supported programs. Use `jasper-angle-capture plan` to inspect an angle
walk and its cost before staging it. Same-pose repeats estimate repeatability;
distinct angles test spatial behaviour. Increasing one cannot replace the other.

Crossover capture uses a microphone connected to the Pi. A USB microphone and
DAC do not acquire a shared sample clock merely by sharing a computer. Timing
claims need the evidence the analysis requires. [Room](room-correction-information-design.md)
still captures through the local browser over HTTPS. The crossover phone relay
was retired in ADR-0222.

The [measurement engine](../jasper/active_speaker/crossover_v2/session.py) owns the common
session lifetime. Domain tools retain analysis, fitting, and application policy.
Layer separation does not require a second capture host or an automatic phase
machine.

## Microphone doctrine

Use the microphone's actual calibration identity and orientation. A serial-
specific calibration file reduces instrument error; a class label does not
create that calibration. The `reference`, `consumer`, and `phone` tiers describe
the default fitter's confidence policy, not universal permission to experiment.

### Cold-start priors

The microphone and driver-class tables live in `linearization_envelope.py`.
They are heuristics for a first proposal. Measured repeatability and spatial
coverage retain their separate meaning; a repeated systematic error can look
stable. Do not copy the tables into another operating guide.

## Sources

The original [driver-linearization research](research/2026-07-23-driver-linearization/README.md)
is retained as research. Dated decisions remain in [adr/](adr/README.md).
For interpretation of gate floors, distortion, repeatability, and directivity,
use [tuning-methodology.md](tuning-methodology.md) when the question needs it.
