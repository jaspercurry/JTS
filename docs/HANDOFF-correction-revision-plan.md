# Correction & tuning — program roadmap

> **Two documents in one file, deliberately.** Everything down to
> "[Appendix — the 2026-07-12 layered-pipeline plan](#appendix--the-2026-07-12-layered-pipeline-plan-historical)"
> is the **live spine** of the correction/measurement program: charter, phase
> ladder, campaign rounds, research index, issue index, current position. The
> appendix below it is the completed 2026-07-12 P1–P7 / H0–H4 campaign, kept as
> archaeology and
> **still tagged historical**. Read the spine for "where are we and what is
> next"; read the appendix for "why is the pipeline layered like this."

## The machine — the program charter (2026-07-31)

Build **the machine**: ingest cloud measurements → **diagnose** → **prescribe**
→ **re-measure** → **re-prescribe**. Abstracted beyond this hardware — the rig
on the bench is one instance, not the target. Do not fit the machine to
defective-speaker data. Within-session consistency is the axiom; cross-session
anomalies get flagged, not explained away.

Verbatim source: the owner's charter as recorded at the head of
`captures/first-principles-panel-20260731/SYNTHESIS.md`, which also holds the
panel's joint verdict, the corrected headline, and the ladder below. Like every
`captures/` path in this document it is **gitignored and laptop-durable** — not
linkable from the repo, and not a substitute for what belongs in-repo.

## Read the three "P" namespaces before you read anything else

The program grew three independent `P<N>` vocabularies and they collide. This
has already cost readers; disambiguate before citing.

| Written as | Means | Owned by |
|---|---|---|
| **P0–P4 (rung)** | the 2026-07-31 first-principles **ladder** — the phase spine below | this document |
| **P1–P7 (probe)** | attribution **probe primitives** (P1 reverse-null, P2 position-variance, P3 two-level linearity, P4 rotation, P5 design-axis, P6 Farina, P7 repeat-variance) | [`attribution-stage-plan.md`](attribution-stage-plan.md) §5 |
| **P1–P7 / H0–H4 (track)** | the completed 2026-07-12 **layered-pipeline campaign** | the appendix below |

Cite rungs as "rung P3", probes as "probe P4", the old campaign as "P-track P5".
Bare `P3` is ambiguous everywhere in this program.

Likewise **WO-0…WO-8** is the attribution **work-order ladder** and is owned by
[`attribution-stage-plan.md`](attribution-stage-plan.md) §7 — not by this file.
This spine records how the rungs *reshape* that ladder's sequencing; it never
restates a WO's definition.

## The phase spine — the P0–P4 ladder

The ladder is the program's phase structure: five ranked **course corrections**
that the 2026-07-31 three-lens panel produced independently and converged on.
It is not a complete work taxonomy — it reshapes the order of work that already
had a home (chiefly WO-4/WO-6/WO-7), and program work that no rung touches is
listed honestly under [Not on a rung](#not-on-a-rung--program-work-with-another-owner).

**Work orders the ladder does NOT touch**, stated so their absence below is
read as a finding rather than an omission: **WO-0** (reported 2026-07-29),
**WO-1** (shipped — `jasper/attribution/` holds the findings artifact, registry,
vocabulary, promotion, per-position evidence, session identity, and storage),
**WO-2** (the quick-sweep harness), and **WO-8** (room-line adoption). Their
text stands as written.

> **RATIFICATION STATUS — read this before acting on the ladder.**
> **The ladder as a whole is PENDING OWNER RATIFICATION** (it is decision 1 of
> the panel's check-in, and sits in the Monday rulings queue). One arm of one
> rung has been executed: **P0's fixed-mic arm is DONE**, run under the standing
> autonomous-audio authorization while the owner was away. **No P1–P4
> implementation has merged** as of `5c7029b63`.
>
> The owner has since given a **provisional green light to build along the
> ladder** ahead of ratification (see [the campaign](#the-campaign--rounds-to-monday-2026-08-03-and-beyond)).
> That authorizes *work*, not *conclusions*: a round may ship a rung's code, and
> still no rung is settled until Monday's ratification and measurement
> validation.
>
> Where a rung conflicts with a work order, **the rung wins pending
> ratification** and the WO item is marked *superseded-pending* below — meaning:
> do not execute the WO item as written, and do not treat the rung as decided
> either. If the owner declines the ladder, every *superseded-pending* mark
> reverts, the WO text stands as-is, and the rounds built on that rung are
> re-scoped rather than defended.

---

### Rung P0 — Measure the repeat floor on hardware

**What it is.** Repeat the shipped stage-2 VERIFY measurement of an unchanged
profile at fixed geometry, and persist the per-bin / per-band repeat floor as a
product number. All three panel lenses independently made this their
prerequisite: without a measured floor, no attempt-to-attempt delta is readable.

**Status.** **Fixed-mic arm DONE** (2026-07-31, jts3) — verdict, thresholds, and
three loop-shaping findings in `captures/repeat-floor-20260731/README.md`.
**Mic-replacement arm NOT MEASURED** — remove / replace / re-aim / re-measure is
the dominant cross-session term and needs hands; it is on the Monday runbook and
has no issue. Rung pending ratification.

**Reshapes.** Nothing in WO-7 is contradicted; a **constraint is added**.
**WO-7's only stopping rule is the attempt budget** (~3 attempts, then honest
"as good as it's gonna get" copy) — read it at
[`attribution-stage-plan.md`](attribution-stage-plan.md) §7. It contains no
repeatability test; the only mentions of "repeatability" in that document sit in
**WO-5's** paragraph, describing its verify-fail discriminator — a different
rule.

The formulation *"in-spec and within repeatability"* comes from the **panel**,
not from WO-7 — `SYNTHESIS.md` P0 and `measurement-verdict.md` both describe
"WO-7's early-stop rule" in those words. Treat it as the panel's **proposal for
a rule WO-7 does not yet have**, and cite it to the panel artifact rather than
to the work order. (An earlier revision of this row quoted it as WO-7's own
language, which would have made the owning document mis-describe itself.)

So: **P0 adds a floor-aware stopping constraint to WO-7's budget-only loop** —
a measured per-bin/per-band floor, plus a **re-baselining rule** the bench
produced: the floor against a *fixed* baseline walks with drift, against the
*predecessor* it does not, so the loop must compare consecutive attempts or
re-baseline. Marked **superseded-pending** because adopting the constraint
changes WO-7's stopping behaviour, not because any WO-7 text is wrong today.
P0 also supplies, for the VERIFY instrument specifically, the repeatability
numbers the appendix's **P-track H1** was chartered to supply behind the
P-track P2/P4 constants.

**Research inputs.** None — P0 is a measurement, not a reading.

**Issues.** None open. The remaining arm is a hands item scheduled in **R13**;
see `captures/MONDAY-RUNBOOK-2026-08-03.md`.

---

### Rung P1 — Frame discipline: fit and disclose before differencing

**What it is.** Fit and disclose the (offset, tilt) between two curves' frames
before differencing them, and grade the tilt-removed residual beside the raw
one. Small; no new capture. It re-derives the honest version of every
cross-frame comparison the product makes — including the corrected headline that
~84% of the scorecard's "2.02× optimistic prediction" was instrument tilt, not
model error.

**Status.** LANDED in R9 (2026-08-01) under the provisional green light —
#1989 (gating contract v1), #1987 (frame discipline), #1991 (early-fire
prominence vote), #1994 (disclosure render + both #1974 sites), all gated
0/0; corpus replay regression clean (zero unexplained deltas). Ladder
ratification itself remains Monday's. Known limit, disclosed not hidden:
the frame fit over the notch-excluded mask is necessary but not
sufficient (skirt residual can still lever a band-edge fit — #1990 §B
carries the measured table and the option set; fit-side margin is a
threshold decision for ratification).

**Reshapes.** Precedes WO-6 and WO-7. **WO-5's frame-anchor question (Q-E) is
superseded-pending**: which anchor the reference frame uses is not a free choice
once P1 fits and discloses the frame — the ruling must land on P1's disclosure,
not beside it. Does not move the 3.0 dB frame tolerance (the panel's
what-not-to-change list is explicit: re-derive under P1 rather than widen it).

**Research inputs.** Gating report (#1969) and its follow-ups — the gating
*contract* (prove and report what the gate did) is P1's disclosure half.

**Issues.** #1966 (**R9** — adaptive gate never fires; measured reflection
precedes the search window), #1867 + #1967 (**same mechanism** — the ≥4 kHz
echo-band floor makes the crossover-region rungs and any crossover-region null
structurally invisible; **#1967 is scheduled R10**, because the unclamp is only
safe once the objective carries the crossover context), #1974 (**moved R12 →
R9** — inconclusive copy blames a reflection on a path with none; R9's exit
requires the gate disclosure to RENDER, and the screens carrying that copy are
the render slot, so the two ship together), #1750 (**R12** —
`detect_echo` bounds round outside the search window), #1790 (gating v2 —
detection, aggregation, anomaly policy), #1783 (chart paints below the validity
floor, legend blames interference), #1859 (byte-identical DSP, 3–7.7 dB apart —
frame or physical?), #1857 (worst-band pointer anchored on a full-range mean),
#1830 (per-band SNR verdict dead where it now matters), #1818 (ambient window
slides, pulling courtesy beeps into the floor estimate), #1847 (`band_levels_dbfs`
Hann-weights chirps — sub-bass reads ~10 dB low), #1938 (**R8** — fixture
defaults `trim_db` from the default curves, mislabeling every custom-curve
caller, closed), #1882 + #1883 (alignment-confidence coverage; `sweep_anchor`
derivation — **#1883 part 1 is R12**), #1652 (CHECK-SNR gate + noise-attributed
VERIFY failures), #1672 (per-serial mic cals disagree ~4.7 dB above 8 kHz),
#1774 (**R12** — re-baseline S0 under the corrected calibration sign), #1969
(**R8/R9** — the banked research: certified in R8, implemented in R9), #1983
(`gating.py`'s detector tuning comment points the wrong way — a measured
spin-out of #1969's certification; the R9 block below says R9 declines to carry
it, but #1989 **did** carry it, and WO-6 then re-derived the same K walk at the
shipped vote).

---

### Rung P2 — Capture identity

