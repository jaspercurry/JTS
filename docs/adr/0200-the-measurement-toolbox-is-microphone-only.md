# ADR-0200: The measurement toolbox is microphone-only

- **Date:** 2026-08-31
- **Status:** Accepted

## Context

The independent methodology review of the 2026-08-31 flat campaign
([`historical/flat-campaign-2026-08-31.md`](../historical/flat-campaign-2026-08-31.md))
recommended an electrical impedance sweep as the first measurement of any
tuning session: it distinguishes sealed from ported/passive-radiator
essentially for free, yields the box tuning frequency, and thereby derives
the protective high-pass before any acoustic sweep plays. All true — and all
requiring electrical test hardware (a DATS-class jig or sense-resistor rig)
beyond the program's capture surfaces, which are a UMIK-class USB measurement
microphone and the phone relay. The repo already treats impedance as external
data: `bass_extension/profile.py` models it as an *import*
(`impedance_import` — source, fc, Q, agreement), and the 2026-07-24
bass-extension deep research frames the in-house parameter extraction as
deliberately "magnitude-only, no-phase, no-impedance."

## Decision

Owner ruling, 2026-08-31, in chat: *"for impedance, we're not going to
measure that — that needs additional hardware beyond a microphone. So that
is not going to happen."*

The measurement toolbox is **microphone-only**. No electrical impedance
measurement path is built, now or as a later wave. Facts an impedance sweep
would supply — enclosure alignment, box tuning, driver resonance — enter the
system only as:

1. **declarations** (presets, the driver research intake), which win when
   manufacturer-sourced;
2. **externally measured data** through the existing `impedance_import`
   slot, disclosed with its source; or
3. **mic-derived methods** (the near-field extraction lane, currently
   parked with the rest of bass extension per ADR-0192), each disclosing
   its own conditioning.

## Consequences

Easier: one instrument class to maintain, no new hardware dependency, no
second calibration story. Harder: sealed-vs-ported and box tuning cannot be
independently verified in-house, so LF-alignment claims ride declarations
and must say so — the methodology doc's honesty guidance owns that
disclosure. Rejected alternative worth remembering: a cheap DATS-style jig
was considered and declined; if a future speaker's LF work ever makes
declaration-quality data unobtainable, that revisit is a new ADR
superseding this one, not a quiet exception.
