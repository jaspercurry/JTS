# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in → CamillaDSP coupling selector (``JASPER_FANIN_CAMILLA_COUPLING``).

The single source of truth for HOW the fan-in mixer's summed program reaches
CamillaDSP's capture. Two transports:

- ``loopback`` — fan-in writes the ALSA snd-aloop substream
  (``hw:Loopback,0,7``); CamillaDSP captures ``plug:jasper_capture`` (a dsnoop
  on ``hw:Loopback,1,7``). With the flag unset or set to ``loopback``, both the
  fan-in daemon and the emitted CamillaDSP capture block stay on the historical
  snd-aloop topology. This is the fail-safe rung of the ladder.

- ``shm_ring`` — the end-to-end SHM-ring path (Ring A + Ring B); see the ring
  vocabulary below and :mod:`jasper.fanin.coupling_reconcile` for the ordered
  transition. This is the hardware-validated product default the ``--auto``
  reconciler resolves eligible solo boxes to.

This module is import-cheap (stdlib only) so socket-activated web surfaces and
the config emitters can resolve the coupling without pulling in NumPy/SciPy.

**Removed 2026-07-11 — the ``transport_pipe`` coupling.** A third transport
(fan-in → bounded named pipe → CamillaDSP ``RawFile`` capture → File playback
pipe → outputd) was a default-off lab path for low latency. It was never
selected by ``--auto`` (which resolves only ``shm_ring`` / ``loopback``) and was
hardware-demoted 2026-07-01 (the 16 KiB Pi kernel page floor made its FIFOs too
deep); ``shm_ring`` now ships as the frame-bounded default that replaced its
diagnostic value. It has been deleted (fan-in ``Output::Fifo`` + ``fifo.rs``,
outputd ``local_content_pipe``, the reconciler arm/gate branches, doctor
validation, and the ``JASPER_FANIN_CAMILLA_PIPE`` /
``JASPER_OUTPUTD_LOCAL_CONTENT_PIPE`` env keys). A persisted
``JASPER_FANIN_CAMILLA_COUPLING=transport_pipe`` now FAILS SAFE to ``loopback``
via :func:`resolve_coupling`, and the ``--auto`` reconciler converges it loudly
(see :func:`coupling_value_removed` and
``jasper.fanin.coupling_reconcile.reconcile_auto``).
"""

from __future__ import annotations

# Environment selector. Read at config-emit time and at fan-in daemon startup.
COUPLING_ENV_VAR = "JASPER_FANIN_CAMILLA_COUPLING"

# The accepted transports. ``loopback`` is the default and the
# byte-identical-to-today path.
COUPLING_LOOPBACK = "loopback"
# Ring A: fan-in writes an SPSC SHM ring (``jasper_ring::RingWriter``) that
# CamillaDSP reads via a CAPTURE direction of the ``jts_ring`` ioplug. Same SHM
# contract v1 as Ring B; roles flipped. The product auto reconciler now resolves
# eligible solo boxes to this coupling by default; explicit loopback / operator
# markers still fail safe to the historical snd-aloop path. The Rust
# ``Coupling::ShmRing`` normalizer MUST agree with this token.
COUPLING_SHM_RING = "shm_ring"
# The recognized coupling tokens. Public so other planners (e.g.
# ``jasper.audio_runtime_plan``) can reuse this SSOT instead of re-listing the
# tokens and drifting when a new lab coupling lands. Any value NOT in this set
# (a typo, or the removed ``transport_pipe``) fails safe to loopback — see
# :func:`resolve_coupling` and :func:`coupling_value_removed`.
# ``_VALID_COUPLINGS`` stays as the backward-compatible private alias.
VALID_COUPLINGS = frozenset({COUPLING_LOOPBACK, COUPLING_SHM_RING})
_VALID_COUPLINGS = VALID_COUPLINGS

# Ring A (``shm_ring``) SHM ring file + slot-count env vars. fan-in creates the
# ring at ``JASPER_FANIN_RING_PATH`` with ``JASPER_FANIN_RING_SLOTS`` slots; the
# Rust daemon resolves both with the SAME defaults (see ``config.rs``). The
# n_slots <-> JASPER_FANIN_RING_SLOTS pairing is the drift axis with the ioplug
# conf.d geometry; the ring header's own validation is the runtime fail-loud
# backstop.
RING_PATH_ENV_VAR = "JASPER_FANIN_RING_PATH"
DEFAULT_FANIN_RING_PATH = "/dev/shm/jts-ring/program.ring"
RING_SLOTS_ENV_VAR = "JASPER_FANIN_RING_SLOTS"
# Ring A/B slot size in frames. Mirrors rust/jasper-fanin/src/config.rs
# RING_SLOT_FRAMES and c/jts-ring-ioplug/pcm_jts_ring.c JTS_RING_DEFAULT_PERIOD.
# The conf.d period parser and contract tests pin those copies to this value.
RING_SLOT_FRAMES = 128
DEFAULT_FANIN_RING_SLOTS = 2
RING_CAMILLA_CHUNKSIZE = 128
RING_CAMILLA_TARGET_LEVEL = 128
RING_CAMILLA_QUEUELIMIT = 1
RING_CAMILLA_ENABLE_RATE_ADJUST = False

# Ring A capture device + wire format. fan-in is S16 native and the SHM ring
# carries S16LE with NO widening (an SHM ring has no kernel page-size floor).
# CamillaDSP captures it as an ALSA device named
# by the ioplug conf.d block (``deploy/alsa/conf.d/60-jts-ring.conf``, shipped
# inert by P1). Pinned here so the hand generator
# (``make-camilla-ring-config.sh`` capture-swap mode) and the Rust writer stay
# one SSOT.
RING_CAPTURE_DEVICE = "jts_ring_capture"
RING_WIRE_FORMAT = "S16_LE"

# ---------------------------------------------------------------------------
# Ring B (camilla -> outputd playback bridge). The OTHER half of the ``shm_ring``
# coupling. The ``shm_ring`` coupling is END-TO-END: fan-in writes Ring A
# (program.ring), CamillaDSP captures it, and CamillaDSP writes its post-DSP
# stereo program to Ring B (content.ring) via the ``jts_ring_playback`` ioplug,
# which jasper-outputd reads one slot per DAC period. Both rings flip together or
# not at all (the coupling reconciler is the single writer of the pair; a partial
# flip is fail-closed to loopback/direct). It is a dual-boundary coupling (Ring A
# capture + Ring B playback).
#
# The env keys below are read by the Rust ``jasper-outputd`` daemon
# (``rust/jasper-outputd/src/config.rs``): ``JASPER_OUTPUTD_CONTENT_BRIDGE`` +
# ``JASPER_OUTPUTD_SHM_RING_PATH`` / ``_SLOTS``. Pinned here so the Python control
# plane (emitters + coupling reconciler) names the same bridge the daemon reads.
# The n_slots defaults now match on purpose: Ring A and Ring B both hold the
# 2-slot latency floor. They are still SEPARATE ring files, so a future coherent
# operator override can tune Ring A without changing Ring B.
OUTPUTD_CONTENT_BRIDGE_ENV_VAR = "JASPER_OUTPUTD_CONTENT_BRIDGE"
OUTPUTD_CONTENT_BRIDGE_DIRECT = "direct"
OUTPUTD_CONTENT_BRIDGE_SHM_RING = "shm_ring"
OUTPUTD_RING_PATH_ENV_VAR = "JASPER_OUTPUTD_SHM_RING_PATH"
DEFAULT_OUTPUTD_RING_PATH = "/dev/shm/jts-ring/content.ring"
OUTPUTD_RING_SLOTS_ENV_VAR = "JASPER_OUTPUTD_SHM_RING_SLOTS"
DEFAULT_OUTPUTD_RING_SLOTS = 2

# Ring B playback device. CamillaDSP writes its post-DSP stereo program to this
# ALSA ioplug device (the WRITE direction of the same ``jts_ring`` plugin whose
# CAPTURE direction is ``jts_ring_capture``). S16_LE — the SHM ring's pinned wire
# format, no widening (fan-in and outputd are both S16 native at the DAC write).
RING_PLAYBACK_DEVICE = "jts_ring_playback"


def resolve_coupling(raw: str | None) -> str:
    """Normalize a raw ``JASPER_FANIN_CAMILLA_COUPLING`` value to a transport.

    Fail-SAFE to ``loopback`` (the byte-identical-to-today path) on unset, empty,
    or any unrecognized value — a typo in the env file, or the REMOVED
    ``transport_pipe`` token on a migrating box, must never silently flip the
    shared realtime capture to a transport the operator did not intend, nor crash
    a config emit. The Rust daemon applies the same normalization so both sides
    agree on every recognized token (``loopback`` / ``shm_ring``).
    Case-insensitive; surrounding whitespace ignored.
    """
    if raw is None:
        return COUPLING_LOOPBACK
    value = raw.strip().lower()
    if value in _VALID_COUPLINGS:
        return value
    return COUPLING_LOOPBACK


def coupling_value_removed(raw: str | None) -> bool:
    """True iff a persisted coupling value is present but NOT a recognized token.

    Catches both a typo and the REMOVED ``transport_pipe`` coupling (deleted
    2026-07-11). Such a value fails safe to ``loopback`` in :func:`resolve_coupling`;
    the ``--auto`` reconciler uses this predicate to converge the box to loopback
    with a loud ``event=…result=removed_coupling_failsafe`` line (so a migrating
    box never silently keeps a deleted mode), and the doctor surfaces it. An
    unset / empty value is NOT "removed" — it is the ordinary loopback default.
    """
    if raw is None:
        return False
    value = raw.strip().lower()
    return bool(value) and value not in _VALID_COUPLINGS


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


def resolve_outputd_content_bridge(raw: str | None) -> str:
    """Normalize a raw ``JASPER_OUTPUTD_CONTENT_BRIDGE`` value.

    Fail-SAFE to ``direct`` (the byte-identical-to-today outputd content source)
    on unset, empty, or any unrecognized value — the Rust daemon
    (``config.rs``) additionally accepts ``rate_match``, but the coupling control
    plane only knows the two bridges the ``loopback``/``shm_ring`` couplings pair
    with: ``direct`` (loopback's partner) and ``shm_ring`` (Ring B). ``rate_match``
    is a separate deferred lab bridge, not part of any coupling. Case-insensitive;
    surrounding whitespace ignored.
    """
    if raw is None:
        return OUTPUTD_CONTENT_BRIDGE_DIRECT
    value = raw.strip().lower()
    if value in (OUTPUTD_CONTENT_BRIDGE_DIRECT, OUTPUTD_CONTENT_BRIDGE_SHM_RING):
        return value
    return OUTPUTD_CONTENT_BRIDGE_DIRECT


def outputd_content_bridge_for_coupling(raw: str | None) -> str:
    """The outputd content bridge that COHERENTLY pairs with a fan-in coupling.

    ``shm_ring`` -> ``shm_ring`` (Ring B), everything else -> ``direct``. This is
    the pairing the coupling reconciler enforces so the two ends never split:
    fan-in on Ring A implies outputd on Ring B. ``loopback`` maps to ``direct``
    (outputd reads the snd-aloop content lane, not the content bridge).
    """
    return (
        OUTPUTD_CONTENT_BRIDGE_SHM_RING
        if resolve_coupling(raw) == COUPLING_SHM_RING
        else OUTPUTD_CONTENT_BRIDGE_DIRECT
    )


def resolve_outputd_ring_path(raw_path: str | None) -> str:
    """Resolve the Ring B (content) SHM ring file path from a raw env value.

    Empty / unset -> :data:`DEFAULT_OUTPUTD_RING_PATH`. Trims whitespace. The Rust
    outputd daemon resolves ``JASPER_OUTPUTD_SHM_RING_PATH`` the same way.
    """
    if raw_path is None:
        return DEFAULT_OUTPUTD_RING_PATH
    value = raw_path.strip()
    return value or DEFAULT_OUTPUTD_RING_PATH


OUTPUTD_RING_SLOTS_MIN = 2
OUTPUTD_RING_SLOTS_MAX = 16


def resolve_outputd_ring_slots(raw_slots: str | None) -> int:
    """Resolve the Ring B n_slots from a raw env value.

    Empty / unset -> :data:`DEFAULT_OUTPUTD_RING_SLOTS` (2, ping-pong). A
    present-but-out-of-range or unparseable value FAILS LOUD (:class:`ValueError`)
    rather than silently clamping — the ioplug/daemon geometry must never shear.
    Range :data:`OUTPUTD_RING_SLOTS_MIN`..=:data:`OUTPUTD_RING_SLOTS_MAX` mirrors
    the Rust ``MIN_SHM_RING_SLOTS`` / ``MAX_SHM_RING_SLOTS`` (config.rs).
    """
    if raw_slots is None:
        return DEFAULT_OUTPUTD_RING_SLOTS
    stripped = raw_slots.strip()
    if not stripped:
        return DEFAULT_OUTPUTD_RING_SLOTS
    try:
        value = int(stripped)
    except ValueError as exc:
        raise ValueError(
            f"{OUTPUTD_RING_SLOTS_ENV_VAR}={raw_slots!r} is not an integer; the "
            "outputd SHM ring slot count must be a whole number"
        ) from exc
    if OUTPUTD_RING_SLOTS_MIN <= value <= OUTPUTD_RING_SLOTS_MAX:
        return value
    raise ValueError(
        f"{OUTPUTD_RING_SLOTS_ENV_VAR}={raw_slots!r} out of range "
        f"{OUTPUTD_RING_SLOTS_MIN}..={OUTPUTD_RING_SLOTS_MAX} — a shear-prone "
        "outputd SHM ring geometry must fail loud, not silently clamp"
    )


def ring_pair_is_coherent(
    coupling_raw: str | None,
    content_bridge_raw: str | None,
) -> bool:
    """True iff the fan-in coupling and outputd content bridge are a coherent pair.

    The two must flip together: both ring (``shm_ring`` + ``shm_ring``) or neither
    (``loopback`` + ``direct``). A PARTIAL flip — one end on the
    ring and the other on ALSA/direct — is fail-closed everywhere (the reconciler,
    the artifact binder, the doctor) because it strands one ring end (a silent
    audio outage: outputd reads a ring nobody writes, or CamillaDSP writes a ring
    nobody reads). Returns True for the two coherent states, False for a partial.
    """
    coupling = resolve_coupling(coupling_raw)
    bridge = resolve_outputd_content_bridge(content_bridge_raw)
    if coupling == COUPLING_SHM_RING:
        return bridge == OUTPUTD_CONTENT_BRIDGE_SHM_RING
    # loopback never pairs with the Ring B bridge.
    return bridge == OUTPUTD_CONTENT_BRIDGE_DIRECT


def capture_kwargs_for_coupling(raw: str | None) -> dict[str, object]:
    """Return the ``emit_sound_config`` capture kwargs for the resolved coupling.

    - ``loopback`` (default): returns ``{}`` so the caller's existing
      ``capture_device`` / ``capture_format`` defaults emit the dsnoop ALSA
      capture — **byte-identical** to today. This empty-dict contract is what
      keeps every existing caller unchanged when the flag is unset. Any
      unrecognized value (a typo, or the removed ``transport_pipe``) resolves to
      ``loopback`` here too.

    - ``shm_ring`` (Ring A + Ring B): returns the FULL end-to-end ring topology
      kwargs — the CamillaDSP capture device ``jts_ring_capture`` (Ring A, fan-in
      writes it) AND the playback device ``jts_ring_playback`` (Ring B, outputd
      reads it), both S16_LE (the SHM ring's pinned wire format; fan-in and
      outputd are S16 native, no widening). The two rings are ONE coupling: an
      armed box's ``/sound/`` save must emit a config whose capture is the ring
      AND whose playback is the ring — a half-ring config (ring capture + ALSA
      loopback playback, or vice versa) would strand one end. These kwargs flow
      through :func:`coupling_capture_kwargs_from_env` into the product emitters
      (``/sound/``, ``/correction/``,
      ``audio_runtime_plan.apply_capture_precedence``) — but only when the
      persisted coupling (``fanin.env``'s :data:`COUPLING_ENV_VAR`, read
      file-fresh by :func:`coupling_capture_kwargs_from_env` on the live-env path
      because the socket-activated wizards do NOT ``EnvironmentFile=`` it) resolves
      to ``shm_ring``, so this is deliberate coherence-when-armed. The ring devices
      only RESOLVE once P1's
      ioplug conf.d block (``60-jts-ring.conf``) is installed and the coupling
      reconciler has armed both rings; until then the flag stays unset (env unset
      -> ``loopback`` -> ``{}``). The ring graph carries its own low-latency
      CamillaDSP geometry: chunk 128 / target 128 / queue 1 / rate_adjust off.
      Those values are coupled to the 2-slot Ring A default; chunk 256 would span
      the entire 2-slot buffer.
    """
    resolved = resolve_coupling(raw)
    if resolved == COUPLING_SHM_RING:
        return {
            "capture_device": RING_CAPTURE_DEVICE,
            "capture_format": RING_WIRE_FORMAT,
            "playback_device": RING_PLAYBACK_DEVICE,
            "playback_format": RING_WIRE_FORMAT,
            "chunksize": RING_CAMILLA_CHUNKSIZE,
            "target_level": RING_CAMILLA_TARGET_LEVEL,
            "queuelimit": RING_CAMILLA_QUEUELIMIT,
            "enable_rate_adjust": RING_CAMILLA_ENABLE_RATE_ADJUST,
        }
    return {}


def coupling_capture_kwargs_from_env(
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Resolve the live ``emit_sound_config`` capture kwargs from the process env.

    The one call shape a config emitter uses to thread the SHARED fan-in→Camilla
    coupling into a live re-emit. Returns ``{}`` for the default ``loopback``
    coupling (byte-identical to today) and the full ring topology kwargs for
    ``shm_ring``.

    **Coupling token is resolved FILE-FRESH on the live-env path** (``env`` is
    ``None``). The wizard processes that call this — jasper-web (``/sound/``) and
    jasper-correction-web (``/correction/``) — do NOT load ``fanin.env`` /
    ``outputd.env`` via ``EnvironmentFile=`` (they carry only ``jasper.env`` +
    their own wizard files), and a socket-activated daemon stays alive across a
    coupling flip, so ``os.environ`` is a STALE reader of the coupling — exactly
    the ``os.environ``-stale class AGENTS.md canonizes for the voice provider
    (fix: read the SSOT file fresh, ``jasper.voice.provider_state``). Without this
    an armed box's ``/sound/`` or ``/correction/`` save would emit a *loopback*
    capture/playback config and silently revert CamillaDSP off the rings (a silent
    audio outage: outputd reads Ring B while CamillaDSP writes the loopback lane).
    So on the live path we consult the persisted ``fanin.env`` for the coupling
    token — the same SSOT the daemons and the reconciler read. An EnvironmentFile
    flip still takes effect on the next regeneration without a code edit; the
    persisted file is just the authoritative source for WHICH coupling.

    An EXPLICIT ``env`` mapping is treated as authoritative (no file fallback) for
    a caller that wants the env it hands in, not a disk read. Today that is unit
    tests only: since the CLI-render-coupling fix, ``jasper.audio_runtime_plan``'s
    live path calls this with ``env=None`` (file-fresh), and no production caller
    synthesizes ``dict(os.environ)`` into the explicit branch anymore — the
    reconciler pre-syncs ``os.environ`` + the files and then leans on the
    ``env is None`` file-fresh read above.
    """
    if env is None:
        # Live-env path: file-fresh coupling token (SSOT).
        # Lazy import — jasper.fanin.coupling_reconcile imports THIS module, so a
        # top-level import would be circular (mirrors every other in-tree caller).
        from jasper.fanin.coupling_reconcile import read_persisted_coupling

        return capture_kwargs_for_coupling(read_persisted_coupling())

    return capture_kwargs_for_coupling(env.get(COUPLING_ENV_VAR))


def member_kwargs_are_pipe_sink(member_kwargs: dict[str, object] | None) -> bool:
    """True when the resolved grouping member kwargs are a SnapFIFO pipe sink.

    A bonded/grouped member (active-leader program bake, or a passive grouping
    follower leader) writes CamillaDSP's playback to the Snapcast pipe with
    ``enable_rate_adjust=False`` (snapclient is the sole rate-tracker — the
    multiroom inv-5). So when this is True, the local coupling must be a no-op for
    that emit (the grouped topology is the Distributed-Active track's concern, not
    this solo-speaker latency hop).
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
