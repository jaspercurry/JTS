# Measurement-loop doctrine

> **Status: canonical doctrine, ratified by the owner 2026-08-21.** Governs
> how an LLM drives the crossover-v2 / correction measurement loop — what it
> may try, what stops it, and who decides. States the ruling once; other
> docs point here rather than restating it.

The owner's goal: an LLM that can recommend its own experiments, measure
them, and grade them against what came back — the way a scientist works, not
a call-center script reading rules off a card. Rules exist only where a
mistake would damage hardware. Everything else is the LLM's and the
household's judgment call, made on data.

## 1. The loop

**measure → analyze → recommend → loop → save.** One vocabulary, in words
anyone would understand: the engine's verbs *are* the loop's language.

- **measure** — a round runs and banks its evidence. Baseline, re-measure and
  candidate-check are the same verb with different arguments, not three
  things. A candidate that is cheap, safe, and reversible is measured without
  ceremony, and every mic movement gathers the maximum information it can
  support, not the minimum that answers one question.
- **analyze** — the evidence is read: what the capture says, and what this
  session did not separate.
- **recommend** — pre-registered expectations are stated and the next thing
  to try is named. Proposing, prescribing and recommending are one act; a
  final call is that same act with more information behind it.
- **loop** — checking that a recommendation held is measuring again. A worse
  round is a gradient sample, not a stop (§3).
- **save** — the result is banked.

## 2. The authority model

- **The LLM recommends; the measurement decides.** Priors, confidence scores
  and rankings are advisory: they never veto an in-band experiment, and
  keep/rollback is decided by citing a measured delta, not a forecast.
- **The owner rules** on taste and on which risk to accept.
- **Hard stops exist only for a known component-damage mechanism** — never
  for "this probably won't work" or "we haven't tried this before."
- **An unproven or stale fact discloses loudly and never stops the work.**
  Outside §4's clamps, staleness and unproven-ness are a doctor line, an
  `event=`, a UI hint — never a stop: last-known-good keeps playing and the
  session keeps measuring. Hardware this project has commissioned once ships
  as known-good, so a household that owns it does not re-commission it. What
  may still refuse is a **claim** — a fact that cannot be proven is not
  banked — and refusing to claim is a different act from refusing to work.
  The repo-wide form of this rule lives in the governance charter
  ([AGENTS.md](../AGENTS.md)).

## 3. The guiding principle — least-bad measured, honed in bites

Owner-ratified 2026-08-14 as the commissioning program's ethos, extended
2026-08-16, and re-affirmed 2026-08-22: *"least bad is still the overall
guiding principle"* (#2865). Migrated here from
[`audio-commissioning-roadmap.md`](historical/audio-commissioning-roadmap.md), which is
historical and keeps its original text as archaeology; this section is the
live home, and it is the file production code cites.

**Tinker-first, never-nanny.** A partially-working speaker beats one aired out
by an error. The system always adopts the best configuration available given
current evidence. Imperfect-but-best-known is the bar — a defect that degrades
a claim does not withhold the tune that claim describes.

**Restore and rollback are reserved for measured regression.** Every defect
outside the closed hard-stop list (§4) **discloses and recommends a next
action**. It never blocks. A gate that refuses on suspicion rather than on a
measured regression is a bug against this principle.

