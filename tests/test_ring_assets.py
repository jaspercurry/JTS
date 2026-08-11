# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared jts_ring asset-presence SSOT (audio-graph consolidation P2).

Pins that the doctor probe and the coupling reconciler's activation gate name the
same three inert assets, and that presence is a pure filesystem stat with no
residue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper import ring_assets
from jasper.fanin_coupling import RING_SLOT_FRAMES, RingWire, resolve_ring_wire


def _shipped_wire(**overrides) -> RingWire:
    """The resolved ring wire, with named fields overridable for a negative test."""
    base = resolve_ring_wire()
    return RingWire(
        sample_format=overrides.get("sample_format", base.sample_format),
        ring_a_channels=overrides.get("ring_a_channels", base.ring_a_channels),
        ring_b_channels=overrides.get("ring_b_channels", base.ring_b_channels),
        period_frames=overrides.get("period_frames", base.period_frames),
    )


def test_asset_path_constants_match_p1_ship_locations():
    # These MUST match what P1's ring-platform.sh installs / the conf.d ships.
    assert ring_assets.RING_IOPLUG_SO == "libasound_module_pcm_jts_ring.so"
    assert ring_assets.RING_CONF_D == "/etc/alsa/conf.d/60-jts-ring.conf"
    assert ring_assets.RING_SHM_DIR == "/dev/shm/jts-ring"
    assert ring_assets.RING_ALSA_PLUGIN_DIR == "/usr/lib/aarch64-linux-gnu/alsa-lib"
    assert ring_assets.ring_ioplug_so_path().endswith(
        "/alsa-lib/libasound_module_pcm_jts_ring.so"
    )


def test_all_present_when_every_asset_exists(tmp_path):
    plugin_dir = tmp_path / "alsa-lib"
    plugin_dir.mkdir()
    (plugin_dir / ring_assets.RING_IOPLUG_SO).write_bytes(b"\x7fELF")
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text("pcm.jts_ring_capture {}\n")
    shm = tmp_path / "jts-ring"
    shm.mkdir()

    presence = ring_assets.ring_asset_presence(
        plugin_dir=str(plugin_dir), conf_d=str(conf), shm_dir=str(shm)
    )
    assert presence.all_present is True
    assert presence.missing() == ()


def test_missing_lists_each_absent_asset(tmp_path):
    # Nothing created -> all three missing, each named.
    presence = ring_assets.ring_asset_presence(
        plugin_dir=str(tmp_path / "nope"),
        conf_d=str(tmp_path / "nope.conf"),
        shm_dir=str(tmp_path / "noshm"),
    )
    assert presence.all_present is False
    missing = presence.missing()
    assert len(missing) == 3
    assert any("ioplug .so absent" in m for m in missing)
    assert any("conf.d absent" in m for m in missing)
    assert any("jts-ring" in m and "absent" in m for m in missing)


def test_partial_presence_reports_only_the_missing(tmp_path):
    plugin_dir = tmp_path / "alsa-lib"
    plugin_dir.mkdir()
    (plugin_dir / ring_assets.RING_IOPLUG_SO).write_bytes(b"\x7fELF")
    # conf.d + shm dir absent.
    presence = ring_assets.ring_asset_presence(
        plugin_dir=str(plugin_dir),
        conf_d=str(tmp_path / "absent.conf"),
        shm_dir=str(tmp_path / "absent-shm"),
    )
    assert presence.so_present is True
    assert presence.conf_present is False
    assert presence.shm_dir_present is False
    assert presence.all_present is False
    assert len(presence.missing()) == 2


def test_presence_leaves_no_residue(tmp_path):
    # A stat-only probe must not create the ring file / conf / dir it checks for.
    conf = tmp_path / "60-jts-ring.conf"
    shm = tmp_path / "jts-ring"
    ring_assets.ring_asset_presence(
        plugin_dir=str(tmp_path / "alsa-lib"),
        conf_d=str(conf),
        shm_dir=str(shm),
    )
    assert not conf.exists()
    assert not shm.exists()


# --- Ring slot geometry (SF3): conf.d period must match outputd's DAC period ----

_RING_CONF_TEMPLATE = """\
pcm.jts_ring_capture {{
    type jts_ring
    path "/dev/shm/jts-ring/program.ring"
    period_frames {p}
    n_slots 2
}}
pcm.jts_ring_playback {{
    type jts_ring
    path "/dev/shm/jts-ring/content.ring"
    period_frames {p}
    n_slots 2
}}
"""


