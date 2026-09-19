# ADR-0328: The measurement band is the audio band; a woofer's low edge is not a damage limit

- **Date:** 2026-09-18
- **Status:** Accepted
- **Extends:** ADR-0227 (excitation ceilings), ADR-0101 (what counts as safety)

## Context

`resolve_driver_excitation_ceilings` turned each driver's declared
`hard_excitation_band_hz` / `measurement_band_hz` into the band a measurement
may excite, and every consumer followed it: the summed sweep started at the
lowest declared edge, the room median inherited that start, and program
admission refused a segment below a low-frequency driver's declared floor
unless a protection high-pass was declared. On jts3 the woofers' declared band
is 30–4000 Hz — copied from the datasheet's "usable frequency range", whose own
provenance line says it is "not an installation-specific safe-drive
guarantee". The consequences on 2026-09-18: every sweep started at 30 Hz, the
room program could not produce a median at all until #5378, and readings near
the sweep's own edge were the noisiest in every round.

The owner ruled (2026-09-18): a speaker asked to play a note below its range
at measurement level simply does not play it; nothing is damaged; every
measurement tool sweeps 20 Hz–20 kHz; standardise on that and remove the
machinery that says otherwise.

The physics agrees for the measurement path. A low-frequency driver's damage
mechanisms are thermal power and over-excursion, and both are set by LEVEL and
DURATION, which the level ceilings, the sweep-duration limits and the
commissioning SPL stop already bound. At the leveled measurement level
(about 75 dB SPL at 1 m) cone excursion at 20 Hz is a small fraction of any
woofer's linear travel, sealed or ported. The same is not true of a
high-frequency driver: a compression driver or dome excited below its
high-pass has almost no travel to spend, which is why its floor and its
required protection filter exist.

## Decision

1. For a low-frequency role (`LOW_FREQUENCY_ROLES`) the resolved excitation
   band starts at `MIN_DRIVER_TEST_FREQUENCY_HZ` (20 Hz), whatever low edge
   the driver declares; for the role that owns the top of the speaker the
   resolved band ends at the audio band's top (20 kHz). Interior edges — a
   high-frequency driver's floor, a low-frequency driver's ceiling — stay as
   declared, and so do every level ceiling, every duration limit, every
   required protection filter, and the SPL stop (non-negotiables 1 and 2 are
   about those).
2. One place decides it: `resolve_driver_excitation_ceilings`. The summed
   sweep band, the per-driver sweep band and program admission read the
   resolved band, so none of them carries a rule of its own about the low
   edge. The declared low edge remains in the profile as what it is — the
   driver's response limit — and the analysis may still read it as a
   validity hint.
3. The bass program is not a measurement-level sweep: it steps the level up on
   purpose, reads the woofer's declared floor itself, and keeps doing so.

## Consequences

- Every summed sweep covers 20 Hz–20 kHz on every speaker, so the room, rear
  and bass readings share one range and the room median always reaches the
  room floor.
- A refusal that said "below the woofer's floor" can no longer happen on the
  measurement path; the refusals that remain there are level, duration, a
  missing protection filter, and the SPL stop.
- Rejected: forcing the band in the sweep composer or weakening the admission
  gate (two more places that would each know about the low edge); asking every
  owner to declare 20 Hz (the datasheet number is a response figure and will
  keep being pasted).
- If a low-frequency driver is ever damaged by a measurement-level sweep, the
  incident goes to the level or duration limit that allowed it, not back to a
  frequency floor.
