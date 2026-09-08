# Active Crossover Builder: product and architecture reference

Start with the [tuning runbook](tuning-operator-runbook.md). This document
explains the crossover's product boundary and measurement reasoning.
[Measurement loop doctrine](measurement-loop-doctrine.md) owns authority;
[tuning methodology](tuning-methodology.md) is optional science guidance.
The [master plan](tuning-master-plan.md) owns scope, and command help owns the
supported calls and inputs.

The old commissioning capture host and summed-graph lane are retired
([ADR-0197](adr/0197-the-commissioning-capture-stack-is-deleted.md),
[ADR-0230](adr/0230-the-summed-graph-commissioning-lane-is-deleted.md)). Their
records and readers do not establish a current capture route. The wired
crossover path replaces the phone relay; [Room](room-correction-information-design.md)
still has local browser capture over HTTPS, as
[ADR-0222](adr/0222-the-relay-is-deleted-the-wired-microphone-is-the-only-capture-path.md)
notes. There is no fixed seed/linearize/align/verify campaign.

## Product goal

Let the operator declare a usable base crossover, inspect measurements, and
try deliberate changes. The external LLM chooses useful analyses and candidate
values. The human places the microphone, starts each sound-producing pose batch,
and decides whether another experiment or adoption is useful.

Manual values remain first-class. Driver research supplies visible, untrusted
prefill; it does not authorize playback. A measured response can inform a
candidate without becoming a second settings store or an automatic optimizer.
Supported topology and filter vocabulary come from the current compiler and
entry contracts, not examples in an older work plan.

Crossover work owns the speaker's driver-domain graph. Room correction owns
listening-area response, and preference owns taste. A crossover experiment
excludes the latter two layers so that its result describes the candidate.

## First principles

An electrical crossover is not the acoustic crossover the listener hears.
Each branch combines the mounted driver response, electrical filters, EQ, gain,
polarity, and propagation delay. The speaker output is their complex sum.

Different questions therefore need different evidence: useful driver bands,
acoustic slopes, relative levels, delay and polarity, the combined response,
spatial behaviour, and headroom. This is a list of possible questions, not a
mandatory measurement campaign. A tone can help set level; it cannot establish
a broadband crossover response. A solo magnitude cannot identify a summed null.
A predicted sum needs compatible source measurements and remains a prediction.

## One model, three states

The existing `ActiveSpeakerPreset` vocabulary owns crossover values. Keep form
state, candidate identity, and the applied runtime distinct:

| State | Meaning |
|---|---|
| Working crossover | Editable values; changing the form does not change sound |
| Candidate | Identified settings that can be reviewed or measured temporarily |
| Applied crossover | The persistent tune confirmed by runtime readback |

A silent preview does not stage a graph or authorize sound. A candidate can
later run through protected temporary playback without being adopted. A
measurement records what played; it does not make that candidate the current
persistent tune. The UI and reports must not merge these states implicitly.

For manual application, the reviewed candidate identity is rechecked under the
DSP writer boundary before its exact configuration is loaded. Changing the
working values requires a new review. Prescriber packets likewise bind a
proposal to its frozen evidence; new evidence needs a new packet.

### Parameter provenance

Keep the declared value, its source, and its measured effect distinct. A
manufacturer fact is not an engineering estimate; a preserved trim is not a
fresh solve. Use the metadata the owning schema actually stores. Do not infer
missing provenance from the value or introduce a parallel per-parameter schema
in prose.

### Replacement semantics

Applying a candidate is explicit and identifies what will replace the tune.
Temporary measurement restores normal playback without adopting that candidate.
A new experiment creates new evidence; it does not rewrite historical captures.
Useful parts of an earlier candidate can be reused, but the resulting combination
is unmeasured until tested.

## User experience

`/sound/` owns physical setup and component declarations. The crossover surface
owns the experiment handoff. Show physical names such as driver role and output,
then the changed values, evidence limits, and next human action. Do not turn an
implementation phase number into a compulsory household step.

### Speaker setup and protected starting crossover

Before a driver emits a test signal, routing, required protective filters,
declared driver limits, and the test-level envelope must agree. The research
helper can prefill values, but the operator's visible installation choices and
normalized declarations remain authoritative. Research import must not silently
replace enclosure, driver/loading type, or resistor-pad choices.

Published facts, estimates with a stated basis, and unknowns have different
meanings. Required slope and the driver's declared low limit remain separate
facts; a cutoff is not a brick wall. A supported corner at that limit needs no
invented margin when the slope condition is met. The
[driver protection owner](../jasper/active_speaker/driver_protection.py) and
[ADR-0227](adr/0227-owner-rulings-the-prose-pass-surfaced.md) define the current
rules. This reference does not duplicate profile fields or numerical caps.

Missing required driver-protection facts can prevent new test audio. They do
not by themselves erase a working normal-playback graph. Preview remains useful
while a physical limit prevents playback.

