# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the DAC-content return ring platform (#3118, PR-1).

The platform is three files that ship together and open nothing:
``deploy/alsa/conf.d/63-jts-ring-dac-content.conf`` declares the PCM,
:mod:`jasper.multiroom.dac_content_ring` owns the same identity in Python, and
``deploy/lib/install/ring-platform.sh`` places the conf.d on every box. Nothing
consumes it yet — the lane is parked (ADR-0178 ``grouped_dac_content_lane``)
and its transport arrives behind a hardware gate.

Three contracts, in rising order of what they cost to get wrong:

**T-1 — the conf.d is a block the ioplug can parse.** This is an AVAILABILITY
guard, not tidiness. ``/etc/alsa/conf.d`` is parsed by alsa-lib on EVERY PCM
open on the box, so a malformed block here would break every renderer on every
box in the fleet — including the boxes that never group. The accepted key set
and the ``n_slots`` bounds are read out of the C source rather than restated,
so the guard cannot claim the plugin accepts something it refuses. Sibling of
``tests/test_grouping_ring_platform.py``, whose helpers this module consumes
rather than re-spelling.

**T-2 — the geometry and the wire have one source of truth.** Three
declarations of the same facts exist by necessity (an ALSA conf file, a Python
module, and outputd's Rust config), in three languages that cannot import each
other. Nothing but this test ties them together, and a disagreement is silent
in the worst direction: the ioplug reports a geometry mismatch as a hard
``-EINVAL`` at attach against an existing header, and reports nothing at all
before the ring exists. The load-bearing one is SLOT == READER PERIOD, which is
the whole reason this ring is a sibling of the grouping ring rather than a
second user of it.

**T-3 — the installer places it, and the deploy DOES unlink its ring file.**
The second half is the asymmetry with the grouping ring and is deliberate; the
reason is in that test's docstring.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.multiroom.dac_content_ring import (
    DAC_CONTENT_RING_CHANNELS,
    DAC_CONTENT_RING_CONF_D,
    DAC_CONTENT_RING_FILE,
    DAC_CONTENT_RING_FORMAT,
    DAC_CONTENT_RING_PCM,
    DAC_CONTENT_RING_PERIOD_FRAMES,
    DAC_CONTENT_RING_SLOTS,
    DAC_CONTENT_RING_WRITER_LOCK,
)

# One owner for "how a jts_ring conf.d block is scanned" and for "how an int is
# read out of the C header": the grouping platform's test already owns both, and
# a second copy here would drift the first time either moved.
from tests.test_grouping_ring_platform import _c_define, _strip_conf_comments

_REPO = Path(__file__).resolve().parents[1]
_DAC_CONTENT_CONF = _REPO / "deploy" / "alsa" / "conf.d" / "63-jts-ring-dac-content.conf"
_IOPLUG_C = _REPO / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"
_IOPLUG_H = _REPO / "c" / "jts-ring-ioplug" / "jts_ring_shm.h"
_RING_PLATFORM_SH = _REPO / "deploy" / "lib" / "install" / "ring-platform.sh"
_OUTPUTD_CONFIG_RS = _REPO / "rust" / "jasper-outputd" / "src" / "config.rs"
_OUTPUTD_DAC_CONTENT_RS = _REPO / "rust" / "jasper-outputd" / "src" / "dac_content.rs"
_OUTPUTD_RING_SOURCE_RS = (
    _REPO / "rust" / "jasper-outputd" / "src" / "shm_ring_source.rs"
)

#: The one rate this box runs. Neither the conf.d block nor the Python
#: identity declares it — the ioplug inherits it and the Rust reader hardcodes
#: it — so it is spelled here once and pinned against that reader below.
_RING_RATE_HZ = 48_000


def _read(path: Path) -> str:
    assert path.exists(), f"source not present: {path}"
    return path.read_text(encoding="utf-8")


