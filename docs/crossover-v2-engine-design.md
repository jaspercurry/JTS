# Crossover-v2 engine design

> **Status: historical design record.** ADR-0198 removed the unwired engine
> methods and seams described by parts of this file. Current `TuningSession`
> has an `open` / `measure` / `close` surface and four `EngineSeams` fields;
> [`session.py`](../jasper/active_speaker/crossover_v2/session.py) and
> [`session_seams.py`](../jasper/active_speaker/crossover_v2/session_seams.py)
> own that shape. Current operation, Apply, verification, and restore behavior
> lives in the [tuning operator runbook](tuning-operator-runbook.md), with the
> boundary decision in
> [ADR-0198](adr/0198-the-unwired-engine-verb-half-is-deleted.md). The material
> below is retained as design provenance, not as a specification for new work.

## The measure verb

The loop's vocabulary was **measure · analyze · recommend · save**; ADR-0198
(#3342) deleted `analyze`, `recommend` and `save` as engine methods. `measure`
is the one verb left, returning its own typed outcome, in
[`crossover_v2/session.py`](../jasper/active_speaker/crossover_v2/session.py).

| Verb | Returns | What it owns |
|---|---|---|
| `measure(spec)` | `MeasureOutcome` (carrying `StimulusOutcome` per stimulus) | a walk of stimuli, each proven and banked |

`MeasureOutcome.record_ids` and `StimulusOutcome.banked` are the seam between a
walk and what it left behind: a stimulus that played but did not bank is
visible as such rather than absent.

## `TuningSession` and the four seams

`TuningSession` holds one session's state and lifecycle — `open` / `close`,
also usable as an async context manager (`__aenter__` / `__aexit__`), with
`is_open`, `graph_fingerprint` and `banked_record_ids` as its read surface.
Every side effect it can have crosses one of **four fields on `EngineSeams`**
([`session_seams.py`](../jasper/active_speaker/crossover_v2/session_seams.py)),
frozen and injected exactly as `V2FlowSeams` is, so a test double is a complete
substitute rather than a partial one:

| Field | Protocol | Verbs | Owns |
|---|---|---|---|
| `graph` | `SessionGraph` | `install` · `patch` · `restore` | the measurement graph for the whole session |
| `volume` | `VolumeClaim` | `acquire` · `prove` · `release` | this session's one hold on the fader |
| `records` | `RecordStore` | `bank` | banked evidence — write-only since ADR-0198; reading a bank back is the doors-and-banks tools', over the bundle's own files |
| `play` | `PlaybackTransaction` | `run` | ready → admit → lock → play → restore, for ONE stimulus |

**Three of the four are declared in `session_seams.py`; `play` is not.**
`PlaybackTransaction` lives in
[`playback_transaction.py`](../jasper/active_speaker/crossover_v2/playback_transaction.py),
which is why a `session_seams`-only reading undercounts the seams at three. It
sits on `EngineSeams` beside the three lifetime slots even though ruling S1
calls it *internal to* `measure`: that distinction is about **vocabulary** — the
front end and the LLM never name a play transaction — not about who owns the
object. Playing audio is a side effect, and side effects are injected.

None of the three **protocols** is `@runtime_checkable`, deliberately: a runtime
`isinstance` against a `Protocol` compares method *names* only, so an object with
a `run` of the wrong signature would pass and the check would buy confidence it
cannot deliver. They are satisfied by shape, checked by mypy and by the call
actually working, and their method bodies raise `NotImplementedError` so a
partial explicit subclass fails loudly rather than returning `None`.

**These seams are ENGINE-INTERNAL.** The modularity claim is that both front
ends drive the same verb; a caller reaching `session.seams.graph.patch(…)`
or `session.seams.records.bank(…)` would be doing engine work outside the
engine, and the second would bank a record the session never counts in
`banked_record_ids`. The field is public because construction and testing need
it.

**All four seams are `async`, and so is the machinery behind every one.**
`MeasurementSessionGraph`'s three verbs, `VolumeOwner`'s `acquire_level` /
`prove` / `release`, and `program_playback.play_program` were already `async`:
the transport is CamillaDSP over a websocket and every production caller is
already on the event loop. It was **one decision for all of them, not one per
seam**, and it carries a second half — each release path shields its cleanup,
so a cancelled `close()` cannot leave the fader at measurement level. See
[ADR-0179](adr/0179-the-tuning-engines-seams-are-async-and-a-release-completes-before-cancellation-propagates.md).

## What is wired today, and what is not

This document describes the engine's shape. Read it against the wiring, because
at HEAD the shape is ahead of it:

- **`TuningSession` is constructed in production** — the converged preparer
  builds one per stage and holds it for the run's lifetime
  (`jasper/web/correction_crossover_v2.py`, `prepare_v2_session`), and the
  wired walk drives `measure()` for its MEASURE captures.
- **`MeasurementSessionGraph` is the seam implementation that runs in
  production today**, through `crossover_v2_flow` and
  `web/correction_crossover_v2` — it is the part of this design already carrying
  real sessions.
- **The crossover-v2 measurement volume does NOT run through the ranked-claim
  owner.** It is wave 5's one **named exception** (plan §W7):
  `web/correction_crossover_v2._session_volume_io`'s `_set` still writes the
  fader directly, on `SessionVolumePlan`, and was deliberately not routed into a
  seam that wave 6 deletes. So `VolumeClaim` above is the contract the session
  volume is *moving to*, not the path it takes at HEAD. Read the ranked-claim
  section as the design it will satisfy, and `SessionVolumePlan` as what holds
  the level today.

## The measurement session graph

`MeasurementSessionGraph`
([`crossover_v2/session_graph.py`](../jasper/active_speaker/crossover_v2/session_graph.py))
fills the `SessionGraph` seam for the measure stage. It exists because **the
graph the routed stimuli play through was already a session constant**: every
argument `camilla_yaml.emit_active_speaker_program_config` takes at the
production call site is a bind-time closure variable, so the per-stimulus path
was emitting, loading and restoring *identical bytes* for every capture — two
config swaps, two ducks and at least five CamillaDSP round-trips each.

**Emit once.** `graph_yaml()` emits at most once per session and caches the
text. The emitter runs its fail-closed proofs on every call
(`_assert_program_graph_proven`), so caching is what turns MS-13's *"once,
before the first stimulus"* from a scheduling promise into a structural fact.

**Install once — and the idempotent install IS the health check.** `install()`
is the one entry. It emits once, then on every subsequent call proves the
running graph is still the one it submitted and reloads only when it is not.
First stimulus and stomped-by-a-concurrent-writer are therefore **the same code
path, not two**. Ruling S6 pre-authorises exactly this (*"a simple
pipeline-health check may remain"*) and ruling S10 fixes its shape: a graph that
can be put back is put back and disclosed, never a refusal to play. `install()`
returns the fingerprint the next stimulus will play through, and may raise
`SessionGraphError` — which the caller reads as "nothing new was installed",
leaving `restore()` still able to put back whatever an earlier install
displaced.

**Prove per stimulus.** The fingerprint names the **submitted** graph, taken
from the text this class submitted rather than from a readback: a normalized
readback is a default-filled superset and would name a different thing on every
CamillaDSP version. It is provenance a record carries — which graph the evidence
was measured through — and never a gate.

**The install's three properties, all at once** (MS-13, and `09`'s correction
PC-8): **role-routed** — role to output channel; **crossover-free** — otherwise
every driver is measured through the crossover the session is designing, which
is the circularity the whole program exists to break; and **per-driver
protected** — the tweeter high-pass *and* the soft-clip limiter together, on
exactly the tweeter output channels.

**Isolation does not come from the graph.** Three axes, named once so nobody
flattens them (PC-7): **lane = transport · graph = routing · WAV = isolation.**
Channel content rides the stimulus WAV, and the correction lane was measured
passing stereo bit-exactly with an idle channel at exact digital zero.

**The graph does not change, so neither do its proofs.** This installs the same
emitter's output the per-stimulus path installed, so MS-1 (every
`ActiveEmitDevices` field derived and forwarded), MS-2, MS-3, MS-5 and MS-13's
`_assert_program_graph_proven` return contract are satisfied by the emit this
class is handed, unchanged. MS-4 holds for the same reason: the stimulus still
enters on the renderer-lane ring the emitter was already given.

