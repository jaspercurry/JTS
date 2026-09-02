# 0218 — Curve slots are fixed, so a quiet save takes the live-edit path

Date: 2026-09-02. Status: accepted. Supersedes one bullet of
[ADR-0211](0211-a-live-eq-edit-ducks-only-when-camilladsp-rebuilds.md)'s
Consequences — the one starting "A curve preset change still ducks,
honestly" — for both halves of that bullet's claim. The rest of ADR-0211
(the plan itself, `set_active_config_raw` as the one write vehicle, the
fader-trace verdict) stands unchanged.

## Decision

Every curve preset now declares the same two frame slots,
`sound_curve_bass` and `sound_curve_tilt` (`#3527`), Flat at 0 dB as their
identity. A preset change no longer touches `pipeline.names`; it is a
parameter write inside a graph whose shape does not vary, so
`jasper.sound.live_edit.plan_live_edit` already calls it quiet under
ADR-0211's existing rule. Fixed curve slots in the frame are the "separate
decision" ADR-0211 named — this ADR is that decision, made.

That alone did not reach the Saved and Off tabs: `load_profile_config`'s
durable write went through `cam.set_config_file_path` unconditionally,
which ducks regardless of whether the graph it loads changes shape. `#3637`
closes that gap. The durable write now asks `plan_live_edit_for` — the same
comparison the live-edit path asks — whenever it is rewriting the file
CamillaDSP already runs; when the answer is in-place, it writes through
`set_active_config_raw(duck=False)` instead of the file loader. A durable
save that changes no structure — a curve preset pick foremost among them —
takes the same quiet path as a live edit.

## Why

Fixed slots removed the *reason* a preset switch used to rebuild, but the
save path ducked on every durable write regardless of reason: it always
called the file loader, which ducks unconditionally on CamillaDSP's own
rule for a config-file load. Nothing upstream of that call had ever asked
whether this particular write needed it. `#3637` asks.

## Consequences

- Flat → Harman → B&K, and any Saved-tab pick between presets that share
  the fixed slots, is now silent at both the live-edit and the durable-save
  layer.
- Four cases are deliberately left ducking, each pinned in `#3637`:
  a load of a **different file** (the audition preview) is a real swap and
  keeps its bracket; a controller with **no raw-config API** — the
  statefile transport, with CamillaDSP down — cannot be asked the question
  and keeps the file loader; an in-place **rollback** reloads the
  pre-`prepare` bytes through the file loader rather than re-sending the
  candidate that failed, because the quiet vehicle is one-shot per prepare;
  and a moved **`Gain`** keeps its bracket even though CamillaDSP would
  write it in place — `plan_live_edit`'s structural rule was safe on the
  live-draft path only because that path freezes the output trim, and a
  durable save is exactly where a trim change (a `/sound` headroom drag, a
  match-loudness toggle, `active_baseline_headroom`) is realised; it
  matches the filter's kind rather than its three emitted trim names so a
  future trim needs no list to keep in step, and a preference-EQ drag or
  curve preset pick still writes in place because those bands are Biquads.
- The Gain hold-back has a cost worth naming: with match loudness on, a
  Saved/Off or profile-to-profile A/B whose loudness compensation differs
  moves `sound_preamp` and still ducks; a preset sweep and any A/B at equal
  trim are silent.
- The fixed-slot half is hardware-verified: two fader-probe runs on jts3
  after all four curve-slot PRs (`#3462`, `#3513`, `#3527`, `#3526`) logged
  50 and 24 live edits with 0 swaps and 0 ducks from a live edit (`#3636`).
  The save-path half (`#3637`) is not yet ear-verified as of this writing —
  it landed after that hardware session. The verification step is one
  Saved-tab A/B and one Flat → Harman → B&K sweep with no `-41.67 dB` fader
  excursion landing beside an `event=sound.apply` line.
