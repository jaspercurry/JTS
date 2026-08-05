# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where the ``jts_ring`` transport platform assets live, and are they present.

The single source of truth for the three inert ring-platform assets P1 ships:
the compiled ioplug ``.so``, the conf.d PCM definitions
(``pcm.jts_ring_capture`` / ``pcm.jts_ring_playback``), and the
``/dev/shm/jts-ring`` tmpfs directory. Two consumers share this SSOT so the
"which files must exist" contract never drifts:

- ``jasper.cli.doctor.audio_runtime.check_ring_platform_assets`` — the
  deploy-time health
  probe (also open-probes the PCMs; that lives in the doctor because it needs
  ``arecord``/``aplay``).
- ``jasper.fanin.coupling_reconcile`` — the ``shm_ring`` **activation gate**: the
  reconciler refuses to ARM the ring coupling when an asset is missing and
  fail-safes to loopback, so a half-installed ring platform can never strand the
  realtime path (the ioplug would fail to resolve and CamillaDSP would crash-loop
  on its statefile). Presence-only here — an open-probe from the reconciler could
  disturb a live arm, and the doctor already owns the deep probe.
- ``jasper.cli.audio_config render-ring-conf-period`` — the per-box conf.d
  **renderer** the output-hardware reconciler shells into. It reuses this
  module's ``period_frames`` regex to REWRITE the value it also parses, so the
  reader and the writer of the conf.d format cannot drift.

Import-cheap (stdlib, plus the import-free ``jasper.fanin_coupling`` constants)
so the reconciler and the socket-activated web surfaces can resolve asset
presence without pulling in the doctor.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from jasper.fanin_coupling import RING_SLOT_FRAMES

# The aarch64 ALSA plugin dir the ioplug ``.so`` installs into (the Pi 5 target).
# Duplicated as a literal in ``jasper.cli.doctor.audio_runtime`` historically;
# this is now the shared home. The build/install path is
# ``deploy/lib/install/ring-platform.sh``.
RING_ALSA_PLUGIN_DIR = "/usr/lib/aarch64-linux-gnu/alsa-lib"
RING_IOPLUG_SO = "libasound_module_pcm_jts_ring.so"
RING_CONF_D = "/etc/alsa/conf.d/60-jts-ring.conf"
# The tmpfs directory the ring files live in (shipped by
# ``deploy/tmpfiles/jts-ring.conf``, mode 2775 root:jasper).
RING_SHM_DIR = "/dev/shm/jts-ring"
# Ring A (fan-in -> CamillaDSP program) and Ring B (CamillaDSP -> outputd content)
# on-disk ring files under RING_SHM_DIR. Basenames match the conf.d ``path``
# values (``jts_ring_capture`` -> program.ring, ``jts_ring_playback`` ->
# content.ring) and the Rust defaults. Ring A is the one whose slot geometry the
# fan-in ``JASPER_FANIN_RING_SLOTS`` env and the conf.d ``jts_ring_capture``
# ``n_slots`` must agree on (the defect-A coherence axis).
RING_A_PROGRAM_FILE = os.path.join(RING_SHM_DIR, "program.ring")
RING_B_CONTENT_FILE = os.path.join(RING_SHM_DIR, "content.ring")
# The conf.d PCM block name for Ring A (fan-in's program ring). ``n_slots`` under
# this block is the drift axis with ``JASPER_FANIN_RING_SLOTS`` (Ring B is the
# ``jts_ring_playback`` block, paired with ``JASPER_OUTPUTD_SHM_RING_SLOTS``).
RING_A_CONF_PCM = "jts_ring_capture"
RING_B_CONF_PCM = "jts_ring_playback"


def ring_ioplug_so_path(*, plugin_dir: str = RING_ALSA_PLUGIN_DIR) -> str:
    """Absolute path of the installed ioplug ``.so``."""
    return os.path.join(plugin_dir, RING_IOPLUG_SO)


