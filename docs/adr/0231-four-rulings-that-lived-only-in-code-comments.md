# ADR-0231: Four rulings that lived only in code comments are recorded here, and one boundary note

- **Date:** 2026-09-04
- **Status:** Accepted

## Context

The wave-4 fresh-eyes audit found four owner/panel rulings whose only record
in the tree was a one-line (or one-paragraph) code comment in
`jasper/active_speaker/crossover_v2/`: move, split or trim the file and the
rule disappears with it — the same risk
[ADR-0227](0227-owner-rulings-the-prose-pass-surfaced.md) and
[ADR-0228](0228-rulings-carried-out-of-refactor-tuning-on-its-retirement.md)
were written to close for their own batches. `tuning-rightsize-recon/WAVE-LOG.md`
(branch `claude/tuning-rightsize/recon-reports`, planning/recon material —
never merged to `main`, read via
`git show origin/claude/tuning-rightsize/recon-reports:tuning-rightsize-recon/WAVE-LOG.md`)
logs four timestamped prose-pass entries (23:40Z, 00:30Z, 01:00Z, 01:15Z)
naming these four rulings as candidates for exactly this kind of follow-up.
Two of the four (MS-4, MS-14) are cited in more sites than that log counted —
session.py, volume_owner.py and `jasper/web/correction_crossover_v2.py` also
reference MS-14 by name, and program_transaction.py and a test also
reference MS-4 — but none of those other sites is an ADR either, so the
register below still closes the gap; the fuller citation list is given per
entry.

A fifth item is not a homeless ruling but an open question from the same
recon branch's `tuning-rightsize-recon/HANDOFF-W4.md` (item 3): PR #3946
moved the PEQ designer from `jasper/correction/` into
`jasper/audio_measurement/peq.py`, and the handoff asked whether that is
consistent with D11 (room correction is a separate product,
`tuning-rightsize-recon/PLAN.md` row D11, same branch) or amends it. §5
answers it.

## Decision

**These rulings stand, and this file is their home until one earns its own
ADR** (which then supersedes the entry).

### 1. Decision 13 — the capture-source seam's split of concerns

> "Decision 13, #2662: the conductor asks for a capture of program X at
> position Y and a provider answers with WAV plus metadata; how a source
> produces that recording is its own private internals."