**A summed sweep steps the graph aside rather than sharing it.**
`SUMMED_SWEEP_PHASES` measure the standing production graph deliberately — the
applied system is the thing under test — so the caller restores before one and
installs again after. That keeps the swap count at **two for an all-routed
walk**, and bounded by the routed/summed transitions otherwise, instead of two
per stimulus.

## The volume owner — four ranked claims

**The owner exists and ships.** `jasper/volume_owner.py` collapses **18
production-reachable fader writers — nine of which can interleave inside a
single crossover-v2 measurement session with nothing arbitrating between them**
— into one owner exposing four claim kinds:

**household · transient-duck · session-measurement · commissioning**

It is installed in production (`volume_coordinator.py`'s `VolumeOwner(...)` and
`install_volume_owner(...)`) and already arbitrates the household level and cue
ducks.

**What "ranked" means, exactly.** Three of the four kinds declare a LEVEL — an
absolute dB the fader should read — and they are **totally ordered: household <
session-measurement < commissioning.** The highest-ranked claim currently held
is *the level in effect*. The fourth kind, the transient duck, **declares no
level at all**: it is an attenuation that composes *below* whichever level is in
effect. That asymmetry is the design, not an omission.

`VolumeClaim` (`session_seams.py`) is the seam through which a `TuningSession`
takes the session-measurement claim. **The crossover-v2 flow does not yet reach
the owner through that seam** — see "What is wired today" above: W7 is wave 5's
named exception, and `SessionVolumePlan` still holds the level for that flow.
The owner is live; the crossover-v2 flow's route to it is what is pending.

