# Crossover measurement & tuning — productization design

> **Shipped (2026-07-19).** This is the design/decision record — *why*
> the conductor flow exists and the alternatives it rejected. Waves
> W1–W6 are complete (PRs #1578–#1604); the v2 flow is hardware-validated
> on JTS3 + UMIK-2 and the `JASPER_CROSSOVER_FLOW` default flipped to
> `v2` on 2026-07-19. **Current operational truth — how to run it, the
> file map, invariants, the failure taxonomy, and the W6 bug catalog —
> now lives in
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md).**
> Read this doc for the decision archaeology; read the HANDOFF for what
> the flow does today. Legacy remains reachable via
> `JASPER_CROSSOVER_FLOW=legacy` until its scheduled W5b deletion.

> **Status: v2.1 design / decision record (2026-07-18, in implementation).**
> v1 (earlier the same day) synthesized the first-principles deep-research
> pass (Appendix A) into a staged adaptation plan. v2 — same day, after an
> adversarial validation pass against the primary sources, a prior-art UX
> study, and a full code-contract mapping — **corrects four v1 claims (§3)
> and replaces the staged adaptation with the conductor architecture (§5)**.
> v2.1 folds in an independent adversarial *design* review (3 blockers /
> 8 should-fixes, all accepted — §5.4 placement contract, §5.5 lifecycle,
> per-capture gain integrity, phase persistence, VERIFY semantics, failure
> taxonomy, scope boundaries). Wave status (updated 2026-07-19): W1–W6
> all complete and merged (W5b deletion pending); the shipped-state
> summary is in the banner above. Motivated by the end-to-end
> hardware-validation run
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

> **Amendment (owner ruling, 2026-07-20): the REVIEW + APPLY human gate
> below is superseded.** A hardware session proved it a dead end — a
> phone-only user cannot bounce to a second browser tab to tap Apply, and
> "apply this?" is unanswerable the instant after measuring (the household
> has no basis to judge a raw candidate). The system already gates
> candidate quality before MEASURE accepts and proof-checks VERIFY after;
> prior art (Sonos Trueplay, Genelec GLM, Anthem ARC) all measure → apply →
> verify automatically, with the human judgment happening AFTER, by ear,
> with undo available. The REVIEW+APPLY bullet's confidence nudge
> (`< 0.6`, "informed consent, not a gate") is now a hard MEASURE-phase
> gate instead: below the floor, MEASURE is rejected with guidance to
> re-measure at a cleaner mic position — never a question. A trusted
> candidate is applied automatically by the conductor, on its own
> background thread, immediately after MEASURE accepts. The soft-held
> deferred-VERIFY mechanism described below is UNCHANGED; only the release
> trigger moved from a human tap to the auto-apply completing. Current
> operational truth (screen names, reason codes, the RESULT screen shape)
> lives in
> [`HANDOFF-crossover-measurement-v2.md`](HANDOFF-crossover-measurement-v2.md)
> gotcha #18. The rest of this section is preserved for design-rationale
> archaeology — read it for the "why," not for the current screen
> sequence.


One mic position for the whole session: **~1 m on the listening axis**
(tweeter height, facing the speaker; picture on the placement screen). The
parallax budget in §3.2 assumes this placement within a tolerance window of
roughly ±0.3 m distance and ±10 cm height — comfortably inside the ~16°
residual by the same formula — and the placement screen's copy/picture
encodes that window, so a "roughly right" placement is genuinely fine. One
relay session spans all phases (§5.7), and the capture page holds **one
MediaStream for the entire session**: any track end/mute/re-acquisition is
reported to the Pi and requires the behavioral gain check to re-pass before
the next capture is admitted (browser AGC can silently return with a
re-acquired stream). Because CHECK-only verification cannot protect the
later captures, **every MEASURE and VERIFY program also opens with a short
two-level pilot pair (~2 s)** so each capture carries its own linearity
evidence, and MEASURE acceptance additionally requires the woofer repeat
pair to agree in level within ±0.3 dB (a gain-riding detector complementing
the timing baselines). The VERIFY pilot pair rides its own flat mid-woofer
band (~200–800 Hz, hi bound clamped to Fc/2.5 for low-Fc presets, with an
[Fc/8, Fc/4] fallback), NOT the summed sweep's full band (W6.7 — the sweep
deliberately crosses the crossover overlap to see the applied interference
notch; a pilot chirp swept through that same notch goes noise-dominated and
misfires the linearity ratio on noise rather than AGC behavior).

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
  preset-owned, now with measured trims/delay/polarity. When the alignment
  estimator's confidence is low (< 0.6, provisional — W6.7), the screen
  carries a warn nudge suggesting a re-measure at a cleaner mic position;
  Apply is NOT blocked (informed consent, not a gate).
