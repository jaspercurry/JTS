# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace


from jasper.bass_extension import profile as profile_mod
from jasper.bass_extension.profile import BassExtensionEvaluation, BassExtensionRefusal
from jasper.cli.doctor import active_speaker as doctor_audio
from jasper.cli.doctor.active_speaker import check_bass_extension_profile


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
