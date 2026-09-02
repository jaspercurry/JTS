# 0219 — A durable save that moves only a trim writes in place

Date: 2026-09-02. Status: accepted. Supersedes exactly two things in
[ADR-0216](0216-curve-slots-are-fixed-so-a-quiet-save-takes-the-live-edit-path.md):
the fourth item of its "Four cases are deliberately left ducking" bullet —
the moved `Gain` — and the bullet after it, "The Gain hold-back has a cost
worth naming". The rest of ADR-0216 stands, including its other three
hold-backs: a load of a different file, a controller with no raw-config API,
and an in-place rollback.

## Decision

`jasper.sound.live_edit.plan_live_edit` is structural only. A durable save
whose only difference is a filter's `parameters` writes in place through
`set_active_config_raw(duck=False)` like any other parameter change,
including when the filter is a `Gain`.

## Why

With match loudness on, every profile carries its own compensating
`sound_preamp`, so a Saved-tab or profile-to-profile A/B moved that Gain and
ducked — and that A/B is precisely the comparison the listener is making. A
rule that fades exactly the audition it was meant to protect is the wrong
rule; the fader bracket is for a graph CamillaDSP rebuilds, which is what
the structural comparison already names.

## Consequences

- A `/sound` headroom-trim drag, a match-loudness toggle, and an active-path
  reconcile that changes `active_baseline_headroom` now land as an instant
  level step rather than a bracketed fade. That is the accepted cost.
- The step is bounded per parameter, not in aggregate. `HEADROOM_TRIM_MAX_DB`
  (12 dB) caps the manual trim, but the match-loudness compensation added on
  top of it has no cap of its own: `loudness_compensation_db` is the mean
  power of `response_preview`, which sums every active filter's response, and
  each contributing band is clamped individually — `ADVANCED_GAIN_LIMIT_DB`
  and `SIMPLE_EQ_LIMIT_DB`, both 12 dB — never the total. One boosted band
  caps a save near 24 dB (12 trim + 12 compensation); several overlapping
  boosted bands push it past that, since nothing bounds the sum.
  `devices.volume_limit` remains the hard ceiling and is untouched by any of
  this. The active-path step, `MAX_PROGRAM_HEADROOM_DB` (40 dB), is worth
  reading for what it actually is: `sound_reconcile` moving
  `active_baseline_headroom` in the background, not the listener gesture the
  Why leans on.
- The live-draft path is unaffected in the steady state: it freezes the
  output trim to the saved profile's own value, so a draft never moves a
  Gain to begin with. That buys "not draft-derived", not "always equal to
  what's running" — when the running graph disagrees with the saved trim (a
  settings re-apply that raised, leaving its `warning` payload with the old
  graph still loaded, or an audition graph whose own compensation differs
  from the saved profile's), the next band drag diffs `sound_preamp` too,
  and it is now a `parameters`-only change like the dragged band, so it
  writes in place un-ducked — where the pre-#3678 Gain rule forced a ducked
  swap.
- This decision still owes the hardware check ADR-0216 named for its own
  half: on the Saved tab with match loudness on, an A/B between two profiles
  with different loudness compensation should be silent, and a `/sound`
  headroom drag should land as a step. The journal tell is `event=sound.apply`
  with no `-41.67 dB` fader excursion beside it for the A/B.
