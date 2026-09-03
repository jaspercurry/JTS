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
``jasper.web.balance_flow`` — see ``tests/test_web_wizard_conventions.py``) plus
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

The play helpers (P6c-0, P6c-i, P6c-ii)
---------------------------------------
This module also owns **building the ``aplay`` argv** that plays a WAV onto
the correction lane, plus three thin spawn wrappers over it — the same
consolidation issue #1789 applied to the device *name*, applied to the
device *spawn*. Before P6c-0 (campaign #2285, U3 arc) ten call sites across
six files each assembled their own inline ``["aplay", ..., "-D", <lane>,
...]`` argv; a lane-name change (or the P6c lane migration itself) would
have meant ten synchronized edits. Now:

* :func:`correction_play_device` is the lane's **one transport reader**
  (P6c-ii): it resolves, fresh on every call, whether this box's
  ``correction`` lane is armed onto its SHM ring
  (``jasper.renderer_lanes`` — the same lane map jasper-fanin reads) and
  returns the ring ``plug:`` PCM or the shipped
  :data:`CORRECTION_SUBSTREAM` snd-aloop alias accordingly.
  :func:`correction_play_argv` builds the argv on top of it, so every
  spawn through these wrappers follows the map with no restart — and every
  OTHER statement of the lane device resolves through the same reader:
  ``web_commissioning``'s ``play_sweep`` ``alsa_device=`` arguments,
  ``commissioning_verification``'s ``play_wav`` call,
  ``program_playback.verified_program_aplay``, ``correction.playback``'s
  play surfaces, and the nine ``"audio_device": {"pcm": ...}``
  observability payloads (four in ``jasper.web.sound_setup``, five in
  ``web_commissioning``) — swept in P6c-ii precisely so an armed box can
  never spawn on the ring while its telemetry reports the substream.
* :func:`popen_correction_play` (sync/thread contexts),
  :func:`exec_correction_play` (asyncio contexts, returns a terminable
  handle), and :func:`run_correction_play` (blocking run-to-completion with
  captured output, the root-CLI shape) are deliberately thin: stdio routing
  and timeouts stay explicit per-site parameters, and none adds
  retry/error policy — the sites that need richer semantics (deadman
  timeouts, cleanup state machines, cancellable tone loops) already route
  through the heavier shared machinery in
  :mod:`jasper.audio_measurement.playback` (``play_wav`` / ``play_sweep`` /
  ``TonePlayer`` / ``play_verified_wav``), which keeps its policy-free
  ``alsa_device`` parameter (see "What is NOT owned here").
* **Every wrapper spawns with** :data:`CORRECTION_PLAY_UMASK` (P6c-i) —
  see that constant's comment for the full story. This is why the
  root-CLI writers (``jasper.cli.aec_commission``, ``jasper.cli.doctor.
  aec_probe``) moved from builder-fed module-local ``_run`` wrappers
  (P6c-0's shape) onto :func:`run_correction_play`: a umask is spawn-time
  state, not argv, and the ring FILE's mode matters whatever the writer's
  uid — a root CLI whose spawn kept the root shell's 0022 would create a
  ring the non-root reader end cannot write its header half into
  (``tests/test_renderer_ring_lanes.py``'s umask pin states the same rule
  for unit-based writers). Their error policies stayed at the sites:
  commissioning still translates a nonzero rc into ``CommissioningError``
  (now beside its call), and the doctor still reads the returned
  ``CompletedProcess`` exactly as ``_shared._run`` produced it — the
  kwargs are byte-identical minus the added ``umask``.
* ``tests/test_correction_lane_play.py`` pins the argv/kwargs of every
  migrated site shape and carries the conventions guard that fails any NEW
  inline ``aplay``/``-D`` spawn outside this module's allowlist. The four
  stdlib-only lab probe scripts under ``scripts/`` remain exempt by design
  (see "Scope of the drift guard" above — same exemption, same reason).

Argv order: P6c-0 preserved each site's historical flag order bit-for-bit
behind a ``quiet_before_device`` knob (the web family spelled ``aplay -D
<dev> -q``, the root-CLI family ``aplay -q -D <dev>`` — identical to
``aplay``, which parses flags positionally-independently). P6c-i retired
the knob, normalizing on the web-family order (also
:mod:`jasper.audio_measurement.playback`'s own order) — the normalization
the P6c-0 review banked as safe once the refactor step no longer needed
byte-identical argv.

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
* Four stdlib-only AEC lab-probe scripts under ``scripts/`` — ``aec-probe-
  timing.py``, ``aec-probe-latency.sh``, ``aec-probe-xvf-ref-level.sh``,
  ``aec-probe-pinknoise.sh`` — spell the literal by design. They are meant to run standalone (bash, or Python using
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
# the string (jasper.correction.playback re-exports it under the same name
# for its Room-compat surface). This is the lane's NAME on the shipped
# snd-aloop transport; the device a spawn actually opens is
# correction_play_device() below, which returns this on an unarmed box.
CORRECTION_SUBSTREAM = "correction_substream"

# The umask every correction-lane spawn runs under (P6c-i), passed to the
# child via subprocess's `umask=` parameter (Python >= 3.9; set in the child
# between fork and exec — no `preexec_fn`, which is documented unsafe in
# threaded programs, and jasper-web is threaded).
#
# Why 0o007: a lane writer and jasper-fanin BOTH write the shared ring
# HEADER, so a ring file must come up group-writable (0660) for whichever
# end did not create it — the same rule tests/test_renderer_ring_lanes.py
# pins as `UMask=0007` on every unit-based ring writer. For the correction
# lane the writers are EPHEMERAL aplay spawns from four different process
# identities (jasper-web wizards, the root jasper-correction-web, the root
# streambox variant, root operator CLIs), so the umask cannot live in one
# unit file — it rides the spawn instead, HERE, beside the argv owner.
#
# Per-spawn beats per-unit in both directions that matter:
#   * jasper-correction-web keeps its deliberately tight `UMask=0077` for
#     the correction profiles/bundles it writes itself; this child-only
#     override loosens nothing but the spawned player.
#   * A root identity's inherited 0022 (shell or systemd default) would
#     create the ring 0640 — group-READABLE, not group-writable — and the
#     non-root reader end could not write its header half.
#
# What it governs depends on this box's lane transport
# (:func:`correction_play_device`): on an ARMED box the lane PCM is the ring
# ioplug and the ring FILE is created INSIDE aplay's process — this umask is
# what makes it group-writable. On the unarmed/fleet-default aloop path,
# aplay opens the snd-aloop pcm and reads the WAV; the
# `correction_substream` chain (`plug` over `hw:Loopback,0,4`,
# deploy/alsa/asoundrc.jasper) creates no filesystem objects, so the umask
# governs nothing there — inert, byte-identical to pre-P6c behaviour.
CORRECTION_PLAY_UMASK = 0o007


def correction_play_device() -> str:
    """The ALSA PCM correction-lane playback opens right now, on this box.

    **The lane's one transport reader** (P6c-ii): every spawn in this
    module resolves its device here, and so does every other statement of
    the lane device (the heavier-machinery ``alsa_device=`` consumers and
    the observability payloads — module docstring, "The play helpers"), so
    an armed box cannot spawn on the ring while its telemetry reports the
    substream.

    Resolution is per CALL, never cached at import: the writers are
    ephemeral spawns with no daemon to restart, so arming takes effect on
    the NEXT spawn purely because this reads the lane map fresh
    (``renderer_lanes.read_armed_labels`` — the same file jasper-fanin
    loads) and derives the device through the same ``device_for`` rule the
    rendered map and the arm CLI's report use. One derivation, three
    consumers, zero drift surface.

    Mid-flip semantics, stated: an IN-FLIGHT spawn keeps the device it
    opened — the argv is baked at spawn and a flip cannot reach a running
    process. A measurement spanning an arm/disarm flip is therefore LOST,
    never mis-rated and never loud: in the arm direction the old spawn's
    aloop audio goes nowhere fan-in is reading; in the disarm direction
    the readerless ring free-runs and drops frames (bounded and
    non-blocking by the ring contract; the writer flock releases on
    process exit). Flips are operator-initiated (`jasper-audio-config
    renderer-lanes`, which prints the fan-in restart instruction), and the
    correction pipeline's own capture-quality checks reject the
    truncated/absent capture such a spanned measurement produces.

    ``renderer_lanes`` is imported lazily: at import time this module stays
    feather-light for the socket-activated wizards (placement promise
    above); at call time the lane machinery is stdlib-only.

    The registry-miss fallback exists to fail SAFE, not to be reachable:
    if the ``correction`` row were ever deleted from ``RENDERER_LANES``
    (a dev-time regression the lane pins catch), spawns degrade to the
    shipped aloop alias — which every box defines — instead of crashing
    every wizard measurement at runtime.
    """
    from jasper import renderer_lanes as rl

    lane = rl.lane_by_label("correction")
    if lane is None:  # fail-safe; provably dead while the registry row exists
        return CORRECTION_SUBSTREAM
    return rl.device_for(lane, "correction" in rl.read_armed_labels())


def correction_play_argv(wav_path: str | os.PathLike[str]) -> list[str]:
    """The ``aplay`` argv that plays one WAV onto the correction lane.

    Single owner of the lane-play command line (module docstring, "The play
    helpers"). The device comes from :func:`correction_play_device` — the
    snd-aloop alias on an unarmed box (the fleet default, byte-identical to
    pre-P6c behaviour), the lane's ring ``plug:`` PCM on an armed one.

    One flag order for every caller since P6c-i — see "Argv order" in the
    module docstring for the retirement of P6c-0's per-site order knob.
    """
    return ["aplay", "-D", correction_play_device(), "-q", str(wav_path)]


# The three spawn wrappers stay THIN on purpose: stdio routing and timeouts
# are explicit per-site parameters (no hidden default redirection), and run
# policy — rc checks, error translation, cleanup — stays at each site or in
# jasper.audio_measurement.playback's heavier machinery. What every wrapper
# DOES own is the spawn-time lane policy: CORRECTION_PLAY_UMASK rides each
# spawn so every lane writer inherits the ring-file mode rule in one edit.


def popen_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
) -> subprocess.Popen[bytes]:
    """Spawn a correction-lane ``aplay`` from sync/thread contexts.

    Returns the live :class:`subprocess.Popen` handle — callers own its
    lifecycle (poll / terminate / wait), exactly as they did with their
    pre-P6c-0 inline ``subprocess.Popen`` calls. ``stdout``/``stderr`` are
    required so each site states its routing: every current caller (the
    wizard sites) passes ``subprocess.DEVNULL``, and ``None`` inherits the
    parent's stdio for a foreground operator CLI.
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

    Returns the live :class:`asyncio.subprocess.Process` handle — the
    walkthrough flows (``jasper.web.sync_flow``, ``jasper.web.
    balance_flow``) need a handle they can terminate on lock/stop, which is
    exactly why they never routed through ``play_sweep``'s
    blocking-to-completion shape (see balance_flow's ``_start_playback``).

    ``umask=`` reaches the child because ``create_subprocess_exec`` forwards
    unknown keywords to the underlying :class:`subprocess.Popen`
    (pinned empirically by the mechanism tests in
    ``tests/test_correction_lane_play.py``).

    ``asyncio`` is imported lazily so wizard modules that only read
    :data:`CORRECTION_SUBSTREAM` or use the sync wrapper never pay for it
    at import time (module docstring, placement promise).
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

    The root-CLI shape (``jasper.cli.aec_commission``, ``jasper.cli.doctor.
    aec_probe``): captured text output, a required per-site ``timeout`` (no
    default — every caller states its bound), and NO rc policy — the
    returned :class:`subprocess.CompletedProcess` is exactly what those
    sites' module-local ``_run`` wrappers produced
    (``subprocess.run(argv, capture_output=True, text=True, timeout=...)``),
    so each site's error translation stays its own. The one addition over
    P6c-0's builder-fed shape is :data:`CORRECTION_PLAY_UMASK` on the
    spawn — see the module docstring for why that had to move the spawn
    itself into the helper.
    """
    return subprocess.run(
        correction_play_argv(wav_path),
        capture_output=True,
        text=True,
        timeout=timeout,
        umask=CORRECTION_PLAY_UMASK,
    )
