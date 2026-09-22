# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring assets, provenance, and header/config agreement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jasper import ring_conf, ring_header
from jasper.audio_hardware.dac import latency_floor_for
from jasper.fanin_coupling import (
    RING_SLOT_FRAMES,
    RingWire,
    resolve_ring_wire,
)
from jasper.json_fields import sha256_file

# lazy: import cost — keep ring asset readers import-cheap.
if TYPE_CHECKING:
    from jasper.output_topology import OutputTopology

# The aarch64 ALSA plugin dir the ioplug ``.so`` installs into. Canonical home
# for the value — do not re-spell it as a literal elsewhere. Build and install
# path: ``deploy/lib/install/ring-platform.sh``.
RING_ALSA_PLUGIN_DIR = "/usr/lib/aarch64-linux-gnu/alsa-lib"
RING_IOPLUG_SO = "libasound_module_pcm_jts_ring.so"
RING_CONF_D = "/etc/alsa/conf.d/60-jts-ring.conf"
# The tmpfs directory the ring files live in (``deploy/tmpfiles/jts-ring.conf``,
# mode 3775 root:jts-ring — sticky + setgid + group-write). Mirrored by value in
# ``rust/jasper-fanin/src/config.rs`` (``RING_SHM_DIR``).
RING_SHM_DIR = "/dev/shm/jts-ring"
# Ring A (fan-in -> CamillaDSP program) and Ring B (CamillaDSP -> outputd content)
# on-disk ring files under RING_SHM_DIR. Basenames match the conf.d ``path``
# values (``jts_ring_capture`` -> program.ring, ``jts_ring_playback`` ->
# content.ring) and the Rust defaults. Ring A is the one whose slot geometry the
# fan-in ``JASPER_FANIN_RING_SLOTS`` env and the conf.d ``jts_ring_capture``
# ``n_slots`` must agree on.
RING_A_PROGRAM_FILE = os.path.join(RING_SHM_DIR, "program.ring")
RING_B_CONTENT_FILE = os.path.join(RING_SHM_DIR, "content.ring")
# The ACTIVE ring's on-disk file — the roleful box's post-crossover per-driver
# hop. A THIRD file beside the two above, never a re-use of Ring B's: the two
# rings coexist on an armed roleful box and carry different programs at
# different widths.
RING_ACTIVE_CONTENT_FILE = os.path.join(RING_SHM_DIR, "active-content.ring")
# The adjacent lock file whose EXCLUSIVE ``flock`` a C ioplug WRITER holds for
# the life of its mapping — ``JTS_RING_WRITER_LOCK_SUFFIX`` in
# ``c/jts-ring-ioplug/jts_ring_shm.h``, pinned against the generated ring ABI
# (``rust/jasper-ring/layout.json``) at both ends so they cannot drift.
# Python is a reader of this lock (the grouping reconciler's active-content
# release barrier, and the doctor's writer-exclusivity guard).
#
# DISTINCT from ``.open.lock`` (``JTS_RING_OPEN_LOCK_SUFFIX``), which is a
# TRANSACTION lock released as soon as create-or-attach completes. Only the
# writer lock answers "does a live writer own this ring": the Rust
# ``RingWriter`` and ``RingReader`` take the ``.open.lock`` and never this one
# (``rust/jasper-ring/src/lib.rs`` ``OpenTransactionLock``), so an fd on a
# ``.writer.lock`` is a C writer and nothing else.
RING_WRITER_LOCK_SUFFIX = ".writer.lock"


def ring_writer_lock_path(ring_path: str) -> str:
    """The writer-lock file that guards ``ring_path``.

    Mirrors ``acquire_writer_lock``'s own construction in
    ``c/jts-ring-ioplug/jts_ring_shm.c`` — the ring path with
    :data:`RING_WRITER_LOCK_SUFFIX` appended, no directory indirection — so a
    Python prober contends on exactly the inode the ioplug's writer holds.
    """
    return f"{ring_path}{RING_WRITER_LOCK_SUFFIX}"


