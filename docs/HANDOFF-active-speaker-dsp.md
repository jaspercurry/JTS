# HANDOFF: active speaker DSP commissioning

> **Status: research-backed workstream seed.** Created 2026-05-25
> from three local deep-research reports on DIY DSP speaker
> commissioning. This is the canonical handoff for JTS speakers where
> CamillaDSP directly drives woofer and tweeter amplifier channels.
> Current JTS production hardware still uses a stereo Apple USB-C
> dongle passthrough path; active crossover hardware is future work.

## Current Operational Truth

Active speaker DSP is a separate layer from room correction.
Room correction asks, "what should be compensated at this listening
position?" Active speaker commissioning asks, "what should this
speaker be before the room is considered?"

For JTS, that means:

- The current `/correction/` wizard must not rewrite crossover,
  polarity, per-driver gain, driver delay, or limiter policy.
- A future active speaker profile is a versioned speaker baseline,
  not a room-correction session.
- Room correction and preference EQ sit downstream of that baseline.
- Every measurement bundle should eventually record the active
  speaker profile ID so later analysis knows what acoustic baseline
  was measured.

The existing deployed audio topology is not yet active 2-way ready:

- Music flows through CamillaDSP to a stereo Apple USB-C dongle.
- TTS/cues currently bypass CamillaDSP and sum at `jasper_out`.
- Active crossover output needs a stable multi-output map. For a
  mono active cabinet this is at least two physical outputs
  (`woofer`, `tweeter`). For a stereo active pair this is four
  physical outputs (`left_woofer`, `left_tweeter`,
  `right_woofer`, `right_tweeter`).
- Before tweeter hardware is connected, all audible paths must be
  proven to pass through the same protected crossover path. A TTS
  bypass into a raw active amp channel is a driver-damage hazard.

## Layer Boundary

Keep the DSP stack conceptually separate:

```text
source/renderers
  -> routing / channel map
  -> speaker baseline: crossover, per-driver EQ, delay, polarity, gain
  -> room correction: position/seat dependent correction
  -> target curve: chosen neutral/warm/bright/house target
  -> preference EQ: reversible user taste profile
  -> limiter/headroom guard
  -> physical outputs
```

The speaker baseline is the thing that makes the box a coherent
speaker. It should be commissioned once per hardware build and
changed deliberately. Room correction is re-run for a room/listening
area. Preference EQ is user taste and should always be reversible.

## Hard Safety Rules

These are not UX polish; they are anti-smoke rules.

- Do not connect the tweeter until channel identity, gain staging,
  and protective high-pass routing have been proven at low level.
- Treat a physical series protection capacitor on the tweeter as
  strongly recommended during commissioning, and likely worth keeping
  in the final build unless the amplifier chain has an independent
  proven protection story.
- A CamillaDSP high-pass and limiter do not protect against wrong
  wiring, wrong channel maps, startup pops, DC faults, `jasper-camilla`
  not running, or a bypass path.
- Start with conservative output gain, tweeter muted, room correction
  disabled, and a temporary protective tweeter high-pass above the
  planned crossover.
- Never sweep a raw tweeter below its safe range just because an LLM
  or simulator says it is probably fine.
- A CamillaDSP limiter is a final clip stop, not a full thermal or
  excursion model. Driver protection needs gain structure, crossover
  limits, physical protection, and measured validation.

## Default Commissioning Stance

The research reports converge on an IIR-first ladder:

- Use an acoustic Linkwitz-Riley fourth-order target as the first
  practical default for a conventional 2-way.
- Treat that as an acoustic target, not merely "insert electrical LR4
  biquads." The drivers, cabinet, protection capacitor, baffle
  diffraction, horn/waveguide, and acoustic center all shape the
  final acoustic slopes.
- Use IIR biquads for the first production baseline: low latency,
  simple CPU budget, inspectable filters, and no pre-ringing.
- Reserve FIR for later expert modes: linear-phase crossover
  experiments, measured excess-phase correction, or imported
  convolution filters after latency and pre-ringing are explicitly
  acceptable.
- Choose crossover frequency from the actual drivers and enclosure:
  tweeter safe operating range and distortion, woofer breakup and
  directivity, center-to-center spacing, baffle geometry, target SPL,
  and off-axis behavior. Do not hard-code a universal frequency.

For Jasper's own future active build, a report suggested a 2 kHz LR4
starting point for a Dayton Epique E150HE-44 plus generic 1-inch
compression-driver/horn pairing. That is an input hypothesis only.
Do not promote it to a default until the exact tweeter, horn, baffle,
amplifier gain, output hardware, and measurements are known.

## Measurement Protocol

Minimum credible active speaker commissioning data:

- Per-driver measurements in the final cabinet/baffle.
- A common timing reference for crossover delay/phase work: electrical
  loopback where possible, or a carefully held acoustic timing
  reference. USB mic magnitude-only traces are not enough for final
  delay alignment.
- Woofer-only and tweeter-only captures from the same mic position
  before the summed capture.
- Gated far-field measurements for quasi-anechoic mid/high behavior.
- Near-field woofer and port measurements for bass, merged with
  far-field data where appropriate.
- At least limited off-axis validation around the crossover region,
  especially vertical 0, +/-10, +/-20, +/-30 degrees if the rig can
  support it.

Indoor gated measurements lose low-frequency resolution. Below the
gate's useful frequency, be cautious: use near-field/merge workflows
and avoid aggressive "speaker baseline" EQ that is really correcting
the room.

## Delay, Phase, and Null Verification

Delay alignment is measured, not guessed.

- Do not assume "delay the tweeter." Delay whichever acoustic source
  arrives earlier after measurement. A horn can make the tweeter
  acoustically later, which may mean the woofer receives delay.
