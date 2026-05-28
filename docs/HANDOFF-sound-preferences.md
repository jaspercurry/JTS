# Handoff: Sound Preferences

Current operational truth for the `/sound/` preference-EQ layer.

## Status

The sound-preference wizard is the independent preference-tuning layer
for users who want to shape the speaker without running room
correction. It lets users apply stock sound curves, simple Bass / Mid /
Treble, bounded advanced PEQ bands, and named custom profiles copied
from any stock or edited draft. It is deliberately separate from
`/correction/`:

- `/correction/` measures the room and emits room PEQs.
- `/sound/` applies user preference shaping after those room PEQs.
- The combined CamillaDSP config preserves ordering:
  room-correction PEQs first, preference EQ second, final `flat`
  terminator last.

The advanced parametric editor is intentionally touch-first: users
adjust filter type, frequency, gain, and Q/width with controls while the
graph visualizes the total curve and highlights the selected band.
Dragging points on the graph is deferred; the graph is a display
surface, not the state authority. Advanced PEQ bands show a vertical
frequency marker and, for peaking filters, a translucent width region
even when gain is still 0 dB, so a newly-added band has a visible place
on the response chart before it changes the sound.

The editable taste layer has two exclusive modes:

- **Basic** — Bass / Mid / Treble.
- **Advanced PEQ** — bounded parametric bands with exact Hz entry plus
  a log-frequency slider for fast touch adjustment.

The saved `SoundProfile` schema can represent both simple EQ and PEQ
for compatibility, but the `/sound/` UI submits one mode at a time:
Basic omits PEQ from the outgoing draft, and Advanced PEQ zeros
Bass / Mid / Treble in the outgoing draft. Stock sound curves remain
available in either mode. There is one compatibility exception: profiles
created before this split that already contain both Basic EQ and PEQ are
preserved as mixed profiles until the user explicitly taps Basic or
Advanced PEQ, which normalizes the draft into the selected mode.

The profile picker has two layers:

- **Stock** profiles are generated from built-in curves: Flat,
  Harman-style, and B&K-style. They are not persisted or editable.
- **Custom** profiles live in `/var/lib/jasper/sound_profiles.json`.
  Users can save a new custom profile from any stock/draft state,
  update an existing custom profile, rename it, or delete it. Custom
  profile library edits do not touch CamillaDSP; ordinary draft editing
  and profile loading may separately update the live Draft through
  `/sound/live-draft`.

`SoundProfile` includes optional `profile_id` / `profile_name` metadata
so the UI can distinguish "applied Flat" from "draft edited from Flat"
without making the metadata part of the DSP math. Stock identities are
`stock:<curve_id>`; custom identities are `custom_<12 hex chars>`.
Deleting a custom library entry does not delete the currently applied
DSP profile; it only removes that profile as a future draft template.

The page now has explicit compare semantics:

- **Applied** — the persisted `/var/lib/jasper/sound_profile.json`.
- **Draft** — the current unsaved form state.
- **Bypass** — preference EQ disabled while preserving room correction.

Dragging editing controls schedules a live Draft update. The browser
updates the graph immediately, coalesces audio updates, and asks
`/sound/live-draft` to upload a generated active CamillaDSP config
without changing the config file path or persisting profile state. Each
live request carries the current durable DSP write epoch from
`/var/lib/jasper/dsp_apply_state.json`; if a save or room-correction
apply wins the writer lock first, the stale live request is skipped.
Bypass / Applied / Draft compare buttons still emit `sound_audition.yml`
and load it through the validation/rollback substrate. `Save to Speaker`
emits `sound_current.yml` and persists only after the CamillaDSP reload
is confirmed.

## Files

- `jasper/sound/profile.py` — import-cheap persisted contract:
  `SoundProfile`, stock curves, simple EQ, bounded parametric bands,
  preview response, component overlays, conservative headroom estimate,
  and common compare-headroom estimate for level-matched auditions.
- `jasper/sound/camilla_yaml.py` — CamillaDSP YAML emitter and
  generated-config inspector. It must stay import-cheap; do not import
  NumPy/SciPy here.
- `jasper/dsp_apply.py` — import-cheap shared DSP apply substrate:
  typed CamillaDSP validation, config reload, rollback, file locking,
  compact last-result persistence, and the durable DSP write epoch used
  to fence stale live updates.
- `jasper/web/sound_setup.py` — `/sound/` page, `/state`, `/preview`,
  `/live-draft`, `/audition`, and `/apply`.
- `jasper/camilla.py` — lazy pyCamillaDSP wrapper. Besides the durable
  config-file loader, it owns the active-config upload/patch escape
  hatches used by live audition surfaces so raw Camilla command names do
  not leak into product code.
- `jasper/camilla_config_contract.py` — shared import-cheap CamillaDSP
  defaults and `PeqFilter` type used by generated config emitters.
