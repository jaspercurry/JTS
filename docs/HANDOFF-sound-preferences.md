# Handoff: Sound Preferences

Current operational truth for the `/sound/` preference-EQ layer.

## Status

The sound-preference wizard is the independent preference-tuning layer
for users who want to shape the speaker without running room
correction. It lets users apply stock sound curves, a five-band Simple
EQ (Sub-bass / Bass / Mid / Presence / Treble), bounded advanced PEQ
bands, and named custom profiles copied from any stock or edited draft.
It is deliberately separate from `/correction/`:

- `/correction/` measures the room and emits room PEQs.
- `/sound/` applies user preference shaping after those room PEQs.
- The combined CamillaDSP config preserves ordering:
  room-correction PEQs first, preference EQ second, final `flat`
  terminator last.

The advanced parametric editor is intentionally touch-first: users
adjust filter type, frequency, gain, and Q/width with controls while the
graph keeps the total response visually dominant. When a PEQ band row is
expanded, the graph also shows that one band's individual response as a
secondary overlay and, for peaking filters, its translucent width region;
collapsed/non-selected bands remain passive markers only.
Dragging points on the graph is deferred; the graph is a display
surface, not the state authority. Advanced PEQ bands show a vertical
frequency marker even when gain is still 0 dB, so a newly-added band has
a visible place on the response chart before it changes the sound.

The Draft editor has two exclusive modes:

- **Simple** — five fixed-frequency gain sliders (Sub-bass 60 Hz,
  Bass 150 Hz, Mid 1 kHz, Presence 4 kHz, Treble 10 kHz). Only gain is
  editable per band; the slot table is `SIMPLE_BANDS`
  (`jasper/sound/profile.py`) and is exposed to the UI via
  `/state.limits.simple_bands`, so the page renders the columns from
  data rather than hardcoding band labels.
- **PEQ** — bounded parametric bands (a collapsible accordion) with
  exact Hz entry plus a log-frequency slider for fast touch adjustment.

Switching modes converts the draft rather than warning: PEQ → Simple
snaps each Simple slot to the nearest enabled PEQ band by log-frequency
(within ~1.2 octaves); Simple → PEQ turns each non-zero Simple band into
a parametric band. This replaces the older Basic/Advanced "mixed
profile" compatibility shim. Older 3-band profiles still load — the two
new Simple bands (Sub-bass, Presence) default to 0 dB — but note the
redesign moved the Bass centre (105 → 150 Hz) and the Treble shelf
(4 k → 10 k), so a migrated profile's bass/treble values shape slightly
different frequencies.

The **Saved** tab lists profiles in two sections:

- **Presets** are generated from built-in curves: Flat, Harman-style,
  and B&K-style. They are not persisted or editable, but can be opened
  in Draft (pencil) and saved as a new custom profile.
- **Your profiles** are custom profiles in
  `/var/lib/jasper/sound_profiles.json`. Each row has edit (pencil) and
  delete actions; the Draft footer adds Overwrite / Save as new /
  Rename. Custom profile library edits do not touch CamillaDSP; ordinary
  draft editing may separately update the live Draft through
  `/sound/live-draft`.

`SoundProfile` includes optional `profile_id` / `profile_name` metadata
so the UI can distinguish "applied Flat" from "draft edited from Flat"
without making the metadata part of the DSP math. Stock identities are
`stock:<curve_id>`; custom identities are `custom_<12 hex chars>`.
Deleting a custom library entry does not delete the currently applied
DSP profile; it only removes that profile as a future draft template.

The top control is **Off / Saved / Draft**, and it is the live source:

- **Off** — preference EQ disabled (bypass) while preserving room
  correction. Clicking Off durably applies a bypassed profile.
- **Saved** — the saved-profile listening lane. Entering Saved
  durably applies the currently selected saved profile; if there is no
  remembered selection, it falls back to the Flat preset. Tapping a row
  changes that remembered Saved selection and applies it immediately.
