# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the grouping-ingress ring platform (#2508, PR-1).

The platform is three files that ship together and open nothing:
``deploy/alsa/conf.d/62-jts-ring-grouping.conf`` declares the PCM,
:mod:`jasper.multiroom.grouping_ring` owns the same identity in Python, and
``deploy/lib/install/ring-platform.sh`` places the conf.d on every box.

Three contracts, in rising order of what they cost to get wrong:

**T-1 — the conf.d is a block the ioplug can parse.** This is an AVAILABILITY
guard, not tidiness. ``/etc/alsa/conf.d`` is parsed by alsa-lib on EVERY PCM
open on the box, so a malformed block here would break every renderer on every
box in the fleet — including the boxes that never group. The accepted key set
and the ``n_slots`` bounds are read out of the C source rather than restated,
so the guard cannot claim the plugin accepts something it refuses.

**T-2 — the geometry and the wire have one source of truth.** Three
declarations of the same facts exist by necessity (an ALSA conf file, a Python
module, and the snapcast stream's pinned sample format), in three languages
that cannot import each other. Nothing but this test ties them together, and a
disagreement is silent in the worst direction: a conf.d that declares a
different wire from the one snapclient decodes to fails negotiation at open,
because the shipped ring PCMs are raw ``type jts_ring`` rather than
``plug``-wrapped.

**T-3 — the installer places it, and the deploy does not unlink its ring
file.** The second half is the asymmetry with the coupling's three ring files
and is deliberate; the reason is in that test's docstring.

**T-6 — the writer's unit adopts the ring-writer contract.** Once snapclient
writes this ring it inherits the platform's cadence rules: a restart slower
than the writer-liveness window, a start limit a burst of refusals cannot
reach, and the umask that leaves the ring file group-writable for its reader.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper import renderer_lanes, ring_assets
from jasper.fanin_coupling import RING_SLOT_FRAMES
from jasper.multiroom.grouping_ring import (
    GROUPING_RING_CHANNELS,
    GROUPING_RING_CONF_D,
    GROUPING_RING_FILE,
    GROUPING_RING_FORMAT,
    GROUPING_RING_PCM,
    GROUPING_RING_PERIOD_FRAMES,
    GROUPING_RING_SLOTS,
    GROUPING_RING_WRITER_LOCK,
)

_REPO = Path(__file__).resolve().parents[1]
_GROUPING_CONF = _REPO / "deploy" / "alsa" / "conf.d" / "62-jts-ring-grouping.conf"
_IOPLUG_C = _REPO / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"
_IOPLUG_H = _REPO / "c" / "jts-ring-ioplug" / "jts_ring_shm.h"
_RING_PLATFORM_SH = _REPO / "deploy" / "lib" / "install" / "ring-platform.sh"
_SNAPCLIENT_UNIT = _REPO / "deploy" / "systemd" / "jasper-snapclient.service"


def _read(path: Path) -> str:
    assert path.exists(), f"source not present: {path}"
    return path.read_text(encoding="utf-8")


def _c_define(name: str, text: str) -> int:
    """One ``#define <name> <int>[u]`` from a C header, or a loud failure."""
    m = re.search(rf"#define\s+{re.escape(name)}\s+(\d+)u?\b", text)
    assert m is not None, f"could not find #define {name}"
    return int(m.group(1))


# --- T-1: the conf.d is a block the ioplug can parse ------------------------


def test_the_grouping_confd_declares_exactly_one_pcm_and_it_is_the_grouping_ring():
    """One file, one block, and its name is the one Python hands consumers.

    A second block here would be a second geometry nobody derived, and a
    misspelled name would resolve to nothing at open — the ioplug never sees
    the request at all, so the failure surfaces as ALSA's generic "No such
    device" rather than anything naming this file.
    """
    conf = _read(_GROUPING_CONF)
    declared = re.findall(r"^\s*pcm\.([A-Za-z0-9_]+)\s*\{", conf, re.MULTILINE)
    assert declared == [GROUPING_RING_PCM], (
        f"{_GROUPING_CONF.name} must declare exactly pcm.{GROUPING_RING_PCM}; "
        f"found {declared}"
    )
    # The conf.d path this file installs to is the one Python names.
    assert GROUPING_RING_CONF_D == f"/etc/alsa/conf.d/{_GROUPING_CONF.name}"


def _strip_conf_comments(text: str) -> str:
    """Drop ``#``-to-end-of-line comments that are not inside a quoted string.

    Needed because the structural counts below must see the DECLARATION, not
    the header prose: this file's comments legitimately contain braces and
    apostrophes. The scanner tracks double quotes as it walks, so a ``#`` inside
    ``path "…"`` is kept.
    """
    out: list[str] = []
    in_quote = False
    in_comment = False
    for ch in text:
        if ch == "\n":
            in_comment = False
            out.append(ch)
            continue
        if in_comment:
            continue
        if ch == '"':
            in_quote = not in_quote
        elif ch == "#" and not in_quote:
            in_comment = True
            continue
        out.append(ch)
    return "".join(out)


@pytest.mark.parametrize(
    ("path", "expected", "expected_depth"),
    [
        (
            _REPO / "deploy/alsa/conf.d/60-jts-ring.conf",
            ring_assets.RING_CONF_PCMS,
            1,
        ),
        (
            _REPO / "deploy/alsa/conf.d/61-jts-renderer-lanes.conf",
            tuple(
                pcm
                for lane in renderer_lanes.RENDERER_LANES
                for pcm in (
                    renderer_lanes.ring_conf_pcm_name(lane.label), lane.ring_device,
                )
            ),
            2,
        ),
        (_GROUPING_CONF, (GROUPING_RING_PCM,), 1),
    ],
)
def test_ring_confd_files_have_only_expected_top_level_blocks(
    path, expected, expected_depth,
):
    """Every non-comment top-level token is one complete PCM declaration.

    Value parsers see only block bodies, so they cannot catch a stray brace,
    token, or alias outside every block. Any such text can break every ALSA
    open or override an earlier conf.d declaration.
    """
    stripped = _strip_conf_comments(_read(path))
    assert stripped.count('"') % 2 == 0, f"unterminated string in {path.name}"
    structural = re.sub(r'"[^"\n]*"', '""', stripped)
    depth = 0
    max_depth = 0
    declared = []
    for raw in structural.splitlines():
        line = raw.strip()
        if not line:
            continue
        before = depth
        if depth == 0:
            match = re.fullmatch(r"pcm\.([A-Za-z0-9_]+)\s*\{", line)
            assert match is not None, f"unexpected top-level text in {path.name}: {line!r}"
            declared.append(match.group(1))
        depth += line.count("{") - line.count("}")
        max_depth = max(max_depth, depth)
        assert depth >= 0, f"stray closing brace in {path.name}"
        if before > 0 and depth == 0:
            assert line == "}", f"text follows a block in {path.name}: {line!r}"
    assert depth == 0, f"unclosed block in {path.name}"
    assert max_depth == expected_depth, f"unexpected block nesting in {path.name}"
    assert tuple(declared) == expected


def test_the_grouping_confd_uses_only_keys_the_ioplug_accepts():
    """Every key in the block is one the C parser handles, derived from the C.

    The ioplug REFUSES an unknown field with -EINVAL ("jts_ring: unknown field
    %s"), and that refusal is deliberate — it is what makes a deploy-skew
    mismatch fail at open instead of on the wire. So a key this file invents is
    not a typo that degrades, it is a PCM that cannot open.

    The accepted set is read off ``pcm_jts_ring.c``'s own ``strcmp(id, …)``
    calls rather than restated here: the skipped-but-legal keys (``type``,
    ``comment``, ``hint``) and the parsed ones come from the same loop, so a
    plugin that learns or loses a field moves this guard with it.
    """
    accepted = set(re.findall(r'strcmp\(id,\s*"([A-Za-z0-9_]+)"\)', _read(_IOPLUG_C)))
    assert {"path", "period_frames", "n_slots", "format", "channels", "type"} <= accepted, (
        "the ioplug key scan found an implausibly small set — the parse loop's "
        f"shape has changed and this derivation needs re-deriving: {sorted(accepted)}"
    )

    conf = _read(_GROUPING_CONF)
    body = conf[conf.index("{") + 1 : conf.rindex("}")]
    used = {
        m.group("key")
        for m in re.finditer(
            r"^\s*(?P<key>[A-Za-z0-9_]+)\s+\S", body, re.MULTILINE
        )
    }
    assert used <= accepted, (
        f"{_GROUPING_CONF.name} declares key(s) the ioplug refuses at open with "
        f"-EINVAL: {sorted(used - accepted)}. Accepted: {sorted(accepted)}"
    )
    # `type jts_ring` is what resolves the block to the plugin at all.
    assert re.search(r"^\s*type\s+jts_ring\s*$", body, re.MULTILINE), (
        f"{_GROUPING_CONF.name} must declare `type jts_ring`"
    )


def test_the_grouping_ring_slot_count_is_inside_the_plugins_bounds():
    """``n_slots`` is range-checked by the C at open, so CI checks it first.

    The equality against the CEILING is asserted on purpose and is not the same
    claim as the range: the design took the deepest geometry the plugin allows,
    which means depth is not tunable upward from a conf.d edit. If
    JTS_RING_MAX_SLOTS ever moves, this line should be re-decided deliberately
    rather than drifting along with it.
    """
    header = _read(_IOPLUG_H)
    min_slots = _c_define("JTS_RING_MIN_SLOTS", header)
    max_slots = _c_define("JTS_RING_MAX_SLOTS", header)
    assert min_slots <= GROUPING_RING_SLOTS <= max_slots, (
        f"n_slots={GROUPING_RING_SLOTS} is outside the ioplug's "
        f"{min_slots}..{max_slots} — the PCM would fail to open with -EINVAL"
    )
    assert GROUPING_RING_SLOTS == max_slots, (
        f"the grouping ring is specified at the plugin's slot ceiling "
        f"({max_slots}); it now reads {GROUPING_RING_SLOTS}. Raising the C "
        "ceiling does not automatically mean this ring should get deeper — "
        "re-decide it (design §3.2) and move this line in the same commit."
    )


# --- T-2: one source of truth for the geometry and the wire -----------------


def _confd_field(key: str) -> str:
    """The single value the grouping block declares for ``key``.

    Deliberately reads the SHIPPED file with the ring platform's own block
    parser rather than a private regex, so "how a jts_ring block is read" keeps
    one owner (:mod:`jasper.ring_assets`, which the conf.d renderer and every
    doctor check already share).
    """
    from jasper import ring_assets

    readers = {
        "period_frames": lambda: ring_assets.ring_conf_period_frames(str(_GROUPING_CONF)),
        "n_slots": lambda: ring_assets.ring_conf_n_slots(
            GROUPING_RING_PCM, str(_GROUPING_CONF)
        ),
        "channels": lambda: ring_assets.ring_conf_channels(
            GROUPING_RING_PCM, str(_GROUPING_CONF)
        ),
        "format": lambda: ring_assets.ring_conf_format(
            GROUPING_RING_PCM, str(_GROUPING_CONF)
        ),
    }
    value = readers[key]()
    assert value is not None, (
        f"{_GROUPING_CONF.name} declares no single `{key}` for "
        f"pcm.{GROUPING_RING_PCM} — absent, unreadable, or torn"
    )
    return str(value)


@pytest.mark.parametrize(
    ("key", "constant"),
    [
        ("period_frames", GROUPING_RING_PERIOD_FRAMES),
        ("n_slots", GROUPING_RING_SLOTS),
        ("channels", GROUPING_RING_CHANNELS),
        ("format", GROUPING_RING_FORMAT),
    ],
)
def test_the_confd_block_and_the_python_constants_agree(key, constant):
    """The ALSA file and :mod:`jasper.multiroom.grouping_ring` are one fact.

    Two languages, no import between them. The writer resolves the geometry
    through the conf.d (ALSA hands the ioplug the parsed block) and every
    Python consumer resolves it through the module, so a drift means the two
    ends of one transport disagree about the same ring — which the ioplug
    reports as a hard -EINVAL at attach against an existing header, and which
    nothing reports at all before the ring exists.
    """
    assert _confd_field(key) == str(constant), (
        f"{_GROUPING_CONF.name}'s `{key}` and grouping_ring.py disagree: "
        f"conf.d={_confd_field(key)!r} vs constant={constant!r}"
    )


def test_only_the_rings_snapclient_writes_ask_to_be_paced():
    """``pace_nominal 1`` is here, and nowhere a clock-carrying writer writes.

    The field opts a PCM's PLAYBACK direction into the ioplug's rate limiter
    (``jts_ring_pace_apply``). The scoping rule is about the WRITER, not the
    ring: every other ring's writer already carries a clock, and the limiter's
    whole reason to exist is the one writer that does not — snapclient, whose
    ALSA player expects the device to pace it. Two ways that scoping breaks
    silently — the field being dropped from a file that needs it (the storm
    comes back with nothing to say so), and the field spreading to a ring that
    never needed it (a rate bound on a path nobody measured) — so both
    directions are pinned.

    The DAC-content return ring (``63-``, #3118) joined this set when it landed:
    same writer, same missing clock. That it is read by DAC-paced outputd rather
    than by CamillaDSP does not exempt it — the governor is a floor under the
    reader's failure, not a substitute for the reader's clock. A THIRD file
    appearing here should be re-decided, not waved through: the question is only
    ever "does this block's writer carry its own clock?".
    """
    conf = _read(_GROUPING_CONF)
    body = conf[conf.index("{") + 1 : conf.rindex("}")]
    assert re.search(r"^\s*pace_nominal\s+1\s*$", body, re.MULTILINE), (
        f"{_GROUPING_CONF.name} must declare `pace_nominal 1` — without it the "
        "grouping ring's writer is unpaced against a stalled or dead reader"
    )

    conf_dir = _GROUPING_CONF.parent
    declaring = sorted(
        path.name
        for path in conf_dir.glob("*.conf")
        if re.search(
            r"^\s*pace_nominal\s+\S", _strip_conf_comments(_read(path)), re.MULTILINE
        )
    )
    assert declaring == [
        _GROUPING_CONF.name,
        "63-jts-ring-dac-content.conf",
    ], (
        "pace_nominal is scoped to the rings snapclient writes; the conf.d "
        f"files declaring it are now: {declaring}"
    )


def test_the_governor_is_armed_from_prepare_and_not_from_start():
    """The pace bucket is armed in the PREPARE callback, never in START.

    A PCM can transfer while still PREPARED — with ``start_threshold > period``
    and a dead reader, ALSA's start condition never trips against the ioplug's
    dead-reader avail discount, so the stream may never leave PREPARED at all.
    ``jts_ring_pace_apply`` early-returns on an unarmed bucket, so arming at
    ``start`` left that whole window ungoverned free-run. The header seam
    (``jts_ring_pointer_prepare``) has its own host test; this pins the half a
    host test cannot see, which is which callback the plugin wires it into.
    """
    c = _read(_IOPLUG_C)

    def _body(fn: str) -> str:
        m = re.search(rf"^static int {re.escape(fn)}\(.*?^}}", c, re.MULTILINE | re.DOTALL)
        assert m is not None, f"could not find {fn} in {_IOPLUG_C.name}"
        return m.group(0)

    prepare = _body("jts_ring_prepare")
    start = _body("jts_ring_start")
    assert "jts_ring_pointer_prepare(" in prepare, (
        "the playback prepare callback must arm the governor via "
        "jts_ring_pointer_prepare; without it a PREPARED writer free-runs"
    )
    for fn in ("jts_ring_pace_arm(", "jts_ring_pointer_prepare("):
        assert fn not in start, (
            f"{fn} must not be called from jts_ring_start — arming there is the "
            "defect this pin exists to catch (a PREPARED stream never reaches it)"
        )

    # The bind/release ledger is DERIVED from pace_bound_ns, which the arming line
    # above zeroes, so the log state has to be cleared in the same callback. A
    # prepare while a bind is standing is ordinary XRUN recovery against a dead
    # reader; leaving these behind makes the next call emit a release attributed to
    # the previous incarnation, with a delta underflowed through zero. Both fields
    # live in the .c, so this is the half the host suite cannot see.
    assert re.search(r"memset\(&p->pace_log,\s*0,\s*sizeof\(p->pace_log\)\)", prepare), (
        "jts_ring_prepare must clear p->pace_log — a stale ledger turns the "
        "pace_bound_ns reset into a phantom release"
    )
    assert re.search(r"p->pace_bind_bound_ns\s*=\s*0", prepare), (
        "jts_ring_prepare must clear p->pace_bind_bound_ns — a stale anchor makes "
        "the next delta underflow"
    )


def test_the_grouping_ring_slot_is_one_ring_slot():
    """The chunk this ring's period reads is itself the box's ring slot.

    ``GROUPING_RING_PERIOD_FRAMES`` reads
    :data:`jasper.fanin_coupling.RING_CAMILLA_CHUNKSIZE`, and that chunk is a
    separate declaration whose whole justification is that one chunk is one
    :data:`jasper.fanin_coupling.RING_SLOT_FRAMES` slot — a link the two have
    only by value. Moving the slot without moving the chunk would leave this
    ring's geometry derived for a chunk that spans a different number of slots,
    which the emitter cannot see.
    """
    assert GROUPING_RING_PERIOD_FRAMES == RING_SLOT_FRAMES, (
        f"the grouping ring's slot ({GROUPING_RING_PERIOD_FRAMES}, the ring "
        f"path's CamillaDSP chunk) and the ring slot ({RING_SLOT_FRAMES}) have "
        "to be one number; changing either alone re-introduces a chunk that "
        "spans a different number of slots than the geometry was derived for"
    )


def test_the_grouping_ring_wire_is_the_snapcast_streams_wire():
    """The ring carries what snapclient decodes to — asserted from the ARGV.

    snapserver pins ``sampleformat=48000:16:2`` on its pipe source, so a
    snapclient writing this ring emits 16-bit stereo. The ring PCM is raw
    ``type jts_ring`` with single-valued hw_params and no ``plug`` wrapper, so
    there is nothing in between to absorb a mismatch: a widened ring would fail
    negotiation rather than convert.

    Read off ``snapserver_argv``'s emitted command line rather than the module
    source, so the pin follows the value actually shipped to snapserver.
    """
    from jasper.multiroom.config import GroupingConfig
    from jasper.multiroom.reconcile import snapserver_argv

    cfg = GroupingConfig(
        enabled=True,
        role="leader",
        channel="stereo",
        bond_id="pin",
        leader_addr="",
        buffer_ms=400,
        codec="flac",
        error=None,
    )
    argv = snapserver_argv(cfg)
    m = re.search(r"sampleformat=(\d+):(\d+):(\d+)", " ".join(argv))
    assert m is not None, f"snapserver_argv pins no sampleformat: {argv}"
    _rate, stream_bits, stream_channels = (int(g) for g in m.groups())

    ring_bits = re.fullmatch(r"S(\d+)_LE", GROUPING_RING_FORMAT)
    assert ring_bits is not None, (
        f"GROUPING_RING_FORMAT={GROUPING_RING_FORMAT!r} is not an S<bits>_LE token"
    )
    assert int(ring_bits.group(1)) == stream_bits, (
        f"the grouping ring is {GROUPING_RING_FORMAT} but snapserver streams "
        f"{stream_bits}-bit — snapclient's ALSA write would not negotiate"
    )
    assert GROUPING_RING_CHANNELS == stream_channels, (
        f"the grouping ring is {GROUPING_RING_CHANNELS}ch but snapserver streams "
        f"{stream_channels}ch"
    )


def test_the_writer_lock_path_is_derived_from_the_platforms_own_rule():
    """One suffix rule, one owner.

    The lock is what makes a second writer's open fail loudly with -EBUSY, and
    its identity is the PATHNAME — so a second spelling of the suffix here
    would let two writers lock two different files and proceed silently.
    :func:`jasper.ring_assets.ring_writer_lock_path` is already pinned against
    the C header; this asserts the grouping ring goes through it rather than
    around it.
    """
    from jasper.ring_assets import ring_writer_lock_path

    assert GROUPING_RING_WRITER_LOCK == ring_writer_lock_path(GROUPING_RING_FILE)
    assert GROUPING_RING_WRITER_LOCK != GROUPING_RING_FILE


def test_the_confd_path_key_is_the_ring_file_python_names():
    conf = _read(_GROUPING_CONF)
    m = re.search(r'^\s*path\s+"([^"]+)"\s*$', conf, re.MULTILINE)
    assert m is not None, f"{_GROUPING_CONF.name} declares no quoted `path`"
    assert m.group(1) == GROUPING_RING_FILE


def test_the_doctor_stall_check_judges_the_grouping_ring(monkeypatch, tmp_path):
    """The grouping ring is in ``check_ring_reader_stall``'s hand-kept tuple.

    It belongs there by the check's own stated reason: the alarm exists for a
    ring whose writer is the **C ioplug**, whose drop counters are process-local
    fields printed at close and therefore invisible to any reader. The grouping
    ring is exactly that shape — snapclient writes it through the same plugin —
    so without an entry a bonded endpoint whose CamillaDSP wedged would drop
    audio with nothing reporting it.

    **Inert until the file exists**, which is why it can land now rather than
    with the transport: ``ring_stall_verdict`` answers ``present=False`` for an
    absent file (asserted below against a path that does not exist), and the
    check ``continue``s on every non-present ring. So on every box in the fleet
    this entry costs one stat and changes no verdict, and it starts judging by
    itself on the box where something creates the ring.

    Pinned by EXECUTION rather than by scanning the source: the paths the check
    actually asks about are recorded, so an entry deleted or respelled fails
    here.
    """
    from jasper import ring_assets
    from jasper.cli import doctor

    # An absent file is not present -> the check skips it. The inertness claim.
    assert ring_assets.ring_stall_verdict(str(tmp_path / "absent.ring")).present is False

    asked: list[str] = []

    def _spy(path: str, **kwargs: object) -> ring_assets.RingStallVerdict:
        asked.append(path)
        return ring_assets.RingStallVerdict(present=False, detail="absent (test)")

    monkeypatch.setattr(ring_assets, "ring_stall_verdict", _spy)
    result = doctor.audio_runtime_ring.check_ring_reader_stall()

    assert GROUPING_RING_FILE in asked, (
        "check_ring_reader_stall must judge the grouping ring — its tuple is "
        f"hand-kept, and it only asked about {asked}"
    )
    # Every ring absent => the check stays quiet, which is the fleet's state.
    assert result.status == "ok"


# --- T-3: install coverage, and the deliberate rm -f asymmetry --------------


def test_the_installer_ships_the_grouping_confd():
    """Placed by the same helper as its two siblings, at the same mode.

    0644 is load-bearing (AGENTS.md, the PR #214 class): a definition only root
    can read is a name the non-root renderer users cannot resolve. Asserting
    the install LINE rather than a doctor presence check is the sibling
    precedent — ``61-jts-renderer-lanes.conf`` is covered exactly this way, and
    :func:`jasper.ring_assets.ring_asset_presence` deliberately stays scoped to
    the coupling's own conf.d, because it is the shm_ring ACTIVATION gate: a
    missing grouping conf.d must not refuse the fan-in coupling's arm.
    """
    platform = _read(_RING_PLATFORM_SH)
    assert "62-jts-ring-grouping.conf" in platform
    assert re.search(
        r'install -m 0644 "\$\{grouping_src\}" '
        r"/etc/alsa/conf\.d/62-jts-ring-grouping\.conf",
        platform,
    ), (
        "the grouping ring PCM must be installed system-wide at 0644 so any "
        "user can resolve the name"
    )


def test_the_deploy_does_not_unlink_the_grouping_ring_file():
    """``grouping.ring`` is deliberately absent from install's ``rm -f`` list.

    The reason is a FAILURE-ESCALATION ASYMMETRY between the two ends' units,
    not a difference in which daemon is running at that instant:

    * A stale-geometry ring on the coupling path is a fatal attach for
      ``jasper-fanin``, whose unit carries ``StartLimitBurst=5`` and
      ``StartLimitAction=reboot`` — it reboots the box mid-install, before
      ``write_build_manifest``. That is the trap
      ``tests/test_install_ring_platform_sequencing.py`` documents, and it makes
      unlinking those three MANDATORY.
    * ``jasper-snapclient.service`` carries ``StartLimitBurst=6`` and NO
      ``StartLimitAction``, by explicit design — its own unit comment reads
      *"follower degrades, visible; never reboots the household."* A stale
      ``grouping.ring`` costs six retries and one ``failed`` unit, surfaced on
      ``/state`` and by ``jasper-doctor``. Unlinking buys nothing against an
      outcome that is already bounded and already visible.

    **Install ORDER is not the reason**, though it is real and is pinned
    mechanically by ``test_install_ring_platform_sequencing``. Order does not
    separate the two cases: the three rings above also have live writers at that
    instant, and the park set that stops snapclient has a third call site
    (``park_low_memory_build_units``, gated on a low-memory box) that runs before
    the ring-platform step — so no statement about who is parked there holds on
    every box. The escalation asymmetry holds in every ordering.

    Scoped WIDER than the sibling in
    ``test_install_ring_platform_sequencing``, deliberately: that one reads
    ``install_jts_ring_platform``'s body, this one scans the whole file, so an
    ``rm -f`` added anywhere in ``ring-platform.sh`` is caught too.

    Grouping-ring design §3.4.
    """
    from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_FILE
    from jasper.ring_assets import (
        RING_A_PROGRAM_FILE,
        RING_ACTIVE_CONTENT_FILE,
        RING_B_CONTENT_FILE,
    )

    platform = _read(_RING_PLATFORM_SH)
    removed = set(re.findall(r"^\s*rm -f (/dev/shm/jts-ring/\S+)$", platform, re.MULTILINE))
    assert removed == {
        RING_A_PROGRAM_FILE,
        RING_B_CONTENT_FILE,
        RING_ACTIVE_CONTENT_FILE,
        # The DAC-content return ring (#3118) is the case that proves the rule
        # is about ESCALATION and not about snapclient: snapclient writes it
        # too, but its READER is outputd (StartLimitAction=reboot), so a stale
        # header reboots the household. Its own membership pin, and the
        # escalation premise it rests on, are in
        # tests/test_dac_content_ring_platform.py.
        DAC_CONTENT_RING_FILE,
        # The renderer-ingress lanes are the case on the OTHER side of the
        # escalation line that is still unlinked: a stale lane ring detaches
        # fan-in and fails the renderer's own open, which is silent and
        # permanent rather than bounded and visible. Membership pin in
        # tests/test_install_ring_platform_sequencing.py.
        f"{renderer_lanes.RING_SHM_DIR}/{renderer_lanes.RENDERER_RING_PREFIX}*.ring",
    }, f"the deploy-time ring rm -f set changed: {sorted(removed)}"
    assert GROUPING_RING_FILE not in removed
    # The asymmetry is stated where the lines are, not only here.
    assert "grouping.ring" in platform, (
        "the rm -f block must name grouping.ring's deliberate absence, or the "
        "next reader adds it as an oversight"
    )


# --- T-6: the writer's unit adopts the ring-writer contract -----------------


def test_snapclient_restarts_slower_than_the_ring_liveness_window():
    """The grouping ring's WRITER must clear the ring's own liveness window.

    A SIGKILLed writer's flock drops with its fd, but it leaves ``writer_pid``
    stamped and its heartbeat frozen-fresh, so the secondary guard refuses a new
    writer for up to that window. snapclient shipped at ``RestartSec=2s`` — ON
    the boundary — which is a respawn racing its own predecessor into an
    avoidable ``-EBUSY``.

    The bound is READ FROM THE C CONSTANT that owns it, through the same parse
    the renderer-lane pin uses. A bespoke "2 s" here would be a second owner of
    a number the header decides, and the two would drift the first time it
    moved.
    """
    from tests.test_renderer_ring_lanes import _ring_liveness_window_sec

    text = _read(_SNAPCLIENT_UNIT)
    m = re.search(r"^RestartSec=(\d+(?:\.\d+)?)s?$", text, re.M)
    assert m, "jasper-snapclient.service declares no RestartSec"
    window = _ring_liveness_window_sec()
    assert float(m.group(1)) > window, (
        f"jasper-snapclient.service restarts in {m.group(1)}s but a killed ring "
        f"writer stays refused for up to {window}s"
    )


def test_snapclient_tolerates_a_burst_of_ring_refusals():
    """RestartSec alone does not save the unit: systemd's default limit (5 starts
    in 10 s) is what turns a transient refusal loop into a PARKED unit, and a
    parked snapclient is a bonded endpoint that stays silent until an operator
    intervenes.

    The same ``> 5`` bar every ring-writing renderer satisfies
    (``test_every_ring_writing_renderer_tolerates_a_burst_of_refusals``). This
    unit sat at 4 — below even systemd's own default — because it was written
    before it wrote a ring. Raising it PRESERVES the unit's stated anti-storm
    intent: still bounded, still ``StartLimitAction`` unset, so a follower whose
    leader is powered off still degrades visibly instead of rebooting.
    """
    text = _read(_SNAPCLIENT_UNIT)
    burst = re.search(r"^StartLimitBurst=(\d+)$", text, re.M)
    interval = re.search(r"^StartLimitIntervalSec=(\d+)$", text, re.M)
    assert burst and interval, "jasper-snapclient.service declares no start limit"
    assert int(burst.group(1)) > 5, (
        f"StartLimitBurst={burst.group(1)} is no better than systemd's default"
    )
    # A DIRECTIVE line, not a substring: the unit's own comment explains the
    # default in prose, and a substring test would read that as a declaration.
    assert re.search(r"^StartLimitAction=", text, re.M) is None, (
        "a follower degrades, visible; it never reboots the household"
    )


def test_snapclient_creates_the_ring_group_writable():
    """``UMask=0007``, because whichever end opens first CREATES the ring file.

    ``ring_mapping_open`` creates at 0660, and the process umask masks it:
    systemd's default 0022 lands 0640 — group-READABLE but not group-WRITABLE —
    while BOTH ends of a ring write its header (the reader stamps ``read_seq``
    and its heartbeat). The setgid directory fixes the file's GROUP; only the
    umask fixes its MODE.

    Scoped to the unit this transport adds. ``deploy/tmpfiles/jts-ring.conf``
    states the rule for every ring writer and the rule is not currently exact —
    ``jasper-camilla.service`` writes Ring B and carries no ``UMask`` anywhere
    in its own file. That is a real gap in an AXIS-1 unit, recorded rather than
    fixed here, and this test deliberately does not assert the fleet-wide claim.
    The same unit is also THIS ring's READER end, and neither camilla unit
    carries ``UMask`` either — latent only, because every participant on this
    ring runs as root today, so no open takes EACCES on a 0640 file. Recorded
    with the gap above; it moves with whoever fixes that one.
    """
    assert re.search(r"^UMask=0007$", _read(_SNAPCLIENT_UNIT), re.M), (
        "jasper-snapclient.service writes a ring and must set UMask=0007"
    )
