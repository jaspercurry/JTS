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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

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
#
# The Rust side is a COMPILE-TIME const with no env override — fan-in always
# creates Ring A with it — so this is the only slot size the transport carries.
# The conf.d WRITER is pinned here too: jasper.ring_assets.render_ring_conf_wire
# refuses any period that is not this value, and its caller refuses a DAC
# LatencyFloor whose outputd_period_frames differs, because writing another
# period would make CamillaDSP's ioplug attach against a geometry fan-in never
# builds (RING_ATTACH_FATAL -> shm_ring crashes at arm instead of refusing).
# Making the slot floor-derived across all four components is issue #2147.
RING_SLOT_FRAMES = 128
DEFAULT_FANIN_RING_SLOTS = 2
RING_CAMILLA_CHUNKSIZE = 128
RING_CAMILLA_TARGET_LEVEL = 128
RING_CAMILLA_QUEUELIMIT = 1
RING_CAMILLA_ENABLE_RATE_ADJUST = False

# Ring A capture device. CamillaDSP captures it as an ALSA device named by the
# ioplug conf.d block (``deploy/alsa/conf.d/60-jts-ring.conf``). Pinned here so
# the hand generator (``make-camilla-ring-config.sh`` capture-swap mode) and the
# Rust writer stay one SSOT.
RING_CAPTURE_DEVICE = "jts_ring_capture"

# The ring wire's sample-format VOCABULARY — the two tokens every end of the
# ring spells identically: the conf.d ``format`` field (C ioplug), fan-in's
# ``JASPER_FANIN_RING_WIRE_FORMAT``, outputd's
# ``JASPER_OUTPUTD_CONTENT_FORMAT``, and CamillaDSP's emitted capture/playback
# ``format:``. They map onto the header's ``sample_format`` ids
# (``jasper.ring_assets.RING_SAMPLE_FORMAT_*``), which the attach compares
# field-by-field.
#
# ``RING_WIRE_FORMAT`` is the NARROW token specifically, not "the ring's
# format": which of the two a box carries is :func:`resolve_ring_wire`'s answer.
# It stays a named constant because it is also the ioplug conf.d default and the
# shipped wire, so "narrow" has one spelling.
RING_WIRE_FORMAT = "S16_LE"
RING_WIRE_FORMAT_WIDE = "S32_LE"
RING_WIRE_FORMATS = (RING_WIRE_FORMAT, RING_WIRE_FORMAT_WIDE)

# THE BOX'S DECLARED RING WIRE — one key, read identically by both languages.
# Rust reads it in ``jasper_fanin::config``'s ``RingWireFormat::from_env_value``;
# Python reads it in :func:`resolve_ring_wire_format`, and
# :func:`resolve_ring_wire` resolves the box's answer through that. It is the
# ONLY input to the wire's format axis: nothing else in either language decides
# it, so the control plane and the daemon cannot disagree about what this box's
# ring carries.
#
# Every other end of the ring is DERIVED from that answer rather than declaring
# its own: the conf.d ``format`` field (rendered by
# ``jasper-audio-hardware-reconcile`` from ``resolve_ring_wire``), outputd's
# ``JASPER_OUTPUTD_CONTENT_FORMAT`` (same writer, via
# ``content_lane_format_for_coupling``), and CamillaDSP's emitted capture/
# playback ``format:``. They are compared anyway
# (``ring_edge_width_ready``) because they land in files written at DIFFERENT
# times — a half-applied render is exactly what that comparison catches.
RING_WIRE_FORMAT_ENV_VAR = "JASPER_FANIN_RING_WIRE_FORMAT"

# Ring A's channel count. Everything upstream of CamillaDSP is a stereo program
# and fan-in's mixer is stereo (``mixer.rs``'s ``CHANNELS: u32 = 2``, "Not
# configurable"), so Ring A is 2 on every box — it is not a per-topology axis
# the way Ring B's is. Mirrors
# ``jasper.active_speaker.runtime_contract.RING_STEREO_PROGRAM_CHANNELS``, which
# is the same number reached from the topology side; the contract test pins them
# equal.
RING_A_CHANNELS = 2

