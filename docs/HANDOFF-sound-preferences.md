# Handoff: Sound Preferences

Current operational truth for the `/sound/` preference-EQ layer.

## Status

The sound-preference wizard is a simple first slice of the larger
room-correction / target-curve / AI tuning vision. It lets users apply
stock sound curves plus Bass, Mid, and Treble without running room
correction. It is deliberately separate from `/correction/`:

- `/correction/` measures the room and emits room PEQs.
- `/sound/` applies user preference shaping after those room PEQs.
- The combined CamillaDSP config preserves ordering:
  room-correction PEQs first, preference EQ second, final `flat`
  terminator last.

This is not the advanced parametric editor yet. The backend data model
already includes bounded parametric bands so a future advanced UI or AI
helper can propose deterministic edits, but the shipped page currently
exposes only Flat / Harman-style / B&K-style plus Bass / Mid / Treble.

## Files

- `jasper/sound/profile.py` — import-cheap persisted contract:
  `SoundProfile`, stock curves, simple EQ, bounded parametric bands,
  preview response, and conservative headroom estimate.
- `jasper/sound/camilla_yaml.py` — CamillaDSP YAML emitter and
  generated-config inspector. It must stay import-cheap; do not import
  NumPy/SciPy here.
- `jasper/dsp_apply.py` — import-cheap shared DSP apply substrate:
  typed CamillaDSP validation, config reload, rollback, file locking,
  and compact last-result persistence.
- `jasper/web/sound_setup.py` — `/sound/` page, `/state`, `/preview`,
  and `/apply`.
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

The generated sound filename is stable:

```text
/var/lib/camilladsp/configs/sound_current.yml
```

`/sound/apply` only preserves room PEQs from configs it knows how to
inspect:

- `/etc/camilladsp/v1.yml` → no room PEQs.
- `/var/lib/camilladsp/configs/correction_<session>_<ts>.yml` → extract
  room PEQs.
- `/var/lib/camilladsp/configs/sound_current.yml` → extract room PEQs.

Anything else is treated as a custom config and rejected rather than
silently overwritten. This is intentional fail-closed behavior.

## Apply Semantics

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

- `current correction` — recognizes correction configs and
  `sound_current.yml` when room PEQs are present.
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

## Future Work

- Advanced parametric UI with explicit Q/frequency/gain controls.
- AI helper that proposes bounded `SoundProfile` edits and asks the user
  to approve before applying.
- Better level-matched compare/proposal flow.
- Optional voice-feedback loop using the existing Pi microphone path.

Last verified: 2026-05-27
