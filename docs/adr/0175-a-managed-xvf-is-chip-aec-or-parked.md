# ADR-0175: A managed XVF is chip-AEC or parked — no label bypasses commissioning

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

The XVF3800's hardware AEC only works when the DAC's clock domain is coherent
with the chip's USB-IN reference path, which is a *measured* property of the
particular DAC, not a property of its name. Meanwhile the profile vocabulary
grew several labels that look like escape hatches — `xvf_chip_aec_testing`,
`xvf_software_aec3`, `direct_mic` — and any of them silently "working" on a
managed XVF would mean a speaker that hears itself, with no evidence trail
explaining why the chip path was abandoned.

## Decision

**On a managed XVF, resolution is chip-AEC or an actionable park. Nothing
downgrades it implicitly.** Concretely:

- `auto` on a managed XVF resolves to the commissioned fixed `xvf_chip_aec`
  path, or parks with a reason and a remediation action. Supported hardware
  plus a persisted alignment artifact is required; missing, invalid, or
  unstable evidence parks.
- `xvf_chip_aec_testing` is a status/compatibility label with no authority to
  run an uncommissioned chip. `xvf_software_aec3` and `direct_mic` remain
  first-class for **non-XVF** microphones and cannot be selected on a managed
  XVF.
- `custom` is the sole lab escape, and it is explicit: the low-level
  `JASPER_WAKE_LEG_*` booleans own the leg set there
  ([ADR-0170](0170-a-selectable-audio-input-profile-owns-its-whole-wake-leg-set.md)).
- A DAC label alone never authorizes activation. The decisive gate is measured
  drift/delay stability; a passive readiness artifact is evidence, not
  permission.

## Consequences

- A speaker with an unqualified DAC is loudly unusable for voice rather than
  quietly bad at it, which matches the "no silent deafness" posture: parking
  carries a reason and an action.
- Adding a new DAC costs a commissioning run. That is the intended price of the
  chip path, and it is why `hifiberry_dac8x_outputd_stability` exists as a
  narrower content-pipeline soak that does not need chip-AEC at all.
- The known-good XVF3800 + HiFiBerry DAC8x path is allowed to read as
  operator-OK on clean passive evidence, so the common install does not demand
  a 30-minute measurement. Unknown DAC paths still warn until drift/delay
  evidence exists.
- Rejected: automatic fallback from chip-AEC to software AEC3 on a managed XVF.
  It converts a hardware problem into an invisible quality regression, and it
  destroys the evidence that would have explained the park.