# ---------------------------------------------------------------------------
# Ring B (camilla -> outputd playback bridge). The OTHER half of the ``shm_ring``
# coupling. The ``shm_ring`` coupling is END-TO-END: fan-in writes Ring A
# (program.ring), CamillaDSP captures it, and CamillaDSP writes its post-DSP
# stereo program to Ring B (content.ring) via the ``jts_ring_playback`` ioplug,
# which jasper-outputd reads one slot per DAC period. Both rings flip together or
# not at all (the coupling reconciler is the single writer of the pair; a partial
# flip is fail-closed to loopback/direct). It is a dual-boundary coupling (Ring A
# capture + the post-DSP playback ring).
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
# CAPTURE direction is ``jts_ring_capture``). Its wire is whatever
# :func:`resolve_ring_wire` resolves for the box — the layout's accept-set
# (``jasper_ring::Geometry::validate_self``) admits S16LE and S32LE, so the wire
# is held to ONE of them by the resolver, not by the layout. outputd's internal
# program is i32, so a narrow slot is an S16 ingress it widens onto its spine
# after the copy, on its own side of the ring.
RING_PLAYBACK_DEVICE = "jts_ring_playback"

# ---------------------------------------------------------------------------
# The ACTIVE ring (ring v2 R7b) — a THIRD ring file and a THIRD ioplug PCM,
# carrying a roleful box's POST-crossover per-driver program from CamillaDSP to
# outputd. Ring B above carries a full-range stereo program; this one does not,
# and the two must never be confused, which is why the role is carried in the
# NAME rather than inferred from a width.
#
# WHY THE NAME AND NOT THE WIDTH. On jts3 — the first box this rung serves —
# the active lane is TWO channels (woofer + compression-driver tweeter), so
# ``content_channels == 2`` is true of the active ring and of the stereo ring
# alike. No channel-count test can tell them apart there. A distinct device
# name, a distinct ring path, and outputd's allowlist over the pair are what
# make the distinction structural instead of numeric.
#
# THE SPELLING IS LOAD-BEARING. ``_forbidden_playback_token``
# (:mod:`jasper.active_speaker.camilla_yaml`) is a case-insensitive SUBSTRING
# test, and ``FORBIDDEN_ACTIVE_PLAYBACK_TOKENS`` carries the STEREO ring's name
# so an active emitter can never target it. ``"jts_ring_playback" in
# "jts_ring_active_playback"`` is False, so this spelling is safe — while the
# equally natural ``jts_ring_playback_active`` would contain the forbidden token
# and self-block every active emit. Both directions are pinned by
# ``tests/test_ring_active_endpoint.py``.
RING_ACTIVE_PLAYBACK_DEVICE = "jts_ring_active_playback"
DEFAULT_OUTPUTD_ACTIVE_RING_PATH = "/dev/shm/jts-ring/active-content.ring"

# The reconciler's marker that outputd's endpoint IS the active ring. Written by
# ``deploy/bin/jasper-audio-hardware-reconcile`` in the SAME helper, from the
# SAME decision, as ``JASPER_OUTPUTD_ACTIVE_LANE`` — the two are one fact with
# two consumers, never two facts. outputd bails on the incoherent pair (marker
# without the lane), and under the ``shm_ring`` bridge it enforces the
# biconditional "the active ring path may be read ONLY by an armed active
# endpoint, and an armed active endpoint may read ONLY the active ring path".
OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR = "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT"


def ring_active_endpoint_armed(env: "Mapping[str, str] | None" = None) -> bool:
    """Is outputd's content endpoint armed as the ACTIVE ring on this box?

    Reads :data:`OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR`, whose single writer is
    ``jasper-audio-hardware-reconcile``. Truthy is the same vocabulary outputd's
    ``env_bool`` accepts (``1`` / ``true`` / ``yes`` / ``on``, case-insensitive)
    so the Python control plane and the Rust reader cannot disagree about what
    "armed" means; ``tests/test_ring_active_endpoint.py`` pins the two together.

    ``env`` is authoritative when passed — the shape reconcilers and the doctor
    use, which already hold ``outputd.env``'s parsed values. ``None`` reads the
    persisted ``outputd.env`` FILE FRESH, for the same reason
    :func:`coupling_capture_kwargs_from_env` reads ``fanin.env`` fresh: the
    socket-activated wizards and the long-lived control daemon do not
    ``EnvironmentFile=`` it and stay alive across a reconcile, so ``os.environ``
    is a stale reader of this key. Fail-SAFE to False on an unreadable file: an
    indeterminate marker must never assert an active-ring endpoint.
    """
    if env is None:
        from jasper.fanin.coupling_reconcile import OUTPUTD_ENV_PATH, read_value

        try:
            with open(OUTPUTD_ENV_PATH, encoding="utf-8") as fh:
                raw = read_value(fh.read(), OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR)
        except OSError:
            return False
    else:
        raw = env.get(OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR)
    return (raw or "").strip().lower() in _ENV_BOOL_TRUE


