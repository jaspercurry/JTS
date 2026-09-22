# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring assets, provenance, live health, and header/config agreement."""

from __future__ import annotations

import os
import time
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
    )
    from jasper.output_topology_store import (  # lazy: keep ring asset readers import-cheap
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
class RingStallVerdict:
    """Is this ring being WRITTEN but not READ? The independent-observer alarm.

    A ring-local frozen dataclass rather than the ``severity``/``code`` dict
    shape ``jasper.output_topology`` uses: those warnings ride
    ``evaluate_output_topology``'s ``warnings`` list, which
    ``OutputTopology.to_dict`` embeds and three persisted fingerprints hash
    (issue #2500). A stall is RUNTIME state that changes second to second, so
    putting it there would make a topology's fingerprint vary with whether a
    daemon happened to be wedged when it was read.

    ``present`` is False when there is no coherent ring file to judge, or when
    the ring exists but nothing has stamped a writer heartbeat yet (an armed but
    idle ring). ``stalled`` is only meaningful when ``present``.

    WHAT IT DETECTS. The C ioplug demotes a reader whose heartbeat has gone
    stale and then FREE-RUNS, dropping the oldest slot per publish so the ring
    stays bounded. Audio is being lost and nobody reports it: the ioplug's
    ``published_slots`` / ``drop_no_reader`` / ``full_waits`` are process-local
    ``jts_ring_writer_t`` fields printed at close, not shared-header fields, and
    outputd — the reader — is exactly the process that is wedged, so its own
    STATUS is the least trustworthy witness. Hence a THIRD observer blocked in
    neither end.

    WHY THE READER'S HEARTBEAT AND NOT ``read_seq``. At demotion the writer
    advances ``read_seq`` on the absent reader's behalf, deliberately, so
    ``occupancy = write_seq - read_seq`` stays honest and ALSA's ``avail`` does
    not stick at 0 (``jts_ring_shm.c``, the ``atomic_store_explicit(&h->read_seq,
    rseq + 1, …)`` guarded by ``if (!reader_is_live(…))``; the Rust writer's
    ``free_run_drop_oldest`` does the same). A "``read_seq`` is flat" clause
    therefore holds only inside the pre-demotion grace and goes false exactly
    when the drops begin — an alarm that switches itself off at the onset of the
    fault it exists to catch. ``reader_heartbeat_ns`` has no such inversion:
    only a reader running its loop stamps it, it stays stale through and after
    demotion, and it is the same fact ``reader_is_live`` uses to demote. Attach
    resync (``read_seq = write_seq``) is a third way the sequence numbers lie;
    the heartbeat is unaffected by that too.

    RESIDUAL: there is no live DROP COUNT for this ring. This verdict says "the
    reader is gone while the writer runs", the condition under which drops
    occur, not a count of them.
    """

    present: bool
    stalled: bool = False
    writer_age_ns: int | None = None
    reader_age_ns: int | None = None
    detail: str = ""


def _heartbeat_age_ns(stamp: int, now_ns: int) -> int | None:
    """Saturating age of one heartbeat stamp, or None when never stamped.

    Saturating because a heartbeat stamped AFTER the observer sampled ``now_ns``
    would make the subtraction underflow and read as enormously stale — an alarm
    on a live ring. Shared by both judges below, so the arithmetic is one rule
    even where the predicates around it differ.
    """
    if stamp == 0:
        return None
    return now_ns - stamp if now_ns > stamp else 0


def _end_is_live(pid: int, heartbeat_ns: int, now_ns: int, timeout_ns: int) -> bool:
    """Is one end of a ring live, by the C ioplug's OWN predicate?

    ``reader_is_live`` / ``writer_is_live`` (``c/jts-ring-ioplug/jts_ring_shm.c``)
    both require **all three**: a non-zero pid, a non-zero heartbeat, and an age
    inside the liveness window. The pid clause must not be dropped:
    ``jts_ring_reader_close`` clears ``reader_pid`` but leaves the last
    heartbeat standing, so a cleanly-closed end stays heartbeat-fresh for a full
    window after the writer has already begun free-running and dropping.
    Judging on the heartbeat alone reports a ring as healthy for those two
    seconds while audio is being lost.
    """
    if pid == 0 or heartbeat_ns == 0:
        return False
    age = _heartbeat_age_ns(heartbeat_ns, now_ns)
    return age is not None and age < timeout_ns


def ring_stall_verdict(
    path: str,
    *,
    now_ns: int | None = None,
    timeout_ns: int = ring_header.RING_LIVENESS_TIMEOUT_NS,
) -> RingStallVerdict:
    """Judge one ring file: writer heartbeat FRESH while reader heartbeat STALE.

    Single-sample, no sleep. Both heartbeats and ``time.monotonic_ns()`` are
    CLOCK_MONOTONIC on the same box (``jts_ring_monotonic_ns`` in C,
    ``monotonic_ns`` in ``jasper-ring``, ``clock_gettime(CLOCK_MONOTONIC)`` in
    CPython), so "advancing over a window" and "fresh right now" are the same
    predicate — and freshness needs one read where advancement would need two
    plus a window the caller could get wrong. Ages are saturating
    (:func:`_heartbeat_age_ns`): a future heartbeat clamps to age 0.

    ``present=False`` (never an alarm) for: an absent / torn / foreign / wrong
    version file; and a ring whose WRITER heartbeat is 0 or itself stale. That
    last one is load-bearing: a ring nobody is writing is idle, not stalled, and
    alarming on it would fire on every unarmed box. The alarm is specifically
    "audio is flowing IN and not OUT".
    """
    header = ring_header.read_ring_header(path)
    if not header.valid:
        return RingStallVerdict(present=False, detail="no coherent ring header")
    if now_ns is None:
        now_ns = time.monotonic_ns()

    def _age(stamp: int) -> int | None:
        return _heartbeat_age_ns(stamp, now_ns)

    # NOTE (#2786): this judge is HEARTBEAT-ONLY on purpose. The C's own
    # `reader_is_live` also requires a non-zero pid, so a cleanly-closed reader
    # is dead to the writer instantly while its last heartbeat stays fresh for
    # one liveness window — during which this verdict still reads "both ends
    # live". :func:`ring_flow_state` applies the full pid-and-heartbeat
    # predicate; this alarm judges four rings and the divergence costs it at
    # most one window of late alarming on a ring whose reader left cleanly. If
    # that ever matters, change it here for all four rings at once rather than
    # letting the two drift; where they disagree is pinned by
    # `test_the_two_judges_diverge_only_on_a_cleanly_closed_end`.
    writer_age = _age(header.writer_heartbeat_ns)
    reader_age = _age(header.reader_heartbeat_ns)
    if writer_age is None:
        return RingStallVerdict(
            present=False, detail="ring has never been written (no writer heartbeat)"
        )
    if writer_age >= timeout_ns:
        return RingStallVerdict(
            present=False,
            writer_age_ns=writer_age,
            reader_age_ns=reader_age,
            detail=(
                f"writer heartbeat is itself stale ({writer_age / 1e6:.0f} ms) — "
                "an idle or stopped ring, not a stall"
            ),
        )
    # The writer is live. Now the reader half.
    if reader_age is not None and reader_age < timeout_ns:
        return RingStallVerdict(
            present=True,
            stalled=False,
            writer_age_ns=writer_age,
            reader_age_ns=reader_age,
            detail=(
                f"both ends live (writer {writer_age / 1e6:.0f} ms, reader "
                f"{reader_age / 1e6:.0f} ms behind)"
            ),
        )
    reader_desc = (
        "never stamped a heartbeat"
        if reader_age is None
        else f"{reader_age / 1e6:.0f} ms behind"
    )
    return RingStallVerdict(
        present=True,
        stalled=True,
        writer_age_ns=writer_age,
        reader_age_ns=reader_age,
        detail=(
            f"the writer is live ({writer_age / 1e6:.0f} ms behind) but the "
            f"reader {reader_desc}, past the {timeout_ns / 1e6:.0f} ms liveness "
            "window the ioplug itself uses — it has demoted the reader and is "
            "free-running, dropping the oldest slot per publish. Audio is being "
            "lost. Check the reader daemon (jasper-outputd for the content "
            "rings) and its journal"
        ),
    )


# The states :func:`ring_flow_state` classifies a ring into. They are published
# verbatim as the ``state`` of ``/state``'s grouping ``ring`` block
# (``jasper.multiroom.state``), so the vocabulary is consumed off the box.
# Named constants rather than bare literals: a misspelled literal compares
# false silently.
RING_FLOW_ABSENT = "absent"
RING_FLOW_UNREADABLE = "unreadable"
RING_FLOW_IDLE = "idle"
RING_FLOW_PRIMING = "priming"
RING_FLOW_READER_STALLED = "reader_stalled"
RING_FLOW_FLOWING = "flowing"


@dataclass(frozen=True)
class RingFlowState:
    """What is happening on one ring right now, in one word plus its evidence.

    :func:`ring_stall_verdict` answers one narrow question — is this ring being
    written but not read — for the doctor's four-ring alarm. This is the
    OPERATOR-FACING classification over the same 128 bytes, and it differs from
    that alarm in two deliberate ways.

    **It judges liveness by the writer's own rule.** Both ends are tested with
    :func:`_end_is_live`, i.e. pid AND heartbeat AND window — the same
    conjunction ``reader_is_live`` / ``writer_is_live`` apply in
    ``c/jts-ring-ioplug/jts_ring_shm.c``. The stall alarm tests the heartbeat
    only, which reports a cleanly-closed end as live for one window after the
    writer has already started dropping; an operator surface cannot afford that
    gap.

    **The startup split.** A ring whose writer is live and whose reader has never
    stamped a heartbeat is the SAME instantaneous shape at second one of a cold
    start and at hour three of a wedged reader, because the pacing governor
    (``jts_ring_pace_apply`` in ``c/jts-ring-ioplug/jts_ring_shm.h``) holds the
    stalled case to roughly nominal rather than letting it storm. So the
    classifier separates them by ``write_seq``, the ring's own age: below one
    liveness window's worth of slots the ring is still
    :data:`RING_FLOW_PRIMING`, above it the reader is genuinely late and the
    state is :data:`RING_FLOW_READER_STALLED`. The budget is DERIVED from the
    header's own ``rate``/``period_frames`` and the ioplug's own demotion window,
    so no threshold is invented here. This splits only the NEVER-ATTACHED case:
    a ring that had a reader and lost it is never priming, however young.

    **The drop cursor.** There is no drop COUNT in the shared header (the
    writer's ``drop_no_reader`` is a process-local ``jts_ring_writer_t`` field —
    see :class:`RingStallVerdict`), and this module does not invent one: a
    counter accumulated across polls would depend on who polled and how often.
    What it publishes instead is the pair the count is derived FROM. While no
    reader is live the writer advances ``read_seq`` itself, one slot per dropped
    publish, so two reads of ``read_seq`` while ``state`` is
    :data:`RING_FLOW_READER_STALLED` bound the drops between them exactly, and
    ``reader_age_ns`` says how long that has been true without differencing
    anything.

    ``writer_age_ns`` / ``reader_age_ns`` are None when that end has never
    stamped a heartbeat. The sequence/epoch fields are None whenever the header
    could not be read at all.
    """

    state: str
    detail: str = ""
    writer_age_ns: int | None = None
    reader_age_ns: int | None = None
    write_seq: int | None = None
    read_seq: int | None = None
    occupancy_slots: int | None = None
    writer_epoch: int | None = None


def _priming_slot_budget(header: ring_header.RingHeader, timeout_ns: int) -> int:
    """Slots a nominal writer publishes in one liveness window.

    The startup grace, in the ring's OWN units: ``rate``/``period_frames`` come
    off the header and ``timeout_ns`` is the ioplug's own demotion window, so a
    ring with a different period or a different window gets a budget that tracks
    it. :func:`ring_header.read_ring_header` gates on magic and version but NOT on the
    geometry's range, so a zero in either field is reachable (a torn read, or a
    foreign file carrying the magic). That answers 0 — no grace, because a grace
    nobody can size must not be granted; the cost of the strict direction is one
    startup transient reported as a stall.
    """
    if header.rate <= 0 or header.period_frames <= 0:
        return 0
    return (timeout_ns * header.rate) // (header.period_frames * 1_000_000_000)


def ring_flow_state(
    path: str,
    *,
    now_ns: int | None = None,
    timeout_ns: int = ring_header.RING_LIVENESS_TIMEOUT_NS,
) -> RingFlowState:
    """Classify one ring file into a single operator-facing state.

    ONE read of the first 128 header bytes, read-only, no mmap, no ALSA, no lock
    — so it can be called against a ring carrying live audio without perturbing
    it. Bounded and non-blocking: the ring lives on tmpfs and the read is a fixed
    128 bytes.

    Total: every failure resolves to a state, never an exception.
    :data:`RING_FLOW_ABSENT` when no file is there (a ring file exists only once
    something opens the PCM), and :data:`RING_FLOW_UNREADABLE` when a file IS
    there but this process cannot read it or it carries no coherent v1 ``JRIN``
    header. Those two are kept apart deliberately: collapsing "nothing has opened
    this device" into "I am not allowed to look" would let a permission problem
    read as an idle speaker.
    """
    header = ring_header.read_ring_header(path)
    if not header.valid:
        if not os.path.exists(path):
            return RingFlowState(
                state=RING_FLOW_ABSENT,
                detail="no ring file — nothing has opened this PCM",
            )
        if not os.access(path, os.R_OK):
            # The requirement, not a mode: ring files are group `jts-ring` by the
            # setgid directory (deploy/tmpfiles/jts-ring.conf), but their MODE is
            # the creating unit's umask — 0660 under UMask=0007, 0640 under
            # systemd's default. Both grant the group read, which is all an
            # observer needs, so naming one mode here would be false on half the
            # boxes.
            return RingFlowState(
                state=RING_FLOW_UNREADABLE,
                detail=(
                    f"{path} exists but is not readable by this process — ring "
                    "files are group-readable by `jts-ring`; this process is not "
                    "in that group"
                ),
            )
        return RingFlowState(
            state=RING_FLOW_UNREADABLE,
            detail="ring file carries no coherent v1 JRIN header",
        )

    if now_ns is None:
        now_ns = time.monotonic_ns()
    writer_age = _heartbeat_age_ns(header.writer_heartbeat_ns, now_ns)
    reader_age = _heartbeat_age_ns(header.reader_heartbeat_ns, now_ns)
    # Occupancy is only meaningful when the cursor pair is coherent. Over-range
    # (the writer lapped a wedged reader, or a torn read) and inverted are both
    # published as None rather than as a number: the C resolves an out-of-range
    # W - R by resyncing to the tip, so the raw difference is not an occupancy
    # anybody would act on. The raw cursors stay published, so nothing is hidden.
    raw_occupancy = header.write_seq - header.read_seq
    occupancy: int | None = (
        raw_occupancy if 0 <= raw_occupancy <= header.n_slots else None
    )
    evidence = {
        "writer_age_ns": writer_age,
        "reader_age_ns": reader_age,
        "write_seq": header.write_seq,
        "read_seq": header.read_seq,
        "occupancy_slots": occupancy,
        "writer_epoch": header.writer_epoch,
    }

    writer_live = _end_is_live(
        header.writer_pid, header.writer_heartbeat_ns, now_ns, timeout_ns
    )
    reader_live = _end_is_live(
        header.reader_pid, header.reader_heartbeat_ns, now_ns, timeout_ns
    )

    if not writer_live:
        if header.writer_heartbeat_ns == 0:
            why = "ring has never been written (no writer heartbeat)"
        elif header.writer_pid == 0:
            why = "the writer has closed the ring (writer pid cleared)"
        else:
            why = (
                f"writer heartbeat is itself stale ({(writer_age or 0) / 1e6:.0f} ms)"
            )
        return RingFlowState(
            state=RING_FLOW_IDLE,
            detail=f"{why} — an idle or stopped ring, not a stall",
            **evidence,
        )
    if reader_live:
        return RingFlowState(
            state=RING_FLOW_FLOWING,
            detail=(
                f"both ends live (writer {(writer_age or 0) / 1e6:.0f} ms, reader "
                f"{(reader_age or 0) / 1e6:.0f} ms behind)"
            ),
            **evidence,
        )

    # The writer is live and no reader is. Startup transient or a real stall?
    never_attached = header.reader_pid == 0 and header.reader_heartbeat_ns == 0
    budget = _priming_slot_budget(header, timeout_ns)
    if never_attached and header.write_seq <= budget:
        return RingFlowState(
            state=RING_FLOW_PRIMING,
            detail=(
                f"writer live, no reader attached yet — {header.write_seq} slot(s) "
                f"published, inside the {budget}-slot startup window"
            ),
            **evidence,
        )
    if never_attached:
        why = f"no reader has ever attached ({header.write_seq} slots published)"
    elif header.reader_pid == 0:
        # Reached the instant a reader closes cleanly, not only after it wedges:
        # the writer stops honouring a pid-less reader immediately, so the drops
        # start immediately too.
        why = "the reader closed the ring and has not come back"
    else:
        why = (
            f"reader pid {header.reader_pid} stopped stamping its heartbeat "
            f"({(reader_age or 0) / 1e6:.0f} ms behind)"
        )
    return RingFlowState(
        state=RING_FLOW_READER_STALLED,
        detail=(
            f"{why} while the writer is live — the ioplug has demoted the reader "
            "and is free-running, dropping the oldest slot per publish. read_seq "
            "is being advanced by the WRITER, so its rise is the drop cursor"
        ),
        **evidence,
    )


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