@dataclass(frozen=True)
class RingAssetPresence:
    """Which ring-platform assets are present on disk. Presence, not health."""

    so_present: bool
    conf_present: bool
    shm_dir_present: bool

    @property
    def all_present(self) -> bool:
        return self.so_present and self.conf_present and self.shm_dir_present

    def missing(self) -> tuple[str, ...]:
        """Human-readable list of the absent assets (empty when all present)."""
        out: list[str] = []
        if not self.so_present:
            out.append(f"ioplug .so absent ({ring_ioplug_so_path()})")
        if not self.conf_present:
            out.append(f"conf.d absent ({RING_CONF_D})")
        if not self.shm_dir_present:
            out.append(f"{RING_SHM_DIR} absent")
        return tuple(out)


def ring_asset_presence(
    *,
    plugin_dir: str = RING_ALSA_PLUGIN_DIR,
    conf_d: str = RING_CONF_D,
    shm_dir: str = RING_SHM_DIR,
) -> RingAssetPresence:
    """Snapshot which of the three ring-platform assets are present on disk.

    Pure filesystem stat — no ALSA open, no subprocess, leaves no residue. Args
    are injectable so tests can repoint the paths at a tmpdir.
    """
    return RingAssetPresence(
        so_present=os.path.exists(os.path.join(plugin_dir, RING_IOPLUG_SO)),
        conf_present=os.path.exists(conf_d),
        shm_dir_present=os.path.isdir(shm_dir),
    )


# The ring's slot geometry IS fixed at ``RING_SLOT_FRAMES`` (128). jasper-fanin
# creates Ring A with that COMPILE-TIME constant
# (rust/jasper-fanin/src/config.rs, no env override) and both conf.d PCM blocks
# share one period value, so the conf.d period is pinned to it too — this file
# is not free to follow a DAC. Making the slot derivable is issue #2147.
#
# The mismatch this parser exists to catch is the OTHER side: the
# ``jts_ring_playback`` ioplug opens Ring B with the conf.d's ``period_frames``,
# and jasper-outputd's ``ShmRingSource`` attaches with
# ``JASPER_OUTPUTD_PERIOD_FRAMES`` (one slot per DAC period — see
# rust/jasper-outputd/src/config.rs "the ring's period_frames is always
# outputd's period_frames"). A geometry mismatch against an existing ring is a
# hard ``open()`` error (c/jts-ring-ioplug: "a geometry mismatch against an
# existing ring is an open() error"). On a box whose resolved outputd period is
# not 128 (the packaged default is 1024, and only a DAC declaring a 128-frame
# latency floor lowers it), CamillaDSP's ring open would fail and the arm would
# roll back with a confusing daemon-level error — so the coupling reconciler
# PREFLIGHTs the match and fail-closes to loopback with a crisp reason. The fix
# is always to bring the OUTPUTD period to the slot, never to raise this file.
#
# :func:`render_ring_conf_period` therefore has exactly one live job: converging
# a conf.d that has drifted OFF ``RING_SLOT_FRAMES`` (a hand edit, a half
# install) back onto it. It refuses any other target. (scripts/ring-proto/arm.sh
# renders the conf period from outputd's resolved env per box — that is the lab
# prototype, which predates the fixed-slot product path and is not this rule.)
#
# One regex, two directions: the ``indent``/``frames`` groups let the renderer
# rewrite exactly the lines this parser reads, so a conf.d the parser accepts is
# a conf.d the renderer can update (and vice versa). Horizontal-whitespace
# classes (not ``\s``) keep both directions line-scoped under ``re.MULTILINE``.
_RING_CONF_PERIOD_RE = re.compile(
    r"^(?P<indent>[^\S\n]*)period_frames[^\S\n]+(?P<frames>\d+)[^\S\n]*$",
    re.MULTILINE,
)


def ring_conf_period_frames(conf_d: str | None = None) -> int | None:
    """Parse the ``period_frames`` pinned in the ring conf.d, or None.

    Returns the single period value the ``jts_ring_*`` PCM blocks declare (both
    Ring A and Ring B share one slot geometry). ``None`` when the file is absent,
    unreadable, has no ``period_frames`` line, or declares *inconsistent* values
    across the two PCMs (a torn conf.d — the caller treats that as a mismatch, not
    a silent pick). Pure text parse, no ALSA. ``conf_d=None`` resolves
    :data:`RING_CONF_D` at CALL time (not a bound default) so a test / caller that
    repoints the module constant is honored.
    """
    path = RING_CONF_D if conf_d is None else conf_d
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    values = {int(m.group("frames")) for m in _RING_CONF_PERIOD_RE.finditer(text)}
    if len(values) != 1:
        # No period line, or the two PCMs disagree — not a usable single geometry.
        return None
    return next(iter(values))