**One declared level, and the same one all session.** Five overlapping notions
of "the level" collapse to one, which is what ruling S8's level recipe requires
of the measurements it compares — *same drive voltage across every per-driver
measurement, no gain touched between them.* **A level ladder moves the
STIMULUS, never this claim.**

**The 0 dB ceiling is not this seam's to relax.** `devices.volume_limit` stays
`0.0` and `CamillaController.set_volume_db` clamps positive writes; the owner
becomes the write door's only caller, **never its exception**.

**`prove()` is a reader, and `None` is an answer.** It returns the number the
fader actually reads *only* when that is within the confirm tolerance of the
level the claim was acquired at, and `None` otherwise — including when the fader
could not be read at all, and including when the claim has been preempted.
`None` means *not proven*, which **refuses to bank the capture** and never to
play the stimulus or to try again. Returning a raw drifted reading instead would
hand `measure` a number to stamp into a record while the speaker played at a
different one.

**The session re-checks anyway, and that is deliberate.**
`TuningSession._proven_level` takes the seam's answer and compares it again
against the level this session already declared, returning `None` if it does not
agree — *"one comparison against the invariant this session already declares"*,
and its docstring calls this *"not defensive decoration"*. Banking the level is
then banking ONE number that both the stimulus and the record agree on. Both
comparisons use the same test: `volume_latch.READBACK_TOLERANCE_DB` (0.05 dB)
via `fader_matches`, the repo's one *"do these two fader dB values agree?"*
predicate. It is called **once per
stimulus**, not once per spec — a claim can be preempted between two positions
of one walk, and a proof taken before the walk would stamp an unverified level
into every record after it.

`acquire()` may raise *after* registering the claim internally, so the session's
failure path calls `release()` regardless — a half-registered claim is given
back rather than stranded. `release()` runs on every path out of the session,
including after an `acquire()` that raised and again if a first release raised;
it is idempotent and a no-op when nothing is held.

### Duck composition, and what survives the parameter

The release algebra the owner inherits is
[ADR-0004](adr/0004-duck-release-algebra-and-reference.md)'s
`min(reference, current + own depth)` — give back this holder's own attenuation
and nothing else, and never end above the level that should be in effect. **Both
halves are load-bearing**, and the failure modes are opposite: replaying an
entry snapshot strands the fader, while a bare relative release clamps to 0 dB —
loud — when a volume change lands inside the window.