# outputd's ``env_bool`` accept-set (``rust/jasper-outputd/src/config.rs``).
# Spelled here so "armed" means one thing across the two languages.
_ENV_BOOL_TRUE = frozenset(("1", "true", "yes", "on"))


@dataclass(frozen=True)
class RingWire:
    """The geometry every end of the SHM ring must declare, resolved once.

    The ring's four independent ends — fan-in (the Ring A writer), the two
    ``jts_ring`` ioplug PCMs CamillaDSP opens, and outputd (the post-DSP ring's
    reader) —
    each declare a geometry, and the attach compares them field-by-field. While
    every axis was a constant, coherence was held by everyone reading the same
    literal. This object is what replaces that: ONE resolution, four declarers.

    **Equality only, never a ranking.** A declared wire either equals the
    resolved one or the end refuses to arm. No axis here supports a "wider is
    fine" claim: no width-ranking primitive exists in-repo, and ``S24_3LE`` —
    live on the DAC edge — already breaks any ordering by byte count.

    ``n_slots`` is deliberately NOT an axis here even though it is part of the
    attach-compared geometry: it already has owners (:func:`resolve_ring_slots`
    for Ring A, :func:`resolve_outputd_ring_slots` for Ring B) that resolve it
    per-ring from env, which this object has no access to. Restating it would
    make two answers for one fact.

    ``ring_active_channels`` is the FIFTH field and the third ring's width, and
    it is a SEPARATE field rather than a widening of ``ring_b_channels`` for the
    same reason: one field per ring END, never one field for two ends. Ring B is
    a full-range stereo program; the active ring is a roleful box's post-
    crossover per-driver program. A single field would have to answer both, and
    on a 2-way box — where the active width is also 2 — the wrong answer is
    numerically invisible. ``None`` means "this box has no active ring", which is
    every non-roleful box (and every roleful box whose sink cannot carry one).
    """

    sample_format: str
    ring_a_channels: int
    ring_b_channels: int
    period_frames: int
    ring_active_channels: int | None = None


def resolve_ring_wire_format(raw: str | None) -> str:
    """Normalize a raw :data:`RING_WIRE_FORMAT_ENV_VAR` value to a wire token.

    THE PYTHON HALF OF A TWO-LANGUAGE PARSE. ``jasper-fanin`` normalizes the same
    key in ``RingWireFormat::from_env_value``
    (``rust/jasper-fanin/src/config.rs``) and this must classify every input the
    same way, because the two resolve the SAME box's wire from the SAME file:

    - unset, or empty after trimming → :data:`RING_WIRE_FORMAT` (narrow). Empty
      is how this repo's env-file writers CLEAR a key, so a cleared key and an
      absent key mean one thing;
    - exactly ``S16_LE`` / ``S32_LE`` after trimming → that token. The match is
      case-SENSITIVE because the C ioplug's own ``strcmp`` is: accepting a
      spelling the ioplug rejects would resolve a wire no reader can open;
    - anything else → :class:`ValueError`. FAIL LOUD, never fall back: silently
      resolving a typo to narrow would emit and render a wire the operator did
      not ask for, while fan-in — which treats the same value as a config-class
      fault and parks at exit 78 — would refuse to start. One typo, two verdicts
      is worse than one refusal.

    ``tests/test_ring_wire_format_contract.py`` pins this against the Rust
    source so the two normalizers cannot drift apart silently.
    """
    if raw is None:
        return RING_WIRE_FORMAT
    value = raw.strip()
    if not value:
        return RING_WIRE_FORMAT
    if value in RING_WIRE_FORMATS:
        return value
    raise ValueError(
        f"{RING_WIRE_FORMAT_ENV_VAR}={raw!r} unsupported "
        f"({'|'.join(RING_WIRE_FORMATS)}) — the token must match the ioplug "
        "conf.d `format` field exactly; jasper-fanin treats the same value as a "
        "config-class fault and parks rather than guessing a wire"
    )


