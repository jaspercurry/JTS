# 0218 — A durable save that moves only a trim writes in place

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
- The step is bounded: `HEADROOM_TRIM_MAX_DB` on the stereo path,
  `MAX_PROGRAM_HEADROOM_DB` on the active path. `devices.volume_limit`
  remains the hard ceiling and is untouched by any of this.
- The live-draft path is unaffected: it freezes the output trim, so a draft
  never moved a Gain to begin with.
