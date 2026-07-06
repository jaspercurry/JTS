# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared jts_ring asset-presence SSOT (audio-graph consolidation P2).

Pins that the doctor probe and the coupling reconciler's activation gate name the
same three inert assets, and that presence is a pure filesystem stat with no
residue.
"""

from __future__ import annotations

from jasper import ring_assets


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
    # SF3: the shipped conf.d pins 128 (placeholder); a box whose outputd period is
    # the packaged 1024 (e.g. jts3 / HiFiBerry, no Apple-dongle floor) mismatches.
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(_RING_CONF_TEMPLATE.format(p=128), encoding="utf-8")
    match = ring_assets.ring_geometry_matches_outputd(1024, conf_d=str(conf))
    assert match.ok is False
    assert match.conf_period_frames == 128
    assert match.outputd_period_frames == 1024
    # Names both numbers and how to fix — not a bare "mismatch".
    assert "128" in match.detail and "1024" in match.detail
    assert "JASPER_OUTPUTD_PERIOD_FRAMES" in match.detail


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


def _write_ring_header(path, *, magic=0x4A52_494E, version=1, period=128, n_slots=2):
    import struct

    hdr = bytearray(ring_assets._RING_HEADER_BYTES)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_MAGIC, magic)
    struct.pack_into("<I", hdr, ring_assets._RING_OFF_VERSION, version)
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
    # MAGIC, HEADER_BYTES, and the u32 field offsets we read.
    assert "pub const MAGIC: u32 = 0x4A52_494E;" in layout
    assert ring_assets._RING_MAGIC == 0x4A52_494E
    assert "pub const HEADER_BYTES: usize = 128;" in layout
    assert ring_assets._RING_HEADER_BYTES == 128
    assert "pub const OFF_VERSION: usize = 4;" in layout
    assert ring_assets._RING_OFF_VERSION == 4
    assert "pub const OFF_PERIOD_FRAMES: usize = 20;" in layout
    assert ring_assets._RING_OFF_PERIOD_FRAMES == 20
    assert "pub const OFF_N_SLOTS: usize = 24;" in layout
    assert ring_assets._RING_OFF_N_SLOTS == 24