Two properties come with it. A claim that **outranks** the household level
*replaces* the reference rather than joining a `min` against it — the household
level sits below a measurement volume, so including it would win every time and
pull the fader off the declared level. And the bound **never inverts**: supplying
a reference can only lower a release, never raise it.

**On `held_target_db` — retired in 6e.** ADR-0004 records the reader as a
parameter on `jasper/camilla.py`'s release path and adds a scope note saying it
goes dead once the engine installs one session-scoped graph and stops swapping
per candidate. That is what happened. Wave 6d stopped the measurement swap
ducking at all, which left no release for a session-owned level to steer, and
6e deleted the reader with it: **`held_target_db` has zero hits in `jasper/` at
HEAD**, and `_duck_release_target_db`'s docstring now records the
declared-reference exception as gone with the swap that needed it.

What outlives it is the **shape**: ask at release time rather than resolving
eagerly, because ownership can end inside a bracket; a `None` answer is a
**fall-through to the canonical release**, not an error; and the read stays
synchronous and non-blocking, because the release path runs inside a shielded
`finally` where awaiting could strand a ducked speaker. A ranked-claim design
that resolves references eagerly reintroduces that defect under a new name.

**Two different `None`s, and they must not be read as one.** `held_target_db()`
returning `None` means *"I no longer own a level"* and falls through to the
canonical release. `VolumeClaim.prove()` returning `None` means *"not proven"*
and **refuses to bank the capture**. Same spelling, opposite consequence: one is
a graceful default, the other withholds evidence.

## The playback transaction

`play_program`
([`program_playback.py`](../jasper/active_speaker/program_playback.py)) is the
one entry that plays a compiled excitation program (CHECK / MEASURE) through the
speaker's real DSP chain. It composes two things:

- the session-scoped fixed measurement volume (`SessionVolumePlan`) — it
  **acquires** the volume assertion and never opens or closes the session; one
  session spans every phase and the flow owns open/close;
- **program admission** (`readmit_program_from_wav`) — re-admitted from a fresh
  WAV byte readback immediately before playback, exactly as `play_admitted_wav`
  re-admits before an isolated driver sweep.

**The graph is no longer this function's business.** `MeasurementSessionGraph`
installs it once per session and proves it before each stimulus, so the
load/restore pair that used to bracket every capture — **and the two ducks and
five-plus CamillaDSP round-trips it cost** — are gone.

**What stays here is the writer lock.** A stimulus must not have the graph
swapped out from under it mid-capture, and holding the lock across the play is
what prevents that. Playback itself rides the existing verified path
(`verified_program_aplay` → `play_verified_wav`) to `correction_substream`. The
play seam and the writer lock are injected callables, so the orchestration is
exercised end-to-end against a fake aplay/DSP boundary — the same shape
`MeasurementSessionGraph` uses to run without CamillaDSP or ALSA.

## Architecture — four parties, one direction of authority

```
phone (dumb recorder)  →  relay  →  Pi session owner  →  pure decision organs
                                          ↓
                                   pure analysis
```

**Phone = dumb recorder.** Per phase it records a known-length window and
uploads one encrypted WAV. No live phone↔Pi feedback mid-capture and no
per-repeat gestures.

