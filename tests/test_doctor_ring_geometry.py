# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ring-geometry-coherence doctor check (defect A).

``check_ring_geometry_coherence`` verifies the Ring-A geometry agrees across
three axes — fan-in's resolved ``JASPER_FANIN_RING_SLOTS``, the conf.d
``jts_ring_capture``, and the on-disk ``program.ring`` header — when shm_ring is
armed. It compares BOTH ``n_slots`` and (on the on-disk header) ``period_frames``;
a mismatch on any axis is the 2026-07-05 crash-loop class (hw_params EINVAL +
ioplug attach_fatal). It skips cleanly when shm_ring is not armed (the ring is
inert).
"""

from __future__ import annotations

import struct

from jasper.cli.doctor import audio


def _write_conf(tmp_path, *, capture_n_slots=8):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        f"pcm.jts_ring_capture {{\n    period_frames 128\n    n_slots {capture_n_slots}\n}}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    return conf


def _write_ring(path, *, n_slots=8, period_frames=128, magic=0x4A52_494E):
    hdr = bytearray(128)
    struct.pack_into("<I", hdr, 0, magic)
    struct.pack_into("<I", hdr, 4, 1)  # version
    struct.pack_into("<I", hdr, 20, period_frames)
    struct.pack_into("<I", hdr, 24, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)


def _arm(monkeypatch):
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )


def _stage(monkeypatch, tmp_path, *, fanin_env_text="", capture_n_slots=8):
    conf = _write_conf(tmp_path, capture_n_slots=capture_n_slots)
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(fanin_env_text, encoding="utf-8")
    program = tmp_path / "program.ring"
    monkeypatch.setattr(audio.ring_assets, "RING_CONF_D", str(conf))
    monkeypatch.setattr(audio, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio.ring_assets, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    return fanin_env, program


def test_skips_cleanly_when_not_armed(monkeypatch, tmp_path):
    # Default coupling (loopback) — the ring is inert, so a "mismatch" is not a live
    # defect. Must report ok/skip regardless of env or conf.d values.
    _stage(monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=2\n")
    # No _arm(): read_persisted_coupling resolves loopback from the (empty) file.
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok"
    assert "skipped" in res.detail
    assert "not armed" in res.detail


def test_ok_when_all_three_axes_agree(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path, capture_n_slots=8)
    _write_ring(program, n_slots=8)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "n_slots=8" in res.detail


def test_fail_when_env_disagrees_with_conf(monkeypatch, tmp_path):
    # The 2026-07-05 defect: stale JASPER_FANIN_RING_SLOTS=2 vs conf.d's 8.
    _arm(monkeypatch)
    _fanin, program = _stage(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=2\n",
        capture_n_slots=8,
    )
    _write_ring(program, n_slots=2)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "2" in res.detail and "8" in res.detail
    assert "crash-loop" in res.detail.lower()


def test_fail_when_on_disk_ring_disagrees(monkeypatch, tmp_path):
    # env + conf.d agree (both 8), but a stale on-disk ring carries 2 slots — the
    # ioplug attach still fails. Caught as the third axis.
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path, capture_n_slots=8)
    _write_ring(program, n_slots=2)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "on-disk" in res.detail.lower()
    assert "2" in res.detail and "8" in res.detail


def test_fail_when_on_disk_ring_period_disagrees(monkeypatch, tmp_path):
    # Nit-7: env + conf.d + on-disk n_slots all agree (8), but the on-disk ring's
    # period_frames is stale (256 vs conf.d 128). The ioplug attach still fails on
    # the SECOND geometry axis — the slot-only check would miss it. Caught now.
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path, capture_n_slots=8)
    _write_ring(program, n_slots=8, period_frames=256)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "period_frames" in res.detail
    assert "256" in res.detail and "128" in res.detail


def test_ok_when_slots_and_period_both_agree(monkeypatch, tmp_path):
    # The positive: n_slots AND period_frames coherent across env + conf.d + on-disk.
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path, capture_n_slots=8)
    _write_ring(program, n_slots=8, period_frames=128)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "period_frames=128" in res.detail


def test_warn_when_ring_file_absent_but_env_conf_agree(monkeypatch, tmp_path):
    # Armed, env + conf.d agree, but no valid on-disk ring yet (fan-in restarting).
    # Not a hard failure — the next writer create will be coherent.
    _arm(monkeypatch)
    _stage(monkeypatch, tmp_path, capture_n_slots=8)
    # No _write_ring — the program.ring path does not exist.
    res = audio.check_ring_geometry_coherence()
    assert res.status == "warn"
    assert "no valid ring header" in res.detail.lower()


def test_fail_when_env_value_invalid(monkeypatch, tmp_path):
    # An out-of-range JASPER_FANIN_RING_SLOTS is a hard failure while armed.
    _arm(monkeypatch)
    _stage(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=99\n",
        capture_n_slots=8,
    )
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "invalid" in res.detail.lower()