### Capture geometry and timing

Crossover capture uses a microphone attached to the Pi. Sharing a computer does
not make a USB microphone and DAC share a sample clock. Calibration corrects
microphone response; it does not establish timing synchronization. Relative
phase and delay claims need the corresponding timing evidence.

Hold geometry fixed when comparing branches or candidates. Moving the microphone
between solo drivers changes propagation delay and level. A near-field capture
can help a low-frequency magnitude question but cannot replace the common-axis
geometry needed for a sum. Same-pose repeats estimate repeatability; distinct
poses test spatial behaviour. The measurement program owns the requested poses
and its resource bounds. The operator can choose another useful experiment.

### Level control and SNR

A short probe establishes a useful capture level with microphone headroom.
It is not the sweep's signal-to-noise ratio (SNR). A broadband reading cannot
prove SNR in the narrower band needed for a particular decision. Increasing
microphone gain does not improve acoustic SNR when signal and room noise rise
together. Longer excitation can improve processing gain, within declared
level and duration bounds.

Both sides of an SNR subtraction must use the same domain and units. A raw
captured band level can be compared with raw ambient band level. A deconvolved
quantity needs a noise estimate processed in that same domain. Dropping raw
ambient into a deconvolved subtraction changes the result with sweep duration
and coverage. Conversely, deconvolution divides out drive level, so it cannot
by itself prove that a raw-level target was reached.

Band-integrated RMS is also different from mean power per FFT bin. The latter
changes with bin count and cannot stand in for an absolute band level. Window
choice matters for a swept signal, since time weighting changes which frequency
received weight. These constraints are owned by
[snr_policy.py](../jasper/audio_measurement/snr_policy.py), including its
band-power estimator and capture-specific consumers.

Magnitude and trim estimates need less SNR than a deep-null or alignment claim.
A null of depth D needs roughly D plus the noise margin to resolve its bottom;
otherwise report a capped depth, not a proved alignment. Evaluate the bands the
claim uses, keep uncovered bands unknown, and retain useful supported bands.
[quality_model.py](../jasper/audio_measurement/quality_model.py) owns the
thresholds. Its decision-specific verdict does not become a general veto on
operator experiments or all uses of the same capture.

### Measurement validity: gating and the low-frequency floor

Gating excludes later reflections at the cost of low-frequency resolution.
For a window of length T seconds, `1/T` is the nominal validity floor;
`2.5/T` is the stricter trusted-magnitude floor disclosed beside it. These
numbers are not interchangeable. A capture's actual floor can exceed the
nominal lower edge of a grading table.

A detected reflection and a window capped at the search bound are different
observations even if they produce the same window length. Keep `floor_source`,
the early-feature ledger, and the pre/post-gate comparison visible. An early
horn or baffle feature should not be assumed to be a room reflection. The
historical P1a consult table and its tested detector parameters now live in
[gating.py](../jasper/audio_measurement/gating.py); code owns those values.

Data below a gate's useful band cannot support a direct-sound flatness claim.
That narrows the claim; it does not require an automatic near-field splice or
prevent a safe trial. Gate-window comparison is an optional diagnostic. Room
measurement deliberately retains reflections because they are its subject.

#### Low-frequency reconstruction: retained scientific limits

Near-field pressure is not a free-field response. The earlier
`sealed_single_radiator_v1` proposal described a restricted reconstruction, not
a currently supported automatic campaign step. Near-field splicing remains
outside the current [toolbox scope](tuning-master-plan.md). Its useful physical
limits are retained here without claiming that its proposed artifact schemas,
refusal codes, or capture host are implemented.

That model assumed one sealed circular radiator, with measured effective
radius `a`, baffle width `w`, height `h`, and near/far microphone distances.
It excluded ports, passive radiators, and coupled cones. A nominal placement
instruction or driver frame diameter is not a measured geometry value.

The ARTA approximation used square-equivalent baffle dimension
`d_eq = w (h/w)^(1/3)`, with dimensions in metres, and `f0 = 34.16/d_eq` Hz.
Its correction was

`B_db(f) = 20 log10 |(1 + j f/f0) / (2 + j f/f0)|`.

This models the broad transition from about −6 dB at low frequency to 0 dB at
high frequency. It does not predict edge ripple, off-axis response, or arbitrary
cabinet radiation. The proposed domain used `0 < r_nf < a/20`, `r_ff > 6a`,
`ka = 2πfa/c <= 0.8` with `c = 343 m/s`, and
`1/40 <= (a/d_eq)^2 <= 1/10`. These are model assumptions, not measurements of
any JTS installation or a new playback permission.

A defensible splice needs common calibrated amplitude, compatible played
settings, continuous overlap above the far-field floor and below the near-field
model limit, and an explicit residual check. Align with one scalar, not a
frequency-shaped fit learned from the overlap. Preserve the source curves,
bounds, offset, and residuals. Missing coverage is not extrapolated silently.

