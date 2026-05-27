# Handoff: Sound Preferences

Current operational truth for the `/sound/` preference-EQ layer.

## Status

The sound-preference wizard is the independent preference-tuning layer
for users who want to shape the speaker without running room
correction. It lets users apply stock sound curves, simple Bass / Mid /
Treble, and bounded advanced PEQ bands. It is deliberately separate
from `/correction/`:

- `/correction/` measures the room and emits room PEQs.
- `/sound/` applies user preference shaping after those room PEQs.
- The combined CamillaDSP config preserves ordering:
  room-correction PEQs first, preference EQ second, final `flat`
  terminator last.

The advanced parametric editor is intentionally touch-first: users
adjust filter type, frequency, gain, and Q/width with controls while the
graph visualizes the total curve and highlights the selected band.
Dragging points on the graph is deferred; the graph is a display
surface, not the state authority.

The page now has explicit compare semantics:

- **Saved** — the persisted `/var/lib/jasper/sound_profile.json`.
- **Draft** — the current unsaved form state.
- **Bypass** — preference EQ disabled while preserving room correction.

Draft / Bypass auditions emit `sound_audition.yml` and load it through
the same validation/rollback substrate, but do **not** persist the
profile. `Save & Apply` emits `sound_current.yml` and persists only
after the CamillaDSP reload is confirmed.

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
  and compact last-result persistence.
- `jasper/web/sound_setup.py` — `/sound/` page, `/state`, `/preview`,
  `/audition`, and `/apply`.
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
going through the same room-plus-preference ordering.

The generated saved sound filename is stable:

```text
/var/lib/camilladsp/configs/sound_current.yml
```

The generated unsaved audition filename is stable:

```text
/var/lib/camilladsp/configs/sound_audition.yml
```

`/sound/apply` only preserves room PEQs from configs it knows how to
inspect:

- `/etc/camilladsp/v1.yml` → no room PEQs.
- `/var/lib/camilladsp/configs/correction_<session>_<ts>.yml` → extract
  room PEQs.
- `/var/lib/camilladsp/configs/sound_current.yml` → extract room PEQs.
- `/var/lib/camilladsp/configs/sound_audition.yml` → extract room PEQs.

Anything else is treated as a custom config and rejected rather than
silently overwritten. This is intentional fail-closed behavior.

## Apply Semantics

`/sound/preview`:

1. Parses and clamps the posted `SoundProfile`.
2. Returns approximate total and component response previews.
3. Does not touch CamillaDSP or disk.

`/sound/audition`:

1. Parses and clamps the posted draft/bypass `SoundProfile`.
2. Computes one common compare-headroom anchor across Bypass / Saved /
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

`/sound/apply`:

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

`/state` and `/sound/state` expose the saved sound profile plus the
latest DSP apply record:

```json
{
  "audio": {
    "sound": {
      "enabled": true,
      "curve_id": "flat",
      "simple_eq": {"bass_db": 0.0, "mid_db": 0.0, "treble_db": 0.0},
      "parametric_band_count": 0,
      "filter_count": 0,
      "headroom_db": 0.0,
      "updated_at": null,
      "last_dsp_apply": {
        "source": "sound",
        "result": "success",
        "candidate_config_path": "/var/lib/camilladsp/configs/sound_current.yml"
      }
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
- Unsaved auditions must never persist profile state. They may leave
  `sound_audition.yml` active until the user switches Bypass/Saved/Draft
  or saves; that is expected and observable via the DSP apply record.

## Future Work

- AI helper that proposes bounded `SoundProfile` edits and asks the user
  to approve before applying.
- Named user presets / profile library. Today there is one editable
  saved custom profile built from stock curve + simple EQ + PEQ bands.
- More precise loudness matching if listening tests show the common
  headroom anchor is not enough.
- Optional desktop-only draggable graph handles. Keep mobile/touch
  controls as the primary path.
- Optional voice-feedback loop using the existing Pi microphone path.

Last verified: 2026-05-27