@dataclass(frozen=True)
class RingConfPeriodRender:
    """The outcome of rendering the ring conf.d slot period for one box.

    ``changed`` is False for the two no-write outcomes — the conf already
    declares the target period, which is the golden case on an Apple box where
    the declared floor equals the shipped 128.
    """

    changed: bool
    period_frames: int
    previous_period_frames: int | None
    conf_d: str


def render_ring_conf_period(
    period_frames: int,
    *,
    conf_d: str | None = None,
) -> RingConfPeriodRender:
    """Rewrite the ring conf.d ``period_frames`` lines to ``period_frames``.

    The ring slot IS one outputd DAC period, so a box whose DAC declares a
    latency floor needs the conf.d to pin that floor's period. This is the
    per-box render the conf.d's own header calls for; the CALLER decides
    whether a render is warranted (the rule is "only from a DECLARED
    :class:`~jasper.audio_hardware.dac.LatencyFloor`"), so this function never
    consults the DAC registry itself.

    **The only renderable period is** :data:`~jasper.fanin_coupling.RING_SLOT_FRAMES`.
    Ring A's slot size is fan-in's COMPILE-TIME constant
    (``rust/jasper-fanin/src/config.rs`` ``RING_SLOT_FRAMES``, with no env
    override; ``mixer.rs`` creates the ring with it), so writing any other
    period into ``pcm.jts_ring_capture`` would make CamillaDSP's ioplug attach
    expect a geometry fan-in never builds — a hard ``RING_ATTACH_FATAL``
    ("ring header does not match expected geometry") that CRASHES shm_ring at
    arm rather than refusing it. Asking for a different period is therefore a
    caller bug and raises; making the slot floor-derived across fan-in, the
    ioplug, the CamillaDSP emitter, and the negotiation model is issue #2147.
    This guard is defence in depth behind the caller's own floor gate.

    **Write-on-change only.** When the conf already declares exactly
    ``period_frames`` the file is left GENUINELY untouched — no rewrite, no
    mtime churn — so a box that renders to the shipped value is byte-identical
    to one that never rendered. Otherwise the whole file is published through
    :func:`jasper.atomic_io.atomic_write_text` (``preserve_target_stat``), so a
    reader never observes a half-written conf.d and the installed file's
    uid/gid/mode survive the replace.

    Only the ``period_frames`` VALUES move: indentation, ``n_slots``, the
    ``path`` values, and every comment survive verbatim, because the rewrite is
    a substitution over the same regex :func:`ring_conf_period_frames` reads.

    Raises ``ValueError`` for a target that is not ``RING_SLOT_FRAMES`` or a
    conf.d that declares no ``period_frames`` line at all (a torn / foreign
    file — never invent one), and ``OSError`` when the file cannot be read or
    replaced.
    """
    if period_frames != RING_SLOT_FRAMES:
        raise ValueError(
            f"period_frames must equal RING_SLOT_FRAMES ({RING_SLOT_FRAMES}), "
            f"got {period_frames}: Ring A's slot size is fan-in's compile-time "
            "constant, so any other conf.d period fails the ioplug attach. "
            "Refuse the render instead (see issue #2147)"
        )
    path = RING_CONF_D if conf_d is None else conf_d
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    previous = [int(m.group("frames")) for m in _RING_CONF_PERIOD_RE.finditer(text)]
    if not previous:
        raise ValueError(
            f"ring conf.d ({path}) declares no period_frames line; refusing to "
            "invent one — redeploy to reinstall it"
        )
    # A torn conf.d (the two PCMs disagreeing) has no single previous value to
    # report, but it is still rendered: converging both lines onto the target is
    # exactly the repair. Mirrors ring_conf_period_frames returning None there.
    distinct = set(previous)
    if distinct == {period_frames}:
        return RingConfPeriodRender(
            changed=False,
            period_frames=period_frames,
            previous_period_frames=period_frames,
            conf_d=path,
        )
    rendered = _RING_CONF_PERIOD_RE.sub(
        lambda m: f"{m.group('indent')}period_frames {period_frames}",
        text,
    )
    # Function-local so the module keeps its stdlib-only import cost for the
    # presence/parse callers (the coupling reconciler and the socket-activated
    # web surfaces); only the renderer pays for atomic_io. preserve_target_stat
    # carries the installed file's uid/gid/mode across the replace, so the 0644
    # renderer-user resolvability the conf.d header depends on survives, and a
    # root-run reconcile does not re-own a file it did not create.
    from jasper.atomic_io import atomic_write_text

    atomic_write_text(path, rendered, preserve_target_stat=True)
    return RingConfPeriodRender(
        changed=True,
        period_frames=period_frames,
        previous_period_frames=previous[0] if len(distinct) == 1 else None,
        conf_d=path,
    )


