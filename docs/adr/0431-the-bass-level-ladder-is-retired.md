# ADR-0431: The bass level ladder is retired

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step A5. Supersedes in part ADR-0366, ADR-0370, ADR-0391, ADR-0403, ADR-0405,
  ADR-0408, ADR-0417, ADR-0423 and ADR-0429, each quoted below.

## Context

Finding F1 of the [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): the
`bass/axis` level ladder answers nothing. A bass round played 3 seats × 4 levels
(`LEVEL_OFFSETS_DB`) × 3 averaged passes (`BASS_PASSES`) = 36 sweeps, and a bass trial 72. Every
level row of the 6 banked bass tables says `headroom_verdict: unknown`, no knee was found, and no
code decision reads a table field. The rungs play 59–76 dB at the seat, at least 12 dB under the
point where the boost gives way, and reaching that point would break the 85 dB stop.

Since [ADR-0429](0429-one-in-room-program-the-room-round-plays-with-bass-and-room-off.md) the
in-room round (`room/seat`) plays bass and room off, files the bass view from its seat takes and
trials every bass document. So the ladder has no remaining job.

## Decision

1. **What goes.** The `bass/axis` preset, its `bass_axis` layout, `levels: auto` and the `bass`
   stimulus row leave the registry. The ladder's runner (`run_levels.py`) and its web host path,
   `RoundPacket` and `finish_bass_packet`, the bass tables (`bass_table*.py`,
   `bass_level_evidence.py`), the pair fit in `bass_fit.py`, `bass_comparison.py`, the
   `bass-compare` and `bass-fit-table` views, the bass stimulus (`bass_stimulus.py`) and the
   refusal rows only they raised are deleted. A stimulus row plays on one driver alone; no summed
   take plays one. The bass program row clears no layer, since no run plays its purpose.
2. **One level, not rungs.** A `MeasureSpec` carries one optional level, `level_dbfs`, the level
   a take was levelled to, in place of `level_ladder_dbfs`; `measure` plays one stimulus per
   position at it. A plan states no `levels`, and every run announces itself on its first take.
3. **The bare name.** `--program bass` names no preset, so it refuses as an unknown preset and
   names the presets there are. The in-room round measures bass, and the bass row's trials and
   first plan stay `room/seat` (ADR-0429).
4. **The price stays.** `priced_preflight` prices a run as `run_levels.preflight_levels` did: its
   captures, microphone moves and estimated seconds. The dry run and the session door read it.
5. **What stays.** The DSP block (`jasper/bass_extension/dynamic*.py`), the bass prescription
   reader, the `bass` view (it reads the in-room seat takes), `bass-alignment` and the
   bass-evidence banking (`measurement_bass.py`).

### What this supersedes

- ADR-0366 §2, lines 71–73: "The bass ladder stays a deliberate series (ADR-0365). Its rungs step
  from the seat reference. Every bass layout sits at the seat or at the mark distance. A bass pose
  anywhere else would first need its ladder anchored at its own probe"; and §6's table row at
  line 149: "`bass/axis` | bass · summed · level ladder · bass stimulus | **Preset.**".
- ADR-0370 §1, line 39: "the bass row clears the room layer on every take, and its own layer on
  the base".
- ADR-0391 §1, line 13: "a bass-ladder rung's base plays in the rung's run"; and line 44:
  "`bass-compare`, after #5737 C4."
- ADR-0403 §2, lines 36–37: a stop levels itself as "a driverless summed stop with no bass
  stimulus"; §4, line 76: "The bass ladder steps down from the bass run's own first-spot level.";
  and its consequence at line 99: "a bass take keeps its ladder".
- ADR-0405 §2, lines 20–21: "A bass stop keeps its ladder."
- ADR-0408 §3, line 34: "the bass trial's ladder needs both graphs at one drive level"; and its
  consequence at lines 53–55: "A hand-staged bass stop over a timing take now levels per graph, so
  `bass_fit`, `bass_table` and `bass_comparison` refuse its pair (`*_capture_context_changed`): that
  is why the bass trial keeps one drive level (§3)."
- ADR-0417 §2, lines 18–19: "Every run applies it but a ladder's later rungs, so a ladder
  announces on its first rung's first take only".
- ADR-0423, line 10: "A bass trial's ladder keeps both (§2)."; §2, lines 29–31: "A take that plays
  the bass stimulus keeps its run's fader. The `bass/axis` ladder is a deliberate one-fader series
  (ADR-0403 §4), so its trial still plays both graphs at one fader, with rule A's cut"; and its
  consequence at line 55: rule A reads "the bass ladder's takes".
- ADR-0429 §1, lines 43–44: "The bass row keeps its rule for `bass/axis`: room off on every take,
  and bass off on the base."; and lines 72–73: "`bass/axis` (and `--program bass`) plays it only
  when a run names it, until #6227 A5 deletes it."

## Consequences

- **Hearing:** this only removes plays. Every remaining preset at every layout, for a base, a base
  and a trial, and a trial alone, resolves to the same plan, captures, composed programs, preview
  and preflight as before, so no level changes. `volume_limit` 0.0, the graph doors, the
  `set_volume_db` clamp, the 85 dB commissioning stop, the declared driver caps and ADR-0405's
  probe staircase do not change.
- The run and dry-run answers drop the ladder's `levels` parameter, the presets answer its
  `level_ladder_db`, and the contract and judge answers the bass section's `levels` detail, so
  their schemas move to `jts_round_run/4`, `jts_round_preflight/4`, `jts_round_presets/4`,
  `jts_prescription_contract/4` and `jts_prescription_judgement/4` (ADR-0344 §4).
- A banked `bass/axis` round no longer resolves its preset: the round lists count it for no
  program, and a view that resolves its preset refuses it (no backward support).
- Rule A (`preflight.run_margins`) and the bound in `plan_run` that holds a run's first seat spot
  to 76 dB now have no ladder take to read; #6227 A6 deletes them.
- `repeated_sweep.repeat_summed_program` and the analysis of repeated summed passes have no
  producer left; their tests still build such programs.
- Rejected:
  - `--program bass` as a name for the in-room round. It costs code, and it would change what a
    banked bare `bass` resolves to.
  - Keeping the ladder as an optional tool. Its rungs cannot reach the give-way point under the
    85 dB stop, so it cannot answer what it was built for.
