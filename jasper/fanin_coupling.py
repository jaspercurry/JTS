# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in → CamillaDSP coupling selector (``JASPER_FANIN_CAMILLA_COUPLING``).

The single source of truth for HOW the fan-in mixer's summed program reaches
CamillaDSP's capture. Three transports (``loopback`` default; ``transport_pipe``
and ``shm_ring`` are LAB flags, inert in every product path):

- ``loopback`` (the **default**) — fan-in writes the ALSA snd-aloop substream
  (``hw:Loopback,0,7``); CamillaDSP captures ``plug:jasper_capture`` (a dsnoop
  on ``hw:Loopback,1,7``). This is exactly today's production topology. With the
  flag unset or set to ``loopback``, both the fan-in daemon and the emitted
  CamillaDSP capture block are **byte-for-byte** what shipped before this module
  existed — proven by tests on both sides.

- ``transport_pipe`` — fan-in writes a bounded named pipe into CamillaDSP
  ``RawFile`` capture, and CamillaDSP writes its post-DSP stereo program to a
  second pipe read by jasper-outputd. CamillaDSP ``enable_rate_adjust`` is off
  and no async resampler is emitted: the pipes are transport only, with the
  outputd blocking DAC write as the single pace root.

This module is import-cheap (stdlib only) so socket-activated web surfaces and
the config emitters can resolve the coupling without pulling in NumPy/SciPy.

**Format split (load-bearing).** fan-in mixes and outputs S16_LE internally
(``mixer.rs`` ``FORMAT = Format::S16LE``). The shared CamillaDSP capture format
is S32_LE (``DEFAULT_CAPTURE_FORMAT``; the ``plug:`` widens the loopback's S16
to S32 today). So under ``transport_pipe`` the fan-in writer MUST widen each i16
sample to i32 before writing the pipe, and the emitted RawFile capture declares
S32_LE. The wire format is pinned here as :data:`PIPE_WIRE_FORMAT` so the Rust
producer and the Python config consumer can never disagree.

The CamillaDSP -> outputd local pipe is also S32_LE, even though ordinary ALSA
playback remains S16_LE. JTS has 16 KiB kernel pages, so a FIFO cannot shrink
below 16 KiB; S32_LE stereo halves that floor from 4096 frames (~85 ms) to 2048
frames (~43 ms). outputd down-converts to i16 at the final DAC write boundary.

