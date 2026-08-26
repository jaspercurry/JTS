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

**Doctor fails the low-latency claim until a route-latency artifact certifies
it with measured per-impulse evidence, and the artifact is bound to the route
identity that produced it.**

- `jasper-route-latency-harness` produces real click-in/capture-back samples;
  `jasper-route-latency-artifact` binds them to the live
  `jasper.audio_runtime_plan` route identity and writes
  `/var/lib/jasper/audio-validation/`.
- Gates: p95 ≤ 40 ms over ≥ 200 impulses / ≥ 5 minutes for the quick pass;
  p99 ≤ 42 ms over ≥ 1000 jittered impulses / ≥ 30 minutes for promotion.
- The budget rides `route_config_hash`. Changing the hash schema, any hashed
  route input, or the budget itself invalidates existing artifacts and requires
  one fresh measured run — a tightened gate is not retroactively "already
  passed".
- The artifact records the live `fanin_direct_negotiated_buffer_frames`
  observed during the run, and doctor re-compares it. A runtime negotiation
  change invalidates certification even when every configured target and the
  route hash still match.
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
  touching the ring geometry, the resampler operating point, or the DAC floor.
- Deliberately given up: a green claim on a box whose evidence is merely stale.
  Regenerate pre-schema or negotiated-buffer-mismatched evidence; do not treat
  it as a compatible warning.
- Aggregate-only inputs require `--harness-id`, so externally computed
  percentiles can never anonymously certify a route.
