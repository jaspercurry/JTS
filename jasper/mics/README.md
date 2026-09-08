# `jasper/mics/` — microphone reference

[`xvf3800.py`](xvf3800.py) owns XVF3800 USB identities, ALSA card names,
mixer controls, firmware variants, geometry, capture channels and chip beam
plans. It is the only microphone family profile.

## Capture support and validation

Recognized firmware is not proof of capture performance or a validated chip
beam plan. The profile recognizes legacy square/circular and Flex linear
and circular variants, each with 2- and 6-channel firmware.

- **Legacy square/circular, 6-channel:** the registered production chip-AEC
  plan is `xvf_square_fixed_150_210`. Its two fixed ASR beams use channels
  0 and 1. Chip AEC also needs the output reference and commissioning state
  selected by the reconciler.
- **Flex, 6-channel:** software AEC3 fallback is implemented. No production
  chip beam plan is registered for either Flex geometry. Recognition and
  fallback support do not establish measured wake or echo performance.
- **2-channel:** direct capture is implemented; the 6-channel bridge does
  not open this endpoint.
- **Other microphones and Pi Zero voice performance:** evidence gaps.
  Direct microphone configuration does not imply a generic software-AEC3
  capture path. The Pi Zero 2 W `streambox` profile excludes local wake/mic/AEC;
  a paired remote with a microphone enables push-to-talk assistant use.

The software-AEC main input uses `MIC_CHANNEL_INDEX` (channel 1) with
`SHF_BYPASS=1`; it does not select raw mic 0. The separate raw0 leg uses
channel 2. Channels 2–5 carry raw microphones, while channels 0/1 depend on
the active chip profile. Channel count alone does not make firmware variants
interchangeable. See [BRINGUP](../../BRINGUP.md#xvf-firmware-switch-to-6-channel-variant-via-dfu)
for flashing and commissioning.

## Consumers

- [`jasper.cli.doctor`](../cli/doctor/__init__.py) reads firmware, mixer and
  bridge status through the profile.
- [`jasper.aec.bridge_capture`](../aec/bridge_capture.py) reads capture
  geometry; [`jasper.cli.aec_bridge`](../cli/aec_bridge.py) also consumes
  chip beam plans.
- [`jasper.cli.xvf_profile`](../cli/xvf_profile.py) publishes the detected
  profile as JSON or shell-safe env assignments, including supported card
  names and mixer controls.
- [`jasper-aec-reconcile`](../../deploy/bin/jasper-aec-reconcile) consumes
  that CLI output to select capture, repair the mixer and arm chip AEC.

## Adding a microphone

Add one concrete family module with the facts that hardware needs, then wire
it into detection and reconciliation. Reuse existing profile consumers where
applicable; include capture and performance evidence for the new path.