**What it is.** Persist what a capture *is*: the pose observable (the
inter-driver differential arrival the flow already computes), `floor_source`,
the DSP fingerprint, the role label — and accept declared driver spacing instead
of hard-coding 0.0.

**Status.** PENDING RATIFICATION; nothing merged. **Not yet scheduled to a
round** — the rung that most wants ratification before it is built, since it
changes what every future capture records.

**Reshapes.** Precedes WO-4: **WO-4's "the mechanism set can be frozen now" is
superseded-pending**, because a registry citing evidence that cannot say where
the mic was banks unfalsifiable findings. P2 is also the **first rung of the ToF
stream**, and the panel's recommendation is explicit — **do not build ToF v1
yet** (it is blocked on baseline *b* and Δ_AC regardless).

**Research inputs.** ToF report (#1877, closed) — the survey and v1 design the
ToF stream inherits; P2 takes only its latency-free differential-arrival
observable, not the v1 build.

**Issues.** #1960 (live jts3 profile carries four boosts current main would not
emit — provenance), #1976 (bundles omit `verify_program.wav`; 28 verify captures
un-replayable), #1971 (**R12**, design half — splice/schedule gates structurally
blind on the VERIFY path), #1864 (plumb declared driver spacing into parallax — the literal
hard-coded 0.0), #1656 (calibration identity follows the saved setup, not the
physical mic), #1660 (relay path never threads `device` into the calibration
identity guard).

---

### Rung P3 — Fix the objective assignment

**What it is.** Stop asking a stage to solve a problem that belongs to another:
give per-branch EQ a **crossover-shaped** target instead of a flat one, make the
summed model carry the **committed** delay and trim, and close the emit loop
offline (the CamillaDSP render harness exists, aimed elsewhere). Two of three
seams are mis-assigned today; this is also the likely home of the genuine
1000–1600 Hz model error left after tilt removal.

**Status.** PENDING RATIFICATION; nothing merged. **R10a (target + guards
half) opened 2026-08-01 in the extended R9 session under the owner's
same-session authorization** — see the split note at the end of the R10
campaign section. R10b (alignment + emit) follows it, never precedes it.

**Reshapes.** Precedes WO-6 and **any delay work** — so **WO-3's delay-adoption
path (Q-A) is superseded-pending**: no delay value is adopted on model evidence
while the model does not carry the committed parameters. Note WO-3's own
reachability caveat is unchanged (the in-model optimum at +125 µs is λ/4 at Fc,
outside the shipped ±λ/6 snap radius).

**Research inputs.** Crossover report (#1968) — directivity-matched Fc selection,
the ranked alignment-verification instruments, and the "no correction filter may
act more than ~½–1 octave beyond a branch's acoustic passband edge" rule that is
the direct guard for #1817. Dirac paper digest — position-stability: invert only
what is stable across positions.

**Issues.** #1817 (**R10** — linearization fits a crossover-shaped branch
against a flat target; the canonical instance, and the source of R10's
passband-edge rule), #1868 (VERIFY grades measured-vs-model, so a real null the
model reproduces passes), #1954 (**R10** — room layer designs depth on the mean;
wire per-frequency spatial variance into the cap), #1955 (constrain the
L/R per-channel axis before Increment 5 exists), #1894 (measurement-adjudicated
Fc + topology), #1675 (programmatic ka/directivity guidance from declared
dimensions), #1752 (`compose_envelope` smoothing leaks depth past a term's own
zero), #1654 (widen the tweeter sweep to the declared floor — shelved, with a
revival trigger WO-5 depends on), #1870 (owner bench: adjudicate tweeter delay at
Fc and name the τ≈303 µs reflector), #1968 (the banked research itself).

---

### Rung P4 — Wire the learning loop

**What it is.** Make the machine remember. Every learning signal currently
terminates before a consumer: the findings store has zero readers,
`flatness_improvement_db` has zero consumers, and VERIFY's own model-error record
is discarded. P4 is the read path plus the render, the household-wire delta
labeled honestly as model-vs-model, and per-speaker persistence of VERIFY's
model error. Cheap — every piece exists; nothing reads it.

**Status.** PENDING RATIFICATION. First rung merged — the findings reader,
#1982, 2026-07-31; **ratification still pending**. Then **R11** for the
attempts loop.

**Reshapes.** Precedes WO-6 and WO-7. WO-7's per-attempt
predicted-vs-measured evidence stream is **unimplementable until P4 exists** —
it is the same read path. No WO text is contradicted; the dependency is added.

**Research inputs.** None directly. (The Dirac digest's separate
position-stability argument lands on rung P3, not here.)

**Issues.** #1964 (done-screen verdict baked at plan-build time), #1965 (Full
tier reads a block that cannot exist at stage 1 — Full shows less than Express),
#1784 (full-band honest two-panel before/after), #1927 (**R8** — VERIFY
pilot-transfer baseline never expires — and see P0 finding 1: a fixed baseline accumulates
drift comparable to the whole floor within ~15 attempts, so this is a rule, not
just a bug, closed), #1876 (convergence: does a clean-slate re-run reach the same tune?),
#1844 (LLM-native tuning workbench W1–W4 — the consumer the loop feeds).

---

## The campaign — rounds to Monday (2026-08-03) and beyond

The middle layer between strategy and a session. The ladder says *what order the
problems get solved in*; the campaign says *which round solves which, and where
in the tree*. A round is a session-sized unit of work with one mission and an
exit criterion; **every code round** additionally owns one code territory.
(R13 is the owner's hardware-and-rulings day — a round by scheduling, with no
code territory and no slice to re-prove.)

> **Provisional green light, honestly stated.** The owner has authorized
> **building along the P0–P4 ladder now**, ahead of formal ratification.
> **Per-rung ratification and measurement validation happen Monday** — so a
> round may build a rung's code, but no round may claim a rung is *settled*.
> If a rung is declined or reshaped on Monday, its round's output is
> re-scoped, not defended.

### Governing principles

**1. Thin vertical slices — a hard invariant.** The product works end-to-end
**today**, and it must work end-to-end **at every round close**. Rounds layer
sophistication onto a working whole; there are no long-lived half-migrated
states and nothing that "comes together at the end." Every behavior change lands
as a **replacement behind a working flow** — and where a capability is not ready
for households, it lands lab-gated on the production rails, per #1866's
flow-first ruling, never on a parallel path.

The consequence is an exit criterion **every code round** carries, on top of its
own: **the round closes with a deploy to jts3 and a mechanical end-to-end pass
with the fixed mic** — the browser flow clicked through, a measurement taken, a
verdict rendered — proving the slice still works. This is a per-round ritual,
not a milestone; R11 additionally owns the *deep* version of that E2E as a
deliverable. **R13 is exempt**: it is the owner's hardware-and-rulings day and
ships no code, so it has no slice to re-prove.

**2. The 80/20 lens — the standing question.** Every round-open and every work
order asks: *is this the most effective way to prove this out — the cheapest
**honest** validation?* With the owner's guardrail attached: 80/20 means a
**proper staff solution that trims speculative flexibility and complexity**. It
never means minimal-and-hacky, and it never trims quality or honesty. Round 7 is
the evidence it works: the horn verdict came out of captures already on disk,
and the P0 repeat floor came out of 2.7 minutes of audio.

**3. The quality bar is already binding.** Separation of concerns, single source
of truth, elegant / modular / resilient / observable — enforced per PR by the
standing method's adversarial gates ([`AGENTS.md`](../AGENTS.md)). The campaign
adds nothing here and restates nothing.

**4. Rounds are code-locality clusters.** Each round owns **one code territory**:
it knocks out that area's work, validates it as far as offline or fixed-mic
evidence allows, ships it at 0 blockers / 0 should-fixes, and only then moves to
the next area. Two reasons beyond tidiness. First, **file collisions**: round 7
ran three PRs braiding through `crossover_envelope_v2.py` and needed careful
merge sequencing to land them — topical rounds mostly remove that class. Second,
**gate economics**: a delta re-review from the same reviewer is far cheaper on
familiar ground than a fresh read of an unrelated file each round.

Where an issue's fix spans territories, it is scheduled in the round that owns
its **primary file**, and the issue index says so.