**Pi = the session owner.** `CrossoverV2Session` in
[`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) holds one
session's mutable state, the injected seams, the locks, and the acts that cannot
be undone or repeated (play, publish, apply, commit, journal). It is also the
**adapter** for its one caller, the web host — which is why a one-line
`return self._x` accessor there is a contract rather than scaffolding. Hand
`authorize_begin` / `on_armed` / `consume_capture` to `run_capture_plan`
to drive a session; `snapshot` / `hydrate` carry phase persistence.

**The decisions are not there.** Every verdict rule, admission policy, prior,
program composition, fit, spatial close and grade lives in
[`jasper/active_speaker/crossover_v2/`](../jasper/active_speaker/crossover_v2/__init__.py)
— one module per organ, each pure and separately testable, and that package's
own `__init__` is the index of what each sibling owns. The session reads its
state, calls an organ, and records what came back.

**The direction is the invariant: the session imports the package; the package
never imports the session or the web host.**
`test_no_domain_module_imports_the_host_or_the_legacy_flow` in
[`test_crossover_v2_journey.py`](../tests/test_crossover_v2_journey.py) holds
that line. When a decision starts being made in the session file it belongs in
an organ; when session state or a seam starts being read in an organ it belongs
in the session file.

**Analysis = pure functions.** `analyze_program_capture` in
[`program_analysis.py`](../jasper/audio_measurement/program_analysis.py) maps
`(ExcitationProgram, WAV, cal, geometry, priors) → ProgramAnalysis` with no
hidden state, so every verdict is reproducible offline from the stored
artifacts.

**All side effects cross one boundary.** `V2FlowSeams` carries six required
seams (`play`, `analyze`, `publish_check`, `publish_candidate`, `apply_complete`,
`apply_failed`) plus optional ones a session can run without. The web host
([`correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py)) binds
the real ones; tests inject fakes. Two names still say *conductor* on purpose —
`V2ConductorSnapshot` and `V2ConductorContext`, with `resolve_conductor_context`
and `persist_conductor_state` beside them: they are persistence- and
host-adjacent, and renaming them would rewrite a durable shape for cosmetics.


## Contracts & invariants (preserve these)

1. **Two safety invariants, one owner each.** *Never too loud* — one derived
   ceiling per driver, from declared sensitivities
   (`derive_hf_measurement_ceiling_dbfs` in `driver_protection.py`). *Never the
   wrong frequency range* — declared band plus a proven high-pass before any
   full-range content; MEASURE's channel routing carries each driver's crossover
   filter by construction.
2. **Sensitivities live in exactly one place: the declaration.**
   `declared_effective_driver_sensitivities(draft)` in `design_draft.py` is the
   SSOT, folded through any declared in-line pad. The same mapping threads into
   program admission *and* play-time readmission, so composed levels and the
   admission gate can never disagree about a derived ceiling.
3. **Session volume is `min(reference, max(caps))`, not `min(caps)`.**
   `session_measurement_volume_db` lets the least-sensitive driver reach the
   reference level while more-sensitive drivers attenuate down digitally —
   attenuating downward is always satisfiable, so every driver's cap is
   enforceable at this volume. `min(caps)` starved multi-way systems. Latched
   once per session; refused below the −60 dB emergency floor
   (`EMERGENCY_MEASUREMENT_VOLUME_DB`). **Nothing moves it, including the apply
   boundary.** The `reference` half is the codified −20 dB
   (`MEASUREMENT_REFERENCE_VOLUME_DB`) until an operator runs
   `jasper-seat-level`, which banks the measured volume in
   `seat_level_reference.py` for this derivation to read; the caps half is not
   operator-derivable.
4. **Analysis is a pure function of `(program, WAV)`.** No side-channel state.
   The `program_id` is a content hash and fingerprints both the analysis and the
   candidate, so a re-run can never be mistaken for a resume.
5. **Clock drift is estimated in-capture.** Each MEASURE capture embeds a
   repeated sweep so ε is estimated from the longest available baseline; baseline
   disagreement ⇒ glitch ⇒ reject plus one retry. The repeated sweep is
   **mandatory**, and the primary gate is anchored to the WOOFER's
   first-vs-last located sweep specifically — a design invariant, not an artifact
   of there being only one repeat.
6. **Adaptive gating, never a false verdict.** The reflection gate width sets a
   validity floor `f_valid_hz = 1/window_s`. VERIFY requires its gate window ≥
   MEASURE's; a forced shorter VERIFY gate yields `verify_inconclusive` — never a
   false pass or fail.