def test_ring_conf_period_frames_parses_single_geometry(tmp_path):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(_RING_CONF_TEMPLATE.format(p=128), encoding="utf-8")
    assert ring_assets.ring_conf_period_frames(str(conf)) == 128


def test_ring_conf_period_frames_none_when_absent_or_torn(tmp_path):
    # Absent file -> None.
    assert ring_assets.ring_conf_period_frames(str(tmp_path / "missing.conf")) is None
    # Two PCMs disagreeing (a torn conf.d) -> None, not a silent pick.
    torn = tmp_path / "torn.conf"
    torn.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 1024\n}\n",
        encoding="utf-8",
    )
    assert ring_assets.ring_conf_period_frames(str(torn)) is None
    # No period_frames line at all -> None.
    empty = tmp_path / "noperiod.conf"
    empty.write_text("pcm.jts_ring_capture { type jts_ring }\n", encoding="utf-8")
    assert ring_assets.ring_conf_period_frames(str(empty)) is None


def test_ring_geometry_matches_when_conf_equals_outputd(tmp_path):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(_RING_CONF_TEMPLATE.format(p=128), encoding="utf-8")
    match = ring_assets.ring_geometry_matches_outputd(128, conf_d=str(conf))
    assert match.ok is True
    assert match.conf_period_frames == 128
    assert match.outputd_period_frames == 128


def test_ring_geometry_mismatch_gives_crisp_actionable_reason(tmp_path):
    # SF3: the shipped conf.d pins the fixed 128-frame slot; a box whose outputd
    # period is the packaged 1024 (e.g. jts3 / HiFiBerry, which declares no
    # 128-frame latency floor) mismatches. This detail is OPERATOR-FACING and
    # fires on today's floorless fleet.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(_RING_CONF_TEMPLATE.format(p=RING_SLOT_FRAMES), encoding="utf-8")
    match = ring_assets.ring_geometry_matches_outputd(1024, conf_d=str(conf))
    assert match.ok is False
    assert match.conf_period_frames == RING_SLOT_FRAMES
    assert match.outputd_period_frames == 1024
    # Names both numbers and how to fix — not a bare "mismatch".
    assert str(RING_SLOT_FRAMES) in match.detail and "1024" in match.detail
    assert "JASPER_OUTPUTD_PERIOD_FRAMES" in match.detail
    # It must NOT advise raising the conf.d to outputd's period: Ring A's slot is
    # fan-in's compile-time constant, so that edit produces a geometry fan-in
    # never builds (RING_ATTACH_FATAL) and nothing converges it back on a
    # floorless box. The only advice is aligning the OUTPUTD period.
    assert "do NOT raise the conf.d period" in match.detail
    assert f"FIXED at {RING_SLOT_FRAMES}" in match.detail
    assert "#2147" in match.detail


def test_ring_geometry_missing_conf_is_failclosed(tmp_path):
    match = ring_assets.ring_geometry_matches_outputd(
        1024, conf_d=str(tmp_path / "missing.conf")
    )
    assert match.ok is False
    assert match.conf_period_frames is None


# --- Ring-A slot count coherence (defect A) ----------------------------------


def test_ring_conf_n_slots_parses_per_block(tmp_path):
    # The parser must scope to the named block, not scan the whole file; Ring A and
    # Ring B happen to both use 2 slots today, but either block may diverge under a
    # coherent future override.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(_RING_CONF_TEMPLATE.format(p=128), encoding="utf-8")
    assert ring_assets.ring_conf_n_slots("jts_ring_capture", str(conf)) == 2
    assert ring_assets.ring_conf_n_slots("jts_ring_playback", str(conf)) == 2


def test_ring_conf_n_slots_none_when_absent_or_missing_block(tmp_path):
    assert ring_assets.ring_conf_n_slots("jts_ring_capture", str(tmp_path / "no.conf")) is None
    # Block present but no n_slots line -> None.
    conf = tmp_path / "noslot.conf"
    conf.write_text("pcm.jts_ring_capture {\n    period_frames 128\n}\n", encoding="utf-8")
    assert ring_assets.ring_conf_n_slots("jts_ring_capture", str(conf)) is None
    # Requested block missing entirely -> None.
    assert ring_assets.ring_conf_n_slots("jts_ring_nope", str(conf)) is None


def test_ring_slot_geometry_matches_when_env_equals_conf(tmp_path):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(_RING_CONF_TEMPLATE.format(p=128), encoding="utf-8")
    match = ring_assets.ring_slot_geometry_matches_conf(2, conf_d=str(conf))
    assert match.ok is True
    assert match.fanin_n_slots == 2
    assert match.conf_n_slots == 2


