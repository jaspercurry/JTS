# ADR-0310: Leveling converges on two consecutive in-band readings

- **Date:** 2026-09-12
- **Status:** Accepted
- **Context:** On jts3 the first session-level attempt read 74.66 and
  75.29 dB SPL at one fader. Both readings sat inside the ±1.0 dB band
  around the 75 dB target, yet the verb refused as `spl_level_unsettled`:
  the pair differed by 0.63 dB, beyond the separate 0.5 dB agreement gate
  of [ADR-0308](0308-the-leveling-verb-levels-with-the-measurement-sweep.md).
  The room had not changed; the gate was tighter than the band it sat in.
- **Decision:** Ratified by the owner's standing rule that reasonably small
  differences must not refuse a session. The loop converges when two
  consecutive readings at one fader both fall within `tolerance_db` of the
  target, and banks their mean as `leveled_db_spl`. The tolerance band
  bounds the pair; the separate agreement gate and its
  `JASPER_SEAT_LEVEL_SETTLED_AGREE_DB` knob are deleted. This amends the
  agreement clause of ADR-0308.
- **Consequences:** The banked level can sit up to `tolerance_db` from the
  target; the 2 dB / 6 dB drift margins on later takes already absorb that.
  `spl_level_unsettled` still names a reading budget exhausted after
  unsettled readings.