7. **Apply is read-only compose, then transactional apply.** `handle_v2_apply`
   reopens the published candidate (the tamper check), gates on
   `expected_candidate_fingerprint`, translates the measured fingerprint into the
   baseline candidate's own fingerprint at the host boundary, then rides the
   existing `apply_baseline_profile` transaction with rollback.

   **7a. A BANKED candidate can be made live again** — `POST
   /correction/crossover/v2/republish` (`{"fingerprint": …}`). The apply slot is
   single-valued and has no lookup, so each measure session overwrites it and a
   failed one leaves it `None`, with every candidate still sitting write-once in
   its bundle. `handle_v2_republish`
   ([`correction_crossover_v2_republish.py`](../jasper/web/correction_crossover_v2_republish.py))
   locates one by its own fingerprint through
   [`candidate_bank.py`](../jasper/active_speaker/candidate_bank.py) (the single
   owner of where banked candidates live), re-verifies it through
   `MeasuredCrossoverCandidate.from_mapping` — the same recompute-and-compare
   apply runs; there is no second hasher — and republishes it with its
   **minting** `session_id` + `evidence.bundle_session_id`. It publishes
   `accepted_phases: ["measure"]` and clears `applied` and the accepted-Sound
   pair, because `_update_current_review`'s compare-and-set gates on all four and
   a failed CAS would apply the graph while recording nothing — no `applied`
   flag, no way-back pointer. It applies nothing: every admission gate reads
   live SSOT, so no state write can satisfy one. **Two things it will not do:** restore
   `verify_priors` (they belong to the stage-1 conductor that ran the fit, so a
   post-apply VERIFY grades INDETERMINATE, never a false pass); and republish a
   candidate whose crossover differs from what `/sound` declares. Journal:
   `event=correction.crossover_v2_banked_candidate_found`,
   `…_candidate_republished`, `…_republish_refused` with a machine `code=`.
8. **The way back is the normal path.** Configs get applied; an earlier
   config gets applied the same way: any banked candidate can be made live
   again via republish-then-apply (invariant 7's door, then the apply door
   with every admission gate it always runs). The one durable pointer is
   `previous_candidate_fingerprint` — the measured candidate the applied
   graph displaced, recorded by `observe_apply_success` and carried
   *unconditionally* across every snapshot, so the wizard's way-back action
   and the round's automatic revert survive a VERIFY re-arm. It is a pointer,
   never a promise: the bank re-verifies the artifact on republish, and the
   apply transaction re-proves the recomposed config. There is no separate
   restore engine, no anchor vocabulary, and no `/sound` declaration undo —
   a revert IS an apply, and writes the declaration the same way any apply
   does.
9. **The walked-away guarantee.** `SessionVolumePlan` holds one measurement
   window with an abort target, a wall-clock ceiling and a restore-once latch
   drained by close, session death, or the ceiling. **Each stage arms its own
   ceiling, sized from the plan it actually emits.** A household that walks away
   can never leave the speaker pinned at measurement volume. The voice-daemon
   measurement pause is held for the whole session so the idle reconciler cannot
   revert it.
10. **The CamillaDSP safety ceiling stays.** `devices.volume_limit` is `0.0`
    (`DEFAULT_VOLUME_LIMIT_DB` in `camilla_config_contract.py`) and positive
    writes clamp to 0 dB in `CamillaController.set_volume_db`. The program graph
    adds no headroom beyond the main volume.

    **10a. The apply boundary's level move is DECLARED, never compensated.** An
    applied graph absorbs its correction's boost as a pre-split common
    attenuation, so the same commanded volume drives the speaker measurably
    quieter. **That attenuation is the excitation-safety property, not a bug to
    cancel** — the graph is `−H` pre-split and `+L_r(f)` post-split with
    `L_r ≤ H`, so a boosted band lands at or under unity however deep the
    correction. Raising the commanded volume to "restore" the level would put the
    boosted band over the compression driver's cap on a sustained swept sine.
    VERIFY therefore measures at the **unchanged** commanded level, and the move
    is declared to the analysis instead: `observe_apply_success` persists
    `expected_post_apply_offset_db` in the same state write as the `applied`
    flag, so the flag that releases VERIFY's hold can never become visible
    without the offset beside it.
