# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in → CamillaDSP coupling vocabulary (``JASPER_FANIN_CAMILLA_COUPLING``).

The single source of truth for HOW the fan-in mixer's summed program reaches
CamillaDSP's capture. ONE transport: ``shm_ring``, the end-to-end SHM-ring path
(Ring A + Ring B). fan-in writes Ring A (program.ring) that CamillaDSP captures
via ``jts_ring_capture``; CamillaDSP writes its post-DSP program to Ring B (or
to the ACTIVE ring on an armed roleful box). See ADR-0100 — a topology the ring
cannot serve parks under its own name
(:mod:`jasper.control.transport_park`); it never falls back.

This module is import-cheap (stdlib only) so socket-activated web surfaces and
the config emitters can resolve the ring without pulling in NumPy/SciPy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, TypedDict, cast

# Environment selector. Read at config-emit time and at fan-in daemon startup.
COUPLING_ENV_VAR = "JASPER_FANIN_CAMILLA_COUPLING"

# Ring A: fan-in writes an SPSC SHM ring (``jasper_ring::RingWriter``) that
# CamillaDSP reads via a CAPTURE direction of the ``jts_ring`` ioplug. Same SHM
# contract v1 as Ring B; roles flipped. The Rust ``Coupling::ShmRing``
# normalizer MUST agree with this token.
COUPLING_SHM_RING = "shm_ring"
# THE transport, spelled once. Public so other planners (e.g.
# ``jasper.audio_runtime_plan``) reuse this SSOT instead of re-listing the token.
# ``_VALID_COUPLINGS`` stays as the backward-compatible private alias.
VALID_COUPLINGS = frozenset({COUPLING_SHM_RING})
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
# Ring A/B slot size in frames. Mirrors rust/jasper-ring/src/layout.rs
# RING_SLOT_FRAMES (the one Rust declaration, which jasper-fanin re-exports
# and jasper-outputd reads) and c/jts-ring-ioplug/pcm_jts_ring.c
# JTS_RING_DEFAULT_PERIOD.
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


def ring_capacity_frames() -> int:
    """Frames the whole ring holds — the ALSA buffer size its ioplug reports.

    The bound a CamillaDSP ``chunksize`` crossing the ring has to clear:
    CamillaDSP sets ``avail_min`` to its chunk, and ALSA refuses an
    ``avail_min`` larger than the device's buffer. It is a property of the
    TRANSPORT, not of the fitted DAC — both factors are compile-time constants
    shared by the fan-in writer (``rust/jasper-ring/src/layout.rs``) and the
    ioplug (``c/jts-ring-ioplug``), so every box's ring is the same size.

    Deliberately not env-derived. ``JASPER_FANIN_RING_SLOTS`` exists, but the
    ioplug takes its slot count from the conf.d block instead, and a disagreeing
    pair fails the attach outright rather than resizing anything.

    THIS FUNCTION IS ISSUE #2147's SEAM: landing it makes the slot size derive
    from the DAC floor across all four components (fan-in, the ioplug, the
    conf.d render, the Camilla emitter) instead of the constant product below.
    It does not remove the clamp in
    ``camilla_config_contract.resolve_camilla_latency_for_devices`` — it makes
    the clamp stop biting, because a board that earns a bigger ring would then
    report one here and its floor would fit.

    The two are the same defect on different axes: #2147 is the PERIOD axis
    (a DAC's declared ``outputd_period_frames`` cannot reach the ring), and the
    clamp is the CHUNK axis (a DAC's declared ``camilla_chunksize`` reached the
    ring when it could not fit).
    """

    return RING_SLOT_FRAMES * DEFAULT_FANIN_RING_SLOTS


RING_CAMILLA_CHUNKSIZE = 128
RING_CAMILLA_TARGET_LEVEL = 128
RING_CAMILLA_QUEUELIMIT = 1
RING_CAMILLA_ENABLE_RATE_ADJUST = False


class RingCamillaGeometry(TypedDict):
    """The four CamillaDSP latency fields :data:`RING_CAMILLA_GEOMETRY` fills."""

    chunksize: int
    target_level: int
    queuelimit: int
    enable_rate_adjust: bool


