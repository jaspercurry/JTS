# ADR-0109: The combo host-clock servo observes resampler correction, not gadget fill

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

The host-slaved USB clock steers the gadget's `Capture Pitch 1000000` control so
a connected host tracks the DAC clock, closing the standing rate offset at its
source. The original servo (written for the deleted aloop-solo capture path)
observed the **gadget FIFO fill slope**: with no rate-matching stage between the
gadget ring and playback, fill slope is a faithful readout of host-vs-DAC rate
error.

In combo mode fan-in's per-lane resampler sits between the gadget ring and the
mix with ±500 ppm of authority, and it holds its fill at the held target by its
own action. That kills the observable: on jts.local (2026-07-03) the fill-based
post-lock probe reliably failed (`response_ratio = -0.88`) and the ladder parked
in `l2_fallback`, while even a nominally locked ladder had a dead fill-error
signal — the fill sat pinned by the resampler, not by the pitch commands.

## Decision

**The one servo core runs on a typed `ObsMode` carried by `HostClockConfig`,
never inferred, and fan-in combo passes `ObsMode::Correction`:** the observable
is the lane resampler's own live correction ppm (its `ratio_milli_ppm` gauge —
the same atomic the STATUS block reads, owned by the resampler on the mixer
thread). The probe measures how far the mean correction moves between the
neutral baseline window and the step window; the L0 servo drives
`correction_ppm → 0`. At `correction_ppm ≈ 0` sustained, the host is truly
slaved to the DAC, the resampler is idle, and the fill rides its held target for
free.

**`Correction` mode's outer control law is a single slow integrator, not the
`Fill`-mode DLL.** The two modes present the outer loop different plants:
`Fill`'s plant is an integrator (a pitch command sets the fill *slope*), so a
third-order DLL is a well-behaved cascade; `Correction`'s plant is near-unity DC
gain through the inner rate controller's lag (ppm command → ~the same ppm of
correction). Driving unity-gain-plus-lag with the `Fill`-tuned DLL puts loop
gain above 1 past 180° of phase and limit-cycles — reproduced against the real
inner controller, where a compliant host at +20 ppm railed correction at ±460
ppm on a ~21 s period. The feed-forward seed (`−baseline_correction`) carries
the DC crystal cancel; the integrator only trims the residual, with conditional
integration for anti-windup.

The `Correction` probe also holds its step longer (the inner observable is
slower than a fill slope) and steps **away from the nearer ±500 ppm rail**,
normalizing the verdict by the signed step — a fixed `+step` against a host
already near the rail cannot show its full response and reads as a false fail. A
non-compliant host's natural drift runs opposite the away-from-rail step, so it
still fails clearly. `Fill` mode is unchanged.

## Consequences

- The setpoint is the resampler's HELD target, shared with the inner rate
  controller, so the outer loop never fights the inner integrator; the ≥10×
  bandwidth separation is derived in the `jasper-host-clock` module docstring
  and belongs there, not in prose.
- Both observables keep the same sign property, so one feed-forward seed and one
  `response_ratio ≥ 0.5` pass band serve both modes.
- STATUS surfaces `host_clock.obs_mode` and `host_clock.correction_ppm`, so the
  combo end state is directly observable. The `dll` block reads diagnostic zeros
  in `Correction` mode — it is not the controller there.
- Given up: one control law for both modes. The servo-sim tests close the ladder
  against the real `jasper_resampler::RateController` so the composite loop's
  actual dynamics — not a model of them — are what gets pinned.