11. **Linearization emission is independently re-validated at every boundary,
    never trust-the-caller.** The emitter and the runtime-safety verifier each
    re-prove biquad type ∈ {Peaking, Highshelf, Lowshelf}, gain at or under
    `MAX_LINEARIZATION_BOOST_DB`, and the shelf-placement structure from scratch
    — the fit engine's own vocabulary and per-filter-cap invariants are not
    assumed to have survived a JSON round-trip. The safety-posture rationale is
    owned by
    [`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md).
12. **A submitted graph is proven live before anything plays.**
    `confirm_graph_is_live` normalizes the submitted YAML through CamillaDSP's
    own `ReadConfig` and compares fingerprints strictly. Text equality against
    `GetConfig` cannot work — the readback is a default-filled,
    value-normalizing superset — so both sides come back through the same
    deserialization path. Normalization failure and mismatch stay distinct
    refusals.
13. **Stage 1's graph names BOTH ends of the box's transport.** CHECK and MEASURE
    are the only phases that emit their own graph — the four
    `SUMMED_SWEEP_PHASES` play into the already-active production graph — and
    that emit derives its whole `devices:` block from the resolved playback
    endpoint in one call (`active_emit_devices` in
    [`camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py)), capture lane
    and wire format and latency geometry together. A ring-armed speaker's
    playback endpoint is the ACTIVE ring, not Ring B: naming only the sink would
    sweep into one ring while CamillaDSP captured a lane nobody feeds — silence
    with every daemon healthy. Which endpoint a roleful box resolves is **not**
    this document's to state; the authority is
    [`audio-paths.md`](audio-paths.md).
14. **Every band a per-driver decision is graded over is clamped to the band that
    driver's own sweep excited.** `overlap_band_hz` does it for the GCC
    alignment, trim solve, ripple and VERIFY tracking; `branch_snr_band_hz` does
    it per branch for the capture-SNR verdict. The clamp's contract, its named
    residual, and why an EMPTY window still cannot enfranchise an unexcited row
    are **code-owned**: read `branch_snr_band_hz`'s docstring in
    [`program_analysis.py`](../jasper/audio_measurement/program_analysis.py).
15. **A prescribed round is opened AT what it was prescribed, and never inherits
    one.** Four things can be prescribed — blend and driver stage through
    `jasper-crossover-prescriber`; alignment and topology arrive as request-body
    keys on session open and refuse the WHOLE session at the tap. The four
    classes, the two entry surfaces and the severity split are tabulated once, in
    [`testing-tooling.md`](testing-tooling.md#the-other-two-prescriptions-do-not-come-through-this-door-2773).
    Two consequences are this document's: a **topology** pin replaces the
    session's own corner *and* preset at both stages (via
    `fc_sweep.recornered_preset`), so the fit, the de-embedding, the emitted
    graph and VERIFY's design target are that topology's rather than the
    incumbent's — and stage 2 must rehydrate the pin or it would grade an applied
    graph for not being the crossover it replaced. And a pinned round **publishes
    no selector verdict** — no round does since the corner hunt was deleted
    (ticket 2.3) and its selector retired (ticket 2.4), so `fc_selection` is
    ABSENT from the record rather than written null.


## File map

[`crossover_v2/__init__.py`](../jasper/active_speaker/crossover_v2/__init__.py)
is the organ package's own index — read it for what each sibling module owns.
Design prose lives in each module's docstring. What that index does not cover:

