# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active, audible AEC reference-path diagnostic.

This probe is intentionally separate from the passive doctor harness: it
generates and plays a test signal, waits for bridge telemetry, and therefore
only runs through the explicit ``--probe-aec`` command-line mode.
"""
from __future__ import annotations

import asyncio
import datetime
import errno
import fcntl
import json
import math
import os
import struct
import time
import wave
from contextlib import contextmanager
from typing import Iterator

from ...audio_measurement.correction_lane import (
    correction_play_device,
    run_correction_play,
)
from ...control import client as control
from ...measurement_window import MeasurementWindowError, measurement_window
from ._shared import CheckResult, _run
from .aec import _AEC_MIC_MUSIC_THRESHOLD, _parse_rms_window


# A 5 s, -26 dBFS sine played to the correction lane reaches the bridge's
# `ref` through fan-in, CamillaDSP, outputd, and outputd's UDP speaker
# monitor, then the bridge's 125 Hz HPF + (default) 0 dB pre-gain; it lands
# in the low thousands of RMS there. A broken path stays at roughly 0-50 RMS.
_PROBE_REF_PASS_THRESHOLD = 200
_PROBE_SINE_PATH = "/tmp/jasper-doctor-probe-sine.wav"
_PROBE_SINE_DURATION_S = 5.0
_PROBE_GATE_OWNER = "doctor-aec-probe"


_PROBE_LOCK_PATH = "/run/jasper/doctor-aec-probe.lock"

REASON_PROBE_LOCK_BUSY = "probe_lock_busy"
REASON_PROBE_LOCK_ERROR = "probe_lock_error"
REASON_PROBE_BRIDGE_NOT_RUNNING = "probe_bridge_not_running"
REASON_PROBE_BRIDGE_RUNNING = "probe_bridge_running"
REASON_PROBE_ACTIVE_SOURCE_UNKNOWN = "probe_active_source_unknown"
REASON_PROBE_ACTIVE_SOURCE_BUSY = "probe_active_source_busy"
REASON_PROBE_RENDERERS_IDLE = "probe_renderers_idle"
REASON_PROBE_CONTROL_STATE_UNAVAILABLE = "probe_control_state_unavailable"
REASON_PROBE_ISOLATION_CLEANUP_FAILED = "probe_isolation_cleanup_failed"
REASON_PROBE_ISOLATION_UNAVAILABLE = "probe_isolation_unavailable"
REASON_PROBE_SINE_WRITE_FAILED = "probe_sine_write_failed"
REASON_PROBE_APLAY_FAILED = "probe_aplay_failed"
REASON_PROBE_APLAY_OK = "probe_aplay_ok"
REASON_PROBE_JOURNAL_UNREADABLE = "probe_journal_unreadable"
REASON_PROBE_NO_RMS_WINDOWS = "probe_no_rms_windows"
REASON_PROBE_REF_HEALTHY = "probe_ref_healthy"
REASON_PROBE_REF_SILENT_MIC_LOUD = "probe_ref_silent_mic_loud"
REASON_PROBE_BOTH_SILENT = "probe_both_silent"


class _ProbeLockError(RuntimeError):
    """The standalone probe's cross-process admission lock failed."""


class _ProbeLockBusy(_ProbeLockError):
    """Another standalone AEC probe owns the admission lock."""


class _ProbeIsolationCleanupError(RuntimeError):
    """The probe body completed, but measurement-window teardown failed."""

    def __init__(self, results: list[CheckResult], cause: MeasurementWindowError):
        super().__init__(str(cause))
        self.results = results


@contextmanager
def _probe_lock(path: str | None = None) -> Iterator[int]:
    """Fail-fast process lock for one standalone audible probe.

    The file is never unlinked, avoiding inode-replacement races. CLOEXEC keeps
    ``aplay`` from inheriting ownership; the kernel closes the descriptor and
    releases ``flock`` if the doctor process exits or crashes.
    """

    lock_path = path or _PROBE_LOCK_PATH
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise _ProbeLockError(f"could not open {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise _ProbeLockBusy(
                    "another jasper-doctor --probe-aec run is already active"
                ) from exc
            raise _ProbeLockError(f"could not lock {lock_path}: {exc}") from exc
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def probe_aec_ref_path() -> list[CheckResult]:
    """Play a brief sine and confirm that the bridge reference RMS rises.

    The probe refuses to run over active renderer playback and reports enough
    differential evidence to distinguish a silent reference path from a
    generally inaudible or muted test signal.
    """
    try:
        with _probe_lock():
            return _probe_aec_ref_path_locked()
    except _ProbeLockBusy as exc:
        return [
            CheckResult(
                "probe — exclusive run",
                "fail",
                f"{exc}; wait for it to finish, then re-run. No test tone was played.",
                reason=REASON_PROBE_LOCK_BUSY,
            )
        ]
    except _ProbeLockError as exc:
        return [
            CheckResult(
                "probe — exclusive run",
                "fail",
                f"could not establish the probe process lock ({exc}); check "
                "/run/jasper permissions. No test tone was played.",
                reason=REASON_PROBE_LOCK_ERROR,
            )
        ]


