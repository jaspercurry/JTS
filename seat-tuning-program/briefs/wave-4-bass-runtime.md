# Brief: lane D — bass evidence and runtime (wave 4, rows 4.1–4.3)

You are running **wave 4** of the JTS seat-matched tuning program
(`seat-tuning-program/PLAN.md` on branch
`claude/loudspeaker-tuning-architecture-iephfa`; tracking issue #4502). Wave 3
built the bass family, its candidate kind, its emission and its protection
ladder. Wave 4 turns the frozen limiter-evidence protocol into a real accepted
bundle, then — and only then — lets a runtime scheduler choose a rung by
listening level. One owner-present hardware campaign, one ADR, then two code
PRs, each from a fresh `origin/main` branch `claude/seat-w4-<row>-<slug>`.

**Read §1.0 before anything else: this wave has a gate the plan's row list does
not show, and rows 4.2/4.3 are contract-blocked until it clears.**

## 0. Read first

`AGENTS.md`; the plan §1, §2, §6, §7; ADR-0255, ADR-0257, ADR-0259, ADR-0260
and every ADR wave 3 added; `docs/bass-extension-waves/limiter-evidence-protocol.md`
**in full** (513 lines, protocol revision `2026-07-19b`) and
`limiter-tap-realization.md` **in full** (1,030 lines, "Rev 7 (errata)") — these
are frozen contracts and this wave is the first thing allowed to satisfy them,
not to amend them except as row 4.1b does, deliberately;
`docs/HANDOFF-bass-extension-plan.md` §8.3 (~`:1006-1048`, the R1 transition
mechanism); `jasper/multiroom/runtime_balance.py` (the one live-patch precedent
in the tree); and wave 3's landed rows — the bass candidate field, its door, the
emission, and the ladder evidence.

Every `file:line` in §1 was verified at main `a1994ef69` (2026-09-09) **before
wave 3 landed**. Facts marked **[W3]** describe what wave 3 changed and must be
re-verified at your HEAD; a row whose premise is false stops and reports.

## 1. Facts

### 1.0 The gate: production wiring is contract-blocked, and clearing it is a row

The limiter-evidence protocol's own "Replayable accepted bundle" section states
that Wave 4 production **remains blocked** until all three of: Jasper produces a
real accepted bundle; the bundle and its replay pass independent review at zero
Blockers and zero Should-fixes; and **a later Wave 4 contract revision names
that bundle's exact fingerprint and authorizes a trusted caller**. The
tap-realization amendment's "What this does NOT authorize" section is blunter
still: *no production wiring of `produce_limiter_thresholds`; no profile
writer; **no scheduler**; no live per-stage tapping; no new daemon, route,
socket, timer or unit.*

So the plan's wave-4 row list is incomplete: **row 4.2 (scheduler) and row 4.3
(first production caller) may not begin until an ADR revises that contract.**
That ADR is row **4.1b** below. Do not treat this as a formality to route
around — it is the mechanism by which a bench-produced number becomes something
the runtime is allowed to trust.

### 1.1 The bench and the replay skeleton, at HEAD

`jasper/bass_extension/bench/` is 15 modules / ~5,678 lines and is intact and
faithful to both frozen docs. `limiter_evidence.py` (1,213 lines) implements the
replay skeleton and has **zero production callers**, grep-proved — deliberately:
the protocol's "Pre-production fence" permits only that module and its one test
file to implement it, and forbids any import or call from
`bass_extension.__init__`, a graph emitter, a daemon, or a web backend.

`produce_limiter_thresholds(evidence, *, required_context)`
(`limiter_evidence.py:1194`) is **pure** — no I/O, no clock, no defaulting —
and total over its JSON input domain, with a fixed refusal precedence: missing →
`missing`; wrong type/shape → `inconsistent`; `measured_context !=
required_context` → `stale`; envelope, ordering or domain violation →
`out_of_envelope`.

**The context builder the protocol said was missing now exists.**
`bench/context.py`'s `LIMITER_DOMAIN_MIN_DBFS`/`MAX_DBFS` (`-120.0`/`0.0`) are
identical to the emitter's own validated bound
(`camilla_yaml.py:2168-2170`). What still does not exist is any code feeding a
real bundle plus that context into the replay as a production call.

### 1.2 What an accepted bundle must contain (do not restate; obey)

Root: exactly `kind`, `schema_version`, `protocol_revision`,
`evidence_fingerprint`, `measured_context`, `campaign_manifest`,
`retained_facts`, `targets`. `measured_context` carries exactly 15 named fields
including `limiter_domain_min_dbfs`/`max_dbfs`/`fingerprint` (trusted outputs of
the reviewed context builder, never manual values), `camilladsp_build_id`,
`tap_implementation_id`, and the frozen `detector_reference` string
`instantaneous_float_sample_peak_dbfs_re_unity_at_limiter_input`.

Each `target` is a strict discriminated union: an early-stop arm
(`refused`/`aborted` + `stop_receipt` + `partial_artifacts`) or an `evaluated`
arm with a `discovery_activation_receipt`, `candidate_sources`, a
`discovery_restoration_receipt`, and `candidates_least_to_most_permissive`.
Acceptance requires transfer, quality, protection and transparency passes plus
`digital_clamp_passed`, with `ordered_owner_chain` containing `bass_ext_lt`,
then `bass_ext_subsonic`, then the context's `limiter_name`, in that order.
Candidate settings are strictly increasing in stored order and non-decreasing
from deepest target through natural: a deeper target may never be more
permissive than a shallower one, or the bundle is `out_of_envelope`.

### 1.3 The tap is offline re-render, not a probe

A live tap is not realizable (CamillaDSP drives one playback device per process
and exposes no interior-stage PCM read). Pre/post-limiter artifacts are produced
by deterministic **offline re-renders** through the exact deployed `camilladsp`
binary, file→file, from proof-carrying truncations of the proved live graph.
Fixed points you may not move: **R2** the truncation boundary is exactly
`names[:i]` / `names[:i+1]` around the limiter's index; **R3** only
`enable_rate_adjust→false` and `devices.playback.format` may differ, and
`devices.capture.channels` is validated then deleted; **R5** the render binary
must be the one `jasper-camilla.service` runs (v4.1.3 pinned) and
`JASPER_CAMILLADSP_BIN` is ignored — a set-but-different value refuses the whole
campaign; **R7** the owner path admits only
`Biquad`/`BiquadCombo`/`Conv`/`Delay`/`Gain`/`Limiter`/`Mixer` and no bypassed
step; **R10** the live cross-check of the render's post-limiter peak against
`get_playback_peak_all()` is *the only rule binding a render to reality, and is
not optional* — fail-closed on disagreement, on an unavailable reading, or on an
absent tolerance.

### 1.4 The runtime path a scheduler would use

**The clamp.** `_coerce_main_volume_db` (`camilla.py:143-165`) clamps to
`[MIN_MAIN_VOLUME_DB, MAX_MAIN_VOLUME_DB]` = `[-150.0, 0.0]` dB and logs
`camilla.main_volume_clamped` when it bites; `MAX_MAIN_VOLUME_DB` is
`DEFAULT_VOLUME_LIMIT_DB = 0.0` (`camilla_config_contract.py:141`) — non-negotiable
#1 itself. `set_volume_db` (`camilla.py:666-686`) goes through it. **Never
weaken either.**

**The patch primitive.** `patch_config` (`camilla.py:964-1004`) wraps
CamillaDSP 4.1's generic `PatchConfig` query verbatim. Its docstring carries the
contract: *serialized but NOT ducked* — the duck exists for structural swaps
that can move total graph gain by tens of dB, while a patch writes declared
parameters of filters already running — and *"that safety is a property of the
CALLERS, not of `PatchConfig`"*. It serializes under the global DSP writer lock;
there is no rate limiting beyond that lock and a 5.0 s per-call attempt budget.

**The precedent.** `multiroom/runtime_balance.py:86-99,163` already patches one
named `Gain` filter's parameters live in production today. That is the whole of
the R1 precedent: **no stepped or interpolated sequencer exists anywhere in the
tree** (grep for `instant_retreat`, `gated_re_extend`, `bass_scheduler` returns
nothing). Row 4.2 builds the stepping on top of this one generic primitive.

**The lock and its bass gate.** `dsp_apply.py`'s `_dsp_apply_lock`
(`:499-599`) is an advisory file lock, re-entrant per asyncio task, and refuses
every mutation with `BassExtensionApplyPending` while
`BASS_EXTENSION_APPLY_INTENT_PATH` exists — even on re-entry. Any scheduler
write goes through this same lock and this same gate. Note that **nothing in
production writes an apply-intent file today**: `_intent_payload`
(`apply_intent.py:136-168`) has zero callers since #4563 deleted the wizard's
transaction, while every reader still checks for the file. A scheduler that
needs the two-phase record live again must say so and own the writer; one that
does not must not leave the readers checking for a file nothing writes.

**Latency.** Both mechanisms add zero buffer latency (minimum-phase IIR, no
chunk/queue/rate change), so the 40 ms USB certification is untouched; ≤ 4 extra
biquads is far below 1% of a Pi 5 core.

### 1.5 Listening level: two different numbers, do not conflate them

**(a) The live knob, which is what a scheduler keys on.**
`volume_coordinator.VolumeState.listening_level` (`:157-206`) is a persisted
0–100 int with an `effective_percent` property that reads 0 while temporarily
muted; `volume_curve.percent_to_db`/`db_to_percent` (`:98`,
`DEFAULT_VOLUME_FLOOR_DB = -50.0` at `:26`) convert it; `/state` already
publishes both `main_volume_db` and `listening_level_percent`
(`control/state_aggregate.py:1058-1059`). The bass schema **already speaks these
units**: `targets.TargetPoint.max_listening_level` (`:95`, validated 0–100 at
`:121`) converts through the same `percent_to_db` with the same floor
(`targets.py:154-155,166`). Nothing reads the live figure for bass purposes
today; that selection function is exactly what row 4.2 adds.

**(b) The measurement-session constant, which it is not.**
`seat_level_reference.py` holds the measured seat-SPL reference used once to
derive a crossover session's fixed measurement volume — one writer
(`seat_level_ramp.py`), one reader (`session_volume_plan`), absent is normal.
`SeatLevelTarget` is the band requested for one run; it is not a speaker
property and has no relationship to runtime scheduling. Keying a rung on this
would be a category error.

### 1.6 The headroom budget (what wave 5.1 will inherit)

`_emit_baseline_filter_definitions` (`camilla_yaml.py:1839-1939`) charges, at
`:1896-1912`:

```
total_headroom_db = baseline_headroom_db
                  + total_positive_boost_db(room_peqs)
                  + linearization_headroom_db(linearization, ...)
                  + max(0.0, output_trim_db)
```

negated into one `Gain` named `active_baseline_headroom`, emitted pre-split on
channels `[0, 1]` ahead of every crossover, limiter and tweeter high-pass, with
a hard `MAX_PROGRAM_HEADROOM_DB = 40.0` ceiling that raises.

**There is no bass term, and before wave 3 there was nothing to charge:** the
emitter only ever emitted `targets[-1]`, the natural member, with
`freq_act == freq_target` and `q_act == q_target` — a structural no-op applying
zero boost. **[W3]** Wave 3's emission consumes the candidate's field instead;
re-verify at HEAD whether a boosted rung can now reach the graph, because the
moment one can, this formula is short a term and wave 5.1 stops being
hypothetical. If your reading says a boosted rung can reach CamillaDSP without a
matching headroom charge, **stop and report it — that is a level bug on the
output path, not a wave-5 nicety.**

### 1.7 The non-negotiable surface this wave comes near

`camilla.py:143-165` (main-fader clamp) · `camilla_config_contract.py:141`
(`devices.volume_limit`) · `camilla_yaml.py:2168-2170` (limiter clip bound
`-120..0`, mirrored by `bench/context.py`) · `camilla_yaml.py:1913-1919`
(`MAX_PROGRAM_HEADROOM_DB`) · `bench/activation.py` (patches `clip_limit` live
and never writes the on-disk config) · `driver_safety.py:118` and
`excitation_safety_plan.py:535,737` (the declared caps API: it admits or refuses
a caller-authored request, it never authors one) · the commissioning SPL stop in
`seat_level_ramp.py` (`REFUSE_SPL_CEILING_EXCEEDED` at `:155`, enforced in the
watched loops at `:451-460` and `:802-808`, ceiling
`profile.py:559` `max_commissioning_level_db_spl = 85.0`, bounded 45–85, sole
reader `commission_wiring.py:139-147`). `SAVE_CONFIGURATION` appears nowhere in
`jasper/` and must stay that way.

## 2. Rows

### 4.1 — The supervised limiter campaign (owner-present hardware) — **NN**

Not a code row. The owner runs the campaign on jts3 per
`limiter-evidence-protocol.md`, with the bench's `--live` path from wave 3's row
3.1 part 2, and produces **one accepted, replayable bundle**. Your job around it:
prepare the manifest and the preflight, sit with the owner during the session,
and afterwards prove the bundle replays — `produce_limiter_thresholds(bundle,
required_context=<the built context>)` returns thresholds, not a refusal, and
returns the *same* thresholds on a second run. Bank the bundle where the
protocol says. No production code changes in this row.

Proof: the bundle validates against §1.2 field by field; the replay is
deterministic; R10's live cross-check passed during the session. Gate: owner
present throughout; **NN**.

### 4.1b — The contract revision (ADR) — **the gate for 4.2 and 4.3**

One ADR, docs only, reserved on #4405 before you write it. It must: name the
accepted bundle's **exact `evidence_fingerprint`**; record the independent
review's outcome (zero Blockers, zero Should-fixes, or it does not merge);
**authorize a named trusted caller** for `produce_limiter_thresholds` and say
precisely what that caller may do with the result; and amend the
tap-realization amendment's "does NOT authorize" list to the extent — and only
the extent — that rows 4.2 and 4.3 need. Everything it does not name stays
forbidden.

Write it as an amendment that cites both frozen docs by revision, the way
ADR-0265 amends ADR-0259 §4. Proof: the ADR names a fingerprint that matches
the banked bundle byte for byte. Gate: owner sign-off; this ADR is the wave's
hinge and nothing downstream starts without it.

### 4.2 — The runtime scheduler — **NN**, adversarial review

Pure target selection plus a bounded patch. Build:

1. **A pure selection function** — given the live `listening_level` (§1.5a) and
   the applied candidate's admissible rungs **[W3]**, return the deepest rung
   whose `max_level_db` covers that level, or the natural rung. Pure, total,
   unit-tested against a table; no I/O, no clock. A rung with no passing
   protection evidence is not a candidate for selection at any level — the
   door's admission rule is upstream of this and this function never re-decides
   it.
2. **Instant retreat, gated re-extend.** Retreat to a shallower rung is
   immediate and unconditional on a level rise. Re-extension downward is gated:
   it waits out a dwell and never re-extends into a rung whose evidence does not
   cover the current level. Asymmetry is the point — getting quieter is always
   safe, getting deeper is not.
3. **The patch.** Step the sealed filter pair's `(fp, Qp)` in 4–8 steps over
   0.5–1 s via `patch_config`, patching only the named bass filters' declared
   parameters. Never patch anything structural; never patch the limiter; never
   touch the main fader. Honor the writer lock and the `BassExtensionApplyPending`
   gate (§1.4) — a scheduler that cannot take the lock does nothing and says so.
4. **Observability**, because this changes what the speaker sounds like without
   anyone asking: one `event=` log line per transition naming from-rung,
   to-rung, the level that caused it and whether it was a retreat, plus the
   current rung on `/state`.

No new daemon, no new unit, no new socket, no new timer (the tap amendment
forbids each by name, and 4.1b authorizes only what it authorizes). No new
`JASPER_*` knob. No added latency.

Proof: the selection table; a fixture transition sequence including a retreat
mid-step; a refusal when the lock is held; the `/state` row. Gate:
`/code-review` high, `/adversarial-review`, and the owner's hardware pass.

### 4.3 — The first production caller

The engine's apply of a scheduled candidate: the scheduler's chosen rung reaches
the emitter through wave 3's candidate field **[W3]**, and the emitter charges
its boost into `active_baseline_headroom` (§1.6) in the same change — a rung
that reaches the graph without its headroom charge is the bug this row exists to
not ship. Re-verify the apply chain at HEAD before designing: at `a1994ef69` it
ran web door → `apply_baseline_profile` → `_apply_baseline_profile_locked`
(which deliberately re-evaluates the household's bass state fresh at apply time
rather than trusting the caller) → `build_baseline_profile_candidate` →
`emit_active_speaker_baseline_config` → `apply_dsp_config`. Wave 3 moved the
bass half of that; find where it landed.

Proof: a fixture scheduled candidate applies, emits with the boost charged, and
`_assert_bass_extension_safe` passes; the headroom ceiling still raises when
exceeded. Gate: `/code-review` high; the emitter change is DSP on the output
path → `/adversarial-review`.

## 3. Rules that bind this wave

- Standing rules in the kickoff snippet and plan §7.
- **The two frozen docs are contracts, not guidance.** You satisfy them; you
  amend them only through row 4.1b, only as far as 4.2 and 4.3 need, and never
  by editing them in place to match what the code does.
- Hard stops stay code-owned: the clamps, the declared caps, the SPL stop, the
  door's admission rule, the ladder's fail rule. The scheduler chooses among
  rungs already proved admissible; it never decides how loud a rung may play.
- Do not wire `produce_limiter_thresholds` into any production path before
  4.1b merges. Do not build a scheduler before 4.1b merges.
- One interpreter per concern on the Pi (ADR-0226): the scheduler is not a new
  process.

## 4. Report back

Per PR: link, line delta, deletions with verdicts, the refusal codes added, the
selection table, validation sentinels including the adversarial-review record,
"stale, not fixed here", and any premise in §1 found false at HEAD with what you
did instead. For row 4.1: the bundle's fingerprint, where it is banked, and the
replay's output twice over.