- **VERIFY (~15 s capture, one phone tap, after apply).** Two-level pilot
  pair + one summed sweep through the applied production graph. **Pass =
  |measured sum − predicted sum| ≤ ±1.5 dB over [Fc/2, 2·Fc] at 1/6-octave
  smoothing, with VERIFY analysis windowed to MEASURE's accepted gate
  parameters**; two exclusions narrow that comparator before the ±1.5 dB
  test runs, both keyed on the PREDICTED side, both applied to RMS and MAX
  alike (W6.9): bins where the predicted sum sits more than 12 dB
  (provisional — W6.7) below its own band median are excluded — inside a
  predicted interference notch, depth agreement is hypersensitive to
  sub-dB/sub-degree branch differences and is not a meaningful tracking
  signal (the W6 run-7 hardware failure: a 27.83 dB raw max against a
  predicted sum whose own ripple was ~30 dB); and bins below THIS capture's
  own gate-derived validity floor (`gating.f_valid_floor_hz` of the applied
  reflection gate) are excluded outright, generalizing the notch exclusion
  from "deep predicted notch" to "below measurement validity" (W6.9 — a
  room reflection close enough to gate VERIFY's measured sum tighter than
  the nominal band makes anything under that floor an artifact of a
  truncated window, not evidence about driver alignment). Both the raw
  full-band numbers and the clamped/excluded comparator are reported (the
  former as a diagnostic only, never gating). **The gate-comparability rule
  described just below now covers the prediction path by construction**: the
  run-7/8 hardware failures were traced to the MEASURE-side prediction
  composing each branch from a FIXED 65 ms window regardless of gate state,
  so a room reflection inside that tail was baked into the predicted sum
  even though VERIFY's adaptively-gated measured sum never had it —
  invisible to the comparability rule because it only ever compared the two
  ADAPTIVE gates (MEASURE's driver responses vs VERIFY's summed response)
  and had no way to see the fixed-window prediction at all. The prediction
  path now shares the identical adaptive reflection gate `_driver_response`
  uses, so there is no longer a hidden third window for the rule to miss.
  A mic position close enough to a hard reflective surface can still push
  the validity floor high enough to make part of [Fc/2, 2·Fc] structurally
  unverifiable at that position — that is an honest limit of the
  measurement, not a new gap, and the existing ~1 m on-axis placement
  prompt above is the mitigation already in place, not a new one. If
  VERIFY's own detected first reflection forces a shorter gate than
  MEASURE's, the verdict is "inconclusive — re-verify," not fail
  (a different gate manufactures overlay differences that aren't driver
  alignment). Target-tracking is displayed but does not gate. **On fail: the
  applied graph stays in force** (it is proof-checked safe regardless); the
  user is offered Re-verify (capture again), Re-measure (back to MEASURE,
  evidence replaced), or Restore previous (the existing apply-rollback
  path), with one specific reason shown — ANY failure code surfacing once
  VERIFY is reached (not just the two VERIFY-specific reasons) renders this
  same screen, because the candidate is already applied by that point and
  the household is entitled to the Undo affordance regardless of which
  check failed (W6.7 — a run-7 `agc_behavioral_fail` mid-VERIFY had
  rendered the ordinary fix_and_retry screen instead, hiding Undo).
  Rendered as the before/after overlay; pairs with the existing A/B
  affordances as the user's proof.

User cost: place the mic once, ~3 phone taps + review/apply, **~2–3 minutes**.
**Only the first capture of a session requires a tap:** an accepted CHECK
auto-advances into MEASURE behind a visible, cancelable countdown, and the
apply-complete host event auto-arms VERIFY the same way — "one tap per
phase" is the upper bound, not the design. Auto-advance also protects
validity: a user returning to the phone cold is the likeliest
mic-displacement event between MEASURE and VERIFY. (The shipped
`CapturePlanEntry.screen` field already carries per-entry presentation;
this is a page policy, not a protocol change.)

