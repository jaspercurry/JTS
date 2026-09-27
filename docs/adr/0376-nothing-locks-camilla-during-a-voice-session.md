# ADR-0376: Nothing locks Camilla during a voice session, so there is no duck lock to ask about

- **Date:** 2026-09-27
- **Status:** Accepted. Supersedes
  [ADR-0177](0177-duck-ownership-is-asked-of-the-owner-never-inferred-from-a-db-gap.md).
- **Context:** ADR-0177 had the coordinator ask jasper-voice whether its duck
  owned Camilla (`STATUS` → `camilla_volume_locked`) and defer fader writes
  while it did. The legacy Camilla `Ducker` that could hold that lock is
  deleted. The one ducker left, `FanInDucker`, ducks program audio inside
  fan-in and leaves Camilla alone, so the lock was never set, the probe never
  answered true, and the defer branch never ran
  ([#5895](https://github.com/jaspercurry/JTS/issues/5895)).
- **Decision:** Nothing locks Camilla during a voice session, so nothing asks.
  `_camilla_volume_locked` and the `camilla_volume_locked` argument of
  `note_voice_session`, the `duck_active_probe` that jasper-control and
  jasper-mux passed in, the `volume.deferred
  reason=camilla_volume_locked|session_signaled` branch, the `STATUS` field
  and `/state.voice.camilla_volume_locked` are deleted.
  `note_voice_session(active)` stays: a session still holds off source
  transitions and the reconciler. `/state.voice.duck_active` stays as session
  telemetry. ADR-0177's rule that a dB gap is no evidence of an owner stands.
- **Consequences:**
  - No runtime path changes: the deleted branches never ran in production.
  - jasper-control and jasper-mux no longer ask jasper-voice anything before
    a volume write.
  - A new duck that writes Camilla has no lock for foreground writes to defer
    to. It needs its own decision.
  - No rolling-upgrade shim (no backward support): during a deploy, a
    jasper-control or jasper-mux still on the old code reads `duck_active` as
    the lock until it restarts, which can only hold a write back.
