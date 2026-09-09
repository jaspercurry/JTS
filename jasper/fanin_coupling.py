# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in → CamillaDSP coupling vocabulary (ring devices, wire, emit kwargs).

The single source of truth for HOW the fan-in mixer's summed program reaches
CamillaDSP's capture. ONE transport: ``shm_ring``, the end-to-end SHM-ring path
(Ring A + Ring B). fan-in writes Ring A (program.ring) that CamillaDSP captures
via ``jts_ring_capture``; CamillaDSP writes its post-DSP program to Ring B (or
to the ACTIVE ring on an armed roleful box). See ADR-0100 — a topology the ring
cannot serve parks under its own name
(:mod:`jasper.control.transport_eligibility`); it never falls back.

This module is import-cheap (stdlib plus :mod:`jasper.env_file`) so
socket-activated web surfaces and the config emitters can resolve the ring
without pulling in NumPy/SciPy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypedDict, cast

from jasper.env_file import read_value

# Ring A: fan-in writes an SPSC SHM ring (``jasper_ring::RingWriter``) that
# CamillaDSP reads via the CAPTURE direction of the ``jts_ring`` ioplug. The Rust
# ``Coupling::ShmRing`` normalizer MUST agree with this token.
COUPLING_SHM_RING = "shm_ring"

# Ring A SHM ring file + slot-count env vars. fan-in creates the ring at
# ``JASPER_FANIN_RING_PATH`` with ``JASPER_FANIN_RING_SLOTS`` slots; the Rust
# daemon resolves both with the SAME defaults (``config.rs``). The n_slots <->
# JASPER_FANIN_RING_SLOTS pairing is the drift axis with the ioplug conf.d
# geometry, validated by the ring header at attach.
RING_PATH_ENV_VAR = "JASPER_FANIN_RING_PATH"
DEFAULT_FANIN_RING_PATH = "/dev/shm/jts-ring/program.ring"
RING_SLOTS_ENV_VAR = "JASPER_FANIN_RING_SLOTS"
# Ring A/B slot size in frames. Compile-time on the other three ends with no env
# override, so this is the only slot size the transport carries: mirrors
# rust/jasper-ring/src/layout.rs RING_SLOT_FRAMES and c/jts-ring-ioplug/
# pcm_jts_ring.c JTS_RING_DEFAULT_PERIOD. jasper.ring_assets.render_ring_conf_wire
# refuses any other period — the ioplug would attach against a geometry fan-in
# never builds and crash at arm instead of refusing.
RING_SLOT_FRAMES = 128
DEFAULT_FANIN_RING_SLOTS = 2


