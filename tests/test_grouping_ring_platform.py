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
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.fanin_coupling import RING_CAMILLA_CHUNKSIZE
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


def test_the_grouping_ring_slot_is_one_camilladsp_chunk():
    """One slot per chunk — the relationship every other ring in the tree ships.

    This is the contract that replaces a runtime clamp: the ring path's
    CamillaDSP chunk is :data:`jasper.fanin_coupling.RING_CAMILLA_CHUNKSIZE`,
    so pinning the grouping ring's slot to it makes the disagreement checkable
    at merge instead of correctable at emit.
    """
    assert GROUPING_RING_PERIOD_FRAMES == RING_CAMILLA_CHUNKSIZE, (
        f"the grouping ring's slot ({GROUPING_RING_PERIOD_FRAMES}) and the ring "
        f"path's CamillaDSP chunk ({RING_CAMILLA_CHUNKSIZE}) have to be one "
        "number; changing either alone re-introduces a chunk that spans a "
        "different number of slots than the geometry was derived for"
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

    The coupling's three ring files are unlinked on every deploy because they
    MUST be — an existing mapping created at an older default geometry is a
    fatal attach mismatch — and the deploy's own core-graph bounce is what
    re-creates and re-attaches them.

    The grouping ring is the other way round. Nothing has stopped its writer at
    that point in the install: ``jasper-snapclient.service`` is parked by
    ``park_audio_clients_for_core_graph_restart`` and started again by
    ``reconcile_grouping_state``, and BOTH live inside ``install_systemd_units``,
    which runs after ``install_jts_ring_platform``. So an unlink there would
    land under a live bonded writer — snapclient writing an inode nothing can
    name while its reader creates and attaches a fresh file, with no error on
    either side. The residual of NOT deleting it is smaller and loud: after a
    conf.d geometry change, a box that already created the ring since boot meets
    -EINVAL at open until a reboot clears the tmpfs.

    Design §3.4, ``captures/DESIGN-PROPOSAL-grouping-ring-2026-08-17.md``.
    """
    platform = _read(_RING_PLATFORM_SH)
    removed = set(re.findall(r"^\s*rm -f (/dev/shm/jts-ring/\S+)$", platform, re.MULTILINE))
    assert removed == {
        "/dev/shm/jts-ring/program.ring",
        "/dev/shm/jts-ring/content.ring",
        "/dev/shm/jts-ring/active-content.ring",
    }, f"the deploy-time ring rm -f set changed: {sorted(removed)}"
    assert GROUPING_RING_FILE not in removed
    # The asymmetry is stated where the lines are, not only here.
    assert "grouping.ring" in platform, (
        "the rm -f block must name grouping.ring's deliberate absence, or the "
        "next reader adds it as an oversight"
    )