- **Draft** — the editor. Dragging a control schedules a live Draft
  update: the browser updates the graph immediately, coalesces audio
  updates, and asks `/sound/live-draft` to upload a generated active
  CamillaDSP config without changing the config file path or persisting
  profile state. Each live request carries the current durable DSP write
  epoch from `/var/lib/jasper/dsp_apply_state.json`; if a save or
  room-correction apply wins the writer lock first, the stale live
  request is skipped.

Off and Saved are **durable** (they go through `/sound/apply`); Draft is
a live, non-persistent preview until the footer Save commits it. The top
tabs are therefore A/B/C listening controls, not passive navigation:
Off bypasses, Saved resumes the saved reference profile, and Draft resumes
the unsaved working profile. The browser treats those live-source changes
as last-intent-wins: if a durable apply is already in flight, the newest
Off/Saved/Draft intent is replayed after the apply returns, so quick A/B
taps cannot leave the graph on one source while the speaker keeps playing
another. The
Draft footer adapts to origin: a new draft offers Save profile /
Discard; editing a custom profile offers Overwrite / Save as new /
Rename / Discard; editing a preset offers Save as new / Discard. Saving
emits `sound_current.yml` and persists only after the CamillaDSP reload
is confirmed.

### Gain staging — boosts boost

A preference boost applies at unity: a +N dB band raises only that band
and leaves the rest of the spectrum untouched, the way a consumer EQ
behaves. The generated config inserts **no** automatic preamp. The only
global attenuation is an opt-in **output trim**, which is 0 by default,
so the default is "boosts boost". The `devices.volume_limit: 0.0` master
ceiling remains the hard clip guard, so removing the old preamp cannot
raise the output ceiling — at high volume a large boost clips at 0 dBFS
rather than ducking the whole mix.

Two opt-in, default-off global settings (distinct from per-profile EQ)
feed that trim. They are owned by `/sound/` at
`/var/lib/jasper/sound_settings.json` (`jasper/sound/settings.py`),
fail-soft to the do-nothing defaults — a missing or corrupt file never
alters the sound:

- **Match loudness** — turns each profile down by its loudness-weighted
  gain (`loudness_compensation_db`: a pink/K-weighted power average of the
  EQ response, **not** peak, so a narrow +8 dB band compensates ~1 dB, not
  8) so switching profiles compares tone, not volume. Anchored to
  attenuation (≥ 0), so it can never cause clipping.
- **Extra headroom** — a manual 0–12 dB digital attenuation
  (`headroom_trim_db`, clamped) for listeners running JTS at full digital
  volume into their own amplifier.

The emitter applies one `output_trim_db = headroom_trim + (loudness
compensation when match-loudness is on)` as the `sound_preamp` gain, and
only when the active profile actually has filters (a flat profile can't
clip from EQ). `estimate_headroom_db` remains as the peak-boost *metric*
surfaced by doctor / `control` `/state` / the calibration advisor; it no
longer drives an auto-preamp.

The `/sound/audition` endpoint and `sound_audition.yml` remain in the
backend as a validated, non-persisting apply: it writes a separate
`sound_audition.yml` through the same validation/rollback substrate as
`/sound/apply`, without persisting the profile. The redesigned UI no
longer drives it — editing previews through the faster `/sound/live-draft`
path and commits through durable apply — but it is intentionally retained
as the validated-preview surface for "propose, preview, then approve"
AI helper flows. As of 2026-05-31,
`jasper.calibration_agent.sound_actions` can opt into that exact backend
for validated advisor `propose_preference_eq_audition` actions. The CLI
flag is `jasper-calibration-agent --call-advisor --audition-sound` (or
`--run-advisor-actions <path> --audition-sound` for offline response
fixtures). This remains reversible: no profile persistence, no volume
authority, and the action result reports the generated config basename
rather than exposing raw filesystem paths.

### Advanced speaker setup entry point