- `jasper/correction/session.py` — correction apply now emits through
  the combined config path so a saved sound profile survives new room
  correction applies.
- `jasper/cli/doctor.py` and `jasper/control/server.py` — observability
  surfaces for profile/config drift and current sound state.

## Config Ownership

CamillaDSP has one active config path, so composition is load-bearing.
Do not add another writer that emits directly to CamillaDSP without
going through the same room-plus-preference ordering. `/sound/live-draft`
is the narrow exception: it emits the same combined room-plus-preference
config shape as the durable path, but uploads it as an active config
only. It must not persist profile state, write `sound_current.yml`,
change the config file path, or bypass the same known-config guard. It
still enters the shared DSP writer lock and checks the durable write
epoch before touching audio.

The generated saved sound filename is stable:

```text
/var/lib/camilladsp/configs/sound_current.yml
```

The generated unsaved audition filename is stable:

```text
/var/lib/camilladsp/configs/sound_audition.yml
```

`/sound/apply`, `/sound/audition`, and `/sound/live-draft` only preserve
room PEQs from configs they know how to inspect:

- `/etc/camilladsp/v1.yml` → no room PEQs.
- `/var/lib/camilladsp/configs/correction_<session>_<ts>.yml` → extract
  room PEQs.
- `/var/lib/camilladsp/configs/sound_current.yml` → extract room PEQs.
- `/var/lib/camilladsp/configs/sound_audition.yml` → extract room PEQs.

Anything else is treated as a custom config and rejected rather than
silently overwritten. This is intentional fail-closed behavior.

The applied profile and named profile library are intentionally separate
files:

```text
/var/lib/jasper/sound_profile.json
/var/lib/jasper/sound_profiles.json
```

`sound_profile.json` answers "what preference profile is currently
applied?" `sound_profiles.json` answers "which named custom profiles can
the user load as a draft?" This separation keeps Bypass / Applied /
Draft and future AI proposals simple.

The UI keeps profile-library management secondary. The primary user flow
is: choose a sound profile, tune Basic or Advanced controls, compare, and
Save to Speaker. Reusable profile actions (`Save Copy`, `Update Profile`,
`Rename`, `Delete`) live under Profile options because they are library
operations, not the main listening loop.

## Apply Semantics

`/sound/preview`:

1. Parses and clamps the posted `SoundProfile`.
2. Returns approximate total and component response previews.
3. Does not touch CamillaDSP or disk.

`/sound/profiles/save`, `/sound/profiles/rename`, and
`/sound/profiles/delete`:

1. Require the shared JSON CSRF header.
2. Mutate only `/var/lib/jasper/sound_profiles.json`.
3. Never load a CamillaDSP config and never change
   `/var/lib/jasper/sound_profile.json`.
4. Stamp custom profiles with their library identity metadata.
5. Return the refreshed profile-library payload for the UI picker.

`/sound/live-draft`:

1. Parses and clamps the posted Draft `SoundProfile`.
2. Computes one common compare-headroom anchor across Bypass / Applied /
   Draft.
3. Requires the posted `dsp_write_epoch` from the latest `/sound/state`.
4. Enters the shared DSP writer lock.
5. Skips the request as stale if the durable DSP write epoch changed.
6. Reads the active CamillaDSP config path with `best_effort=False`.
7. Rejects unknown/custom active configs.
8. Extracts room PEQs from known JTS-generated configs.
9. Emits a generated combined config in memory.
10. Uploads that YAML through `CamillaController.set_active_config_raw`.
11. Does **not** write `sound_audition.yml`, change the Camilla config
   file path, mutate `/var/lib/jasper/dsp_apply_state.json`, or persist
   `/var/lib/jasper/sound_profile.json`.
12. Returns `live_status=unavailable` without reloading a config when the
    controller does not expose active-config upload or CamillaDSP rejects
    the live upload; explicit compare buttons remain the safe reload path.

`/sound/audition`:

1. Parses and clamps the posted draft/bypass `SoundProfile`.
2. Computes one common compare-headroom anchor across Bypass / Applied /
   Draft. This is deterministic clipping-safe level matching, not a
   psychoacoustic loudness model.
3. Reads the active CamillaDSP config path with `best_effort=False`.
4. Rejects unknown/custom active configs.
5. Emits `sound_audition.yml` atomically inside the DSP apply lock.
6. Runs CamillaDSP validation when available.
7. Loads the config through the CamillaDSP websocket.
8. Confirms the active config path when CamillaDSP is reachable.
9. Rolls back to the prior config path if reload/confirm fails.
10. Does **not** persist `/var/lib/jasper/sound_profile.json`.

`/sound/apply` (`Save to Speaker` in the UI):

