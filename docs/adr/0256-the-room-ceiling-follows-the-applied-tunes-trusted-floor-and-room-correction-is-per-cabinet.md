# ADR-0256: The room ceiling follows the applied tune's trusted floor, and room correction is per cabinet

- **Date:** 2026-09-08
- **Status:** Accepted. §4's six-position default is amended for the seat
  program by
  [ADR-0260](0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md).
- Refs: Bank, "Combined quasi-anechoic and in-room equalization of
  loudspeaker responses", AES 134 (2013);
  `docs/room-correction-regime-plan.md` D1, D2, D6, D7.

## Context

Bank's method corrects the loudspeaker's direct sound at high resolution above
the frequency where the reflection gate stops being trustworthy, and corrects
loudspeaker and room jointly below a transition, on responses measured
*through* stage one from several listening positions. The transition is set
by the gate actually achieved, not chosen a priori, and in-room EQ never
reaches above it because it brightens the direct sound.

JTS already has both stages and the layering between them. The speaker stage
is `jasper/active_speaker/`, with two published gate floors, `1/T` and `2.5/T`
(`jasper/audio_measurement/gating.py:143-212`). The room stage is
`jasper/correction/`: a cloud around one seat, six positions by default
(`jasper/correction/session.py:77`), power-mean averaged
(`jasper/audio_measurement/analysis.py:251-267`, called from
`session.py:1612`), cuts-only bells, an accept-or-revert loop.
`docs/measurement-loop-doctrine.md` §1a measures the room through the applied
speaker tune — Bank's stage two. What is missing is the transition:
`ROOM_BOUNDARY_DEFAULT_HZ = 350.0` (`jasper/audio_measurement/room_boundary.py:115`),
clamped to `[ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ]` = [250, 500]
(`:121-122`), and the module docstring says whether the ceiling should follow
the measured trusted floor "is a room-layer policy question, deliberately not
answered here" (`:78-80`). Its roadmap (`:97-100`) and the regime plan's D1
reach a per-room ceiling through a Schroeder-frequency estimator, which needs
a room volume nothing measures; D1 itself says so.

The input exists. The trusted floor rides the applied candidate as
`exclusion_evidence` (`jasper/active_speaker/measured_crossover_candidate.py:393-412`)
and is persisted whole by `persist_applied_baseline_profile`
(`jasper/active_speaker/baseline_profile.py:3562`); nothing in
`jasper/correction/` reads it. Two more shapes are fixed by this ruling: the
spatial σ is a per-frequency cut-depth cap (`jasper/correction/variance_cap.py`),
never a trend; and the PEQ design band has a hard edge (`band_mask` at
`jasper/audio_measurement/peq.py:148`). And the active emitter applies one
room PEQ set to both channels (`jasper/active_speaker/camilla_yaml.py:1790-1809`,
wired to `channels: [0, 1]` at `:1927-1931`), so a second cabinet on the same
DAC would inherit the first's room.

## Decision

Owner ruling, 2026-09-08:

1. **The ceiling is derived, per applied tune.** The room layer's upper band
   edge is the applied candidate's disclosed trusted floor, clamped to
   `[ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ]`. When no applied floor is
   readable the layer falls back to `ROOM_BOUNDARY_DEFAULT_HZ` and discloses
   the fallback. This answers `room_boundary.py`'s open question and
   supersedes regime-plan D1's estimator: the Schroeder path is not built. The
   clamp bounds and the invariant that the ceiling never sits below the gated
   spec's lower edge (`room_boundary.py:53`) stand. The 350–357 Hz gap the
   docstring discloses on a 7 ms room closes by construction: the ceiling is
   the floor.
2. **Trend, confidence, taper.** The cloud's common trend is the median across
   positions; the spatial σ stays the confidence signal (the depth cap). The
   correction target tapers to flat over about one-third octave below the
   ceiling instead of ending at a hard edge, so the hand-off to the
   direct-sound stage is continuous.
3. **Room correction is per cabinet.** One PEQ set per output side when the
   layout is stereo (`SIDES_BY_LAYOUT`; ADR-0258). Today's mono cabinet is
   unchanged.
4. **Standing.** D6 (no room FIR) and D7 (a small cloud) are reaffirmed, at
   the session's six-position default. D2's residual tier above the ceiling is deferred to last;
   nothing corrects above the ceiling until then.

## Consequences

- The estimator is not built; the regime plan's per-room transition is
  satisfied by rule 1 without a room volume. `room_boundary.py`'s roadmap
  paragraph and `jasper/correction/strategy.py:141-147` (safe/balanced/
  assertive bound to MIN/DEFAULT/MAX at import time; its own comment says a
  per-room ceiling "requires changing this composition") are stale from this
  date and change in the wave that lands the reader.
- Later waves, not this ADR: a reader of the applied profile's floor (in
  `jasper/correction/`, or in `audio_measurement` per ADR-0231 §5), the taper
  in the target shaping, and the median in the averaging step — the shared
  math already computes one beside the power mean
  (`jasper/audio_measurement/spatial_combine.py:1527-1528`). The per-side
  emitter and the per-side round-trip reader belong to ADR-0258.
- The ceiling moves when the tune moves: a re-commissioned speaker with a
  shorter gate raises the room ceiling, and a room session designed against
  the old ceiling is disclosed-stale (ADR-0101), never parked.
- Gives up: a physically motivated transition (Schroeder) in favour of the one
  the measurement earned. Rejected: choosing the ceiling by hand per room —
  the gate already measured it.
