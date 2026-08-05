# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ring-conf floor-render doctor check (PR-6).

``check_ring_conf_floor_render`` compares two facts, each read from its owner:
the active DAC profile's DECLARED ``LatencyFloor`` (the DAC registry) and the
``period_frames`` the ring conf.d pins (the file). The ring slot IS one outputd
DAC period, so a box whose DAC declares a floor should have that floor rendered
into its conf.d by ``jasper-audio-hardware-reconcile``; this check is the
standing surface that catches a box where that render did not land.

A DAC with no declared floor is ``ok`` by RULE, not by luck — the shipped
conf.d default stands and there is nothing to render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.audio_hardware.dac import LatencyFloor
from jasper.cli.doctor import audio_runtime as audio

SHIPPED_RING_CONF = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
)


def _stage(monkeypatch, tmp_path, *, dac_id, conf_text=None):
    conf = tmp_path / "60-jts-ring.conf"
    if conf_text is None:
        conf.write_bytes(SHIPPED_RING_CONF.read_bytes())
    else:
        conf.write_text(conf_text, encoding="utf-8")
    monkeypatch.setattr(audio, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio, "_active_audio_dac_id", lambda: dac_id)
    return conf


def _synthetic_floor(period_frames):
    return LatencyFloor(
        camilla_chunksize=256,
        camilla_target_level=1536,
        outputd_period_frames=period_frames,
        outputd_dac_buffer_frames=4 * period_frames,
    )


def test_ok_when_the_dac_declares_no_floor(monkeypatch, tmp_path):
    # State 1: nothing to render — the shipped default stands by rule.
    _stage(monkeypatch, tmp_path, dac_id="hifiberry_dac8x")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "no latency floor" in result.detail
    assert "shipped default" in result.detail


def test_ok_when_the_conf_matches_the_declared_floor(monkeypatch, tmp_path):
    # State 2: the golden Apple case — the declared floor IS the shipped 128.
    _stage(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "128" in result.detail
    assert "apple_usb_c_dongle" in result.detail


def test_warns_when_the_conf_diverges_from_the_declared_floor(
    monkeypatch, tmp_path
):
    # State 3: a declared floor the conf.d has not been rendered to. Warn, not
    # fail — the conf.d is inert unless shm_ring is armed, and the coupling
    # reconciler independently fail-closes to loopback on this mismatch.
    monkeypatch.setattr(
        audio, "latency_floor_for", lambda _id: _synthetic_floor(1024)
    )
    _stage(monkeypatch, tmp_path, dac_id="hifiberry_dac8x")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "warn"
    # Names BOTH numbers and the remedy, not a bare "mismatch".
    assert "128" in result.detail
    assert "1024" in result.detail
    assert "jasper-audio-hardware-reconcile" in result.detail


@pytest.mark.parametrize(
    "conf_text",
    [
        # Torn: the two PCMs disagree, so there is no single geometry.
        "pcm.jts_ring_capture {\n    period_frames 128\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 1024\n}\n",
        # No period_frames line at all.
        "pcm.jts_ring_capture { type jts_ring }\n",
    ],
)
def test_warns_when_the_conf_period_is_indeterminate(
    monkeypatch, tmp_path, conf_text
):
    monkeypatch.setattr(
        audio, "latency_floor_for", lambda _id: _synthetic_floor(1024)
    )
    _stage(
        monkeypatch, tmp_path, dac_id="hifiberry_dac8x", conf_text=conf_text
    )

    result = audio.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "no single period_frames" in result.detail
    assert "redeploy" in result.detail


def test_warns_when_the_conf_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        audio, "latency_floor_for", lambda _id: _synthetic_floor(1024)
    )
    monkeypatch.setattr(audio, "_JTS_RING_CONF_D", str(tmp_path / "missing.conf"))
    monkeypatch.setattr(audio, "_active_audio_dac_id", lambda: "hifiberry_dac8x")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "absent or torn" in result.detail


def test_check_is_registered_in_the_audio_doctor_group():
    from jasper.cli.doctor import audio as audio_group

    assert (
        audio_group.check_ring_conf_floor_render
        is audio.check_ring_conf_floor_render
    )
    assert "check_ring_conf_floor_render" in audio_group.__all__
