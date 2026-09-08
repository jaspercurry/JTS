# ADR-0260: Poses are flexible and categorized, and bass extension has no nearfield rung

- **Date:** 2026-09-08
- **Status:** Accepted. Supersedes (partial)
  [ADR-0257](0257-bass-extension-resumes-rebased-on-wired-capture-and-validated-in-room-below-the-ceiling.md) §3,
  the nearfield fit as the protection basis. Amends
  [ADR-0256](0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md) §4,
  the six-position default, for the seat program.
- Refs: `seat-tuning-program/PLAN.md` §1 (the Bank / Room Matching table) on
  branch `claude/loudspeaker-tuning-architecture-iephfa`; Bank, AES 134 (2013);
  [ADR-0192](0192-the-campaign-is-the-validation.md) §3.

## Context

A pose is a bearing at a pinned distance. `ProgramPose`
(`jasper/active_speaker/measurement_programs.py:39`) carries `azimuth_deg`,
`elevation_deg` and `repeats`, no kind and no distance; the registry header
(`:23`) says "No level and no mark distance", and every pose is banked at
`MARK_DISTANCE_M = 1.0` (`crossover_v2/spatial.py:1393`), as
`jasper/cli/round_views/close_reference.py:24` notes. The close reference
exists as a view, not a pose kind: it compares a close round (`--close-m`)
with the far round and recommends a distance (`--distance`, via
`active_speaker/branch_chain.recommended_distance`), and
`docs/tuning-methodology.md` §6 names it beside `classify-features`,
`gate-sweep` and `distortion`. ADR-0192 §3 parked nearfield for the speaker
stage (gating goes lower instead); ADR-0256 §4 reaffirmed a small cloud at the
room session's six-position default.

The bass plan's protection basis is a nearfield fit at the dust cap
(`docs/HANDOFF-bass-extension-plan.md` §5.3 at :505-512, §6.1 at :530-542):
the sealed adapter's `fit_plant` (`jasper/bass_extension/adapters/sealed.py:136`,
through `alignment.second_order_highpass_db`) fits a second-order high-pass to
a magnitude curve the deleted relay was to capture at the cone, and the
adapter contract names a `WOOFER_NEARFIELD` position (plan `:542`). Room gain
is neither modeled nor measured, and ADR-0257 §3 kept that fit as the basis,
with room gain defined as in-room response minus the nearfield model.

The sources go the other way. Bank skips nearfield explicitly, corrects speaker
and room jointly below the transition on responses measured through stage one,
and targets a 4th-order high-pass at 30 Hz in-room. Dutch & Dutch Room Matching
measures a cube around the head, centre plus six face centres about 30 cm out,
averages it, and corrects low frequencies only (`PLAN.md` §1).

## Decision

Owner ruling, 2026-09-08:

1. **A take carries its kind, distance and window.** Kind ∈ {bearing, seat,
   close}. Distance is metres per take: the `MARK_DISTANCE_M` pin retires as a
   constant and becomes the bearing kind's default. The window (gated or
   ungated) is an analysis choice recorded on the analysis, not on the
   capture. Every banked take is categorized so the LLM can weigh it: a gated
   bearing at about 1 m answers speaker questions above the trusted floor; a
   close take at about 0.3 m is the room-suppressed reference; the ungated
   seat cube answers speaker-plus-room questions. **No pose is forbidden;
   none is required.**
2. **The seat cube is a program:** the head position plus six face centres
   about 30 cm out, seven takes. This amends ADR-0256 §4's six-position
   default for the seat program only; D7's spirit, a small cloud that does not
   chase more positions, stands.
3. **Bass extension has no nearfield rung.** The family is fitted on the
   seat-cube median, through the applied tune, below the ceiling (ADR-0256),
   to an extended-corner target per rung, with Bank's in-room target shape as
   the precedent. The protection basis is declared plant facts (the adapters
   as parameter models), the in-room distortion-versus-level ladder
   (`jasper/audio_measurement/distortion.py`), and the limiter-evidence
   protocol. This supersedes ADR-0257 §3's "the nearfield fit stays as the
   protection basis"; §3's in-room validation and one headroom budget stand.
   Room gain is published when a close reference is taken and estimated
   against the declared plant otherwise.
4. **Close-mic stays optional**, as `round-views close-reference`. The
   nearfield mic-ceiling spike (`docs/bass-extension-waves/wave-0-hardware-spikes.md:50`;
   plan `:1577`) and any `WOOFER_NEARFIELD`-style required position retire.

## Consequences

- The pose vocabulary row and the seat program are the next wave's:
  `ProgramPose` gains kind and distance, the seat kind carries an offset from
  the head, and `seat/cube` and `close/spot` join `_PROGRAMS`. The human still
  moves the microphone through the position-ready walk (ADR-0255).
- The bass fit view consumes the median. `fit_plant` is re-pointed at the
  median, where it yields the effective in-situ corner rather than the cone's.
- The protection ladder is a code-owned program: stepped-level sweeps at the
  seat, distortion-versus-level per rung, evidence banked per rung. A failing
  rung is inadmissible at that level whoever judges; hardware damage is a hard
  stop (AGENTS.md non-negotiable 2), not a disclosure.
- The bass plan's §5.3 and §6.1 fit text is stale from this date and is
  corrected in the wave that lands the bass fit; the wave-4, wave-6 and
  commissioning-UX docs retire under ADR-0259 §3.
- Gives up: a room-free corner and Q for pole placement, in exchange for a fit
  that includes the room's own gain, which is the gain the extension is meant
  to use. Rejected: a required nearfield take as the bass program's first rung
  (a required pose, measuring a quantity the in-room target does not use).
