# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ``jts_ring`` transport platform: asset vocabulary and live-ring health.

Single source of truth for the three ring-platform assets every box ships —
the compiled ioplug ``.so``, the conf.d PCM definitions
(:data:`RING_CONF_PCMS`), and the ``/dev/shm/jts-ring`` tmpfs directory — plus
the live-ring health layer built on the on-disk SHM header
(:class:`RingHeader`): stall/liveness verdicts (:func:`ring_stall_verdict`),
priming/flow state (:func:`ring_flow_state`), and header-vs-conf.d coherence
(:func:`ring_header_matches_conf`).

Import-cheap (stdlib, plus the import-free ``jasper.fanin_coupling`` constants)
so callers can resolve asset presence and ring health without pulling in the
doctor.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from jasper.fanin_coupling import (
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_SLOT_FRAMES,
    RingWire,
)

# The aarch64 ALSA plugin dir the ioplug ``.so`` installs into. Canonical home
# for the value — do not re-spell it as a literal elsewhere. Build and install
# path: ``deploy/lib/install/ring-platform.sh``.
RING_ALSA_PLUGIN_DIR = "/usr/lib/aarch64-linux-gnu/alsa-lib"
RING_IOPLUG_SO = "libasound_module_pcm_jts_ring.so"
RING_CONF_D = "/etc/alsa/conf.d/60-jts-ring.conf"
# The tmpfs directory the ring files live in (``deploy/tmpfiles/jts-ring.conf``,
# mode 3775 root:jts-ring — sticky + setgid + group-write).
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
# ``c/jts-ring-ioplug/jts_ring_shm.h``, pinned against that header by
# ``tests/test_ring_slot_ceiling_pin.py`` so the two spellings cannot drift.
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


# The conf.d PCM block name for Ring A (fan-in's program ring). ``n_slots`` under
# this block is the drift axis with ``JASPER_FANIN_RING_SLOTS`` (Ring B is the
# ``jts_ring_playback`` block, paired with ``JASPER_OUTPUTD_SHM_RING_SLOTS``).
RING_A_CONF_PCM = "jts_ring_capture"
RING_B_CONF_PCM = "jts_ring_playback"
RING_ACTIVE_CONF_PCM = RING_ACTIVE_PLAYBACK_DEVICE
# Every PCM block the ring conf.d defines, in file order. The renderer walks it
# and so do the guards, so "which blocks exist" is one list rather than a
# repeated literal triple.
RING_CONF_PCMS = (RING_A_CONF_PCM, RING_B_CONF_PCM, RING_ACTIVE_CONF_PCM)