def read_declared_ring_wire_format(
    env: Mapping[str, str] | None = None,
) -> str:
    """The box's declared ring wire format, resolved the way fan-in resolves it.

    FILE-FRESH on the live path (``env`` is ``None``), over the same chain
    systemd gives ``jasper-fanin`` — ``/etc/jasper/jasper.env`` then
    ``/var/lib/jasper/fanin.env``, later wins. Not ``os.environ``: the callers
    that need this answer are socket-activated wizards and long-lived daemons
    that never loaded ``fanin.env`` at all, which is the ``os.environ``-stale
    class AGENTS.md canonizes (the voice-provider fix). An explicit ``env``
    mapping is authoritative with no file fallback, for a caller that means the
    env it hands in.

    A file that cannot be read contributes nothing — an absent ``fanin.env`` is
    the ordinary unarmed state — but a file that IS readable and declares a
    value this repo does not recognize raises, exactly as fan-in would.
    """
    if env is not None:
        return resolve_ring_wire_format(env.get(RING_WIRE_FORMAT_ENV_VAR))

    # Lazy imports: jasper.fanin.coupling_reconcile imports THIS module, so a
    # top-level import would be circular (mirrors coupling_capture_kwargs_from_env).
    from pathlib import Path

    from jasper.env_file import read_value
    from jasper.fanin.coupling_reconcile import FANIN_ENV_PATH, JASPER_ENV_PATH

    for path in (FANIN_ENV_PATH, JASPER_ENV_PATH):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        raw = read_value(text, RING_WIRE_FORMAT_ENV_VAR)
        if raw is not None:
            return resolve_ring_wire_format(raw)
    return RING_WIRE_FORMAT


def resolve_ring_wire(topology: Any = None) -> RingWire:
    """Resolve the per-box SHM ring wire.

    ``topology`` is an :class:`~jasper.output_topology.OutputTopology` (typed
    loosely because this module stays import-cheap for the socket-activated web
    surfaces, so the topology layer is imported lazily). Pass the box's saved
    topology where it is in hand; ``None`` answers for the shipped conf.d
    geometry, which is what a caller with no topology to consult — the ioplug
    open-probe, a conf.d render on a box whose topology is not the question —
    must use.

    Each axis and who decides it:

    - ``sample_format`` — the box's own declaration, through
      :func:`read_declared_ring_wire_format`: the one
      :data:`RING_WIRE_FORMAT_ENV_VAR` value ``jasper-fanin`` resolves from the
      same chain, defaulting to :data:`RING_WIRE_FORMAT` (narrow) when the box
      declares nothing. The layout's accept-set is wider (S16LE and S32LE, both
      ends of the ring already parse both), so which one a box carries is a
      DECLARATION, not a policy constant — until 2026-08-11 this axis was
      pinned narrow here with no input at all, which meant an operator could
      declare a wide wire to fan-in and every Python end would still emit and
      render narrow (jts3's blocked wide arm). The ioplug conf.d default is the
      narrow token, which is why an unrendered conf.d and an undeclared box
      agree without the file saying so.
    - ``ring_a_channels`` — :data:`RING_A_CHANNELS` on every box. Not a
      per-topology axis: the program upstream of CamillaDSP is stereo and
      fan-in's mixer is not configurable.
    - ``ring_b_channels`` — from
      :func:`~jasper.active_speaker.runtime_contract.ring_channels_for_topology`,
      the single ring-eligibility/width answer. A topology with NO ring width
      (roleful, composite, explicit mono) falls back to the shipped stereo
      declaration, because that is genuinely what the conf.d on that box says
      and what an open-probe of it must ask for. Whether such a box may ARM is
      not this function's question — ``topology_supports_shm_ring`` and the arm
      preflights own that, and they refuse it.
    - ``period_frames`` — :data:`RING_SLOT_FRAMES`, fan-in's compile-time slot
      size. Reading it through the resolver is what gives issue #2147 a seam:
      making the slot floor-derived becomes "this axis stops being a constant"
      rather than a change at every declaring end.
    - ``ring_active_channels`` — from
      :func:`~jasper.active_speaker.runtime_contract.active_ring_channels_for_topology`,
      the ACTIVE ring's width, and ``None`` on every box that has no active ring
      (which is every box that is not roleful). Deliberately a DIFFERENT
      question from ``ring_b_channels``: the two rings coexist on a roleful box
      and carry different programs, so one function cannot answer for both — a
      single answer would stamp the active width into the STEREO ring's conf
      block, which on a 2-way box is invisible because both are 2.
    """
    ring_b_channels = RING_A_CHANNELS
    ring_active_channels: int | None = None
    if topology is not None:
        # Lazy import: the topology layer is heavy and this module is imported by
        # the socket-activated wizards (see the module docstring).
        from jasper.active_speaker.runtime_contract import (
            active_ring_channels_for_topology,
            ring_channels_for_topology,
        )

        resolved = ring_channels_for_topology(topology)
        if resolved is not None:
            ring_b_channels = resolved
        ring_active_channels = active_ring_channels_for_topology(topology)
    return RingWire(
        sample_format=read_declared_ring_wire_format(),
        ring_a_channels=RING_A_CHANNELS,
        ring_b_channels=ring_b_channels,
        period_frames=RING_SLOT_FRAMES,
        ring_active_channels=ring_active_channels,
    )