**Least-bad measured, honed in bites.** The target of an intervention cycle is
the least bad **measured** configuration, not a match to the prediction — a
realized result will likely never perfectly match what the model commanded. So
a series gets a few bites at that apple: up to three rounds to hone, which the
owner may extend to four (#2602 owns the cap).

Realized-versus-predicted mismatch is a **learning signal**, never by itself a
reason to retreat. The bites exist to separate what is in our control to fix —
model and accounting defects, which get fixed — from what is not — driver and
room physics, which get commanded for the achievable instead — and then to land
somewhere better than we started.

Two decisions the machinery must not conflate. **What plays** is always the
least-bad measured configuration, so a round that measures worse than the
previous state restores that state: that restore is this principle working, not
a violation of it. **Whether the series learns and continues** has one answer
every time — it does. A worse round is a gradient sample, not a stop. Every
round, kept or restored or refused, banks its measurement into the series state
so the next bite is commanded from it. Only the round budget, the plateau, and
the safety class end a series; rollback ends one only for the safety class or
for genuine corruption — an unmeasured or integrity-lost state the model cannot
reason about. **The safety class is §4's closed list**, which is stated there
once and is the enumeration to read: the ethos that ruled this named the class
as driver protection, hearing safety, and the clipping/volume ceiling, and that
naming is its own gloss, not a second list to hold §4 to.

So the rule a verdict class is held to: measured no-worse than the previous
state → keep, bank, continue; measured worse → restore the playing
configuration, bank, continue. A class that retreats from a measured-acceptable
state on realized ≠ commanded alone is a bug against this principle.

**Probabilistic posture, 80/20 execution.** Per-frequency graded evidence —
support counts, confidence margins, tapered authority — beats a binary
per-session verdict. The implementation is **deterministic decision tables
fed by graded evidence**, extending the envelope min-composition pattern the
code already uses. No Bayesian machinery, no inference engine.

**Investment split.** The measurement substrate gets foundation-grade
investment, because wrong measurements poison every layer downstream of
them. Intervention layers get the 80/20 lens. When those two pull against
each other, the substrate wins.

**Selection is intervention-granular, not only candidate-granular.** Owner
refinement, 2026-08-22 (#2862): *"if one intervention was really good but
another was bad but the entire candidate was worse, there is still a least bad
part that we may want to use in a future config."* A candidate that loses as a
unit can still carry an intervention whose measured evidence stands, so a record
must keep intervention identity distinct from candidate identity — otherwise a
measured-good intervention dies with its carrier and its evidence is re-earned
from scratch. The per-feature record join (ticket 1.10 in
[`tuning-master-plan.md`](tuning-master-plan.md)) is the substrate; the grading
rules for "this intervention was good inside a losing candidate" are design
work, not settled doctrine.

## 4. The hard-stop enumeration (closed list)

This is the ruling the tree converges to, not a snapshot of its current
refusal surface — known deviations at this doc's date are tracked below,
never silently treated as more hard stops. The only refusals a scientist
accepts from the bench, because each guards a real component against
damage:

- the excitation ledger and the excitation safety plan
  (`jasper/active_speaker/excitation_safety_plan.py`). **The ledger admits a
  program against the DECLARED session volume**, so a stimulus emitted at any
  other fader position was never the one it admitted — which is why the
  per-stimulus fader hold
  (`jasper/active_speaker/volume_latch.py`, `hold_fader_at`) refuses
  `measurement_volume_drift` rather than disclosing. Not a new gate but this
  bullet's own mechanism restated at the moment of emission: a fader above the
  declared volume drives every branch past the declared level-duration limits
  this bullet exists to enforce. **It trips on two conditions, and the second
  is the fail-closed half of the same rule** — the fader read and did not
  agree, OR the fader could not be read at all, because a level that cannot be
  established cannot be shown to be under the caps either. **It proves and
  never writes** (wave 5): `SessionVolumePlan.open` establishes the
  measurement volume once per session, and a per-stimulus repair here was a
  second writer moving the fader behind the session's back. It passes the
  nanny test below because it refuses only what it cannot PROVE, after an
  independent second read — never a level it merely dislikes — and every
  refusal names the reading it took (#2925; found the other way round — an
  overnight campaign measured 8.712 dB QUIET, with the ledger equally wrong in
  the direction that is loud)
- the output limiters — `STARTUP_LIMITER_CLIP_LIMIT_DB` /
  `BASELINE_LIMITER_CLIP_LIMIT_DB` in
  `jasper/active_speaker/camilla_yaml.py` — and the volume /
  `HARD_CEILING_DBFS` rail (`jasper/audio_measurement/ramp.py`)
- declared per-driver excitation bands and level-duration limits —
  `permitted_band` / `level_duration_limits` in `excitation_safety_plan.py`.
  **Declared** is the load-bearing word, and since 2026-08-23 it is enforced
  as such: `max_effective_peak_dbfs` is optional, a value on the record is
  honoured verbatim rather than clamped to a class default, and it bounds the
  per-driver composed level rather than the un-segmented measurement volume
  (row (h)). **A published protection slope is declared in the same sense and
  is inside this bullet** — `TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT`
  (`crossover_v2/topology_prescription.py`) refuses a pinned order below the
  slope the maker published, and it refuses nothing at all when the maker
  published no slope. Said here because the bullet's own vocabulary named
  bands and level-duration limits only, which left the tree's one published-
  slope refusal reading as if it sat outside a closed list it belongs to
- the commissioning level stop — `max_commissioning_level_db_spl`
  (`jasper/active_speaker/profile.py`)
- firmware brick hazards, e.g. the XVF `SAVE_CONFIGURATION` ban
  (`jasper/cli/aec_tune.py`)

**No reason code takes a graph off the speaker, and the registry's "hard
stop" is not this list's.** `REASON_REGISTRY` (`crossover_v2/refusal_copy.py`)
decides what the household is told and whether another attempt can help —
nothing more. Its `TEMPLATE_HARD_STOP` screen and a `retry_budget` of `0` mean
"no extra attempt can help", a statement about the condition, and they end an
attempt or a session, never a graph that is already playing. What removes an
applied graph is the restore path — `commissioning_apply.py`'s
`_restore_failed_mutation_locked` / `restore_pending_candidate_apply` — driven
by the adoption table on a measured delta (§3). Said here because the registry
is the surface a reviewer reaches first, and two things wearing one name in
one file is how it gets read as a list of stops. **A hard stop is a member of
the list above and nothing else** — in the CLAMP / INTEGRITY / DISCLOSURE
taxonomy ([ADR-0101](adr/0101-proven-once-disclose-on-change.md), §4a) those
members are the CLAMPs, and `hard_stop` in the registry is a screen name.

Everything else — geometry blindness, beaming priors, confidence
heuristics, prediction-engine rankings — is **provenance**, not a gate: it
rides with the data into the evidence packet for the prescriber (human or
LLM) to weigh. It must never refuse an experiment on its own.

### 4a. The integrity class — refusing a CLAIM

The clamps are not the only refusals in the round path, and they are not the
largest group. About **100 refusals protect the honesty of the evidence rather
than the speaker**, against §4's **5 clamp mechanisms and their ~112
enforcement points** (refusal census, 2026-08-25; the counts and their
composition are in
[`REFACTOR-TUNING-2026-08.md`](REFACTOR-TUNING-2026-08.md) §1). The class is
named here because a review holding only §4 and §5 re-derives "is this
safety?" from scratch every time, which is how a quality ceiling wore a
refusal's costume as long as it did.

**A dishonest measurement is worse than no measurement.** A refusal whose only
cost is a re-measure, and whose alternative is banking a number that is not
what it claims to be, is not a nanny.

**The test, and it decides the class.** From
[ADR-0002](adr/0002-measure-again-discriminator.md), which extracts the
owner's 2026-08-03 ruling on #2087:

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

1. **Its cost is a re-measure, never a withheld tune.** A refusal here that
   also withholds an already-measured configuration has drifted into the
   adoption table's job, and it is re-argued there.
2. **It fails toward "unknown", never toward "bad".**
   `delta_probe.VERDICT_UNAVAILABLE` is the pattern: "no evidence to refuse
   on, and no permission granted either."
3. **It banks what it refused, wherever a series is running** — §3's "every
   round, kept or restored or refused, banks its measurement into the series
   state." **Verified on the adoption path only**, and two things stand
   against stating it absolutely. A **leveling pass has no series state and
   banks nothing on purpose**: the seat-level ramp's refusal "always carries a
   `reason` from the `REFUSE_*` set and persisted nothing", which is rule 2
   working rather than a violation, and it is pinned that way. And the
   **capture screens do not bank at round granularity**: `SCREEN_SNR_FLOOR`
   puts full numbers in the journal and writes no evidence artifact, and
   nothing pins that either way. The second is a gap disclosed, not licensed —
   a screen that banks is the target.

**The STOP-RELAXER pattern.** When a stop is *mostly* right, narrow it with a
named, tested relaxer rather than deleting it or leaving it broad. Three
production functions exist only to admit a state a stop would otherwise
refuse, and each has live consumers:
`setup_status.setup_blocked_only_by_in_sequence_anchor` (the one admitted
blocked setup shape), `delta_probe.seam_rollback_deferral` (a seam rollback
whose realized deviation points entirely quieter than commanded), and
`revalidation.applied_profile_revalidation_satisfies_driver_target_proof`
(first-time driver-target proof on an applied-profile edit). A census that
greps for `raise` sees none of them, which is why they are named here. The
pattern is **not** how the topology-staleness block was narrowed: §2's
disclosure rule deleted that block outright, and a rotated topology
fingerprint is a warning with a doctor line now, not a blocker.

### Known deviations at 2026-08-21, and where each stands

Five tested refusals sat outside the list above when this doc was ratified,
none naming a component-damage mechanism; a sixth, a seventh, an eighth, and a
ninth were found afterwards and are carried here on the same terms. **Eight are
now closed and one is retained by ruling, so this table tracks nothing
outstanding.** Rows are
struck as they close and a struck row stays, so a reader meeting one of these
names in an old round or an old commit can tell it was retired on purpose:

| # | refusal | file | status |
|---|---|---|---|
| ~~a~~ | ~~`BOOST_VERTICALLY_BLIND`~~ | `jasper/active_speaker/crossover_v2/driver_prescription.py` | **CLOSED** — removed in #2805; a boost admitted on a horizontal capture now owes a measurement, not a plane |
| ~~b~~ | ~~`FC_REJECT_BEAMING` clamps the Fc grid against a prior #1675 rules "guidance, never refuses"~~ | — | **CLOSED 2026-08-21.** The refusal only ever bound the corner hunt's proposal grid, and the hunt was deleted with `fc_sweep`'s sweep half (plan ticket 2.3). No admissibility bound reads the ka onset now; it rides the receipt as provenance, which is what section 4 asked for. |
| ~~c~~ | ~~`REASON_CORRECTION_NOT_AN_IMPROVEMENT`~~ — refused on predicted-vs-predicted, no measurement in the loop | `jasper/active_speaker/crossover_v2/accountability.py`, `jasper/active_speaker/crossover_v2_flow.py` | **CLOSED** — it vetoed jts3's first prescribed-boost round on 2026-08-22 (`improvement_db=-0.703`, one line after disclosing its own inputs 11.635 dB apart); the forecast now banks `LEDGER_NOT_AN_IMPROVEMENT` and the round proceeds |
| ~~d~~ | ~~`_strategy_gates` score floors~~; ~~`measurement_evidence_failure`'s fail-severity apply blocker~~ | `jasper/correction/confidence.py`, `jasper/correction/failures.py` | **CLOSED** — the score floors' only veto (`response._policy_allows`) went in #2808 and every remaining reader was already disclosure; the apply blocker is deleted, and a `fail`-severity finding now reaches the household as a `warn` nudge |
| e | `prescription_route` refuses the boost class outright | `jasper/active_speaker/crossover_v2/blend_prescription.py` | **RETAINED by ruling R8** (`docs/tuning-master-plan.md`): "Blend's `BOOST_ROUTE_UNAVAILABLE` stays for its two recorded reasons (blend is not a headroom term; a summed capture cannot attribute a deficit to a driver)" — a stated limit of the instrument, not a prior about the outcome |
| ~~f~~ | ~~`TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT` refused a pinned order against a code floor while calling it "declared"~~ | `jasper/active_speaker/crossover_v2/topology_prescription.py` | **CLOSED 2026-08-23** — found after ratification. The gate read `required_protection_filters[highpass].minimum_slope_db_per_octave`, which is `max(published, PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)`, so jts3's DE250 — B&C publish "1.6 kHz — 12 dB/oct. or higher" — had a 2400 Hz order-2 pin refused "below the protected driver's declared minimum of 24 dB/octave". The owner ruled: "If it was in the safe overall envelope, it's safe to test." The refusal survives but now compares the PUBLISHED condition only, which makes it a declared-value refusal rather than a class floor and puts it inside section 4 on the same footing as the declared excitation bands; a maker who publishes nothing gets no slope refusal at all, and the 24 dB/octave commissioning figure discloses on the receipt (`recommended_slope_db_per_octave`) beside the design page's existing `tweeter_slope_below_recommended_floor` warning ([#2897](https://github.com/jaspercurry/JTS/pull/2897)) |
| ~~g~~ | ~~the driver door's classification bar~~ — ~~`driver_feature_not_classified`~~, ~~`driver_feature_not_cuttable`~~, ~~`driver_feature_not_boostable`~~, ~~`driver_feature_depth_unavailable`~~, ~~`driver_boost_exceeds_feature_depth`~~, ~~`driver_boost_unvouched`~~ | `jasper/active_speaker/crossover_v2/driver_prescription.py` | **CLOSED 2026-08-23.** Not in the table when it was ratified — a seventh deviation, found by its cost: a role whose incumbent carried a Lowshelf could never keep it, because nothing vouches for a filter the fit engine placed, so naming the role always deleted the shelf ([#2863](https://github.com/jaspercurry/JTS/issues/2863)). The vouch is a prediction about whether a filter will help, so it now discloses `unvouched_filters` on the propose/stage report and refuses nothing. The caps that bound what a filter COSTS are untouched |
| ~~h~~ | ~~`level_duration_limits.max_effective_peak_dbfs` bounded the un-segmented measurement VOLUME~~, and ~~`max_effective_peak_above_code_policy`~~ refused a declared level against a class default | `jasper/active_speaker/session_volume_plan.py`, `jasper/active_speaker/driver_safety.py` | **CLOSED 2026-08-23** — found after ratification, an eighth deviation, by its cost. `jasper-seat-level --target-db-spl 75` refused `spl_target_unreachable` at 68.3 dB SPL on the new-horn box: the ceiling was `min(driver caps) − stimulus peak`, and the binding cap was the tweeter's, derived from a woofer figure the research ask itself had told the assistant to send. The ask's own words for that object are "measurement-protocol discipline, not datasheet facts", so no term in the chain named a damage mechanism, and ~30 dB of digital headroom sat unused. The owner ruled: "It's a fixed-gain amp — we do ALL volume via software... No limit unless we have no headroom to give." The un-segmented ceiling is now full scale less the binding branch peak and DISCLOSES each declared cap it drives past on `event=active_speaker.unsegmented_ceiling_bound`; the save-time refusal is deleted and the field is optional. The per-driver cap keeps its other job — setting and admitting each driver's composed segment level, where it is level-MATCHING rather than a ceiling — so it stays inside section 4 above on the declared-value footing |
| ~~i~~ | ~~`REASON_DRIVER_LEVELS_DISAGREE`~~ — the realized-level gate refused a round on a QUALITY measure | `jasper/active_speaker/crossover_v2/accountability.py`, `jasper/active_speaker/crossover_v2/refusal_copy.py`, `jasper/audio_measurement/program_analysis.py` | **CLOSED 2026-08-24** — found after ratification, a ninth deviation, by its cost. It graded inter-driver tonal balance and named no damage mechanism, so section 4 never covered it. Its located cost was a **guaranteed refusal**: the number it grades IS the MEASURE ripple polish's trim excursion (`difference_db == polish_delta_db[tweeter] − polish_delta_db[woofer]` — the give-back is a per-role constant, so each role's excursion passes straight through and the pair's error is their difference; the woofer term is 0.0 in the shipped program, which polishes only the tweeter trim, so it reduces to the tweeter's, and that reduced form verified 8-for-8 against the banked campaign), and the polish was ADMITTED out to `RIPPLE_TRIM_SANITY_MARGIN_DB` (6.0 dB) while this gate REFUSED past `REALIZED_LEVEL_MATCH_TOLERANCE_DB` (3.0). Every polish landing in that 3.0-6.0 dB dead band produced a round the session was then certain to refuse, on two thresholds neither of which had measured anything; jts3's polish landed at 3.9 dB and was refused, where the campaign it was compared against had lived at 1.5-1.9. Both halves are fixed together: the polish admission is now COUPLED to this gate's tolerance, so the dead band closes by construction and a rejected polish falls back to the band-average trim with the excursion disclosed; and the gate itself banks `event=…_level_match_finding` plus a level-frame finding carrying `polish_delta_db_*`, and the round proceeds. Section 3's other half is met in the same change: the finding carries the recommendation the deleted refusal made — re-check each range's entered sensitivity and any resistor pad, then measure again — and keeps that refusal's tense ("would not end up level"), since the levels are read off the fit's MODEL of the emission and not off a capture of an apply that has not happened. What went is the stop, not the next action. The absolute rails that bound loudness are untouched and are elsewhere — non-positive trim clamps, the output limiters, `devices.volume_limit`, and the commissioning SPL stop |

## 5. The nanny test

Before a review adds a new refusal, ask: **does this block a reversible
experiment a scientist would run, on the theory it might not work?** If
yes, it ships as an informational flag, never a gate. A refusal earns its
place only by naming the component-damage mechanism it guards against —
"seems risky" is not a mechanism.

## 6. Pointers

- Round runner (the loop's home; measure and apply are two separate,
  fingerprint-named steps): `scripts/run-crossover-round.py`.
- Prescription doors (alignment, topology, blend, driver) —
  `jasper-crossover-prescriber` (`packet` / `propose` / `stage`; its fourth
  verb `status` orients rather than prescribes) plus the
  session-open request body; cataloged in
  [`testing-tooling.md`](testing-tooling.md#crossover-prescriber-harness).
- Evidence packet — one document per round a reader (human or LLM) can
  answer from: `jasper/active_speaker/crossover_v2/evidence_packet.py`.
- More than one capture per mic position, so one mic movement answers more
  questions (§1, `measure`) — `--per-position N` on the round runner above, plus the
  derived `position_cycle.json` that says which pose each take was measured
  at. Multiple DSP *configs* per position has a door but no wiring:
  `POST /crossover/v2/republish` makes a banked candidate the live one by its
  own fingerprint, so republish-then-apply reaches a named prior config
  between takes. The open part is sequencing — holding a pose's next capture
  until the apply has landed — which is a design to write rather than a
  refusal to remove. The `awaiting_apply` hold is explicitly not the seam for
  it (its own vocabulary says "no new design may depend on it").

---

Scope of the 2026-08-22 verification: the deviation table above was re-derived
against the tree — every row's named symbol grepped for a live producer — and
rows (a), (c), and (d) closed; row (b) closed separately the same day (#2853)
and its account is that PR's. The other sections were re-read and stand
unchanged; the nanny test and the pointers are numbered one higher since,
unmoved.

Migration (2026-08-23, #2865): section 3 arrived from
[`audio-commissioning-roadmap.md`](historical/audio-commissioning-roadmap.md)'s Ethos —
its ruling text re-read from that file, and the owner's 2026-08-22
re-affirmation and intervention-granularity refinement quoted from #2865 and
#2862, at writing time.

Row (f) was added and closed on 2026-08-23 (#2897), and only that row: the
rest of the table and the sections around it were not re-derived that day.

Row (g) was added and closed the same day, also on its own: every one of its
six slugs was grepped for a live producer and none remains. Rows (a)-(e) were
not re-derived that day either; their account is the 2026-08-22 pass above.
Sections 1-6 were re-read and stand unchanged. So the date below records rows
(f) and (g) and nothing else.

Row (h) was added and closed the same day, on its own again, and the section 4
bullet it qualifies was re-derived with it: `max_effective_peak_above_code_policy`
was grepped for a live producer and none remains, and
`unsegmented_stimulus_ceiling_db`'s one production caller
(`jasper/cli/seat_level.py`) was read against the new derivation. No other row
and no other bullet was re-derived that day.

Row (i) was added and closed on 2026-08-24, on its own, and its fix round
re-derived three more things the same day: `M7`'s `fix_classes` declaration in
`jasper/attribution/mechanisms.py` (both `eq` and `refit` are declared, which is
what lets the realized-only case route the first), the deleted refusal sentence
read back off `origin/main`'s `refusal_copy.py` so the recommendation could be
carried rather than paraphrased, and `findings.py`'s hardware-noun list, which
is why that sentence is reworded rather than pasted. What was re-derived when
the row was written: `REASON_DRIVER_LEVELS_DISAGREE` was grepped for a live
producer and none remains (its registry row is deleted with it), the
`difference_db == polish_delta_db[tweeter] − polish_delta_db[woofer]` identity
was re-read at the two sites that produce each side (`intervention.plan_linearization`'s anchor block
and `program_analysis._build_candidate`'s polish), and the two constants named
in the row were read at their definitions. Section 3's disclose-and-recommend
rule and section 5's nanny test were re-read as the authority for the
demotion; section 4's list was re-read to confirm it never carried this gate,
and no bullet in it changed. No other row was re-derived that day.

Last verified: 2026-08-24
