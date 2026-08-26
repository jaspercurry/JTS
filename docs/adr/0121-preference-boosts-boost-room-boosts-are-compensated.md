# ADR-0121: Preference boosts boost; room-correction boosts are headroom-compensated

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Two different layers can add gain to the same CamillaDSP graph: the household's
preference EQ (`/eq/`) and room correction's designed PEQs. They were once
treated the same way, with an automatic preamp derived from the peak boost
inserted for both.

That is wrong for preference EQ. A consumer EQ that turns the whole mix down
when you raise the bass does not behave the way anyone expects, and the
attenuation is invisible — the user hears "quieter", not "protected". It is
also unnecessary as a safety measure: `devices.volume_limit: 0.0` is the hard
ceiling and is a non-negotiable.

It is right for room correction. The assertive strategy (`cuts_only=False`) can
emit boosts the household never asked for and cannot see, so those cannot be
left to clip.

## Decision

**A preference boost applies at unity.** A +N dB band raises that band and
leaves the rest of the spectrum alone; the generated config inserts no
automatic preamp for preference gain. The only global attenuation on the
preference layer is the opt-in output trim (`headroom_trim_db` plus
match-loudness compensation when enabled), which is 0 by default. At high
volume a large boost clips at 0 dBFS rather than ducking the mix.
`estimate_headroom_db` survives as the peak-boost *metric* doctor and `/state`
report; it drives nothing.

**Room-correction boosts are compensated automatically.**
`jasper.camilla_stereo_prefix.build_stereo_prefix` derives a `room_headroom`
preamp from the worst-case additive room boost — the sum of positive room-PEQ
gains, an upper bound on the combined peak — and attenuates the whole signal by
it, so a corrected room boost can never exceed unity. Cuts-only correction (the
default) emits no `room_headroom` at all, leaving the config byte-identical.

These are deliberately separate mechanisms. `output_trim_db` compensates only
the preference layer and is skipped on a flat profile, so it would not protect
room boosts on a household that has set no preference EQ.

## Consequences

- `/eq/` behaves like the consumer EQ it is; the layer ordering (room chain,
  then preference, then the `flat` terminator) stays legible.
- Active-speaker baselines follow the same policy: preference bands ride at
  unity ahead of the crossover split; explicit output trim and match-loudness
  attenuation still fold into `active_baseline_headroom`.
- A household that boosts hard at high volume can clip. That is audible,
  attributable, and under their control — unlike a silent global duck.
- The `volume_limit: 0.0` ceiling does the safety work, so removing the old
  preamp cannot raise the output ceiling.
