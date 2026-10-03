# ADR-0433: A passing MEASURE take is kept, and its raise rides the next take

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step D2, under the owner's rule that a passing take is never replayed. Amends
  [ADR-0417](0417-the-courtesy-prelude-announces-each-run-once.md) §4, quoted below.

## Context

Finding F5 of the [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): a passing
take is replayed every speaker round. On jts3 ([#6113](https://github.com/jaspercurry/JTS/issues/6113)
runs 2 and 3) the first MEASURE take passed, but its tweeter read 33.1 and 33.0 dB of alignment SNR
against 35 dB. The verdict asked `retake_louder` (the alignment-only raise in
`capture_dispatch._assess_recording`). The SPL stop held the raise to 2.62 and 3.29 dB of the 8 dB it
asked. The replay read 35.7 and 36.3 dB, and in run 3 its delay moved 2 µs with the same polarity and
residual: it changed nothing material. The replay played 36.3 s, and the first take was discarded.

The raise was already carried to every later take: the run's gain plan is session-wide
(`CrossoverV2Session.rearm_measure_after_transient`). So the replay added only a play. Only the mark's
timing (azimuth 0°, elevation 0°) feeds a decision: `alignment_evidence.commissioning_alignment` (the
packet's next action and a composed candidate's commissioning alignment) and ADR-0345's timing read
(which the analysis makes on the design axis only) take the newest kept take there. An off-axis take's
alignment feeds none, and #6227 D3 plans off-axis takes of one sweep per driver, which read about
4.8 dB less alignment SNR, so they would ask the raise often.

## Decision

1. **A take is kept when its raise can ride later takes.** A MEASURE take whose magnitude passes and
   whose alignment SNR alone asks a raise (`alignment_only`) is accepted with no charge when it is off
   the mark, or when it is at the mark and the plan holds a later MEASURE take there
   (`CrossoverV2Session.raise_rides_next`). The mark is `contracts.on_design_axis`, the test that
   `commissioning_alignment` reads too. The take's evidence keeps the raise (`next_gain_db.<role>`) and
   its alignment levels (`alignment.<role>.*`). The level-drift merge in `capture_dispatch.assess`
   treats it as any accepted take.
2. **The raise moves the gain plan.** The web host rearms the session's gain plan from every MEASURE
   verdict that names a raise, kept or retaken (`correction_run_host.bind_plan_analysis`). So every
   later MEASURE take composes at the raised gain, at whatever pose. Off the mark with no later MEASURE
   take, the raise is moot. The raise rule, its caps and its SPL bound do not change.
3. **The last take at the mark is retaken, as before.** A take at the mark with no later MEASURE take
   there is retaken at its raise, when that raise can lift it to its floor (§4): the one take of
   `tournament/express` and the fourth mark take of `baseline_express`. So is a take whose magnitude
   also fails. If the newest take at the mark were a short take,
   [ADR-0345](0345-a-timing-reading-that-is-not-comparable-never-asks-for-a-reset.md) would read the
   saved timing as `not_comparable` (`snr_short`) and ask for `measure_timing`, a new round. That still
   happens when the later take at the mark never lands (the operator presses Complete, its placement is
   spent, or the run stops): `raise_rides_next` reads the plan, not what lands, so the kept short take
   is then the newest there, in a run that is partial anyway. The owner may change either rule.
4. **A raise its caps hold under the shortfall is not replayed.** In the same room, a replay reads
   each role's alignment SNR higher by its raise. So when the driver's ceiling or the SPL headroom
   holds a take's raise under its own shortfall in any role, a replay at that raise cannot reach the
   floor and could not change the answer, the rule of
   [ADR-0428](0428-a-placement-stops-after-two-attempts-with-the-same-fault-and-reading.md). The take
   is kept with its evidence, and the host still rearms by the capped raise. This removes that take's
   replays, which raised it 0.18, 0.01 or about 0 dB at a time until its placement was spent; where
   they would have spent it, the run goes on at the capped raise (the first Hearing case below). The
   kept take is still short, so as the newest take at the mark it makes ADR-0345 ask for
   `measure_timing`, as the last of its replays did.
5. **A set keys on its stimulus's shape.** A run-manifest set keys on the capture fields that
   [ADR-0408](0408-over-a-timing-take-each-candidate-graph-probes-its-own-graph.md) compares two takes
   on (`measurement_context.SHAPE_FIELDS`: the side, the capture device, the fader `level_db` and
   `stimulus_shape_id`), with the graph, role and calibration fields as before
   (`run_manifest.set_basis`). It never keys on the level-bearing `stimulus_dbfs`, `stimulus_id` or
   `stimulus_peak_dbfs`. Takes that differ only in how loud they played share a set, and each row keeps
   its own level. This keeps [ADR-0299](0299-one-evidence-manifest-per-run.md)'s rule that "Sets group
   one configuration and capture condition across poses" (line 21), with a stimulus's level read as no
   part of that condition.
6. **The level-drift reference keeps the level-bearing id.** `RunManifest.level_observation` still
   finds a take's reference by its fader and its `stimulus_id`. The reference is one broadband
   loudest-window level, from whichever driver plays loudest, so subtracting one driver's raise from it
   could falsely trip the 2 dB same-pose bound. So the first take after a raise has no drift
   reference, as a replay has none today.
7. **Readers follow the set key.** The near-field level mismatch (`driver_level_mismatches`) reads
   each take's own gain and fader, and `speaker_fit` matches a take to its set by shape.

### What this amends

- ADR-0417 §4, lines 21–23: "A take keys its run-manifest set and compares with other takes on the
  stimulus it measures: `program.take_stimulus_id`, its program's `stimulus_id` less the prelude, and
  the stimulus shape likewise." A take now keys its set on the shape, which is prelude-free and
  gain-free (§5 above). Its drift reference and its record's `stimulus_id` still use
  `program.take_stimulus_id`.

## Consequences

- **Hearing:** a run that plays to its end plays the plays of the flow before this ADR, less the
  replays, each at its pose and gain: a later take composes from the gain plan as the replay's verdict
  raised it. Where a replay would itself have asked a further raise, the next take plays under the
  gain the flow before played there and asks its own raise from its own reading. Two cases play what
  the flow before did not, and one case stays as it was:
  - A run whose placement was spent ended `retries_spent` at the replay. It now keeps the take and
    plays the rest of the run at the raise. Those plays are new for that run; the 85 dB watch bounds
    them.
  - A replay whose sweep clipped cut every role by 3 dB (`CLIP_RETRY_BACKOFF_DB`). With no replay
    that cut is skipped, so a later take can play louder than in the flow before at the same pose, and
    skipped cuts add up across kept takes.
  - As before this ADR, a raise sized at a quiet pose rides the run's later takes, and at a louder
    pose only the 85 dB watch holds it.

  Every raise stays inside the SPL headroom that its own take read, by the same rule, caps and bound.
  `volume_limit` 0.0, the graph doors, the `set_volume_db` clamp, the 85 dB commissioning stop and its
  watch, the declared driver caps and ADR-0405's probe staircase do not change.
- Proof: `test_a_short_measure_take_is_kept_unless_it_is_the_last_at_the_mark` runs the executor through
  the web host's analysis and composer, on a fake chain whose first MEASURE take reads 33 dB of tweeter
  alignment SNR with 3.3 dB of SPL headroom, beside the same run with the rule switched off. At
  `speaker/mark` (two takes at the mark), off the mark (a take at 20°, then one at −20°), and with one
  take at 20° only, the run plays the plays of the run before less the first take's replay, each at its
  bearing and its gains. With one take at the mark (`tournament/express`), or a take at the mark and
  then one at 20°, the run is unchanged. A kept take shares each driver's set with the takes after it.
  `test_alignment_only_retry_uses_driver_and_spl_headroom` pins §4: a take whose raise its caps hold
  under its 6 dB shortfall is kept, one whose raise can reach the floor is retaken, and one whose
  magnitude fails is retaken. On the same executor run (a scratch harness, not committed), a
  `tournament/express` take whose SPL headroom held its raise to 0.18 dB against a 2 dB shortfall
  played twice more and ended the run `retries_spent`; it is now kept, and the run completes.
- On a two-way speaker a MEASURE take plays six sweeps and the timing take one. A `speaker/mark` round
  whose first take reads short plays 13 sweeps, not 19. A replay now follows only the last take at the
  mark, at a raise that can reach its floor, so the replays a round can add, one for each MEASURE take,
  fall from 12 sweeps to 6 on `speaker/mark`, from 48 to 6 on `baseline_express` (49 clean) and from
  96 to 6 on `baseline_full` (97 clean). Each replay saved is also a `speaker` charge saved from its placement's two extra takes
  ([ADR-0422](0422-a-placement-gets-two-extra-takes-and-a-probe-at-its-ceiling-stops.md)).
- A kept pair can hold one take at the CHECK gain and one at the raise. The repeat view and the mark
  repeat spread compare their transfer functions, each deconvolved from its own stimulus gain, as
  ADR-0408 accepts for an A/B pair. The packet's timing line reads the newest take at the mark, which
  banks its own shortfall as before and after. The take that asked the raise keeps that shortfall and
  the raise in its own record. A kept off-axis take keeps its short alignment reading; no decision
  reads it. The page shows no retake for a kept take.
- Every new run's `set_id` moves once: the set basis drops `stimulus_dbfs`, `stimulus_id` and
  `stimulus_peak_dbfs` and gains `stimulus_shape_id`. Per-set views, their evidence bases and the
  packet's sets follow. Take records, program `stimulus_id`s and request fingerprints do not move. A
  round banked before this change keeps the sets its manifest stored, but they name no stimulus shape,
  so `speaker_fit` refuses its takes (no backward support).
- Takes that differ only in level now share a set. The near-field takes of one woofer at 15 mm and at
  30 mm, each levelled at its own place, are one set per woofer. A refused level retake joins the set
  of the take that replaced it. A level probe keeps a set of its own: its shape differs, and its basis
  names `level_probe`.
- Rejected:
  - Keeping a short last take at the mark whose raise could lift it to its floor. ADR-0345 would ask
    for a new round, which costs more than one replay.
  - Holding an off-axis take to the mark's rule, a later take at its own pose. Its timing feeds no
    decision, so its replay buys nothing.
  - Letting a mark take's raise ride a later take off the mark. On `baseline_express` a short fourth
    mark take would then be the newest take at the mark, with the same ADR-0345 result.
  - Finding the drift reference by shape too. A one-driver raise moves the broadband reference by a
    share that no take states.