As of 2026-06-02, `/sound/` also shows a collapsed **Advanced speaker
setup** card for active crossover commissioning. It is intentionally
read-only: a user can click **Check environment** to fetch
`/sound/active-speaker/environment`, which runs the read-only
`jasper.active_speaker.environment` probe and reports ALSA playback-device
count, current CamillaDSP config classification, validation status,
load-gate status, and why safe playback is still blocked. It does not play
tones, start sweeps, reload CamillaDSP, load active crossover configs, or
touch live audio. The same card can **Arm safe session** and **Stop** a
no-audio safety session through `/sound/active-speaker/arm` and
`/sound/active-speaker/stop`; arming only persists the current safety state
when the environment load gate passes, and Stop is a normal-sized,
idempotent control that records the session as stopped. It still does not
emit tones or authorize playback. When armed, the card can also prepare a
bounded no-audio channel-test plan through `/sound/active-speaker/tone-plan`.
That plan shows the target output, frequency, level, and duration, but it
still returns `would_play: false` and does not authorize playback. The actual
substrate starts in
`jasper.active_speaker` and the canonical safety/design plan lives in
[`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md).

## Files

- `jasper/active_speaker/` — import-cheap active-speaker preset,
  channel-map, safety-envelope, baseline-profile schemas, and
  muted/protected startup-template YAML emission plus read-only
  environment reporting, no-audio safe-playback session state, and
  preset-derived no-audio tone-plan preparation. Current scope is
  validation/template generation and status/session/plan bookkeeping only;
  no hardware loading or playback.
- `jasper/sound/profile.py` — import-cheap persisted contract:
  `SoundProfile`, stock curves, simple EQ, bounded parametric bands,
  preview response, expanded-band overlays, the peak-boost `estimate_headroom_db`
  metric, and `loudness_compensation_db` (the loudness-weighted gain the
  opt-in match-loudness setting applies).
- `jasper/sound/settings.py` — import-cheap global output settings
  (`SoundSettings`: `headroom_trim_db`, `match_loudness`) persisted to
  `/var/lib/jasper/sound_settings.json`, fail-soft to the do-nothing defaults.
- `jasper/sound/camilla_yaml.py` — CamillaDSP YAML emitter and
  generated-config inspector. It must stay import-cheap; do not import
  NumPy/SciPy here.
- `jasper/dsp_apply.py` — import-cheap shared DSP apply substrate:
  typed CamillaDSP validation, config reload, rollback, file locking,
  compact last-result persistence, and the durable DSP write epoch used
  to fence stale live updates.
- `jasper/web/sound_setup.py` — `/sound/` page, `/state`, `/preview`,
  `/live-draft`, `/audition`, `/apply`, and `/settings`.
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

- `/etc/camilladsp/outputd-cutover.yml` → no room PEQs.
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

The primary user flow is: pick Off / a Saved profile, or open Draft to
tune Simple or PEQ controls and Save. Library operations (Save as new,
Overwrite, Rename, Delete) live on the Saved rows and in the Draft
footer rather than a separate menu.

The page is built on the **canonical design system**: shared tokens,
fonts, and component primitives live in `deploy/assets/app.css` (served
static by nginx, linked via `jasper.web._common.canonical_page`), with
only sound-specific component CSS inline. `/sound/` is the first wizard
on this system; see AGENTS.md "Canonical design system" for the
convention other wizards follow.

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
2. Computes the draft's output trim from the global sound settings (manual
   headroom + loudness compensation when match-loudness is on; 0 by default,
   so boosts apply at unity).
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
2. Computes the output trim from the global sound settings (same opt-in
   headroom + match-loudness as the live-draft path; 0 by default).
3. Reads the active CamillaDSP config path with `best_effort=False`.
4. Rejects unknown/custom active configs.
5. Emits `sound_audition.yml` atomically inside the DSP apply lock.
6. Runs CamillaDSP validation when available.
7. Loads the config through the CamillaDSP websocket.
8. Confirms the active config path when CamillaDSP is reachable.
9. Rolls back to the prior config path if reload/confirm fails.
10. Does **not** persist `/var/lib/jasper/sound_profile.json`.

`/sound/settings`:

1. Requires the shared JSON CSRF header (route-checked before CSRF).
2. Clamps and saves the global `SoundSettings`
   (`headroom_trim_db`, `match_loudness`) to
   `/var/lib/jasper/sound_settings.json`.
3. Re-applies the active profile through the `/sound/apply` path so the new
   output trim takes effect immediately and persists in `sound_current.yml`.
4. Saves the settings **before** the re-apply, so a failed re-apply still
   sticks (returned as an error; the saved setting takes effect on the next
   apply).

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
  headroom, the global match-loudness / headroom settings, the effective
  output trim, and warns when a saved active profile is not reflected in a
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
profile-library picker payload and latest DSP apply record. `/sound/state`
additionally carries the global `sound_settings` and the effective
`output_trim_db` for the current profile, so the page renders the
match-loudness switch and headroom slider from server truth. The central
`jasper-control` `/state.audio.sound` carries the same `match_loudness`,
`headroom_trim_db`, and `output_trim_db` (and `jasper-doctor` prints them),
so an operator can see why a profile sounds quieter or level-matched without
opening the page. `/state` also includes the runtime Camilla config truth so
dashboards do not confuse "profile desired" with "profile actually loaded":

```json
{
  "audio": {
    "sound": {
      "enabled": true,
      "curve_id": "flat",
      "profile_id": "stock:flat",
      "profile_name": "Flat",
      "simple_eq": {"sub_bass_db": 0.0, "bass_db": 0.0, "mid_db": 0.0, "presence_db": 0.0, "treble_db": 0.0},
      "parametric_band_count": 0,
      "filter_count": 0,
      "headroom_db": 0.0,
      "updated_at": null,
      "last_dsp_apply": {
        "source": "sound",
        "result": "success",
        "active_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
        "candidate_config_path": "/var/lib/camilladsp/configs/sound_current.yml"
      },
      "runtime": {
        "active_config_path": "/etc/camilladsp/outputd-cutover.yml",
        "last_apply_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
        "matches_last_apply": false,
        "state": "base",
        "active": false,
        "warning": "Desired sound profile is not the active CamillaDSP config."
      },
      "dsp_write_epoch": "<latest dsp apply op_id or none>"
    }
  }
}
```

For `/state.audio.sound`, `enabled` remains the persisted preference.
Use `runtime.active` / `runtime.state` to answer whether CamillaDSP is
currently running the saved profile, the flat outputd base config, a
custom config, or an unexpected mismatch.

`/correction/status` also includes `last_dsp_apply` so a failed apply
can be diagnosed without scraping journal logs.

## Guardrails

- Keep `/sound/` cheap to load. The combined `jasper-web` process must
  not import NumPy/SciPy on cold start.
- Keep preference EQ bounded. The Simple EQ range is ±12 dB (matching
  the slider UI; shared with the calibration advisor via
  `SIMPLE_EQ_LIMIT_DB`) and advanced bands are capped. Boosts apply at
  unity by default (no auto-preamp); only the opt-in output trim
  (`headroom_trim_db` + match-loudness compensation) attenuates, and the
  `devices.volume_limit: 0.0` ceiling stays the hard clip guard.
- Do not merge room-correction target selection and preference EQ into
  one opaque layer. They can share UI affordances later, but the DSP
  contract must keep them distinct.
- A/B bypass should toggle only preference EQ, not erase room
  correction.
- The graph is visualization only. The canonical editable state is the
  bounded `SoundProfile` JSON model.
- Simple and PEQ are exclusive modes; switching converts the draft
  (PEQ → Simple snaps to the nearest slot; Simple → PEQ expands non-zero
  bands). Older 3-band profiles still load, with Sub-bass / Presence
  defaulting to 0 dB.
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

- Browser/voice AI helper UI around the existing advisor harness: propose
  bounded `SoundProfile` edits, audition them through `/sound/audition`,
  compare against the listener's chosen baseline, then ask the user before
  saving anything.
- Optional profile export/import once we know what users want to share.
- A clipping indicator (live on `/sound/`, backed by a `/state` field from
  CamillaDSP's clipped-sample counter, with doctor carrying the cumulative
  count) so the opt-in headroom trim is guided rather than guessed.
- Optional desktop-only draggable graph handles. Keep mobile/touch
  controls as the primary path.
- Optional voice-feedback loop using the existing Pi microphone path.

Last verified: 2026-06-02
