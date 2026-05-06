"""Boot-time XVF3800 chip init — `jasper-aec-init`.

Runs as a one-shot systemd unit before jasper-aec-bridge starts.
Two jobs:

  1. REBOOT 1 — clear any prior session's chip state cleanly.
  2. Bring the chip's UAC2 PCM playback level to 0 dB unity. The
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
SpeexDSP cancellation host-side.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("jasper.aec_init")


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
