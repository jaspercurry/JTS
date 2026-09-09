# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace


from jasper.active_speaker import setup_status
from jasper.bass_extension import profile as profile_mod
from jasper.bass_extension.candidate_field import bass_extension_summary
from jasper.bass_extension.profile import BassExtensionEvaluation, BassExtensionRefusal
from jasper.cli.doctor import active_speaker as doctor_audio
from jasper.cli.doctor.active_speaker import check_bass_extension_profile
from jasper.control import state_aggregate

from tests.test_bass_extension_candidate_field import bass_extension_field


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
    assert result.reason == doctor_audio.REASON_BASS_EXTENSION_NOT_COMMISSIONED


def test_doctor_malformed_profile_is_fail(monkeypatch):
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation("malformed", (), None, "invalid JSON at byte 4"),
    )
    assert result.status == "fail"
    assert result.reason == doctor_audio.REASON_BASS_EXTENSION_MALFORMED


def test_doctor_stale_profile_is_warn(monkeypatch):
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation(
            "stale",
            (
                BassExtensionRefusal.BASELINE_NOT_APPLIED,
                BassExtensionRefusal.PROFILE_STALE,
            ),
            None,
            "baseline fingerprint mismatch; algorithm version mismatch",
        ),
    )
    assert result.status == "warn"
    assert result.reason == doctor_audio.REASON_BASS_EXTENSION_STALE


def test_doctor_accepted_profile_is_ok_with_corners(monkeypatch):
    profile = SimpleNamespace(
        targets=[SimpleNamespace(fp_hz=31.0), SimpleNamespace(fp_hz=61.2)]
    )
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation("accepted", (), profile, "profile is accepted"),
    )
    assert result.status == "ok"


def test_doctor_bypassed_profile_is_ok(monkeypatch):
    result = _doctor_result(
        monkeypatch,
        BassExtensionEvaluation("bypassed", (), SimpleNamespace(), "bypassed"),
    )
    assert result.status == "ok"
    assert result.reason == doctor_audio.REASON_BASS_EXTENSION_BYPASSED


class _FakeCamillaController:
    def __init__(self, **_kwargs):
        pass

    async def get_volume_db(self, **_kwargs):
        return None

    async def get_playback_rms_all(self, **_kwargs):
        return None

    async def get_playback_peak_all(self, **_kwargs):
        return None

    async def get_clipped_samples(self, **_kwargs):
        return None

    async def get_config_file_path(self, **_kwargs):
        return None


async def _state_snapshot(monkeypatch, tmp_path):
    import jasper.camilla as camilla_mod

    async def no_status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(camilla_mod, "CamillaController", _FakeCamillaController)
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "volume.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "spotify.env"))
    return await state_aggregate._get_state(
        camilla_host="127.0.0.1",
        camilla_port=1234,
        voice_socket_path=str(tmp_path / "voice.sock"),
        voice_socket_command=no_status,
        mux_socket_command=no_status,
        local_status_json=no_status,
        aec_full_status=lambda: {},
        read_transit_state_func=lambda: {"packs": []},
        ha_status_snapshot=lambda: {"configured": False, "connected": False},
    )


def _applied_setup(monkeypatch, tmp_path, field, *, recovery: bool = False) -> None:
    """The setup snapshot `/state` projects its bass block from."""
    intent = tmp_path / "apply_intent.json"
    if recovery:
        intent.write_text("{}")
    monkeypatch.setattr(setup_status, "BASS_EXTENSION_APPLY_INTENT_PATH", intent)
    monkeypatch.setattr(
        state_aggregate,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {"protected_profile": {"bass_extension": field}},
    )


async def test_state_bass_extension_is_the_applied_familys_block(
    monkeypatch, tmp_path
):
    field = bass_extension_field()
    _applied_setup(monkeypatch, tmp_path, field)

    state = await _state_snapshot(monkeypatch, tmp_path)

    section = dict(state["bass_extension"])
    section.pop("observed_at")  # every /state section is stamped (issue #4197)
    assert section["status"] == "accepted"
    assert section["runtime_eligible"] is True
    assert section["apply_recovery_required"] is False
    assert section["natural_hz"] == field["rungs"][-1]["target"]["fp_hz"]
    assert section["family"] == bass_extension_summary(field)


async def test_state_bass_extension_is_null_without_an_applied_family(
    monkeypatch, tmp_path
):
    _applied_setup(monkeypatch, tmp_path, None)

    state = await _state_snapshot(monkeypatch, tmp_path)

    assert state["bass_extension"] is None


async def test_state_bass_extension_still_reports_an_interrupted_apply(
    monkeypatch, tmp_path
):
    """The recovery banner outlives the profile it was applying."""
    _applied_setup(monkeypatch, tmp_path, None, recovery=True)

    state = await _state_snapshot(monkeypatch, tmp_path)

    assert state["bass_extension"]["commissioned"] is False
    assert state["bass_extension"]["apply_recovery_required"] is True
