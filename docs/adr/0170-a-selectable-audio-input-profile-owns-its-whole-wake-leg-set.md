# ADR-0170: A selectable audio-input profile owns its whole wake-leg set

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Each armed wake leg costs a resident Silero/openWakeWord instance. On a 1 GB
Pi that is not free, and the XVF3800 can emit three chip beams (the primary
session beam plus fixed 150°/210° beams) at once. The optional beams were
independent `JASPER_WAKE_LEG_CHIP_AEC_*` booleans, so a box that had once
enabled them for a lab comparison kept running three detectors after the
operator selected a profile that says "the chip does the echo cancellation."
Nothing on `/aec` or `/wake/` made that visible, and the RAM went unattributed.

## Decision

**Selecting a profile applies that profile's complete leg set, including
turning off legs the profile does not name.** `auto`, `xvf_chip_aec`,
`xvf_chip_aec_testing`, `xvf_software_aec3`, and `direct_mic` each write every
`JASPER_WAKE_LEG_*` key, resetting the optional chip beams to `0`
(`jasper/audio_profile_state.py`). `custom` is the only profile that preserves
whatever the low-level booleans say — it exists precisely to be the lab escape.

The reconciler stays the only writer of concrete `JASPER_MIC_DEVICE*` values,
so an armed leg and a real UDP carrier cannot disagree; `jasper-aec-bridge`
emits an optional beam only when its runtime device env exists; and doctor and
audio-validation fail on a wake leg the applied profile did not ask for.

## Consequences

- The default chip-AEC install runs exactly one detector, and the RAM cost of a
  profile is derivable from its name rather than from accumulated history.
- Enabling a fixed beam for a comparison is a deliberate two-step: switch to
  `custom`, then set the boolean. Switching back to a named profile silently
  discards it — that is the point, but it means lab state does not survive a
  profile change.
- Rejected: leaving the optional beams orthogonal to profile selection and
  merely warning about them. A warning that fires on a box nobody is looking at
  is not a budget.
