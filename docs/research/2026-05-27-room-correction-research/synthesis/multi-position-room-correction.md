# Multi-Position Room Correction Confidence - Synthesis

> **Status: research synthesis.** Distilled from
> [`../raw/multi-position-room-correction-chatgpt.md`](../raw/multi-position-room-correction-chatgpt.md),
> [`../raw/multi-position-room-correction-claude.md`](../raw/multi-position-room-correction-claude.md),
> and
> [`../raw/multi-position-room-correction-gemini.md`](../raw/multi-position-room-correction-gemini.md)
> on 2026-05-27. This is not current operational truth; use it to
> guide the confidence engine and bundle schema.

## Bottom Line

The reports agree that multi-position room correction is not "average
everything and EQ the average." It is an evidence system for deciding
which features are safe, useful, and representative enough to correct.

The JTS confidence model should be:

- band-centric
- filter-centric
- channel-centric
- bundle-replayable
- conservative about boosts
- explicit about uncertainty

The biggest immediate product win is first-class confidence reporting,
not more aggressive filters.

## Core Scientific Boundary

All reports split the problem by frequency regime:

- Below the room transition/Schroeder region, modal peaks are often
  more spatially coherent and more minimum-phase-like. EQ can help,
  especially with cuts.
- Above the transition region, fine peaks and dips are often
  direct/reflected interference, SBIR, LBIR, comb filtering, or
  loudspeaker/directivity behavior. Narrow automatic EQ becomes risky.
- The transition is not a magic fixed number. Practical domestic-room
  guidance clusters around 150-300 Hz, with some cautious treatment up
  to 500 Hz only when features are broad, stable, and well measured.

This supports JTS's current safe/balanced/assertive strategy model and
argues for making its thresholds explicitly data-driven.

## Position Count Recommendation

Use a practical default:

- 1 position: allowed only as limited seat-specific fallback.
- 3 positions: minimum for multi-position correction.
- 5 positions: default consumer workflow.
- 7-9 positions: higher confidence for wide couches or assertive
  strategy gates.
- More positions: better diagnostics and future FIR/multi-sub support,
  not automatically more correction.

Suggested 5-position cluster:

- main listening point, ear height
- 30 cm left
- 30 cm right
- 30 cm forward
- 30 cm back or secondary seat

The UI should explain five positions as a balanced compromise, not as
an acoustical law.

## Averaging Model

Do not choose one canonical average. Store and compute multiple views:

- RMS / linear-power average for acoustic energy and modal peak
  detection.
- Median or smoothed dB average for perceptual trend display and target
  comparison.
- Percentile spread or standard deviation for spatial stability.
- Same-position repeatability for data quality.
- Phase/vector averages only for same-position or carefully aligned
  phase-aware tasks, not arbitrary seats.

Important nuance:

- RMS averaging prevents deep nulls from canceling out high-energy
  peaks in the average.
- dB averaging can be useful for display but can overweight dips.
- vector averaging across different seats can create artificial
  cancellations and should not drive global magnitude correction.
- MMM can estimate a listening-area average quickly, but it suppresses
  the per-position variance that JTS needs for confidence reporting.
  Treat MMM as a future adjunct, not a replacement for fixed positions.

## Channel-Centric Correction

For stereo:

- Measure left and right separately at each position.
- Design per-channel candidates from single-channel responses.
- Use L+R summed measurement as preview/diagnostic only.
- Preserve an inter-channel similarity or asymmetry metric.
- If L/R are similar, symmetric or matched correction may preserve
  imaging better.
- If the room is asymmetric, independent L/R correction may be the
  more honest tonal choice.

For mono:

- Use the same multi-position variance logic.
- Skip stereo imaging diagnostics.
- Boundary loading and corner placement warnings become more important.

## Confidence Scores

Each band and candidate filter should carry deterministic sub-scores:

- `data_quality`: clipping, SNR, noise, timing, repeatability
- `spatial_stability`: standard deviation, percentile spread, sign
  consistency across positions
- `eq_plausibility`: broadness, minimum-phase likelihood, null/SBIR
  suspicion, excess group delay where available
- `benefit_estimate`: peak prominence, width, decay severity, affected
  seats
- `risk`: boost demand, high Q, out-of-band correction, channel
  mismatch, placement suspicion, driver/headroom risk

Then aggregate into:

- High confidence: reliable data, low spread, broad repeatable excess
  energy, in correction band. Normal cut-only PEQ allowed.
- Medium confidence: one metric marginal. Smaller cuts, stronger
  warnings, no boosts.
- Low confidence: bad data, high spread, narrow/notch-like feature,
  sign flips, likely cancellation, out-of-band. Do not auto-correct.

This gives future LLM behavior a deterministic substrate: the assistant
can explain the score instead of inventing one.

## Strategy Gates

Recommended policy shape:

- Safe:
  - at least 3 positions for multi-position mode
  - high-confidence cuts only
  - mostly below 150-200 Hz
  - no boosts
  - conservative Q and cut depth
- Balanced:
  - preferably 5 positions
  - high and some medium-confidence broad cuts
  - ceiling around 200-250 Hz
  - no boosts except potentially very limited below transition when all
    gates pass
- Assertive:
  - 7+ positions or unusually strong measurement quality
  - broader correction range up to about 300 Hz, maybe higher only for
    broad target/tilt behavior
  - still no null boosting
  - stronger warnings and rollback

The reports disagree on exact thresholds, and some report thresholds
are clearly engineering proposals. Keep them configurable and
replayable.

## Thresholds Worth Starting From

Use these as policy seeds, not immutable science:

- Minimum broadband SNR: around 25 dB to proceed; lower triggers
  re-measure or degradation.
- Coherence/SNR blanking: blank or downweight bins with poor reliability
  when available.
- High confidence modal-band spread: roughly under 1.5-2 dB.
- Medium confidence spread: roughly 2-4 dB.
- Low confidence/no auto correction: above roughly 4-6 dB spread,
  depending on band and strategy.
- Maximum boosts: 0 dB in safe mode; at most +3 dB in tightly gated
  modal cases. Never boost narrow/deep/null-like dips.
- Maximum cuts: safe around -4 to -6 dB, balanced around -6 to -9 dB,
  assertive up to -12 dB only with excellent evidence.
- Maximum Q: conservative default around 6, tighter above transition,
  higher only for stable modal cuts if needed.
- Minimum feature width: broad enough to be meaningful; reports suggest
  values around 1/12 to 1/6 octave in low bass and broader above.

## Feature Handling

Repeatable modal peak:

- low variance across positions
- appears at similar frequency
- broad enough to matter
- minimum-phase/excess-GD evidence if available
- cut is safe and useful

Seat-local peak:

- high variance
- appears strongly at one or two positions
- apply smaller or no cut
- explain that stronger EQ would hurt other seats

Deep/narrow null:

- high variance or strong excess-GD spike
- likely cancellation/SBIR/LBIR
- no boost
- recommend placement or accept limitation

Measurement artifact:

- low SNR, clipping, dropouts, corrupt impulse, ambient event, mic
  obstruction, wrong input
- zero confidence
- re-measure before acoustical interpretation

Stereo geometric cancellation:

- L-only and R-only look reasonable but L+R sum has a null
- label as geometry/cancellation, not per-channel EQ target

## Bundle Schema Additions

Persist:

- acquisition metadata: sweep settings, sample rate, routing, software
  version, threshold profile
- mic metadata: model, serial, calibration source/checksum, orientation
- position metadata: labels, relative coordinates, height, weights,
  focus/reference point
- raw captures and deconvolved IRs per channel and position
- per-position frequency responses and smoothing variants
- spatial average traces and variance arrays
- same-position repeats where available
- SNR/noise/clipping/dropout/timing metrics
- channel similarity metrics
- candidate feature list with accepted/rejected status
- per-filter rationale, confidence, affected seats, thresholds crossed
- global confidence and allowed strategies
- target profile and correction strategy
- post-apply verification linkage

The current JTS `position_analysis.json` is a good first artifact. The
next step is to enrich it from "variance arrays exist" into "variance
drives explainable decisions."

## Prior-Art Lessons

- REW gives the clearest averaging taxonomy. Borrow its distinctions.
- Audyssey, Dirac, Sonarworks, HouseCurve, RoomPerfect, and Trinnov all
  encode the idea that multi-position correction is area optimization.
- RoomPerfect's "RoomKnowledge" is the most relevant consumer-facing
  confidence metaphor.
- Trinnov point weighting is useful for future focus-seat/wide-area
  modes.
- MSO is the best reminder that reducing seat-to-seat variation often
  requires source placement/cooperation, not just EQ.
- Proprietary tools do not reveal exact confidence math, so JTS should
  avoid pretending there is a vendor-consensus formula.

## Implementation Implications For JTS

- Keep the current conservative PEQ path as the default.
- Add per-band/per-filter confidence to the design report.
- Make the UI show why a strategy is allowed or blocked.
- Let more positions increase certainty before they increase
  aggressiveness.
- Store rejected features, not just accepted filters.
- Add placement warnings when a problem is geometric or spatially
  unstable.
- Use deterministic confidence facts as the future LLM explanation
  surface.

## Open Questions To Verify

- Best initial spread thresholds on real JTS measurements.
- Whether JTS can estimate modal decay reliably from current browser
  captures.
- How to estimate transition frequency without asking users for room
  volume/T60.
- Whether fixed-position UX can be made simple enough on a phone.
- When to introduce point weighting versus keeping the first pass
  unweighted.
- How much channel symmetry preservation matters in normal JTS rooms.
- Whether MMM should be supported as a fast advanced path after the
  fixed-position model is mature.

Last synthesized: 2026-05-27