The VERIFY fail screen leads with one default — "Try again" (internally:
re-verify once, then re-measure) — plus "Undo (restore previous sound)";
the explicit Re-verify / Re-measure / Restore trio lives behind the expert
disclosure. The ±0.3 dB repeat-agreement and drift-agreement thresholds are
provisional constants to be re-derived from W6 bench distributions; a
repeat-level failure reuses the `drift_baselines_disagree` reason code —
never a new user-facing code.

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
already). **Program admission attests N per-segment plans plus two
per-channel whole-file facts recomputed from the rendered WAV bytes: each
channel's true peak ≤ its driver's admitted cap, and out-of-segment channel
energy below a floor** — that is what makes the attestation about the
artifact rather than about intentions; the static graph's target filter +
caps remain the structural backstop. Playback re-admits both facts from a
fresh readback exactly as today (`admitted_playback`). Sweep legs reuse
`sweep.synchronized_swept_sine`; inter-sweep gaps come from the MESM rule
(gap ≥ expected IR length + harmonic pre-ring for the preceding sweep's
band).

### 5.4 Channel-routed program graph (static safety)

CHECK/MEASURE programs are **2-channel WAVs** played once through
`correction_substream`: a new commissioning graph variant maps capture ch0 →
the woofer output path and ch1 → the tweeter output path. **Each channel
carries the *target* crossover filter for its driver** (the LR4 high-pass for
the tweeter — which satisfies the declared protective-HP floor by
construction, since Fc is aligned to it — and the LR4 low-pass for the
woofer), plus the existing level caps. Measuring *as-crossed branches* makes
the two responses directly summable (`P = W_xo + s·T_xo·e^{−jωτ}`), removes
any protective-filter double-counting from the prediction, and keeps the
tweeter behind its final ≥24 dB/oct high-pass during every excitation. The
schedule lives in the WAV channels, so per-driver sequencing is
sample-accurate while the CamillaDSP graph stays **static and provable**, and
no graph reload happens mid-program (v1 loaded a fresh isolated graph per
sweep). **Placement contract (load-bearing):** the routing is a **new mixer
mode** (program ch0 → woofer path, ch1 → tweeter path); the target LP/HP,
protective HP, limiter, and level caps remain **on the physical output
channels** — the positions `output_highpass_protected` and
`tweeter_guard_present` actually inspect. Filters must NOT be emitted
per-program-channel pre-mixer: on the 2-way preset, program ch1 numerically
coincides with tweeter output 1, and the subset-of-role guard cannot
distinguish a pre-split ch-1 HP from the post-split tweeter HP (a false-PASS
shape). W2 ships a contract test proving the routed variant passes both
proofs and that a pre-split per-channel-HP variant is rejected. VERIFY plays
a mono sweep through the **applied production graph** — measuring the real
system, not a commissioning construct.

### 5.5 Session volume plan (SSOT)

A session-scoped owner replaces the per-step ramp machinery, reusing the
proven fail-closed latch trio from `CrossoverLevelLease`
(`_begin_volume_transition` → set-and-confirm → restore-once; unresolved ⇒ the
`volume_recovery` screen). Semantics: on session open, snapshot `main_volume`,
set the fixed measurement volume (constant across all phases — per-driver
level differences are digital, in the program; the 25 dB sensitivity spread is
handled by segment gains, not by re-leveling the speaker); restore exactly
once on session close/abandon.

