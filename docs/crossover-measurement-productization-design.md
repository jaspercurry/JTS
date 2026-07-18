# Crossover measurement & tuning — productization design

> **Status: v2 design / decision record (2026-07-18). Committed plan, not yet
> implemented.** v1 (earlier the same day) synthesized the first-principles
> deep-research pass (Appendix A) into a staged adaptation plan. v2 — same day,
> after an adversarial validation pass against the primary sources, a
> prior-art UX study, and a full code-contract mapping — **corrects four v1
> claims (§3) and replaces the staged adaptation with the conductor
> architecture (§5)**. Motivated by the end-to-end hardware-validation run
> ([crossover-room-e2e-validation-log.md](crossover-room-e2e-validation-log.md)).
> This doc does **not** restate the canonical measurement/correction references
> ([docs/HANDOFF-audio-measurement-core.md](HANDOFF-audio-measurement-core.md),
> [docs/HANDOFF-correction.md](HANDOFF-correction.md)); it defines how the
> `/correction/crossover/` measurement flow is being rebuilt.

## 1. Context & problem

JTS calibrates a **fully active, bi-amped 2-way crossover** (separate DAC
channels → woofer + tweeter; no passive network — the DSP crossover *is* the
sonic crossover and the tweeter's only protection). The measurement/tuning flow
is browser-driven: the mic is a calibrated USB mic (UMIK-2, Dayton iMM-6C) or
phone mic on the **user's** device, captured via `getUserMedia`, E2E-encrypted,
relayed to the speaker, which pulls the recording back. Target user is a
non-expert; north star is *"place the mic roughly where we suggest, tap through
a few prompts, and the speaker ends up correctly aligned and flat."*

**Why v2 exists.** The v1 flow's cost was structural, not parametric. A full
automatic 2-way run was ~17 page actions plus ~12 phone-capture round-trips
across **two mic geometries** (per-driver level ramps, 3×2 near-field sweeps,
per-driver re-levels, 3×2 reference-axis sweeps, apply) — and the
delay/polarity machinery was never wired into the wizard (automatic apply set
driver trims only). Every interaction was a distributed transaction across
three parties (Pi, relay worker, phone page), and the ~86 fix-PRs the flow
absorbed in 2026-07 concentrated in exactly the machinery that multiplicity
demands: repeat admission, geometry handoff, identity validation, volume
restore. The measurement *math* was never the bug source.

The redesign lever is therefore **collapsing the interaction topology, not
tuning steps**: fewer, richer captures; one mic position; zero user-facing
leveling; all intelligence server-side in pure functions.

## 2. The constraint that shapes everything

**There is no hardware loopback and no sample-accurate timing between playback
(on the speaker) and capture (on the user's device).** Absolute latency is
unknown, large, and varies per session; the speaker DAC clock and capture ADC
clock are independent and drift. Therefore **a driver's *absolute* acoustic
delay is unrecoverable** from an isolated capture. Every timing/phase method
must be robust to this.

Verified implementation facts: the **relay does not break single-capture
timing** (it transports a complete continuous WAV, not a real-time stream), and
tweeter safety gating is band-limiting/level-capping, not a timing break. One
v1 caveat is now upgraded to a design input: **the browser capture path is not
a clean ADC** — silent resampling to the `AudioContext` rate, WebRTC drift
compensation that can be *time-varying*, and AudioWorklet dropouts on mobile
are all documented. v2 therefore treats capture-integrity verification as a
*runtime* concern (§5.6): every measure capture carries its own drift/glitch
evidence and is rejected when its internal baselines disagree.

## 3. Corrections to v1 (adversarial validation, 2026-07-18)

Four v1 claims were checked against the primary sources (Bryan/Kolar/Abel AES
2010 Paper 8169; Gamper HSCMA 2017; REW/VituixCAD docs; Chromium/WebKit
trackers) and corrected. The v1 text below Appendix A is preserved for
archaeology; **these corrections govern.**

1. **Clock-drift budget: error ≈ ε × T_sep, where T_sep is the full time
   between excitation events — first sweep's *duration* + gap — not the silent
   gap alone.** A drifting ADC stretches the whole recording timeline by
   (1+ε): both papers model the shift as proportional to *elapsed time*. The
   v1 "<0.4 s chirp gap" framing is ~10× optimistic once multi-second sweeps
   are used: a 3 s sweep pair at 50 ppm accumulates ~170 µs ≈ **122° of
   relative-phase error at 2 kHz** — alignment-breaking for LR4. Consequence:
   **the repeated sweep inside the same capture is mandatory, not optional.**
   Two identical sweeps at a known scheduled separation form an in-capture
   drift estimator (measured separation / scheduled − 1 = ε; Gamper's
   least-squares ratio method needs no loopback); dividing ε out makes long
   sweeps free. The repeat doubles as the **dropped-buffer/glitch detector**
   and as the rule-of-two transient check.
2. **Alignment must be measured at ~1 m on the listening axis, not 10–20 cm.**
   With ~15 cm driver spacing, mic-to-driver parallax (√(r²+d²)−r) is
   ~150–230 µs at 10–20 cm but ~11 µs at 3 m: aligning as-measured at 15 cm
   mis-aligns the listening position by ~120°+ at 2 kHz, and mic-height
   sensitivity at that range (~15°/cm) exceeds hand-placement repeatability.
   At ~1 m the residual parallax is small (~16° worst case) *and*
   deterministic: v2 applies the geometric correction from the declared driver
   spacing and prescribed distance, and budgets the residual. 10–20 cm remains
   acceptable for on-axis *magnitude* only — but v2 does not need it (§5.2).
3. **"REW aborts only above 30% clipping" was a misread** — that is REW's
   catastrophic hard-abort, not a tolerance; *any* clipped run corrupts the
   response. Accept-and-normalize stands, but the clip gate is any
   at-full-scale run, with the UMIK-2 caveat that its PGA must sit at a known
   gain for the prediction model to hold.
4. **`getSettings()` is not sufficient AGC/EC/NS verification** (Chromium has
   misreported; flags are coupled). The v2 session uses `{exact:false}`-style
   constraint requests plus a **behavioral check**: the check-phase pilots are
   played at two known relative levels and the captured ratio must match
   (±0.5 dB); a mismatch fails the session loudly. This subsumes the ramp
   staircase's linearity role. (Operator note: browser-side AGC hygiene was
   separately verified on the household's devices; the behavioral check is the
   permanent regression guard, not an open question.)

Minor: v1's "target level adaptive to measured distance" was circular —
absolute distance is unrecoverable without loopback, and accept-and-normalize
removes the need to know it.

## 4. Prior-art conclusion (UX study, 2026-07-18)

Every shipping calibrator that owns its output chain — Genelec GLM/AutoCal,
Trinnov, Anthem ARC / Audyssey / YPAO, Sonos Trueplay — exposes **no level
control to the user**: fixed internal output reference, wide-dynamic-range
capture, normalize in post, continuous SNR gating with silent
discard-and-continue. The only products that push leveling onto the user are
Dirac Live and REW — precisely because they *don't* own the playback chain —
and it is their #1 documented friction. The v1 flow's ramp/lock/re-level loop
reproduced the Dirac anti-pattern inside a product that owns its entire chain.
JTS goes further than an AVR can: there is no external gain knob anywhere, so
user-facing leveling is eliminated outright. Proof-of-success prior art:
before/after overlay + one-tap audible A/B (Sonos/Trinnov pattern; Trinnov
adds phase — the high-credibility view for a crossover product).

## 5. The v2 architecture — the conductor model

### 5.1 Principles

1. **Phone = dumb recorder.** Per phase, the capture device does exactly one
   thing: record a known-length window and upload one WAV. No live feedback
   loops phone↔Pi mid-capture, no per-repeat gestures.
2. **Pi = conductor.** The Pi compiles an **excitation program** (a pure-data
   schedule of stimuli with per-segment digital gains and safety attestation),
   plays it as one continuous stream at a fixed session volume, and owns all
   sequencing.
3. **Analysis = pure functions.** `(program, capture WAV) → analysis` is
   deterministic and fixture-testable; every quantity (responses, trims,
   delay, polarity, drift, SNR, verdicts) derives from that pair. No
   side-channel state.
4. **Single owners.** One program compiler; one session volume plan; one
   analysis entry point per phase; apply consumes one fingerprinted candidate.
5. **Atomic, idempotent phases.** Re-running a phase replaces its evidence;
   resume = "which phases hold accepted evidence" — no interlocked
   sub-states.

### 5.2 The three phases

One mic position for the whole session: **~1 m on the listening axis**
(tweeter height, facing the speaker; picture on the placement screen). One
relay session spans all phases (§5.7).

- **CHECK (~25 s capture, one phone tap).** Program: leading silence (the
  session's ambient measurement, reusing the framed-ambient policy) + per
  driver, two short band-limited pilot chirps at two known relative levels
  (−10 dB apart). Analysis yields: ambient band floor, the behavioral AGC/
  linearity verdict (§3.4), channel-map sanity (energy lands in the expected
  band per driver), coarse polarity, mic/cal sanity, and the **solved gain
  plan** for MEASURE (target capture peak −12…−9 dBFS, ≥6 dB guard, SNR floor
  from ambient). Replaces: both per-driver ramp level-matches, the separate
  ambient wait before every sweep, and the AGC attestation gate.
- **MEASURE (~20 s capture, one phone tap).** Program (2-channel routing,
  §5.4): guard silence + **woofer sweep → tweeter sweep → woofer sweep
  repeat**, gaps sized by the MESM constraint (next IR must clear the prior
  sweep's harmonic-distortion pre-ring). Sweeps are ESS within each driver's
  declared band (≈3–4 s each — long LF reach is not needed at a ~250 Hz gated
  validity floor; bass belongs to the room/bass-extension passes). Analysis
  (§5.6) yields per-driver gated complex responses (cal applied), relative
  delay (drift- and parallax-corrected), polarity, trims, per-band SNR, and
  the drift/glitch verdict. Accept-and-normalize: hot-but-unclipped is
  accepted; a failed capture triggers **one** targeted retry with adjusted
  gain and a single specific reason.
- **REVIEW + APPLY (control page, no capture).** The proposed candidate —
  trims, polarity, delay, predicted summed response vs target — with one
  Apply action (existing fingerprint pattern). Crossover Fc/slope stay
  preset-owned, now with measured trims/delay/polarity.
- **VERIFY (~15 s capture, one phone tap, after apply).** One summed sweep
  through the applied production graph. Analysis: measured summed response
  vs the MEASURE-predicted sum and the target — ripple through Fc within
  tolerance = pass. Rendered as the before/after overlay; pairs with the
  existing A/B affordances as the user's proof.

User cost: place the mic once, ~3 phone taps + review/apply, **~2–3 minutes**.

### 5.3 Excitation program (new module, `jasper/audio_measurement/program.py`)

Pure data + pure composer; no I/O beyond WAV write.

```python
@dataclass(frozen=True)
class ProgramSegment:
    segment_id: str          # "ambient" | "pilot_w_hi" | "sweep_w" | "sweep_w_rep" | ...
    kind: str                # "silence" | "pilot" | "sweep" | "summed_sweep"
    role: str | None         # driver role; None for silence / summed
    channel: int | None      # program-WAV channel carrying the stimulus
    start_sample: int        # exact schedule inside the program WAV
    n_samples: int
    f1_hz: float | None
    f2_hz: float | None
    gain_db: float           # digital gain on the unit-peak stimulus
    effective_peak_dbfs: float  # gain + session volume + graph gain (admission input)

@dataclass(frozen=True)
class ExcitationProgram:
    program_id: str          # content hash (fingerprints analysis + candidate)
    phase: str               # "check" | "measure" | "verify"
    sample_rate_hz: int      # 48_000
    channels: int            # 2 for check/measure (ch0=woofer, ch1=tweeter); 1 for verify
    segments: tuple[ProgramSegment, ...]
    total_samples: int
```

Composers: `build_check_program(...)`, `build_measure_program(gain_plan, ...)`,
`build_verify_program(...)`. Every segment passes
`prepare_driver_excitation_plan` (band ⊆ permitted band, effective peak ≤
`min(profile cap, protection cap)` — the DE250 −65 dBFS HF cap lives there
already); the compiled program carries the per-segment attestations, and
playback re-admits from a fresh readback exactly as today
(`admitted_playback`). Sweep legs reuse `sweep.synchronized_swept_sine`;
inter-sweep gaps come from the MESM rule (gap ≥ expected IR length + harmonic
pre-ring for the preceding sweep's band).

### 5.4 Channel-routed program graph (static safety)

CHECK/MEASURE programs are **2-channel WAVs** played once through
`correction_substream`: a new commissioning graph variant maps capture ch0 →
the woofer output path and ch1 → the tweeter output path, each behind its
existing protection (tweeter protective HP, level caps). The schedule lives in
the WAV channels, so per-driver sequencing is sample-accurate while the
CamillaDSP graph stays **static and provable** — `graph_safety`'s
`tweeter_guard_present` / `output_highpass_protected` proofs apply unchanged,
and no graph reload happens mid-program (v1 loaded a fresh isolated graph per
sweep). VERIFY plays a mono sweep through the **applied production graph** —
measuring the real system, not a commissioning construct.

### 5.5 Session volume plan (SSOT)

A session-scoped owner replaces the per-step ramp machinery, reusing the
proven fail-closed latch trio from `CrossoverLevelLease`
(`_begin_volume_transition` → set-and-confirm → restore-once; unresolved ⇒ the
`volume_recovery` screen). Semantics: on session open, snapshot `main_volume`,
set the fixed measurement volume (constant across all phases — per-driver
level differences are digital, in the program; the 25 dB sensitivity spread is
handled by segment gains, not by re-leveling the speaker); restore exactly
once on session close/abandon. Deleted: `LevelMatchSession` use in the
crossover flow, `CrossoverLevelRunStore`, level locks, per-sweep
solve-corrections.

### 5.6 Analysis pipeline (pure, per phase)

`analyze_program_capture(program, samples, cal, geometry, priors) →
ProgramAnalysis`:

1. **Locate** the program in the capture: matched-filter the first stimulus
   (one global unknown offset); every other segment sits at its scheduled
   offset ± a small search window (generalizes the existing
   `_capture_to_magnitude` locator).
2. **Segment integrity:** per-segment peak/clip runs; schedule residuals.
3. **Drift (MEASURE):** ε = measured separation of the two woofer IRs /
   scheduled − 1. The baselines available (sweep-to-repeat, plus schedule
   residuals across all located segments) must agree within threshold —
   disagreement ⇒ glitch ⇒ reject capture (one retry). ε is stored with the
   evidence.
4. **Per-driver response:** deconvolve → `direct_arrival_window` + first-
   reflection gate → complex TF, mic cal applied; band SNR verdicts via the
   existing split policy; validity floor from the gate width.
5. **Alignment (MEASURE):** relative delay = tweeter-vs-woofer IR offset,
   ε-corrected, then **band-limited GCC-PHAT (≈Fc/2…2Fc) with sub-sample
   refinement on the upsampled correlation** (not raw parabolic); geometry
   prior (declared driver spacing) bounds the search (±2 ms) and the
   deterministic parallax term (√(r²+d²)−r at the prescribed ~1 m) is
   subtracted. Polarity from the correlation sign, cross-checked against the
   flatter predicted sum. Confidence gates reuse
   `cross_correlation_alignment`'s shape.
6. **Prediction/validation:** predicted summed response from the two complex
   TFs + candidate (trims, delay, polarity) vs target; VERIFY compares the
   measured sum against this prediction and the ripple tolerance.

New estimator code (ε, GCC-PHAT sub-sample) is net-new and lands with
synthetic-fixture tests: composed captures with injected known ε, delay,
polarity, noise, and dropped-buffer faults must round-trip within tolerance.

### 5.7 Relay/capture protocol: heterogeneous capture plans

Protocol v3 today runs "N repeats of ONE spec." v2 extends `CapturePlan`
(schema_version 2, additive — the spec comment already anticipated per-capture
presentation) with an optional per-capture entry table:

```python
@dataclass(frozen=True)
class CapturePlanEntry:
    index: int
    kind_label: str        # "check" | "measure" | "verify"
    duration_ms: int
    screen: Mapping | None # phone-side prompt/copy for this capture
```

The capture page's v3 session loop reads the entry for the next index
(duration + prompt); `begin_capture{index, attempt}` and the worker stay
untouched (the worker never parses specs — opacity preserved). Pi-side
admission maps index → phase and holds per-phase retry budgets; between
MEASURE and VERIFY the phone page shows the entry's "waiting for apply"
screen driven by host events. One session, one link, one TTL window.
Program durations fit comfortably inside existing caps: the longest capture
is ~25 s ≪ the 45 s hard timeout, ~2.4 MiB ≪ the 5 MiB WAV cap — no cap
bumps, only the locator window derives per-entry from the manifest instead of
the global constant.

### 5.8 Apply: trims + polarity + delay

Apply extends from trims-only to the full measured candidate: polarity flows
into the preset region fields (`lower_polarity`/`upper_polarity`) and delay
into the per-driver `Delay` filter (`ms`, ≤ the existing 20 ms DSP ceiling)
that `camilla_yaml` already knows how to emit — then `delay_graph` +
`graph_safety` re-prove the emitted graph as today. The candidate is
fingerprinted over `(program_id, analysis, proposal)` so apply freshness works
unchanged.

### 5.9 Deleted with this design (crossover flow scope)

The near-field geometry pass and the near-field/reference-axis handoff; both
per-driver ramp level-matches and `MeasurementRamp` use in this flow; per-
repeat tap admission and the 120 s windows; `CrossoverLevelRunStore` and level
locks; per-sweep solve corrections; the null-walk as the flow's delay source
(superseded by the single-capture estimator + VERIFY; the physical walk
remains available as an expert diagnostic until proven redundant on hardware).
Envelope steps collapse to
`("speaker_setup", "microphone_check", "measure", "review_apply", "verify")`
(schema 6 → 7); each retired screen's state machinery goes with it.

## 6. Wave plan

Each wave: implementer agent in an isolated worktree → hardware-free tests in
the same PR → adversarial review gate (0 blockers, 0 should-fixes) → green CI
→ squash-merge. Contracts in §5.3–§5.8 are frozen so waves can run in
parallel.

- **W1 — measurement core (pure).** `program.py` composer + locator/segmenter
  + drift estimator + GCC-PHAT sub-sample alignment + `analyze_program_capture`
  + prediction. Synthetic-fixture round-trip tests (known ε/delay/polarity/
  noise/glitch injection).
- **W2 — playback + safety.** Channel-routed commissioning graph variant +
  multi-segment excitation admission + `SessionVolumePlan` (latch-trio reuse)
  + program playback through the admitted-aplay path.
- **W3 — protocol.** `CapturePlanEntry` (spec + session loop + capture page);
  per-entry locator windows; worker untouched (opacity test).
- **W4 — apply extension.** Measured polarity/delay through preset →
  `camilla_yaml` → `delay_graph`/`graph_safety` proofs; candidate fingerprint
  over the new evidence.
- **W5 — flow + envelope collapse.** Phase orchestration in
  `correction_crossover_flow`/`backend` (check → gain solve → measure →
  review/apply → verify), envelope schema 7, screen JS, deletions (§5.9),
  test-suite rewrite for the collapsed states.
- **W6 — hardware validation.** Full run on JTS3 through real Chrome + relay
  + UMIK-2; before/after captured; design-doc benchmarks measured; docs
  verified-stamped.

W1/W3/W4 have no inter-dependencies; W2 consumes W1's manifest dataclass; W5
integrates all; W6 gates "done."

## 7. Acceptance benchmarks & empirical gates

- Median captures-per-phase ≤ 1.2 across sessions; zero silent AGC/NS
  corruption (behavioral check catches or session fails loudly).
- Relative-delay repeatability within ±1 sample @ 48 kHz (±20.8 µs) across 10
  sessions; ε baselines agree within threshold in every accepted capture.
- VERIFY summed ripple ≤ ±1.5 dB through Fc on the reference hardware.
- Wall-clock: mic placement → verified apply ≤ 5 min for a 2-way.

Empirical gates running alongside (not blocking W1–W4): the JTS3 + UMIK-2
drift probe (measured ε magnitude/stability between the DAC8x clock and a
desktop-USB capture chain) sizes the drift-correction thresholds; W6 measures
the same quantities through the real phone path. The design is robust to the
answer either way — every MEASURE capture carries its own drift/glitch
verdict, so population variance degrades to per-session retries, not silent
wrong alignments.

## 8. Primary sources

- **Appendix A** — v1 deep-research report (verbatim), including the citation
  list; §3 above corrects four of its operational claims after checking the
  primary sources.
- The e2e run that motivated this:
  [crossover-room-e2e-validation-log.md](crossover-room-e2e-validation-log.md).
- v1 of this document (the staged adaptation plan) is preserved in git history
  (the PR #1578 squash commit) for archaeology.

---

## Appendix A — deep-research report (primary source)

The full first-principles report — reasoning, worked clock-drift budget, and the
complete citation list (Keele JAES 1974; Farina log-sine; Bryan/Kolar/Abel AES
2010 Paper 8169; Gamper HSCMA 2017 + public MATLAB; REW acoustic-timing-reference
+ USB-mic guidance; VituixCAD excess-group-delay; TI SLLA122 ±50/±100 ppm) — is
preserved verbatim in
[crossover-measurement-deep-research-2026-07-18.md](crossover-measurement-deep-research-2026-07-18.md).
Read it with §3's corrections in hand: the drift budget there is stated as
gap-scaled (corrected to event-separation-scaled), the 10–20 cm distance is
proposed for alignment (corrected to ≥1 m listening-axis), and the REW 30%
figure is cited as a tolerance (corrected to a catastrophe threshold).

## Appendix B — research brief

The brief that produced the report (product context, the no-loopback
constraint, the per-step flow + UX + first-principles math, the tradeoffs, and
the ask) is archived at `deep-research-crossover-measurement-prompt.md`
(session artifact; available on request to add in-repo).

---

_Last updated: 2026-07-18 (v2 conductor design; pre-implementation)._
