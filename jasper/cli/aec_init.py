"""Boot-time XVF3800 chip init — `jasper-aec-init`.

Runs as a one-shot systemd unit before jasper-aec-bridge starts.
Three jobs:

  1. Set `SHF_BYPASS=1` to disable the chip's on-board AEC stage.
     The chip's AEC was designed for the topology where the chip
     drives the speaker via its own codec; in our external-DAC
     topology, the chip's AEC reference path is sabotaged (see
     docs/HANDOFF-aec.md). With SHF_BYPASS=1, the chip's AEC
     adaptive filter is removed from channels 0/1's signal path,
     but the rest of the chip pipeline — beamforming, NS, AGC,
     HPF — still runs. Software AEC3 (jasper-aec-bridge) handles
     echo cancellation host-side using the music chain as ref.
  2. Set `AEC_HPFONOFF` to apply a chip-side high-pass filter on
     the mic signals before any chip-side DSP. The mic feeds
     openWakeWord (fmin = 60 Hz per Google's speech_embedding
     model) and real-time speech LLMs — no human listens, so
     cutting sub-speech LF rumble is a free win. XMOS's shipped
     smart-speaker default is 125 Hz (option 2); we match that.
     Configurable via JASPER_AEC_CHIP_HPF_HZ.
  3. Bring the chip's UAC2 PCM playback level to 0 dB unity.
     The chip's default for these mixer controls is ~-20 dB,
     which would attenuate any audio the host sent to the chip's
     USB-IN. We don't currently route audio that way (software
     AEC ignores the chip's USB-IN entirely), but the convention
     is still cleaner with PCM at unity in case future tuning
     experiments use the chip's USB-IN as a reference path.

We do NOT call SAVE_CONFIGURATION — firmware 2.0.6 had a brick
hazard on that op (respeaker repo issue #8). 2.0.8 may have fixed
it but we don't need persistence on the chip side, so we skip.

We also do NOT call REBOOT, even though some XVF reference designs
(e.g. Reachy Mini #389) recommend it on host boot. In our pipeline
the chip's AEC adaptive filter is disabled (SHF_BYPASS=1 above), so
"clear adaptive-filter state" — REBOOT's only documented benefit
in that recipe — is moot. The three writes below are idempotent
and overwrite whatever values the chip currently holds, so REBOOT
adds nothing but a ~3 s chip-side outage. Calling REBOOT was also
the root cause of the 2026-05-16 USB-renumerate feedback loop:
every REBOOT triggered a USB disconnect, which fired the
`controlC*` udev rule, which restarted aec-init, which called
REBOOT again. See docs/HANDOFF-aec.md "Lessons learned" #9.

The historical `AUDIO_MGR_SYS_DELAY` calibration job is gone —
that was for the chip's on-chip AEC, which we don't use (see
SHF_BYPASS above). The 6-ch firmware exposes raw mics on
channels 2-5; the bridge captures channel 1 (ASR beam, with
chip BF/NS/AGC/HPF applied) and runs WebRTC AEC3 host-side.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

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

        # NB: no REBOOT here. The writes below are idempotent and
        # overwrite the chip's current state directly; the prior
        # REBOOT step is removed because it triggered a USB
        # renumeration feedback loop with the controlC* udev rule.
        # See docs/HANDOFF-aec.md "Lessons learned" #9.

        # Disable the chip's on-board AEC. SHF_BYPASS=1 removes the
        # AEC adaptive filter from the signal path on channels 0/1
        # (beamforming, NS, AGC, HPF all stay). We do this because
        # the chip's AEC was designed for the topology where the
        # chip drives the speaker via its own codec; in our
        # external-USB-DAC topology, the chip's AEC reference path
        # is sabotaged (the chip mirrors the host's UAC volume into
        # AEC_FAR_EXTGAIN, which attenuates the reference by an
        # unpredictable amount). Software AEC3 in jasper-aec-bridge
        # handles echo cancellation host-side instead. See
        # docs/HANDOFF-aec.md for the full investigation.
        try:
            dev.write("SHF_BYPASS", [1])
            logger.info("XVF SHF_BYPASS=1 (chip AEC stage disabled)")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "SHF_BYPASS write failed: %s; chip AEC may still be "
                "in the signal path (bridge will compensate with sw AEC)", e,
            )

        # Apply chip-side HPF on the mic signal. Lives at mic ingress
        # in the chip pipeline (before AEC, BF, NS). The HPF affects
        # the processed output channels (0/1) — which is now what
        # the bridge captures (see jasper/mics/xvf3800.py
        # MIC_CHANNEL_INDEX=1). XMOS default for smart-speaker
        # presets is on125 (125 Hz, 4th-order Butterworth).
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

        # Set chip's UAC2 PCM playback to 0 dB unity. The chip's
        # default for these mixer controls is ~-20 dB. The XVF
        # firmware auto-mirrors the host's UAC volume into
        # AEC_FAR_EXTGAIN which used to matter when we relied on
        # chip AEC. Now software AEC ignores the chip's USB-IN, but
        # the convention is still cleaner with PCM at unity.
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
