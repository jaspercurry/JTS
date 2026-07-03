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