**Provenance:** [#2662](https://github.com/jaspercurry/JTS/issues/2662),
ruled 2026-08-17 ("Capture-source ruling (2026-08-17, later the same day): a
sixth ruling adds decision 13 — the commissioning flow gets two first-class
capture sources, a microphone plugged into the Pi beside the existing relay
flow", `docs/active-speaker-tuning-layers-design.md:28-30`, reiterated at
line 1665). The comment reached its current one-line-paragraph form in PR
#3941 (commit `55b299a35`, 2026-09-03, "Trim prose to the AGENTS.md bar in
six crossover_v2 modules"), which is the pass that flagged it as a ruling
with no ADR home (recon `WAVE-LOG.md` 00:30Z).

**Enforces/depends on:** `jasper/active_speaker/crossover_v2/capture_source.py`
— the `CaptureSource` protocol's three conductor-owned hooks
(`authorize_begin`, `on_armed`, `consume_capture`) and the ownership split
(provider mints the session identity; host owns the persisted-code mapping).
Also cited at `tests/test_crossover_v2_capture_source.py:5`.

**Standing.** Binds until superseded by its own ADR; #2662 stays the argument,
this entry the record that it rules.

### 2. Refusals HOLD the incumbent, rather than revert

> "Refusals HOLD the incumbent rather than revert (panel ruling, 2026-08-18):
> an instrument that could not measure has no standing to remove a correction
> adopted on measured evidence. `BLEND_NO_INCUMBENT` is the one arm that
> cannot hold: its cost is that it REMOVES an applied correction
> (`filters=()`), reachable only through a corrupt or absent applied
> profile."

**Provenance:** panel ruling, 2026-08-18. No issue number and no fuller
statement anywhere else — grepped `docs/`, `docs/historical/`, `jasper/`,
`tests/`; this file is the only site that has ever named it. The comment
reached its current form in the same PR #3941 pass as §1
(recon `WAVE-LOG.md` 00:30Z names it explicitly as a ruling with no ADR home; a
prior version of this pass was sent back by the constants reviewer at
01:00Z for dropping the `BLEND_NO_INCUMBENT` cost and reachability facts —
both are quoted verbatim above so this ADR carries them forward).

**Enforces/depends on:**
`jasper/active_speaker/crossover_v2/blend_correction.py` — every refusal arm
of the blend-correction fit returns `_hold(...)` (incumbent filters
unchanged) rather than an empty or reverted correction, with
`BLEND_NO_INCUMBENT` as the sole documented exception. The empty-fit path at
`blend_correction.py:585-589` restates the same rule inline and is
untouched by this ADR.

**Standing.** Binds until superseded by its own ADR.

### 3. MS-4 — a stimulus enters pre-DSP, never through the post-crossover active ring

> "MS-4: a stimulus enters pre-DSP, never through the post-crossover active
> ring."

**Provenance:** hard constraint MS-4, `docs/REFACTOR-TUNING-2026-08.md` §2
("**MS-4 — Stimuli enter pre-DSP.** A stimulus may ride a *renderer-lane*
ring (ingress into fan-in) but never the post-crossover
`jts_ring_active_playback`, whose single-producer epoch takeover *admits* a
stray writer where a raw `hw` device would refuse."). That plan reached
"Status: final" with its gates settled 2026-08-25 and landed on `main` via
PR #3595; it was retired by commit `f6e75f94a` ("Retire
REFACTOR-TUNING-2026-08, carrying its rulings to ADR-0228"), which does not
carry MS-4 (MS-4 is a hard constraint from the plan's contract section, not
one of the twelve S-numbered owner rulings ADR-0228 collects). The comment
reached its current one-line form in PR #3944
(commit `07c9215c3`, 2026-09-03, "crossover_v2: right-size prose in
driver_prescription/planning lane (P3)"), which is the pass that flagged it
as a ruling with no ADR home (recon `WAVE-LOG.md` 01:15Z).

**Enforces/depends on:**
`jasper/active_speaker/crossover_v2/playback_transaction.py` —
`STAGE_ADMIT`. Also cited at `program_transaction.py:57` ("Fresh
re-admission refused the program before any audio (MS-4's gate)"), pinned by
`tests/test_ring_active_endpoint.py::test_both_rings_are_forbidden_test_pcm_targets`
and `tests/test_crossover_v2_program_transaction.py:206`, and restated at
`docs/historical/crossover-v2-engine-design.md:150`.

**Standing.** Binds until superseded by its own ADR.

### 4. MS-14 — every stimulus plays at the declared level, proven, or not at all

> "the level, and MS-14's proof is taken through that claim before ``run``."

**Provenance:** hard constraint MS-14, `docs/REFACTOR-TUNING-2026-08.md` §2
("**MS-14 — Every stimulus plays at the declared level, proven, or not at
all.** The fader is read back and proven before any audio, and a fader that
cannot be proven refuses the capture rather than banking it… **This is the
shape ruling S10 preserves**: it refuses to CLAIM, never to WORK — the
stimulus still plays and the session still measures again."). Same plan,
same "final" status (2026-08-25), same retirement into ADR-0228 without
carrying this row (MS-14 is a hard constraint, not one of ADR-0228's twelve
S-numbered rulings, though ADR-0228's entry for S10 quotes the "MS-14 is the
canonical survivor" sentence from the same plan paragraph). The comment
reached its current one-line form in the same PR #3944 pass as §3
(recon `WAVE-LOG.md` 01:15Z names it explicitly as a ruling with no ADR home).

**Enforces/depends on:**
`jasper/active_speaker/crossover_v2/program_transaction.py` —
`ProgramPlaybackTransaction`. Also cited at `playback_transaction.py:41-42`
(`STAGE_LOCK`), `session.py:138,155,317`, `volume_owner.py:598-627`, and
`jasper/web/correction_crossover_v2.py:5702,5821`; pinned by
`tests/test_crossover_v2_measurement_volume_drift.py::test_a_drifted_fader_refuses_the_capture_before_any_audio`,
`tests/test_volume_owner.py:299-625`,
`tests/test_crossover_v2_engine_skeleton.py:1016` and
`tests/test_engine_twin.py:136`.

**Standing.** Binds until superseded by its own ADR.

### 5. D11 is unchanged: PR #3946 keeps room correction and speaker tuning as separate products

`jasper/correction/` (the PEQ wizard's product code) and
`jasper/active_speaker/` (the speaker-tuning engine) are two products with
two walks — that is D11 (`tuning-rightsize-recon/PLAN.md` row D11, on the
`claude/tuning-rightsize/recon-reports` branch). PR #3946 moved
`design_peq`/`predicted_response` and the rest of the PEQ math out of
`jasper/correction/` whole into `jasper/audio_measurement/peq.py` — the
shared measurement/truth-layer package both products already depend on for
everything else (frame ledgers, driver response curves, calibration). At
this ADR's HEAD, `jasper/correction/session.py` and `jasper/correction/strategy.py`
import `PEQ`/`design_peq` from `jasper.audio_measurement.peq` for the room
product, and `jasper/active_speaker/linearization_fit.py` imports
`design_peq`/`predicted_response` from the same module for the speaker
product; neither product imports the other. This keeps D11's boundary
exactly where it was: the truth layer holds the generic PEQ math once, and
the room-correction product and the speaker-tuning product each consume it
as a leaf dependency, the same relationship every other measurement primitive
already has with both products. **This is not a D11 amendment** — D11 says
the *products* stay separate, not that their shared math must live inside
one of them, and no product code moved into the other.

## Consequences

- Deleting, splitting or moving `capture_source.py`, `blend_correction.py`,
  `playback_transaction.py` or `program_transaction.py` no longer takes
  §§1–4's rulings with it.
- The four sites keep a one-line pointer (`See ADR-0231 §N.`) at the comment
  that used to carry the ruling; nothing else in those files changes.
- §5 closes recon `HANDOFF-W4.md` item 3: PR #3946 is confirmed consistent
  with D11, not an amendment to it. D11 itself is untouched and stays where
  recon `PLAN.md` states it.
- Five decisions in one file is against this directory's one-decision-per-file
  rule, taken deliberately, as ADR-0227 and ADR-0228 took it: each row is a
  quoted ruling plus its site, and five one-paragraph ADRs would cost more to
  read than the rulings are worth. A row that grows an argument earns its own
  ADR, which supersedes the row.
