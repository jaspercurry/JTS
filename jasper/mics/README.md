# `jasper/mics/` — per-microphone profiles

One module per supported mic family. Today the only profile is
[`xvf3800.py`](xvf3800.py) (Seeed ReSpeaker XVF3800 USB UA). The
package exists so that mic-family-specific knowledge (USB identity,
ALSA card name, mixer invariants, firmware variants, AEC wiring)
lives in one canonical place instead of being scattered across
doctor checks, the AEC bridge, the reconciler, and BRINGUP.

## Adding a new mic

1. Create `jasper/mics/<family-slug>.py` (e.g. `inmp441.py`,
   `respeaker_4mic_v2.py`).
2. Mirror the fields and helpers from `xvf3800.py` for whatever
   actually applies to your mic. The XVF profile is a reference,
   not an interface — your mic may have no firmware variants, no
   DFU path, or a totally different mixer scheme. Only include what
   you need; do NOT pad with `None` or sentinels just to "match
   the shape."
3. Add it to `PROFILES` in `__init__.py`.
4. If the mic needs to be the active AEC mic, the reconciler
   ([deploy/bin/jasper-aec-reconcile](../../deploy/bin/jasper-aec-reconcile))
   currently hardcodes the XVF card name. It'll need an upgrade —
   either Python-ize it, or split per-mic reconciler scripts. Don't
   speculatively generalize until that lands.

## Why no `MicProfile` interface?

There's exactly one mic in this registry today. Defining a Protocol
or abstract base class from one data point is the over-abstraction
trap — the interface ends up shaped like the first mic and fights
the second one. When a second mic actually lands, compare it to
`xvf3800.py`, factor out what's genuinely common, and only then
define an interface.

This is a deliberate decision, not laziness. See the package
docstring in [`__init__.py`](__init__.py) for the longer rationale.

## Consumers today

- [`jasper.cli.doctor`](../cli/doctor/__init__.py) — the
  `check_xvf_firmware_6ch`, `check_xvf_mixer_state`, and
  `check_aec_bridge_running` functions read constants and call
  helpers from `jasper.mics.xvf3800` (no inline literals).
- [`jasper.cli.aec_bridge`](../cli/aec_bridge.py) — reads
  `ALSA_CARD_NAME`, `MIC_CHANNEL_INDEX`, and the recommended
  channel count from the XVF profile.
- [`deploy/bin/jasper-aec-reconcile`](../../deploy/bin/jasper-aec-reconcile)
  — bash, can't import Python. Carries its own copies of the
  mixer names and channel-count check with a code comment
  pointing back here as the source of truth.

## What this package is NOT

- **Runtime mic auto-detection.** No VID/PID-based dispatch right
  now. Each consumer knows which mic it's talking to. If/when we
  support multiple mic families simultaneously, that's when an
  enumeration layer makes sense.
- **A firmware-flash framework.** DFU vs I2C-update vs no-firmware-
  at-all are all wildly different. The `dfu_flash_command()` helper
  on `xvf3800` is a string-returning convenience for doctor
  messages, not an abstraction.
- **A driver layer.** ALSA and the kernel's snd-usb-audio do the
  driving. Profiles only describe device-specific facts the higher
  levels need to know about.