# What a conf.d PCM block declares when it omits ``format`` / ``channels``.
# Mirrors the C ioplug's ``JTS_RING_DEFAULT_FORMAT`` / ``JTS_RING_DEFAULT_CHANNELS``
# (``c/jts-ring-ioplug/pcm_jts_ring.c``). The renderer writes a key only where
# the resolved wire differs from these, so a block whose value equals one never
# gains a line.
#
# ``RING_CONF_DEFAULT_FORMAT`` MIRRORS THE C IOPLUG AND DOES NOT FOLLOW THE
# RESOLVER. The ring wire's resolver defaults WIDE
# (``jasper.fanin_coupling.resolve_ring_wire_format``) while the compiled-in
# ioplug default is ``S16_LE``, and moving this constant to match the resolver
# would make Python believe a stale ``.so`` parses a ``format`` field it cannot
# — precisely what :func:`ring_ioplug_wire_supported` exists to catch. The
# disagreement is what keeps that capability gate live, and it is why
# ``deploy/alsa/conf.d/60-jts-ring.conf`` DECLARES ``format S32_LE`` explicitly
# rather than relying on an omitted key.
RING_CONF_DEFAULT_FORMAT = "S16_LE"
RING_CONF_DEFAULT_CHANNELS = 2


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
    """SHA-256 of the installed ioplug ``.so``, or ``None`` if unreadable.

    Chunked read: the ``.so`` is small, but streaming keeps the reconciler's
    memory bounded on a 1 GB box regardless of what ships there later.
    """
    import hashlib

    digest = hashlib.sha256()
    try:
        with open(ring_ioplug_so_path(plugin_dir=plugin_dir), "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def ring_wire_capabilities(wire: RingWire) -> frozenset[str]:
    """The ioplug capabilities this wire NEEDS, beyond the ioplug's own defaults.

    The conf.d renderer writes a ``format`` / ``channels`` key only where the
    resolved wire differs from :data:`RING_CONF_DEFAULT_FORMAT` /
    :data:`RING_CONF_DEFAULT_CHANNELS` (see :func:`render_ring_conf_wire`), and
    an omitted key is what an older ioplug expects. So the capability a wire
    needs is the set of keys it forces onto the conf.d — non-empty on every box
    that has not pinned itself narrow, because the wire resolver defaults WIDE
    (``jasper.fanin_coupling.resolve_ring_wire_format``) while
    :data:`RING_CONF_DEFAULT_FORMAT` stays the C ioplug's own ``S16_LE``.

    THREE AXES, one per conf.d key the renderer can write:

    * ``format`` — every block shares one token, so one comparison covers all
      three;
    * ``channels`` on Ring A / Ring B — the full-range stereo pair;
    * ``channels`` on the ACTIVE block — the post-crossover per-driver width, a
      SEPARATE axis because :func:`render_ring_conf_wire` writes that block from
      ``ring_active_channels`` while a roleful box's Ring A/B stay structurally
      2, so a roleful box driving 4+ channels forces the key through this block
      alone. The coercion mirrors the renderer's own
      (``ring_active_channels or RING_CONF_DEFAULT_CHANNELS``) so "which boxes
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
    if wire.sample_format != RING_CONF_DEFAULT_FORMAT:
        needed.add(RING_CAP_WIRE_FORMAT)
    if (
        wire.ring_a_channels != RING_CONF_DEFAULT_CHANNELS
        or wire.ring_b_channels != RING_CONF_DEFAULT_CHANNELS
        or (wire.ring_active_channels or RING_CONF_DEFAULT_CHANNELS)
        != RING_CONF_DEFAULT_CHANNELS
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


# The ring's slot geometry IS fixed at ``RING_SLOT_FRAMES`` (128). jasper-fanin
# creates Ring A with that COMPILE-TIME constant
# (rust/jasper-ring/src/layout.rs, no env override) and every conf.d PCM block
# shares one period value, so the conf.d period is pinned to it too — this file
# is not free to follow a DAC. Making the slot derivable is issue #2147.
#
# The mismatch this parser exists to catch is the OTHER side: the
# ``jts_ring_playback`` ioplug opens Ring B with the conf.d's ``period_frames``,
# and jasper-outputd's ``ShmRingSource`` attaches with
# ``JASPER_OUTPUTD_PERIOD_FRAMES`` (one slot per DAC period — see
# rust/jasper-outputd/src/config.rs). A geometry mismatch against an existing
# ring is a hard ``open()`` error in the C ioplug. On a box whose resolved
# outputd period is not 128 (the packaged default is 1024; a DAC declaring a
# 128-frame latency floor lowers it, and so does an operator
# ``JASPER_OUTPUTD_PERIOD_FRAMES`` in ``/etc/jasper/jasper.env``, which outranks
# the reconciler's floor-derived value), CamillaDSP's ring open would fail and
# the arm would roll back with a confusing daemon-level error — so the coupling
# reconciler PREFLIGHTs the match and refuses to arm with a crisp reason
# instead. The fix is always to bring the OUTPUTD period to the slot, never to
# raise this file.
#
# :func:`render_ring_conf_wire`'s PERIOD axis therefore has exactly one live
# job: converging a conf.d that has drifted OFF ``RING_SLOT_FRAMES`` (a hand
# edit, a half install) back onto it. It refuses any other target. Its format
# and channels axes are per-box and carry no such fixed target.
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

    Returns the single period value the ``jts_ring_*`` PCM blocks declare (every
    ring shares one slot geometry). ``None`` when the file is absent,
    unreadable, has no ``period_frames`` line, or declares *inconsistent* values
    across the blocks (a torn conf.d — the caller treats that as a mismatch, not
    a silent pick). Pure text parse, no ALSA.
    """
    path = RING_CONF_D if conf_d is None else conf_d
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    values = {int(m.group("frames")) for m in _RING_CONF_PERIOD_RE.finditer(text)}
    if len(values) != 1:
        # No period line, or the blocks disagree — not a usable single geometry.
        return None
    return next(iter(values))


# --- Ring A slot-count coherence -------------------------------------------
#
# The ring's ``n_slots`` is a SECOND geometry axis independent of period_frames.
# fan-in creates Ring A with ``resolve_ring_slots(JASPER_FANIN_RING_SLOTS)`` slots
# (default 2); the ``jts_ring_capture`` ioplug conf.d block pins ``n_slots`` (2 in
# the shipped file); the on-disk ring header records the ``n_slots`` the writer
# actually created. A mismatch on ANY of the three axes is a hard failure:
#   - fan-in env vs conf.d: fan-in creates a ring at one slot count while
#     CamillaDSP's ioplug attaches expecting another → hw_params EINVAL + ioplug
#     attach_fatal ("ring header does not match expected geometry") →
#     CamillaDSP crash-loop → start-limit-hit.
#   - on-disk vs expected: a stale ring file left over from a prior geometry is
#     a create-or-ATTACH open() error for the writer, because
#     ``jasper_ring::RingWriter::create_or_attach`` validates the existing
#     header's geometry against the requested one.
#
# Per-block field parsing. The conf.d has one PCM block per ring
# (:data:`RING_CONF_PCMS`) and they can declare DIFFERENT geometry: Ring A's
# ``channels`` is always the stereo program, Ring B's follows the box's output
# topology, and the ACTIVE ring's is the post-crossover per-driver width. So
# every field parser here is scoped to one named block; a whole-file scan would
# collapse legitimately different values into "torn".
#
# ``_ring_conf_block_body`` finds that block by MATCHING BRACES rather than by
# regex. A `[^}]*` body terminates at the FIRST `}`, so any nested block —
# ALSA's own ``hint { … }`` convention is the obvious one — would truncate the
# body and hide every field after it. Quoted values are skipped so a brace
# inside ``path "…"`` cannot unbalance the scan.
_RING_CONF_BLOCK_OPEN_RE_TEMPLATE = r"pcm\.{name}[^\S\n]*\{{"


def _ring_conf_block_body_span(text: str, pcm_name: str) -> tuple[int, int] | None:
    """``(start, end)`` offsets of a named PCM block's BODY, or ``None``.

    ``start`` is just past the opening brace, ``end`` is at the matching closing
    brace. Returns ``None`` when the block is absent or its braces never balance
    (a truncated / torn file — report nothing rather than guess a body).
    """
    opener = re.compile(_RING_CONF_BLOCK_OPEN_RE_TEMPLATE.format(name=re.escape(pcm_name)))
    m = opener.search(text)
    if m is None:
        return None
    start = m.end()
    depth = 1
    i = start
    quote: str | None = None
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    return None


def _ring_conf_block_body(text: str, pcm_name: str) -> str | None:
    span = _ring_conf_block_body_span(text, pcm_name)
    return None if span is None else text[span[0] : span[1]]


def _read_conf_text(conf_d: str | None) -> str | None:
    """The ring conf.d's text, or ``None`` when it is absent/unreadable."""
    path = RING_CONF_D if conf_d is None else conf_d
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


# One regex per scalar field, shared by the parsers and the renderer so a conf.d
# the parser accepts is one the renderer can update (and vice versa).
# Horizontal-whitespace classes (not ``\s``) keep both directions line-scoped
# under ``re.MULTILINE``.
_RING_CONF_N_SLOTS_RE = re.compile(
    r"^(?P<indent>[^\S\n]*)n_slots[^\S\n]+(?P<value>\d+)[^\S\n]*$", re.MULTILINE
)
_RING_CONF_CHANNELS_RE = re.compile(
    r"^(?P<indent>[^\S\n]*)channels[^\S\n]+(?P<value>\d+)[^\S\n]*$", re.MULTILINE
)
# ``format`` is an ALSA token, optionally quoted (both spellings are valid ALSA
# conf and the C ioplug reads either through snd_config_get_string).
_RING_CONF_FORMAT_RE = re.compile(
    r"^(?P<indent>[^\S\n]*)format[^\S\n]+(?P<quote>[\"']?)(?P<value>[A-Za-z0-9_]+)"
    r"(?P=quote)[^\S\n]*$",
    re.MULTILINE,
)


def _single_block_value(
    pattern: re.Pattern[str],
    pcm_name: str,
    conf_d: str | None,
    *,
    absent: str | None = None,
) -> str | None:
    """The single value ``pattern`` matches inside one PCM block, or ``None``.

    ``None`` means indeterminate — file absent/unreadable, block missing, or the
    block declaring the field more than once with different values. This never
    silently picks one of a torn pair.

    ``absent`` is what an UNDECLARED key means. ``None`` (the default) keeps the
    key mandatory: a block that omits it is indeterminate, which is right for
    ``n_slots``/``period_frames``, where nothing supplies a value if the conf.d
    does not. Pass a string for a key whose omission the C ioplug fills in with
    a documented default — an omitted ``format``/``channels`` genuinely declares
    that wire.
    """
    text = _read_conf_text(conf_d)
    if text is None:
        return None
    body = _ring_conf_block_body(text, pcm_name)
    if body is None:
        return None
    values = {m.group("value") for m in pattern.finditer(body)}
    if not values:
        return absent
    if len(values) != 1:
        return None
    return next(iter(values))


def ring_conf_n_slots(pcm_name: str, conf_d: str | None = None) -> int | None:
    """Parse the ``n_slots`` pinned for a named PCM block in the ring conf.d.

    ``pcm_name`` is one of :data:`RING_CONF_PCMS`.
    Returns the single ``n_slots`` value that block declares, or ``None`` when
    the file is absent/unreadable, the block is missing, or the block declares no
    single ``n_slots`` (a torn conf.d — the caller treats that as a mismatch, not
    a silent pick). Pure text parse, no ALSA.
    """
    raw = _single_block_value(_RING_CONF_N_SLOTS_RE, pcm_name, conf_d)
    return None if raw is None else int(raw)


def ring_conf_channels(pcm_name: str, conf_d: str | None = None) -> int | None:
    """Parse the ``channels`` a named PCM block declares, or the ioplug default.

    An ABSENT ``channels`` key is not indeterminate — the C ioplug defaults it to
    :data:`RING_CONF_DEFAULT_CHANNELS`, so a block that omits it declares exactly
    that wire. This returns the default in that case, and ``None`` only when the
    conf.d/block itself cannot be read or the block declares the key more than
    once with different values.
    """
    raw = _single_block_value(
        _RING_CONF_CHANNELS_RE,
        pcm_name,
        conf_d,
        absent=str(RING_CONF_DEFAULT_CHANNELS),
    )
    return None if raw is None else int(raw)


def ring_conf_format(pcm_name: str, conf_d: str | None = None) -> str | None:
    """Parse the ``format`` a named PCM block declares, or the ioplug default.

    Same absent-means-default contract as :func:`ring_conf_channels`: the C
    ioplug defaults an undeclared ``format`` to
    :data:`RING_CONF_DEFAULT_FORMAT`, so a block that omits the key still
    declares a complete wire.

    The SHIPPED conf.d does not rely on that for this key — it spells
    ``format S32_LE`` in every block, because the resolver defaults wide while
    the plugin's compiled-in default is narrow, so silence here would declare
    the opposite of what every other end resolves. The absent-key branch remains
    live for a hand-edited or foreign file, and for ``channels``, which the
    shipped file does still omit.
    """
    return _single_block_value(
        _RING_CONF_FORMAT_RE, pcm_name, conf_d, absent=RING_CONF_DEFAULT_FORMAT
    )


@dataclass(frozen=True)
class RingConfWireRender:
    """The outcome of rendering the ring conf.d wire for one box.

    ``changed`` is False for the no-write outcome: the conf already declares the
    target wire.

    ``previous_period_frames`` is ``None`` for a TORN conf.d whose PCM blocks
    disagreed, because there was no single previous value to report.

    ``ring_active_channels`` is what the ACTIVE ring's block was rendered to —
    the ioplug default on every box without an active ring, which is what the
    shipped file already declares.
    """

    changed: bool
    period_frames: int
    previous_period_frames: int | None
    sample_format: str
    ring_a_channels: int
    ring_b_channels: int
    conf_d: str
    ring_active_channels: int = RING_CONF_DEFAULT_CHANNELS


def _render_block_field(
    body: str,
    *,
    pattern: re.Pattern[str],
    key: str,
    value: str,
    default: str,
) -> str:
    """Return ``body`` with ``key`` declaring ``value``.

    Three cases, and the split is what keeps an unrendered conf.d byte-identical:

    - the key is already declared → SUBSTITUTE in place (indentation, ordering
      and every other line survive);
    - the key is absent and ``value`` is the ioplug's own ``default`` → write
      NOTHING. An absent key already declares that wire, so adding the line
      would churn the file for no change in meaning;
    - the key is absent and ``value`` differs from the default → INSERT it,
      anchored after the block's ``n_slots`` (else ``period_frames``) line so
      the geometry keys stay together and the indentation is copied from the
      anchor.

    A present key is never DELETED when it returns to the default: rewriting it
    to the explicit default converges just as exactly.
    """
    if pattern.search(body):
        return pattern.sub(lambda m: f"{m.group('indent')}{key} {value}", body)
    if value == default:
        return body
    anchor = _RING_CONF_N_SLOTS_RE.search(body) or _RING_CONF_PERIOD_RE.search(body)
    if anchor is None:
        raise ValueError(
            f"ring conf.d block declares neither n_slots nor period_frames, so "
            f"there is no anchor to insert '{key} {value}' after; refusing to "
            "invent a block shape — redeploy to reinstall the conf.d"
        )
    indent = anchor.group("indent")
    return (
        body[: anchor.end()] + f"\n{indent}{key} {value}" + body[anchor.end() :]
    )


def render_ring_conf_wire(
    wire: RingWire,
    *,
    conf_d: str | None = None,
) -> RingConfWireRender:
    """Rewrite the ring conf.d so every PCM block declares ``wire``.

    ``wire`` is a :class:`~jasper.fanin_coupling.RingWire` — the ONE per-box
    resolution of the ring's geometry. Taking the resolved object rather than
    four loose scalars is deliberate: the four ends of the ring must declare the
    same tuple, so a call site cannot pass a format from one resolution and a
    channel count from another.

    What lands where:

    - ``period_frames`` — every block, one shared value (the ring slot IS one
      outputd DAC period). The CALLER decides whether a render is warranted (the
      rule is "only from a DECLARED
      :class:`~jasper.audio_hardware.dac.LatencyFloor`"), so this function never
      consults the DAC registry itself.
    - ``format`` — every block, one shared value. The rings carry one wire
      format.
    - ``channels`` — PER BLOCK. ``jts_ring_capture`` (Ring A) declares
      ``ring_a_channels``: everything upstream of CamillaDSP is a stereo
      program, and fan-in's mixer is stereo. ``jts_ring_playback`` (Ring B)
      declares ``ring_b_channels``, which follows the box's output topology.
      ``jts_ring_active_playback`` (the ACTIVE ring) declares
      ``ring_active_channels`` — the post-crossover per-driver width — and when
      the wire resolves ``None`` there (every non-roleful box) that block keeps
      the ioplug's default, i.e. is left exactly as shipped. This is the axis on
      which the three rings legitimately differ, which is why the parsers above
      are block-scoped.

    **The only renderable period is** :data:`~jasper.fanin_coupling.RING_SLOT_FRAMES`.
    Ring A's slot size is fan-in's COMPILE-TIME constant
    (``rust/jasper-ring/src/layout.rs`` ``RING_SLOT_FRAMES``, with no env
    override; ``mixer.rs`` creates the ring with it), so writing any other
    period into ``pcm.jts_ring_capture`` would make CamillaDSP's ioplug attach
    expect a geometry fan-in never builds — a hard ``RING_ATTACH_FATAL``
    ("ring header does not match expected geometry") that CRASHES shm_ring at
    arm rather than refusing it. Asking for a different period is therefore a
    caller bug and raises; making the slot floor-derived across fan-in, the
    ioplug, the CamillaDSP emitter and the negotiation model is issue #2147.
    This guard is defence in depth behind the caller's own floor gate.

    **Write-on-change only.** When the conf already declares exactly this wire
    the file is left GENUINELY untouched — no rewrite, no mtime churn. A box on
    the shipped wire never gains a line either, by two routes: the shipped
    ``format`` line is already the resolved token so it is SUBSTITUTED in place
    with the same value, and an omitted ``channels`` key already declares
    :data:`RING_CONF_DEFAULT_CHANNELS` so nothing is inserted. Otherwise the
    whole file is published through
    :func:`jasper.atomic_io.atomic_write_text` (``preserve_target_stat``), so a
    reader never observes a half-written conf.d and the installed file's
    uid/gid/mode survive the replace.

    Only geometry VALUES move: the ``path`` values, block ordering and every
    comment survive verbatim, because each rewrite is a substitution over the
    same regex the matching parser reads.

    Raises ``ValueError`` for a period that is not ``RING_SLOT_FRAMES``, a conf.d
    that declares no ``period_frames`` line at all, or one whose PCM blocks
    cannot be found (a torn / foreign file — never invent one), and ``OSError``
    when the file cannot be read or replaced.
    """
    period_frames = wire.period_frames
    sample_format = wire.sample_format
    ring_a_channels = wire.ring_a_channels
    ring_b_channels = wire.ring_b_channels
    # A box with no active ring (every non-roleful topology, and any roleful one
    # whose driven width is indeterminate) declares the ioplug's own default in
    # that block — what the SHIPPED file already says — so the block is left
    # byte-identical rather than rendered to an invented width.
    ring_active_channels = wire.ring_active_channels or RING_CONF_DEFAULT_CHANNELS

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
    # A torn conf.d (the blocks disagreeing) has no single previous value to
    # report, but it is still rendered: converging every line onto the target is
    # exactly the repair. Mirrors ring_conf_period_frames returning None there.
    distinct = set(previous)

    rendered = _RING_CONF_PERIOD_RE.sub(
        lambda m: f"{m.group('indent')}period_frames {period_frames}",
        text,
    )
    # What each block gets, keyed by name. The ACTIVE block is required only when
    # there is something to write into it: a conf.d predating the active ring (an
    # in-flight deploy, where the new Python is installed a step before the new
    # conf.d) has no such block, and on that box there is no active ring either,
    # so raising would turn an upgrade ordering into a failed reconcile over a
    # value that was never going to change. When the wire DOES resolve a real
    # active width the block's absence is a genuine fault and still raises: the
    # ioplug attaches with what the block says, so skipping the write silently
    # would ship a shear.
    #
    # The arm path does not rely on that leniency — ``active_ring_endpoint_proof``
    # independently refuses to arm unless the block declares the resolved width,
    # so a missing block fails CLOSED there whatever the renderer did.
    per_block = {
        RING_A_CONF_PCM: (ring_a_channels, True),
        RING_B_CONF_PCM: (ring_b_channels, True),
        RING_ACTIVE_CONF_PCM: (
            ring_active_channels,
            ring_active_channels != RING_CONF_DEFAULT_CHANNELS,
        ),
    }
    # WALK :data:`RING_CONF_PCMS`, the one list of which blocks exist, rather than
    # a second literal tuple. The lookup is what makes a fourth ring FAIL LOUD:
    # adding a name to RING_CONF_PCMS without deciding its width raises here, at
    # render time, rather than shipping a block the renderer never touches.
    undeclared = [name for name in RING_CONF_PCMS if name not in per_block]
    if undeclared:
        raise ValueError(
            f"ring conf.d renderer has no width for {', '.join(undeclared)}: every "
            "PCM in RING_CONF_PCMS must declare what this renderer writes into it "
            "— refusing to render a conf.d with a block nobody owns"
        )
    for pcm_name in RING_CONF_PCMS:
        channels, required = per_block[pcm_name]
        # Re-find the span each pass: rendering Ring A's body moves Ring B's.
        span = _ring_conf_block_body_span(rendered, pcm_name)
        if span is None:
            if not required:
                continue
            raise ValueError(
                f"ring conf.d ({path}) has no readable pcm.{pcm_name} block "
                "(absent or unbalanced braces); refusing to invent one — "
                "redeploy to reinstall it"
            )
        body = rendered[span[0] : span[1]]
        # Channels first, then format: each insert anchors immediately after
        # ``n_slots``, so rendering in reverse leaves the file reading
        # period_frames / n_slots / format / channels in every block.
        body = _render_block_field(
            body,
            pattern=_RING_CONF_CHANNELS_RE,
            key="channels",
            value=str(channels),
            default=str(RING_CONF_DEFAULT_CHANNELS),
        )
        body = _render_block_field(
            body,
            pattern=_RING_CONF_FORMAT_RE,
            key="format",
            value=sample_format,
            default=RING_CONF_DEFAULT_FORMAT,
        )
        rendered = rendered[: span[0]] + body + rendered[span[1] :]

    if rendered == text:
        # A no-op render can only happen when every period_frames line already
        # read `period_frames`: the substitution rewrites EVERY matched line to
        # that one target, so a torn `distinct` (2+ values, or a single value
        # differing from the target) would always change at least one line.
        # `distinct == {period_frames}` is guaranteed here, not re-checked.
        return RingConfWireRender(
            changed=False,
            period_frames=period_frames,
            previous_period_frames=period_frames,
            sample_format=sample_format,
            ring_a_channels=ring_a_channels,
            ring_b_channels=ring_b_channels,
            ring_active_channels=ring_active_channels,
            conf_d=path,
        )
    # Function-local so the module keeps its stdlib-only import cost for the
    # presence/parse callers; only the renderer pays for atomic_io.
    # preserve_target_stat carries the installed file's uid/gid/mode across the
    # replace, so the 0644 renderer-user resolvability the conf.d depends on
    # survives and a root-run reconcile does not re-own a file it did not create.
    from jasper.atomic_io import atomic_write_text

    atomic_write_text(path, rendered, preserve_target_stat=True)
    return RingConfWireRender(
        changed=True,
        period_frames=period_frames,
        previous_period_frames=previous[0] if len(distinct) == 1 else None,
        sample_format=sample_format,
        ring_a_channels=ring_a_channels,
        ring_b_channels=ring_b_channels,
        ring_active_channels=ring_active_channels,
        conf_d=path,
    )


# The ring SHM header layout — the u32 prefix of rust/jasper-ring/src/layout.rs,
# duplicated here (Python has no way to link the Rust const). The golden layout
# test in the Rust crate is the offset SSOT, and ``test_ring_assets`` pins these
# against it.
#
# ALL SIX declared geometry fields are read, not just the two the slot-count
# guard needs: the Rust/C attach compares every one of them field-by-field, so a
# Python guard reading only ``period_frames``/``n_slots`` would report a
# coherent ring where the ioplug attach will fail.
_RING_MAGIC = 0x4A52_494E  # "JRIN" little-endian (layout.rs MAGIC)
_RING_HEADER_BYTES = 128  # layout.rs HEADER_BYTES
# The one layout version this parser's offsets describe (layout.rs VERSION).
_RING_HEADER_VERSION = 1
_RING_OFF_MAGIC = 0  # u32
_RING_OFF_VERSION = 4  # u32
_RING_OFF_RATE = 8  # u32
_RING_OFF_CHANNELS = 12  # u32
_RING_OFF_SAMPLE_FORMAT = 16  # u32
_RING_OFF_PERIOD_FRAMES = 20  # u32
_RING_OFF_N_SLOTS = 24  # u32
# The two RUNTIME liveness fields, both little-endian u64 CLOCK_MONOTONIC
# nanoseconds (layout.rs OFF_WRITER_HEARTBEAT_NS / OFF_READER_HEARTBEAT_NS).
# Unlike the six geometry fields above these change every period, so they answer
# "is this ring moving", not "what shape is it".
_RING_OFF_WRITER_HEARTBEAT_NS = 64  # u64
_RING_OFF_READER_HEARTBEAT_NS = 80  # u64
# The remaining RUNTIME fields, all little-endian u64, inside the same 128 bytes.
# What each one buys an observer that the two heartbeats above do not:
#   - the two SEQUENCE cursors are the only cross-process trace of DROPS. When
#     the writer demotes an absent reader it advances ``read_seq`` on that
#     reader's behalf, one slot per dropped publish, so while nothing live is
#     stamping ``reader_heartbeat_ns`` a rising ``read_seq`` IS the drop cursor.
#     Their DIFFERENCE is the ring's occupancy.
#   - ``reader_pid`` separates "no reader has ever attached" (0 with a zero
#     heartbeat) from "a reader attached and stopped beating" (a pid with a
#     stale heartbeat). Both leave the reader not-live; only the second is a
#     fault. ``ring_flow_state`` uses the split.
#   - ``writer_epoch`` counts writer REATTACHES, so a flapping writer is legible
#     without differencing journal lines.
_RING_OFF_WRITER_EPOCH = 32  # u64
_RING_OFF_WRITE_SEQ = 40  # u64
_RING_OFF_READ_SEQ = 48  # u64
_RING_OFF_WRITER_PID = 56  # u64
_RING_OFF_READER_PID = 72  # u64

# The staleness window a heartbeat may fall behind before its stamper counts as
# gone. NOT a number chosen here: it is the C ioplug's own
# ``JTS_RING_WRITER_LIVENESS_TIMEOUT_NS`` (``jts_ring_shm.h``), the exact
# threshold ``reader_is_live`` applies to ``reader_heartbeat_ns`` when deciding
# whether to demote a reader and free-run. Spelling the same number makes an
# observer's verdict and the mechanism it reports agree by construction; an
# observer with its own threshold would eventually disagree with the writer
# about who is alive. Pinned against the C header by
# ``tests/test_ring_stall_alarm.py``.
RING_LIVENESS_TIMEOUT_NS = 2_000_000_000

# The ``sample_format`` header field's wire values (layout.rs
# SAMPLE_FORMAT_S16LE / SAMPLE_FORMAT_S32LE, mirrored by the C header's
# JTS_RING_SAMPLE_FORMAT_*). These ids are written into the shared header and
# compared field-by-field on attach, so they are a wire contract pinned to the
# literals 1 and 2 by ``tests/test_ring_slot_ceiling_pin.py``.
RING_SAMPLE_FORMAT_S16LE = 1
RING_SAMPLE_FORMAT_S32LE = 2
# Header sample_format id -> the ALSA format token the conf.d and the emitters
# spell. ``jasper.fanin_coupling`` owns that token vocabulary; this map is how a
# header byte is named in a human-readable mismatch detail.
RING_SAMPLE_FORMAT_NAMES = {
    RING_SAMPLE_FORMAT_S16LE: "S16_LE",
    RING_SAMPLE_FORMAT_S32LE: "S32_LE",
}


@dataclass(frozen=True)
class RingHeader:
    """The geometry fields read from an on-disk ring SHM file header.

    ``valid`` is False when the file is absent, too small for a header, does not
    carry the ``JRIN`` magic (a torn / partially-initialized / foreign file), or
    declares a layout ``version`` this parser does not describe. A
    ``valid=False`` header is NOT trusted for a geometry comparison — the caller
    treats it as "no coherent ring present".

    The version gate is a PARSER property, not a policy one: these offsets are
    the v1 layout's, so a file announcing another version may put different
    meanings at them and its "geometry" would be fiction. ``version`` is still
    reported so a caller can name what it saw.
    """

    valid: bool
    magic: int = 0
    version: int = 0
    rate: int = 0
    channels: int = 0
    sample_format: int = 0
    period_frames: int = 0
    n_slots: int = 0
    # RUNTIME, not geometry: CLOCK_MONOTONIC ns, 0 when never stamped. The
    # writer stamps its own every publish/wait tick; the reader stamps its own
    # every DAC period, filled or not. Compared against ``time.monotonic_ns()``
    # by :func:`ring_stall_verdict` — same clock, same box, so freshness is a
    # single-sample read and needs no sampling window.
    writer_heartbeat_ns: int = 0
    reader_heartbeat_ns: int = 0
    # RUNTIME, not geometry (see the offset block above). ``write_seq`` /
    # ``read_seq`` are monotonic slot cursors; ``writer_pid`` / ``reader_pid``
    # are 0 when that end is not attached; ``writer_epoch`` increments on every
    # writer reattach. All 0 on a never-used ring.
    writer_epoch: int = 0
    write_seq: int = 0
    read_seq: int = 0
    writer_pid: int = 0
    reader_pid: int = 0

    @property
    def sample_format_name(self) -> str:
        """The header's ``sample_format`` as an ALSA token, or ``id=N`` for one
        outside the two the layout defines."""
        return RING_SAMPLE_FORMAT_NAMES.get(
            self.sample_format, f"id={self.sample_format}"
        )


def read_ring_header(path: str) -> RingHeader:
    """Read the geometry fields from a ring SHM file header (little-endian u32s).

    Pure filesystem read of the first :data:`_RING_HEADER_BYTES` bytes — no mmap,
    no ALSA, no writer disturbance (read-only open). Returns ``RingHeader(valid=
    False)`` for an absent/short/magic-less file, and for one whose ``version``
    is not :data:`_RING_HEADER_VERSION`. The magic gate matters: the Rust writer
    publishes ``JRIN`` LAST (a Release store), so a header without it is not yet
    a coherent ring and must not drive a delete/mismatch decision on its (zero)
    geometry fields.
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
    version = struct.unpack_from("<I", head, _RING_OFF_VERSION)[0]
    if version != _RING_HEADER_VERSION:
        # Magic matched but the layout is not the one these offsets describe:
        # report the version, vouch for nothing else.
        return RingHeader(valid=False, magic=magic, version=version)
    return RingHeader(
        valid=True,
        magic=magic,
        version=version,
        rate=struct.unpack_from("<I", head, _RING_OFF_RATE)[0],
        channels=struct.unpack_from("<I", head, _RING_OFF_CHANNELS)[0],
        sample_format=struct.unpack_from("<I", head, _RING_OFF_SAMPLE_FORMAT)[0],
        period_frames=struct.unpack_from("<I", head, _RING_OFF_PERIOD_FRAMES)[0],
        n_slots=struct.unpack_from("<I", head, _RING_OFF_N_SLOTS)[0],
        writer_heartbeat_ns=struct.unpack_from(
            "<Q", head, _RING_OFF_WRITER_HEARTBEAT_NS
        )[0],
        reader_heartbeat_ns=struct.unpack_from(
            "<Q", head, _RING_OFF_READER_HEARTBEAT_NS
        )[0],
        writer_epoch=struct.unpack_from("<Q", head, _RING_OFF_WRITER_EPOCH)[0],
        write_seq=struct.unpack_from("<Q", head, _RING_OFF_WRITE_SEQ)[0],
        read_seq=struct.unpack_from("<Q", head, _RING_OFF_READ_SEQ)[0],
        writer_pid=struct.unpack_from("<Q", head, _RING_OFF_WRITER_PID)[0],
        reader_pid=struct.unpack_from("<Q", head, _RING_OFF_READER_PID)[0],
    )


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
    timeout_ns: int = RING_LIVENESS_TIMEOUT_NS,
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
    import time

    header = read_ring_header(path)
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


def _priming_slot_budget(header: RingHeader, timeout_ns: int) -> int:
    """Slots a nominal writer publishes in one liveness window.

    The startup grace, in the ring's OWN units: ``rate``/``period_frames`` come
    off the header and ``timeout_ns`` is the ioplug's own demotion window, so a
    ring with a different period or a different window gets a budget that tracks
    it. :func:`read_ring_header` gates on magic and version but NOT on the
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
    timeout_ns: int = RING_LIVENESS_TIMEOUT_NS,
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
    import os
    import time

    header = read_ring_header(path)
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
    header = read_ring_header(path)
    if not header.valid:
        return RingHeaderCoherence(present=False)

    expected_slots = (
        expected_n_slots
        if expected_n_slots is not None
        else ring_conf_n_slots(pcm_name, conf_d)
    )
    expected_period = ring_conf_period_frames(conf_d)
    expected_format = ring_conf_format(pcm_name, conf_d)
    expected_channels = ring_conf_channels(pcm_name, conf_d)

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