# Every ALSA PCM name the ring ioplug owns, in ring order (A, B, ACTIVE). The
# set a caller tests membership against when it has a device name in hand and
# needs to know "is this end of the graph a ring end?" — the emitter side
# (``jasper.active_speaker.camilla_yaml.active_sink_params``) and the arm gate
# (``jasper.fanin.coupling_reconcile.ring_edge_width_ready``) both read it, so
# neither carries its own list of the three names.
RING_PCM_DEVICES = (
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    RING_ACTIVE_PLAYBACK_DEVICE,
)


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
    on unset, empty, or any unrecognized value. The vocabulary is exactly the
    Rust daemon's (``config.rs``): ``direct`` (loopback's partner) and
    ``shm_ring`` (Ring B). Case-insensitive; surrounding whitespace ignored.

    The REMOVED ``rate_match`` lab bridge lands here as an unrecognized value
    and resolves ``direct``, matching the daemon's own fail-safe arm
    (``REMOVED_RATE_MATCH_BRIDGE_SPELLINGS``). Do NOT reach for this resolver
    where a *policy* must reject a stale value: the route-latency policy in
    :mod:`jasper.audio_runtime_plan` compares the RAW literal precisely so this
    fail-safe cannot launder a removed bridge into a green low-latency claim.
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