| File | What it owns |
|---|---|
| [`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) | `CrossoverV2Session` — session state, seams, irreversible acts, the host adapter; the capture-plan builders, tier/plan shape, cloud prompts. |
| [`session_volume_plan.py`](../jasper/active_speaker/session_volume_plan.py) | One fixed measurement volume per session: the `min(−20, max(caps))` SSOT plus open/close/abandon and the restore-once latch. |
| [`volume_owner.py`](../jasper/volume_owner.py) | The one owner of CamillaDSP's main fader and its four ranked claim kinds. Sits at `jasper/` root, outside the organ package, so the package index does not cover it. |
| [`measured_crossover_candidate.py`](../jasper/active_speaker/measured_crossover_candidate.py) | `MeasuredCrossoverCandidate` — the fingerprinted apply artifact. |
| [`candidate_bank.py`](../jasper/active_speaker/candidate_bank.py) | Where banked candidates live on disk, and finding one by its own fingerprint. |
| [`linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py) | The Layer-1a correction envelope: per-bin allowed depth and the terms it takes the `min` across. |
| [`linearization_fit.py`](../jasper/active_speaker/linearization_fit.py) | The Layer-1a fit engine: `fit_driver_linearization` and its budgets, bands, give-back, and the trim solve. |
| [`camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py) | The baseline emitter, and the independent re-validation of every linearization filter before it reaches CamillaDSP. |
| [`crossover_envelope_v2.py`](../jasper/active_speaker/crossover_envelope_v2.py) | The pure `status → envelope` renderer: step list, screen dispatch, registry copy. |
| [`delta_probe.py`](../jasper/active_speaker/delta_probe.py) | The realized-vs-commanded map for an applied correction change, and its four verdicts. |
| [`web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py) | The web host: endpoint bindings, durable v2 state, the real seams, apply/restore, `resolve_conductor_context`, `persist_conductor_state`. |
| [`web/correction_crossover_v2_republish.py`](../jasper/web/correction_crossover_v2_republish.py) | The republish door: re-publish a banked candidate by fingerprint so apply can reach it. |
| [`web/correction_crossover_v2_relay.py`](../jasper/web/correction_crossover_v2_relay.py) | The relay capture provider: plan-walk hosting, the phone phase ladder, purge grace, link-TTL policy. |
| [`web/correction_crossover_v2_wired.py`](../jasper/web/correction_crossover_v2_wired.py) | The WIRED capture provider: source resolution, the local plan walk, the answer mint. |
| [`audio_measurement/wired_capture.py`](../jasper/audio_measurement/wired_capture.py) | The wired capture engine: registry-anchored device probe, parameterized S32_LE ALSA capture with exact gap accounting, the ≥128-zero dropout scan, 32-bit WAV encode. |
| [`audio_measurement/program.py`](../jasper/audio_measurement/program.py) | The excitation-program model and its composers. Pure data, no safety decisions. |
| [`audio_measurement/program_analysis.py`](../jasper/audio_measurement/program_analysis.py) | The pure analysis: locate/segment, drift, gated transfer functions, the configured-Fc composition, prediction, VERIFY tracking. |
| [`audio_measurement/timeline_slip.py`](../jasper/audio_measurement/timeline_slip.py) | The sub-sample timeline-step gate and the residual per-driver phase exposure it cannot close (`SLIP_GATE_SAMPLES`). |
| [`audio_measurement/spatial_combine.py`](../jasper/audio_measurement/spatial_combine.py) | The spatial-cloud combiner and the echo/geometry diagnostics. numpy only. |
| [`audio_measurement/interference_nulls.py`](../jasper/audio_measurement/interference_nulls.py) | The interference-null identification gate and the per-position variance classifier. |
| [`audio_measurement/frame_fit.py`](../jasper/audio_measurement/frame_fit.py) | The frame between two curves about to be differenced — the model and its disclosure record, no band and no verdict. |
| [`attribution/`](../jasper/attribution/__init__.py) | Mechanism attribution's schema and persistence half: findings, the declaration registry, promotion, bundle-lifetime storage. |

---

**Scope of this file's warranty — it is two halves with two different
provenances, and they are not equally warranted.**

The sections above the Architecture heading — the measure verb, the seams, the
session graph, the volume owner, the playback transaction, and "what is wired
today" — were **derived from the code at HEAD on 2026-08-26** and each names the
symbol it describes. Read a claim against that symbol before relying on it.

`## Architecture`, `## Contracts & invariants` and `## File map` were **moved
verbatim** out of `tuning-operator-runbook.md`, and they arrive carrying that
file's warranty rather than a fresh derivation: they were themselves a merge of
`llm-operator-runbook.md` and the live spine of
`HANDOFF-crossover-measurement-v2.md`, so **every claim there carries the reading
its source carried**, and the per-pass record of what each of those readings did
and did not re-verify is
[`historical/crossover-measurement-v2-verification-log.md`](historical/crossover-measurement-v2-verification-log.md).
Two consequences worth stating: invariants 13 and 15 say *"this document's"* and
were written when that meant the runbook — they still read correctly under this
owner — and a moved claim is only as fresh as its last pass in that log.

Last verified: 2026-08-26 — the authored sections against the tree; the moved
sections carry their prior readings unchanged.