**The session volume's source:** derived so the **least-sensitive
(highest-cap) driver** reaches the measurement reference level with digital
headroom — `min(MEASUREMENT_REFERENCE_VOLUME_DB = −20 dB, max(caps))`,
refused with a typed error when the result is at or below the −60 dB
emergency floor; more-sensitive drivers are digitally **attenuated down** to
their own caps (always satisfiable downward). The 2026-07-18 W2 adversarial
gate caught the min-cap misreading of this section's earlier text: a
min-cap-derived volume pins the least-sensitive driver ~40 dB under its own
ceiling and collapses its SNR below the trim floor. The reference constant
is provisional pending W6 bench validation. It is an input to program
admission (one definition path), and it is **never adjusted after CHECK**:
the gain solve operates strictly within [SNR floor, 0 dBFS − 6 dB guard]; if
infeasible at max gain, the session fails with a named reason ("move the
phone closer" / "the room is too loud right now").

**Abandon is a defined event set** (the latch trio is reused for its crash
semantics, not its lifecycle — today's lease is per-step with no TTL, and its
recovery route refuses `active` states): the plan's durable state carries an
`opened_at` timestamp, and each of (a) explicit stop from either surface,
(b) relay-session end/TTL expiry (the Pi owns the session and observes its
death), and (c) a hard wall-clock ceiling on the held plan (~1800 s ≈ 2× the
relay TTL, enforced live and on hydration) drains the restore-once path; if
restore cannot confirm by readback, the state latches unresolved →
`volume_recovery`. A user who walks away can never leave the speaker pinned
at measurement volume.

Deleted: `LevelMatchSession` use in the crossover flow,
`CrossoverLevelRunStore`, level locks, per-sweep solve-corrections.

### 5.6 Analysis pipeline (pure, per phase)

`analyze_program_capture(program, samples, cal, geometry, priors) →
ProgramAnalysis`:

1. **Locate** the program in the capture: matched-filter the first stimulus
   (one global unknown offset); every other segment sits at its scheduled
   offset ± a small search window (generalizes the existing
   `_capture_to_magnitude` locator).
2. **Segment integrity:** per-segment peak/clip runs; schedule residuals.
3. **Drift (MEASURE):** ε = measured separation of the two woofer IRs /
   scheduled − 1, derived from the **longest available baseline** (the
   repeat pair spans the whole program by construction; the 2026-07-18 bench
   probe showed the short-baseline estimate is ~10× noisier). The baselines
   available (sweep-to-repeat, plus schedule residuals across all located
   segments) must agree within threshold — disagreement ⇒ glitch ⇒ reject
   capture (one retry). ε is stored with the evidence.
4. **Per-driver response:** deconvolve → `direct_arrival_window` + first-
   reflection gate → complex TF, mic cal applied; band SNR verdicts via the
   existing split policy; validity floor from the gate width.
5. **Alignment (MEASURE):** band-limited GCC-PHAT over the true branch-sweep
   overlap supplies a sub-sample, ε-corrected seed (not raw parabolic),
   polarity, and capture-quality confidence. The applied delay is then chosen
   by minimizing summed ripple over that same overlap inside the active
   crossover region's declared `delay_range_ms` magnitude range, plus the
   shared plausibility margin. The full-IR GCC seed is residualized once by
   the branch crop's argmax offset, then supplies the sign and centers one
   ±half-period comb lobe; a fresh preset need not have an applied
   `delay_target_driver` yet. The selected/applied value is the residual in
   the independently argmax-referenced branch frame — the full-IR crop offset
   is never added back as a second physical delay. The deterministic parallax
   term (√(r²+d²)−r at the prescribed ~1 m) is the only coordinate transform
   restored for the measurement-mic prediction, so VERIFY remains comparable.
   Polarity comes from
   the correlation sign, cross-checked against the
   flatter predicted sum. The confidence gate remains explicitly GCC-seed
   capture confidence; flatness seed/objective improvement and boundary state
   are stored separately rather than mislabelling it as confidence in the
   selected minimum.
6. **Prediction/validation:** because MEASURE captures as-crossed branches
   (§5.4), the predicted applied sum is directly
   `W_xo·10^(trim_w/20) + s·T_xo·10^(trim_t/20)·e^(−jωτ)`; trims level-match
   the branches through the crossover region and the candidate (trims, s, τ)
   is validated against the target's ripple tolerance. VERIFY compares the
   measured post-apply sum against this prediction.

New estimator code (ε, GCC-PHAT sub-sample) is net-new and lands with
synthetic-fixture tests: composed captures with injected known ε, delay,
polarity, noise, and dropped-buffer faults must round-trip within tolerance.

**Phase persistence & session binding (the W5 contract):** each phase
publishes its evidence and derived plan — CHECK: ambient report + behavioral
verdict + `GainPlan`; MEASURE: `ProgramAnalysis` + candidate — as
evidence-store artifacts under the session's commissioning run; the v2
measured candidate rides the existing publish → tamper-checked reopen →
apply → crash-restore chain (`commissioning_service.publish_candidate` and
kin), re-keyed to `(program_id, analysis, proposal)`. MEASURE's SNR verdicts
consume CHECK's framed ambient; MEASURE's own guard silence is a cross-check
only. **Phase evidence is bound to the relay session**: a new session
invalidates CHECK and MEASURE evidence (mic position is unverifiable across
sessions) — resume within one session skips accepted phases; resume after
session death restarts at CHECK, which is 25 s by design precisely so this
is cheap.

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

Apply carries the full measured candidate. (Precisely: the apply
*transaction* already moved per-role `{gain_db, delay_ms, inverted}`
corrections end-to-end — what is new is a **measured** candidate writing
polarity into the preset region fields and a measured delay value, plus the
v2 evidence core.) Shipped as `MeasuredCrossoverCandidate` (W4, merged):
delay/polarity fold through `corrections` into the single emitter
(`emit_active_speaker_baseline_config` — Delay filter in ms via the
single-owner `quantized_delay_ms`, ≤ the 20 ms DSP ceiling; inversion via the
per-driver Gain, no double-inversion), proven pre-apply by
`prove_static_delay_binding` + the `graph_safety` protection proofs — a
failed proof blocks with `correction.crossover_alignment_proof_blocked` and
no partial write. The candidate is fingerprinted over `(program_id,
analysis, preset, trims, alignment)` via the existing `json_fingerprint`
recipe so apply freshness works unchanged.

### 5.9 Deleted with this design (crossover flow scope)

The near-field geometry pass and the near-field/reference-axis handoff; both
per-driver ramp level-matches and `MeasurementRamp` use in this flow; per-
repeat tap admission and the per-capture operator windows (the relay
session's `DEFAULT_TIMEOUT_S = 120 s` budget stays as the transport backstop;
what dies is the tap-per-repeat cadence gated on it); `CrossoverLevelRunStore`
and level locks; per-sweep solve corrections; the null-walk as the flow's
delay source (superseded by the single-capture estimator + VERIFY; the
physical walk remains available as an expert diagnostic until proven
redundant on hardware). The envelope's step *tuple* stays five entries —
`("speaker_setup", "microphone_check", "measure", "review_apply", "verify")`
(schema 6 → 7; the fourth step is renamed `"apply"`, schema 8, per the 2026-07-20
owner ruling in §5.2's amendment) — the real deletion is the sub-state
machinery inside the steps (ramp/level-lock/near-field/comparison-set
logic); each retired screen's state machinery goes with it.

### 5.10 Failure taxonomy (the W5 screen contract)

**These are internal reason codes, not screens.** W5 ships at most four
screen templates, each parameterized by reason copy — (1) silent auto-retry
banner (`clipped`, `drift_baselines_disagree` — per §4's own prior-art
conclusion, the first retry of a transient code is automatic, no decision
screen), (2) fix-and-retry prompt (`snr_floor`, `delay_exceeds_search_window`,
`locate_failed`, `agc_behavioral_fail`), (3) hard stop
(`channel_map_mismatch`), (4) session restart (`relay_timeout`).
`volume_unresolved` and the VERIFY-fail screen already exist or are defined
in §5.2. Every terminal verdict has one owning phase, one retry budget, and
one-reason/one-action copy:

| Code | Phase | Retry budget | Action copy shape |
|---|---|---|---|
| `agc_behavioral_fail` | CHECK (re-armed on stream change) | 1 | name the browser/AGC cause; retry after re-permission |
| `noisy_room_linearity` | CHECK | 1 | same captured-vs-programmed pilot-delta symptom as `agc_behavioral_fail`, but the CHECK gain solve's own SNR-floor verdict against this capture's ambient bands is ALSO failing, so the room — not the phone's AGC — is named (W6.12: hardware round 4 proved a desk-ambient burst can trip the linearity gate with the phone's AGC verifiably off; distinguishing the two needed no new instrumentation, just reading `gain_plan.snr_floor_ok`, already computed independent of the linearity outcome) |
| `snr_floor` | CHECK / MEASURE | 1 | "room is too loud right now" / "move the phone closer" |
| `channel_map_mismatch` | CHECK | 0 (hard stop) | "check speaker wiring, or if the room is noisy, quiet it" — never auto-swap |
| `clipped` | MEASURE / VERIFY | 1 (gain-adjusted) | automatic quieter retry, say so |
| `drift_baselines_disagree` (glitch) | MEASURE | 1 | "capture glitched — retrying" |
| `delay_exceeds_search_window` | MEASURE | 1 | re-check mic placement vs the picture |
| `locate_failed` | any capture | 1 | "couldn't hear the speaker — check volume/mic" |
| `relay_timeout` / session death | any | new session | re-open link; CHECK restarts (evidence invalidated) |
| `volume_unresolved` | session | — | existing `volume_recovery` screen |
| `verify_out_of_tolerance` | VERIFY | 2 (re-verify) | offer Re-verify / Re-measure / Restore previous |

### 5.11 Scope boundaries (non-goals & doors)

- **3-way is a named non-goal for v2.** The program/WAV layer generalizes
  (N channels, per-segment roles), but the candidate must reshape from one
  alignment triple to per-boundary `(trim, s, τ)` entries and the prediction
  becomes an N-branch sum — a schema change, not an additive wave. The `mid`
  `delay_role` is refused as ambiguous today (W4) on purpose.
- **Subwoofer/main alignment is owned by the bass-extension program.** This
  flow measures nothing below its gated validity floor and hands off nothing
  else.
- **Fc/slope re-derivation and driver EQ beyond trims are a v3 door, not a
  permanent closure.** v2 deliberately measures as-crossed and cannot recover
  them (dividing out the target filter explodes stopband noise). The door
  opens with one additive program variant capturing raw branches behind the
  protective-only HP — a second graph variant + segments, no change to the
  conductor architecture.

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
- **W5a — the v2 happy path (unblocks W6).** Phase orchestration as a NEW
  path in `correction_crossover_flow`/`backend` (check → gain solve →
  measure → review/apply → verify), envelope schema 7, the auto-advance tap
  policy, the four failure-screen templates (§5.10), phase persistence +
  session binding (§5.6), and the MEASURE/VERIFY leading pilot pair +
  repeat level-agreement acceptance (extends W1's composers per §5.2).
  **The legacy flow stays intact and reachable as the fallback.**
- **W5b — deletions + polish (gated on W6's first green hardware run).**
  The §5.9 deletions and legacy test retirement (the big rewrite), failure
  copy polished from W6's *observed* failures, the VERIFY-fail expert
  disclosure, A/B polish. Hardware truth arrives before the 5.5k-line
  deletion — deleting the only working flow before the replacement has
  touched hardware is the one sequencing risk this plan refuses.
- **W6 — hardware validation (after W5a).** Full run on JTS3 through real Chrome + relay
  + UMIK-2; before/after captured; design-doc benchmarks measured; docs
  verified-stamped.

W1/W3/W4 have no inter-dependencies; W2 consumes W1's manifest dataclass;
W5a integrates W1–W4 (legacy kept as fallback); W6 runs on W5a; W5b — the
§5.9 deletions — is gated on W6's first green run and closes "done."

## 7. Acceptance benchmarks & empirical gates

- Median captures-per-phase ≤ 1.2 across sessions; zero silent AGC/NS
  corruption (behavioral check catches or session fails loudly).
- Relative-delay repeatability within ±1 sample @ 48 kHz (±20.8 µs) across 10
  sessions; ε baselines agree within threshold in every accepted capture.
- VERIFY summed ripple ≤ ±1.5 dB over [Fc/2, 2·Fc] @ 1/6-octave (per §5.2's
  VERIFY definition) on the reference hardware.
- Wall-clock: mic placement → verified apply ≤ 5 min for a 2-way.

**W6 operationalization** (a single bench campaign can't measure fleet
medians, and "zero silent corruption" is unfalsifiable without a negative):
≥ 9 of 10 scripted bench sessions accept the first capture of every phase;
one deliberate negative trial per device class (constraints omitted / AGC
forced on) MUST be rejected by the behavioral check — a negative trial that
passes silently fails the wave; delay repeatability and VERIFY ripple as
stated.

Empirical gates: the JTS3 + UMIK-2 bench probe ran 2026-07-18 (five trials,
three scheduled sweeps per capture at 0/3/10 s through the mux test-gate →
`correction` lane → production chain; IR peak-to-noise 42–44 dB): **measured
ε ≈ 29.3–30.0 ppm, constant within each capture (0.1–2.2 ppm intra-trial
spread) and stable across trials (σ ≤ 0.4 ppm on the mid/long baselines)**.
Residual relative-delay error after a repeated-sweep correction projects to
~0.6–4.1 µs (1σ) for a ~10 s program — comfortably inside the ±20.8 µs
budget — while the short 3 s baseline alone is ~10× noisier, hence the
longest-baseline rule in §5.6.3. Uncorrected, the same rig would accumulate
~200–300 µs across a program — confirming §3.1's "the repeat is mandatory."
W6 re-measures these quantities through the real phone path. The design is
robust to population variance either way — every MEASURE capture carries its
own drift/glitch verdict, so a bad clock degrades to a per-session retry, not
a silent wrong alignment.

**W6 first-contact findings (2026-07-18).** The first JTS3 runs
(protected-tweeter reference, caps woofer −8 / B&C DE250 tweeter −65
dBFS-effective) surfaced five defects, all fixed and pinned hardware-free
before the acceptance run (W6.1): (A) CHECK/VERIFY programs weren't cap-clamped;
(B) seam exceptions escaped the runner silently — the seams raise open-endedly
(`CamillaUnavailable` is a bare `Exception`), leaving the volume active, the
relay leaked, and the phone frozen — closed with a catch-all cleanup arm
(terminal host event + persisted `program_unplayable`/`internal_error` + volume
drain + purge + re-raise), not an enumerated exception list; (C) the session
volume was protected only per-play, so the idle reconciler reverted it — the
session now holds one measurement window whose abort target the per-play path
registers, keeping the mux gate-lease abort able to stop an in-flight sweep;
(D) `/crossover/status`+`/envelope` never matched the `crossover_v2:*` relay
slot; (E) the stale-active reset, recover-volume routing, and 1800 s ceiling
didn't actually recover.

**W6.5 — sensitivity-derived HF measurement ceiling (2026-07-19 ruling).**
The −65 dBFS tweeter cap above was hardware-measured to read near-inaudible
(27 dB in-band SNR) against the woofer's comfortable −26 dBFS-effective
pilots — a 25.2 dB sensitivity delta (B&C DE250-8 ~108.5 dB vs Dayton Epique
E150HE-44 ~83.3 dB) the naked-tone class default never accounted for. Driver
protection is now exactly two invariants, one owner each: wrong-frequency-range
(the declared hard band + the proven protective HP, unchanged) and too-loud
(one derived ceiling instead of stacked hedges). On the program-admission path
only — a graph that carries the driver's crossover HP by construction — an
HF driver's ceiling is derived from a low-frequency sibling's own cap and the
two drivers' declared sensitivities, `min(declared_lf_cap − (sens_hf −
sens_lf), −35 dBFS)`, superseding the class-default seed only when the
household hasn't already typed a real, different value. Every other caller
(isolated driver capture, the v1 ramp solver, ear-check ramps) is unaffected.
The sensitivities' one owner is the DECLARATION — the design draft's
`manual_settings` (`declared_driver_sensitivities`), never a second copy on
the confirmed safety profile — so already-declared boxes (JTS3's persisted
draft carries 83.3/108.5 today) fire the derivation with zero migration. The
conductor context resolves caps on this path and threads the same declared
mapping into program admission AND play-time readmission, so composed levels
and the admission gate cannot disagree about a derived ceiling.
See `jasper.active_speaker.driver_protection.derive_hf_measurement_ceiling_dbfs`,
`jasper.active_speaker.design_draft.declared_driver_sensitivities`, and
`jasper.active_speaker.excitation_safety_plan.resolve_driver_excitation_ceilings`.

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

_Last updated: 2026-07-19 (v2.3 — W6.9 forensics fixes folded: the VERIFY
tracking comparator (RMS and MAX, and the notch-exclusion bin set) now clamps
to this capture's own gate-derived validity floor, and the MEASURE-side
prediction (`_aligned_branch_tf`) now shares VERIFY's adaptive reflection gate
instead of a fixed 65 ms window, closing the run-7/8 hardware bug where a room
reflection was baked into the predicted sum invisibly to the gate-comparability
check; v2.2's W6.7 measurement-honesty fixes folded: VERIFY notch-excluded MAX
tracking, the VERIFY pilot pair's own flat band, the VERIFY-phase
failure-screen override, and the review_apply low-confidence nudge; v2.1
design-review amendments folded; W1–W6 complete, default flipped to v2
2026-07-19, W5b deletion pending)._