- Compare woofer and tweeter phase traces through at least roughly
  one octave around the crossover.
- Use summed response and reverse-polarity null depth as practical
  validation. After alignment, invert one driver; a strong, centered
  null around the crossover is the clearest quick proof that the
  branches are meeting as intended on the design axis.
- Validate off-axis after the on-axis null looks good. A crossover can
  sum acceptably on-axis while creating vertical holes in the
  listening window.
- Group delay, impulse response, and step response are supporting
  views; they are not substitutes for phase-aware summation.

## CamillaDSP Profile Architecture

The future active speaker path should use bounded profile templates,
not freeform YAML generated by an LLM.

Baseline profile shape:

```text
stereo or mono source
  -> explicit mixer to physical driver outputs
  -> per-driver crossover filters
  -> per-driver EQ needed to hit the acoustic target
  -> per-driver delay / polarity / gain trim
  -> per-driver limiter / protection block
  -> output device
```

Important implementation implications:

- Channel labels must be explicit and persisted.
- A commissioning-safe profile should start with tweeter outputs
  muted or heavily protected.
- Generated configs should be validated before load, and rollback
  should be obvious.
- Bypassing a mixer that changes channel count can break a CamillaDSP
  pipeline; the profile should make bypass points intentional.
- The active baseline profile should live separately from room
  correction profiles under `/var/lib/jasper`, with its own bundle
  metadata and accepted/rejected state.

## Deterministic Tooling Roadmap

Code should eventually own:

1. Active topology detection: output channel count, named physical
   channel map, and "all audible paths are crossover-protected" gate.
2. Commissioning-safe CamillaDSP profile generation.
3. Channel identification: quiet band-limited tone per output, with
   operator confirmation before drivers are connected.
4. Per-driver measurement mode: isolate woofer/tweeter, enforce
   safe sweep range and level, and record active filters.
5. Measurement import: REW/VituixCAD FRD/IR imports first; REW local
   API integration is plausible later.
6. Provenance in bundles: driver, angle, axis, distance, timing
   reference, mic calibration, gate/window, active profile, sweep
   level, and raw capture path.
7. Crossover candidate compiler: structured crossover/filter/delay/
   gain/limiter data to validated CamillaDSP YAML.
8. Delay/polarity checks: predicted sum, measured sum, inverted-null
   depth, phase tracking, and group-delay plots.
9. Acceptance gates: no "commissioned" label without timing-valid
   driver measurements and at least minimal off-axis validation.
10. Rollback and A/B: accepted speaker baseline, previous baseline,
    room correction bypass, preference EQ bypass.

The first shipped product slice should likely be interop-first:
import REW/VituixCAD measurements and generate/verify a safe
CamillaDSP profile, rather than immediately rebuilding all of REW and
VituixCAD inside JTS.

## LLM Boundary

An LLM advisor can help explain, sequence, and translate:

- explain why a crossover dip is not a room mode;
- ask whether timing reference was used;
- explain null-test results and off-axis concerns;
- suggest which deterministic check to run next;
- generate user-facing summaries and audit-log narration.

The LLM must not:

- emit arbitrary CamillaDSP YAML;
- decide to remove tweeter protection;
- invent limiter thresholds without driver/amp data;
- call magnitude-only data valid for phase alignment;
- silently fold room correction or taste EQ into the speaker baseline.

## Open Questions

- What exact output hardware will the active build use: multi-channel
  USB DAC, HAT, DSP amp board, or separate amp chain?
- Is the target a mono active cabinet with two outputs, a stereo
  active pair with four outputs, or both?
- What exact woofer, tweeter, horn/waveguide, baffle, enclosure, and
  center-to-center spacing will Jasper's first active build use?
- How should TTS/cues be routed so they always pass through the same
  crossover protection as music?
- Which parts should be in-product JTS tooling versus external
  REW/VituixCAD workflow with imports?
- Does the deployed CamillaDSP version expose the limiter/filter
  primitives we want, or do we need a compatibility layer?
- What profile schema should represent speaker baseline versus room
  correction versus preference EQ?

## Failure Modes To Keep Visible

- Full-range signal reaches tweeter due to wrong channel map,
  disabled crossover, daemon crash, or bypass audio path.
- On-axis-only optimization creates vertical lobing/nulls around the
  crossover.
- USB mic measurement without timing reference is used for delay
  alignment.
- Listening-position room correction hides a speaker-baseline
  crossover problem.
- Limiter threshold is treated as thermal/excursion safety when it is
  only a digital peak guard.
- Protection capacitor or horn path changes acoustic response, but
  measurements were taken without the final hardware in place.
- FIR is enabled without latency, CPU, headroom, and pre-ringing
  checks.

## Source Reports

This handoff distills three local reports provided on 2026-05-25:

- `/Users/jaspercurry/Downloads/DIY DSP Speaker Commissioning Research.md`
- `/Users/jaspercurry/Downloads/deep-research-report (3).md`
- `/Users/jaspercurry/Downloads/compass_artifact_wf-49865d5e-0039-4341-bdd2-0d4cd4826800_text_markdown.md`

Key external prior-art families named by the reports:

- REW for measurement, timing reference, impulse/phase/group-delay
  inspection, and possible local API integration.
- VituixCAD for crossover simulation, near/far merge, directivity,
  listening-window, and polar validation.
- CamillaDSP for active routing, IIR biquads, FIR convolution,
  gain/delay/polarity, and limiter/compressor primitives.
- Linkwitz/Riley/Vanderkooy/Lipshitz crossover literature for
  non-coincident driver integration.
- rePhase / CamillaFIR / DRC-FIR as later FIR references, not first
  implementation defaults.

Last verified: 2026-05-25