def ring_ioplug_so_path(*, plugin_dir: str | None = None) -> str:
    """Absolute path of the installed ioplug ``.so``.

    ``plugin_dir=None`` resolves :data:`RING_ALSA_PLUGIN_DIR` at CALL time, not
    as a bound default, so a caller that repoints the module constant is honored
    instead of silently reading the original path. Every ``None`` default in
    this module follows that rule.
    """
    return os.path.join(
        RING_ALSA_PLUGIN_DIR if plugin_dir is None else plugin_dir, RING_IOPLUG_SO
    )


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
    plugin_dir: str | None = None,
    conf_d: str | None = None,
    shm_dir: str | None = None,
) -> RingAssetPresence:
    """Snapshot which of the three ring-platform assets are present on disk.

    Pure filesystem stat — no ALSA open, no subprocess, leaves no residue.
    """
    return RingAssetPresence(
        so_present=os.path.exists(ring_ioplug_so_path(plugin_dir=plugin_dir)),
        conf_present=os.path.exists(RING_CONF_D if conf_d is None else conf_d),
        shm_dir_present=os.path.isdir(RING_SHM_DIR if shm_dir is None else shm_dir),
    )


# ---------------------------------------------------------------------------
# ioplug PROVENANCE — what the .so that is INSTALLED can actually parse.
#
# Presence is not capability. The ioplug build is deliberately DEGRADE-TO-WARN
# (``deploy/lib/install/ring-platform.sh``): when the compile fails the install
# continues and the PREVIOUS ``.so`` stays in place beside freshly-installed Rust
# daemons. Presence-only checks — and the doctor's open-probe, which a stale but
# structurally-valid ioplug passes — cannot see that. The failure this record
# closes is specific: a conf.d rendered with a ``format`` / ``channels`` key the
# old ``.so`` does not know is refused at ``open()`` with ``-EINVAL``
# ("jts_ring: unknown field %s"), so CamillaDSP cannot start against the ring.
#
# So the installer records what it installed and the reconciler COMPARES
# records; it never opens a PCM to find out, because an open-probe against a
# live ring hits the ioplug's SPSC guard and probing from the arm path is the
# disturbance the doctor's armed-skip exists to avoid.
RING_IOPLUG_PROVENANCE = "/var/lib/jasper/ring-ioplug.provenance"

# The capability VOCABULARY: one token per conf.d field the ioplug must parse
# for a wire that declares it to be openable. Not version numbers — the record
# names what is supported rather than when it was built.
RING_CAP_WIRE_FORMAT = "wire_format"
RING_CAP_WIRE_CHANNELS = "wire_channels"
#: ``pace_nominal`` — the grouping ring's playback rate limiter. In the vocabulary
#: for the same reason as the two above: a conf.d declaring the field against an
#: older ``.so`` is refused at ``open()`` with ``-EINVAL``, so the record has to be
#: able to name it. No ``RingWire`` implies it (the grouping ring is its own
#: conf.d, not part of the ring_a/ring_b/ring_active wire), so
#: :func:`ring_wire_capabilities` never asks for it — the record simply carries it.
RING_CAP_PACE_NOMINAL = "pace_nominal"
RING_IOPLUG_CAPS = (RING_CAP_WIRE_FORMAT, RING_CAP_WIRE_CHANNELS, RING_CAP_PACE_NOMINAL)

# The provenance file's keys (a plain ``KEY=value`` text file, mode 0644, written
# by ``record_ring_ioplug_provenance`` in ring-platform.sh).
RING_PROVENANCE_SHA_KEY = "JTS_RING_IOPLUG_SHA256"
RING_PROVENANCE_CAPS_KEY = "JTS_RING_IOPLUG_CAPS"


@dataclass(frozen=True)
class RingIoplugProvenance:
    """What the installer recorded about the ioplug ``.so`` it installed.

    ``recorded`` is False when the file is absent or carries no usable sha. That
    is not an error condition by itself: a wire needing no capability beyond the
    ioplug's own defaults never consults this record at all (see
    :func:`ring_ioplug_wire_supported`).
    """

    recorded: bool
    sha256: str = ""
    caps: frozenset[str] = frozenset()


def read_ring_ioplug_provenance(
    path: str | None = None,
) -> RingIoplugProvenance:
    """Read the installer's ioplug provenance record. Never raises.

    Unparseable / absent / sha-less content answers ``recorded=False`` rather
    than a partial record: a record that cannot name WHICH ``.so`` it describes
    cannot vouch for the one on disk, so there is nothing to trust.
    """
    path = RING_IOPLUG_PROVENANCE if path is None else path
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError: a truncated or
        # non-text file at this path must answer "no record", not explode inside
        # an arm preflight.
        return RingIoplugProvenance(recorded=False)
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"')
    sha = values.get(RING_PROVENANCE_SHA_KEY, "")
    if not sha:
        return RingIoplugProvenance(recorded=False)
    caps = frozenset(
        stripped
        for token in values.get(RING_PROVENANCE_CAPS_KEY, "").split(",")
        if (stripped := token.strip())
    )
    return RingIoplugProvenance(recorded=True, sha256=sha, caps=caps)