**Why a separate flag from the lean lane.** The lean lane
(``JASPER_LEAN_LANE`` / ``stage_lean_capture_config`` /
``apply_lean_capture_config``) swaps CamillaDSP's capture to a File pipe fed by
ONE exclusive wired source (usbsink), bypassing the mixer entirely. This
coupling keeps the FULL fan-in mixer (all renderer lanes, TTS, ducking,
music-only tap) and changes the local program transport on both sides of
Camilla. They are different points on the same convergence: once
``transport_pipe`` soaks, it supersedes the lean lane's separate File-capture
path. **Do not delete the lean lane or the adaptive output-buffer shrink yet** —
they are superseded *after* this soaks, not before.
"""

from __future__ import annotations

from jasper.camilla_config_contract import (
    DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE,
    DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT,
)

# Environment selector. Read at config-emit time and at fan-in daemon startup.
COUPLING_ENV_VAR = "JASPER_FANIN_CAMILLA_COUPLING"

# The accepted transports. ``loopback`` is the default and the
# byte-identical-to-today path.
COUPLING_LOOPBACK = "loopback"
COUPLING_TRANSPORT_PIPE = "transport_pipe"
# Ring A (prototype): fan-in writes an SPSC ping-pong SHM ring
# (``jasper_ring::RingWriter``) that CamillaDSP reads via a CAPTURE direction of
# the ``jts_ring`` ioplug. Same SHM contract v1 as Ring B; roles flipped. Like
# ``transport_pipe`` this is a LAB flag — inert in every product path (env unset
# resolves to ``loopback``), armed only by ``scripts/ring-proto/`` for the soak.
# The Rust ``Coupling::ShmRing`` normalizer MUST agree with this token.
COUPLING_SHM_RING = "shm_ring"
# The recognized coupling tokens. Public so other planners (e.g.
# ``jasper.audio_runtime_plan``) can reuse this SSOT instead of re-listing the
# tokens and drifting when a new lab coupling lands (Ring A's ``shm_ring`` was
# exactly that drift: the plan kept an independent {loopback, transport_pipe}
# set and false-warned on the new flag). ``_VALID_COUPLINGS`` stays as the
# backward-compatible private alias.
VALID_COUPLINGS = frozenset(
    {COUPLING_LOOPBACK, COUPLING_TRANSPORT_PIPE, COUPLING_SHM_RING}
)
_VALID_COUPLINGS = VALID_COUPLINGS

# The shared-capture named pipe written by fan-in under ``transport_pipe`` and
# RawFile-read by CamillaDSP. DISTINCT from the lean lane's FIFO
# (``DEFAULT_LEAN_CAPTURE_FIFO`` = /run/jasper-usbsink/lean.pipe), which is fed
# by usbsink, not the mixer. Lives under the fan-in daemon's own /run dir (tmpfs,
# recreated each boot) so the producer owns it. Overridable via
# ``JASPER_FANIN_CAMILLA_PIPE`` for the soak.
PIPE_PATH_ENV_VAR = "JASPER_FANIN_CAMILLA_PIPE"
DEFAULT_FANIN_CAMILLA_PIPE = "/run/jasper-fanin/camilla.pipe"
OUTPUTD_PIPE_PATH_ENV_VAR = "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE"

# Wire format on the pipe. fan-in mixes S16 internally but widens to S32_LE on
# the wire so the RawFile capture matches the SHARED capture format (S32_LE), the
# same width the proven usbsink lean writer emits. Pinned here so producer and
# consumer can never drift.
PIPE_WIRE_FORMAT = "S32_LE"

# Ring A (``shm_ring``) SHM ring file + slot-count env vars. fan-in creates the
# ring at ``JASPER_FANIN_RING_PATH`` with ``JASPER_FANIN_RING_SLOTS`` slots; the
# Rust daemon resolves both with the SAME defaults (see ``config.rs``). The
# n_slots <-> JASPER_FANIN_RING_SLOTS pairing is the drift axis with the ioplug
# conf.d geometry; the ring header's own validation is the runtime fail-loud
# backstop.
RING_PATH_ENV_VAR = "JASPER_FANIN_RING_PATH"
DEFAULT_FANIN_RING_PATH = "/dev/shm/jts-ring/program.ring"
RING_SLOTS_ENV_VAR = "JASPER_FANIN_RING_SLOTS"
DEFAULT_FANIN_RING_SLOTS = 8

# Ring A capture device + wire format. fan-in is S16 native and the SHM ring
# carries S16LE with NO widening (unlike ``transport_pipe``'s S32 FIFO-page
# floor, an SHM ring has none). CamillaDSP captures it as an ALSA device named
# by the ioplug conf.d block. Pinned here so the hand generator
# (``make-camilla-ring-config.sh`` capture-swap mode) and the Rust writer stay
# one SSOT — no product emitter path consumes these.
RING_CAPTURE_DEVICE = "jts_ring_capture"
RING_WIRE_FORMAT = "S16_LE"


def resolve_coupling(raw: str | None) -> str:
    """Normalize a raw ``JASPER_FANIN_CAMILLA_COUPLING`` value to a transport.

    Fail-SAFE to ``loopback`` (the byte-identical-to-today path) on unset, empty,
    or any unrecognized value — a typo in the env file must never silently flip
    the shared realtime capture to a transport the operator did not intend, nor
    crash a config emit. The Rust daemon applies the same normalization so both
    sides agree on every recognized token (``loopback`` / ``transport_pipe`` /
    ``shm_ring``). Case-insensitive; surrounding whitespace ignored.
    """
    if raw is None:
        return COUPLING_LOOPBACK
    value = raw.strip().lower()
    if value in _VALID_COUPLINGS:
        return value
    return COUPLING_LOOPBACK


def is_transport_pipe_coupling(raw: str | None) -> bool:
    """True iff the resolved coupling is ``transport_pipe``."""
    return resolve_coupling(raw) == COUPLING_TRANSPORT_PIPE


def is_shm_ring_coupling(raw: str | None) -> bool:
    """True iff the resolved coupling is ``shm_ring`` (Ring A)."""
    return resolve_coupling(raw) == COUPLING_SHM_RING


def resolve_ring_path(raw_path: str | None) -> str:
    """Resolve the Ring A SHM ring file path from a raw env value.

    Empty / unset → :data:`DEFAULT_FANIN_RING_PATH`. Trims whitespace. The Rust
    daemon resolves ``JASPER_FANIN_RING_PATH`` the same way so the writer and the
    ioplug conf.d block name the same ring file.
    """
    if raw_path is None:
        return DEFAULT_FANIN_RING_PATH
    value = raw_path.strip()
    return value or DEFAULT_FANIN_RING_PATH


RING_SLOTS_MIN = 2
RING_SLOTS_MAX = 16


def resolve_ring_slots(raw_slots: str | None) -> int:
    """Resolve the Ring A n_slots from a raw env value.

    Empty / unset → :data:`DEFAULT_FANIN_RING_SLOTS`. A present-but-out-of-range
    or unparseable value FAILS LOUD (:class:`ValueError`) rather than silently
    clamping — a shear-prone geometry (the ioplug conf.d block and the daemon
    would disagree on the ring depth) must never ship, and repo doctrine is
    fail-loud on a bad operator value. This MUST agree with the Rust daemon,
    which ``anyhow::bail!``s on the same ``JASPER_FANIN_RING_SLOTS`` range: the
    n_slots <-> JASPER_FANIN_RING_SLOTS pairing is the drift axis the ring header
    also validates at attach. The range :data:`RING_SLOTS_MIN`..=
    :data:`RING_SLOTS_MAX` mirrors the ring header's ``MIN_N_SLOTS`` /
    ``MAX_N_SLOTS`` and ``config.rs``'s ``RING_SLOTS_MIN`` / ``RING_SLOTS_MAX``.
    """
    if raw_slots is None:
        return DEFAULT_FANIN_RING_SLOTS
    stripped = raw_slots.strip()
    if not stripped:
        return DEFAULT_FANIN_RING_SLOTS
    try:
        value = int(stripped)
    except ValueError as exc:
        raise ValueError(
            f"{RING_SLOTS_ENV_VAR}={raw_slots!r} is not an integer; the SHM ring "
            "slot count must be a whole number"
        ) from exc
    if RING_SLOTS_MIN <= value <= RING_SLOTS_MAX:
        return value
    raise ValueError(
        f"{RING_SLOTS_ENV_VAR}={raw_slots!r} out of range "
        f"{RING_SLOTS_MIN}..={RING_SLOTS_MAX} — a shear-prone SHM ring geometry "
        "must fail loud, not silently clamp (the ioplug conf.d block and the "
        "daemon would disagree on the ring depth)"
    )


def capture_kwargs_for_coupling(
    raw: str | None,
    *,
    pipe_path: str | None = None,
    outputd_pipe_path: str | None = None,
) -> dict[str, object]:
    """Return the ``emit_sound_config`` capture kwargs for the resolved coupling.

    - ``loopback`` (default): returns ``{}`` so the caller's existing
      ``capture_device`` / ``capture_format`` defaults emit the dsnoop ALSA
      capture — **byte-identical** to today. This empty-dict contract is what
      keeps every existing caller unchanged when the flag is unset.

    - ``transport_pipe``: returns the dual-pipe kwargs — ``capture_pipe_path``
      for fan-in -> Camilla RawFile, ``playback_pipe_path`` for Camilla ->
      outputd File playback, ``enable_rate_adjust=False``, and
      ``transport_paced_pipe=True``. No Camilla async resampler is emitted.

    - ``shm_ring`` (Ring A, prototype): returns the ALSA capture-device kwargs —
      ``capture_device=jts_ring_capture`` (the ioplug conf.d name) and
      ``capture_format=S16_LE`` (fan-in is S16 native; the SHM ring carries S16LE
      with no widening). This is the SSOT the hand generator
      (``make-camilla-ring-config.sh`` capture-swap mode) reads so Python and the
      Rust writer agree on the capture side. Like ``transport_pipe``, these kwargs
      DO flow through :func:`coupling_capture_kwargs_from_env` into the product
      emitters (``/sound/``, ``/correction/``,
      ``audio_runtime_plan.apply_capture_precedence``) — but only when the lab
      flag :data:`COUPLING_ENV_VAR`\\ =``shm_ring`` is set in the env. This is
      deliberate coherence-when-armed: on an armed lab box a household
      ``/sound/`` save emits a CamillaDSP config whose capture device is
      ``jts_ring_capture``, so the emitted config and the running daemon name the
      SAME ring. The capture device only RESOLVES once the arm script has
      installed the ioplug ``jts_ring_capture`` conf.d block; until then the flag
      must stay unset (env unset -> ``loopback`` -> ``{}``, byte-identical to
      today). It is inert in every product path while the flag is unset.

    ``pipe_path`` overrides the capture pipe path (the env override is resolved by
    :func:`resolve_pipe_path`; pass its result here so the emitted config and the
    daemon point at the same pipe).
    """
    resolved = resolve_coupling(raw)
    if resolved == COUPLING_SHM_RING:
        return {
            "capture_device": RING_CAPTURE_DEVICE,
            "capture_format": RING_WIRE_FORMAT,
        }
    if resolved != COUPLING_TRANSPORT_PIPE:
        return {}
    return {
        "capture_pipe_path": pipe_path or DEFAULT_FANIN_CAMILLA_PIPE,
        "playback_pipe_path": outputd_pipe_path or DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE,
        "resampler_type": None,
        "resampler_profile": None,
        "enable_rate_adjust": False,
        "transport_paced_pipe": True,
        "playback_format": DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT,
    }


def resolve_pipe_path(raw_path: str | None) -> str:
    """Resolve the shared-capture pipe path from a raw env value.

    Empty / unset → :data:`DEFAULT_FANIN_CAMILLA_PIPE`. Trims whitespace. The
    Rust daemon resolves ``JASPER_FANIN_CAMILLA_PIPE`` the same way so the
    producer and the RawFile-capture consumer always name the same pipe.
    """
    if raw_path is None:
        return DEFAULT_FANIN_CAMILLA_PIPE
    value = raw_path.strip()
    return value or DEFAULT_FANIN_CAMILLA_PIPE


def resolve_outputd_pipe_path(raw_path: str | None) -> str:
    """Resolve the Camilla -> outputd local content pipe path."""
    if raw_path is None:
        return DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE
    value = raw_path.strip()
    return value or DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE


def coupling_capture_kwargs_from_env(
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Resolve the live ``emit_sound_config`` capture kwargs from the process env.

    The one call shape a config emitter uses to thread the SHARED fan-in→Camilla
    coupling into a live re-emit: reads :data:`COUPLING_ENV_VAR` +
    :data:`PIPE_PATH_ENV_VAR` together so the emitted RawFile-capture config names
    the SAME pipe the Rust ``FifoWriter`` writes (the path env is resolved by
    :func:`resolve_pipe_path` on BOTH sides). Returns ``{}`` for the default
    ``loopback`` coupling (byte-identical to today). Read at emit time — a
    systemd ``EnvironmentFile`` flip takes effect on the next config regeneration
    without a code edit, exactly like the CamillaDSP latency knobs.
    """
    import os

    source = os.environ if env is None else env
    return capture_kwargs_for_coupling(
        source.get(COUPLING_ENV_VAR),
        pipe_path=resolve_pipe_path(source.get(PIPE_PATH_ENV_VAR)),
        outputd_pipe_path=resolve_outputd_pipe_path(
            source.get(OUTPUTD_PIPE_PATH_ENV_VAR)
        ),
    )


