# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The fan-in ALSA lane shared by correction and commissioning playback.

**This module is the single source of truth for the ALSA pcm alias name**
room correction's measurement sweeps/tones and active-speaker commissioning's
isolated-driver/summed captures both play into:

    WAV -> correction_substream -> jasper-fanin -> CamillaDSP -> outputd

``correction_substream`` is one private snd-aloop lane (``/etc/asound.conf``,
backed by ``hw:Loopback,0,4``) shared by two features. Before this module
existed the name was independently re-declared as ``DEFAULT_ALSA_DEVICE``,
``CORRECTION_SUBSTREAM``, ``COMMISSION_TONE_ALSA_DEVICE``,
``VOLUME_FLOOR_TONE_ALSA_DEVICE``, and two identically-named
``PLAYBACK_DEVICE`` constants across ``jasper.correction``,
``jasper.active_speaker``, ``jasper.web``, and ``jasper.cli`` — five distinct
names spread across eleven files with no shared constant and no test pinning
that the copies agreed (issue #1789). A rename would have silently diverged.
``tests/test_correction_substream_ssot.py`` is the drift guard.

Why this module lives in ``jasper.audio_measurement`` and not
``jasper.correction``: ``jasper.correction`` and ``jasper.active_speaker``
import each other (the same fact
:mod:`jasper.audio_measurement.room_boundary` documents for the room-
correction band edge), so neither package is "below" the other and homing a
shared constant in either would be an arbitrary pick some consumer has to
reach sideways for. ``jasper.audio_measurement`` is imported by both while
importing neither, and is already the documented shared kernel for
primitives every tuning layer reuses (see this package's own
``__init__.py``).

It is also the *lightweight* choice, which is why the constant does not
simply stay in ``jasper.correction.playback`` (the previous "canonical"
site): that module, and ``jasper.audio_measurement.playback`` beneath it,
import ``numpy`` at module scope for WAV/DSP mechanics. Several consumers of
this constant are socket-activated wizard modules that must stay light
(``jasper.web.sound_setup``, ``jasper.web.sync_flow``,
``jasper.web.balance_flow`` — see AGENTS.md "Web wizard conventions") plus
``jasper.active_speaker.web_commissioning`` — none of which imported numpy
before this refactor, and none should start now. This module's only
module-scope import beyond ``__future__`` is the stdlib ``subprocess``
(``asyncio`` is imported lazily inside the one async helper below), so
reading the constant or calling the play helpers costs those wizards
nothing heavy. That promise is not just stated: it is pinned by
``tests/test_correction_substream_ssot.py``'s poisoned-subprocess guard,
which imports each of those four modules with ``numpy``/``scipy`` poisoned
in ``sys.modules`` and asserts the import still succeeds — the same
technique catches a stray convenience re-export added to THIS module's own
package ``__init__.py`` (``jasper/audio_measurement/__init__.py``), not just
a direct import in a consumer.

The play helpers (P6c-0)
------------------------
This module also owns **building the ``aplay`` argv** that plays a WAV onto
the correction lane, plus two thin spawn wrappers over it — the same
consolidation issue #1789 applied to the device *name*, applied to the
device *spawn*. Before P6c-0 (campaign #2285, U3 arc) ten call sites across
six files each assembled their own inline ``["aplay", ..., "-D", <lane>,
...]`` argv; a lane-name change (or the P6c lane migration itself) would
have meant ten synchronized edits. Now:

* :func:`correction_play_argv` is the **single owner** of the argv (binary
  + ``-q`` + ``-D`` + device + WAV). P6c-ii makes the device here
  lane-map-aware (SHM ring lane instead of the static snd-aloop alias) —
  one edit in this module covers every spawn that routes through these
  helpers. That is the SPAWN's device, not every statement of the lane
  fact: P6c-ii must also sweep the consumers that pass
  :data:`CORRECTION_SUBSTREAM` (under their aliases) into the heavier
  playback machinery — ``web_commissioning``'s ``play_sweep``
  ``alsa_device=`` arguments, ``commissioning_verification``'s ``play_wav``
  call, ``program_playback.verified_program_aplay``'s parameter default,
  and ``correction.playback``'s ``DEFAULT_ALSA_DEVICE`` re-export plus its
  function defaults — and the nine observability payloads reporting
  ``"audio_device": {"pcm": ...}`` (four in ``jasper.web.sound_setup``,
  five in ``web_commissioning``), which would otherwise keep reporting a
  lane the spawn no longer uses.
* :func:`popen_correction_play` (sync/thread contexts) and
  :func:`exec_correction_play` (asyncio contexts, returns a terminable
  handle) are deliberately thin: stdio routing stays an explicit per-site
  parameter, and neither adds timeout/retry/error policy — the sites that
  need richer semantics (deadman timeouts, cleanup state machines,
  cancellable tone loops) already route through the heavier shared
  machinery in :mod:`jasper.audio_measurement.playback` (``play_wav`` /
  ``play_sweep`` / ``TonePlayer`` / ``play_verified_wav``), which keeps its
  policy-free ``alsa_device`` parameter (see "What is NOT owned here").
* The root-CLI writers (``jasper.cli.aec_commission``,
  ``jasper.cli.doctor.aec_probe``) keep their module-local ``_run``
  wrappers — those wrappers ARE the site-owned run policy (commissioning's
  ``CommissioningError`` translation, the doctor's plain-``subprocess.run``
  convention) — and feed them argv from :func:`correction_play_argv`
  instead of a hand-spelled list.
* ``tests/test_correction_lane_play.py`` pins the argv/kwargs of every
  migrated site shape and carries the conventions guard that fails any NEW
  inline ``aplay``/``-D`` spawn outside this module's allowlist. The five
  stdlib-only lab probe scripts under ``scripts/`` remain exempt by design
  (see "Scope of the drift guard" above — same exemption, same reason).

The ``quiet_before_device`` knob exists because the pre-P6c-0 sites used
two flag orders — the web/wizard family spelled ``aplay -D <dev> -q`` and
the root-CLI family spelled ``aplay -q -D <dev>``. ``aplay`` treats both
identically; P6c-0 is bit-for-bit argv-preserving by contract, so the knob
reproduces each site's historical order rather than silently normalizing.
Normalizing to one order is a P6c-i/-ii decision, not this refactor's.

Scope of the drift guard
-------------------------
``tests/test_correction_substream_ssot.py`` only scans ``jasper/``. Two
things legitimately spell the literal outside that scope, on purpose:

* ``deploy/alsa/asoundrc.jasper`` declares the actual ``pcm.correction_substream``
  block the system's ALSA config reads at runtime. It is not Python, so it
  cannot import this constant — but it is not disconnected from this module
  either: :func:`jasper.cli.doctor.audio_runtime.check_fanin_asound_wiring` reads
  :data:`CORRECTION_SUBSTREAM` as the expected-alias dict key it checks
  ``/etc/asound.conf`` against, and ``tests/test_doctor_audio_runtime.py``'s
  hand-maintained
  ``_FANIN_ASOUND`` fixture mirrors ``asoundrc.jasper``'s
  ``pcm.correction_substream`` block byte-for-byte. So the Python constant and
  the checked-in ALSA config stay cross-validated through that doctor check,
  even though the AST guard here never reads either non-Python file.
* Five stdlib-only AEC lab-probe scripts under ``scripts/`` — ``aec-probe-
  timing.py``, ``aec-probe-latency.sh``, ``aec-probe-xvf-ref-level.sh``,
  ``aec-probe-pinknoise.sh``, ``chip-aec-baseline-check.sh`` — spell the
  literal by design. They are meant to run standalone (bash, or Python using
  only the stdlib), often copied straight onto the Pi for isolated hardware
  debugging outside the project venv; importing ``jasper`` would defeat that.

What is NOT owned here
-----------------------
* **A default on the neutral playback surface.**
  :mod:`jasper.audio_measurement.playback`'s ``play_wav`` and
  ``ensure_sine_wav`` deliberately take no ``alsa_device`` / ``cache_dir``
  default — that neutral leaf is policy-free by design (see its own module
  docstring: "Feature owners choose the ALSA lane"). Giving it a default,
  even by importing :data:`CORRECTION_SUBSTREAM` into it, would re-inject a
  feature-owned opinion into shared mechanics every tuning layer reuses. This
  module is where the constant lives, never a reason to reach INTO the
  neutral leaf and give it one. Pinned by
  ``tests/test_audio_measurement_playback.py::
  test_neutral_surface_requires_owner_policy``, which asserts
  ``not hasattr(playback, "DEFAULT_ALSA_DEVICE")``.
* **The derived commission-tone backend tags.** ``COMMISSION_TONE_BACKEND``,
  ``SUMMED_COMMISSION_SPEECH_BACKEND``, ``DRIVER_CAPTURE_SWEEP_BACKEND``, and
  ``SUMMED_CAPTURE_SWEEP_BACKEND`` in
  :mod:`jasper.active_speaker.web_commissioning` hold compound strings like
  ``"correction_substream_continuous_tone"``. They share this lane's name as
  a prefix but are a different vocabulary (event/backend tags, not the ALSA
  device), so they are deliberately NOT routed here. (One of the four,
  ``SUMMED_COMMISSION_SPEECH_BACKEND``, has its own independent-declaration
  drift bug — see issue #1957 — which is a separate fix, not a reason to
  fold that family into this module.)
"""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import os

# The snd-aloop pcm alias correction/commissioning WAV and tone playback is
# routed into before jasper-fanin -> CamillaDSP -> outputd. Declared exactly
# once — every other reference imports this name rather than re-declaring
# the string. jasper.correction.playback re-exports it as the historical
# DEFAULT_ALSA_DEVICE name for one external compatibility import
# (jasper.active_speaker.commissioning_host); every other consumer imports
# CORRECTION_SUBSTREAM directly from here.
CORRECTION_SUBSTREAM = "correction_substream"


def correction_play_argv(
    wav_path: str | os.PathLike[str],
    *,
    quiet_before_device: bool = False,
) -> list[str]:
    """The ``aplay`` argv that plays one WAV onto the correction lane.

    Single owner of the lane-play command line (module docstring, "The play
    helpers"). Device resolution is the P6c-ii seam: when the correction/
    test lane moves from the snd-aloop alias onto an SHM ring lane, the
    SPAWN's device becomes lane-map-aware here — and the module docstring
    names the other device-fact sites P6c-ii must sweep alongside this one
    (heavier-machinery ``alsa_device=`` consumers and the observability
    payloads that report the lane).

    ``quiet_before_device`` reproduces the two historical flag orders
    (``aplay -q -D <dev> <wav>`` for the root-CLI family, ``aplay -D <dev>
    -q <wav>`` — the default, matching :mod:`jasper.audio_measurement.
    playback`'s own spawns — for the web/wizard family). Both are identical
    to ``aplay``; see the module docstring for why P6c-0 preserves rather
    than normalizes them.
    """
    wav = str(wav_path)
    if quiet_before_device:
        return ["aplay", "-q", "-D", CORRECTION_SUBSTREAM, wav]
    return ["aplay", "-D", CORRECTION_SUBSTREAM, "-q", wav]


# The two spawn wrappers stay THIN on purpose: stdio routing is an explicit
# per-site parameter (no hidden default redirection), and run policy —
# timeouts, rc checks, error translation, cleanup — stays at each site or in
# jasper.audio_measurement.playback's heavier machinery. P6c-i's spawn-time
# policy hook (the ring-lane umask for group-writable SHM files) lands in
# these wrappers, next to the argv owner, so every lane writer inherits it
# in one edit.


def popen_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
    quiet_before_device: bool = False,
) -> subprocess.Popen[bytes]:
    """Spawn a correction-lane ``aplay`` from sync/thread contexts.

    Returns the live :class:`subprocess.Popen` handle — callers own its
    lifecycle (poll / terminate / wait), exactly as they did with their
    pre-P6c-0 inline ``subprocess.Popen`` calls. ``stdout``/``stderr`` are
    required so each site states its routing: the wizard sites pass
    ``subprocess.DEVNULL``; the operator CLI (``jasper.cli.aec_tune``)
    passes ``None`` to keep aplay's stderr on the operator's terminal.
    """
    return subprocess.Popen(
        correction_play_argv(wav_path, quiet_before_device=quiet_before_device),
        stdout=stdout,
        stderr=stderr,
    )


async def exec_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
) -> asyncio.subprocess.Process:
    """Spawn a correction-lane ``aplay`` from asyncio contexts.

    Returns the live :class:`asyncio.subprocess.Process` handle — the
    walkthrough flows (``jasper.web.sync_flow``, ``jasper.web.
    balance_flow``) need a handle they can terminate on lock/stop, which is
    exactly why they never routed through ``play_sweep``'s
    blocking-to-completion shape (see balance_flow's ``_start_playback``).

    ``asyncio`` is imported lazily so wizard modules that only read
    :data:`CORRECTION_SUBSTREAM` or use the sync wrapper never pay for it
    at import time (module docstring, placement promise).
    """
    import asyncio

    return await asyncio.create_subprocess_exec(
        *correction_play_argv(wav_path),
        stdout=stdout,
        stderr=stderr,
    )
