"""Boot-time XVF3800 chip init — `jasper-aec-init`.

Runs as a one-shot systemd unit before jasper-aec-bridge starts.
Three jobs:

  1. REBOOT 1 — clear any prior session's chip state cleanly.
  2. Set `AEC_HPFONOFF` to apply a chip-side high-pass filter on
     the mic signals before any chip-side DSP. The mic feeds
     openWakeWord (fmin = 60 Hz per Google's speech_embedding
     model) and real-time speech LLMs — no human listens, so
     cutting sub-speech LF rumble is a free win for downstream
     accuracy and removes content AEC3 would otherwise waste
     adaptive-filter capacity trying to cancel. XMOS's shipped
     default for smart-speaker presets is 125 Hz (option 2);
     we match that. Configurable via JASPER_AEC_CHIP_HPF_HZ.
  3. Bring the chip's UAC2 PCM playback level to 0 dB unity. The
     chip's defaults (after REBOOT) put PCM at ~-20 dB, which the
     chip then mirrors into AEC_FAR_EXTGAIN — a non-issue for
     us now (we don't use chip-side AEC) but still good hygiene
     in case anyone re-enables it later.

We do NOT call SAVE_CONFIGURATION — firmware 2.0.6 had a brick
hazard on that op (respeaker repo issue #8). 2.0.8 may have fixed
it but we don't need persistence on the chip side, so we skip.

The historical `AUDIO_MGR_SYS_DELAY` calibration job is gone —
that was for the chip's on-chip AEC, which we abandoned in favor
of software AEC in jasper-aec-bridge. The 6-ch firmware exposes
raw mics on channels 2-5; the bridge takes raw mic 0 and runs
WebRTC AEC3 cancellation host-side.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("jasper.aec_init")

# AEC_HPFONOFF parameter: 0=off, 1=70 Hz, 2=125 Hz, 3=150 Hz, 4=180 Hz.
# All four are 4th-order Butterworth applied at mic ingress before AEC,
# BF, NS in the chip pipeline. Higher cutoff = more aggressive LF
# rejection but nulls more openWakeWord mel bins (model's fmin = 60 Hz,
# so 125 Hz nulls ~2-3 of 32 bins, 180 Hz nulls ~4-5). 125 Hz matches
# XMOS's shipping smart-speaker default and is the production choice
# here; override via JASPER_AEC_CHIP_HPF_HZ in /etc/jasper/jasper.env
# if you want to A/B different cutoffs.
_CHIP_HPF_MAP = {
    "0": 0, "off": 0,
    "70": 1,
    "125": 2,
    "150": 3,
    "180": 4,
}
_DEFAULT_CHIP_HPF_HZ = "125"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aec-init %(levelname)s %(message)s",
    )

    # Lazy import — pyusb pulls in libusb at module load. If the
    # XVF isn't plugged in, we want to log a clean error, not crash
    # the systemd unit on import.
    try:
        from ..xvf import xvf_host
    except Exception as e:  # noqa: BLE001
        logger.error("xvf_host import failed: %s", e)
        return 1

    # The chip can take a few seconds to enumerate after boot. Retry
    # the find() up to 10 times with 1 sec backoff.
    dev = None
    for attempt in range(10):
        dev = xvf_host.find()
        if dev is not None:
            break
        logger.info("XVF3800 not yet on USB, retrying (%d/10)", attempt + 1)
        time.sleep(1)
    if dev is None:
        logger.error("XVF3800 (VID:PID 2886:001a) not found after 10 sec")
        return 1

    try:
        version = dev.read("VERSION")
        logger.info("XVF3800 firmware version: %s", ".".join(str(v) for v in version))

        # REBOOT 1 — clear adaptive-filter state. Per Reachy Mini
        # issue #389, recommended after every host reboot.
        logger.info("REBOOT 1 (clearing AEC adaptive state)")
        dev.write("REBOOT", [1])
        # The chip drops off USB during reboot. Re-find.
        time.sleep(2)
        for attempt in range(10):
            dev = xvf_host.find()
            if dev is not None:
                break
            time.sleep(1)
        if dev is None:
            logger.error("XVF3800 did not re-enumerate after REBOOT")
            return 1

        # Apply chip-side HPF on the mic signal. Lives at mic ingress
        # in the chip pipeline (before AEC, BF, NS — all of which
        # the bridge bypasses by using raw mic 0, but still good
        # hygiene + LF rumble doesn't waste USB bandwidth). XMOS
        # default for smart-speaker presets is on125 (125 Hz, 4th-
        # order Butterworth).
        hpf_hz = os.environ.get("JASPER_AEC_CHIP_HPF_HZ", _DEFAULT_CHIP_HPF_HZ).strip()
        hpf_value = _CHIP_HPF_MAP.get(hpf_hz.lower())
        if hpf_value is None:
            logger.warning(
                "JASPER_AEC_CHIP_HPF_HZ=%r is not one of %s; "
                "falling back to default %s Hz",
                hpf_hz, sorted(_CHIP_HPF_MAP.keys()), _DEFAULT_CHIP_HPF_HZ,
            )
            hpf_value = _CHIP_HPF_MAP[_DEFAULT_CHIP_HPF_HZ]
            hpf_hz = _DEFAULT_CHIP_HPF_HZ
        try:
            dev.write("AEC_HPFONOFF", [hpf_value])
            logger.info(
                "XVF AEC_HPFONOFF set to %s (%s)",
                hpf_value, "off" if hpf_value == 0 else f"{hpf_hz} Hz",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "AEC_HPFONOFF write failed: %s; chip will use its default", e,
            )

        # Set chip's UAC2 PCM playback to 0 dB unity. After REBOOT
        # the chip resets its mixer to ~-20 dB. The XVF firmware
        # auto-mirrors the host's UAC volume into AEC_FAR_EXTGAIN
        # which used to matter when we relied on chip AEC. Now
        # software AEC ignores the chip's USB-IN, but the convention
        # is still cleaner with PCM at unity.
        for ctl in ("PCM,0", "PCM,1"):
            r = subprocess.run(
                ["amixer", "-c", "Array", "sset", ctl, "60", "unmute"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                logger.warning("amixer set %s failed: %s", ctl, r.stderr.strip())
        logger.info("XVF UAC2 PCM volume set to 0 dB unity")
    finally:
        try:
            dev.dev.close()
        except Exception:  # noqa: BLE001
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