@dataclass(frozen=True)
class RingGeometryMatch:
    """Whether the conf.d ring period matches outputd's resolved DAC period."""

    ok: bool
    conf_period_frames: int | None
    outputd_period_frames: int
    detail: str = ""


def ring_geometry_matches_outputd(
    outputd_period_frames: int,
    *,
    conf_d: str | None = None,
) -> RingGeometryMatch:
    """Check the conf.d ring slot period equals outputd's resolved period.

    The ring slot IS one outputd DAC period (ping-pong: CamillaDSP writes one
    slot per period, outputd reads one slot per period). If the installed conf.d
    period differs from the period outputd will resolve, CamillaDSP's ring
    ``open()`` fails against outputd's existing ring — so arming must be refused
    with a crisp reason instead of a confusing rollback. A missing/torn conf.d
    period is a mismatch (fail-closed), not a pass.
    """
    conf_path = RING_CONF_D if conf_d is None else conf_d
    conf_period = ring_conf_period_frames(conf_path)
    if conf_period is None:
        return RingGeometryMatch(
            ok=False,
            conf_period_frames=None,
            outputd_period_frames=outputd_period_frames,
            detail=(
                f"ring conf.d ({conf_path}) has no single period_frames — the ring "
                "slot geometry is indeterminate; redeploy to reinstall it"
            ),
        )
    if conf_period != outputd_period_frames:
        return RingGeometryMatch(
            ok=False,
            conf_period_frames=conf_period,
            outputd_period_frames=outputd_period_frames,
            detail=(
                f"ring conf.d period_frames={conf_period} != outputd resolved "
                f"JASPER_OUTPUTD_PERIOD_FRAMES={outputd_period_frames}; the ring "
                "slot is one outputd DAC period, so CamillaDSP's ring open would "
                f"fail against outputd's ring. The ring slot is FIXED at "
                f"{RING_SLOT_FRAMES} by fan-in's compile-time RING_SLOT_FRAMES, so "
                "do NOT raise the conf.d period to match outputd — that is a "
                "geometry fan-in never builds and the attach fails hard. Align on "
                f"{RING_SLOT_FRAMES}: if the conf.d has drifted off it, run sudo "
                "systemctl start jasper-audio-hardware-reconcile.service (it "
                "converges the conf.d back); if the OUTPUTD period is off it, this "
                f"DAC declares no {RING_SLOT_FRAMES}-frame latency floor and "
                "shm_ring is unavailable to it — stay on loopback until issue "
                "#2147 makes the slot derivable"
            ),
        )
    return RingGeometryMatch(
        ok=True,
        conf_period_frames=conf_period,
        outputd_period_frames=outputd_period_frames,
    )