def test_ring_slot_geometry_mismatch_gives_crisp_reason(tmp_path):
    # Default migration: fan-in resolves an old 8-slot value while the conf.d pins
    # the new 2-slot production default. Names both counts + the fix, not a bare
    # "mismatch".
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(_RING_CONF_TEMPLATE.format(p=128), encoding="utf-8")
    match = ring_assets.ring_slot_geometry_matches_conf(8, conf_d=str(conf))
    assert match.ok is False
    assert match.fanin_n_slots == 8
    assert match.conf_n_slots == 2
    assert "n_slots=8" in match.detail and "n_slots=2" in match.detail
    assert "JASPER_FANIN_RING_SLOTS" in match.detail


def test_ring_slot_geometry_missing_conf_is_failclosed(tmp_path):
    match = ring_assets.ring_slot_geometry_matches_conf(
        8, conf_d=str(tmp_path / "missing.conf")
    )
    assert match.ok is False
    assert match.conf_n_slots is None


# --- On-disk ring header reader (defect A stale-file guard) -------------------


def _write_ring_header(
    path,
    *,
    magic=0x4A52_494E,
    version=1,
    rate=48000,
    channels=2,
    sample_format=ring_assets.RING_SAMPLE_FORMAT_S16LE,
    period=128,
    n_slots=2,
):
    import struct

    hdr = bytearray(ring_assets._RING_HEADER_BYTES)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_MAGIC, magic)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_VERSION, version)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_RATE, rate)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_CHANNELS, channels)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_SAMPLE_FORMAT, sample_format)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_PERIOD_FRAMES, period)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_N_SLOTS, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)


def test_read_ring_header_reads_valid_geometry(tmp_path):
    ring = tmp_path / "program.ring"
    _write_ring_header(ring, period=128, n_slots=2)
    header = ring_assets.read_ring_header(str(ring))
    assert header.valid is True
    assert header.version == 1
    assert header.period_frames == 128
    assert header.n_slots == 2


def test_read_ring_header_reads_every_declared_geometry_field(tmp_path):
    """All six fields round-trip, at DISTINCT values.

    The stale-file guards compare a header against what the writer will build,
    and the attach compares every field. A reader that only saw slots/period
    would report a coherent ring for a file that shears on rate, channels, or
    format — which is the shape that plays wrong audio instead of failing loud.
    Distinct values are what makes a swapped-offset bug fail here: with the
    shipped 2-channel / S16 pair, `channels` and `sample_format` are 2 and 1 at
    adjacent offsets, and several wrong wirings would still "pass".
    """
    ring = tmp_path / "wide.ring"
    _write_ring_header(
        ring,
        rate=44100,
        channels=6,
        sample_format=ring_assets.RING_SAMPLE_FORMAT_S32LE,
        period=256,
        n_slots=4,
    )
    header = ring_assets.read_ring_header(str(ring))
    assert header.valid is True
    assert header.version == 1
    assert header.rate == 44100
    assert header.channels == 6
    assert header.sample_format == ring_assets.RING_SAMPLE_FORMAT_S32LE
    assert header.sample_format_name == "S32_LE"
    assert header.period_frames == 256
    assert header.n_slots == 4


def test_read_ring_header_invalid_when_absent_short_or_magicless(tmp_path):
    # Absent file.
    assert ring_assets.read_ring_header(str(tmp_path / "gone.ring")).valid is False
    # Too short for a header.
    short = tmp_path / "short.ring"
    short.write_bytes(b"\x00" * 32)
    assert ring_assets.read_ring_header(str(short)).valid is False
    # Full-size but WRONG magic (a torn / foreign file) — must not be trusted.
    bad = tmp_path / "bad.ring"
    _write_ring_header(bad, magic=0xDEADBEEF, n_slots=2)
    header = ring_assets.read_ring_header(str(bad))
    assert header.valid is False
    # The (untrusted) geometry fields are NOT surfaced when invalid.
    assert header.n_slots == 0


