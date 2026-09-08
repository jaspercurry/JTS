# ADR-0258: The topology vocabulary is sides × driver roles, and cardioid is a variant of the bass role

- **Date:** 2026-09-08
- **Status:** Accepted

## Context

The software must work unchanged for a passive 1-way with no active crossover,
a 2-way, a 3-way, and a 3-way whose third channel is an active cardioid
bass/mid; and one DAC may carry two cabinets. Today jts3 is a Pi 5, an
8-channel DAC HAT and one mono 2-way cabinet
(`jasper/active_speaker/presets/*.json`, `layout: mono`). Next is a second
identical cabinet on the same DAC, then the 3-way with the cardioid channel: a
duplicate feed of the bass role's signal restricted to a sub-band, with its
own delay and polarity.

The profile vocabulary already has both axes: `SUPPORTED_LAYOUTS = {"mono",
"stereo"}` and `SIDES_BY_LAYOUT` (`jasper/active_speaker/profile.py:80-81`),
`required_driver_roles` for one, two and three ways and `lowest_driver_role`
(`:108-136`); `runtime_contract.py:553-564` classifies stereo 2-way and
3-way. But the rows hang off one axis or neither: linearization and polarity
are keyed by role only; room PEQs are one set applied to both channels
(`jasper/active_speaker/camilla_yaml.py:1790-1809`, `:1927-1931`); no capture
graph solos one side. The flat (passive) emitter does have a per-channel room
axis, `room_peqs_right`, built for the multiroom leader bake
(`jasper/sound/camilla_yaml.py:367-375`) — a side axis by another name, on
the other graph.

## Decision

Owner ruling, 2026-09-08:

1. **Two keys, always.** Every tuning, room and bass row keys on output side
   and driver role. Never on `way_count == 2`, `layout == "mono"`, or a named
   driver. A mono layout is one side; a passive 1-way is one role with no
   crossover; a 3-way is three roles.
2. **Cardioid is a variant of the bass role.** The same source signal,
   restricted to a sub-band, with its own delay, polarity and level, emitted
   as another output of that role. It is not a fourth way and gets no role
   name of its own. Its design — band, delay model, protection — is deferred;
   this ADR reserves the vocabulary so per-side and per-role work in later
   waves does not preclude it.
3. **Facts attach to the axis that varies them.** Per-cabinet facts attach to
   a side: the room PEQ set (ADR-0256 §3), the level trim, the bass fit check
   (ADR-0257 §3). Per-model facts attach to a role: linearization filters,
   crossover, the bass family shape. A per-model fact is measured once and
   applies to every side that carries the role.

## Consequences

- Grows a side axis later, not here: the active emitter's room PEQ stage
  (`jasper/active_speaker/camilla_yaml.py`), the round-trip reader that
  recovers the applied graph, a side-solo capture graph under
  `docs/measurement-loop-doctrine.md` §1a, and the bass owner's channel set
  (`bass_management_corner_hz()` names one bass system; a stereo pair with no
  sub is one role on two sides).
- `jasper/sound/camilla_yaml.py`'s `room_peqs_right` and the active emitter's
  per-side set are one concern; when the active side axis lands they converge
  — never a third.
- Grows a role variant later: an emitter output of the bass role carrying its
  own band, delay, polarity and level; the linearization and protection rows
  stay keyed to the bass role.
- Rejected: `way_count == 4` for the cardioid (it duplicates a signal, it does
  not split the spectrum); a `cardioid` layout (layout is sides, not drivers);
  per-driver names as keys (a preset swap would orphan every row).
