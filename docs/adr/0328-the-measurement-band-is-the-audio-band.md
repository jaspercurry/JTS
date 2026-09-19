# ADR-0328: The measurement band is the audio band; the only protective band edge is a high-frequency driver's floor

- **Date:** 2026-09-18
- **Status:** Accepted
- **Extends:** ADR-0227 (excitation ceilings), ADR-0101 (what counts as safety)

## Context

`resolve_driver_excitation_ceilings` turned each driver's declared
`hard_excitation_band_hz` / `measurement_band_hz` into the band a measurement
may excite, and every consumer followed it: the summed sweep started at the
lowest declared edge, the room median inherited that start, and program
admission refused a segment below a low-frequency driver's declared floor
unless a protection high-pass was declared — and refused a summed sweep that
ran above a woofer's band unless that woofer proved a 24 dB/octave low-pass.
On jts3 (2026-09-18) the woofers' declared band is 30–4000 Hz, copied from the
datasheet's "usable frequency range", whose own provenance line says it is
"not an installation-specific safe-drive guarantee". The consequences: every
sweep started at 30 Hz; the room program could not produce a median at all
until #5378; and the `speaker` program could not run at any level, because the
cardioid's rear woofer carries no low-pass in the timing graph and the summed
sweep runs to 20 kHz (#5383).

The owner ruled (2026-09-18): a speaker asked to play a note outside its range
at measurement level simply does not play it; nothing is damaged; every
measurement tool sweeps 20 Hz–20 kHz; standardise on that and remove the
machinery that says otherwise.

The physics agrees for the measurement path. A low-frequency driver's damage
mechanisms are thermal power and over-excursion, and both are set by LEVEL and
DURATION, which the level ceilings, the sweep-duration limits and the
commissioning SPL stop already bound. At the leveled measurement level (about
75 dB SPL at 1 m) cone excursion at 20 Hz is a small fraction of any woofer's
linear travel, sealed or ported, and a woofer fed 4–20 kHz is fed almost
nothing it can turn into heat or travel. The one band edge that is different
is a high-frequency driver's floor: a compression driver or dome excited below
its high-pass has almost no travel to spend, which is why its floor and its
required protection filter exist.

## Decision

1. Full-speaker (summed) sweeps cover the audio band, 20 Hz–20 kHz, on every
   speaker. The resolver is the one owner of the band: the lower edge is
   `MIN_DRIVER_TEST_FREQUENCY_HZ` for every role except a high-frequency role,
   which keeps its floor rule unchanged; the upper edge is the audio band's
   top for the role that owns the top of the speaker (a high-frequency role,
   or the single driver of a 1-way) and the declared edge for every other
   role.
2. The only protective band edge is a high-frequency driver's floor. Program
   admission keeps: every level ceiling, every duration limit, every
   protection filter the owner DECLARED as required (proved in the graph), the
   high-frequency floor, and the terminal-mute check. It no longer refuses a
   summed segment for starting below a low-frequency driver's declared edge,
   and it no longer demands that a low-frequency driver prove a low-pass before
   a full-band sweep may play.
3. Per-driver MEASURE sweeps keep their analysis band
   (`MEASURE_SWEEP_F_LO_HZ` upward, inside each driver's own band): that band
   is where the gated crossover fit reads, it spends the sweep's duration
   where the fit needs the signal, and it protects nothing — it is an analysis
   choice, not a gate.
4. The bass program is not a measurement-level sweep: it steps the level up on
   purpose, reads the woofer's declared floor itself, and keeps doing so.
5. The hearing clamps (`volume_limit`, `set_volume_db`, the SPL stop) are
   untouched.

## Consequences

- Every summed sweep covers 20 Hz–20 kHz on every layout, so the room, rear
  and bass readings share one range and the room median always reaches the
  room floor. The cost is named: the same sweep duration over more octaves is
  about 1.5 dB less signal per octave on the summed sweep.
- The `speaker` program can run on the cardioid: its rear woofer needs no
  low-pass proof to receive a measurement-level full-band sweep.
- Summed `program_id`s change, so a summed round banked before this ADR
  cannot be replayed bit-for-bit by a tool that re-composes its program;
  per-driver MEASURE programs are unchanged.
- The refusals that remain on the measurement path are level, duration, a
  missing DECLARED protection filter, a high-frequency segment below its
  floor, and the SPL stop; a refused segment names which one it hit.
- Rejected: forcing the band in the sweep composer or special-casing the
  admission gate per layout (more places that would each know about band
  edges); asking every owner to declare 20 Hz (the datasheet number is a
  response figure and will keep being pasted).
- If a driver is ever damaged by a measurement-level sweep, the incident goes
  to the level or duration limit that allowed it, not back to a frequency
  edge.