def _rust_production_half(path: Path) -> str:
    """A Rust source with its ``#[cfg(test)]`` module cut off.

    The pins below count their matches, and the crate's own tests construct
    the same readers with throwaway geometries — an unscoped count would trip
    on them, or worse, pass by matching a test literal. The split is on a
    COLUMN-ZERO attribute: ``dac_content.rs`` also carries an indented
    ``#[cfg(test)]`` on a field, well above the call this pins.
    """
    return re.split(
        r"^#\[cfg\(test\)\]", _read(path), maxsplit=1, flags=re.MULTILINE
    )[0]


# --- T-1: the conf.d is a block the ioplug can parse ------------------------


def test_the_dac_content_confd_declares_exactly_one_pcm_and_it_is_the_return_ring():
    """One file, one block, and its name is the one Python hands consumers.

    A second block here would be a second geometry nobody derived, and a
    misspelled name would resolve to nothing at open — the ioplug never sees
    the request at all, so the failure surfaces as ALSA's generic "No such
    device" rather than anything naming this file.
    """
    conf = _read(_DAC_CONTENT_CONF)
    declared = re.findall(r"^\s*pcm\.([A-Za-z0-9_]+)\s*\{", conf, re.MULTILINE)
    assert declared == [DAC_CONTENT_RING_PCM], (
        f"{_DAC_CONTENT_CONF.name} must declare exactly pcm.{DAC_CONTENT_RING_PCM}; "
        f"found {declared}"
    )
    # The conf.d path this file installs to is the one Python names.
    assert DAC_CONTENT_RING_CONF_D == f"/etc/alsa/conf.d/{_DAC_CONTENT_CONF.name}"


def test_the_dac_content_confd_is_structurally_one_block_and_nothing_else():
    """Catches MALFORMATION and the ALIAS form, which key names cannot.

    The key-name guard below reads the block's contents; it is blind to text
    OUTSIDE the block and to a declaration with no braces at all. Both are
    reachable and both are worse than a bad key: a stray brace or top-level
    token makes the whole drop-in unparseable, and alsa-lib reads this directory
    on EVERY PCM open on the box. The alias form
    (``pcm.jts_ring_dac_content "jts_ring_grouping"``) declares no block, so a
    block-shaped scan never sees it — and this file sorts LAST in conf.d, after
    60-, 61- and 62-, so it holds override authority over every PCM they define.
    """
    stripped = _strip_conf_comments(_read(_DAC_CONTENT_CONF))
    assert stripped.count('"') % 2 == 0, (
        f"{_DAC_CONTENT_CONF.name} has an odd number of double quotes — an "
        "unterminated string makes the whole drop-in unparseable"
    )
    assert stripped.count("{") == stripped.count("}") == 1, (
        f"{_DAC_CONTENT_CONF.name} must be exactly one unnested PCM block; found "
        f"{stripped.count('{')} '{{' and {stripped.count('}')} '}}'. A stray "
        "brace makes alsa-lib fail to parse the directory on every PCM open."
    )
    prefix = stripped[: stripped.index("{")].strip()
    suffix = stripped[stripped.index("}") + 1 :].strip()
    assert prefix == f"pcm.{DAC_CONTENT_RING_PCM}", (
        f"the only thing before the block may be `pcm.{DAC_CONTENT_RING_PCM}`; "
        f"found {prefix!r}"
    )
    assert suffix == "", (
        f"nothing may follow the block; found {suffix!r}. An ALSA alias line "
        "here declares no braces, so the block-shaped guards cannot see it — "
        "and this file sorts after 60-/61-/62-, so it would win."
    )


