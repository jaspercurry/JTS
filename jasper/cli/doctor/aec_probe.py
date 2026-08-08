# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active, audible AEC reference-path diagnostic.

This probe is intentionally separate from the passive doctor harness: it
generates and plays a test signal, waits for bridge telemetry, and therefore
only runs through the explicit ``--probe-aec`` command-line mode.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import struct
import time
import wave

from ...audio_measurement.correction_lane import CORRECTION_SUBSTREAM
from ...control import client as control
from ._shared import CheckResult, _loopback_playback_active, _run
from .aec import _AEC_MIC_MUSIC_THRESHOLD, _AEC_RMS_RE


# A 5 s, -26 dBFS sine through dsnoop + plug + the bridge's 125 Hz HPF +
# (default) 0 dB pre-gain lands in the low thousands of RMS at the bridge's
# `ref`. A broken path stays at roughly 0-50 RMS.
_PROBE_REF_PASS_THRESHOLD = 200
_PROBE_SINE_PATH = "/tmp/jasper-doctor-probe-sine.wav"
_PROBE_SINE_DURATION_S = 5.0


def probe_aec_ref_path() -> list[CheckResult]:
    """Play a brief sine and confirm that the bridge reference RMS rises.

    The probe refuses to run over active renderer playback and reports enough
    differential evidence to distinguish a silent reference path from a
    generally inaudible or muted test signal.
    """
    results: list[CheckResult] = []

    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        results.append(CheckResult(
            "probe — bridge running", "fail",
            f"bridge state is '{is_active}'; can't probe a stopped bridge. "
            "`systemctl status jasper-aec-bridge`.",
        ))
        return results
    results.append(CheckResult("probe — bridge running", "ok", "active"))

    try:
        state = control.get_state(timeout=3)
        active = state.get("active_source")
        if not isinstance(active, str):
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                "jasper-control /state did not provide a trustworthy "
                f"active_source ({active!r}); idleness could not be "
                "established, so no test tone was played.",
            ))
            return results
        if active != "idle":
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                f"active_source={active!r}; refuse to play the test sine "
                "while audio or a voice session may be active. Stop the "
                "active source and re-run.",
            ))
            return results
        if _loopback_playback_active():
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                "a fan-in input lane is currently open in /proc/asound; "
                "refuse to play test sine over active renderer audio. "
                "Stop playback and re-run.",
            ))
            return results
        results.append(CheckResult(
            "probe — renderers idle", "ok", f"active_source={active!r}"
        ))
    except (
        control.ControlError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        results.append(CheckResult(
            "probe — renderers idle", "fail",
            f"jasper-control /state unavailable or malformed ({exc}); "
            "idleness could not be established, so no test tone was played.",
        ))
        return results

    sample_rate = 48_000
    amplitude = 0.05  # -26 dBFS
    frequency = 1_000
    sample_count = int(_PROBE_SINE_DURATION_S * sample_rate)
    samples = bytearray()
    for index in range(sample_count):
        value = int(
            amplitude
            * 32767
            * math.sin(2 * math.pi * frequency * index / sample_rate)
        )
        samples += struct.pack("<hh", value, value)
    try:
        with wave.open(_PROBE_SINE_PATH, "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(samples)
    except OSError as exc:
        results.append(CheckResult(
            "probe — generate sine", "fail",
            f"could not write {_PROBE_SINE_PATH}: {exc}",
        ))
        return results

    probe_start = datetime.datetime.now(datetime.timezone.utc)
    since = probe_start.strftime("%Y-%m-%d %H:%M:%S UTC")
    play = _run(
        ["aplay", "-q", "-D", CORRECTION_SUBSTREAM, _PROBE_SINE_PATH],
        timeout=_PROBE_SINE_DURATION_S + 5.0,
    )
    try:
        os.unlink(_PROBE_SINE_PATH)
    except OSError:
        pass
    if play.returncode != 0:
        results.append(CheckResult(
            "probe — aplay sine", "fail",
            f"aplay failed: {play.stderr.strip() or f'rc={play.returncode}'}. "
            "If 'Unknown PCM', re-run install.sh so /etc/asound.conf defines "
            "correction_substream; if 'invalid argument', check "
            "/proc/asound/Loopback exists.",
        ))
        return results
    results.append(CheckResult(
        "probe — aplay sine", "ok",
        f"{_PROBE_SINE_DURATION_S:.0f} s of {frequency} Hz sine to "
        "correction_substream",
    ))

    time.sleep(6.0)
    journal = _run(
        [
            "journalctl", "-u", "jasper-aec-bridge.service",
            "--since", since, "--no-pager", "--output=cat",
        ],
        timeout=5.0,
    )
    if journal.returncode != 0:
        results.append(CheckResult(
            "probe — bridge journal", "warn",
            f"could not read journal: {journal.stderr.strip()}",
        ))
        return results

    max_ref = 0
    max_mic = 0
    window_count = 0
    for line in journal.stdout.split("\n"):
        match = _AEC_RMS_RE.search(line)
        if not match:
            continue
        window_count += 1
        max_ref = max(max_ref, int(match.group(1)))
        max_mic = max(max_mic, int(match.group(2)))

    if window_count == 0:
        results.append(CheckResult(
            "probe — ref signal observed", "warn",
            "no bridge rms windows since probe start; bridge may have "
            "stalled or the journal is not capturing INFO-level lines.",
        ))
        return results
    if max_ref >= _PROBE_REF_PASS_THRESHOLD:
        results.append(CheckResult(
            "probe — ref signal observed", "ok",
            f"max ref={max_ref} across {window_count} windows (threshold "
            f"≥{_PROBE_REF_PASS_THRESHOLD}); reference chain healthy",
        ))
    elif max_mic >= _AEC_MIC_MUSIC_THRESHOLD:
        results.append(CheckResult(
            "probe — ref signal observed", "fail",
            f"max ref={max_ref} (need ≥{_PROBE_REF_PASS_THRESHOLD}) but max "
            f"mic={max_mic} — speaker is reproducing the test tone (mic hears "
            "it) yet ref path is silent. Reference chain is broken. "
            "See docs/HANDOFF-aec.md § 'Lessons learned' #6.",
        ))
    else:
        results.append(CheckResult(
            "probe — ref signal observed", "warn",
            f"max ref={max_ref} AND max mic={max_mic} — neither path saw the "
            "test tone. Check that the speaker is on (main_volume not muted), "
            "the Apple dongle is plugged in, and the chip mic isn't muted "
            "(`jasper-doctor` mixer check).",
        ))
    return results
