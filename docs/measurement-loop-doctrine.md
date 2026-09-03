# Measurement-loop doctrine

> **Status: canonical doctrine, ratified by the owner 2026-08-21.** Governs
> how an LLM drives the crossover-v2 / correction measurement loop — what it
> may try, what stops it, and who decides. States the ruling once; other docs
> point here rather than restating it.

The owner's goal: an LLM that recommends its own experiments, measures them,
and grades them against what came back — the way a scientist works, not a
call-center script reading rules off a card. Rules exist only where a mistake
would damage hardware; everything else is the LLM's and the household's
judgment call, made on data. Operational pointers — the round runner, the
prescription doors, the evidence packet, the per-position and republish
controls — live in
[`tuning-operator-runbook.md`](tuning-operator-runbook.md), not here.

## 1. The loop

**measure → analyze → recommend → loop → save.** This is the control loop's
vocabulary, not a list of methods on `TuningSession`. The engine owns one
tuning operation, `measure`, inside its `open` / `close` lifetime. The
doors-and-banks tools analyze the banked evidence, recommend the next action,
and persist their own accounting under
[ADR-0198](adr/0198-the-unwired-engine-verb-half-is-deleted.md).

- **measure** — a round runs and banks its evidence. Baseline, re-measure and
  candidate-check are the same verb with different arguments, not three things.
  A candidate that is cheap, safe, and reversible is measured without ceremony,
  and every mic movement gathers the maximum information it can support, not
  the minimum that answers one question.
  Tool: `jasper-round open` / `wait`.
- **analyze** — the evidence is read: what the capture says, and what this
  session did not separate.
  Tool: `jasper-crossover-prescriber packet`.
- **recommend** — pre-registered expectations are stated and the next thing to
  try is named. Proposing, prescribing and recommending are one act; a final
  call is that same act with more information behind it.
  Tool: `jasper-crossover-prescriber propose`.
- **loop** — checking that a recommendation held is measuring again (§3).
  Tool: `jasper-round-views frozen`, the re-measure graded against the round it
  is checking.
- **save** — the result is banked.
  Tool: `jasper-round-bank`.

### 1a. The layering rule — what a measurement plays through

**Owner ruling, 2026-09-01.** When tuning layer N, the measurement plays
through layer N and everything *below* it, and never anything above it.

- Preference EQ (`/sound/`) sits above everything tunable, so it is part of no
  measurement graph and of no tuning comparison. Room correction follows the
  same rule: absent while drivers are being linearized, and holding
  linearization fixed beneath it when it is itself the layer under tune.