def test_the_dac_content_confd_uses_only_keys_the_ioplug_accepts():
    """Every key in the block is one the C parser handles, derived from the C.

    The ioplug REFUSES an unknown field with -EINVAL, and that refusal is
    deliberate — it is what makes a deploy-skew mismatch fail at open instead of
    on the wire. So a key this file invents is not a typo that degrades, it is a
    PCM that cannot open. The accepted set is read off ``pcm_jts_ring.c``'s own
    ``strcmp(id, …)`` calls rather than restated here.
    """
    accepted = set(re.findall(r'strcmp\(id,\s*"([A-Za-z0-9_]+)"\)', _read(_IOPLUG_C)))
    assert {
        "path",
        "period_frames",
        "n_slots",
        "format",
        "channels",
        "pace_nominal",
        "type",
    } <= accepted, (
        "the ioplug key scan found an implausibly small set — the parse loop's "
        f"shape has changed and this derivation needs re-deriving: {sorted(accepted)}"
    )

    conf = _read(_DAC_CONTENT_CONF)
    body = conf[conf.index("{") + 1 : conf.rindex("}")]
    used = {
        m.group("key")
        for m in re.finditer(r"^\s*(?P<key>[A-Za-z0-9_]+)\s+\S", body, re.MULTILINE)
    }
    assert used <= accepted, (
        f"{_DAC_CONTENT_CONF.name} declares key(s) the ioplug refuses at open with "
        f"-EINVAL: {sorted(used - accepted)}. Accepted: {sorted(accepted)}"
    )
    # `type jts_ring` is what resolves the block to the plugin at all.
    assert re.search(r"^\s*type\s+jts_ring\s*$", body, re.MULTILINE), (
        f"{_DAC_CONTENT_CONF.name} must declare `type jts_ring`"
    )