def ring_capacity_frames() -> int:
    """Frames the whole ring holds — the ALSA buffer size its ioplug reports.

    The bound a CamillaDSP ``chunksize`` crossing the ring has to clear:
    CamillaDSP sets ``avail_min`` to its chunk, and ALSA refuses an ``avail_min``
    larger than the device's buffer. A property of the TRANSPORT, not of the
    fitted DAC — both factors are compile-time constants shared by the fan-in
    writer and the ioplug, so every box's ring is the same size. Not env-derived:
    the ioplug takes its slot count from the conf.d block, and a disagreeing pair
    fails the attach rather than resizing anything.
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


# The geometry a graph built END-TO-END on the ring passes EXPLICITLY. Certified
# together: chunk 128 is one ring slot, queuelimit 1 makes the slot handshake
# blocking, and rate_adjust is off because that leaves the rate controller
# nothing to steer.
#
# NOT the fallback for an ordinary sound/correction graph. Those carry the box's
# own floor clamped to the ring's capacity
# (``camilla_latency.resolve_camilla_latency_for_devices``), so moving them onto
# this pair is a retune with a listening test.
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
# ``JASPER_FANIN_RING_WIRE_FORMAT``, outputd's ``JASPER_OUTPUTD_CONTENT_FORMAT``,
# and CamillaDSP's emitted capture/playback ``format:``. They map onto the
# header's ``sample_format`` ids (``jasper.ring_assets.RING_SAMPLE_FORMAT_*``),
# which the attach compares field-by-field.
#
# ``RING_WIRE_FORMAT`` is the NARROW token specifically — the C ioplug's
# compiled-in default and the operator's rollback token. Which of the two a box
# carries is :func:`resolve_ring_wire`'s answer, and the resolver's default is
# :data:`RING_WIRE_FORMAT_WIDE`.
RING_WIRE_FORMAT = "S16_LE"
RING_WIRE_FORMAT_WIDE = "S32_LE"
RING_WIRE_FORMATS = (RING_WIRE_FORMAT, RING_WIRE_FORMAT_WIDE)

# THE BOX'S DECLARED RING WIRE — one key, read identically by both languages
# (Rust in ``jasper_fanin::config``'s ``RingWireFormat::from_env_value``). It is
# the ONLY input to the wire's format axis; every other end of the ring is
# derived from it and compared anyway by ``ring_edge_width_ready``, because the
# ends land in files written at different times.
#
# THE KEY HAS NO WRITER, AND THAT IS WHAT MAKES IT A ROLLBACK LEVER: the only
# reason to set it is to pin a box NARROW, and a lever a reconciler could rewrite
# on the next pass would not be one. ``tests/test_ring_wire_format_contract.py``
# pins the empty writer set.
RING_WIRE_FORMAT_ENV_VAR = "JASPER_FANIN_RING_WIRE_FORMAT"

# Ring A's channel count. fan-in's mixer is stereo and not configurable
# (``mixer.rs``'s ``CHANNELS: u32 = 2``), so Ring A is 2 on every box — unlike
# Ring B's, this is not a per-topology axis. Mirrors
# ``jasper.active_speaker.runtime_contract.RING_STEREO_PROGRAM_CHANNELS``, the
# same number reached from the topology side; a contract test pins them equal.
RING_A_CHANNELS = 2

# ---------------------------------------------------------------------------
# Ring B (camilla -> outputd playback bridge): CamillaDSP writes its post-DSP
# stereo program to content.ring via the ``jts_ring_playback`` ioplug, which
# jasper-outputd reads one slot per DAC period.
#
# The env keys below are read by the Rust ``jasper-outputd`` daemon
# (``rust/jasper-outputd/src/config.rs``) and pinned here so the Python control
# plane names the same bridge the daemon reads. Ring A and Ring B both hold the
# 2-slot latency floor but stay SEPARATE ring files, so one can be tuned without
# the other.
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
# default (``rust/jasper-outputd/src/config.rs``), NOT whatever
# :func:`resolve_ring_wire` would pick for this box. A reader that followed the
# resolver here would refuse an arm for a wire the daemon has in fact declared.
OUTPUTD_DEFAULT_CONTENT_FORMAT = "S16_LE"

# The CamillaDSP→outputd content hop's width on the snd-aloop lanes. Wide, so
# CamillaDSP's float math stays wide all the way to outputd's i32 program spine
# and the ONE deliberate output quantization happens at the DAC edge, at the
# DAC's own declared width — outputd's mixing, ducking and trim then do their
# arithmetic on full-resolution content.
#
# Both other carriers of this width are derived rather than restated:
# ``deploy/camilladsp/outputd-cutover.yml`` on both ring halves, and outputd's
# ``JASPER_OUTPUTD_CONTENT_FORMAT`` through
# :func:`content_lane_format_for_coupling`.
DEFAULT_PLAYBACK_FORMAT = "S32_LE"

# Ring B playback device — the WRITE direction of the same ``jts_ring`` plugin
# whose CAPTURE direction is ``jts_ring_capture``. Its wire is whatever
# :func:`resolve_ring_wire` resolves for the box: the layout's accept-set
# (``jasper_ring::Geometry::validate_self``) admits both S16LE and S32LE, so the
# resolver, not the layout, holds the wire to one of them.
RING_PLAYBACK_DEVICE = "jts_ring_playback"

# ---------------------------------------------------------------------------
# The ACTIVE ring — a THIRD ring file and ioplug PCM, carrying a roleful box's
# POST-crossover per-driver program from CamillaDSP to outputd. The role is
# carried in the NAME, not inferred from a width: on a two-way roleful box the
# active lane is also 2 channels, so no channel-count test can tell it apart
# from Ring B's stereo program.
#
# THE SPELLING IS LOAD-BEARING. ``_forbidden_playback_token``
# (:mod:`jasper.active_speaker.camilla_yaml`) is a case-insensitive SUBSTRING
# test over ``FORBIDDEN_ACTIVE_PLAYBACK_TOKENS``, which carries Ring B's name.
# ``"jts_ring_playback" in "jts_ring_active_playback"`` is False, so this
# spelling is safe, while ``jts_ring_playback_active`` would self-block every
# active emit. Both directions are pinned by
# ``tests/test_ring_active_endpoint.py``.
RING_ACTIVE_PLAYBACK_DEVICE = "jts_ring_active_playback"
DEFAULT_OUTPUTD_ACTIVE_RING_PATH = "/dev/shm/jts-ring/active-content.ring"

# The reconciler's marker that outputd's endpoint IS the active ring. Written by
# ``deploy/bin/jasper-audio-hardware-reconcile`` in the SAME helper, from the
# SAME decision, as ``JASPER_OUTPUTD_ACTIVE_LANE``: one fact with two consumers.
# outputd bails on the incoherent pair (marker without the lane), and under the
# ``shm_ring`` bridge enforces the biconditional "the active ring path may be
# read ONLY by an armed active endpoint, and an armed active endpoint may read
# ONLY the active ring path".
OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR = "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT"


# ---------------------------------------------------------------------------
# The named transport SHAPES. ``TransportTopology.name`` is the discriminator
# every consumer matches on, so each distinct transport gets its own name: an
# exhaustive match over named shapes fails LOUD on one nobody handled.
#
# ``shm_ring_active`` is selected on the PERSISTED COUPLING plus the reconciler's
# endpoint MARKER, deliberately NOT on the observed ``camilla_playback_device``:
# selecting on the observed device would make
# :func:`jasper.transport_coherence.transport_coherence_report`'s playback
# comparison vacuous.
TRANSPORT_SHM_RING_ACTIVE = "shm_ring_active"
# One END of the box is off the one transport (ADR-0100): a coupling or bridge
# declaration a daemon parks on. Not a second route: jasper.control.transport_eligibility
# is what names such a box. The ring MARKER's shape is NOT this one — see
# TRANSPORT_DAC_CONTENT_RING below, which is served.
TRANSPORT_OFF_RING = "off_ring"
# A DUMB bonded member: outputd's content comes off the dac-content RETURN ring
# and no CENTRAL post-DSP ring is attached. Its own shape rather than
# TRANSPORT_OFF_RING, whose comparisons assume nothing is feeding outputd —
# while Ring A is still live here and must keep being compared.
TRANSPORT_DAC_CONTENT_RING = "dac_content_ring"
# Every named shape, so an exhaustive consumer can assert it handled one.
TRANSPORT_SHAPES = frozenset(
    (
        TRANSPORT_OFF_RING,
        COUPLING_SHM_RING,
        TRANSPORT_SHM_RING_ACTIVE,
        TRANSPORT_DAC_CONTENT_RING,
    )
)
# Every shape whose post-DSP hop is an SHM ring CamillaDSP drives. Membership,
# never a ``==`` on one name: a consumer that tested only ``shm_ring`` would
# silently take its OFF-RING arm on an active-ring box. The dac-content shape is
# NOT a member — its post-DSP hop is a ring CamillaDSP does not drive, so every
# camilla-endpoint comparison here is meaningless there.
RING_TRANSPORT_SHAPES = frozenset((COUPLING_SHM_RING, TRANSPORT_SHM_RING_ACTIVE))


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

    ``env`` is authoritative when passed. ``None`` reads the persisted
    ``outputd.env`` FILE FRESH: the socket-activated wizards and the long-lived
    control daemon never ``EnvironmentFile=`` it and stay alive across a
    reconcile, so ``os.environ`` is a stale reader of this key. Fail-SAFE to
    False on an unreadable file — an indeterminate marker must never assert an
    active-ring endpoint.
    """
    if env is None:
        from jasper.env_load import OUTPUTD_ENV_PATH  # lazy: read at call time

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

    ``n_slots`` is deliberately NOT an axis here even though the attach compares
    it: it has per-ring owners already (:func:`resolve_ring_slots`,
    :func:`resolve_outputd_ring_slots`) that read env this object cannot see.

    ``ring_active_channels`` is the ACTIVE ring's width, kept separate from
    ``ring_b_channels`` because one field per ring END is what keeps a 2-way
    box's identical widths from hiding a crossed answer. ``None`` means this box
    has no active ring.
    """

    sample_format: str
    ring_a_channels: int
    ring_b_channels: int
    period_frames: int
    ring_active_channels: int | None = None


def resolve_ring_wire_format(raw: str | None) -> str:
    """Normalize a raw :data:`RING_WIRE_FORMAT_ENV_VAR` value to a wire token.

    The Python half of a two-language parse: ``jasper-fanin`` normalizes the same
    key in ``RingWireFormat::from_env_value`` (``rust/jasper-fanin/src/config.rs``)
    and must classify every input the same way.

    - unset, or empty after trimming → :data:`RING_WIRE_FORMAT_WIDE`. Empty is
      how this repo's env-file writers clear a key. The default is WIDE because
      narrow would be a width regression on the hop the ring replaces, which
      already carries :data:`DEFAULT_PLAYBACK_FORMAT`;
    - exactly ``S16_LE`` / ``S32_LE`` after trimming → that token. The match is
      case-SENSITIVE because the C ioplug's own ``strcmp`` is: accepting a
      spelling the ioplug rejects would resolve a wire no reader can open;
    - anything else → :class:`ValueError`. Fail loud, never fall back: fan-in
      treats the same value as a config-class fault and parks at exit 78, so a
      Python fallback would give one typo two verdicts.

    ``tests/test_ring_wire_format_contract.py`` pins this against the Rust
    source.
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
    Not ``os.environ``: the callers are socket-activated wizards and long-lived
    daemons that never loaded ``fanin.env``.

    A file that cannot be read contributes nothing — an absent ``fanin.env`` is
    the ordinary unarmed state — but a readable file declaring an unrecognized
    value raises, exactly as fan-in would.
    """
    from jasper.env_load import BASE_ENV_PATH, FANIN_ENV_PATH  # lazy: read at call time

    for path in (FANIN_ENV_PATH, BASE_ENV_PATH):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        raw = read_value(text, RING_WIRE_FORMAT_ENV_VAR)
        if raw is not None:
            return resolve_ring_wire_format(raw)
    return RING_WIRE_FORMAT_WIDE


def assistant_wire_is_wide(*, wire_format: str | None = None) -> bool:
    """Whether THIS BOX's ASSISTANT IPC wire is wide (S32 at the i32 spine scale).

    The Python mirror of ``Config::program_wire_is_wide``, which calls
    ``jasper_tts_protocol::TtsWireWidth::from_box_declaration``;
    :mod:`tests.test_ring_wire_format_contract` pins the two by reading the Rust
    source.

    ``wire_format`` defaults to a FILE-FRESH read
    (:func:`read_declared_ring_wire_format`) because the callers never loaded
    ``fanin.env``. Passing it explicitly is authoritative, with no file fallback.
    """
    if wire_format is None:
        wire_format = read_declared_ring_wire_format()
    return wire_format == RING_WIRE_FORMAT_WIDE


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
      :func:`read_declared_ring_wire_format`. The layout's accept-set holds both
      tokens, so which one a box carries is a DECLARATION, not a policy
      constant. The shipped conf.d declares the wide token in every block rather
      than omitting the key, because the C ioplug's own default is the narrow
      one (:data:`~jasper.ring_assets.RING_CONF_DEFAULT_FORMAT`) and silence
      would mean the opposite of what the resolver answers.
    - ``ring_a_channels`` — :data:`RING_A_CHANNELS` on every box.
    - ``ring_b_channels`` — from
      :func:`~jasper.active_speaker.runtime_contract.ring_channels_for_topology`.
      A topology with no ring width (roleful, composite, explicit mono) falls
      back to the shipped stereo declaration, which is what that box's conf.d
      says and what an open-probe of it must ask for. Whether such a box may ARM
      is ``topology_supports_shm_ring``'s and the arm preflights' question.
    - ``period_frames`` — :data:`RING_SLOT_FRAMES`, fan-in's compile-time slot
      size.
    - ``ring_active_channels`` — from
      :func:`~jasper.active_speaker.runtime_contract.active_ring_channels_for_topology`,
      and ``None`` on every box that is not roleful. A different question from
      ``ring_b_channels``: the two rings coexist on a roleful box and carry
      different programs, and a single answer would stamp the active width into
      Ring B's conf block invisibly on a 2-way box.
    """
    ring_b_channels = RING_A_CHANNELS
    ring_active_channels: int | None = None
    if topology is not None:
        from jasper.active_speaker.runtime_contract import (  # lazy: import cost, this module is imported by the socket-activated wizards
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


# Every ALSA PCM name the ring ioplug owns, in ring order (A, B, ACTIVE) — the
# set answering "is this end of the graph a ring end?" for both the emitter side
# and the arm gate.
RING_PCM_DEVICES = (
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    RING_ACTIVE_PLAYBACK_DEVICE,
)

# The transport's observability token, reported by the driver-commission journal
# lines and ``/state``'s commissioning block. Observability, not config: nothing
# parses it back.
TRANSPORT_RING = "ring"


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
    clamping: the ioplug conf.d block and the daemon would then disagree on the
    ring depth. The range :data:`RING_SLOTS_MIN`..=:data:`RING_SLOTS_MAX` mirrors
    the ring header's ``MIN_N_SLOTS`` / ``MAX_N_SLOTS`` and ``config.rs``'s
    ``RING_SLOTS_MIN`` / ``RING_SLOTS_MAX``, which ``anyhow::bail!``s on the same
    range.
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

    UNDECLARED IS THE RING: ``None`` and empty/whitespace answer True, because
    the daemon reads that key as
    ``env_str("JASPER_OUTPUTD_CONTENT_BRIDGE", "shm_ring")`` and this predicate
    answers what outputd IS RUNNING. Everything else answers False — each of
    those makes outputd park (config.rs), so False is the honest answer there
    too. The accepted set mirrors the daemon's aliases
    (:data:`_OUTPUTD_RING_BRIDGE_SPELLINGS`).

    CANNOT SEE A READ FAILURE: callers that cannot open ``outputd.env`` hand it
    an empty string, indistinguishable from an undeclared key.
    """
    declared = (raw or "").strip().lower()
    return not declared or declared in _OUTPUTD_RING_BRIDGE_SPELLINGS


def dac_content_lane_marker_armed(env: "Mapping[str, str]") -> bool:
    """Is this box armed onto the bonded dac-content RETURN ring?

    Reads :data:`~jasper.multiroom.dac_content_ring.DAC_CONTENT_LANE_ENV`, whose
    single writer is ``jasper.multiroom.reconcile.outputd_grouping_env``. A BARE
    marker, so the accept-set is outputd's own ``env_bool`` vocabulary
    (:data:`OUTPUTD_ENV_BOOL_TRUE`) and ``=0`` is not armed — a reader testing
    mere PRESENCE would call a cleared bond armed, since that writer clears by
    writing the key EMPTY.
    """
    from jasper.multiroom.dac_content_ring import (  # lazy: cycle — that module imports this one at module scope
        DAC_CONTENT_LANE_ENV,
    )

    return _outputd_env_bool(env.get(DAC_CONTENT_LANE_ENV))


def dac_content_ring_served(env: "Mapping[str, str]") -> bool:
    """Will outputd SERVE this box off the bonded dac-content return ring?

    outputd's acceptance, mirrored key for key: the marker armed AND no bridge
    DECLARED beside it. Blank counts as undeclared because outputd reads that key
    with ``env_optional`` (``rust/jasper-outputd/src/config.rs``), which is how
    the grouping writer clears the ``shm_ring`` that
    ``jasper-fanin-coupling-auto`` leaves in the first env layer.

    Marker WITHOUT that clearing is :func:`dac_content_marker_contradicted`, the
    pair outputd refuses at EX_CONFIG.
    """
    return dac_content_lane_marker_armed(env) and not _outputd_bridge_declared(env)


def dac_content_marker_contradicted(env: "Mapping[str, str]") -> bool:
    """Marker armed AND a bridge declared beside it — the pair outputd REFUSES.

    ``rust/jasper-outputd/src/config.rs`` bails EX_CONFIG on this shape, and the
    unit's ``RestartPreventExitStatus=78`` turns that into a parked daemon: the
    box is silent while every writer thinks it is bonded.
    """
    return dac_content_lane_marker_armed(env) and _outputd_bridge_declared(env)


def _outputd_bridge_declared(env: "Mapping[str, str]") -> bool:
    """Does this env DECLARE a content bridge, as outputd's ``env_optional`` reads it?"""
    return bool((env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip())


def outputd_content_is_central_ring(env: "Mapping[str, str]") -> bool:
    """Does outputd take the CENTRAL post-DSP ring as its content source here?

    TWO KEYS, ONE QUESTION: an armed dac-content marker selects the bonded RETURN
    ring and leaves ``shm_ring`` unattached while declaring no bridge — which
    :func:`outputd_bridge_is_ring` alone would read as the central ring.

    Takes the MERGED env (:func:`jasper.env_load.outputd_reconciled_env`): the
    marker lives in outputd's second ``EnvironmentFile=`` layer. An empty mapping
    reads as the ring, the same as an unwritten box.
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
    ``jasper_ring::{MIN_N_SLOTS, MAX_N_SLOTS}`` (``rust/jasper-ring/layout.json``).
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
    nothing writes — a dead-lane CamillaDSP config on a healthy box.

    Both ends at the format :func:`resolve_ring_wire` resolves for this box,
    which is what makes the emitted config and the ring's other declaring ends
    one answer. Resolved with NO topology: the devices are fixed and the format
    is one per box.

    THE DEVICE AXIS ONLY. CamillaDSP's latency geometry is resolved per graph by
    ``camilla_latency.resolve_camilla_latency_for_devices`` (the box's floor,
    clamped to :func:`ring_capacity_frames` at a ring end); only a graph built
    end-to-end on the ring passes :data:`RING_CAMILLA_GEOMETRY` instead.

    **THE TWO HALVES ARE NOT INTERCHANGEABLE**, which is why :func:`capture_half`
    exists. CAPTURE is topology-invariant and safe anywhere. PLAYBACK must never
    cross into an emit whose sink is already owned: ``jts_ring_playback`` is the
    STEREO Ring B, and pointing a ``File``/SNAPFIFO pipe (the leader's bake) or a
    roleful box's ACTIVE ring at it strands the bond or sends a full-range
    program to a per-driver ring. ``resolve_output_layout`` owns that device.
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

    ONE definition of that hop's width, for both of its ends: CamillaDSP's
    emitted ``playback: format:`` and outputd's requested
    ``JASPER_OUTPUTD_CONTENT_FORMAT``. Read back OUT of the emit kwargs rather
    than from the resolver directly, so an emit that stopped forcing the ring's
    own width shows up here instead of being papered over by a second read of
    the same resolver.

    NOT a sink-type axis: a bonded leader's File/pipe sink is pinned to
    ``DEFAULT_PIPE_SINK_FORMAT`` and does not write this hop at all. Callers that
    need the format for an arbitrary sink want
    ``jasper.camilla_config_contract`` instead.
    """
    value = capture_kwargs_for_coupling().get("playback_format")
    if isinstance(value, str) and value:
        return value
    return DEFAULT_PLAYBACK_FORMAT


def coupling_capture_kwargs_from_env() -> dict[str, object]:
    """The live ``emit_sound_config`` capture kwargs — always the ring's.

    Consults NO env: the ring is the only central transport (ADR-0100), so no
    unresolved token can make this answer ``{}`` — which would re-emit a graph
    capturing a lane fan-in does not write, mid-save.
    """
    return capture_kwargs_for_coupling()
