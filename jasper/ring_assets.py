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
- ``jasper.cli.audio_config render-ring-conf-wire`` — the per-box conf.d
  **renderer** the output-hardware reconciler shells into. It reuses this
  module's own regexes to REWRITE the values it also parses, so the reader and
  the writer of the conf.d format cannot drift.

Import-cheap (stdlib, plus the import-free ``jasper.fanin_coupling`` constants)
so the reconciler and the socket-activated web surfaces can resolve asset
presence without pulling in the doctor.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from jasper.fanin_coupling import RING_SLOT_FRAMES, RingWire

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

# What a conf.d PCM block declares when it omits ``format`` / ``channels``.
# Mirrors the C ioplug's ``JTS_RING_DEFAULT_FORMAT`` / ``JTS_RING_DEFAULT_CHANNELS``
# (``c/jts-ring-ioplug/pcm_jts_ring.c``), which reproduce the pre-ring-v2 pinned
# wire exactly. They are what makes an UNRENDERED conf.d byte-identical to the
# shipped file while still declaring a complete wire: the renderer writes a key
# only where the resolved wire differs from these, so a box on the shipped wire
# never gains a line.
RING_CONF_DEFAULT_FORMAT = "S16_LE"
RING_CONF_DEFAULT_CHANNELS = 2


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


# ---------------------------------------------------------------------------
# ioplug PROVENANCE — what the .so that is INSTALLED can actually parse.
#
# Presence is not capability. The ioplug build is deliberately DEGRADE-TO-WARN
# (``deploy/lib/install/ring-platform.sh``): when the compile fails, the install
# continues and the PREVIOUS ``.so`` stays in place beside freshly-installed Rust
# daemons. Presence-only checks — and the doctor's open-probe, which a stale but
# structurally-valid ioplug passes — cannot see that. The failure that record
# closes is specific: a conf.d rendered with a ``format`` / ``channels`` key that
# the old ``.so`` does not know is refused at ``open()`` with ``-EINVAL``
# ("jts_ring: unknown field %s"), so CamillaDSP cannot start against the ring.
#
# So the installer records what it installed, and the reconciler COMPARES
# records — it never opens a PCM to find out (an open-probe against a live ring
# hits the ioplug's SPSC guard, and probing from the arm path is exactly the
# disturbance the doctor's armed-skip exists to avoid).
RING_IOPLUG_PROVENANCE = "/var/lib/jasper/ring-ioplug.provenance"

# The capability VOCABULARY: one token per conf.d field the ioplug must parse
# for a wire that declares it to be openable. These are not version numbers —
# a pre-ring-v2 ``.so`` refuses BOTH fields, and each was added independently,
# so the record names what is supported rather than when it was built.
RING_CAP_WIRE_FORMAT = "wire_format"
RING_CAP_WIRE_CHANNELS = "wire_channels"
RING_IOPLUG_CAPS = (RING_CAP_WIRE_FORMAT, RING_CAP_WIRE_CHANNELS)

# The provenance file's keys (a plain ``KEY=value`` text file, mode 0644, written
# by ``record_ring_ioplug_provenance`` in ring-platform.sh).
RING_PROVENANCE_SHA_KEY = "JTS_RING_IOPLUG_SHA256"
RING_PROVENANCE_CAPS_KEY = "JTS_RING_IOPLUG_CAPS"