**5. The campaign contract.** Strategy *and* campaign live here — one writer. A
per-round brief (`captures/NEXT-SESSION-PROMPT-*.md`) carries only the current
mission. **At every round close the conductor updates
[CURRENT POSITION](#current-position) and re-scopes the future rounds in this
section** — reflect on the grand plan at the end of every round, rather than
letting a handoff quietly re-plan it.

---

### R8 — spine + gating foundation *(Friday 2026-07-31, overnight into 08-01 — COMPLETE)*

**Territory:** program docs and tooling (this roadmap, the harnesses,
`captures/`) plus the findings-reader seam.

**Mission:** give the program its spine, and put the gating research on a
certified footing.

**Deliverables.** This roadmap PR; the consolidated gating-contract work-order
issue; the **detector-certification harness** built offline (dEchorate corpus +
synthetic image-source injection) with the pass criteria **frozen before
testing** — P_D ≥ 0.9 at P_FA ≤ 0.05 for reflections ≥ −12 dB, ≥ 1 ms, ≥ 20 dB
SNR, ToA error ≤ 0.15 ms — then spot-checked against
`captures/gating-experiments-20260731/`; the **P4 findings reader** (the
machine's first act of remembering); debt #1938 and #1927 + #1924; the Monday
runbook.

**Validation:** offline — corpus certification and replay against banked
captures, then the standing fixed-mic slice pass. No new *acoustic* evidence.

**Exit:** spine merged; detector certified against the frozen criteria; banked
findings visible in the envelope. **Plus the standing slice exit** — deploy to
jts3 and a mechanical fixed-mic end-to-end pass.

**Closed:** ten PRs merged at 0/0 across the R7+R8 waves this session — four
in R8 (#1980 spine, #1981 fixture-trim fix, #1982 findings reader — P4
rung 1 LIVE, #1984 baseline lifetime) on top of R7's six landed earlier the
same session. Detector certified offline; verdict keeps the shipped core
(see R9 below). Deployed `1cedf9ee7`. **E2E slice check PASS**
(`captures/r8-slice-check-20260731/` — Express stage-1 walked end-to-end on
the live build; two real anomalies self-recovered exercising the new
honesty copy live; state proven byte-identical; the findings line is
legitimately absent this session, and the "checked N–M Hz" line is
post-apply and structurally unreachable at stage-1 stop).

### R9 — the instrument round (rung P1) *(Friday 2026-07-31, overnight into 08-01 — COMPLETE)*

> **Outcome (2026-08-01).** Mission met: the instrument's graded claims can
> no longer overstate (the one disclosed limit — the band-edge notch-skirt
> lever on the frame fit — rides #1990 §B). Five PRs merged at 0/0 — #1989 (gating contract v1: disclosure
> persisted+surfaced, certified peak fix, asymmetric-cost classification
> guard, E5's both corrections, #1983 comment), #1987 (frame discipline:
> fit+disclose over the notch-excluded mask, tilt-removed graded beside raw),
> #1996 (month-boundary test hermeticity; #1993's product half was then
> settled by measurement and closed — spend is conserved across the
> boundary and cap enforcement reads a rolling window),
> #1991 (early-fire prominence vote K=12/Q=7.5 — the widened-grid corpus
> optimum K=12/Q=13.5 was REJECTED by the jts3 anatomy screen 13/13, the
> bench overruling the corpus exactly as the corpus overruled the research in
> R8; −31% early fires, zero decision changes on the existing corpus), #1994
> (disclosure render: one clause writer for both screens, outcome+code+gate
> an atomic triple, the deictic guard). Replay regression (run at
> `045477e05`, i.e. through #1987 — #1991's gating change has its own
> corpus + anatomy evidence and #1994 is copy/render): 1635/1637 leaves
> identical, 57/57 arrays bit-identical, zero unexplained deltas, honest
> numbers demonstrated from product code
> (`captures/replay-scorecard-20260731/regression-r9-20260731/`). Our-chain
> certification banked (§R9 + §WO-6 of
> `captures/detector-certification-20260801/`). New issues: #1988 (ε-tilt,
> candidate frame-tilt mechanism), #1990, #1992, #1993. Scope note from the
> regression: the product's VERIFY frame is fitted over its graded tracking
> band — every comparison the product makes is now frame-disciplined; the
> 6-octave 84%-tilt construction stays harness-level until R10 gives the
> product a full-band comparison. **Slice exit, honestly split:** deploy
> verified (jts3 = the round's tip, doctor 0 failed / 5 known warnings),
> state-proof paths held. The live probe reached TRANSPORT only — session,
> CSRF, and a review-phase envelope fetch (`verify: null`); the R9
> disclosure lines were never produced live, because the box has never
> entered a verify state, so the disclosure feed rests on the gated
> render-seam tests and the replay, not on a live envelope. No human has
> seen the pixels: background agents cannot click the Browser pane's
> per-action approval for `.local` origins, and jts3's wizard is parked at a
> real owner decision (pending unapplied candidate `8ca42d15…` missing spec
> by 1.5 dB — leftover bench state). Both are Monday-runbook items; verify
> grades applied state, so the live-envelope confirmation follows the
> owner's Apply/Measure-again/Leave-as-is call.

**Territory:** the measurement/analysis layer — `jasper/audio_measurement/`
(`gating.py`, `program_analysis.py`) and the frame/level math analysis consumes,
plus their tests.

**Mission:** make the instrument incapable of overstating what it measured.

**Deliverables.** The gating contract — `source_of_bound` and the pre/post-gate
delta persisted *and* surfaced, the trusted floor (≈2.5/T) disclosed beside the
nominal 1/T, and E5's eval-band ∩ radiated-band correction.

> **Detector plan revised by R8's certification** (source: the detector-
> certification comment on #1969, 2026-07-31; artifacts
> `captures/detector-certification-20260801/`, criteria frozen before any run).
> **Both** detectors fail the frozen criteria, and the research-recommended
> running-local-kurtosis challenger **loses to the shipped core** (AUC 0.331 vs
> 0.625) for a structural reason: kurtosis is blind to band-limited reflections.
> The reports are hypothesis sources, never authorities — the bench overruled
> one, which is the system working. So R9 **keeps the conservative shipped
> core** and ships: (1) the measured **peak-reporting fix** (strict-ToA
> 0.464 → 0.609, byte-identical detection decisions — free); (2) the
> **asymmetric-cost classification guard** — a false detection on a DUT-internal
> feature is catastrophic where a miss is merely optimistic (the challenger fired
> 13/13 on the horn's 646 µs internal feature, which would set the trusted floor
> to 3870 Hz and destroy the whole 2 kHz crossover evidence band), so any
> τ below the minimum gate **classifies and never gates**; (3) the early-fire
> mechanism fix **only if** a variant improves the criteria **on our own ESS
> deconvolution chain** — our-chain certification precedes any swap, because the
> corpus chain's absolute numbers do not transfer. `gating.py`'s
> wrong-way tuning comment is filed as #1983, not carried here.
>
> **All three landed.** (1) and (2) in #1989, with #1983's comment carried
> after all rather than deferred. (3) in the WO-6 PR: the conditional was met
> on our chain (a prominence vote on the shipped candidate), so **R9 does move
> gate decisions** — unlike (1) and (2), which were byte-identical. The
> operating point is `REFLECTION_PROMINENCE_DB = 7.5 dB` at the unchanged
> `REFLECTION_THRESHOLD_DB = 12 dB`, selected on a 315-cell grid and then
> screened against jts3's established anatomy, which overruled the
> corpus-optimal point (`captures/detector-certification-20260801` §WO-6).

Lens A's **frame discipline**: fit and disclose
(offset, tilt) before differencing, and grade the tilt-removed residual beside
the raw one. Then re-run the corpus replay under the new instrument, so the
honest numbers become the product's numbers.

**Validation:** replay regression against the banked corpus.

**Exit:** the instrument cannot overstate; replay regression green. **Plus the
standing slice exit** — deploy to jts3 and a mechanical fixed-mic end-to-end
pass, which here also confirms the new disclosure renders rather than only
persists.

### R10 — the objective round (rung P3, offline) *(Saturday 2026-08-01)*

**Territory:** the fit/prescription layer — `jasper/correction/`
(`linearization_fit`, `peq`, `strategy`) and the fit/trim/alignment code in
`jasper/active_speaker/`; validated through the replay harness.

**Mission:** stop asking a stage to solve another stage's problem.

**Deliverables.** Crossover-shaped per-branch target; a contribution-weighted,
stopband-limited fit enforcing #1817's hard rule (**no significant gain more
than ½–1 octave past a branch's acoustic passband edge**); the summed model
carrying the **committed** delay and trim; the emit loop closed offline through
the existing render harness (the shelf-Q class); a delay search for summed
flatness; #1954's variance-capped depth; and #1967's null-registry unclamp
**with** the new objective context, not before it.

**Validation:** replay against the corpus; predicted-vs-old-fit figures.

**Exit:** crossover-region residual drops on replay; zero filters acting in a
branch's stopband. **Plus the standing slice exit** — deploy to jts3 and a
mechanical fixed-mic end-to-end pass. This is the round where the slice
invariant bites hardest: the new objective replaces the old fit **behind the
working flow**, never beside it.

> **This is the fattest round and the likeliest to split.** If it splits, the
> **target + guards** half precedes the **alignment + emit** half — never the
> reverse, since the emit loop is only interpretable once the objective is right.

> **Split taken (2026-08-01).** The owner authorized continuing into R10
> within the R9 session; the split above is exercised as written. **R10a
> (this session): target + guards** — crossover-shaped per-branch target
> (#1817), contribution-weighted stopband-limited fit with the
> passband-edge hard rule, #1954's variance-capped depth, and #1967's
> unclamp with the new objective context. **R10b (same session if the
> window holds, else next session): alignment + emit** — the summed model
> carrying committed delay and trim, the offline emit loop through the
> render harness, and the delay search. Validation unchanged: corpus
> replay + predicted-vs-old-fit figures; the new objective replaces the
> old fit behind the working flow.

### R11 — the loop round (rung P4 / WO-7 chassis + live fixed-mic validation) *(Saturday night 08-01 / Sunday 2026-08-02)*

**Territory:** the conductor/report layer — `crossover_v2_flow`,
`crossover_envelope_v2`, and the household wire / journal / sidecar surfaces;
plus the in-flow run on jts3.

**Mission:** close the loop — the machine improves a tune across attempts and
knows when to stop.

**Deliverables.** The attempts loop built to P0's constraints
(consecutive-attempt comparison, averaging capped around 4 repeats, a
floor-aware stop); deltas on the household wire labeled **model-vs-model**;
VERIFY's model-error record persisted per speaker. Then run it in-flow on jts3
at fixed geometry — the 0.052 dB grade-metric floor is what makes per-attempt
deltas real — plus a full browser-flow mechanical E2E (published capture page
against the new Pi, every screen, screenshots banked).

**Validation:** live, fixed-mic, on jts3. First round with new hardware
evidence.

**Exit:** the machine demonstrably improves a tune across attempts at fixed
geometry, and stops correctly. The standing slice exit is subsumed here by the
round's own deeper E2E — every screen, screenshots banked.

### R12 — polish + convergence *(Sunday 2026-08-02 / Monday-am buffer)*

**Territory:** copy and UX surfaces — `capture_relay/spec`, `capture-page/`,
the `jasper/web/` wizards, and docs.

**Mission:** make Monday need only the owner's hands and ears.

**Deliverables.** #1941 stages 4-5-7 built (publishing gated behind #1792);
**#1941 Stage 6 BUILD — the walk reorder — merge-gated on the Monday hardware
re-run** (no other round builds it, and its acceptance requires "hardware re-run
on 08-04 before merge"; build it here so Monday's re-run is a *gate*, not a
prerequisite for starting); the copy-honesty set #1974 / #1978 / #1979; the
corpus-pin trio #1774 / #1750 / #1883 (part 1); debt #1948, #1971 (design half),
#1973, #1975, plus the nit ledgers on the #1956 and #1972 PR comments; the
campaign reflection; and the Monday package — runbook final, rulings queued
**with their graphs already generated** (the Q-E frame-drag reframe figure is
already done — `captures/replay-scorecard-20260731/figures/fig-h-issue-1857-qe-frame-choice.png`,
generated in R8; R12 confirms the rest), and the round-13 Monday-E2E brief.

**Validation:** offline; UX review.

**Exit:** Monday needs only the owner's hands and ears. **Plus the standing
slice exit** — deploy to jts3 and a mechanical fixed-mic end-to-end pass, so the
speaker the owner walks up to on Monday is the one the round left working.

> **Slip rule.** If anything slips past Monday it is **R12's UX half** — never
> the P1 → P3 → P4 chain.

### R13 — measure-and-validate day *(Monday 2026-08-03, owner)*

**Territory:** hardware and rulings; no code territory.

The hands-required checklist is `captures/MONDAY-RUNBOOK-2026-08-03.md` — the
owner's first hour back. It carries the rulings queue (ladder ratification, Q-E,
H3-swap scheduling), the mic-replacement repeat-floor arm, the horn A/B, and the
full re-walk.

---

## The research index

Five artifacts banked 2026-07-31. **Read them there; this spine never restates
their content.** All five live in the gitignored, laptop-durable `captures/`.

| Artifact | Establishes | Banked | Consumed by |
|---|---|---|---|
| Dirac paper digest | Correction is a mixed-phase, position-stability problem: invert only what is stable across positions; magnitude-only inversion is the common error. Evidence tier: reasoned argument by an interested expert (Dirac's founder) | `captures/paper-digest-20260731.md` | rung P3 (#1954, #1955) |
| ToF survey + v1 design | Absolute time-of-flight is unrecoverable in a browser chain, but the **intra-capture differential arrival** between two driven drivers cancels the unknown start latency exactly — an angle ruler, not a range ruler, from one position | `captures/DEEP-RESEARCH-tof-report-2026-07-31.md`; issue #1877 (closed) | rung P2 (the observable only — **not** ToF v1) |
| Crossover decision framework | Directivity matching is the automatable Fc rule; reverse-null depth quantifies alignment by vector math (valid for in-phase/even-order only); the dominant failure is EQ undoing its own crossover, guarded by a passband-edge bound | `captures/DEEP-RESEARCH-crossover-report-2026-07-31.md`; issue #1968 | rung P3 |
| Gating and quasi-anechoic measurement | The fixed 7 ms gate is behaving correctly: sub-millisecond DUT-internal energy is un-gateable in principle, so the fix is a gating **contract** that proves and discloses what it did — not a smarter search. **Amended by measurement:** R9's own-chain certification found the shipped detector's dominant failure is firing EARLY (18.1% of criteria-region positives) and that a prominence vote fixes it, so R9 shipped the contract *and* a smarter search (§WO-6) | `captures/DEEP-RESEARCH-gating-report-2026-07-31.md`; issue #1969 | rung P1 |
| Gating follow-ups | Detector certification is adoptable (running-local-kurtosis core, dEchorate + synthetic injection, ROC); FDW is an HF enhancer with per-frequency disclosure, never LF validity; two-mic field separation declined | `captures/DEEP-RESEARCH-gating-followups-report-2026-07-31.md`; #1969 follow-ups comment | rung P1 |

## The phase → issue index

Every open program issue appears **exactly once** — under a rung above, or under
one of the named owners below. The index records *placement*, not progress:
an issue's state, scope, and remaining work live on the issue.

**Round tags.** An issue scheduled into a campaign round carries a `— Rn` tag
where it is indexed. **An untagged issue is not yet scheduled**, which is a
statement about the campaign, not about the issue's importance. Rung and round
answer different questions — a P1 defect can be scheduled in R10 when its fix is
only safe after the objective lands, and the tag says so.

### Not on a rung — program work with another owner

- **Attribution work orders** (owner: [`attribution-stage-plan.md`](attribution-stage-plan.md) §7) — #1866 (the direction and its rulings), #1933 (flow-first WO-2/WO-3), #1869 (WO-3 alignment evidence gaps), #1922 (WO-4: per-driver level-sanity gate and named-driver attribution), #1873 + #1924 (WO-5's deterministic-mismatch discriminator and its copy — **#1924 R8**), #1791 (WO-8 room-correction regime).
- **Two-stage chassis (T1–T3)** — #1806. WO-6 and WO-7 both sit on it; the ladder does not change that.
- **Crossover-v2 flow and product surface** — #1947, #1872, #1863, #1862, #1840, #1788, #1706, #1703, #1684, #1650, #1671, #1665, #1833, #1832, #1926, #1925, #1913, #1860, #1950.
- **Measurement UX and copy line** — #1941 (**R12** — stages 4-5-7, **plus Stage 6 built in R12 and merge-gated on the Monday hardware re-run**), #1979 (**R12**), #1978 (**R12**), #1961, #1865, #1962, #1985 (Crossover review's "Leave it as it is" exits to an unrelated HTTPS interstitial — found by the R8 slice check; untagged).
- **Capture page and relay platform** — #1792 (**R12** — the publish gate R12's UX build sits behind), #1861, #1975 (**R12**).
- **Hardware bench sessions** — #1848 (JTS3 commissioning acceptance). The owner-attended delay/reflector bench is indexed under rung P3 instead, because its output is a P3 input.
- **Corpus and evidence tooling** — #1884 (corpus-pin visibility in CI).
- **Bass Extension program** (separate program, own plan) — #1738, #1723, #1822.

### Outside the program

Named so the boundary is visible, not indexed: #1973 (**R12**, debt), #1952,
#1948 (**R12**, debt), #1789, #1852, #1842, #1843, #1718, #1717, #1716, #1715,
#1709, #1678 — enumerated by owner so the boundary can be audited: CI and
test-infra (#1973, #1716, #1715, #1709), multiroom (#1852, #1842, #1678), Rust
(#1718, #1717), voice (#1843), daemon runtime (#1952), method tooling (#1948),
and one correction-subsystem constant with no measurement behaviour (#1789).
Each has its own owner and none hangs off a rung. Two
are pulled into R12 as debt because that round's territory already touches
them; that schedules the work, it does not adopt them into the program.

## CURRENT POSITION

Update **this block** at the end of every round, together with re-scoping the
remaining rounds in [the campaign](#the-campaign--rounds-to-monday-2026-08-03-and-beyond).
Do not restate strategy in a handoff; move the marker here and point at it.

```
date:           2026-08-01 (R9 closed; R10a opened in the same
                owner-extended session)
jts3_sha:       5f434edb8 — verified deployed (build.txt status=ok),
                jasper-doctor 0 failed / 5 known warnings
active_round:   R10a — the objective round, target + guards half (rung P3;
                territory: jasper/correction/ fit layer +
                jasper/active_speaker/ fit/trim). R10b (alignment + emit)
                queued behind it
active_rung:    P3, under the owner's same-session authorization; ladder
                ratification itself still Monday's. P1 is COMPLETE —
                see the R9 outcome block in the campaign section
last_round:     R9 — the instrument round, COMPLETE at five PRs merged 0/0
                (#1989 #1987 #1996 #1991 #1994), replay regression clean
                (zero unexplained deltas), our-chain certification banked.
                Slice exit honestly split: live probe = transport only
                (review-phase envelope, verify null); disclosure feed
                rests on gated tests + replay; browser-pixel pass + the
                parked wizard decision (pending candidate 8ca42d15…) are
                Monday-runbook items
next_mission:   R10a work orders (this session);
                captures/NEXT-SESSION-PROMPT-round-10.md for a fresh session
blocked_on:     Monday-gated: ladder P0–P4 ratification, Q-E, the
                enclosure-hole timeline, the mic-replacement repeat-floor
                arm, H3-swap scheduling, the #1990 §B fit-margin
                threshold decision, and the two new runbook items from
                the R9 slice check (#1993 was settled by measurement and
                closed — both halves)
```

## How this document relates to session handoffs and issues

One writer per fact. Drift between these is a bug, not a style question.

| Fact | Lives in | Never in |
|---|---|---|
| **Strategy** — charter, rungs, sequencing, what supersedes what | **this document** | handoffs, issues |
| **Campaign** — which round owns which territory, and its exit criterion | **this document** | handoffs, issues |
| Session state — where we are right now | the CURRENT POSITION block above | a handoff's prose |
| The current round's mission and what just moved | `captures/NEXT-SESSION-PROMPT-*.md` (a **mission brief**) | this document |
| A single task: its defect, evidence, and fix | its **GitHub issue** | this document, handoffs |
| Method — conductor rule, adversarial gate, values | [`AGENTS.md`](../AGENTS.md) | everywhere else |

Three planning layers, deliberately: **strategy** (the ladder — what order the
problems get solved in), **campaign** (the rounds — which round solves which,
and where in the tree), **session** (one round's brief). The middle layer is the
one the program was missing; without it every handoff re-planned the program
from scratch.

From round 9 on, a session handoff is: **current position + what moved + next
mission**, pointing here. If a handoff starts re-deriving strategy or
re-sequencing rounds, that content belongs in this file instead.

---

## Appendix — the 2026-07-12 layered-pipeline plan (historical)

> **Status: historical.** Snapshot from 2026-07-12, after the hardware-free
> P-track P1–P7 had merged and before the Room modernization program replaced
> this plan. Preserved for primary-source archaeology — threshold and flow facts
> below will drift. Read this for the layered-pipeline rationale and completed
> program history, not current state. Current shipped behavior lives in
> [HANDOFF-correction.md](HANDOFF-correction.md); intended Room product behavior
> lives in
> [room-correction-information-design.md](room-correction-information-design.md).
> H1's on-device settle, AGC, and threshold work carries forward in that
> design's hardware track. H0/H2/H3/H4 remain crossover/bass hardware work and
> do not become Room-owned tasks. **Its `P<N>` labels are the P-track
> namespace** — see the disambiguation table in the spine above; they are not
> the ladder's rungs.

### TL;DR

This is **not a rebuild.** The layered model is already the repo's
design-of-record (`HANDOFF-active-speaker-dsp.md`'s "Layer Boundary" section
documents Layer A/B/C, and the CamillaDSP graph already composes them in
order behind a `volume_limit: 0.0` ceiling), the shared measurement core
already exists (`active_speaker`
imports the `jasper.correction` sweep/deconv kernel verbatim), the level ramp is
half-built (`correction/autolevel.py`), and bass management is shipped (wireless
2.1 + local-DAC sub, LR4 @ 80 Hz, with the "sub-LP upper ceiling" already the
`graph_safety` invariant). The work is **consolidate → close the loops → prove
on hardware.** The maintainer is away, so all consolidation/loop-closing happens
now (hardware-free); on-device proof is parked with per-PR checklists.

### 0. Governing principles

1. **Conditional layered pipeline.** Active speakers do Layer A first; passive
   speakers (single full-range DAC — the majority) skip it. Detected today via
   `output_topology` (`full_range_passive` vs `composite`).
2. **Three-tier control (the governing rule):**
   - **Safety = hard-enforced. The one thing we block.** No genuinely
     unsafe/conflicting config (full-range to a compression driver, an
     unprotected tweeter, a sub low-pass above 200 Hz). Already fail-closed in
     `active_speaker/graph_safety.py`; every candidate graph re-proves it.
   - **Measurement quality = nudge, never block.** Mic didn't move / uncalibrated
     → a sentence + a checkmark, Continue always live. "That's on them."
   - **Preference / taste = allow, never block.** Wacky tilt is fine — subjective.
3. **One shared measurement core** — `jasper/audio_measurement/` (sweep, deconv,
   analysis, calibration, a parameterized `QualityModel`, a shared
   `RampController`). Layers differ by *method* (near-field-gated vs
   listening-position; per-driver vs summed), not by primitive.
4. **One shared target** across Layers B+C: correction removes the room's
   deviation *from* a target; preference *chooses* that target. Physically the
   two DSP stages stay separate (modal cuts below transition; broadband tilt
   above), but the target, the agent, and the vocabulary are unified.
5. **Verify-by-re-measure with a *deterministic* acceptance verdict** is JTS's
   genuine differentiator — no shipping product (Dirac/Audyssey/Trinnov/
   Sonarworks) and no surveyed paper closes this loop. The LLM proposes;
   deterministic code decides; the room's re-measurement is the judge.
6. **Dumb frontend / smart backend.** The browser captures audio and renders a
   server-computed JSON screen envelope; all smoothing, analysis, verdicts, and
   filter design live on the Pi.

### 1. The layered architecture

For an **active** speaker the ordered pipeline is:

**Layer A — Speaker** (near-field, gated, room *removed*; PRIMARY/foundational,
replaced from presets by commissioning, not skippable): per-driver level-match,
crossover corner/slope, delay/polarity, bass-management high-pass, protection.
Per speaker build. → **Layer B — Room** (listening position, room *included*,
spatial average): modal-region correction to the shared target. Per room. →
**Layer C — Preference** (broadband tilt/shelf, to taste).

For a **passive** speaker the pipeline is **B → C only** (Layer A hidden).

The graph already stages this safely (verified in `active_speaker/camilla_yaml.py
::_emit_baseline_pipeline`): on the stereo bus pre-split → Layer B `room_peq_*` →
`active_baseline_headroom` gain → Layer C `preference_filters` → split mixer →
per-output Layer A driver chain `[bass-mgmt HP, crossover, delay, polarity/gain,
limiter]`, tweeters additionally protective-HP'd, `volume_limit: 0.0` ceiling,
re-proven by `runtime_contract.py` before every load. Preference is strictly
upstream of every driver limiter, so a preference boost can never bypass driver
protection.

The UI becomes a pipeline that walks the applicable layers and lets the user
**re-enter at the right layer** (moved the couch → just redo B; new taste → just
C).

### 2. What exists today (verified against code)

- **Shared core is real:** `active_speaker/driver_acoustics.py` imports
  `jasper.correction.{sweep,deconv,analysis,quality,calibration}` verbatim;
  `camilla_emit.py` is the shared emission leaf; room PEQs compose *into* the
  active-speaker graph as `room_peq_N` slots.
- **Level ramp half-built:** `correction/autolevel.py::AutolevelController` ramps
  `main_volume` from quiet and locks — but ramps *blind* (lock decision in the
  browser), does not recover relay latency, does not pick an SNR window Pi-side.
- **Bass management shipped:** wireless 2.1 (`multiroom/channel_split.py` +
  `reconcile.py` mains-HP) and local-DAC sub (`active_speaker/profile.py
  LocalSubwoofer`), both LR4 @ 80 Hz (40–200 bounds). The "sub-LP upper ceiling"
  is already `graph_safety.py::sub_audible_guard_present` (200 Hz cap + mandatory
  limiter). Gaps: two layers emit the crossover independently; room correction
  (20–500 Hz) overlaps the crossover with no awareness (double-correction risk);
  the bass wizard `correction_bass_flow.py` is a stub.
- **Crossover commissioning** is the most-built layer (~35 modules, safe-by-
  construction, with a fail-closed paired-evidence contract) but **acoustically unvalidated on real
  drivers.** JTS5 runs the crossover live; JTS3 has been flat passthrough (the
  live shrill/hot-tweeter L0 hole). Time/phase: paired polarity persistence and
  proposal admission are built, but the wizard's per-region normal/reverse DSP
  capture loop is still pending; per-driver **delay deliberately deferred**
  (browser captures aren't sample-synced).
- **LLM:** `calibration_agent/` is a live OpenAI advisor
  (`model_client.py::AdvisorModelSettings{provider,model,base_url}`) but
  CLI-only (zero non-test importers), scoped to *preference* auditions, with an
  excellent safety substrate (redaction, strict-schema validation, ±12 dB
  re-clip, reversible audition, prohibited-key blocklist).
- **Room-correction trust gaps (round 1):** confidence gates are computed but
  never consulted at apply (per the maintainer, that's fine — we *nudge*, never
  block); the shown "improvement" is *predicted*, not a real measured
  before/after delta; no honest before/after visualization.

### 3. Subsystem designs

#### 3.1 Level-match ramp (relay-closed) — the maintainer's priority

The analog amp gain is unknown; JTS controls only digital level. Upgrade
`AutolevelController` into a shared `RampController` in `jasper/audio_measurement/`.

**Transport — batched, not singular.** The relay `event` channel is a pure
last-write-wins single slot (`relay/src/worker.js::postEvent` overwrites
`meta.event` on every post) read by the Pi's ~0.75 s poll
(`capture_relay/session.py::DEFAULT_POLL_INTERVAL_S`). Singular per-sample
`{level:{...}}` events would be decimated to ~1 Hz with every intervening
phone post silently lost — not the dense series any transport-delay recovery
needs. So level events carry **batched, client-timestamped sample arrays**:
a rolling 2–4 s window of `{seq,t_client_ms,rms_dbfs,peak_dbfs,clip,
agc_frozen}` samples, posted ≤2 Hz. This is still zero relay *schema* change
(same `event` slot, richer payload) but the brief must budget the rate: level
posts plus phone-status polls must fit the relay's 80 req/10 s per-phone-route
cap. **Race note:** phone `event` posts and Pi `host_event` posts each read
the whole `meta/<id>` R2 object and write it back (`postEvent` /
`postHostEvent`, both `putMeta` after an independent read) — under continuous
level streaming a Pi ramp-control post and a phone level post routinely
interleave and the last writer silently reverts the other's field (a lost
`aborted`, a lost stop/hold host-event). All ramp control signals must
therefore be **latched and idempotent**, with the Pi re-posting until its
signal is observed back, and every phone level event must carry the phone's
current abort/armed state as a superset envelope rather than relying on a
one-shot host-event round trip.
- Play a **quiet-start staircase ramp** (band-limited noise, ~1 dB per ~0.4–0.6 s
  step) of `main_volume` (already 0 dB-clamped in `camilla.py::_coerce_main_volume_db`).
- The phone computes mic RMS→dBFS locally (freeze getUserMedia AGC/NS/EC; report
  `agc_frozen` per event) and streams the batched samples above over the
  existing relay `event` channel.
- **Settle-based two-point mapping, not cross-correlation.** An earlier draft
  of this plan prescribed reusing `capture_relay/alignment.py
  ::cross_correlation_alignment` to recover the transport delay τ between the
  known played envelope and the received mic envelope; that's replaced here
  because the estimator is waveform-domain (48 kHz, 5 ms main-lobe exclusion,
  peak-to-second-peak confidence) and on a ~1 Hz *monotonic*-staircase
  envelope it is structurally near-degenerate — a ramp correlated with a ramp
  yields a broad unimodal plateau where τ can't be separated from the unknown
  amp gain, and the exclusion radius collapses to one sample so confidence
  reads ≈0 even on perfect data. The replacement never estimates τ at all:
  ramp coarse from quiet → once the (delayed) reported level crosses a
  conservative pre-window, **hold ≥ the max loop latency** → read the settled
  level (the gain map is now exact at that held point, transport delay
  already elapsed) → step or jump the computed remainder → hold again →
  require **k ≥ 3 consecutive in-window samples** before treating the level
  as trustworthy → lock. If a correlation-based estimator is ever revisited,
  it needs non-monotonic probe markers (known level dips, not a monotonic
  ramp) and its own validation — `alignment.py` reuse is only honest for
  sweep waveforms, not envelope series.
- **Stop-ahead** the instant the settled mic level enters the **−20…−12 dBFS**
  window with clip margin — never blast up to find it. If the ramp maxes out
  without reaching the window (amp too quiet), the shared default stops and
  tells the user to raise the analog amp; never exceed the 0 dB ceiling to
  compensate. A geometry owner may explicitly accept `bounded_low_level` only
  after fresh cap evidence passes the same frozen-AGC, no-clip, liveness,
  noise-margin, stability-spread, and maximum-shortfall guards. Room and active
  crossover now opt in; the result remains labeled degraded and downstream
  sweep quality still decides whether it is usable.
- **Failure and margin rules** (the previous draft left these unspecified):
  (a) a reading is only trustable once it clears **noise_floor + ~10 dB**. The
  relay adapter derives that floor from a two-second rolling median of ten
  finite 200 ms pre-tone samples, never the first USB-mic startup block; below the
  margin the RMS is ambient-dominated and the early ramp shape is meaningless;
  (b)
  `clip=true` on any sample is an **immediate abort**, not a data point; (c)
  `agc_frozen=false` (iOS has historically ignored the constraint request)
  **degrades to the existing manual-lock UX** with a nudge and disables the
  drift rule below — never silently trust an AGC-compressed level as a
  reference map; (d) the quantitative overshoot guard includes the first
  crossing step: `step_db + ramp_rate × max_loop_latency < half the window
  width`. The coarse staircase stops that full overshoot distance below the
  window bottom, freezes for a settled read, and only then makes one computed
  jump toward the **window midpoint**; (e)
  `RampController` preserves `AutolevelController`'s quiet-start, bounded-rise,
  timeout, clipping-abort, and graceful-stop safety shape. Its relay-ramp
  **shared dynamic cap** is the lower of `original + 12 dB` and **−3 dBFS**
  `main_volume`, with no upward floor for a quiet listening setting. Room's
  listening-position owner uses +15 dB / 0 dB because its stimulus is already
  −12 dBFS. The fixed-reference crossover route now uses that same
  listening-position policy for a geometry-scoped level action before each
  driver's stationary far-field repeats. Existing 3 cm crossover
  near-field retains the tighter kernel default. It remains
  tighter than, and is not to be confused with, the independent 0 dB hard
  ceiling above. Terminal ramp snapshots expose sample-admission counts and
  maximum observed RMS/peak, trust threshold, and trust deficit so a zero-
  trusted failure identifies noise, AGC, non-finite input, or feed loss.
- **Lock**, scoped **per mic-geometry step, not blanket per-session.**
  Near-field (Layer A, phone at the baffle) and listening-position (Layer B)
  differ by roughly 15–25 dB at the mic for the same played level — a
  listening-position lock reused for a near-field capture blows past the
  window into clip, and the reverse starves listening-position SNR. The flow
  re-ramps on every geometry transition (cheap once `RampController` exists).
  Layer A further treats each physical driver as its own geometry and uses a
  preset-derived tone inside that driver's protected passband; woofer, mid, and
  tweeter locks are never interchangeable even at the same 3 cm distance.
  `MeasurementLevelLock` is the lock for the *current* geometry step, not one
  value for the whole session.
- **Drift check**, split by cause and computed on the right signal: at the
  **same geometry**, a *uniform* per-band dB shift vs the lock (|mean Δ| >
  ~3 dB, all bands within ±2 dB of the mean) means the amp/volume moved —
  flag + offer re-level. A geometry *change* expects a level shift and must
  not trigger that message — the two must not be conflated in the UI. A
  *non-uniform* change at the same geometry is acoustic, not a level drift.
  Critically, the drift reference must be stored from the **raw
  (pre-`normalize_to_band`) magnitudes** — every capture is normalized so its
  200–1000 Hz band-mean reads 0 dB, which erases exactly the uniform shift
  this check exists to catch; compare sweep-to-sweep on raw band levels
  (`raw_magnitude_db`, already retained in replay artifacts), not ramp RMS
  against sweep levels.

Hardware-free now: the algorithm, the relay batched-event schema, the
settle-based two-point mapping, the SNR-window/stop/lock/drift logic, all
under synthetic/mocked tests. Parked: the on-device settle-cadence tuning and
the iPhone/Android AGC-freeze confirmation (H1).

#### 3.2 Room correction — simple, honest, dumb-frontend

- **One JSON screen envelope** per step (`{screen, curves{measured,target,
  predicted,verify — server-smoothed}, fill_segments[], headline{before,after,
  delta}, verdict_text, nudges[], next_action, progress}`). Browser is a pure
  renderer; all smoothing/thresholds/verdicts on the Pi. **Shipped — P3b
  (#1155/#1157);** the `GET /envelope` endpoint (`jasper/correction/envelope.py`)
  drives the page.
- **Stepped wizard:** entry → mic + calibration *nudge* → level-match (§3.1) →
  guided N-position sweep with "move the mic" prompts → review vs target → apply
  → verify → **before/after result** → save. Every gate is a sentence + a
  checkmark; nothing disabled. **Shipped — P3b (#1155/#1157).**
- **Honest two-tone before/after fill** — a ~40-line extension of the existing
  canvas `drawSpread()`: green where |after−target| < |before−target| (helped),
  amber where a band regressed. Headline one number: "±6 dB → ±2 dB in the bass."
  Never show a raw jagged curve — server-smoothed (variable for design,
  psychoacoustic for the "what you hear" view). **Shipped in P3a (#1151)**,
  then relocated into the §3.2 screen envelope by P3b (#1155/#1157).
- **Real measured before/after delta** in `verify_metrics` — recompute the
  pre-correction deviation over the *same* 50–350 Hz band from the stored measured
  curve (do not reuse `design.before`, which is over a different band), and stop
  calling the *predicted* number "improvement." **Shipped in P3a (#1151).**

#### 3.3 Bass management — corner/slope/level now, delay/polarity via the null-walk

- **Ownership:** the Speaker layer owns crossover corner/slope, sub level, and
  sub delay/polarity; the Room layer corrects the *already-bass-managed summed*
  response (it *reads* the corner, never re-picks it); Preference owns the
  sub-bass shelf. One bass target — never double-cut.
- **The LR4 emit primitive is already shared** — `camilla_emit.emit_linkwitz_riley`
  is used verbatim by both `channel_split` and `active_speaker`. P5's real
  unification work is narrower than it first looks: the **duplicated corner
  constant/bounds** (`channel_split.DEFAULT_CROSSOVER_HZ` vs
  `profile.DEFAULT_SUB_CROSSOVER_HZ`, both already 80 Hz / 40–200 bounds, but
  two independent numbers that can drift) plus the §6 corner-precedence
  default for the main+wireless-sub case. Fix the stale `channel_split.py`
  "mains HP is a V1 non-goal" docstring while touching this file — it
  contradicts the shipped `reconcile.py` mains-HP path.
- **"Reads the corner, never double-cuts" — operative definition.** The
  measured listening-position response already *is* the acoustic sum, so
  "the room designer corrects the summed response" is automatic once it
  measures at listening position; the load-bearing rule is what the PEQ
  designer is forbidden from doing *near* the corner: **no boosts within
  ±1/3 octave of Fc** (an LR4 sum is flat there by design — a measured dip at
  the corner is usually phase/placement, not a room mode, and boosting it
  fights the crossover rather than correcting the room). The room designer
  receives the active corner value, and the envelope's `verdict_text`
  distinguishes "that's your crossover, not a room mode" from a genuine
  room-mode call.
- **Timing is part of bass management.** Sub level rides the §3.1 ramp
  (band-limited to the overlap region). Sub↔mains **delay/polarity** rides the
  same timing-locked null-walk as driver time-align (parked, hardware) — with the
  extra wrinkle that a *wireless* sub also carries snapcast transport delay to
  account for.
- Hardware-free now: unifying the corner constant/bounds, the corner-precedence
  default, the near-Fc no-boost rule + verdict-text distinction, building out the
  bass wizard, and the stale docstring fix. Parked: sub-level ramp on-device and
  sub↔mains delay.

#### 3.4 The tuning LLM — OpenAI-first, one agent spanning both jobs

Reuse the shipped `calibration_agent` propose→validate→execute→revert contract;
extend it, don't rebuild it.

- **Provider:** start on the existing OpenAI adapter — the seeded config
  default is a current GPT model (do not doc-pin a model name here; a model
  rename is a config-value change, not a plan-doc edit). The `provider` field
  on `AdvisorModelSettings` is the swap seam for better models later, though
  today `resolve_settings` hard-rejects any `provider != "openai"` —
  that's the intended current state, not a gap to close in P6. (Correct the
  design doc's stale "Anthropic-first" mandate to "OpenAI-shipped,
  provider-swappable.")
- **Key provisioning:** reuse `OPENAI_API_KEY` from the existing
  `/var/lib/jasper-secrets/voice_keys.env` — it's already there whenever the
  household's voice provider is OpenAI, and `jasper-web` already has group
  read on that file (WS1 Phase 4a). Don't provision a second copy. When the
  household is on Gemini/Grok voice and no OpenAI key exists, the tuning-LLM
  surface is **hidden with a nudge**, never a broken button. Confirm which
  process makes the paid call (correction runs under `jasper-web` today) and
  that it actually has compartment access before wiring the live surface.
- **Two jobs, one agent, one target:** *interpret the measurement* (correction —
  "you've a 60 Hz room mode; here's a tighter filter") and *shape the target to
  taste* (preference — "warmer" → a bounded low-shelf on the shared target), with
  a fixed voicing lexicon and an optional short A/B audition loop that learns a
  small 2–3-D preference vector (tilt + bass level). The LLM proposes bounded
  JSON; deterministic code validates/clamps and is the only writer of CamillaDSP.
- **Two loops, different closure:** correction claims are *verified* by re-measure
  ("55 Hz mode now within 2 dB of target"); preference claims are subjective and
  phrased as questions ("this should sound warmer — better?"). Privacy holds — the
  LLM sees only the redacted curve summary `advisor_context.py` already produces,
  never raw audio.
- Surface it first as an *interpreter/narrator* in the flow (plain-language "here's
  what your room is doing," explains the verdict), then as a confirm-gated proposer
  whose every proposal is simulated and rejected-if-it-would-ring before Apply.

### 4. Roadmap — hardware-free (do now, while Jasper is away)

Each item is one or more small PRs to `main`, each with hardware-free tests.

- **P1 — Foundation.** (a) Close the **L0 safety hole**: one consolidated
  `GraphValidator` wired at the `camilla_yaml` emit gate so a flat graph with a
  tweeter role can never go live, pinned by a test. (b) **Extract the kernel** to
  `jasper/audio_measurement/` (sweep/deconv/analysis/calibration move unchanged;
  add a parameterized `QualityModel(room|driver|ramp)` so forked thresholds
  become profiles) behind characterization tests.
- **P3a — Room correction: honest before/after (§3.2, shipped — #1151).**
  The measured before/after delta and the Pi-computed `fill_segments`,
  rendered into the existing single-page UI; the predicted-vs-measured
  relabel.
- **P3b — Room correction: the screen envelope + stepped flow (§3.2,
  shipped — #1155/#1157).** The `{screen, curves{measured,target,predicted,verify}
  ,fill_segments[],headline,verdict_text,nudges[],next_action,progress}` JSON
  envelope endpoint (`jasper/correction/envelope.py`), the stepped
  dumb-frontend wizard, and the mic/calibration nudges. It landed as the two
  PRs the plan called for: (1) the `GET /envelope` endpoint added *additively*
  alongside the old payloads (#1155); (2) the page migrated to consume the
  envelope and the legacy client-side computation retired (#1157) — avoiding
  the one-shot rewrite of both large files that AGENTS.md warns about on this
  fast `main`.
- **P2 — Level-match ramp (§3.1)** logic: `RampController` + the relay's
  batched level-event schema + settle-based two-point mapping + SNR-window
  stop + per-geometry lock + drift (on raw magnitudes), all under synthetic
  tests. *(Status: implemented hardware-free on `claude/p2-ramp-controller`,
  adversarial-panel remediation applied — buffered settle read with hold
  extension, run-token-scoped feed, armed gate, evidence-gated MAXED_OUT,
  derived safety timeout; H1 on-device tuning pending. Operational summary
  in [HANDOFF-correction.md](HANDOFF-correction.md) §Status.)*
- **P4 — Verify-acceptance loop.** `AcceptanceEvaluator` (store predicted curve
  at apply → after verify compute error-to-target reduction + a "did any band
  get worse" guard → accept / surface / auto-revert on clear regression),
  under synthetic before/after captures. **The acceptance rule, concretely**
  (a naive per-band comparison would revert good corrections on measurement
  noise — see §8): (1) aggregate to **≥1/3-octave smoothed bands** before any
  per-band verdict — never judge on raw per-bin noise; (2) "clear regression"
  = a band worsening **beyond the repeatability floor**, seeded from
  `spatial.py`'s existing 4–6 dB std constants and shipped as **env-tunable
  knobs** (mirroring the `JASPER_CAPTURE_ALIGNMENT_THRESHOLD` pattern in
  `alignment.py`), retuned once H1 supplies real on-device repeatability data,
  **and** an overall RMS delta that's negative beyond noise — not either alone;
  (3) **matched comparison basis** — verify is captured at position 1 (a flow
  instruction) and/or compared against the stored position-1 curve, never only
  against the multi-position average, so before and after are apples-to-apples;
  (4) **one confirmatory re-measure is required before auto-revert** — a
  second concordant verify is cheap, a false revert is trust-expensive; (5)
  every accept/surface/revert verdict emits an `event=` log, lands in the
  envelope's `verdict_text`, and is recorded in the evidence bundle. *(Status:
  implemented hardware-free on `claude/p4-acceptance-loop-v2`,
  adversarial-review remediation applied — outcome-recorded truthful revert
  surfacing, strict-adjacency concordance, floor-level smooth-noise +
  multi-position-basis pins; H1 threshold retuning pending. Operational
  summary in [HANDOFF-correction.md](HANDOFF-correction.md) §Status.)*
- **P5 — Bass management unification (§3.3, non-timing):** unify the
  duplicated crossover-corner constant/bounds and apply the §6 corner-
  precedence default; room correction reads the corner and enforces the
  ±1/3-octave no-boost rule near Fc with the crossover-vs-room-mode verdict
  distinction; build out the bass wizard; fix the stale docstring. *(Status:
  implemented hardware-free. The corner default/bounds/order now have ONE home
  — `jasper.camilla_emit.BASS_MANAGEMENT_CORNER_HZ_*` — and `multiroom.config`,
  `active_speaker.profile`, `output_topology`, and `multiroom.channel_split`
  bind their public names to it (the 200 Hz sub-LP guard ceiling references the
  same constant). The §6 precedence is explicit + pinned in
  `reconcile.outputd_grouping_env` (an active endpoint clears the wireless HP;
  for an active main WITH a local sub, mains-HP is applied once, in CamillaDSP —
  an active main with only a wireless sub currently gets no mains-HP, the
  pre-existing "Remaining" gap in HANDOFF-distributed-active.md, which the
  resolver + `/correction/bass/` display report honestly rather than claim
  away). Room correction reads the corner via
  the new `jasper.bass_management` resolver and `strategy.design_correction`
  excludes boosts within ±1/3 octave of Fc (cuts allowed) + annotates a
  `crossover_region` report block; the envelope's REVIEW verdict_text + a
  `crossover_region_dip_not_boosted` nudge carry the crossover-vs-room-mode
  distinction (envelope schema v2→v3). The `/correction/bass/` wizard is a
  read-only display of the corner/owner/sub/mains-HP state. H-track parked:
  sub-level ramp on-device + sub↔mains delay (H3).)*
- **P6 — Tuning LLM (§3.4):** extend the advisor vocabulary to target + correction
  moves; reuse the existing OpenAI key from `jasper-secrets` (hide the surface
  with a nudge when no OpenAI key is configured); surface the interpreter in
  the flow; the confirm-gated proposer with simulate-before-apply. (Paid-call
  cost discipline per AGENTS.md — never in CI.)
  *(Status: MERGED #1170, 2026-07-06 — through Fable-max adversarial
  review + focused re-review, then LIVE-VALIDATED against real `gpt-5.4`
  via the capped harness (~$0.12 total): the first run caught reasoning
  tokens consuming `max_output_tokens` on the Responses API
  (`status=incomplete`), fixed by pinning `reasoning: {effort: low}` and
  moving one shared `TUNING_LLM_MAX_OUTPUT_TOKENS` (2500) to the model
  boundary; fixtures are refreshed from the real captures. H-track: the
  on-device browser pass of the panel remains.)* Shipped:
  (1) **key seam** — `jasper/calibration_agent/key_provisioning.py` reads
  `OPENAI_API_KEY` FRESH from the `jasper-secrets` compartment file
  (`/var/lib/jasper-secrets/voice_keys.env`); `jasper-correction-web` is NOT a
  Tier-A non-root daemon — it runs as **root** and its unit sources only
  `jasper.env`, so the key is read from the file directly (root bypasses the
  group), not from `os.environ`. Model id via `JASPER_TUNING_LLM_MODEL`
  (default = the current GPT-class flagship, tracking the research provider).
  `availability()` drives the **hidden-with-nudge** surface; the provider seam
  stays OpenAI-only. (2) **vocabulary** (schema v2) — `response.py` gains
  `propose_correction_peq_adjustment` (bounded alt PEQ set, validated against
  the ACTIVE strategy caps + boost-stacking headroom) and `propose_target_move`
  (named target id or house-curve `warmth` in `[-1,2]`); the prohibited-key
  blocklist / re-clip / preference actions are untouched. (3) **interpreter**
  — `POST /correction/interpret` narrates the server-computed residual /
  modes / P4 verdict / P5 crossover annotation / confidence via a redacted
  packet (`correction_advisor.build_correction_advisor_context`: derived
  summaries only, downsampled residual, NEVER raw audio/device ids); a
  **provenance check** flags any user-facing number the model authored that is
  not in the packet. (4) **confirm-gated proposer** — `POST /correction/propose`
  validates + deterministically SIMULATES each correction proposal
  (`proposal_sim`: `peq.predicted_response` + AutoEQ-style ring guard + headroom
  ceiling + P4 `evaluate_acceptance` on the simulated curve), returning only
  simulate-accepted PEQ proposals as `applicable` (target moves are honest
  suggestion-only cards — no apply route; the UI points at the flow's Target
  curve picker); `POST /correction/propose/apply`
  RE-VALIDATES + RE-SIMULATES server-side, requires explicit `confirm:true`,
  requires the P4 judge to have actually run (`missing_acceptance_basis`
  rejection when baseline/target curves are absent), derives `applied` from
  the real outcome (a CamillaDSP-rejected reload reports `applied:false` —
  never a false success), and routes the set through the EXISTING
  `session.apply()` path (no new apply,
  same headroom re-clip). The LLM never emits YAML/FIR/volume; the room's
  re-measure remains the judge. (5) **UI** — a hidden-with-nudge "Tuning
  assistant" panel (explanation + provenance note + confirm cards for PEQ
  proposals only; untrusted model text via `textContent` only), envelope
  schema v4 `tuning_llm` block. Fixture-driven tests only (real-shape OpenAI fixtures
  under `tests/fixtures/`); `scripts/tuning-llm-live-check.py` is the
  budget-capped (`--yes-spend`, 2-call cap, cost estimate) live-validation +
  fixture-capture harness. **Spend accounting is observable AND ledgered**
  (follow-up SHIPPED): each paid call is gated before (a `SpendCap` over
  `household_usage_reader` — voice + tuning ledgers summed — refuses with
  HTTP 429 at the household daily cap) and recorded after into a separate
  `usage-tuning.db` that root correction-web alone writes (never the
  jasper-voice-owned `usage.db`), pricing synthesized text-modality details so
  `gpt-5.4`'s text-only rate card doesn't record $0. Fail-soft record,
  fail-open cap read; voice sessions refuse once tuning spend exhausts the
  shared cap. See `docs/HANDOFF-calibration-agent.md` "Cost discipline".
- **P7 — Active-crossover measurement flow (hardware-free shaping).** *(Status:
  implemented hardware-free on `claude/p7-crossover-flow`, adversarial-review
  remediation applied — real-payload-shape consume guard, `hard_timeout_ms`
  recording-deadline floor, shared `load_commissioning_view` loader feeding
  the envelope the full coordinator input set, server-computed measurement
  exclusion at POST **and** armed time.)* The Layer-A commissioning *flow* is
  fair game now — only its acoustic proof is parked (H2). Shipped: (1) the
  relay `crossover_sweep` transport wired into `correction_crossover_flow`
  (`POST /crossover/relay-capture`, the third `RelayCaptureKind` caller) so
  driver/summed captures ride the **same** relay transport +
  `record_*_capture` upload seam the room/sync flows use — gated +
  default-off, fail-soft when the relay base is unset, reading the play
  payload's REAL shape (`status` + nested `playback.audio_emitted`, top-level
  `test_level_dbfs`/`sweep_meta` — the same canonical read as the same-origin
  JS) and refusing while room/balance/sync measurements are active
  (server-computed at POST, re-checked when the phone arms); (2) the
  stimulus-params alignment — `build_crossover_sweep_spec` derives its role
  duration from the kernel-side signal plan (12 s woofer/subwoofer, 8 s
  midrange/summed, 4 s tweeter; the exact sweep the Pi plays + deconvolves),
  so there is **one** sweep definition, not a forked second one. Driver capture
  holds a 14 s controlled quiet interval before playback; the phone's hard
  deadline is 45 s and the bounded mono-WAV allowance is 5 MiB. Normal stop is
  still the Pi's authenticated `sweep_complete` event. The same 30 s floor + the
  missing `sweep_complete` publication were fixed for the **sync** relay kind
  in the same pass (pre-existing bug: the capture page deadline-kills any
  relay capture whose Pi never posts `sweep_complete`); (3) the **P2 nit** —
  `MeasurementSession.run_level_match` now RETAINS the run's
  `LevelMatchSession` in a single-flight, identity-guard-cleared slot and
  exposes `lock_level_match` / `cancel_level_match`, so a manual Lock/Cancel
  reaches the running `RampController`. Geometry-scoped driver keys + these
  seams now back the shipped near-field and fixed-reference-axis crossover
  level actions; (4)
  a **parallel minimal** commissioning screen envelope
  (`active_speaker/crossover_envelope.py`, `GET /crossover/envelope`) aligned
  with the room flow's envelope-driven pattern — it composes the
  coordinator's **already-built** step model into the shared `{screen,
  verdict_text, nudges, next_action, progress}` shape via the shared
  `commissioning_coordinator.load_commissioning_view` loader (the same
  load-everything-then-compose the `/sound/` card uses; the coordinator is a
  pure composer, so partial inputs silently report a stuck flow — and the
  coordinator's state machine stays untouched, the smaller honest change than
  coupling two disjoint state machines onto the room `SessionState`
  envelope); (5) passive-gating — `active=False` on `/crossover/status` + the
  envelope for a `full_range_passive` speaker (no active driver/summed
  targets), pinned by a test, so passive users never see Layer A (§1). The L0
  gate + graph safety + commission ramp Stop-gates were untouched (safety
  floor). Every acoustic assumption gets an on-device sanity-check line for
  **H2**, not a claim.

**Cross-cutting, every phase:** ships `event=` structured logs for its new
state transitions, a schema-version bump plus a pinning test for any
bundle/`result.json`/envelope field addition, and env-knob defaults (not
hardcoded constants) for any threshold whose true value is hardware-gated.

**Ordering: P3a → P3b → P2 → P7 → P4 → P5 → P6** (P3b and P2 are
parallelizable — disjoint files: the room-flow envelope vs. the ramp
controller — but coordinate on the session/status touchpoints they both
write; P4 may move ahead of P7 if P7 stalls, since verify curves are already
band-normalized and measure/verify already share one locked volume per
session). **P7 explicitly consumes P3b's envelope and P2's `RampController`**
— this resolves, in one direction, the plan's prior internal contradiction
where P7's own brief said it "rides the level-ramp" while the ordering
placed P7 before P2 (built first, P7 could only wire the existing
browser-locked `AutolevelController` and would need rework once
`RampController` landed). If P7 must start earlier than this order allows,
its brief scopes it explicitly to the existing `AutolevelController` behind
a named `RampController` seam, rather than silently building on the old ramp.
P3a is a merge, not a phase — it lands first regardless. P5 and P6 stay
last. Foundation (P1) first in all cases.

### 5. Roadmap — hardware-gated (parked until hardware in hand)

Each carries an on-device validation checklist attached to its PR; **none merges
as "validated" — it merges as "hardware-free complete, on-device pending."**

- **H0 — Prove the loop on JTS5.** A throwaway CLI spike routing a sweep through
  the production active graph to one driver, printing a real level-match number —
  AND confirming the relay + measurement capture actually work on JTS5 (no
  passwordless sudo there; XVF mic absent → phone/relay or USB calibrated mic).
- **H1 — Level-ramp on-device tuning:** settle-cadence tuning, AGC-freeze
  confirmation on iOS/Android, **and** derivation of the P4 acceptance
  thresholds and the P2 window/drift constants from measured on-device
  repeatability — the env-knob defaults seeded in P2/P4 are placeholders
  until H1 supplies real numbers.
- **H2 — Active-crossover on-device *sanity-check*** of the P7 flow (guided L1
  level-match woofer→tweeter; L2 calibrated null-margin polarity). The flow is
  built hardware-free in P7; H2 is the acoustic proof only.
- **H3 — Bass timing:** sub-level ramp + sub↔mains delay/polarity.
- **H4 — Future delay/phase:** the timing-locked reverse-polarity null-walk
  (driver time-align + sub delay), calibrated-mic-gated; the delay filter slot
  already exists.

### 6. Decisions (locked)

- Kernel module: `jasper/audio_measurement/`.
- LLM: OpenAI-first (the seeded config default, not a doc-pinned model name —
  see §3.4), swappable via the `provider` field.
- Shared target across Room + Preference: yes. Auto-revert on clear regression: yes.
- Pipeline branches on active/passive; active is primary/foundational.
- Safety hard-blocks; measurement-quality + preference never block.
- Bass management includes delay/polarity (rides the null-walk; wireless sub adds
  transport delay).
- Hardware validation on JTS5 (H0 confirms relay/measurement there).
- **Corner precedence (default):** when a speaker is both an active main and
  bonded to a wireless sub, the active-speaker local config owns the crossover
  corner; the wireless-sub path defers to it (one writer); mains-HP applied once.

### 7. Execution model (orchestration)

Follows the JTS orchestrator pattern (memory: `orchestrator-pattern-default`).

- **Orchestrator = Fable (Mythos-class), max effort.** Decomposes each phase into
  small PRs, directs implementer subagents, runs review gates, verifies
  load-bearing claims personally, decides merge. Stays in the loop between PRs.
- **Implementers = subagents.** Opus (effort xhigh) for reasoning-heavy or
  safety-critical work (L0 gate, kernel extraction, ramp math, verify-loop, graph
  safety); Sonnet-5 (effort max) for well-specified mechanical work (JSON
  envelope wiring, docstring/doc fixes, the canvas fill, test scaffolding).
- **Reviewers = separate reviewer subagents, always isolated from the
  implementer; the review model is tiered by consequence at Fable's
  judgment — Fable-max (panel for safety-critical) for consequential/subtle
  PRs; Opus-max for low-risk/mechanical PRs; Opus/Sonnet never review
  safety-critical work.** Reviews run **Jasper's canonical staff-maintainer
  adversarial review prompt** (memory:
  `reference_adversarial_review_prompt`), verbatim, scope-adapted per PR, with
  structured output `{blockers, should_fix, nits, findings[], report_md}`.
  Safety-critical PRs — anything touching audio/hearing safety, the
  CamillaDSP graph, DSP math, or secrets — get a **perspective-diverse panel
  of Fable-max reviewers** (correctness + hearing-safety + resilience
  lenses), not one reviewer.
- **Per-PR loop:** plan → implement (Opus/Sonnet) → self-verify (`scripts/test-fast`
  then `scripts/test-merge`; ruff; mypy; shell/rust lanes as relevant) →
  adversarial review → fix to **zero Blocker + zero Should-fix** → Fable verifies
  the load-bearing claims against the code → docs-impact (`scripts/docs-impact.py`,
  `docs-linkcheck.py`) → PR to `main`.
- **Merge gate (hardware-away adaptation):** CI green + adversarial-clean
  (0 Blocker/Should-fix) + Fable's independent verification + docs scanned. Because
  Jasper is away, the usual "hardware-validate before PR" becomes **"attach the
  on-device validation checklist and mark it pending"** — no PR claims on-device
  behavior it hasn't proven; anything that changes live audio on a box ships behind
  its existing default-off/gated posture until §5 validation runs.
- **Coordination:** `main` moves fast (Claude + Codex). `git fetch` + rebase before
  each push; short-lived branches; one concern per PR.

### 8. Risks & open items

- **Acoustically unproven Layer A.** The whole crossover-measurement edifice is
  untested on real drivers; H0 is the gate before trusting L1/L2 numbers. The
  hardware-free work (L0 gate, kernel, safety) is valid regardless.
- **JTS5 relay/measurement unknown.** Whether the relay is ported/working on JTS5
  is unconfirmed — H0's first job.
- **Paid LLM cost** (P6) — reuse the shipped cost discipline; never CI on every commit.
- **Corner precedence** for main+wireless-sub uses the §6 default unless revised.
- **§3.1 transport delay was mis-specified in an earlier draft** (singular
  events over a last-write-wins relay slot, cross-correlated with a
  waveform-domain estimator on monotonic envelopes) — would have made the
  ramp fail-loud essentially always, or spuriously pass on a loudness-deciding
  path. Resolved by the batched-transport + settle-based-mapping rewrite in
  §3.1; P2's brief should not need to rediscover this.
- **§4/P4's naive acceptance rule would revert good corrections on measurement
  noise** — comparing a single verify position against the multi-position
  average, unqualified, sits inside the repo's own 4–6 dB seat-to-seat
  repeatability floor (`spatial.py`). Resolved by the concrete acceptance
  rule (1/3-octave aggregation, env-tunable repeatability threshold, matched
  comparison basis, confirmatory re-measure before revert) now in P4's bullet
  in §4.

---

**Shape note.** The spine runs past the documentation paradigm's "<400 lines"
guidance for a HANDOFF's current-state half. The overrun is the phase→issue
index and the campaign, both of which are the owner-required payload and both
of which scale with the program rather than with the prose. Splitting either
into a second file would defeat the one-spine purpose, so the length is a
deliberate, recorded exception — not licence for the spine's *prose* to grow.

**Verification scope.** The spine (charter, ladder, campaign, research index,
issue index, CURRENT POSITION, the handoff/issue contract) was verified
2026-07-31 against `5c7029b63`: every issue number re-read from `gh`, every
research artifact re-read at its banked path, and every WO claim re-read in
[`attribution-stage-plan.md`](attribution-stage-plan.md) §7–§8. That last check
was **added after the first revision failed it** — rung P0's row had quoted
"in-spec and within repeatability" as WO-7's own language when the phrase is
the panel's, which the review gate caught. The correction is in P0's Reshapes
paragraph; the lesson is recorded here because a reconciliation document is
exactly where an unverified quotation does the most damage. **The appendix
below the spine is an unchanged 2026-07-12 snapshot and was NOT re-verified** —
per the documentation paradigm, historical sections are deliberately not kept in
sync with code. Do not read the date below as a warranty on appendix facts.

Last verified: 2026-07-31
