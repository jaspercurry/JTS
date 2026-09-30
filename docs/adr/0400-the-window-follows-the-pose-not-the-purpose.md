# ADR-0400: The window follows the pose, not the purpose

- **Date:** 2026-09-30
- **Status:** Accepted. Amends
  [ADR-0366](0366-one-pose-model-a-level-found-at-the-pose-and-a-band-stated-from-it.md) §3: a
  take whose purpose is the room (room, bass, rear) is no longer read ungated for that purpose.

## Context

ADR-0366 §3 read a take ungated at a driver within the near-field distance, or when its purpose
was the room, bass or rear. So one place could bank a gated curve (a speaker take) or an ungated
one (a room take), and each reader took the curve it found. C3 PR 1
([#6105](https://github.com/jaspercurry/JTS/pull/6105)) made every take that the gate reads also
bank its ungated reading (ADR-0383 §2). A purpose then no longer needs to pick what is banked
([#5737](https://github.com/jaspercurry/JTS/issues/5737) C3).

## Decision

1. **The exemption.** A take is read ungated only at a seat pose, or at a driver within the
   near-field distance (100 mm). `gate_exemption(kind, driver, distance_m)` takes no purpose.
   Wherever the gate applies, the take banks both windows. The capture host decides the
   exemption for every phase from the pose on the take's record, the same pose its bands read.
   So no phase is exempt by its name.
2. **The readers.** Each program names its window. Room, bass and rear readers read `ungated`,
   and speaker readers read `gated`. A speaker role whose gate found no window banks only
   `ungated`, so a speaker reader reads nothing for it. The views that draw a take's banked
   curves (the measurements page, `frequency`, `candidates`) show both windows, each labelled
   by its window. The impulse views read the kept impulse through the window they name. The
   trusted band is banked on each curve: the gated curve gets the gate floor, the ungated curve
   none. So no band reads a purpose.
3. **Deleted.** `SEAT_EXEMPT`, `room_sweep` and its plans flag, `run_manifest.room_sets`, the
   purpose-keyed sweep band (under ADR-0328, every summed sweep is already 20 Hz-20 kHz), the
   gated-overlay decode and the `GROUP_PHASES` exemption. A speaker round then holds no room
   evidence. Room evidence comes from `room/seat` or `rear/seat`.

## Consequences

- A bass, rear or room take at a bearing or behind pose banks both windows. Its readers read
  the ungated one, as before, and the page also draws its gated one. A seat take and a
  near-field take bank one window, `ungated`.
- `speaker/mark` walks only its per-driver stops, so it takes fewer captures, and its round
  packet has no room section.
- A take banked before the band moved onto its curves refuses `take_curves_not_banked`
  (`field: trusted_band`) where its band is read
  ([#2902](https://github.com/jaspercurry/JTS/issues/2902)).
- Rejected: the purpose picks the graded window (the take window reads its purposes). It needs
  fewer reader edits, but it puts the purpose back into the window rule.
