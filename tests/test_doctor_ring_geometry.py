# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ring-geometry-coherence doctor check (defect A).

``check_ring_geometry_coherence`` verifies the Ring-A geometry agrees across
three axes — fan-in's resolved ``JASPER_FANIN_RING_SLOTS`` through the systemd
env chain, the conf.d ``jts_ring_capture``, and the on-disk ``program.ring``
header — when shm_ring is armed. Against the on-disk header it compares every
axis the ioplug attach compares: ``n_slots``, ``period_frames``,
``sample_format`` and ``channels``. A mismatch on any of them is the 2026-07-05
crash-loop class (hw_params EINVAL + ioplug attach_fatal). It skips cleanly when
shm_ring is not armed (the ring is inert).
"""

from __future__ import annotations

import struct

import pytest

from jasper.cli.doctor import audio_runtime as audio


def _write_conf(tmp_path, *, capture_n_slots=2):
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        f"pcm.jts_ring_capture {{\n    period_frames 128\n    n_slots {capture_n_slots}\n}}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    return conf


def _write_ring(
    path,
    *,
    n_slots=2,
    period_frames=128,
    magic=0x4A52_494E,
    rate=48000,
    channels=2,
    sample_format=1,  # SAMPLE_FORMAT_S16LE
):
    hdr = bytearray(128)
    struct.pack_into("<I", hdr, 0, magic)
    struct.pack_into("<I", hdr, 4, 1)  # version
    struct.pack_into("<I", hdr, 8, rate)
    struct.pack_into("<I", hdr, 12, channels)
    struct.pack_into("<I", hdr, 16, sample_format)
    struct.pack_into("<I", hdr, 20, period_frames)
    struct.pack_into("<I", hdr, 24, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)


def _arm(monkeypatch):
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )


def _stage(
    monkeypatch,
    tmp_path,
    *,
    fanin_env_text="",
    jasper_env_text="",
    capture_n_slots=2,
):
    conf = _write_conf(tmp_path, capture_n_slots=capture_n_slots)
    fanin_env = tmp_path / "fanin.env"
    jasper_env = tmp_path / "jasper.env"
    fanin_env.write_text(fanin_env_text, encoding="utf-8")
    jasper_env.write_text(jasper_env_text, encoding="utf-8")
    program = tmp_path / "program.ring"
    monkeypatch.setattr(audio.ring_assets, "RING_CONF_D", str(conf))
    monkeypatch.setattr(audio, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio.ring_assets, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )
    return fanin_env, program


def test_skips_cleanly_when_not_armed(monkeypatch, tmp_path):
    # Default coupling (loopback) — the ring is inert, so a "mismatch" is not a live
    # defect. Must report ok/skip regardless of env or conf.d values.
    _stage(monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=8\n")
    # No _arm(): read_persisted_coupling resolves loopback from the (empty) file.
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok"
    assert "skipped" in res.detail
    assert "not armed" in res.detail


def test_ok_when_all_three_axes_agree(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path)
    _write_ring(program)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "n_slots=2" in res.detail


def test_ok_detail_reports_the_on_disk_wire(monkeypatch, tmp_path):
    """The ok detail names the wire it compared, not just the slot geometry.

    A reader that can see the whole header should say what it saw — otherwise
    "the Python layer can see the wire" is true only inside tests.
    """
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path)
    _write_ring(program)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "S16_LE/2ch" in res.detail
    assert "48000 Hz" in res.detail


@pytest.mark.parametrize(
    ("kwargs", "axis"),
    [
        ({"sample_format": 2}, "sample_format"),
        ({"channels": 6}, "channels"),
    ],
)
def test_fail_when_the_on_disk_wire_shears(monkeypatch, tmp_path, kwargs, axis):
    """THE HOLE R5b CLOSED, and this test used to assert it stayed open.

    Its earlier form wrote a 44.1 kHz / 6-channel / S32_LE header — a wire the
    fleet does not run — and asserted ``ok``, because the wire axes were
    REPORTED and compared nowhere. That is exactly the silent hole: a ring file
    whose slots and period match while its format or channel count does not
    passed every Python guard, and the first symptom would have been CamillaDSP
    failing the ioplug attach and crash-looping. The inversion is deliberate.

    ``rate`` is NOT in the parametrize list: the conf.d declares no rate, so
    there is no expected value to compare against and the comparator skips that
    axis rather than guessing. It stays reported.
    """
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path)
    _write_ring(program, **kwargs)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail", res.detail
    assert axis in res.detail
    assert "jasper-fanin-coupling-reconcile" in res.detail


def test_fail_when_env_disagrees_with_conf(monkeypatch, tmp_path):
    # Default migration class: stale JASPER_FANIN_RING_SLOTS=8 vs conf.d's 2.
    _arm(monkeypatch)
    _fanin, program = _stage(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=8)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "2" in res.detail and "8" in res.detail
    assert "crash-loop" in res.detail.lower()


def test_fail_when_base_env_disagrees_with_conf(monkeypatch, tmp_path):
    # The effective env chain is /etc/jasper/jasper.env, then fanin.env. A stale
    # base-env value still controls the next fan-in start when fanin.env has no
    # override, so doctor must not report the product default.
    _arm(monkeypatch)
    _fanin, program = _stage(
        monkeypatch,
        tmp_path,
        jasper_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=8)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "2" in res.detail and "8" in res.detail
    assert "crash-loop" in res.detail.lower()


def test_ok_when_fanin_env_overrides_stale_base_env(monkeypatch, tmp_path):
    # Later systemd EnvironmentFile wins. The reconciler's fix writes this exact
    # fanin.env override to neutralize stale /etc/jasper/jasper.env residue.
    _arm(monkeypatch)
    _fanin, program = _stage(
        monkeypatch,
        tmp_path,
        fanin_env_text="JASPER_FANIN_RING_SLOTS=2\n",
        jasper_env_text="JASPER_FANIN_RING_SLOTS=8\n",
    )
    _write_ring(program, n_slots=2)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "n_slots=2" in res.detail


def test_fail_when_on_disk_ring_disagrees(monkeypatch, tmp_path):
    # env + conf.d agree (both 2), but a stale on-disk ring carries 8 slots — the
    # ioplug attach still fails. Caught as the third axis.
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path)
    _write_ring(program, n_slots=8)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "on-disk" in res.detail.lower()
    assert "2" in res.detail and "8" in res.detail


def test_fail_when_on_disk_ring_period_disagrees(monkeypatch, tmp_path):
    # Nit-7: env + conf.d + on-disk n_slots all agree (2), but the on-disk ring's
    # period_frames is stale (256 vs conf.d 128). The ioplug attach still fails on
    # the SECOND geometry axis — the slot-only check would miss it. Caught now.
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path)
    _write_ring(program, period_frames=256)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "period_frames" in res.detail
    assert "256" in res.detail and "128" in res.detail


def test_ok_when_slots_and_period_both_agree(monkeypatch, tmp_path):
    # The positive: n_slots AND period_frames coherent across env + conf.d + on-disk.
    _arm(monkeypatch)
    _fanin, program = _stage(monkeypatch, tmp_path)
    _write_ring(program, period_frames=128)
    res = audio.check_ring_geometry_coherence()
    assert res.status == "ok", res.detail
    assert "period_frames=128" in res.detail


def test_warn_when_ring_file_absent_but_env_conf_agree(monkeypatch, tmp_path):
    # Armed, env + conf.d agree, but no valid on-disk ring yet (fan-in restarting).
    # Not a hard failure — the next writer create will be coherent.
    _arm(monkeypatch)
    _stage(monkeypatch, tmp_path)
    # No _write_ring — the program.ring path does not exist.
    res = audio.check_ring_geometry_coherence()
    assert res.status == "warn"
    assert "no valid ring header" in res.detail.lower()


def test_fail_when_env_value_invalid(monkeypatch, tmp_path):
    # An out-of-range JASPER_FANIN_RING_SLOTS is a hard failure while armed.
    _arm(monkeypatch)
    _stage(
        monkeypatch, tmp_path, fanin_env_text="JASPER_FANIN_RING_SLOTS=99\n",
    )
    res = audio.check_ring_geometry_coherence()
    assert res.status == "fail"
    assert "invalid" in res.detail.lower()


# --- the outputd buffer-health check's RING-WIRE branches --------------------
#
# `_outputd_buffer_health` validates the shm_ring content geometry and then
# compares the wire outputd ATTACHED to (published in its top-level `shm_ring`
# block) against the wire this box's resolver answers. Those are two independent
# sources, so a disagreement is a real shear — outputd reading a geometry nobody
# declared — and it is a `fail`, not a detail line. Both axes are pinned because
# they fail for different reasons and print different remedies.


def _outputd_ring_status(*, fmt="S16_LE", channels=2, period=128, slots=2):
    """A STATUS payload for an ATTACHED shm_ring outputd, per rust state.rs."""
    return {
        "content": {
            "source": "shm_ring",
            "buffer_frames": period,
            "ring": {
                "slots": slots,
                "slot_frames": period,
                "capacity_frames": slots * period,
            },
        },
        "shm_ring": {
            "enabled": True,
            "attached": True,
            "slots": slots,
            "format": fmt,
            "channels": channels,
            "occupancy": 1,
        },
    }


def _buffer_health(data, *, period=128):
    return audio._outputd_buffer_health(
        data,
        data["content"],
        ring_mode=True,
        content_buffer=data["content"]["buffer_frames"],
        dac_buffer=period * 4,
        period_frames=period,
    )


def test_buffer_health_passes_when_the_attached_wire_matches():
    """POSITIVE CONTROL. On the box's default (undeclared) wire the comparison
    is silent, so the two failure pins below are proving a branch rather than a
    broken happy path.

    ``fmt="S32_LE"`` because the resolver's default went WIDE
    (``jasper.fanin_coupling.resolve_ring_wire_format``): an undeclared box —
    this test stubs neither the wire nor the env chain — now resolves S32_LE,
    not the C ioplug's compiled-in S16_LE.
    """
    result = _buffer_health(_outputd_ring_status(fmt="S32_LE"))
    assert isinstance(result, str), result
    assert "shm_ring_wire=S32_LE/2ch" in result
    assert "shm_ring_attached=True" in result


def test_buffer_health_fails_on_an_attached_format_the_box_does_not_declare():
    """outputd attached to a Ring B FORMAT nobody declared.

    S16_LE is the mismatching token now: the resolver's default went WIDE, so
    an undeclared box's wire IS S32_LE and S16_LE is what nobody declares.
    """
    result = _buffer_health(_outputd_ring_status(fmt="S16_LE"))
    assert not isinstance(result, str), "a wire shear must be a CheckResult, not detail"
    assert result.status == "fail"
    assert "shm_ring.format='S16_LE'" in result.detail
    assert "nobody declared" in result.detail
    # The remedy must name the reconcile that CLEARS the mismatched file — a bare
    # "redeploy" leaves the stale ring in place and the box parks again.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_buffer_health_fails_on_an_attached_channel_count_the_box_does_not_declare():
    """The channels axis has teeth independently of the format axis.

    ``fmt="S32_LE"`` pins the format to the box's default so that axis stays
    silent — an unpinned S16_LE default would (now) ALSO mismatch the format
    axis, and this test would no longer isolate the channels comparison.
    """
    result = _buffer_health(_outputd_ring_status(fmt="S32_LE", channels=6))
    assert not isinstance(result, str)
    assert result.status == "fail"
    assert "shm_ring.channels=6" in result.detail
    assert "nobody declared" in result.detail


def test_buffer_health_skips_the_wire_comparison_before_attach():
    """Only checked once ATTACHED.

    Before the attach outputd publishes its own DECLARATION, which proves
    nothing about a ring that does not exist yet. Comparing there would fail a
    box that is merely starting up — and it would do so with the shear remedy,
    sending an operator to clear a ring file that is not the problem.
    """
    data = _outputd_ring_status(fmt="S32_LE", channels=6)
    data["shm_ring"]["attached"] = False
    result = _buffer_health(data)
    assert isinstance(result, str), result
    assert "shm_ring_attached=False" in result


def test_buffer_health_resolves_the_wire_with_the_boxs_topology(monkeypatch):
    """The comparison asks the SAME question the reconciler's gates ask.

    ``ring_b_channels`` is the one per-topology axis in the wire, so resolving it
    without the topology answers the shipped stereo declaration and would report
    a shear on a box whose Ring B legitimately carries a different width — the
    doctor contradicting the reconciler that armed it.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.fanin_coupling as fc

    sentinel = object()
    monkeypatch.setattr(cr, "load_topology_for_wire", lambda: sentinel)
    seen: list[object] = []

    def _resolve(topology=None):
        seen.append(topology)
        return fc.RingWire(
            sample_format="S16_LE",
            ring_a_channels=2,
            ring_b_channels=6,
            period_frames=128,
        )

    monkeypatch.setattr(fc, "resolve_ring_wire", _resolve)
    # 6 channels is a SHEAR against the shipped resolver and a MATCH against this
    # topology-derived one, so a passing result proves the topology was threaded.
    result = _buffer_health(_outputd_ring_status(channels=6))
    assert seen == [sentinel], "the buffer-health wire must be topology-resolved"
    assert isinstance(result, str), result
