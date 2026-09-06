# ADR-0244: `/state.audio_graph` section deleted

- **Date:** 2026-09-06
- **Status:** Accepted

## Context

An observability sweep for issue #4197 found `/state.audio_graph`
(`_camilla_unit_state`, `_audio_graph_state`, `_coupling_state`,
`_observed_ring_wire`, `_combo_state` — ~295 lines, five helpers in
`jasper/control/state_aggregate.py`) with no reader anywhere in the repo
beyond its own tests: no Python, JS, shell, or doc consumer calls it or
parses the section. Per ADR-0233 rule 2, a `/state` field must justify a
machine reader. The facts it re-projected are each read elsewhere by their
own dedicated reader: the CamillaDSP unit verdict by
`audio_health._camilla_stopped`/`_state_issues` (feeding
`/state.audio_health`), the fan-in→CamillaDSP coupling by the doctor's
`check_fanin_coupling_value`/`check_fanin_coupling`
(`jasper/cli/doctor/audio_runtime_fanin.py`), and the USB combo state by the
doctor's usbsink rows (`combo_armed_from_env`,
`jasper/cli/doctor/usbsink.py`).

## Decision

The `/state.audio_graph` section is deleted, along with the five helpers
that built it and their tests. `STATE_SCHEMA_VERSION` moves 2 → 3 (a
top-level key was removed; PR #4295 took 2 for `observed_at`).

## Consequences

`curl /state` no longer carries `audio_graph`. A future consumer that needs
one of these facts reads the surface that already owns it —
`/state.audio_health`, the doctor's fan-in-coupling checks, or the doctor's
usbsink combo row — rather than a re-projection with no reader.