def test_read_ring_header_refuses_a_version_these_offsets_do_not_describe(tmp_path):
    """A magic-matching header at another layout version vouches for nothing.

    These offsets are the v1 layout's. Nothing compared `version` before, so a
    file announcing a different layout had its bytes read AS IF v1 and its
    "geometry" believed. The version is still reported, so a caller can name
    what it saw rather than say "no ring".
    """
    ring = tmp_path / "future.ring"
    _write_ring_header(ring, version=7, period=999, n_slots=9)
    header = ring_assets.read_ring_header(str(ring))
    assert header.valid is False
    assert header.version == 7
    # Nothing else is surfaced from a layout we cannot parse.
    assert header.period_frames == 0
    assert header.n_slots == 0
    assert header.channels == 0
    assert header.sample_format == 0


def test_unknown_sample_format_id_is_named_honestly(tmp_path):
    # A header carrying an id outside the layout's two must not be printed as
    # one of them; the detail says exactly what the byte was.
    ring = tmp_path / "odd.ring"
    _write_ring_header(ring, sample_format=9)
    assert ring_assets.read_ring_header(str(ring)).sample_format_name == "id=9"


def test_ring_header_offsets_match_rust_layout():
    """The Python header offsets duplicate rust/jasper-ring/src/layout.rs (no way to
    link the Rust const). Pin them against the Rust golden layout so the two can't
    drift silently — a change to either side must update both (the same discipline
    as the Rust crate's own golden_layout test)."""
    from pathlib import Path

    layout = (
        Path(__file__).resolve().parents[1]
        / "rust" / "jasper-ring" / "src" / "layout.rs"
    ).read_text(encoding="utf-8")
    # MAGIC, HEADER_BYTES, VERSION, and the u32 field offsets we read.
    assert "pub const MAGIC: u32 = 0x4A52_494E;" in layout
    assert ring_assets._RING_MAGIC == 0x4A52_494E
    assert "pub const HEADER_BYTES: usize = 128;" in layout
    assert ring_assets._RING_HEADER_BYTES == 128
    assert "pub const VERSION: u32 = 1;" in layout
    assert ring_assets._RING_HEADER_VERSION == 1
    assert "pub const OFF_VERSION: usize = 4;" in layout
    assert ring_assets._RING_OFF_VERSION == 4
    assert "pub const OFF_RATE: usize = 8;" in layout
    assert ring_assets._RING_OFF_RATE == 8
    assert "pub const OFF_CHANNELS: usize = 12;" in layout
    assert ring_assets._RING_OFF_CHANNELS == 12
    assert "pub const OFF_SAMPLE_FORMAT: usize = 16;" in layout
    assert ring_assets._RING_OFF_SAMPLE_FORMAT == 16
    assert "pub const OFF_PERIOD_FRAMES: usize = 20;" in layout
    assert ring_assets._RING_OFF_PERIOD_FRAMES == 20
    assert "pub const OFF_N_SLOTS: usize = 24;" in layout
    assert ring_assets._RING_OFF_N_SLOTS == 24


def test_ring_sample_format_ids_match_rust_layout():
    """The sample_format ids are HEADER FIELD VALUES compared at attach.

    Python names them for a mismatch detail, so a Python-side drift would print
    the wrong format for a real shear. The ceiling pin test asserts the C and
    Rust copies agree; this is the third declaration.
    """
    from pathlib import Path

    layout = (
        Path(__file__).resolve().parents[1]
        / "rust" / "jasper-ring" / "src" / "layout.rs"
    ).read_text(encoding="utf-8")
    assert "pub const SAMPLE_FORMAT_S16LE: u32 = 1;" in layout
    assert ring_assets.RING_SAMPLE_FORMAT_S16LE == 1
    assert "pub const SAMPLE_FORMAT_S32LE: u32 = 2;" in layout
    assert ring_assets.RING_SAMPLE_FORMAT_S32LE == 2
    # The names are the ALSA tokens the conf.d and every emitter spell.
    from jasper.fanin_coupling import RING_WIRE_FORMATS

    assert set(ring_assets.RING_SAMPLE_FORMAT_NAMES.values()) == set(RING_WIRE_FORMATS)


# --- Per-box render: the conf.d slot period follows the DAC's declared floor ---
#
# The rule this pins: a per-box ring conf.d is rendered ONLY from a DECLARED
# LatencyFloor, and ONLY when that floor equals RING_SLOT_FRAMES — Ring A's slot
# size is fan-in's compile-time constant, so any other period would make the
# ioplug attach against a geometry fan-in never builds (RING_ATTACH_FATAL at
# arm). render_ring_conf_wire is the write half of the conf.d format whose
# read half is ring_conf_period_frames above; both use one regex, so a conf.d
# the parser accepts is one the renderer can update.

SHIPPED_RING_CONF = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
)


