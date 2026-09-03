# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The fan-in ALSA lane shared by correction and commissioning playback.

SSOT for both the pcm alias name and the ``aplay`` argv that plays a WAV onto
it: ``WAV -> correction_substream -> jasper-fanin -> CamillaDSP -> outputd``,
one private snd-aloop lane backed by ``hw:Loopback,0,4``
(``deploy/alsa/asoundrc.jasper``). ``tests/test_correction_substream_ssot.py``
is the drift guard.

Homed here rather than in ``jasper.correction`` because that package and
``jasper.active_speaker`` import each other while ``audio_measurement`` is
imported by both and imports neither. Module-scope imports stay stdlib-only:
several consumers are socket-activated wizards that must not pull numpy, and
the same test pins that with a poisoned-``sys.modules`` import guard.

Scope of the drift guard: it scans ``jasper/`` only. Two things legitimately
spell the literal outside it — ``deploy/alsa/asoundrc.jasper``, which declares
the runtime ``pcm.correction_substream`` block and is cross-validated against
this constant by ``jasper.cli.doctor.audio_runtime.check_fanin_asound_wiring``;
and the four stdlib-only ``scripts/aec-probe-*`` lab probes, which must run
standalone off-venv on the Pi and so cannot import ``jasper``.
"""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import os

# The snd-aloop pcm alias correction/commissioning WAV and tone playback is
# routed into. This is the lane's NAME on the shipped snd-aloop transport; the
# device a spawn actually opens is correction_play_device() below, which
# returns this on an unarmed box.
CORRECTION_SUBSTREAM = "correction_substream"

# The umask every correction-lane spawn runs under, passed through
# subprocess's `umask=` (set in the child between fork and exec — never
# `preexec_fn`, which is unsafe in a threaded program, and jasper-web is
# threaded).
#
# 0o007 because a lane writer and jasper-fanin BOTH write the shared ring
# HEADER, so the ring file must come up group-writable (0660) for whichever end
# did not create it — the rule tests/test_renderer_ring_lanes.py pins as
# `UMask=0007` on unit-based ring writers. Correction-lane writers are
# ephemeral aplay spawns from four process identities, so it cannot live in one
# unit file and rides the spawn instead. A root identity's inherited 0022 would
# create the ring 0640 — group-readable, not group-writable — and the non-root
# reader end could not write its header half. Inert on the unarmed aloop path,
# which creates no filesystem objects.
CORRECTION_PLAY_UMASK = 0o007


def correction_play_device() -> str:
    """The ALSA PCM correction-lane playback opens right now, on this box.

    The lane's one transport reader: every spawn here and every other statement
    of the lane device resolves through it, so an armed box cannot spawn on the
    ring while its telemetry reports the substream.

    Resolved per CALL, never cached at import — the writers are ephemeral
    spawns with no daemon to restart, so arming takes effect on the next spawn
    because this reads ``renderer_lanes``' map fresh. An IN-FLIGHT spawn keeps
    the device it opened, so a measurement spanning an arm/disarm flip is lost
    rather than mis-rated; the capture-quality checks reject what it produces.

    ``renderer_lanes`` is imported lazily to keep module import feather-light
    for the socket-activated wizards.
    """
    from jasper import renderer_lanes as rl

    lane = rl.lane_by_label("correction")
    if lane is None:  # fail-safe; provably dead while the registry row exists
        return CORRECTION_SUBSTREAM
    return rl.device_for(lane, "correction" in rl.read_armed_labels())


def correction_play_argv(wav_path: str | os.PathLike[str]) -> list[str]:
    """The ``aplay`` argv that plays one WAV onto the correction lane."""
    return ["aplay", "-D", correction_play_device(), "-q", str(wav_path)]


# The three spawn wrappers stay THIN on purpose: stdio routing and timeouts are
# explicit per-site parameters, and run policy — rc checks, error translation,
# cleanup — stays at each site or in jasper.audio_measurement.playback's
# heavier machinery. What every wrapper DOES own is CORRECTION_PLAY_UMASK, so
# every lane writer inherits the ring-file mode rule in one edit.


def popen_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
) -> subprocess.Popen[bytes]:
    """Spawn a correction-lane ``aplay`` from sync/thread contexts.

    Returns the live handle; callers own its lifecycle.
    ``stdout``/``stderr`` are required so each site states its routing.
    """
    return subprocess.Popen(
        correction_play_argv(wav_path),
        stdout=stdout,
        stderr=stderr,
        umask=CORRECTION_PLAY_UMASK,
    )


async def exec_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
) -> asyncio.subprocess.Process:
    """Spawn a correction-lane ``aplay`` from asyncio contexts.

    Returns the live handle, which the walkthrough flows terminate on
    lock/stop. ``umask=`` reaches the child because ``create_subprocess_exec``
    forwards unknown keywords to :class:`subprocess.Popen`. ``asyncio`` is
    imported lazily so consumers of the sync surfaces never pay for it.
    """
    import asyncio

    return await asyncio.create_subprocess_exec(
        *correction_play_argv(wav_path),
        stdout=stdout,
        stderr=stderr,
        umask=CORRECTION_PLAY_UMASK,
    )


def run_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Play one WAV onto the correction lane, blocking until it exits.

    The root-CLI shape: captured text output, a required per-site ``timeout``,
    and NO rc policy — each site's error translation stays its own.
    """
    return subprocess.run(
        correction_play_argv(wav_path),
        capture_output=True,
        text=True,
        timeout=timeout,
        umask=CORRECTION_PLAY_UMASK,
    )