@dataclass(frozen=True)
class RingIoplugProvenance:
    """What the installer recorded about the ioplug ``.so`` it installed.

    ``recorded`` is False when the file is absent or carries no usable sha —
    which is the state of every box that has not run an installer carrying this
    feature, and of every box whose ioplug build failed before a record was ever
    written. That is NOT an error condition by itself: a wire that needs no
    capability beyond the ioplug's own defaults never consults this record at
    all (see :func:`ring_ioplug_wire_supported`).
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

    ``path=None`` resolves :data:`RING_IOPLUG_PROVENANCE` at CALL time, not as a
    bound default — the same rule :func:`ring_conf_n_slots` follows, and for the
    same reason: a default bound at import captures the constant forever, so a
    caller (or a test) that repoints the module attribute is silently ignored
    and the read lands on the real path. That silence had a live cost here — the
    doctor's check named ``RING_IOPLUG_PROVENANCE`` in its own message while
    reading a path nothing could redirect, which is one fact with two answers.
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


def ring_ioplug_so_sha256(*, plugin_dir: str = RING_ALSA_PLUGIN_DIR) -> str | None:
    """SHA-256 of the installed ioplug ``.so``, or ``None`` if unreadable.

    Chunked read (the ``.so`` is small, but streaming keeps the reconciler's
    memory bounded on a 1 GB box regardless of what ships there later).
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
    an omitted key is what a pre-ring-v2 ioplug expects. So the capability a wire
    needs is exactly the set of keys it forces onto the conf.d — which is EMPTY
    for the shipped wire, on every box today. That emptiness is the whole
    dormancy story: a narrow box's capability gate short-circuits before it ever
    looks at a record, so a box with no record behaves exactly as it did before.
    """
    needed: set[str] = set()
    if wire.sample_format != RING_CONF_DEFAULT_FORMAT:
        needed.add(RING_CAP_WIRE_FORMAT)
    if (
        wire.ring_a_channels != RING_CONF_DEFAULT_CHANNELS
        or wire.ring_b_channels != RING_CONF_DEFAULT_CHANNELS
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
    plugin_dir: str = RING_ALSA_PLUGIN_DIR,
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

    Short-circuits to ``ok`` when the wire needs nothing (:func:`ring_wire_capabilities`
    is empty), which is every box on the shipped wire — no file is read and no
    hash is computed on that path.

    ``provenance_path=None`` resolves the module constant at CALL time — see
    :func:`read_ring_ioplug_provenance` for why a bound default is wrong here.
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
                f"{wire.ring_b_channels}ch declares no conf.d field beyond the "
                "ioplug's own defaults, so any installed ioplug can open it"
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
# :func:`render_ring_conf_wire`'s PERIOD axis therefore has exactly one live
# job: converging a conf.d that has drifted OFF ``RING_SLOT_FRAMES`` (a hand
# edit, a half install) back onto it. It refuses any other target. Its format
# and channels axes are per-box and carry no such fixed target.
# (scripts/ring-proto/arm.sh
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
# Per-block field parsing. The conf.d has TWO PCM blocks (Ring A and Ring B),
# and since ring v2 they can declare DIFFERENT geometry: Ring B's ``channels``
# follows the box's topology while Ring A's is always the stereo program. So
# every field parser here is scoped to one named block; a whole-file scan would
# collapse two legitimately different values into "torn".
#
# ``_ring_conf_block_body`` finds that block by MATCHING BRACES rather than by
# regex. A `[^}]*` body (what this used before the per-block fields landed)
# terminates at the FIRST `}`, so any nested block — ALSA's own ``hint { … }``
# convention is the obvious one — would truncate the body and hide every field
# after it. Quoted values are skipped so a brace inside ``path "…"`` cannot
# unbalance the scan.
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
    that wire, and reporting "indeterminate" for the shipped file would fail
    every guard on every box.
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

    ``pcm_name`` is ``jts_ring_capture`` (Ring A) or ``jts_ring_playback`` (Ring
    B). Returns the single ``n_slots`` value that block declares, or ``None`` when
    the file is absent/unreadable, the block is missing, or the block declares no
    single ``n_slots`` (a torn conf.d — the caller treats that as a mismatch, not
    a silent pick). Pure text parse, no ALSA. ``conf_d=None`` resolves
    :data:`RING_CONF_D` at CALL time (not a bound default) so a test/caller that
    repoints the module constant is honored.
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
    :data:`RING_CONF_DEFAULT_FORMAT`, so an unmodified shipped conf.d declares
    that wire even though the token appears nowhere in it.
    """
    return _single_block_value(
        _RING_CONF_FORMAT_RE, pcm_name, conf_d, absent=RING_CONF_DEFAULT_FORMAT
    )