def _shipped_conf_copy(tmp_path):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_bytes(SHIPPED_RING_CONF.read_bytes())
    return conf


def _drifted_conf_copy(tmp_path, period_frames=1024):
    """The shipped conf.d with its period drifted off the transport slot.

    The remaining live write path: a hand-edited or half-installed conf.d that
    the render converges back onto RING_SLOT_FRAMES.
    """
    conf = _shipped_conf_copy(tmp_path)
    conf.write_text(
        conf.read_text(encoding="utf-8").replace(
            f"period_frames {RING_SLOT_FRAMES}", f"period_frames {period_frames}"
        ),
        encoding="utf-8",
    )
    return conf


def test_render_ring_conf_wire_is_a_no_op_when_it_already_matches(tmp_path):
    # The GOLDEN case: an Apple box's declared floor IS the shipped slot, so the
    # render leaves the file byte- AND mtime-identical. "Renders to the shipped
    # value" and "never rendered" must be indistinguishable on disk.
    conf = _shipped_conf_copy(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    outcome = ring_assets.render_ring_conf_wire(
        _shipped_wire(), conf_d=str(conf)
    )

    assert outcome.changed is False
    assert outcome.period_frames == RING_SLOT_FRAMES
    assert outcome.previous_period_frames == RING_SLOT_FRAMES
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_render_ring_conf_wire_moves_only_the_period_values(tmp_path):
    # Converging a drifted conf.d: both PCM blocks follow, and NOTHING else
    # moves — comments, n_slots, path, type, and indentation survive verbatim.
    conf = _drifted_conf_copy(tmp_path)
    before = conf.read_text(encoding="utf-8")

    outcome = ring_assets.render_ring_conf_wire(
        _shipped_wire(), conf_d=str(conf)
    )

    assert outcome.changed is True
    assert outcome.previous_period_frames == 1024
    after = conf.read_text(encoding="utf-8")
    assert ring_assets.ring_conf_period_frames(str(conf)) == RING_SLOT_FRAMES
    assert after.count(f"    period_frames {RING_SLOT_FRAMES}") == 2
    assert "period_frames 1024" not in after
    # Every other line is untouched.
    assert [
        line for line in before.splitlines()
        if not line.strip().startswith("period_frames ")
    ] == [
        line for line in after.splitlines()
        if not line.strip().startswith("period_frames ")
    ]


def test_render_ring_conf_wire_second_pass_writes_nothing(tmp_path):
    # Idempotence: reconcile runs on every boot/udev event, so a converged box
    # must stop writing (no mtime churn, no torn-read window).
    conf = _drifted_conf_copy(tmp_path)
    ring_assets.render_ring_conf_wire(_shipped_wire(), conf_d=str(conf))
    settled_bytes = conf.read_bytes()
    settled_mtime = conf.stat().st_mtime_ns

    outcome = ring_assets.render_ring_conf_wire(
        _shipped_wire(), conf_d=str(conf)
    )

    assert outcome.changed is False
    assert conf.read_bytes() == settled_bytes
    assert conf.stat().st_mtime_ns == settled_mtime


def test_render_ring_conf_wire_preserves_mode(tmp_path):
    # The conf.d must stay renderer-user resolvable (the PR #214 class): 0644
    # survives the temp-file replace.
    conf = _drifted_conf_copy(tmp_path)
    conf.chmod(0o644)

    ring_assets.render_ring_conf_wire(_shipped_wire(), conf_d=str(conf))

    assert conf.stat().st_mode & 0o777 == 0o644


def test_render_ring_conf_wire_converges_a_torn_conf(tmp_path):
    # A torn conf.d (the two PCMs disagreeing) has no single previous value to
    # report, but converging both onto the transport slot IS the repair.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        f"pcm.jts_ring_capture {{\n    period_frames {RING_SLOT_FRAMES}\n}}\n"
        "pcm.jts_ring_playback {\n    period_frames 1024\n}\n",
        encoding="utf-8",
    )

    outcome = ring_assets.render_ring_conf_wire(
        _shipped_wire(), conf_d=str(conf)
    )

    assert outcome.changed is True
    assert outcome.previous_period_frames is None
    assert ring_assets.ring_conf_period_frames(str(conf)) == RING_SLOT_FRAMES