# --- Ring A slot-count coherence (defect A) --------------------------------
#
# The ring's ``n_slots`` is a SECOND geometry axis independent of period_frames.
# fan-in creates Ring A with ``resolve_ring_slots(JASPER_FANIN_RING_SLOTS)`` slots
# (default 2); the ``jts_ring_capture`` ioplug conf.d block pins ``n_slots`` (2 in
# the shipped file); the on-disk ring header records the ``n_slots`` the writer
# actually created. A mismatch on ANY of the three axes is a hard failure:
#   - fan-in env vs conf.d: fan-in creates an old 8-slot ring but CamillaDSP's
#     ioplug attaches expecting 2 → hw_params EINVAL + ioplug attach_fatal
#     ("ring header does not match expected geometry") → CamillaDSP crash-loop →
#     start-limit-hit.
#     (The 2026-07-06 default migration class: old 8-slot ring state must converge
#     to the new 2-slot production default.)
#   - on-disk vs expected: a stale ring file left over from a prior geometry (e.g.
#     an old 8-slot file from before this 2-slot default) is a create-or-ATTACH
#     open() error for the writer, because
#     ``jasper_ring::RingWriter::create_or_attach`` validates the existing header's
#     geometry against the requested one.
#
# ``_RING_CONF_PCM_N_SLOTS_RE`` extracts the ``n_slots`` line WITHIN a named PCM
# block. The conf.d has TWO blocks (Ring A and Ring B); they both pin 2 slots
# today, but this parser still scopes to the requested block so a future coherent
# override on one ring cannot be hidden by a whole-file scan. It is intentionally
# forgiving of the ALSA conf brace style the
# shipped file uses (``pcm.NAME {`` … ``n_slots N`` … ``}``).
_RING_CONF_PCM_BLOCK_RE_TEMPLATE = (
    r"pcm\.{name}\s*\{{(?P<body>[^}}]*)\}}"
)
_RING_CONF_N_SLOTS_RE = re.compile(r"^\s*n_slots\s+(\d+)\s*$", re.MULTILINE)


def ring_conf_n_slots(pcm_name: str, conf_d: str | None = None) -> int | None:
    """Parse the ``n_slots`` pinned for a named PCM block in the ring conf.d.

    ``pcm_name`` is ``jts_ring_capture`` (Ring A) or ``jts_ring_playback`` (Ring
    B). Returns the single ``n_slots`` value that block declares, or ``None`` when
    the file is absent/unreadable, the block is missing, or the block declares no
    single ``n_slots`` (a torn conf.d — the caller treats that as a mismatch, not
    a silent pick). Pure text parse, no ALSA. ``conf_d=None`` resolves
    :data:`RING_CONF_D` at CALL time (not a bound default) so a test/caller that
    repoints the module constant is honored.
    """
    path = RING_CONF_D if conf_d is None else conf_d
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    block_re = re.compile(
        _RING_CONF_PCM_BLOCK_RE_TEMPLATE.format(name=re.escape(pcm_name)),
        re.DOTALL,
    )
    m = block_re.search(text)
    if m is None:
        return None
    values = {int(v.group(1)) for v in _RING_CONF_N_SLOTS_RE.finditer(m.group("body"))}
    if len(values) != 1:
        return None
    return next(iter(values))


# The ring SHM header layout — a small subset of rust/jasper-ring/src/layout.rs,
# duplicated here (Python has no way to link the Rust const). ONLY the fields the
# stale-file geometry guard needs are read; the golden layout test in the Rust
# crate is the offset SSOT, and ``test_ring_assets`` pins these against it.
_RING_MAGIC = 0x4A52_494E  # "JRIN" little-endian (layout.rs MAGIC)
_RING_HEADER_BYTES = 128  # layout.rs HEADER_BYTES
_RING_OFF_MAGIC = 0  # u32
_RING_OFF_VERSION = 4  # u32
_RING_OFF_PERIOD_FRAMES = 20  # u32
_RING_OFF_N_SLOTS = 24  # u32


@dataclass(frozen=True)
class RingHeader:
    """The geometry fields read from an on-disk ring SHM file header.

    ``valid`` is False when the file is absent, too small for a header, or does
    not carry the ``JRIN`` magic (a torn / partially-initialized / foreign file).
    A ``valid=False`` header is NOT trusted for a geometry comparison — the caller
    treats an unreadable/invalid on-disk ring as "no coherent ring present".
    """

    valid: bool
    magic: int = 0
    version: int = 0
    period_frames: int = 0
    n_slots: int = 0