# The geometry a graph built END-TO-END on the ring passes EXPLICITLY: the
# ACTIVE ring's per-driver graph (``active_emit_devices``) and the flat boot
# graph (``emit_flat_outputd_cutover_config``). Certified together — chunk 128
# is one ring slot and queuelimit 1 makes the slot handshake blocking, which is
# also why rate_adjust is off (nothing for the rate controller to steer).
#
# NOT the fallback for an ordinary sound/correction graph. Those carry the box's
# own floor clamped to the ring's capacity
# (``camilla_config_contract.resolve_camilla_latency_for_devices``), so moving
# them onto this pair is a retune with a listening test, not a refactor.
#
# ``MappingProxyType`` so a caller cannot retune every ring box by mutating it;
# the cast is what keeps ``**RING_CAMILLA_GEOMETRY`` per-key typed at the two
# emitters.
RING_CAMILLA_GEOMETRY: Final[RingCamillaGeometry] = cast(
    RingCamillaGeometry,
    MappingProxyType(
        {
            "chunksize": RING_CAMILLA_CHUNKSIZE,
            "target_level": RING_CAMILLA_TARGET_LEVEL,
            "queuelimit": RING_CAMILLA_QUEUELIMIT,
            "enable_rate_adjust": RING_CAMILLA_ENABLE_RATE_ADJUST,
        }
    ),
)

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
# It stays a named constant because it is still the C ioplug's compiled-in
# default (``jasper.ring_assets.RING_CONF_DEFAULT_FORMAT``, which mirrors it) and
# the operator's rollback token, so "narrow" has one spelling.
#
# It is NO LONGER the resolver's default: :func:`resolve_ring_wire_format`
# answers :data:`RING_WIRE_FORMAT_WIDE` for an undeclared box, and the shipped
# conf.d declares that token explicitly in every block.
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
#
# THE KEY HAS NO WRITER, AND THAT IS WHAT MAKES IT A ROLLBACK LEVER. Since the
# resolver's default is wide, the only reason to set this key is to pin a box
# NARROW — and a lever a reconciler could rewrite on the next boot, deploy or
# udev pass would not be one. So nothing in this repo
# writes it: every production site under jasper/, deploy/ and scripts/ that
# names the key is a READ, a gate's error string, or prose. Adding a writer
# would silently destroy the fleet's only way back to the narrow wire, so
# ``tests/test_ring_wire_format_contract.py`` pins the empty writer set by
# asserting no such line also names an env-write primitive.
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
# which jasper-outputd reads one slot per DAC period. It is a dual-boundary
# coupling (Ring A capture + the post-DSP playback ring).
#
# The env keys below are read by the Rust ``jasper-outputd`` daemon
# (``rust/jasper-outputd/src/config.rs``): ``JASPER_OUTPUTD_CONTENT_BRIDGE`` +
# ``JASPER_OUTPUTD_SHM_RING_PATH`` / ``_SLOTS``. Pinned here so the Python control
# plane (emitters + coupling reconciler) names the same bridge the daemon reads.
# The n_slots defaults now match on purpose: Ring A and Ring B both hold the
# 2-slot latency floor. They are still SEPARATE ring files, so a future coherent
# operator override can tune Ring A without changing Ring B.
OUTPUTD_CONTENT_BRIDGE_ENV_VAR = "JASPER_OUTPUTD_CONTENT_BRIDGE"
OUTPUTD_CONTENT_BRIDGE_SHM_RING = "shm_ring"
OUTPUTD_RING_PATH_ENV_VAR = "JASPER_OUTPUTD_SHM_RING_PATH"
DEFAULT_OUTPUTD_RING_PATH = "/dev/shm/jts-ring/content.ring"
OUTPUTD_RING_SLOTS_ENV_VAR = "JASPER_OUTPUTD_SHM_RING_SLOTS"
DEFAULT_OUTPUTD_RING_SLOTS = 2

