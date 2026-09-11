# ADR-0293: The no-provider park is owned by the voice daemon

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

ADR-0165 put the unconfigured-provider park in two places: `jasper-voice`'s
own `Config.from_env` — which raises, so the daemon speaks `voice_not_set_up`
and exits 78 — and a `jasper-aec-reconcile` gate on the same generated
provider manifest, which ran `disable --now jasper-voice.service` whenever
the active provider was unset, invalid, or missing from the manifest.
`disable --now` stops the unit immediately, which pre-empts the daemon's own
start — and non-negotiable 6 (no silent deafness) requires that start to
speak before it parks. Two owners of one axis, one of which could take the
other's cue away.

## Decision

`jasper-voice` owns the unconfigured or unrecognised-provider case end to
end: `Config.from_env` raises on `run()`'s first statement, the daemon plays
`voice_not_set_up`, and exits 78 — a code the unit's `SuccessExitStatus` and
`RestartPreventExitStatus` turn into a quiet park. `jasper-aec-reconcile` no
longer disables jasper-voice on that axis, and no longer gates its pass on
the provider.

## Consequences

A previously-configured box whose provider goes unset or invalid now hears
the cue on the daemon's next start, not at provider-save time: the wizard's
`restart_voice_daemon()` returns `SKIPPED` when the provider is unset, so
saving a blank provider does not itself restart the daemon. A
never-configured box stays silent until #4814 ships a baked WAV. No
`ExecCondition --changed` short-circuit is added to `jasper-aec-reconcile`'s
unit: a provider recovery carries no file delta the change gate can see, so
gating the pass on `--changed` would skip the recovery this axis depends on.