def ring_pair_intent_is_coherent(
    coupling_raw: str | None,
    content_bridge_raw: str | None,
) -> bool:
    """True iff the two persisted INTENT tokens are a coherent pair.

    The two must flip together: both ring (``shm_ring`` + ``shm_ring``) or neither
    (``loopback`` + ``direct``). A PARTIAL flip — one end on the
    ring and the other on ALSA/direct — is fail-closed everywhere (the reconciler,
    the artifact binder, the doctor) because it strands one ring end (a silent
    audio outage: outputd reads a ring nobody writes, or CamillaDSP writes a ring
    nobody reads). Returns True for the two coherent states, False for a partial.

    **INTENT, and the name says so.** This compares two env strings, each first
    passed through its own fail-SAFE resolver, and nothing else: no ring header,
    no conf.d, no daemon STATUS. It cannot see a wire that shears on format,
    channels, period or slots, a stale on-disk ring file, or a box whose env pair
    agrees while the loaded CamillaDSP graph does not. It was called
    ``ring_pair_is_coherent`` until R5b, which reads as a verdict on the ring; the
    verdict on the WIRE is
    :func:`jasper.fanin.coupling_reconcile.ring_edge_width_ready` (every
    declaring end, the loaded CamillaDSP graph included) and the OBSERVED tuple
    both daemons publish, surfaced at
    ``/state.audio_graph.coupling.observed``.
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
      reads it), both at the format :func:`resolve_ring_wire` resolves for this
      box, which is what makes the emitted config and the ring's other three
      declaring ends one answer instead of four. (outputd widens a narrow
      consumed slot onto its own i32 program spine after the copy, on its side
      of the ring.) The resolution is taken with NO topology — the shipped
      geometry — because nothing this function emits is per-topology: the
      devices are fixed and the format is one per box. A wire whose format ever
      became topology-dependent would shear against this emit, and
      ``ring_edge_width_ready`` is the gate that reports that rather than a
      parameter here that no caller passes. The
      two rings are ONE coupling: an
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

    **These are the STEREO ring's devices, and the ACTIVE ring is deliberately
    not represented here.** The kwargs feed ``emit_sound_config`` — the FLAT
    full-range program lane — and are merged LAST by
    :func:`~jasper.audio_runtime_plan.apply_capture_precedence`, so anything they
    name overwrites what the caller resolved. That would be a real stomp if this
    function could ever be asked about a roleful box: it would hand the flat
    emitter the full-range stereo ring on a box whose ring carries per-driver
    channels.

    It cannot, and the reason is structural rather than careful ordering: the
    flat program lane is REFUSED outright on a roleful topology
    (``flat_program_graph_blocked_reason`` → ``CarrierCannotHostEq``), because a
    full-range graph on a crossover box would send full-range audio to a
    protected tweeter. A box with an active ring is a roleful box by definition,
    so the emit these kwargs serve never runs there. That is also why this
    function takes no topology — every box it can legitimately answer for has the
    same stereo answer. An active graph's device is resolved by
    ``resolve_output_layout`` instead, on the path that emits per-driver graphs.
    """
    resolved = resolve_coupling(raw)
    if resolved == COUPLING_SHM_RING:
        wire = resolve_ring_wire()
        return {
            "capture_device": RING_CAPTURE_DEVICE,
            "capture_format": wire.sample_format,
            "playback_device": RING_PLAYBACK_DEVICE,
            "playback_format": wire.sample_format,
            "chunksize": RING_CAMILLA_CHUNKSIZE,
            "target_level": RING_CAMILLA_TARGET_LEVEL,
            "queuelimit": RING_CAMILLA_QUEUELIMIT,
            "enable_rate_adjust": RING_CAMILLA_ENABLE_RATE_ADJUST,
        }
    return {}


def content_lane_format_for_coupling(raw: str | None) -> str:
    """The CamillaDSP→outputd content-hop sample format this coupling carries.

    ONE definition of that hop's width, for both of its ends:

    - CamillaDSP's emitted ``playback: format:`` — this returns exactly what
      :func:`capture_kwargs_for_coupling` puts in ``playback_format``, or the
      emitters' own default (``DEFAULT_PLAYBACK_FORMAT``) for the ``loopback``
      coupling, whose kwargs are deliberately empty so every existing caller
      keeps its default.
    - outputd's requested ``JASPER_OUTPUTD_CONTENT_FORMAT`` — the audio-hardware
      reconciler emits this value, so the reader cannot ask for a width the
      writer does not emit. Deriving both from the same function is what makes
      that a structural property instead of two constants a maintainer must
      remember to move together.

    ``shm_ring`` therefore answers :func:`resolve_ring_wire`'s
    ``sample_format`` for this box, and ``loopback`` answers the box's
    program-lane default, which the wide-output-path program widens to S32_LE.
    Unrecognized values fail safe to ``loopback`` exactly as
    :func:`resolve_coupling` does.

    NOT a sink-type axis: a bonded leader's File/pipe sink is pinned to
    ``DEFAULT_PIPE_SINK_FORMAT`` (D4) and does not write this hop at all — its
    outputd content lane is fed by the endpoint-crossover CamillaDSP instance.
    Callers that need the format for an arbitrary sink want
    ``jasper.camilla_config_contract`` instead.
    """
    # Local import: this module stays stdlib-only at import time for the
    # socket-activated web surfaces (see the module docstring), and the
    # contract module is the same one-way direction every other caller uses.
    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT

    value = capture_kwargs_for_coupling(raw).get("playback_format")
    if isinstance(value, str) and value:
        return value
    return DEFAULT_PLAYBACK_FORMAT


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
