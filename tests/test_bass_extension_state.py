# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.bass_extension import profile as profile_mod
from jasper.bass_extension.profile import BassExtensionEvaluation
from jasper.cli.doctor.audio import check_bass_extension_profile
from jasper.control import state_aggregate


def _doctor_result(monkeypatch, evaluation: BassExtensionEvaluation):
    import jasper.active_speaker.baseline_profile as baseline_mod
    import jasper.output_topology as topology_mod

    monkeypatch.setattr(
        profile_mod,
        "evaluate_bass_extension_profile",
        lambda **_kwargs: evaluation,
    )
    monkeypatch.setattr(
        baseline_mod,
        "load_applied_baseline_profile_state",
        lambda: None,
    )
    monkeypatch.setattr(topology_mod, "load_output_topology", lambda: None)
    return check_bass_extension_profile()


def test_doctor_missing_profile_is_ok(monkeypatch):
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation("missing", (), None, "profile is absent"),
    )
    assert result.status == "ok"
    assert "not commissioned" in result.detail


def test_doctor_malformed_profile_is_fail(monkeypatch):
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation("malformed", (), None, "invalid JSON at byte 4"),
    )
    assert result.status == "fail"
    assert "malformed" in result.detail
    assert "invalid JSON" in result.detail


def test_doctor_stale_profile_is_warn(monkeypatch):
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation(
            "stale",
            (),
            None,
            "baseline fingerprint mismatch; algorithm version mismatch",
        ),
    )
    assert result.status == "warn"
    assert "baseline fingerprint mismatch" in result.detail
    assert "algorithm version mismatch" in result.detail


def test_doctor_accepted_profile_is_ok_with_corners(monkeypatch):
    profile = SimpleNamespace(
        targets=[SimpleNamespace(fp_hz=31.0), SimpleNamespace(fp_hz=61.2)]
    )
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation("accepted", (), profile, "profile is accepted"),
    )
    assert result.status == "ok"
    assert "deepest=31Hz" in result.detail
    assert "natural=61.2Hz" in result.detail


def test_doctor_bypassed_profile_is_ok(monkeypatch):
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation("bypassed", (), SimpleNamespace(), "bypassed"),
    )
    assert result.status == "ok"
    assert "bypassed" in result.detail


class _FakeCamillaController:
    def __init__(self, **_kwargs):
        pass

    async def get_volume_db(self, **_kwargs):
        return None

    async def get_playback_rms(self, **_kwargs):
        return None

    async def get_playback_peak(self, **_kwargs):
        return None

    async def get_clipped_samples(self, **_kwargs):
        return None

    async def get_config_file_path(self, **_kwargs):
        return None


async def _state_snapshot(monkeypatch, tmp_path):
    import jasper.camilla as camilla_mod

    async def no_status(*_args, **_kwargs):
        return None

    async def no_mpris(*_args, **_kwargs):
        return None

    monkeypatch.setattr(camilla_mod, "CamillaController", _FakeCamillaController)
    monkeypatch.setattr(state_aggregate.mpris, "shairport_playing", no_mpris)
    monkeypatch.setattr(state_aggregate, "_audio_graph_state", lambda **_kwargs: None)
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "volume.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "spotify.json"))
    return await state_aggregate._get_state(
        camilla_host="127.0.0.1",
        camilla_port=1234,
        voice_socket_path=str(tmp_path / "voice.sock"),
        voice_socket_command=no_status,
        mux_socket_command=no_status,
        local_status_json=no_status,
        aec_full_status=lambda: {},
        dial_heartbeat={},
        read_transit_state_func=lambda: {"packs": []},
        ha_status_snapshot=lambda: {"configured": False, "connected": False},
    )


@pytest.mark.asyncio
async def test_state_bass_extension_section_is_populated(monkeypatch, tmp_path):
    summary = {
        "commissioned": True,
        "status": "accepted",
        "profile_id": "bex-123456789abc",
    }
    monkeypatch.setattr(profile_mod, "bass_extension_state_summary", lambda: summary)

    state = await _state_snapshot(monkeypatch, tmp_path)

    assert state["bass_extension"] == summary


@pytest.mark.asyncio
async def test_state_bass_extension_section_is_fail_soft(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("profile read failed")

    monkeypatch.setattr(profile_mod, "bass_extension_state_summary", boom)

    state = await _state_snapshot(monkeypatch, tmp_path)

    assert state["bass_extension"] is None