def test_render_ring_conf_wire_refuses_to_invent_a_period_line(tmp_path):
    # No period_frames line at all is a torn/foreign file. Fail loud rather than
    # appending a geometry the ioplug would then attach against.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text("pcm.jts_ring_capture { type jts_ring }\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no period_frames"):
        ring_assets.render_ring_conf_wire(_shipped_wire(), conf_d=str(conf))
    assert conf.read_text(encoding="utf-8") == (
        "pcm.jts_ring_capture { type jts_ring }\n"
    )


@pytest.mark.parametrize("target", [0, -128, RING_SLOT_FRAMES * 2])
def test_render_ring_conf_wire_rejects_any_non_slot_target(tmp_path, target):
    # Ring A's slot is fan-in's compile-time constant, so the writer cannot emit
    # any other period even if a caller asks — the ioplug attach would hard-fail
    # (RING_ATTACH_FATAL) and CRASH shm_ring at arm rather than refuse it.
    conf = _shipped_conf_copy(tmp_path)
    before_bytes = conf.read_bytes()

    with pytest.raises(ValueError, match="RING_SLOT_FRAMES"):
        ring_assets.render_ring_conf_wire(
            _shipped_wire(period_frames=target), conf_d=str(conf)
        )
    assert conf.read_bytes() == before_bytes


def test_render_ring_conf_wire_raises_on_a_missing_conf(tmp_path):
    with pytest.raises(OSError):
        ring_assets.render_ring_conf_wire(
            _shipped_wire(), conf_d=str(tmp_path / "missing.conf")
        )


# --- The wire axes: per-block format + channels -------------------------------
#
# The conf.d gained two fields whose ABSENCE is meaningful: the C ioplug defaults
# an undeclared `format` to S16_LE and `channels` to 2, reproducing the pre-v2
# wire exactly. That is what lets the renderer stay silent on a box running the
# shipped geometry, and it is why the parsers report the default rather than
# "indeterminate" for a block that omits the key.


def test_shipped_conf_declares_the_wire_by_omission(tmp_path):
    """The shipped file names neither key, and still declares a complete wire."""
    conf = _shipped_conf_copy(tmp_path)
    text = conf.read_text(encoding="utf-8")
    assert "\n    format " not in text
    assert "\n    channels " not in text
    for pcm in (ring_assets.RING_A_CONF_PCM, ring_assets.RING_B_CONF_PCM):
        assert ring_assets.ring_conf_format(pcm, str(conf)) == (
            ring_assets.RING_CONF_DEFAULT_FORMAT
        )
        assert ring_assets.ring_conf_channels(pcm, str(conf)) == (
            ring_assets.RING_CONF_DEFAULT_CHANNELS
        )


def test_conf_defaults_match_the_c_ioplug():
    """The absent-key meaning is the ioplug's, not ours to choose.

    If the C defaults moved and these did not, an unrendered conf.d would open a
    geometry the Python side believes is something else — silently, because the
    file says nothing either way.
    """
    c_src = (
        Path(__file__).resolve().parents[1]
        / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"
    ).read_text(encoding="utf-8")
    assert "#define JTS_RING_DEFAULT_FORMAT JTS_RING_SAMPLE_FORMAT_S16LE" in c_src
    assert ring_assets.RING_CONF_DEFAULT_FORMAT == "S16_LE"
    assert "#define JTS_RING_DEFAULT_CHANNELS 2" in c_src
    assert ring_assets.RING_CONF_DEFAULT_CHANNELS == 2


def test_render_leaves_the_shipped_conf_byte_identical_for_every_ring_topology():
    """INERTNESS BAR. No topology the resolver serves moves a shipped conf.d.

    Not "the default case does not write" — EVERY topology, driven through the
    same resolver the reconciler calls. A render that wrote explicit
    format/channels lines would be semantically identical and still fail this:
    the bar is the bytes, because that is what makes "rendered" and "never
    rendered" indistinguishable on a deployed box.
    """
    import shutil
    import tempfile

    from jasper.fanin_coupling import resolve_ring_wire
    from tests.test_active_speaker_runtime_contract import (
        _full_range_mono,
        _full_range_stereo,
        _topology,
    )
    from tests.test_runtime_contract_ring import _dual_apple_stereo

    topologies = {
        "none": None,
        "unconfigured": _topology([]),
        "full_range_stereo": _full_range_stereo(),
        "full_range_mono": _full_range_mono(),
        "composite": _dual_apple_stereo(),
    }
    for label, topology in topologies.items():
        tmp = Path(tempfile.mkdtemp()) / "60-jts-ring.conf"
        shutil.copy(SHIPPED_RING_CONF, tmp)
        before = tmp.read_bytes()
        before_mtime = tmp.stat().st_mtime_ns

        outcome = ring_assets.render_ring_conf_wire(
            resolve_ring_wire(topology), conf_d=str(tmp)
        )

        assert outcome.changed is False, label
        assert tmp.read_bytes() == before, label
        assert tmp.stat().st_mtime_ns == before_mtime, label