# The width outputd REQUESTS on its content upstream. Single writer:
# ``jasper-audio-hardware-reconcile``, from
# :func:`content_lane_format_for_coupling`.
OUTPUTD_CONTENT_FORMAT_ENV_VAR = "JASPER_OUTPUTD_CONTENT_FORMAT"
# The width outputd assumes when that key is absent or empty: outputd's own
# documented default (``rust/jasper-outputd/src/config.rs``), the pre-flip S16
# lane, NOT whatever :func:`resolve_ring_wire` would pick for this box. A reader
# that followed the resolver here would refuse an arm for a wire the daemon has
# in fact declared.
OUTPUTD_DEFAULT_CONTENT_FORMAT = "S16_LE"

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
# The ACTIVE ring — a THIRD ring file and a THIRD ioplug PCM,
# carrying a roleful box's POST-crossover per-driver program from CamillaDSP to
# outputd. Ring B above carries a full-range stereo program; this one does not,
# and the two must never be confused, which is why the role is carried in the
# NAME rather than inferred from a width.
#
# WHY THE NAME AND NOT THE WIDTH. On a two-way roleful box, the active lane is
# TWO channels (woofer + compression-driver tweeter), so
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


# ---------------------------------------------------------------------------
# The named transport SHAPES. ``TransportTopology.name`` is the discriminator
# every consumer matches on, so each distinct transport gets its own name rather
# than a shared name with a device threaded through it: an exhaustive match over
# named shapes fails LOUD on one nobody handled, where a threaded device value
# shears silently through five call sites.
#
# ``shm_ring_active`` is selected on the PERSISTED COUPLING plus the reconciler's
# endpoint MARKER — deliberately NOT on the observed ``camilla_playback_device``.
# Selecting on the observed device would make
# :func:`jasper.transport_coherence.transport_coherence_report`' playback
# comparison vacuous: it would derive the expectation from the very value it is
# checking, so a Camilla graph pointed at the wrong ring would define itself
# correct.
TRANSPORT_SHM_RING = COUPLING_SHM_RING
TRANSPORT_SHM_RING_ACTIVE = "shm_ring_active"
# One END of the box is off the one transport (ADR-0100) — the LEGACY FIFO
# spelling of the round-trip ``dac_content`` lane, which outputd requires
# ``CONTENT_BRIDGE=direct`` for, or a coupling/bridge a daemon parks on. Not a
# second route: jasper.control.transport_park is what names such a box. The
# ring MARKER's shape is NOT this one — see TRANSPORT_DAC_CONTENT_RING below,
# which is served.
TRANSPORT_OFF_RING = "off_ring"
# A DUMB bonded member: outputd's content comes off the dac-content RETURN ring
# and no CENTRAL post-DSP ring is attached at all. Its own shape rather than
# TRANSPORT_OFF_RING, which would drop a healthy bonded member into the arm
# whose comparisons assume nothing is feeding outputd — while Ring A is still
# live on this box and must keep being compared.
TRANSPORT_DAC_CONTENT_RING = "dac_content_ring"
# Every named shape, so an exhaustive consumer can assert it handled one.
TRANSPORT_SHAPES = frozenset(
    (
        TRANSPORT_OFF_RING,
        TRANSPORT_SHM_RING,
        TRANSPORT_SHM_RING_ACTIVE,
        TRANSPORT_DAC_CONTENT_RING,
    )
)
# Every shape whose post-DSP hop is an SHM ring CamillaDSP drives. Membership,
# never a ``==`` on one name: a consumer that tested only ``shm_ring`` would
# silently take its OFF-RING arm on an active-ring box, which is the D5
# permanent-red-line shape. The dac-content shape is NOT a member: its post-DSP
# hop is a ring, but CamillaDSP does not drive it, so every camilla-endpoint
# comparison in this set is meaningless there.
RING_TRANSPORT_SHAPES = frozenset((TRANSPORT_SHM_RING, TRANSPORT_SHM_RING_ACTIVE))