def _probe_aec_ref_path_locked() -> list[CheckResult]:
    """Run prechecks and the audible body under the process lock."""

    results: list[CheckResult] = []

    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        results.append(CheckResult(
            "probe — bridge running", "fail",
            f"bridge state is '{is_active}'; can't probe a stopped bridge. "
            "`systemctl status jasper-aec-bridge`.",
            reason=REASON_PROBE_BRIDGE_NOT_RUNNING,
        ))
        return results
    results.append(CheckResult(
        "probe — bridge running", "ok", "active",
        reason=REASON_PROBE_BRIDGE_RUNNING,
    ))

    try:
        state = control.get_state(timeout=3)
        active = state.get("active_source")
        if not isinstance(active, str):
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                "jasper-control /state did not provide a trustworthy "
                f"active_source ({active!r}); idleness could not be "
                "established, so no test tone was played.",
                reason=REASON_PROBE_ACTIVE_SOURCE_UNKNOWN,
            ))
            return results
        if active != "idle":
            results.append(CheckResult(
                "probe — renderers idle", "fail",
                f"active_source={active!r}; refuse to play the test sine "
                "while audio or a voice session may be active. Stop the "
                "active source and re-run.",
                reason=REASON_PROBE_ACTIVE_SOURCE_BUSY,
            ))
            return results
        # WHICH LAYERS HOLD THE PROPERTY NOW ("never play a test sine over live
        # audio"), stated explicitly because this is hearing-adjacent:
        #
        #  - `_run_isolated_probe()` below is the layer that actually holds it,
        #    and it held it more strongly than this check ever did. Its
        #    `measurement_window` asks mux to EXCLUDE EVERY MUSIC LANE
        #    (`_acquire_measurement_gate` -> `TEST_SELECT`), VERIFIES the landed
        #    state, and mux reasserts that selection once per tick for the whole
        #    window — so even a renderer that starts mid-probe cannot enter the
        #    mix. It is transport-agnostic (mux owns the mix on both couplings)
        #    and it FAILS CLOSED: if the gate cannot be established or proven —
        #    including when mux itself is unreachable — it raises
        #    `MeasurementWindowError` and no tone is played. The deleted check
        #    was a point-in-time read that could go stale between the read and
        #    the play; this one is held for the duration.
        #
        #  - The `active_source` precheck just above is the transport-agnostic
        #    COURTESY refusal: it gives the operator "stop the active source and
        #    re-run" before anything is gated. It catches a playing renderer on
        #    either coupling, because mux elects a winner on every confirmed
        #    inactive->active transition and `/state`'s `active_source` is
        #    derived from mux's `selected_source`/`winner`.
        results.append(CheckResult(
            "probe — renderers idle", "ok", f"active_source={active!r}",
            reason=REASON_PROBE_RENDERERS_IDLE,
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
            reason=REASON_PROBE_CONTROL_STATE_UNAVAILABLE,
        ))
        return results

    try:
        results.extend(asyncio.run(_run_isolated_probe()))
    except _ProbeIsolationCleanupError as exc:
        results.extend(exc.results)
        results.append(
            CheckResult(
                "probe — audio isolation cleanup",
                "fail",
                "the probe body completed; playback outcome is shown above, "
                f"but isolation cleanup failed ({exc}). Household audio may "
                "remain gated until the mux/voice safety leases expire; check "
                "System status before re-running.",
                reason=REASON_PROBE_ISOLATION_CLEANUP_FAILED,
            )
        )
    except MeasurementWindowError as exc:
        results.append(CheckResult(
            "probe — audio isolation", "fail",
            f"could not establish exclusive music and voice isolation ({exc}); "
            "no test tone was played.",
            reason=REASON_PROBE_ISOLATION_UNAVAILABLE,
        ))
    return results


async def _run_isolated_probe() -> list[CheckResult]:
    """Run every audible/probe action while the shared isolation is held."""

    entered = False
    body_results: list[CheckResult] | None = None
    try:
        async with measurement_window(
            gate_owner=_PROBE_GATE_OWNER,
            require_voice_pause=True,
        ):
            entered = True
            # The body uses synchronous subprocess and journal helpers. Keep
            # them off this event loop so both mux and voice leases can renew.
            body = asyncio.create_task(asyncio.to_thread(_play_and_assess_probe))
            body_results, cancelled = await _await_probe_body(body)
            if cancelled:
                raise asyncio.CancelledError
            return body_results
    except MeasurementWindowError as exc:
        if entered and body_results is not None:
            raise _ProbeIsolationCleanupError(body_results, exc) from exc
        raise


