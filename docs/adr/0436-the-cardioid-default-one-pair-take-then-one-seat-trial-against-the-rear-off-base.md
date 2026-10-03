# ADR-0436: The cardioid default: one pair take, then one seat trial against the rear-off base

- **Date:** 2026-10-03
- **Status:** Accepted: owner decision 3 on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step B3. Supersedes in part
  [ADR-0303](0303-a-trial-plays-the-candidate-as-composed.md),
  [ADR-0336](0336-the-seat-trial-judges-rear-and-room-from-the-same-seat-takes.md),
  [ADR-0366](0366-one-pose-model-a-level-found-at-the-pose-and-a-band-stated-from-it.md),
  [ADR-0370](0370-each-run-purpose-declares-what-it-plays-and-a-bass-run-plays-with-room-off.md),
  [ADR-0420](0420-a-round-goes-stale-only-when-a-layer-under-it-changes.md) and
  [ADR-0425](0425-the-rear-seed-is-computed-from-the-declared-geometry.md), and restates the
  premise of [ADR-0413](0413-one-resolver-the-door-resolves-every-run-request.md) §3, each quoted
  below. Rewrites the rear bullet of [the measurement-loop doctrine](../measurement-loop-doctrine.md)
  §1a.

## Context

Finding F6 of the [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): the
cardioid default never measured what its program states. The rear row said "Set the rear woofer to
reduce sound behind the speaker", but the page started `rear/pair` at the mark only (the row's
`start`), `jasper-round run --program rear` started `rear/express` at `rear_express` (the first
`rear/*` row), and the rear score needs front and behind takes, so it was always unavailable. The
playbook's Seat loop took the pair twice at the mark and then trialled five graphs at three seats:
17 takes and 27 sweeps. Every rear take played the applied bass and room layers.

The owner's design on #6227: one `rear/pair` take at the mark, a preview of the seed with no sound
([ADR-0425](0425-the-rear-seed-is-computed-from-the-declared-geometry.md)), then one seat trial of
rear off against the seed at three seats. Its takes are the in-room base, so they play with bass and
room off: a program plays the layers below it, with its own layer and the layers above it off.
`rear/express` stops being a default trial, the behind check is optional, and `--vary` stays for a
seed that misses.

## Decision

1. **One default start, owned by the registry.** `rear/pair` is the first `rear/*` row, and its
   default layout is `tournament_express`: the mark, taken once. Its other layouts, `rear_express`,
   `speaker_mark` and `rear_behind`, follow it. The rear row states no `start`, so the page,
   `jasper-round run --program rear` and the copied prompt start the same plan. Bass is the only row
   with a `start` ([ADR-0429](0429-one-in-room-program-the-room-round-plays-with-bass-and-room-off.md)
   §3). `rear/express` stays a preset and the arm's trial.
2. **Every rear take plays bass and room off.** A program row states the layers every take of it
   clears (`clears`), beside the layers its base also clears (`base_clears`, ADR-0429) and whether
   its branches take clears its own layer (`branches_clear_own`,
   [ADR-0386](0386-a-rear-pair-take-clears-the-rear-layer-at-the-door.md)). The rear row clears the
   room and the bass layers on every take, base and candidate alike. So the pair take plays the raw
   woofers with the rear stage, bass and room cleared, and a seat take plays its candidate's rear
   stage with bass and room off. The door derives the played graph as before (ADR-0370 §2), and
   nothing measurement-only is banked. Each candidate graph's seat set still levels itself
   ([ADR-0423](0423-each-candidate-graphs-summed-set-levels-itself-at-every-spot.md)): its level key
   reads the layers the take plays.
3. **The chosen set is the in-room base.** `rear/seat` names the purposes rear, room and bass, so its
   bank files the rear view, and the room and bass views of each set, from the same takes. The
   in-room preview admits a set whose takes played bass and room cleared, a candidate's included
   ([ADR-0421](0421-bass-has-a-preview-model-the-in-room-preview-adds-the-composed-boost.md)). So
   room and the optional bass are designed on the chosen candidate's set (`--set`), and a cardioid
   build plays no separate in-room round: the in-room program needs only its trial.
4. **The rear-off reference is the base.** A trial's candidates stay `base,<fp>`. A base with no rear
   stage plays its rear output muted, so on a first build the base is the rear-off graph. The rear
   view takes it as the rear-muted reference and reads the late-energy and band figures against it. A
   candidate whose section states `rear_muted: true` still wins, and a base whose applied profile
   does not read is never taken as rear off. Nothing builds a muted copy: on a re-tune, where the base
   plays a rear stage, the agent adds a composed copy with `rear_muted: true` to `--candidates` when it
   wants the rear-off reference.