def test_render_writes_only_the_axes_that_leave_the_ioplug_default(tmp_path):
    """A wide, N-channel wire lands as explicit keys — and only where it differs.

    Ring A stays stereo, so its `channels` line is still absent (2 IS the
    default); Ring B is 6, so it gains one. Both gain `format`. Everything else
    in the file — comments, path, n_slots, indentation — survives verbatim.
    """
    conf = _shipped_conf_copy(tmp_path)
    before = conf.read_text(encoding="utf-8")

    outcome = ring_assets.render_ring_conf_wire(
        _shipped_wire(sample_format="S32_LE", ring_b_channels=6), conf_d=str(conf)
    )

    assert outcome.changed is True
    assert ring_assets.ring_conf_format(ring_assets.RING_A_CONF_PCM, str(conf)) == (
        "S32_LE"
    )
    assert ring_assets.ring_conf_format(ring_assets.RING_B_CONF_PCM, str(conf)) == (
        "S32_LE"
    )
    assert ring_assets.ring_conf_channels(ring_assets.RING_A_CONF_PCM, str(conf)) == 2
    assert ring_assets.ring_conf_channels(ring_assets.RING_B_CONF_PCM, str(conf)) == 6
    after = conf.read_text(encoding="utf-8")
    # Ring A gained format only; Ring B gained format AND channels.
    assert after.count("    format S32_LE") == 2
    assert after.count("    channels 6") == 1
    assert "    channels 2" not in after
    # Nothing but the two new key kinds appeared.
    assert [
        line for line in after.splitlines() if line not in before.splitlines()
    ] == ["    format S32_LE", "    format S32_LE", "    channels 6"]


def test_render_round_trips_through_the_parsers(tmp_path):
    # render -> parse -> equality, on every axis and on BOTH blocks. The parsers
    # and the writer share their regexes precisely so this holds.
    conf = _shipped_conf_copy(tmp_path)
    wire = _shipped_wire(sample_format="S32_LE", ring_b_channels=8)
    ring_assets.render_ring_conf_wire(wire, conf_d=str(conf))

    assert ring_assets.ring_conf_period_frames(str(conf)) == wire.period_frames
    for pcm, expected_channels in (
        (ring_assets.RING_A_CONF_PCM, wire.ring_a_channels),
        (ring_assets.RING_B_CONF_PCM, wire.ring_b_channels),
    ):
        assert ring_assets.ring_conf_format(pcm, str(conf)) == wire.sample_format
        assert ring_assets.ring_conf_channels(pcm, str(conf)) == expected_channels
        assert ring_assets.ring_conf_n_slots(pcm, str(conf)) == 2


def test_render_is_idempotent_on_a_wide_wire(tmp_path):
    conf = _shipped_conf_copy(tmp_path)
    wire = _shipped_wire(sample_format="S32_LE", ring_b_channels=6)
    ring_assets.render_ring_conf_wire(wire, conf_d=str(conf))
    settled_bytes = conf.read_bytes()
    settled_mtime = conf.stat().st_mtime_ns

    outcome = ring_assets.render_ring_conf_wire(wire, conf_d=str(conf))

    assert outcome.changed is False
    assert conf.read_bytes() == settled_bytes
    assert conf.stat().st_mtime_ns == settled_mtime


def test_render_converges_a_wide_conf_back_to_the_narrow_wire(tmp_path):
    """Rollback: a box rendered wide must converge exactly when the wire narrows.

    A present key is rewritten to the explicit default rather than deleted, so
    the file stops declaring the stale wire — leaving a stale `format S32_LE`
    behind would shear the ends the moment the resolver answered narrow.
    """
    conf = _shipped_conf_copy(tmp_path)
    ring_assets.render_ring_conf_wire(
        _shipped_wire(sample_format="S32_LE", ring_b_channels=6), conf_d=str(conf)
    )

    outcome = ring_assets.render_ring_conf_wire(_shipped_wire(), conf_d=str(conf))

    assert outcome.changed is True
    after = conf.read_text(encoding="utf-8")
    assert "S32_LE" not in after
    assert "channels 6" not in after
    for pcm in (ring_assets.RING_A_CONF_PCM, ring_assets.RING_B_CONF_PCM):
        assert ring_assets.ring_conf_format(pcm, str(conf)) == "S16_LE"
        assert ring_assets.ring_conf_channels(pcm, str(conf)) == 2