def member_kwargs_are_pipe_sink(member_kwargs: dict[str, object] | None) -> bool:
    """True when the resolved grouping member kwargs are a SnapFIFO pipe sink.

    A bonded/grouped member (active-leader program bake, or a passive grouping
    follower leader) writes CamillaDSP's playback to the Snapcast pipe with
    ``enable_rate_adjust=False`` (snapclient is the sole rate-tracker — the
    multiroom inv-5). That is mutually exclusive with the local transport-pipe
    topology, which also wants to own Camilla's playback pipe. So when this is
    True, the local coupling must be a no-op for that emit (the grouped topology
    is the Distributed-Active track's concern, not this solo-speaker latency hop).
    The solo defaults (``enable_rate_adjust`` truthy / absent, no
    ``playback_pipe_path``) return False → coupling applies. Mirrors
    ``jasper.multiroom.member_config``'s leader-vs-solo distinction without
    importing it (keeps this module import-cheap for the socket-activated
    emitters).
    """
    if not member_kwargs:
        return False
    if member_kwargs.get("playback_pipe_path"):
        return True
    # An explicit enable_rate_adjust=False is the pipe-sink signal even if the
    # path resolution is deferred; treat it as a sink to stay fail-safe.
    return member_kwargs.get("enable_rate_adjust") is False