def ring_ioplug_so_sha256(*, plugin_dir: str | None = None) -> str | None:
    """SHA-256 of the installed ioplug ``.so``, or ``None`` if unreadable."""
    try:
        return sha256_file(ring_ioplug_so_path(plugin_dir=plugin_dir))
    except OSError:
        return None


def ring_wire_capabilities(wire: RingWire) -> frozenset[str]:
    """The ioplug capabilities this wire NEEDS, beyond the ioplug's own defaults.

    The conf.d renderer writes a ``format`` / ``channels`` key only where the
    resolved wire differs from :data:`ring_conf.RING_CONF_DEFAULT_FORMAT` /
    :data:`ring_conf.RING_CONF_DEFAULT_CHANNELS` (see :func:`ring_conf.render_ring_conf_wire`), and
    an omitted key is what an older ioplug expects. So the capability a wire
    needs is the set of keys it forces onto the conf.d — non-empty on every box
    that has not pinned itself narrow, because the wire resolver defaults WIDE
    (``jasper.fanin_coupling.resolve_ring_wire_format``) while
    :data:`ring_conf.RING_CONF_DEFAULT_FORMAT` stays the C ioplug's own ``S16_LE``.

    THREE AXES, one per conf.d key the renderer can write:

    * ``format`` — every block shares one token, so one comparison covers all
      three;
    * ``channels`` on Ring A / Ring B — the full-range stereo pair;
    * ``channels`` on the ACTIVE block — the post-crossover per-driver width, a
      SEPARATE axis because :func:`ring_conf.render_ring_conf_wire` writes that block from
      ``ring_active_channels`` while a roleful box's Ring A/B stay structurally
      2, so a roleful box driving 4+ channels forces the key through this block
      alone. The coercion mirrors the renderer's own
      (``ring_active_channels or ring_conf.RING_CONF_DEFAULT_CHANNELS``) so "which boxes
      force the key" has one answer, not two.

    WHAT THIS DOES NOT WEIGH: the axes above answer "which keys does this WIRE
    force onto the conf.d", not "which keys does the conf.d on disk DECLARE".
    Since the shipped conf.d spells ``format`` explicitly, a box an operator has
    pinned narrow resolves an empty format axis while its rendered conf.d still
    carries a ``format`` line — an older ioplug would refuse it at ``open()``
    with this predicate reporting nothing needed. It needs an operator pin AND
    an unvouched plugin to bite, and closing it means keying the predicate on
    the FILE rather than the wire — a contract change to a safety-adjacent gate,
    so it is issue #2597 rather than a silent widening here.
    """
    needed: set[str] = set()
    if wire.sample_format != ring_conf.RING_CONF_DEFAULT_FORMAT:
        needed.add(RING_CAP_WIRE_FORMAT)
    if (
        wire.ring_a_channels != ring_conf.RING_CONF_DEFAULT_CHANNELS
        or wire.ring_b_channels != ring_conf.RING_CONF_DEFAULT_CHANNELS
        or (wire.ring_active_channels or ring_conf.RING_CONF_DEFAULT_CHANNELS)
        != ring_conf.RING_CONF_DEFAULT_CHANNELS
    ):
        needed.add(RING_CAP_WIRE_CHANNELS)
    return frozenset(needed)


@dataclass(frozen=True)
class RingIoplugWireSupport:
    """Whether the INSTALLED ioplug can open a conf.d declaring a given wire."""

    ok: bool
    needed: frozenset[str]
    detail: str = ""


