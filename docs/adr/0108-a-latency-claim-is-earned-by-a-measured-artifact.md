# ADR-0108: A low-latency route claim is earned by a measured artifact, never by configuration

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`usb_low_latency_48k` is a *claim* a box makes about the USB-in route: that the
host→speaker path meets a stated budget while staying inside the shared
fan-in → CamillaDSP → outputd protection path. Every earlier attempt to justify
that claim leaned on configuration — the env knobs are set, therefore the route
must be fast. That reasoning failed twice in a way configuration cannot see: a
runtime ALSA negotiation can hand back a different buffer than the one
requested, and a settings set that measured well on one operating point (a
256+256 resampler lab geometry) could not sustain itself once cushion decay and
the host-clock ladder were armed.

## Decision

**A low-latency claim is earned by a route-latency artifact carrying measured
per-impulse evidence, bound to the route identity that produced it — never by
configuration.** An unproven box fails; a *proven* box whose proof has merely
aged or drifted discloses and keeps running, per
[ADR-0101](0101-proven-once-disclose-on-change.md).

- `jasper-route-latency-harness` produces real click-in/capture-back samples;
  `jasper-route-latency-artifact` binds them to the live
  `jasper.audio_runtime_plan` route identity and writes
  `/var/lib/jasper/audio-validation/`.
- Gates: p95 ≤ 40 ms over ≥ 200 impulses / ≥ 5 minutes for the quick pass;
  p99 ≤ 42 ms over ≥ 1000 jittered impulses / ≥ 30 minutes for promotion.
- The budget rides `route_config_hash`. Changing the hash schema, any hashed
  route input, or the budget itself means the existing artifact no longer
  certifies the live route and one fresh measured run is owed — a tightened
  gate is not retroactively "already passed". That is a **disclosure**
  (`config_mismatch`), not a park.
- Facts about the *proof's* validity rather than the route's —
  `config_mismatch`, `p95_uncertified`, `artifact_stale`,
  `artifact_from_future` — warn with a rerun action and keep every issue token,
  so a consumer sees what is stale without the claim being pulled. Facts about
  the *route* — a measured `p95_exceeds_40ms`, an absent p95,
  `route_health_anomaly` or any `live_*` mismatch — fail. Every leg runs before
  the single classification, so a measured breach can never hide behind a
  disclosure.
- The artifact records the live `fanin_direct_negotiated_buffer_frames`
  observed during the run, and doctor re-compares it. **Its presence is the one
  binding that stays fail** (`route_binding_missing:…`): an artifact that never
  bound the run to a negotiated buffer proves nothing about the live route, so
  it is never a compatible warning.
- `--route-health-ok` is an operator declaration, never inferred: the harness
  prints the counter deltas and states whether the declaration *would* be
  justified. Without it the artifact records `route_health_anomaly` and doctor
  rejects the claim.
- The route policy compares the **raw** env literal. A deleted knob's persisted
  spelling (`rate_match`) fail-safes to `direct` at the daemon so a stale line
  cannot park the final-output owner, but the box still reports an honest red
  claim rather than a silently downgraded green one.

## Consequences

- A configuration-only change can never promote a route. That is the point: the
  gate exists because settings and measured behaviour diverged.
- Certification is per-box and perishable. Re-cert is the routine cost of
  touching the ring geometry, the resampler operating point, or the DAC floor —
  and until it is run, the box says so rather than going silent on the claim.
- Deliberately given up: a green claim on a box whose proof has aged or drifted.
  It reads amber with the reason named, which is the honest middle the earlier
  fail-closed gate did not have — parking a working route on unproven-ness is
  reserved for the non-negotiables.
- Aggregate-only inputs require `--harness-id`, so externally computed
  percentiles can never anonymously certify a route.