The result is magnitude evidence only. Do not synthesize usable inter-driver
phase from the model, infer shared-clock timing, or call it an isolated natural
driver response when the played electrical filters remain included. The
[original research](research/2026-06-19-active-crossover-calibration/README.md)
and sources below retain the scientific basis.

### Review, apply, and acoustic checks

A review should show what changed, which evidence supports it, and what remains
predicted or unknown. Relevant details can include level, delay, polarity,
spatial agreement, valid frequency range, and headroom. Load larger artifacts
only when the question calls for them.

Apply validates the graph, loads it, and confirms runtime readback. That is a
graph check, not a new acoustic measurement. Another capture can test the
candidate's predicted effect; it remains an explicit experiment. Optional
flatness, classification, or distortion results do not create a mandatory
acceptance campaign. The separate Room acceptance loop retains its own policy.

## Architecture and ownership

The common session is
[TuningSession](../jasper/active_speaker/crossover_v2/session.py): `open`,
`measure`, and `close`. Its
[four seams](../jasper/active_speaker/crossover_v2/session_seams.py) supply
graph, volume, records, and playback. CLI and web entry points are adapters;
analysis and candidate tools own their outputs outside the capture lifetime.

[GeneratedExcitationWav](../jasper/audio_measurement/admitted_playback.py)
keeps stored generation/WAV identities and their integrity checks readable.
The one-use `play_admitted_wav` control adapter is retired. Current measurement
access uses `TuningSession.measure` through
[program_playback.py](../jasper/active_speaker/program_playback.py), with fresh
program admission and verified WAV playback. The live transaction records
emission and cleanup observations; it does not need the retired runner's
persisted before-audio receipt protocol.

### Slice 0: measurement-validity substrate

Shared gating, SNR, calibration, and response calculations remain useful Python
tools. A retired host does not make those numerical capabilities dead. Preserve
their typed inputs and evidence limits without rebuilding the old delivery
slices or making every analysis a capture prerequisite.

## Durable evidence and observability

Records should distinguish the capture, the candidate or graph actually played,
and the tune currently applied. Keep source identities and analysis settings
with derived answers. Older records can remain useful when a missing field
limits one claim. Banked authoring context is not proof of what a microphone
captured; a successful calculation is not proof of successful cleanup.

### Runtime surface

Use the current command's status and returned artifact paths. The runbook's
entry contract and generated menu describe the public route. Do not infer a
live commissioning host from a historical `commissioning_run` field or record.

### Structured events

Capture and apply owners emit structured events with IDs and outcomes. The
`correction.*` namespace also appears in retained commissioning records. It is
not evidence that the former relay event flow still runs. Code owns event names
and fields; this document does not keep a second event registry.

## Language guide

Name the physical action and the result: move the mic, measure, compare, apply,
or restore. Distinguish requested, measured, predicted, applied, and unknown.
Keep internal graph and transport names in details unless they explain an action
the operator needs to take. A failed restore must not be presented as success.

## Research basis

The internal research packet that motivated this design is
[`docs/research/2026-06-19-active-crossover-calibration/`](research/2026-06-19-active-crossover-calibration/README.md).
Primary technical references include Linkwitz's discussion of acoustic
crossover behavior and polar response, AES work on noncoincident-driver phase
and listening-window optimization, KLIPPEL transfer-function/directivity
measurement guidance, REW's sweep and timing-reference guidance, and Dirac's
multi-position room-measurement guidance:

- [Linkwitz: crossovers](https://www.linkwitzlab.com/crossovers.htm)
- AES E-Library work on crossover networks for noncoincident drivers and on
  crossover filter design optimized for a listening window (cited by title;
  the AES library is paywalled and its URLs are unstable).
- [Rane Note 160: Linkwitz–Riley crossovers and lobing error](https://www.ranecommercial.com/legacy/note160.html)
- [KLIPPEL: transfer-function measurement](https://www.klippel.de/manuals/frequencyresponse-distortion/trf/trf.html)
- [KLIPPEL: loudspeaker directivity measurement](https://klippel.de/training/attachments/training8/Training_8_Measurement_of_Loudspeaker_Directivity_en.pdf)
- [D. B. Keele Jr.: Low-Frequency Loudspeaker Assessment by Nearfield
  Sound-Pressure Measurement](https://pearl-hifi.com/06_Lit_Archive/14_Books_Tech_Papers/Keele_D_B/LF_Near-field_Measurement.pdf)
- [ARTA Application Note 4: Loudspeaker Free-Field Response](https://www.artalabs.hr/AppNotes/AN4-FreeField-Rev03eng.pdf)
- [REW: making measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html)
- [Dirac Live technical overview](https://www.dirac.com/wp-content/uploads/2024/06/Dirac-Live-a-technical-overview-white-paper.pdf)
