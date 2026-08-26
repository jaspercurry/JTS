# ADR-0177: Duck ownership is asked of the owner, never inferred from a dB gap

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-volume.md was trimmed to its
  operational spine)

## Context

While the legacy Camilla `Ducker` owns `main_volume` for a voice session, a
volume write from another surface must defer — otherwise the write and the
duck fight, and `Ducker.restore()` lands the fader on a level nobody asked
for. jasper-voice knows this in-process (`_camilla_volume_locked`), but
jasper-control builds a fresh `VolumeCoordinator` per HTTP request, so that
flag is always `False` there. The cross-daemon question had to be answered
some other way.

Until 2026-05-25 it was answered by inference: *if the requested target is
more than 5 dB above Camilla's current `main_volume`, assume a duck and
defer.* That signal is structurally ambiguous — "the Ducker has lowered
Camilla by 25 dB" and "a client sent one large relative increase" look
identical to it, and any sufficiently large request crossed the threshold.

The misfire was not cosmetic. A deferred write still persisted
`listening_level` (the caller does it in `_dispatch`'s `finally`) but not
`main_volume_db`. With no Ducker actually running, nothing converged them, so
every later relative request computed its target from the now-inflated
`listening_level`, the gap widened, and the heuristic fired again — a
self-perpetuating cascade. Control surfaces read 100% while the speaker stayed
quiet.

## Decision

**Ask the owner.** jasper-voice is the source of truth about whether its own
Ducker holds Camilla, so the coordinator asks it over UDS
(`STATUS` → `camilla_volume_locked`) instead of guessing from the fader.
`_duck_active_probe` is that question; `_camilla_volume_locked` is the same
fact read directly on jasper-voice's own long-lived coordinator.

The question asked is "is a duck *actually* active", not "would this write
*look like* it is fighting a duck". A dB gap is a symptom shared by two
unrelated causes and is never evidence of ownership.

**Voice-session state and Camilla ownership are two facts, not one.** The
production `FanInDucker` attenuates renderer/program audio inside fan-in and
leaves Camilla alone, so `duck_active=true, camilla_volume_locked=false` is
the ordinary speech state and volume writes proceed through it. Only the
legacy Camilla ducker sets the lock. Both fields are published in
`/state.voice`.

**The probe fails open.** `None` — voice unreachable, wedged, malformed reply
— means "unknown" and the write lands. A remote must never go inert because
of an inter-daemon problem, and a wedged daemon cannot be ducking anyway.

## Consequences

- **The defer is now exactly as narrow as the ownership it protects.** Fan-in
  speech no longer suppresses foreground volume writes, which is what makes
  "Jarvis, louder" work mid-reply.
- **A rolling upgrade is handled explicitly, not by luck.** The probe falls
  back to the older `duck_active` field when `camilla_volume_locked` is absent
  from the reply, so a half-upgraded pair degrades to the previous (coarser)
  behaviour rather than to the heuristic.
- **The cascade class is closed at the root.** No amount of relative-increase
  traffic can now manufacture a phantom duck, because size is no longer part
  of the signal.
- **Deliberately given up: independence.** jasper-control now depends on
  jasper-voice answering a question to make the *best* decision. That coupling
  is bounded by the fail-open rule — the dependency can only cost a moment of
  un-ducked music, never a dead control.
- **Rejected: a shared duck lease.** An `{owner, depth_db, expires_at}` record
  in `/run` was considered and is tracked as the tuning program's own
  question (`REFACTOR-2026-08.md` wave 5, superseded there). Asking the one
  daemon that knows is cheaper than maintaining a second source of truth that
  can itself go stale.