def ring_ioplug_wire_supported(
    wire: RingWire,
    *,
    plugin_dir: str | None = None,
    provenance_path: str | None = None,
) -> RingIoplugWireSupport:
    """Can the installed ioplug ``.so`` parse the conf.d this wire renders?

    A RECORD COMPARE, never a probe: hash the installed ``.so`` and check the
    installer's record both describes THAT file and claims the capabilities the
    wire needs. Three fail-closed shapes, each with its own remediation:

    - **no record** — nothing describes the installed ``.so``; redeploy;
    - **stale record** — the recorded sha is not the installed file's, so the
      ``.so`` was replaced (or survived a failed rebuild) after the record was
      written and the record vouches for a different binary;
    - **missing capability** — the record describes this ``.so`` and says it
      cannot parse a field the wire needs.

    Short-circuits to ``ok`` when the wire needs nothing
    (:func:`ring_wire_capabilities` is empty) — no file is read and no hash is
    computed on that path. That arm is reached only by a box an operator has
    pinned narrow; on every other box this is a live record compare.
    """
    provenance_path = (
        RING_IOPLUG_PROVENANCE if provenance_path is None else provenance_path
    )
    needed = ring_wire_capabilities(wire)
    if not needed:
        return RingIoplugWireSupport(
            ok=True,
            needed=needed,
            detail=(
                f"wire {wire.sample_format}/{wire.ring_a_channels}ch:"
                f"{wire.ring_b_channels}ch forces no conf.d field beyond the "
                "ioplug's own defaults, so this predicate has nothing to weigh "
                "(it answers for the WIRE, not for the conf.d on disk, which "
                "since the wide-wire flip spells `format` on every box — #2597)"
            ),
        )
    wanted = ", ".join(sorted(needed))
    record = read_ring_ioplug_provenance(provenance_path)
    if not record.recorded:
        return RingIoplugWireSupport(
            ok=False,
            needed=needed,
            detail=(
                f"wire {wire.sample_format}/{wire.ring_b_channels}ch needs ioplug "
                f"capability [{wanted}], but no provenance record describes the "
                f"installed {ring_ioplug_so_path(plugin_dir=plugin_dir)} "
                f"({provenance_path} absent or unusable). Redeploy so the "
                "installer records what it built; a conf.d carrying a field the "
                "installed ioplug cannot parse is refused at open() with -EINVAL "
                "and CamillaDSP cannot start against the ring"
            ),
        )
    installed = ring_ioplug_so_sha256(plugin_dir=plugin_dir)
    if installed is None:
        return RingIoplugWireSupport(
            ok=False,
            needed=needed,
            detail=(
                f"wire {wire.sample_format}/{wire.ring_b_channels}ch needs ioplug "
                f"capability [{wanted}], but "
                f"{ring_ioplug_so_path(plugin_dir=plugin_dir)} could not be read "
                "to confirm the provenance record describes it"
            ),
        )
    if installed != record.sha256:
        return RingIoplugWireSupport(
            ok=False,
            needed=needed,
            detail=(
                f"STALE ioplug: {ring_ioplug_so_path(plugin_dir=plugin_dir)} "
                f"hashes {installed[:12]}… but the provenance record describes "
                f"{record.sha256[:12]}…, so the installed plugin is NOT the one "
                f"the installer recorded (the ioplug build degrades to a WARN and "
                f"leaves the previous .so in place). The wire needs [{wanted}]; "
                "redeploy and check the transcript for a jts_ring ioplug build "
                "failure"
            ),
        )
    missing = needed - record.caps
    if missing:
        return RingIoplugWireSupport(
            ok=False,
            needed=needed,
            detail=(
                f"the installed ioplug cannot parse [{', '.join(sorted(missing))}]: "
                f"wire {wire.sample_format}/{wire.ring_b_channels}ch renders a "
                "conf.d field this plugin refuses at open() with -EINVAL. "
                f"Recorded capabilities: [{', '.join(sorted(record.caps)) or 'none'}]. "
                "Redeploy to rebuild the ioplug from current source"
            ),
        )
    return RingIoplugWireSupport(
        ok=True,
        needed=needed,
        detail=(
            f"the installed ioplug records capability [{wanted}] for sha "
            f"{record.sha256[:12]}…, which matches the plugin on disk"
        ),
    )