- A **comparability** question inherits the same scope. The fingerprint a
  round banks at entry, and compares against later, covers the candidate and
  below — structure, linearization, blend, trim, headroom, limiters — and
  deliberately excludes the preference slots
  (`crossover_v2.tuning_scope`, #3489). A whole-graph hash would report a
  boundary on every household EQ save, which is noise that trains a driver to
  ignore the flag.

**Known departure, open at writing time.** The rule above is the ruling; the
code does not yet meet it everywhere. Routed stimuli satisfy it by construction
— `measurement_emit.emit_measurement_graph` takes no `SoundProfile`, pinned by
`test_the_measurement_graph_never_carries_preference_eq`. The
`programs.SUMMED_SWEEP_PHASES` captures do not: they deliberately measure the
STANDING production graph, which carries the household's preference EQ. That
set is VERIFY, both position clouds, **and ENTRY_BASELINE** — so it includes
the very pair `evaluate_benefit` differences, and an EQ save between a round's
"before" and its "after" moves them apart for a reason no verdict attributes.
Harmless on the runs banked so far only because jts3's sound profile was
`enabled=false` with 0 bands throughout both campaigns, which is a property of
those runs and not a guarantee. Closing it is measurement-path work and has no
issue of its own yet.

## 2. The authority model

- **The LLM recommends; the measurement decides.** Priors, confidence scores
  and rankings are advisory: they never veto an in-band experiment, and
  keep/rollback is decided by citing a measured delta, not a forecast.
- **The owner rules** on taste and on which risk to accept.
- **Hard stops exist only for a known component-damage or hearing-safety
  mechanism** — never for "this probably won't work" or "we haven't tried this
  before."
- **An unproven or stale fact discloses loudly and never stops the work.**
  Outside §4's clamps, staleness and unproven-ness are a doctor line, an
  `event=`, a UI hint — never a stop: last-known-good keeps playing and the
  session keeps measuring. Hardware this project has commissioned once ships as
  known-good, so a household that owns it does not re-commission it. What may
  still refuse is a **claim** — a fact that cannot be proven is not banked —
  and refusing to claim is a different act from refusing to work. The repo-wide
  form lives in the governance charter ([AGENTS.md](../AGENTS.md)).
- **Evidence that decided nothing is still evidence, and is reported as such.**
  `jasper-round-views cloud-binding` re-fits a banked round's linearization
  with the position cloud's null evidence cut, so whether that evidence
  actually BOUND the fitted prescription — and in which bands — is a number a
  round can be asked for rather than a thing assumed either way. Observed
  only; it moves no grade, and it says nothing about whether the wired answer
  was right.

## 3. The guiding principle — least-bad measured, honed in bites

Owner-ratified 2026-08-14 as the commissioning program's ethos, extended
2026-08-16, and re-affirmed 2026-08-22: *"least bad is still the overall guiding
principle"* (#2865). This is the live home and
the file production code cites;
[`audio-commissioning-roadmap.md`](historical/audio-commissioning-roadmap.md)
is historical and keeps its original text as archaeology.

**Tinker-first, never-nanny.** A partially-working speaker beats one aired out
by an error. The system always adopts the best configuration available given
current evidence: imperfect-but-best-known is the bar, and a defect that
degrades a claim does not withhold the tune that claim describes.

**Restore and rollback are reserved for measured regression.** Every defect
outside the closed hard-stop list (§4) **discloses and recommends a next
action**. It never blocks. A gate that refuses on suspicion rather than on a
measured regression is a bug against this principle.

**Least-bad measured, honed in bites.** The target of an intervention cycle is
the least bad **measured** configuration, not a match to the prediction — a
realized result will likely never perfectly match what the model commanded. So
a series gets up to three rounds to hone, which the owner may extend to four
(#2602 owns the cap). Realized-versus-predicted mismatch is a **learning
signal**, never by itself a reason to retreat: the bites separate what is in
our control to fix — model and accounting defects, which get fixed — from what
is not — driver and room physics, which get commanded for the achievable
instead — and then to land somewhere better than we started.

Two decisions the machinery must not conflate. **What plays** is always the
least-bad measured configuration, so a round that measures worse than the
previous state restores that state: that restore is this principle working, not
a violation of it. So the rule a verdict class is held to: measured no-worse
than the previous state → keep, bank, continue; measured worse → restore the
playing configuration, bank, continue. A class that retreats from a
measured-acceptable state on realized ≠ commanded alone is a bug against this
principle. **Whether the series learns and continues** has one
answer every time — it does. A worse round is a gradient sample, not a stop.
Every round, kept or restored or refused, banks its measurement into the series
state so the next bite is commanded from it. Only the round budget, the
plateau, and the safety class end a series; rollback ends one only for the
safety class or for genuine corruption — an unmeasured or integrity-lost state
the model cannot reason about. **The safety class is §4's closed list**, stated
there once.

**Probabilistic posture, 80/20 execution.** Per-frequency graded evidence —
support counts, confidence margins, tapered authority — beats a binary
per-session verdict. The implementation is **deterministic decision tables fed
by graded evidence**, extending the envelope min-composition pattern the code
already uses. No Bayesian machinery, no inference engine.

**Investment split.** The measurement substrate gets foundation-grade
investment, because wrong measurements poison every layer downstream of them.
Intervention layers get the 80/20 lens. When those two pull against each other,
the substrate wins.

**Selection is intervention-granular, not only candidate-granular.** Owner
refinement, 2026-08-22 (#2862): *"if one intervention was really good but
another was bad but the entire candidate was worse, there is still a least bad
part that we may want to use in a future config."* A candidate that loses as a
unit can still carry an intervention whose measured evidence stands, so a
record must keep intervention identity distinct from candidate identity —
otherwise a measured-good intervention dies with its carrier and its evidence
is re-earned from scratch. The per-feature record join (ticket 1.10 in
[`tuning-master-plan.md`](tuning-master-plan.md)) is the substrate; the grading
rules for "this intervention was good inside a losing candidate" are design
work, not settled doctrine.

## 4. The hard-stop enumeration (closed list)

**Five mechanisms, and this list is positively complete.** A refusal that is
not one of the five — or one of the enforcement families named under it — is
not a hard stop, whatever its name or its household screen says. The five
expand to roughly **112 enforcement points** across the round path, because
"the excitation ledger", "the output limiters" and "declared per-driver bands"
are each families rather than single `raise` sites; that is why the list is
short and the surface is not (refusal census, 2026-08-25; the 112 are never
enumerated — the five below are the composition, at family granularity). Each
guards a real component — or the household's hearing — against damage, and
nothing else on the bench may hard-stop.

1. **The excitation ledger and the excitation safety plan**
   (`jasper/active_speaker/excitation_safety_plan.py`). The ledger admits a
   program **against the DECLARED session volume**, so a stimulus emitted at any
   other fader position was never the one it admitted. Carried by the ledger's
   own excitation-identity and program-admission refusals; by the session volume
   plan (`session_volume_plan.py`); and at the moment of emission by the
   per-stimulus fader hold (`volume_latch.hold_fader_at`), which **refuses
   `measurement_volume_drift` rather than disclosing** because a fader above the
   declared volume drives every branch past the declared level-duration limits
   this bullet exists to enforce. **The fail-closed half is the same rule, not a
   second one**: the hold trips when the fader read and did not agree, and
   equally when it could not be read at all, because a level that cannot be
   established cannot be shown to be under the caps. It **proves and never
   writes** — `SessionVolumePlan.open` sets the measurement volume once per
   session, and a per-stimulus repair would be a second writer moving the fader
   behind the session's back. It passes §5 because it refuses only what it
   cannot PROVE, after an independent second read, and every refusal names the
   reading it took (#2925).
2. **The output limiters and the volume rail** —
   `STARTUP_LIMITER_CLIP_LIMIT_DB` / `BASELINE_LIMITER_CLIP_LIMIT_DB`
   (`jasper/active_speaker/camilla_yaml.py`) and `HARD_CEILING_DBFS`
   (`jasper/audio_measurement/ramp.py`). Carried by the CamillaDSP emit gates and
   the field invariants guarding them — including the config-side
   `devices.volume_limit = 0.0` and the non-positive trim invariants, every one
   refusing *before* the YAML leaves the emitter — by the same contract re-asked
   at load and deploy (`runtime_contract.classify_camilla_graph`,
   `safe_graph_for_current_topology`, `flat_program_graph_blocked_reason`, whose
   ~67 codes the census tallies separately so one module cannot dominate the
   ratio — the ~112 above excludes them), by the path-safety tweeter-floor arms,
   and by the `SAFETY_*` verification findings that send a clipped or
   uncommanded-louder result to the restore path as `SafetyStatus.UNSAFE`.
3. **Declared per-driver excitation bands and level-duration limits** —
   `permitted_band` / `level_duration_limits` in `excitation_safety_plan.py`.
   **Declared** is the load-bearing word: `max_effective_peak_dbfs` is optional,
   a value on the record is honoured verbatim rather than clamped to a class
   default, and it bounds the per-driver composed level rather than the
   un-segmented measurement volume. **A published protection slope is declared
   in the same sense** — `TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT`
   (`crossover_v2/topology_prescription.py`) refuses a pinned order below the
   slope the maker published, and refuses nothing at all when the maker
   published no slope. Also carried by the topology fc edges and
   `FILTER_OUTSIDE_PASSBAND` (`crossover_v2/driver_prescription.py`), and by
   `driver_safety.validate_research_low_limit_plausibility`, because an
   implausible protection corner is a tweeter-damage mechanism.
4. **The commissioning level stop** — `max_commissioning_level_db_spl`
   (`jasper/active_speaker/profile.py`). Carried by the seat-level SPL / clip /
   ceiling refusals, checked on *every* sample before the median
   (`cli/seat_level.py`, `seat_level_ramp.py`), and by the stage-5 ramp and
   audible-policy blockers (`commission_ramp.py`, `audible_policy.py`). The
   clamps that **cannot be proven to hold** sit here on the same footing as the
   fader hold above and refuse for that reason alone:
   `mic_calibration_unavailable` (`audio_measurement/calibration.py` —
   "absolute SPL is never guessed"), `driver_cap_ceiling_underivable`, and the
   driver-floor confirmation pair.
5. **Firmware brick hazards** — e.g. the XVF `SAVE_CONFIGURATION` ban
   (`tests/test_xvf_host.py`'s `_FORBIDDEN_COMMANDS`).

**One retention that is kept by ruling rather than by mechanism.** Blend's
`BOOST_ROUTE_UNAVAILABLE` (`crossover_v2/blend_prescription.py`) refuses the
boost class outright, and ruling R8 in
[`tuning-master-plan.md`](tuning-master-plan.md) keeps it: *"Blend's
`BOOST_ROUTE_UNAVAILABLE` stays for its two recorded reasons (blend is not a
headroom term; a summed capture cannot attribute a deficit to a driver)."* That
is a stated limit of the instrument rather than a prior about the outcome — the
blend stage is emitted under `camilla_yaml.MAX_BLEND_CORRECTION_GAIN_DB = 0.0`,
so it has no headroom to spend — and boosts go through the driver door instead.

**No reason code takes a graph off the speaker, and the registry's "hard stop"
is not this list's.** `REASON_REGISTRY` (`crossover_v2/refusal_copy.py`) decides
what the household is told and whether another attempt can help — nothing more.
Its `TEMPLATE_HARD_STOP` screen and a `retry_budget` of `0` mean "no extra
attempt can help", a statement about the condition, and they end an attempt or a
session, never a graph that is already playing. What removes an applied graph is
`commissioning_apply.py`'s restore path — a failed mutation goes back to the
exact predecessor graph, driven by the adoption table on a measured delta (§3);
a candidate apply still pending when the box restarts is disclosed by the
commissioning status as `restore_required` and restored by the operator, not
automatically (ADR-0230). Said here because
the registry is the surface a reviewer reaches first, and two things wearing one
name in one file is how it gets read as a list of stops. **A hard stop is a
member of the list above and nothing else** — in the CLAMP / INTEGRITY /
DISCLOSURE taxonomy
([ADR-0002](adr/0002-measure-again-discriminator.md); the INTEGRITY class is §4a
below) those members are the CLAMPs, and `hard_stop` in the registry is a screen
name.

Everything else — geometry blindness, beaming priors, confidence heuristics,
prediction-engine rankings — is **provenance**, not a gate: it rides with the
data into the evidence packet for the prescriber (human or LLM) to weigh. It
must never refuse an experiment on its own.

**What `deviation (a)`…`(i)` means, where a comment still says it.** This
section used to carry a nine-row table of refusals that sat outside the list
while they were burned down. Eight closed; the ninth, row (e), is the retention
above. **Closed did not always mean demoted** — rows (f) and (h) closed by being
ADMITTED into the list above, and both still refuse. The table is deleted
because a positively complete list is what it was compensating for; its
rationale and its history are in git. What survives is the map, so a citation
resolves without it:

- **(a)** `BOOST_VERTICALLY_BLIND` — closed (#2805)
- **(b)** `FC_REJECT_BEAMING` — closed (#2853)
- **(c)** `REASON_CORRECTION_NOT_AN_IMPROVEMENT` — closed (2026-08-22)
- **(d)** `_strategy_gates` score floors, `measurement_evidence_failure`'s apply
  blocker — closed (2026-08-22)
- **(e)** `BOOST_ROUTE_UNAVAILABLE` — **retained**, ruling R8; the sentence above
- **(f)** `TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT` — closed by admission into
  §4 (#2897); **still refuses**
- **(g)** the driver door's classification bar —
  `driver_feature_not_classified`, `driver_feature_not_cuttable`,
  `driver_feature_not_boostable`, `driver_feature_depth_unavailable`,
  `driver_boost_exceeds_feature_depth`, `driver_boost_unvouched` — closed (#2863)
- **(h)** `max_effective_peak_dbfs`'s un-segmented ceiling,
  `max_effective_peak_above_code_policy` — closed by admission into §4
  (2026-08-23); the **per-driver cap still refuses**
- **(i)** `REASON_DRIVER_LEVELS_DISAGREE` — closed (#2937)

### 4a. The integrity class — refusing a CLAIM

The clamps are not the only refusals in the round path, and they are not the
largest group. About **100 refusals protect the honesty of the evidence rather
than the speaker**, against §4's **5 clamp mechanisms and their ~112 enforcement
points** (refusal census, 2026-08-25). The class is named here because a review
holding only §4 and §5 re-derives "is this safety?"
from scratch every time, which is how a quality ceiling wore a refusal's costume
as long as it did.

**A dishonest measurement is worse than no measurement.** A refusal whose only
cost is a re-measure, and whose alternative is banking a number that is not what
it claims to be, is not a nanny.

**The test, and it decides the class.** From
[ADR-0002](adr/0002-measure-again-discriminator.md), which extracts the owner's
2026-08-03 ruling on #2087:

> Before a check may stop a session, ask whether measuring again would
> plausibly fix what it saw. Yes → it is a defect in the capture; refuse, and
> still bank. No → it describes the room, the rig, or the result; disclose and
> recommend, never block.

**Six sub-kinds**, so a reviewer can place a new check without re-arguing the
class: **provenance binding** (the thing measured is the thing named) ·
**capture validity** (the capture is of the signal it claims) · **instrument
reachability** (a number exists at all) · **position and ordering** (evidence
lands on the slot it was taken for) · **restore anchoring** (a restore knows
what it restores to) · **liveness** (the round ends rather than hanging).

**Three rules the class carries.**

1. **Its cost is a re-measure, never a withheld tune.** A refusal here that also
   withholds an already-measured configuration has drifted into the adoption
   table's job, and it is re-argued there.
2. **It fails toward "unknown", never toward "bad".**
   `delta_probe.VERDICT_UNAVAILABLE` is the pattern: "no evidence to refuse on,
   and no permission granted either."
3. **It banks what it refused, wherever a series is running** — §3's "every
   round, kept or restored or refused, banks its measurement into the series
   state." **Verified on the adoption path only**, and two things stand against
   stating it absolutely. A **leveling pass has no series state and banks
   nothing on purpose**: the seat-level ramp's refusal "always carries a
   `reason` from the `REFUSE_*` set and persisted nothing", which is rule 2
   working rather than a violation, and it is pinned that way. And the **capture
   screens do not bank at round granularity**: `SCREEN_SNR_FLOOR` puts full
   numbers in the journal and writes no evidence artifact, and nothing pins that
   either way. The second is a gap disclosed, not licensed — a screen that banks
   is the target.

**The STOP-RELAXER pattern.** When a stop is *mostly* right, narrow it with a
named, tested relaxer rather than deleting it or leaving it broad. Three
production functions exist only to admit a state a stop would otherwise refuse,
and each has live consumers:
`setup_status.setup_blocked_only_by_in_sequence_anchor` (the one admitted
blocked setup shape), `delta_probe.seam_rollback_deferral` (a seam rollback
whose realized deviation points entirely quieter than commanded), and
`revalidation.applied_profile_revalidation_satisfies_driver_target_proof`
(first-time driver-target proof on an applied-profile edit). A census that greps
for `raise` sees none of them, which is why they are named here. The pattern is
**not** how the topology-staleness block was narrowed: §2's disclosure rule
deleted that block outright, and a rotated topology fingerprint is a warning
with a doctor line now, not a blocker.

## 5. The nanny test

Before a review adds a new refusal, ask: **does this block a reversible
experiment a scientist would run, on the theory it might not work?** If yes, it
ships as an informational flag, never a gate. A refusal earns its place only by
naming the mechanism it guards against — component damage, or the household's
hearing. "Seems risky" is not a mechanism.

---

Migration (2026-08-23, #2865): section 3 arrived from
[`audio-commissioning-roadmap.md`](historical/audio-commissioning-roadmap.md)'s
Ethos — its ruling text re-read from that file, and the owner's 2026-08-22
re-affirmation and intervention-granularity refinement quoted from #2865 and
#2862, at writing time.

Last verified: 2026-08-26 — §4's five clamps and §4a's named symbols were each
grepped for a live producer at that day's `main`; §§1-3 and 5 were re-read and
stand.
