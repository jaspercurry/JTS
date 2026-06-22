# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Boot-time XVF3800 chip init — `jasper-aec-init`.

Runs as a one-shot systemd unit before jasper-aec-bridge starts.
Three jobs:

  1. Restore the software-AEC fallback profile (`SHF_BYPASS=1`) when
     chip-AEC is not requested.
     The chip's AEC was designed for the topology where the chip
     drives the speaker via its own codec; in our external-DAC
     topology, the chip's AEC reference path is sabotaged (see
     docs/HANDOFF-aec.md). With SHF_BYPASS=1, the chip's AEC
     adaptive filter is removed from channels 0/1's signal path.
     Empirically, this bypasses the SHF post-processing path too, so
     channels 0/1 become raw-ish mic feeds rather than beamformed /
     NS / AGC outputs. In the `xvf_software_aec3` fallback profile,
     software AEC3 (jasper-aec-bridge) handles echo cancellation
     host-side using the music chain as ref.
     Chip-AEC mode is the narrow exception: the wake toggle sets
     `JASPER_AEC_CHIP_AEC_ENABLED=1` for production, while the recorder
     sets `JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1` for labeled corpus
     comparison. In either mode this init unit applies and read-back
     verifies a volatile 150/210 fixed-beam chip profile. Production
     init explicitly restores the normal bypassed mux/profile switches
     whenever those flags are absent so exit does not depend on
     rebooting the XVF.
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
the chip's AEC adaptive filter is disabled for production
(SHF_BYPASS=1 above), so "clear adaptive-filter state" — REBOOT's
only documented benefit in that recipe — is normally moot. Corpus
chip-AEC comparison mode still avoids REBOOT because the profile is
volatile, idempotent, and should not create a USB re-enumeration event
mid-session. Calling REBOOT was also
the root cause of the 2026-05-16 USB-renumerate feedback loop:
every REBOOT triggered a USB disconnect, which fired the
`controlC*` udev rule, which restarted aec-init, which called
REBOOT again. See docs/HANDOFF-aec.md "Lessons learned" #9.

The historical boot-time `AUDIO_MGR_SYS_DELAY` calibration job is gone.
Chip-AEC production/corpus modes set their own volatile delay/profile
from env. The 6-ch firmware exposes raw mics on channels 2-5; in the
software-AEC fallback profile the bridge captures channel 1 as a raw-ish
chip feed and runs WebRTC AEC3 host-side. In chip-AEC mode, the bridge
captures channels 0/1 as chip ASR beams and forwards them directly.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence

from jasper.log_event import log_event
from jasper.mics import xvf3800

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


def _chip_beam_plan() -> xvf3800.ChipBeamPlan | None:
    return xvf3800.chip_beam_plan_from_env(os.environ)


def _chip_corpus_profile(
    plan: xvf3800.ChipBeamPlan,
) -> tuple[tuple[str, list[int | float]], ...]:
    return (
        ("SHF_BYPASS", [0]),
        ("AUDIO_MGR_SYS_DELAY", [12]),
        ("AEC_ASROUTONOFF", [1]),
        ("AEC_ASROUTGAIN", [1.0]),
        ("AEC_FIXEDBEAMSONOFF", [1]),
        ("AEC_FIXEDBEAMSGATING", [1]),
        ("AEC_FIXEDBEAMSAZIMUTH_VALUES", [leg.azimuth_rad for leg in plan.legs]),
        ("AEC_FIXEDBEAMSELEVATION_VALUES", [leg.elevation_rad for leg in plan.legs]),
        ("AEC_AECEMPHASISONOFF", [2]),
        ("AEC_FAR_EXTGAIN", [0.0]),
        ("AUDIO_MGR_OP_L", [7, 0]),
        ("AUDIO_MGR_OP_R", [7, 1]),
    )


_CHIP_PRODUCTION_PROFILE: tuple[tuple[str, list[int | float]], ...] = (
    ("SHF_BYPASS", [1]),
    ("AEC_ASROUTONOFF", [0]),
    ("AEC_FIXEDBEAMSONOFF", [0]),
    ("AEC_FIXEDBEAMSGATING", [0]),
    ("AEC_AECEMPHASISONOFF", [0]),
    ("AEC_FAR_EXTGAIN", [0.0]),
    ("AUDIO_MGR_OP_L", [8, 0]),
    # The bridge consumes XVF capture channel 1 in production. The
    # firmware default for the right USB channel is silence, so restore
    # it to the same non-silent user-chosen beam route as channel 0.
    ("AUDIO_MGR_OP_R", [8, 0]),
)
_VERIFY_FLOAT_TOLERANCE = 1e-4


class ChipProfileError(RuntimeError):
    """Raised when a required volatile XVF profile write did not stick."""


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _values_match(expected: Sequence[int | float], actual: object) -> bool:
    if actual is None:
        return False
    if not isinstance(actual, Sequence) or isinstance(actual, str | bytes):
        actual_values: Sequence[object] = (actual,)
    else:
        actual_values = actual
    if len(actual_values) != len(expected):
        return False
    for want, got in zip(expected, actual_values, strict=True):
        if isinstance(want, float):
            if abs(float(got) - want) > _VERIFY_FLOAT_TOLERANCE:
                return False
        elif int(got) != want:
            return False
    return True