@dataclass(frozen=True)
class RingConfWireRender:
    """The outcome of rendering the ring conf.d wire for one box.

    ``changed`` is False for the no-write outcome — the conf already declares the
    target wire, which is the golden case on a box running the shipped geometry
    (an Apple box whose declared floor equals the shipped 128, on the shipped
    S16_LE / 2-channel wire).

    ``previous_period_frames`` is ``None`` for a TORN conf.d whose two PCM blocks
    disagreed, because there was no single previous value to report.
    """

    changed: bool
    period_frames: int
    previous_period_frames: int | None
    sample_format: str
    ring_a_channels: int
    ring_b_channels: int
    conf_d: str


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
    to the explicit default converges just as exactly, and a substitution cannot
    disturb a line the deletion path would have to find the boundaries of.
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
    """Rewrite the ring conf.d so both PCM blocks declare ``wire``.

    ``wire`` is a :class:`~jasper.fanin_coupling.RingWire` — the ONE per-box
    resolution of the ring's geometry. Taking the resolved object rather than
    four loose scalars is deliberate: the four ends of the ring must declare the
    same tuple, and a call site that could pass a format from one resolution and
    a channel count from another is exactly the shear this rung exists to close.

    What lands where:

    - ``period_frames`` — both blocks, one shared value (the ring slot IS one
      outputd DAC period). This is the per-box render the conf.d's own header
      calls for; the CALLER decides whether a render is warranted (the rule is
      "only from a DECLARED :class:`~jasper.audio_hardware.dac.LatencyFloor`"),
      so this function never consults the DAC registry itself.
    - ``format`` — both blocks, one shared value. The rings carry one wire
      format; a box with two would need every emitter, gate and doctor surface
      to carry two forever.
    - ``channels`` — PER BLOCK. ``jts_ring_capture`` (Ring A) declares
      ``ring_a_channels``: everything upstream of CamillaDSP is a stereo
      program, and fan-in's mixer is stereo. ``jts_ring_playback`` (Ring B)
      declares ``ring_b_channels``, which follows the box's output topology.
      This is the one axis on which the two rings can legitimately differ, which
      is why the parsers above are block-scoped.

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

    **Write-on-change only.** When the conf already declares exactly this wire
    the file is left GENUINELY untouched — no rewrite, no mtime churn — so a box
    that renders to the shipped values is byte-identical to one that never
    rendered. Because an omitted ``format``/``channels`` key already declares
    the ioplug's default (:data:`RING_CONF_DEFAULT_FORMAT` /
    :data:`RING_CONF_DEFAULT_CHANNELS`), a box on the shipped wire never gains a
    line either. Otherwise the whole file is published through
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

    rendered = _RING_CONF_PERIOD_RE.sub(
        lambda m: f"{m.group('indent')}period_frames {period_frames}",
        text,
    )
    for pcm_name, channels in (
        (RING_A_CONF_PCM, ring_a_channels),
        (RING_B_CONF_PCM, ring_b_channels),
    ):
        # Re-find the span each pass: rendering Ring A's body moves Ring B's.
        span = _ring_conf_block_body_span(rendered, pcm_name)
        if span is None:
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
        # A no-op render (nothing anywhere in the file changed) can only happen
        # when every period_frames line already read `period_frames`: the
        # substitution below rewrites EVERY matched line to that one target
        # value, so a torn `distinct` (2+ original values, or a single value
        # that differs from the target) would always change at least one line.
        # `distinct == {period_frames}` is therefore guaranteed here, not a
        # second condition to re-check.
        return RingConfWireRender(
            changed=False,
            period_frames=period_frames,
            previous_period_frames=period_frames,
            sample_format=sample_format,
            ring_a_channels=ring_a_channels,
            ring_b_channels=ring_b_channels,
            conf_d=path,
        )
    # Function-local so the module keeps its stdlib-only import cost for the
    # presence/parse callers (the coupling reconciler and the socket-activated
    # web surfaces); only the renderer pays for atomic_io. preserve_target_stat
    # carries the installed file's uid/gid/mode across the replace, so the 0644
    # renderer-user resolvability the conf.d header depends on survives, and a
    # root-run reconcile does not re-own a file it did not create.
    from jasper.atomic_io import atomic_write_text

    atomic_write_text(path, rendered, preserve_target_stat=True)
    return RingConfWireRender(
        changed=True,
        period_frames=period_frames,
        previous_period_frames=previous[0] if len(distinct) == 1 else None,
        sample_format=sample_format,
        ring_a_channels=ring_a_channels,
        ring_b_channels=ring_b_channels,
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

    ONE AXIS: ``period_frames``, and it vouches for nothing else. The WIRE axes
    (``sample_format`` / ``channels``) across the ring's declaring ends belong to
    ``jasper.fanin.coupling_reconcile.ring_edge_width_ready``, and the on-disk
    header's agreement with the conf.d belongs to
    :func:`ring_header_matches_conf`. Restating either here would make two
    answers for one fact; the arm runs all three.
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


# The ring SHM header layout — the u32 prefix of rust/jasper-ring/src/layout.rs,
# duplicated here (Python has no way to link the Rust const). The golden layout
# test in the Rust crate is the offset SSOT, and ``test_ring_assets`` pins these
# against it.
#
# ALL SIX declared geometry fields are read, not just the two the slot-count
# guard needs. ``rate``/``channels``/``sample_format`` have been real header
# fields since v1 and the Rust/C attach compares every one of them field-by-
# field, so a Python guard that reads only ``period_frames``/``n_slots`` cannot
# see a file that shears on the other three — it would report a coherent ring
# where the ioplug attach will fail.
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
    the v1 layout's, so a file announcing another version may put entirely
    different meanings at them and its "geometry" would be fiction. ``version``
    is still reported so a caller can name what it saw.
    """

    valid: bool
    magic: int = 0
    version: int = 0
    rate: int = 0
    channels: int = 0
    sample_format: int = 0
    period_frames: int = 0
    n_slots: int = 0

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

    THE HOLE THIS CLOSES. The header has carried ``rate``/``channels``/
    ``sample_format`` since v1 and the Rust and C attach paths compare every one
    of them field-by-field — but every Python guard compared only ``n_slots``
    and ``period_frames``. A ring file whose slots and period match while its
    FORMAT or CHANNEL COUNT does not therefore passed every coherence check we
    had, and the first thing to notice would have been the ioplug failing the
    attach at arm. Comparing here is the difference between a shear that is
    cleared before it can bite and one that crash-loops CamillaDSP.

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

    ONE AXIS: ``n_slots``, and it vouches for nothing else. The WIRE axes
    (``sample_format`` / ``channels``) across the ring's declaring ends belong to
    ``jasper.fanin.coupling_reconcile.ring_edge_width_ready``, and the on-disk
    header's agreement with the conf.d belongs to
    :func:`ring_header_matches_conf`. Restating either here would make two
    answers for one fact; the arm runs all three.
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