NESTED_BLOCK_CONF = """\
pcm.jts_ring_capture {
    type jts_ring
    path "/dev/shm/jts-ring/program.ring"
    period_frames 128
    n_slots 2
    hint {
        show on
        description "JTS ring A"
    }
}

pcm.jts_ring_playback {
    type jts_ring
    path "/dev/shm/jts-ring/content.ring"
    period_frames 128
    n_slots 2
    channels 4
}
"""


def test_block_parsers_survive_a_nested_brace_block(tmp_path):
    """A `hint { ... }` inside a PCM block must not truncate the body.

    ALSA conf nests, and a `[^}]*` body — what the block scan used before the
    per-block wire fields landed — ends at the FIRST closing brace. That would
    have hidden every field after a nested block, and read Ring B's fields for
    Ring A when the scan ran past the end of Ring A's own block.
    """
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(NESTED_BLOCK_CONF, encoding="utf-8")

    assert ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(conf)) == 2
    assert ring_assets.ring_conf_n_slots(ring_assets.RING_B_CONF_PCM, str(conf)) == 2
    # Ring A declares no channels (default); Ring B's 4 must NOT leak into it.
    assert ring_assets.ring_conf_channels(ring_assets.RING_A_CONF_PCM, str(conf)) == 2
    assert ring_assets.ring_conf_channels(ring_assets.RING_B_CONF_PCM, str(conf)) == 4


def test_render_survives_a_nested_brace_block(tmp_path):
    # The writer uses the same brace-matching scan, so a nested block neither
    # truncates the edit nor gets rewritten.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(NESTED_BLOCK_CONF, encoding="utf-8")

    ring_assets.render_ring_conf_wire(
        _shipped_wire(sample_format="S32_LE", ring_b_channels=6), conf_d=str(conf)
    )

    after = conf.read_text(encoding="utf-8")
    assert 'description "JTS ring A"' in after
    assert "        show on" in after
    assert ring_assets.ring_conf_format(ring_assets.RING_A_CONF_PCM, str(conf)) == (
        "S32_LE"
    )
    assert ring_assets.ring_conf_channels(ring_assets.RING_B_CONF_PCM, str(conf)) == 6


def test_block_parsers_report_nothing_for_an_absent_or_unbalanced_block(tmp_path):
    conf = tmp_path / "60-jts-ring.conf"
    # Unbalanced: the block never closes, so its body has no end to trust.
    conf.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n",
        encoding="utf-8",
    )
    assert ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(conf)) is None
    assert ring_assets.ring_conf_format(ring_assets.RING_A_CONF_PCM, str(conf)) is None
    assert (
        ring_assets.ring_conf_channels(ring_assets.RING_A_CONF_PCM, str(conf)) is None
    )
    # Absent block.
    assert ring_assets.ring_conf_format(ring_assets.RING_B_CONF_PCM, str(conf)) is None


def test_render_refuses_a_conf_whose_blocks_cannot_be_found(tmp_path):
    # A period line but no readable PCM block: a torn/foreign file. Refuse
    # rather than write a wire into a shape we do not understand.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text("period_frames 128\n", encoding="utf-8")
    before = conf.read_bytes()

    with pytest.raises(ValueError, match="no readable pcm"):
        ring_assets.render_ring_conf_wire(
            _shipped_wire(sample_format="S32_LE"), conf_d=str(conf)
        )
    assert conf.read_bytes() == before


def test_a_block_declaring_a_field_twice_is_indeterminate(tmp_path):
    # Two different values for one key is torn, not a silent pick.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        "pcm.jts_ring_capture {\n"
        "    period_frames 128\n"
        "    n_slots 2\n"
        "    channels 2\n"
        "    channels 6\n"
        "}\n",
        encoding="utf-8",
    )
    assert (
        ring_assets.ring_conf_channels(ring_assets.RING_A_CONF_PCM, str(conf)) is None
    )


def test_quoted_format_token_parses(tmp_path):
    # Both spellings are valid ALSA conf and snd_config_get_string reads either.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        'pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n'
        '    format "S32_LE"\n}\n',
        encoding="utf-8",
    )
    assert ring_assets.ring_conf_format(ring_assets.RING_A_CONF_PCM, str(conf)) == (
        "S32_LE"
    )