def _load_topology_for_ring_wire(path: str | None) -> tuple[OutputTopology | None, str]:
    """Unreadable topology keeps the shipped stereo wire; arm preflights reject it."""
    from jasper.output_topology import (  # lazy: import cost, keep ring asset readers import-cheap
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        return load_output_topology_strict(path), "loaded"
    except (OutputTopologyError, OSError, ValueError):
        return None, "topology_unreadable"


def ring_conf_wire_report(
    *,
    profile_id: str,
    conf_d: str = "",
    output_topology: str | None = None,
    topology: OutputTopology | None = None,
) -> dict[str, str]:
    """Render only declared floors matching Ring A's fixed slot (issue #2147)."""
    resolved_conf_d = conf_d or RING_CONF_D
    floor = latency_floor_for(profile_id) if profile_id else None
    if floor is None:
        return {
            "result": "skipped",
            "reason": "no_declared_floor",
            "conf": str(resolved_conf_d),
        }
    if floor.outputd_period_frames != RING_SLOT_FRAMES:
        return {
            "result": "skipped",
            "reason": f"ring_slot_fixed_{RING_SLOT_FRAMES}",
            "period_frames": str(floor.outputd_period_frames),
            "conf": str(resolved_conf_d),
        }
    topology_reason = "loaded"
    if topology is None:
        topology, topology_reason = _load_topology_for_ring_wire(output_topology)
    outcome = ring_conf.render_ring_conf_wire(resolve_ring_wire(topology), conf_d=resolved_conf_d)
    report = {
        "result": "rendered" if outcome.changed else "unchanged",
        "period_frames": str(outcome.period_frames),
    }
    if outcome.previous_period_frames is not None:
        report["previous_period_frames"] = str(outcome.previous_period_frames)
    report["sample_format"] = str(outcome.sample_format)
    report["ring_a_channels"] = str(outcome.ring_a_channels)
    report["ring_b_channels"] = str(outcome.ring_b_channels)
    report["ring_active_channels"] = str(outcome.ring_active_channels)
    report["topology"] = topology_reason
    report["conf"] = str(outcome.conf_d)
    return report


@dataclass(frozen=True)
class RingHeaderCoherence:
    """Whether an ON-DISK ring file's header matches what the ioplug will attach.

    ``present`` is False when there is no coherent ring file to judge (absent,
    magic-less, or a layout version this parser does not describe) — NOT a
    mismatch: the writer reclaims such a file itself.

    ``ok`` is only meaningful when ``present``. ``axis`` names the FIRST axis
    that disagreed, so a caller can log which one without re-deriving it.
    """

    present: bool
    ok: bool = True
    axis: str = ""
    detail: str = ""


def ring_header_matches_conf(
    path: str,
    pcm_name: str,
    *,
    conf_d: str | None = None,
    expected_n_slots: int | None = None,
) -> RingHeaderCoherence:
    """Compare an on-disk ring header against its conf.d block, ALL FOUR axes.

    All four, because the Rust and C attach paths compare every one of
    ``n_slots``/``period_frames``/``sample_format``/``channels``
    field-by-field. A guard reading only slots and period would call a file
    coherent that the ioplug then refuses at arm.

    ONE COMPARATOR, three callers (the stale-file delete, the CONFIRM-path
    self-heal predicate, and the doctor's coherence check) so "coherent" cannot
    mean three things. Axes are checked in the order a reader thinks about them:
    depth, then timing, then the wire.

    ``expected_n_slots`` overrides the conf.d's own value for the caller that
    has a better answer (the stale-file guard falls back to fan-in's resolved
    env when the conf.d is unreadable). An axis whose EXPECTED value is
    indeterminate is SKIPPED rather than guessed — the conf.d parsers already
    fold an omitted ``format``/``channels`` into the ioplug's documented
    default, so indeterminate here means the file or block could not be read at
    all, which is not evidence of a shear.
    """
    header = ring_header.read_ring_header(path)
    if not header.valid:
        return RingHeaderCoherence(present=False)

    conf_d = RING_CONF_D if conf_d is None else conf_d
    expected_slots = (
        expected_n_slots
        if expected_n_slots is not None
        else ring_conf.ring_conf_n_slots(pcm_name, conf_d)
    )
    expected_period = ring_conf.ring_conf_period_frames(conf_d)
    expected_format = ring_conf.ring_conf_format(pcm_name, conf_d)
    expected_channels = ring_conf.ring_conf_channels(pcm_name, conf_d)

    for axis, on_disk, expected in (
        ("n_slots", header.n_slots, expected_slots),
        ("period_frames", header.period_frames, expected_period),
        ("sample_format", header.sample_format_name, expected_format),
        ("channels", header.channels, expected_channels),
    ):
        if expected is None:
            continue
        if on_disk != expected:
            return RingHeaderCoherence(
                present=True,
                ok=False,
                axis=axis,
                detail=(
                    f"on-disk ring {path} has {axis}={on_disk} != expected "
                    f"{expected} (pcm.{pcm_name})"
                ),
            )
    return RingHeaderCoherence(
        present=True,
        ok=True,
        detail=(
            f"on-disk ring {path} matches pcm.{pcm_name} on n_slots, "
            "period_frames, sample_format and channels"
        ),
    )