def _write_required(dev, param: str, values: list[int | float]) -> None:
    try:
        dev.write(param, values)
        actual = dev.read(param)
    except Exception as e:  # noqa: BLE001
        raise ChipProfileError(f"{param}={values} failed: {e}") from e
    if not _values_match(values, actual):
        raise ChipProfileError(
            f"{param} readback mismatch: wrote {values}, read {actual}"
        )
    log_event(
        logger,
        "chip_profile_write",
        param=param,
        values=values,
        verified=1,
    )


def _write_best_effort(dev, param: str, values: list[int | float]) -> None:
    try:
        dev.write(param, values)
        log_event(
            logger,
            "chip_profile_write",
            param=param,
            values=values,
            verified=0,
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "chip_profile_write_failed",
            param=param,
            error=e,
            level=logging.WARNING,
        )


def _apply_required_profile(
    dev,
    profile: Sequence[tuple[str, list[int | float]]],
) -> None:
    for param, values in profile:
        _write_required(dev, param, values)


def _corpus_profile_with_delay(
    plan: xvf3800.ChipBeamPlan,
    sys_delay: int,
) -> tuple[tuple[str, list[int | float]], ...]:
    return tuple(
        (param, [sys_delay] if param == "AUDIO_MGR_SYS_DELAY" else values)
        for param, values in _chip_corpus_profile(plan)
    )


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
        try:
            dev = xvf_host.find()
        except xvf_host.XvfControlError as e:
            log_event(
                logger,
                "xvf_control_unavailable",
                error=e,
                level=logging.ERROR,
            )
            return 1
        if dev is not None:
            break
        logger.info("XVF3800 not yet on USB, retrying (%d/10)", attempt + 1)
        time.sleep(1)
    if dev is None:
        logger.error(
            "XVF3800 (VID:PID %s) not found after 10 sec",
            "/".join(xvf3800.USB_VID_PIDS),
        )
        return 1

    try:
        version = dev.read("VERSION")
        logger.info("XVF3800 firmware version: %s", ".".join(str(v) for v in version))

        # NB: no REBOOT here. The writes below are idempotent and
        # overwrite the chip's current state directly; the prior
        # REBOOT step is removed because it triggered a USB
        # renumeration feedback loop with the controlC* udev rule.
        # See docs/HANDOFF-aec.md "Lessons learned" #9.

        corpus_chip_aec = _env_truthy("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED")
        production_chip_aec = _env_truthy("JASPER_AEC_CHIP_AEC_ENABLED")
        if corpus_chip_aec or production_chip_aec:
            beam_plan = _chip_beam_plan()
            if beam_plan is None:
                log_event(
                    logger,
                    "chip_profile_failed",
                    mode="corpus" if corpus_chip_aec else "chip_aec",
                    error="no validated chip beam plan for detected XVF geometry",
                    level=logging.ERROR,
                )
                return 1
            mode = "corpus" if corpus_chip_aec else "chip_aec"
            delay_env = (
                "JASPER_AEC_CORPUS_CHIP_SYS_DELAY"
                if corpus_chip_aec else "JASPER_AEC_CHIP_SYS_DELAY"
            )
            sys_delay = int(os.environ.get(delay_env, "12"))
            logger.info(
                "applying chip-AEC %s profile "
                "(beam_plan=%s, sys_delay=%d)",
                mode, beam_plan.plan_id, sys_delay,
            )
            try:
                _apply_required_profile(
                    dev,
                    _corpus_profile_with_delay(beam_plan, sys_delay),
                )
            except ChipProfileError as e:
                log_event(
                    logger,
                    "chip_profile_failed",
                    mode=mode,
                    error=e,
                    level=logging.ERROR,
                )
                return 1
            log_event(
                logger,
                "chip_profile_applied",
                mode=mode,
                shf_bypass=0,
                sys_delay=sys_delay,
                op_l="7,0",
                op_r="7,1",
            )
        else:
            # Restore the software-AEC fallback profile. SHF_BYPASS=1
            # removes the AEC adaptive filter from the signal path on
            # channels 0/1. Empirically this bypasses the SHF
            # post-processing path too, so channels 0/1 are raw-ish chip
            # feeds; software AEC3 in jasper-aec-bridge handles fallback
            # cancellation when chip-AEC is unavailable or disabled.
            # Also restore the output mux and corpus-only beam/AEC
            # switches that wake-corpus mode writes. These commands are
            # volatile, but "exit corpus mode" must be deterministic
            # without requiring a reboot.
            try:
                _apply_required_profile(dev, _CHIP_PRODUCTION_PROFILE)
            except ChipProfileError as e:
                log_event(
                    logger,
                    "chip_profile_failed",
                    mode="production",
                    error=e,
                    level=logging.ERROR,
                )
                return 1
            log_event(
                logger,
                "chip_profile_applied",
                mode="production",
                shf_bypass=1,
                op_l="8,0",
                op_r="8,0",
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
        card = xvf3800.alsa_card_name()
        for ctl in ("PCM,0", "PCM,1"):
            r = subprocess.run(
                ["amixer", "-c", card, "sset", ctl, "60", "unmute"],
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