def test_the_dac_content_ring_geometry_is_inside_the_plugins_bounds():
    """``n_slots`` and the slot BYTE size are range-checked by the C at open.

    The equality against the slot CEILING is asserted on purpose and is not the
    same claim as the range: this ring, like the grouping ring, took the deepest
    geometry the plugin allows, which means depth is not tunable upward from a
    conf.d edit. If JTS_RING_MAX_SLOTS ever moves, this line should be
    re-decided deliberately rather than drifting along with it.

    The byte check is the one the 8x-larger slot actually needs: a 1024-frame
    slot is 8x the grouping ring's, so ``JTS_RING_MAX_SLOT_BYTES`` stops being
    obviously satisfied and becomes a real bound (4096 of 65536 today).
    """
    header = _read(_IOPLUG_H)
    min_slots = _c_define("JTS_RING_MIN_SLOTS", header)
    max_slots = _c_define("JTS_RING_MAX_SLOTS", header)
    assert min_slots <= DAC_CONTENT_RING_SLOTS <= max_slots, (
        f"n_slots={DAC_CONTENT_RING_SLOTS} is outside the ioplug's "
        f"{min_slots}..{max_slots} — the PCM would fail to open with -EINVAL"
    )
    assert DAC_CONTENT_RING_SLOTS == max_slots, (
        f"the DAC-content ring is specified at the plugin's slot ceiling "
        f"({max_slots}); it now reads {DAC_CONTENT_RING_SLOTS}. Raising the C "
        "ceiling does not automatically mean this ring should get deeper — "
        "re-decide it and move this line in the same commit."
    )

    bits = re.fullmatch(r"S(\d+)_LE", DAC_CONTENT_RING_FORMAT)
    assert bits is not None, (
        f"DAC_CONTENT_RING_FORMAT={DAC_CONTENT_RING_FORMAT!r} is not an S<bits>_LE token"
    )
    slot_bytes = (
        DAC_CONTENT_RING_PERIOD_FRAMES
        * DAC_CONTENT_RING_CHANNELS
        * (int(bits.group(1)) // 8)
    )
    max_slot_bytes = _c_define("JTS_RING_MAX_SLOT_BYTES", header)
    assert slot_bytes <= max_slot_bytes, (
        f"one slot is {slot_bytes} B, over the ioplug's {max_slot_bytes} B "
        "ceiling — the PCM would fail to open with -EINVAL"
    )


# --- T-2: one source of truth for the geometry and the wire -----------------


def _confd_field(key: str) -> str:
    """The single value the DAC-content block declares for ``key``.

    Deliberately reads the SHIPPED file with the ring platform's own block
    parser rather than a private regex, so "how a jts_ring block is read" keeps
    one owner (:mod:`jasper.ring_assets`, which the conf.d renderer and every
    doctor check already share).
    """
    from jasper import ring_assets

    readers = {
        "period_frames": lambda: ring_assets.ring_conf_period_frames(
            str(_DAC_CONTENT_CONF)
        ),
        "n_slots": lambda: ring_assets.ring_conf_n_slots(
            DAC_CONTENT_RING_PCM, str(_DAC_CONTENT_CONF)
        ),
        "channels": lambda: ring_assets.ring_conf_channels(
            DAC_CONTENT_RING_PCM, str(_DAC_CONTENT_CONF)
        ),
        "format": lambda: ring_assets.ring_conf_format(
            DAC_CONTENT_RING_PCM, str(_DAC_CONTENT_CONF)
        ),
    }
    value = readers[key]()
    assert value is not None, (
        f"{_DAC_CONTENT_CONF.name} declares no single `{key}` for "
        f"pcm.{DAC_CONTENT_RING_PCM} — absent, unreadable, or torn"
    )
    return str(value)


@pytest.mark.parametrize(
    ("key", "constant"),
    [
        ("period_frames", DAC_CONTENT_RING_PERIOD_FRAMES),
        ("n_slots", DAC_CONTENT_RING_SLOTS),
        ("channels", DAC_CONTENT_RING_CHANNELS),
        ("format", DAC_CONTENT_RING_FORMAT),
    ],
)
def test_the_confd_block_and_the_python_constants_agree(key, constant):
    """The ALSA file and :mod:`jasper.multiroom.dac_content_ring` are one fact.

    Two languages, no import between them. The writer resolves the geometry
    through the conf.d (ALSA hands the ioplug the parsed block) and every Python
    consumer resolves it through the module, so a drift means the two ends of
    one transport disagree about the same ring — which the ioplug reports as a
    hard -EINVAL at attach against an existing header, and which nothing reports
    at all before the ring exists.
    """
    assert _confd_field(key) == str(constant), (
        f"{_DAC_CONTENT_CONF.name}'s `{key}` and dac_content_ring.py disagree: "
        f"conf.d={_confd_field(key)!r} vs constant={constant!r}"
    )


def test_the_dac_content_ring_slot_is_one_outputd_dac_period():
    """SLOT == READER PERIOD. The invariant that makes this a separate ring.

    outputd consumes exactly one slot per DAC period, so a slot that is not the
    reader's period leaves it holding a partial slot every period — which is
    also the reason this ring exists at all rather than reusing
    ``jts_ring_grouping``, whose CamillaDSP reader sips 128 frames. Read off the
    Rust constant that owns the number, so a period change in outputd fails HERE
    rather than as a geometry mismatch on metal.
    """
    m = re.search(
        r"^pub const DEFAULT_PERIOD_FRAMES:\s*u32\s*=\s*(\d+)\s*;",
        _read(_OUTPUTD_CONFIG_RS),
        re.MULTILINE,
    )
    assert m is not None, (
        "could not find DEFAULT_PERIOD_FRAMES in "
        f"{_OUTPUTD_CONFIG_RS.name} — this derivation needs re-deriving"
    )
    assert DAC_CONTENT_RING_PERIOD_FRAMES == int(m.group(1)), (
        f"the DAC-content ring's slot ({DAC_CONTENT_RING_PERIOD_FRAMES}) and "
        f"outputd's DAC period ({m.group(1)}) have to be one number; changing "
        "either alone makes the reader hold a partial slot every period"
    )


@pytest.mark.parametrize(
    ("rust_const", "rust_type", "constant"),
    [
        ("DEFAULT_DAC_CONTENT_RING_PATH", "&str", DAC_CONTENT_RING_FILE),
        ("DAC_CONTENT_RING_SLOTS", "u32", DAC_CONTENT_RING_SLOTS),
    ],
)
def test_the_reader_side_rust_constants_agree_with_the_python_ones(
    rust_const, rust_type, constant
):
    """The THIRD declarer the module docstring names: outputd's Rust config.

    outputd's ring arm resolves the return ring from its own constants, because
    nothing hands a Rust daemon a Python module — so the path it opens and the
    depth it declares are spelled a second time there. A drift in the PATH means
    the reader attaches a ring the writer never writes (a leader that plays
    silence with a climbing starvation counter, forever); a drift in the SLOTS
    is a geometry mismatch the crate refuses at attach, parking the box. Neither
    is visible in either language alone.
    """
    rust = _read(_OUTPUTD_CONFIG_RS)
    literal = f'"{constant}"' if rust_type == "&str" else str(constant)
    needle = f"pub const {rust_const}: {rust_type} = {literal};"
    assert needle in rust, (
        f"{_OUTPUTD_CONFIG_RS.name} must spell `{needle}` — outputd's reader and "
        f"jasper.multiroom.dac_content_ring disagree about {rust_const}"
    )


def test_the_rust_ring_arm_opens_the_wire_python_and_the_confd_declare():
    """FORMAT and CHANNELS, pinned where the reader actually spells them.

    ``config.rs`` declares the path and the depth as named constants, so the
    parametrized pin above reaches them. The other two axes are not constants
    at all: they are POSITIONAL literals in the one call that builds the
    reader, which is why nothing above guards them. A drift is a geometry
    mismatch ``RingReader::create_or_attach`` refuses — the leader parks
    instead of playing, and neither language shows the disagreement alone.

    The two spellings are compared NORMALIZED (underscores dropped, folded to
    lower) rather than through a lookup table, so no edit keeps them in step
    artificially — and unlike a title-case derivation it stays correct for the
    crate's three-part ``S24_3Le``, which no rule of that shape reproduces.
    """
    calls = re.findall(
        r"ShmRingSource::new\(\s*[^,]+,\s*[^,]+,\s*(\d+),\s*SampleFormat::(\w+)\s*,",
        _rust_production_half(_OUTPUTD_DAC_CONTENT_RS),
    )
    assert len(calls) == 1, (
        f"{_OUTPUTD_DAC_CONTENT_RS.name} must build the ring reader exactly "
        f"once for this pin to guard it; found {len(calls)}. A second call "
        "site of the same shape would leave this checking only the first."
    )
    channels, sample_format = calls[0]
    assert int(channels) == DAC_CONTENT_RING_CHANNELS, (
        f"the reader opens {channels} channels where the conf.d block and "
        f"jasper.multiroom.dac_content_ring declare {DAC_CONTENT_RING_CHANNELS}"
    )
    assert sample_format.replace("_", "").lower() == (
        DAC_CONTENT_RING_FORMAT.replace("_", "").lower()
    ), (
        f"the reader opens SampleFormat::{sample_format} where the conf.d "
        f"block and jasper.multiroom.dac_content_ring declare "
        f"{DAC_CONTENT_RING_FORMAT}"
    )


def test_the_ring_readers_rate_is_the_one_rate_this_box_runs():
    """The RATE axis — the only one no declarer but the reader carries.

    The conf.d block names no rate and the Python identity holds no constant
    for it; the ioplug inherits it and this literal is where it enters the
    ring's geometry. It is VALIDATED rather than negotiated: the geometry goes
    field-by-field against the live header at attach, so a changed rate here
    presents as a refused attach, not a resample. The depth claim this module
    documents is computed from the same number.
    """
    rates = re.findall(
        r"^\s*rate:\s*([\d_]+)\s*,",
        _rust_production_half(_OUTPUTD_RING_SOURCE_RS),
        re.MULTILINE,
    )
    assert len(rates) == 1, (
        f"{_OUTPUTD_RING_SOURCE_RS.name} must build the geometry exactly once "
        f"for this pin to guard it; found {len(rates)}. A second geometry at "
        "another rate would leave this checking only the first."
    )
    assert int(rates[0].replace("_", "")) == _RING_RATE_HZ, (
        f"the ring reader pins {rates[0]} Hz where this tree's depth and "
        f"timing claims are computed at {_RING_RATE_HZ} Hz"
    )


def test_the_dac_content_ring_is_deeper_in_time_than_the_grouping_ring():
    """The depth claim the module's comment makes, checked rather than asserted.

    Both rings sit at the plugin's 16-slot ceiling, so the ONLY thing that
    separates 341 ms of cushion from 43 ms is the slot — which is the reader's
    period. This pins the consequence, so a future edit that "unifies" the two
    geometries has to explain where the leader's cushion went.
    """
    from jasper.multiroom.grouping_ring import (
        GROUPING_RING_PERIOD_FRAMES,
        GROUPING_RING_SLOTS,
    )

    assert DAC_CONTENT_RING_PERIOD_FRAMES > GROUPING_RING_PERIOD_FRAMES
    dac_frames = DAC_CONTENT_RING_PERIOD_FRAMES * DAC_CONTENT_RING_SLOTS
    grouping_frames = GROUPING_RING_PERIOD_FRAMES * GROUPING_RING_SLOTS
    assert dac_frames > grouping_frames
    # The rate this depth is computed at is the one the reader pins, checked
    # against the Rust literal by the rate test above.
    assert round(dac_frames / _RING_RATE_HZ * 1000) == 341, (
        f"the module documents 341 ms of depth; the geometry now buys "
        f"{dac_frames / _RING_RATE_HZ * 1000:.0f} ms"
    )


def test_the_dac_content_ring_asks_to_be_paced():
    """``pace_nominal 1``, for the same reason the grouping ring declares it.

    The field opts this PCM's PLAYBACK direction into the ioplug's rate limiter
    (``jts_ring_pace_apply``). It is here because of the WRITER, not the reader:
    snapclient's ALSA player expects the device to pace it, so against a stalled
    or dead reader it storms. That outputd owns the DAC clock is what holds the
    steady state at nominal — it is not a reason to drop the floor under the
    failure.

    Both directions are pinned in ``tests/test_grouping_ring_platform.py``'s
    scoping test, which owns the fleet-wide claim about WHICH files may declare
    this field; this one owns only "the block still has it".
    """
    conf = _read(_DAC_CONTENT_CONF)
    body = conf[conf.index("{") + 1 : conf.rindex("}")]
    assert re.search(r"^\s*pace_nominal\s+1\s*$", body, re.MULTILINE), (
        f"{_DAC_CONTENT_CONF.name} must declare `pace_nominal 1` — without it "
        "the return ring's writer is unpaced against a stalled or dead reader"
    )


def test_the_writer_lock_path_is_derived_from_the_platforms_own_rule():
    """One suffix rule, one owner.

    The lock is what makes a second writer's open fail loudly with -EBUSY, and
    its identity is the PATHNAME — so a second spelling of the suffix here would
    let two writers lock two different files and proceed silently.
    :func:`jasper.ring_assets.ring_writer_lock_path` is already pinned against
    the C header; this asserts the DAC-content ring goes through it rather than
    around it.
    """
    from jasper.ring_assets import ring_writer_lock_path

    assert DAC_CONTENT_RING_WRITER_LOCK == ring_writer_lock_path(DAC_CONTENT_RING_FILE)
    assert DAC_CONTENT_RING_WRITER_LOCK != DAC_CONTENT_RING_FILE


def test_the_confd_path_key_is_the_ring_file_python_names():
    conf = _read(_DAC_CONTENT_CONF)
    m = re.search(r'^\s*path\s+"([^"]+)"\s*$', conf, re.MULTILINE)
    assert m is not None, f"{_DAC_CONTENT_CONF.name} declares no quoted `path`"
    assert m.group(1) == DAC_CONTENT_RING_FILE


# --- T-3: install coverage, and the deliberate rm -f membership -------------


def test_the_installer_ships_the_dac_content_confd():
    """Placed by the same helper as its three siblings, at the same mode.

    0644 is load-bearing (AGENTS.md, the PR #214 class): a definition only root
    can read is a name the non-root renderer users cannot resolve. Asserting the
    install LINE rather than a doctor presence check is the sibling precedent —
    ``61-jts-renderer-lanes.conf`` and ``62-jts-ring-grouping.conf`` are both
    covered exactly this way, and :func:`jasper.ring_assets.ring_asset_presence`
    deliberately stays scoped to the coupling's own conf.d because it is the
    shm_ring ACTIVATION gate: a missing conf.d here must not refuse the fan-in
    coupling's arm.
    """
    platform = _read(_RING_PLATFORM_SH)
    assert "63-jts-ring-dac-content.conf" in platform
    assert re.search(
        r'install -m 0644 "\$\{dac_content_src\}" '
        r"/etc/alsa/conf\.d/63-jts-ring-dac-content\.conf",
        platform,
    ), (
        "the DAC-content ring PCM must be installed system-wide at 0644 so any "
        "user can resolve the name"
    )


def test_the_deploy_unlinks_the_dac_content_ring_file():
    """``dac-content.ring`` IS in install's ``rm -f`` list — unlike grouping.

    The discriminator is the FAILURE-ESCALATION ASYMMETRY the installer's own
    comment records, and this ring lands on the opposite side of it from the
    grouping ring:

    * Its READER is ``jasper-outputd``, whose unit carries
      ``StartLimitBurst=5`` + ``StartLimitAction=reboot`` — the same escalation
      as ``jasper-fanin``. A stale-geometry ring is a fatal attach, and five of
      those reboot the box mid-install.
    * Its geometry is DERIVED from outputd's own ``DEFAULT_PERIOD_FRAMES``, so
      the deploy that changes that number is exactly the deploy that leaves a
      stale header behind. The grouping ring's geometry has no such coupling to
      a value a deploy can move.

    ``jasper-snapclient.service`` — the WRITER, and the reason ``grouping.ring``
    stays out — carries ``StartLimitBurst=6`` and no ``StartLimitAction``. That
    bounds the writer's half; it says nothing about the reader's, which is the
    half that reboots.

    Asserted through the constant rather than the literal, so a path respelled
    in Python fails here too.
    """
    platform = _read(_RING_PLATFORM_SH)
    removed = set(
        re.findall(r"^\s*rm -f (/dev/shm/jts-ring/\S+)$", platform, re.MULTILINE)
    )
    assert DAC_CONTENT_RING_FILE in removed, (
        "the DAC-content ring's reader reboots the household on a fatal attach; "
        f"its file must be unlinked at deploy. rm -f set: {sorted(removed)}"
    )
    # RING FILES ONLY: unlinking a `.writer.lock` / `.open.lock` opens a silent
    # inode-tear window between two holders.
    assert DAC_CONTENT_RING_WRITER_LOCK not in removed


def test_the_outputd_reader_unit_escalates_to_reboot():
    """The premise the rm -f membership rests on, pinned where it is claimed.

    The asymmetry above is an argument ABOUT TWO UNITS. If outputd ever dropped
    ``StartLimitAction=reboot``, this ring would belong on the grouping ring's
    side of the line and the reason recorded in ``ring-platform.sh`` would be
    stale prose pointing the wrong way.
    """
    unit = _read(_REPO / "deploy" / "systemd" / "jasper-outputd.service")
    assert re.search(r"^StartLimitAction=reboot$", unit, re.MULTILINE), (
        "jasper-outputd no longer reboots on a start-limit burst — re-decide "
        "whether dac-content.ring still belongs in the deploy's rm -f list"
    )


def test_the_ring_has_exactly_one_consumer_and_it_spells_the_owned_name():
    """The inertness pin's successor: ONE opener, and it imports the identity.

    Its predecessor asserted nothing opened this ring, and said in so many
    words to delete it in the PR that wires the first consumer — this is that
    PR. What replaces it is the claim that still has teeth: the grouping
    reconciler is the only thing that names this PCM, and it names it by
    IMPORTING the constant, so the ALSA block and the ``--soundcard`` argument
    cannot drift into two spellings of one wire.
    """
    from jasper.multiroom import reconcile

    assert reconcile.DAC_CONTENT_RING_PCM is DAC_CONTENT_RING_PCM
    hits = sorted(
        path.relative_to(_REPO).as_posix()
        for path in (_REPO / "jasper").rglob("*.py")
        if DAC_CONTENT_RING_PCM in path.read_text(encoding="utf-8")
    )
    assert hits == ["jasper/multiroom/dac_content_ring.py"], (
        f"pcm.{DAC_CONTENT_RING_PCM} is spelled as a LITERAL outside its "
        f"identity module: {hits}. Import the constant instead — a second "
        "spelling is a wire the two ends can disagree about."
    )