5. **Copy.** The rear row sets the rear woofer to cut the wall bounce at the listening position, not
   to "reduce sound behind the speaker".

### The default and its count

A first-build cardioid run is one `rear/pair` take at the mark and one `rear/seat` trial of two
graphs at three seats: 1 + 6 = 7 takes and 6 + 6 = 12 sweeps. A pair take plays each woofer alone
twice, then their sum and its companion; a seat take plays one summed sweep. Each graph's first take
also plays its probe: both branches alone for the pair, and one per seat graph. Before this, the page
started 2 pair takes (12 sweeps), and the playbook's trial of five graphs played 15 takes (15 sweeps).
The in-room program then plays its one trial, base against its document at the same three seats: 6
takes.

### What this supersedes

- ADR-0303, line 19: "A trial plays each candidate exactly as the composer resolved it." A rear
  trial plays each candidate as composed less bass and room, as ADR-0370 played a bass trial's
  candidates less room; each take records the `cleared_layers` it played. The in-room trial still
  plays its document's candidate whole.
- ADR-0336, lines 29–31: "Its `rear/seat` row carries `co_purposes: ["room"]`, so the banker runs
  both views on the same takes." Its purposes are rear, room and bass, so the banker runs the rear,
  room and bass views on the same takes.
- ADR-0336, lines 52–54: "The room fit follows the chosen candidate's set through `--set`, and its
  composition uses that candidate as the base. Trial the composed document before apply." The
  in-room document, room and the optional bass, is designed on that set, which played bass and room
  off. The rear choice is applied after its seat trial, since an apply needs no further trial
  (ADR-0425 §6), so the in-room document's base is the applied tune, and the in-room trial follows.
- ADR-0366 §6, line 144: "`rear/pair` | rear · front and rear woofer on one clock | **Preset.**
  Layouts `rear_express`, `speaker_mark`, `rear_behind`". Its layouts are `tournament_express` (its
  default), `rear_express`, `speaker_mark` and `rear_behind`.
- ADR-0370 §1, line 40: "every other row clears nothing, so speaker, rear, room and reference runs
  play as before." ADR-0386 amended it for the rear row's branches take, and ADR-0429 for the room
  row's base. The rear row clears bass and room on every take.
- ADR-0420, lines 48–50: "Today a rear take still plays the applied bass and room layers, and a
  speaker trial plays its whole candidate, until their programs clear those layers (#6227 A2, B3)." A
  rear take plays them cleared. A speaker trial still plays its whole candidate.
- ADR-0425 §6, lines 36–37: "A trial's muted reference is an explicit copy of the section with
  `rear_muted: true` (step B3 builds it in)." The rear-off reference is a base with no rear stage
  (§4). A muted copy is the agent's choice on a re-tune, and nothing builds it.
- ADR-0413 §3, lines 27–28, its premise: "Only a bass run's takes clear the room layer, and no bass
  preset takes a timing take, so a bass run's probe clears it too." ADR-0429 restated it, and
  ADR-0431 removed the bass run. Every rear take now clears the room layer, and none plays at a run's
  fader: a pair take plays at its own probes' levels
  ([ADR-0407](0407-a-branch-played-alone-plays-at-its-own-probes-level.md)), and each summed rear set
  levels itself (ADR-0423). So the room-off rise still has no input.

## Consequences

- **Hearing:** no level, probe, clamp or door code changes; only the layers a rear take plays. Each
  seat graph's first take probes the graph it plays, now without bass and room, and lands at
  74 ± 2 dB (ADR-0423). A pair take's graph no longer carries the applied bass boost or room set, so
  it plays closer to the drivers graph its probes read: each branch lands nearer its probe's
  80 ± 2 dB, and no branch carries a bass reserve, so each branch's ceiling under the fader is its
  driver cap (ADR-0407 §1). `volume_limit` 0.0, the graph doors, the `set_volume_db` clamp, the
  85 dB commissioning stop and the declared driver caps do not change.
- A rear seat round now files a bass view per set too, so it counts for the bass program as well as
  for rear and room (ADR-0429 §2).
- On a first build, a seat trial's band levels and late energy read against the base. Before, they
  had no rear-muted reference unless the trial played a muted copy.
- `jasper-round run --program rear` with a layout or candidates that `rear/pair` does not take now
  refuses: name `rear/express`.
- Rejected:
  - A muted-copy builder that composes the base with `rear_muted: true`. On a first build the base
    already plays rear off, and on a re-tune `--candidates` adds a copy the agent composes.
  - Routing a bare program name through `first_plan`. It would make `--program bass` run the in-room
    round, against [ADR-0431](0431-the-bass-level-ladder-is-retired.md) §3.