1. Reads the active CamillaDSP config path with `best_effort=False`.
2. Rejects unknown/custom active configs.
3. Enters the shared DSP apply path in `jasper/dsp_apply.py`.
4. Emits `sound_current.yml` atomically inside the DSP apply lock.
5. Runs `camilladsp --check <config>` when the binary is available.
   The config file is positional; `--check` is the validation flag.
6. Loads the config through the CamillaDSP websocket.
7. Confirms the active config path when CamillaDSP is reachable.
8. Rolls back to the prior config path if reload/confirm/persist fails.
9. Persists `/var/lib/jasper/sound_profile.json` only after a successful
   reload and confirmation.

`/correction/apply`:

1. Designs room PEQs from the measurement session.
2. Loads the saved `SoundProfile`.
3. Uses the same shared DSP apply path as `/sound/apply`, including
   locked YAML emission, validation, reload, rollback, and last-result
   persistence.
4. Emits a combined config to the correction filename so current
   correction status still works.

## Observability

`jasper-doctor` includes:

- `current correction` — recognizes correction configs plus
  `sound_current.yml` / `sound_audition.yml` when room PEQs are present.
- `sound profile` — reports saved profile, filter count, estimated
  headroom, and warns when a saved active profile is not reflected in a
  generated active config.
- `DSP apply state` — reports the most recent DSP config apply result
  from `/var/lib/jasper/dsp_apply_state.json`; rollback failure is a
  doctor failure.

Live Draft updates intentionally do not write DSP apply state, so they
are observed through `event=sound.live_draft` logs and the `/sound/`
status line rather than doctor. Durable Save to Speaker remains the
stateful operation doctor audits. Repeated live-unavailable warnings are
rate-limited so a broken live-upload environment does not spam the
journal while the user drags a slider.

`/state` and `/sound/state` expose the saved sound profile plus the
profile-library picker payload and latest DSP apply record:

```json
{
  "audio": {
    "sound": {
      "enabled": true,
      "curve_id": "flat",
      "profile_id": "stock:flat",
      "profile_name": "Flat",
      "simple_eq": {"bass_db": 0.0, "mid_db": 0.0, "treble_db": 0.0},
      "parametric_band_count": 0,
      "filter_count": 0,
      "headroom_db": 0.0,
      "updated_at": null,
      "last_dsp_apply": {
        "source": "sound",
        "result": "success",
        "candidate_config_path": "/var/lib/camilladsp/configs/sound_current.yml"
      },
      "dsp_write_epoch": "<latest dsp apply op_id or none>"
    }
  }
}
```

`/correction/status` also includes `last_dsp_apply` so a failed apply
can be diagnosed without scraping journal logs.

## Guardrails

- Keep `/sound/` cheap to load. The combined `jasper-web` process must
  not import NumPy/SciPy on cold start.
- Keep preference EQ bounded. The v1 simple EQ range is ±6 dB, advanced
  bands are capped, and generated configs add digital preamp attenuation
  for positive boosts.
- Do not merge room-correction target selection and preference EQ into
  one opaque layer. They can share UI affordances later, but the DSP
  contract must keep them distinct.
- A/B bypass should toggle only preference EQ, not erase room
  correction.
- The graph is visualization only. The canonical editable state is the
  bounded `SoundProfile` JSON model.
- Basic and Advanced PEQ are exclusive for newly-created drafts. Existing
  mixed profiles from the older combined UI must not silently lose their
  hidden Basic EQ; preserve them until an explicit mode switch normalizes
  the draft.
- Named custom profiles are draft templates. Loading, renaming, saving,
  or deleting one must not persist profile state unless the user saves to
  the speaker. Editing or loading a draft may change live audio through
  `/sound/live-draft`; that live draft remains intentionally non-durable.
- Unsaved auditions must never persist profile state. They may leave
  `sound_audition.yml` active until the user switches Bypass / Applied /
  Draft or applies; that is expected and observable via the DSP apply
  record.
- Live Draft must only touch the preference EQ layer of a known JTS
  config. It must never change room PEQs, source routing, limiter,
  crossover, or the `devices.volume_limit: 0.0` safety ceiling.
- Live Draft requests must be coalesced client-side. Do not fire one
  CamillaDSP upload per touch pixel.
- Live Draft requests must include and verify the durable DSP write
  epoch. A stale live request must be a no-op, not an older active
  config upload after `Save to Speaker` or `/correction/apply`.

## Future Work

- AI helper that proposes bounded `SoundProfile` edits and asks the user
  to approve before applying.
- Optional profile export/import once we know what users want to share.
- More precise loudness matching if listening tests show the common
  headroom anchor is not enough.
- Optional desktop-only draggable graph handles. Keep mobile/touch
  controls as the primary path.
- Optional voice-feedback loop using the existing Pi microphone path.

Last verified: 2026-05-28