@dataclass(frozen=True)
class TransportTopology:
    """Resolved audio transport topology for status/doctor surfaces."""

    name: str
    fanin_to_camilla: Mapping[str, Any]
    camilla_to_outputd: Mapping[str, Any]
    camilla: Mapping[str, Any]
    outputd_content_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fanin_to_camilla": dict(self.fanin_to_camilla),
            "camilla_to_outputd": dict(self.camilla_to_outputd),
            "camilla": dict(self.camilla),
            "outputd_content_source": self.outputd_content_source,
        }


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
    return _outputd_env_bool(raw)


def _outputd_env_bool(raw: "str | None") -> bool:
    """Read one BARE outputd marker the way outputd's ``env_bool`` reads it."""
    return (raw or "").strip().lower() in OUTPUTD_ENV_BOOL_TRUE


#: outputd's ``env_bool`` accept-set (``rust/jasper-outputd/src/config.rs``).
#: Spelled here so "armed" means one thing across the two languages, for every
#: bare outputd marker — the ACTIVE endpoint's and the dac-content lane's alike.
OUTPUTD_ENV_BOOL_TRUE = frozenset(("1", "true", "yes", "on"))


@dataclass(frozen=True)
class RingWire:
    """The geometry every end of the SHM ring must declare, resolved once.

    The ring's four independent ends — fan-in (the Ring A writer), the two
    ``jts_ring`` ioplug PCMs CamillaDSP opens, and outputd (the post-DSP ring's
    reader) — each declare a geometry, and the attach compares them
    field-by-field: ONE resolution, four declarers.

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

    - unset, or empty after trimming → :data:`RING_WIRE_FORMAT_WIDE`. Empty
      is how this repo's env-file writers CLEAR a key, so a cleared key and an
      absent key mean one thing. The default is WIDE because narrow is a width
      REGRESSION on the hop the ring replaces: the loopback CamillaDSP→outputd
      hop already carries
      :data:`~jasper.camilla_config_contract.DEFAULT_PLAYBACK_FORMAT` (S32_LE),
      so arming a ring at S16_LE would narrow a hop that was wide before the
      arm. Nothing in this repo WRITES this key — see
      :data:`RING_WIRE_FORMAT_ENV_VAR` — so an operator's ``S16_LE`` is a
      rollback lever no boot, deploy or udev pass can overwrite;
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
        return RING_WIRE_FORMAT_WIDE
    value = raw.strip()
    if not value:
        return RING_WIRE_FORMAT_WIDE
    if value in RING_WIRE_FORMATS:
        return value
    raise ValueError(
        f"{RING_WIRE_FORMAT_ENV_VAR}={raw!r} unsupported "
        f"({'|'.join(RING_WIRE_FORMATS)}) — the token must match the ioplug "
        "conf.d `format` field exactly; jasper-fanin treats the same value as a "
        "config-class fault and parks rather than guessing a wire"
    )


def read_declared_ring_wire_format() -> str:
    """The box's declared ring wire format, resolved the way fan-in resolves it.

    FILE-FRESH, over the same chain systemd gives ``jasper-fanin`` —
    ``/etc/jasper/jasper.env`` then ``/var/lib/jasper/fanin.env``, later wins.
    Not ``os.environ``: the callers that need this answer are socket-activated
    wizards and long-lived daemons that never loaded ``fanin.env`` at all, which
    is the ``os.environ``-stale class AGENTS.md canonizes (the voice-provider
    fix).

    A file that cannot be read contributes nothing — an absent ``fanin.env`` is
    the ordinary unarmed state — but a file that IS readable and declares a
    value this repo does not recognize raises, exactly as fan-in would.
    """
    # Lazy imports: jasper.fanin.coupling_reconcile imports THIS module, so a
    # top-level import would be circular.
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
    return RING_WIRE_FORMAT_WIDE


def assistant_wire_is_wide(
    *,
    wire_format: str | None = None,
    coupling: str | None = None,
) -> bool:
    """Whether THIS BOX's ASSISTANT IPC wire is wide (S32 at the i32 spine scale).

    THE PYTHON MIRROR OF ONE RULE. `jasper-fanin` resolves the identical
    conjunction in `Config::program_wire_is_wide`, which calls
    `jasper_tts_protocol::TtsWireWidth::from_box_declaration`; this restates that
    function's verdict, and
    :mod:`tests.test_ring_wire_format_contract` pins the two against each other
    by reading the Rust source rather than by trusting this docstring.

    **BOTH halves.** A wide wire needs the resolved ``S32_LE`` ring wire format
    AND a coupling that leaves fan-in on the ring.

    UNDECLARED IS THE RING on the transport half, which is why it asks
    :func:`coupling_value_removed` rather than the ``shm_ring`` token: the Rust
    side passes ``coupling_is_shm_ring: true`` unconditionally
    (``Config::program_wire_is_wide``) because ADR-0100 left one transport and
    ``jasper-fanin`` serves an absent key, an empty value and the token alike.
    Requiring the literal token here resolved NARROW on every box the reconciler
    had not written while the daemon on that same box ran WIDE — the two-language
    shear this predicate exists to prevent (#3655). Only a value the daemon
    REFUSES (exit 78, the unit parks) answers narrow, which is also what
    ``jasper-voice`` resolves in that situation.

    Both inputs default to a FILE-FRESH read of the same SSOT files the daemons
    read — :func:`read_declared_ring_wire_format` for the format and
    :func:`jasper.fanin.ring_health.persisted_coupling_feeds_ring` for the
    transport — because the callers that need this answer are long-lived daemons
    and socket-activated wizards that never loaded ``fanin.env``. Passing either
    explicitly is authoritative for that half, with no file fallback, for a
    caller that means the value it hands in — and the coupling must be handed in
    RAW, not resolved: a resolver answering "the ring or nothing" cannot spell
    the refused value this half turns on.
    """
    if wire_format is None:
        wire_format = read_declared_ring_wire_format()
    if coupling is None:
        # Lazy import: jasper.fanin.ring_health imports THIS module, so a
        # top-level import would be circular (mirrors every other in-tree caller).
        from jasper.fanin.ring_health import (
            FANIN_ENV_PATH,
            persisted_coupling_feeds_ring,
        )

        # Passed explicitly: the predicate's own default is bound at def time,
        # so a caller (or a test) repointing the module constant would not move
        # a no-argument call.
        on_ring = persisted_coupling_feeds_ring(FANIN_ENV_PATH)
    else:
        on_ring = not coupling_value_removed(coupling)
    return wire_format == RING_WIRE_FORMAT_WIDE and on_ring


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
      same chain, defaulting to :data:`RING_WIRE_FORMAT_WIDE` when the box
      declares nothing. The layout's accept-set is wider (S16LE and S32LE, both
      ends of the ring already parse both), so which one a box carries is a
      DECLARATION, not a policy constant. The shipped conf.d DECLARES the
      wide token in every block rather than omitting the key, so an unrendered
      conf.d and an undeclared box agree because the file says so — the C
      ioplug's own default is still the narrow token
      (:data:`~jasper.ring_assets.RING_CONF_DEFAULT_FORMAT`), so silence would
      now mean the opposite of what the resolver answers.
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
      preflights own that. **They do not simply refuse it**: a box with no Ring
      B may still arm the ACTIVE ring, which is a different transport with its
      own width (``ring_active_channels`` below). ``ring_topology_ready``'s
      ACTIVE arm admits a ROLEFUL topology, including a roleful composite,
      once its endpoint is staged. What genuinely cannot arm either ring is an
      explicit mono, or a PASSIVE composite.
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
# (``jasper.active_speaker.camilla_yaml.active_emit_devices``) and the arm gate
# (``jasper.fanin.coupling_reconcile.ring_edge_width_ready``) both read it, so
# neither carries its own list of the three names.
RING_PCM_DEVICES = (
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    RING_ACTIVE_PLAYBACK_DEVICE,
)

# The transport's observability token, spelled once for the two surfaces that
# report it — the ``driver_commission_prepared`` / ``driver_commission_load``
# journal lines and ``/state``'s commissioning block. An observability value, not
# config: nothing parses it back. Its ``alsa`` sibling and the two-route
# ``transport_label`` chooser retired with the loopback route (ADR-0100); the
# surfaces now answer this token or nothing, and "is this device a ring end?" is
# membership in :data:`RING_PCM_DEVICES`.
TRANSPORT_RING = "ring"


def resolve_coupling(raw: str | None) -> str | None:
    """The transport a raw ``JASPER_FANIN_CAMILLA_COUPLING`` value NAMES, if any.

    :data:`COUPLING_SHM_RING` for the ring token (case-insensitive, whitespace
    ignored); ``None`` for everything else — unset, empty, or a value outside
    :data:`VALID_COUPLINGS`. ``None`` is "this file names no transport", NOT a
    second transport: since ADR-0100 there is only one, and a caller that needs
    to tell an absent key from a retired token asks
    :func:`coupling_value_removed`.

    THIS IS NOT THE DAEMON'S RULE, and nothing may derive a runtime expectation
    from it. The Rust daemon serves ``None`` / ``""`` / ``shm_ring`` alike and
    refuses anything else as a config-class fault (exit 78, the unit parks), so
    a running fan-in is on the ring whatever this returns.
    """
    value = (raw or "").strip().lower()
    return value if value in _VALID_COUPLINGS else None


def coupling_value_removed(raw: str | None) -> bool:
    """True iff a persisted coupling value is present but NOT in
    :data:`VALID_COUPLINGS`.

    Catches a typo and — since ADR-0100 — the retired ``loopback`` token on a
    box that has not yet run a reconcile. The doctor surfaces it; the next
    reconcile pass rewrites the file. An unset / empty value is NOT "removed":
    an absent key is the ordinary state of a box the reconciler has not written
    yet.
    """
    if raw is None:
        return False
    value = raw.strip().lower()
    return bool(value) and value not in _VALID_COUPLINGS


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


#: Every spelling ``Config::from_env`` accepts for the ring, lower-cased. Kept in
#: lockstep with the Rust match arm (``rust/jasper-outputd/src/config.rs``):
#: answering a narrower set here would report a box on an alias as OFF the
#: transport it is demonstrably running.
_OUTPUTD_RING_BRIDGE_SPELLINGS = frozenset(
    {OUTPUTD_CONTENT_BRIDGE_SHM_RING, "shmring", "ring"}
)


def outputd_bridge_is_ring(raw: str | None) -> bool:
    """Is outputd on the ring, given this box's raw bridge declaration?

    UNDECLARED IS THE RING. ``None`` (key absent) and empty/whitespace both
    answer True, because that is what the daemon does with them —
    ``env_str("JASPER_OUTPUTD_CONTENT_BRIDGE", "shm_ring")`` — and this predicate
    answers what outputd IS RUNNING, not what an operator happened to type. The
    inverse reading is what made a healthy undeclared box read as a split
    transport: `check_ring_split_transport` compared a ring GRAPH against a
    not-ring ANSWER and called a playing speaker silent.

    Everything else answers False, away from the ring: a stale ``direct``, a
    retired lab spelling, a typo. Each of those makes outputd park (config.rs),
    so False is also the honest answer about what it is running.

    The accepted set mirrors the daemon's aliases exactly — see
    :data:`_OUTPUTD_RING_BRIDGE_SPELLINGS`.

    CANNOT SEE A READ FAILURE. Callers that cannot open ``outputd.env`` hand it
    an empty string, which is indistinguishable from an undeclared key; a caller
    that must tell those apart has to do so before it asks.
    """
    declared = (raw or "").strip().lower()
    return not declared or declared in _OUTPUTD_RING_BRIDGE_SPELLINGS


def dac_content_lane_marker_armed(env: "Mapping[str, str]") -> bool:
    """Is this box armed onto the bonded dac-content RETURN ring?

    Reads :data:`~jasper.multiroom.dac_content_ring.DAC_CONTENT_LANE_ENV`, whose
    single writer is ``jasper.multiroom.reconcile.outputd_grouping_env``. A BARE
    marker, so the accept-set is outputd's own ``env_bool`` vocabulary
    (:data:`OUTPUTD_ENV_BOOL_TRUE`) and ``=0`` is not armed — a reader that
    tested mere PRESENCE would call a cleared bond armed, because that writer
    clears by writing the key EMPTY.

    The lazy import is deliberate: ``jasper.multiroom.dac_content_ring`` reaches
    this module through ``jasper.ring_assets``, so naming it at module level
    would close that into a cycle.
    """
    from jasper.multiroom.dac_content_ring import DAC_CONTENT_LANE_ENV

    return _outputd_env_bool(env.get(DAC_CONTENT_LANE_ENV))


def dac_content_ring_served(env: "Mapping[str, str]") -> bool:
    """Will outputd SERVE this box off the bonded dac-content return ring?

    outputd's acceptance, mirrored key for key: the marker armed AND no bridge
    DECLARED beside it. Blank counts as undeclared because outputd reads that
    key with ``env_optional`` (``rust/jasper-outputd/src/config.rs``), which is
    exactly how the grouping writer clears the ``shm_ring`` that
    ``jasper-fanin-coupling-auto`` leaves in the first env layer.

    Marker WITHOUT that clearing is :func:`dac_content_marker_contradicted` —
    the pair outputd refuses at EX_CONFIG — so the two split the armed boxes
    between them and no reader has to guess which side it is on.
    """
    return dac_content_lane_marker_armed(env) and not _outputd_bridge_declared(env)


def dac_content_marker_contradicted(env: "Mapping[str, str]") -> bool:
    """Marker armed AND a bridge declared beside it — the pair outputd REFUSES.

    ``rust/jasper-outputd/src/config.rs`` bails EX_CONFIG on this shape, and the
    unit's ``RestartPreventExitStatus=78`` turns that into a parked daemon: the
    box is silent while every writer thinks it is bonded. Named here so the
    surfaces that report a box can report THIS rather than a healthy-looking
    ring shape.
    """
    return dac_content_lane_marker_armed(env) and _outputd_bridge_declared(env)


def _outputd_bridge_declared(env: "Mapping[str, str]") -> bool:
    """Does this env DECLARE a content bridge, as outputd's ``env_optional`` reads it?"""
    return bool((env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip())


def outputd_content_is_central_ring(env: "Mapping[str, str]") -> bool:
    """Does outputd take the CENTRAL post-DSP ring as its content source here?

    TWO KEYS, ONE QUESTION: an armed dac-content marker selects the bonded
    RETURN ring and leaves ``shm_ring`` unattached, while declaring no bridge —
    which :func:`outputd_bridge_is_ring` alone reads as the central ring.

    Takes the MERGED env (:func:`jasper.env_load.outputd_reconciled_env`),
    because the marker lives in outputd's second ``EnvironmentFile=`` layer. An
    empty mapping reads as the ring, the same as an unwritten box.
    """
    return not dac_content_lane_marker_armed(env) and outputd_bridge_is_ring(
        env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
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


def capture_kwargs_for_coupling() -> dict[str, object]:
    """Return the ``emit_sound_config`` capture kwargs for the ring.

    UNCONDITIONAL: a ``{}`` here would emit a graph whose capture names a lane
    nothing writes — a dead-lane CamillaDSP config, mid-EQ-apply, on a healthy
    box.

    The FULL end-to-end ring topology: the CamillaDSP capture device
    ``jts_ring_capture`` (Ring A, fan-in writes it) AND the playback device
    ``jts_ring_playback`` (Ring B, outputd reads it), both at the format
    :func:`resolve_ring_wire` resolves for this box, which is what makes the
    emitted config and the ring's other three declaring ends one answer instead
    of four. (outputd widens a narrow consumed slot onto its own i32 program
    spine after the copy, on its side of the ring.) The resolution is taken with
    NO topology — the shipped geometry — because nothing this function emits is
    per-topology: the devices are fixed and the format is one per box.

    THE DEVICE AXIS ONLY. CamillaDSP's latency geometry is not a fact about the
    transport devices: it is resolved per graph by
    ``camilla_config_contract.resolve_camilla_latency_for_devices`` (the box's
    floor, clamped to :func:`ring_capacity_frames` at a ring end), and only a
    graph built end-to-end on the ring passes :data:`RING_CAMILLA_GEOMETRY`
    instead.

    **THE TWO HALVES ARE NOT INTERCHANGEABLE**, which is why :func:`capture_half`
    exists. CAPTURE is topology-INVARIANT — Ring A's device is fixed, its
    ``sample_format`` is the box's own declaration (:func:`resolve_ring_wire`,
    not a per-topology axis) and its width is :data:`RING_A_CHANNELS` — so it is
    safe anywhere. PLAYBACK must never cross into an emit whose sink is already
    owned: ``jts_ring_playback`` is the STEREO Ring B, and pointing a
    ``File``/SNAPFIFO pipe (the leader's bake) or a roleful box's ACTIVE ring at
    it strands the bond or sends a full-range program to a per-driver ring.
    ``resolve_output_layout`` owns that device.
    """
    wire = resolve_ring_wire()
    return {
        "capture_device": RING_CAPTURE_DEVICE,
        "capture_format": wire.sample_format,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": wire.sample_format,
    }


#: The CAPTURE half of :func:`capture_kwargs_for_coupling`'s result — see its
#: docstring for why only this half may cross into an emit that owns its sink.
CAPTURE_HALF_KEYS = ("capture_device", "capture_format")


def capture_half(kwargs: Mapping[str, object]) -> dict[str, object]:
    """Keep only :data:`CAPTURE_HALF_KEYS` — ONE owner of which keys those are.

    For the three emits against a sink they already own (the leader's program bake
    and both carrier paths); see :func:`capture_kwargs_for_coupling` for why only
    this half may cross.
    """
    return {key: value for key, value in kwargs.items() if key in CAPTURE_HALF_KEYS}


def content_lane_format_for_coupling() -> str:
    """The CamillaDSP→outputd content-hop sample format the ring carries.

    ONE definition of that hop's width, for both of its ends:

    - CamillaDSP's emitted ``playback: format:`` — exactly what
      :func:`capture_kwargs_for_coupling` puts in ``playback_format``.
    - outputd's requested ``JASPER_OUTPUTD_CONTENT_FORMAT`` — the audio-hardware
      reconciler emits this value, so the reader cannot ask for a width the
      writer does not emit. Deriving both from the same function is what makes
      that a structural property instead of two constants a maintainer must
      remember to move together.

    So this answers :func:`resolve_ring_wire`'s ``sample_format`` for this box —
    read back OUT of the emit kwargs rather than from the resolver directly, so
    an emit that ever stopped forcing the ring's own width is visible here (and
    to ``ring_edge_width_ready``, which compares the ends) instead of being
    papered over by a second read of the same resolver.

    NOT a sink-type axis: a bonded leader's File/pipe sink is pinned to
    ``DEFAULT_PIPE_SINK_FORMAT`` (D4) and does not write this hop at all — its
    outputd content lane is fed by the endpoint-crossover CamillaDSP instance.
    Callers that need the format for an arbitrary sink want
    ``jasper.camilla_config_contract`` instead.
    """
    # Local import: this module stays stdlib-only at import time for the
    # socket-activated web surfaces (see the module docstring).
    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT

    value = capture_kwargs_for_coupling().get("playback_format")
    if isinstance(value, str) and value:
        return value
    return DEFAULT_PLAYBACK_FORMAT


def coupling_capture_kwargs_from_env() -> dict[str, object]:
    """The live ``emit_sound_config`` capture kwargs — always the ring's.

    The one call shape a config emitter uses to thread the SHARED fan-in→Camilla
    coupling into a live re-emit. It consults NO env: the ring is the only
    central transport (ADR-0100), so there is nothing for a token to select and
    no unresolved token can make this answer ``{}`` — which would re-emit a
    graph capturing a lane fan-in does not write, silently, in the middle of a
    ``/sound/`` or ``/correction/`` save.
    """
    return capture_kwargs_for_coupling()