def read_ring_header(path: str) -> RingHeader:
    """Read the geometry fields from a ring SHM file header (little-endian u32s).

    Pure filesystem read of the first :data:`_RING_HEADER_BYTES` bytes — no mmap,
    no ALSA, no writer disturbance (read-only open). Returns ``RingHeader(valid=
    False)`` for an absent/short/magic-less file. The magic gate matters: the Rust
    writer publishes ``JRIN`` LAST (a Release store), so a header without it is
    not yet a coherent ring and must not drive a delete/mismatch decision on its
    (zero) geometry fields.
    """
    import struct

    try:
        with open(path, "rb") as fh:
            head = fh.read(_RING_HEADER_BYTES)
    except OSError:
        return RingHeader(valid=False)
    if len(head) < _RING_HEADER_BYTES:
        return RingHeader(valid=False)
    magic = struct.unpack_from("<I", head, _RING_OFF_MAGIC)[0]
    if magic != _RING_MAGIC:
        return RingHeader(valid=False)
    return RingHeader(
        valid=True,
        magic=magic,
        version=struct.unpack_from("<I", head, _RING_OFF_VERSION)[0],
        period_frames=struct.unpack_from("<I", head, _RING_OFF_PERIOD_FRAMES)[0],
        n_slots=struct.unpack_from("<I", head, _RING_OFF_N_SLOTS)[0],
    )


@dataclass(frozen=True)
class RingSlotGeometryMatch:
    """Whether fan-in's resolved Ring-A n_slots matches the conf.d ``n_slots``."""

    ok: bool
    fanin_n_slots: int
    conf_n_slots: int | None
    detail: str = ""


def ring_slot_geometry_matches_conf(
    fanin_n_slots: int,
    *,
    conf_d: str | None = None,
    pcm_name: str = RING_A_CONF_PCM,
) -> RingSlotGeometryMatch:
    """Check fan-in's resolved Ring-A n_slots equals the conf.d block ``n_slots``.

    fan-in creates Ring A with ``fanin_n_slots`` slots; the ``jts_ring_capture``
    ioplug attaches expecting the conf.d ``n_slots``. If they differ, CamillaDSP's
    ring attach fails with a hard geometry error (hw_params EINVAL + ioplug
    ``attach_fatal reason=ring header does not match expected geometry``) and the
    daemon crash-loops — so arming must be refused with a crisp reason. A
    missing/torn conf.d ``n_slots`` is a mismatch (fail-closed), not a pass.
    """
    conf_n_slots = ring_conf_n_slots(pcm_name, conf_d)
    if conf_n_slots is None:
        conf_path = RING_CONF_D if conf_d is None else conf_d
        return RingSlotGeometryMatch(
            ok=False,
            fanin_n_slots=fanin_n_slots,
            conf_n_slots=None,
            detail=(
                f"ring conf.d ({conf_path}) has no single n_slots for pcm."
                f"{pcm_name} — the Ring A slot geometry is indeterminate; "
                "redeploy to reinstall it"
            ),
        )
    if conf_n_slots != fanin_n_slots:
        return RingSlotGeometryMatch(
            ok=False,
            fanin_n_slots=fanin_n_slots,
            conf_n_slots=conf_n_slots,
            detail=(
                f"fan-in Ring A n_slots={fanin_n_slots} (resolved from "
                f"JASPER_FANIN_RING_SLOTS) != conf.d pcm.{pcm_name} "
                f"n_slots={conf_n_slots}; fan-in would create a {fanin_n_slots}-slot "
                f"program.ring while CamillaDSP's ioplug attaches expecting "
                f"{conf_n_slots}, a hard hw_params/attach geometry error that "
                "crash-loops CamillaDSP. Match them (clear the stale "
                f"JASPER_FANIN_RING_SLOTS to the default, or set the conf.d block "
                "to match) before arming"
            ),
        )
    return RingSlotGeometryMatch(
        ok=True,
        fanin_n_slots=fanin_n_slots,
        conf_n_slots=conf_n_slots,
    )