async def _await_probe_body(
    body: asyncio.Task[list[CheckResult]],
) -> tuple[list[CheckResult], bool]:
    """Keep isolation held through repeated cancellation until body stops."""

    cancelled = False
    current = asyncio.current_task()
    while not body.done():
        try:
            await asyncio.shield(body)
        except asyncio.CancelledError:
            cancelled = True
            if current is not None:
                current.uncancel()
    if cancelled:
        # Retrieve a worker failure so the task cannot emit an unhandled-
        # exception warning, but cancellation remains the caller's outcome.
        if body.cancelled() or body.exception() is not None:
            raise asyncio.CancelledError
    return body.result(), cancelled


def _play_and_assess_probe() -> list[CheckResult]:
    results: list[CheckResult] = []
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
            reason=REASON_PROBE_SINE_WRITE_FAILED,
        ))
        return results

    probe_start = datetime.datetime.now(datetime.timezone.utc)
    since = probe_start.strftime("%Y-%m-%d %H:%M:%S UTC")
    # No euid gate here: the doctor's documented contract is a root run
    # (`jasper-doctor --help`: "Run as root."); preconditions fail LOUDLY as
    # CheckResults instead of a silent skip. An unprivileged run therefore
    # fails this probe at device open — the aloop path at /dev/snd
    # (root:audio), or the correction ring lane when armed — and the
    # guidance below names the fix.
    play = run_correction_play(
        _PROBE_SINE_PATH,
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
            f"The correction lane resolves to {correction_play_device()!r} on "
            "this box (renderer-lane map). If 'Unknown PCM', re-run "
            "install.sh so the deployed ALSA config defines it; if 'invalid "
            "argument', check the backing transport exists "
            "(/proc/asound/Loopback unarmed, /dev/shm/jts-ring when the "
            "correction lane is armed); if 'Permission denied', run the "
            "doctor as root (its documented contract) — an unprivileged run "
            "cannot open the playback lane.",
            reason=REASON_PROBE_APLAY_FAILED,
        ))
        return results
    results.append(CheckResult(
        "probe — aplay sine", "ok",
        f"{_PROBE_SINE_DURATION_S:.0f} s of {frequency} Hz sine to "
        f"{correction_play_device()}",
        reason=REASON_PROBE_APLAY_OK,
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
            reason=REASON_PROBE_JOURNAL_UNREADABLE,
        ))
        return results

    max_ref = 0
    max_mic = 0
    window_count = 0
    for line in journal.stdout.split("\n"):
        window = _parse_rms_window(line)
        if window is None:
            continue
        window_count += 1
        max_ref = max(max_ref, window.ref)
        max_mic = max(max_mic, window.mic)

    if window_count == 0:
        results.append(CheckResult(
            "probe — ref signal observed", "warn",
            "no bridge rms windows since probe start; bridge may have "
            "stalled or the journal is not capturing INFO-level lines.",
            reason=REASON_PROBE_NO_RMS_WINDOWS,
        ))
        return results
    if max_ref >= _PROBE_REF_PASS_THRESHOLD:
        results.append(CheckResult(
            "probe — ref signal observed", "ok",
            f"max ref={max_ref} across {window_count} windows (threshold "
            f"≥{_PROBE_REF_PASS_THRESHOLD}); reference chain healthy",
            reason=REASON_PROBE_REF_HEALTHY,
        ))
    elif max_mic >= _AEC_MIC_MUSIC_THRESHOLD:
        results.append(CheckResult(
            "probe — ref signal observed", "fail",
            f"max ref={max_ref} (need ≥{_PROBE_REF_PASS_THRESHOLD}) but max "
            f"mic={max_mic} — speaker is reproducing the test tone (mic hears "
            "it) yet ref path is silent. Reference chain is broken.",
            reason=REASON_PROBE_REF_SILENT_MIC_LOUD,
        ))
    else:
        results.append(CheckResult(
            "probe — ref signal observed", "warn",
            f"max ref={max_ref} AND max mic={max_mic} — neither path saw the "
            "test tone. Check that the speaker is on (main_volume not muted), "
            "the Apple dongle is plugged in, and the chip mic isn't muted "
            "(`jasper-doctor` mixer check).",
            reason=REASON_PROBE_BOTH_SILENT,
        ))
    return results
