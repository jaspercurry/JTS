# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.active_speaker import setup_status
from jasper.bass_extension.candidate_field import bass_extension_summary
from jasper.bass_extension.refusals import BassExtensionRefusal
from jasper.cli.doctor import active_speaker as doctor_audio
from jasper.cli.doctor.active_speaker import check_bass_extension_profile

from tests.test_bass_extension_candidate_field import bass_extension_field


def _doctor_result(monkeypatch, applied):
    import jasper.active_speaker.baseline_profile as baseline_mod

    monkeypatch.setattr(
        baseline_mod, "load_applied_baseline_profile_state", lambda: applied
    )
    return check_bass_extension_profile()


def test_doctor_reads_the_applied_candidates_family(monkeypatch):
    field = bass_extension_field()
    result = _doctor_result(
        monkeypatch, {"recomposition_snapshot": {"bass_extension": field}}
    )

    assert result.status == "ok"
    assert result.reason == ""
    natural = field["rungs"][-1]["target"]["fp_hz"]
    assert f"natural={natural:g}Hz" in result.detail


@pytest.mark.parametrize(
    "applied",
    [None, {"recomposition_snapshot": {}}],
)
def test_doctor_reports_no_family_when_none_is_applied(monkeypatch, applied):
    result = _doctor_result(monkeypatch, applied)

    assert result.status == "ok"
    assert result.reason == doctor_audio.REASON_BASS_EXTENSION_NOT_COMMISSIONED


def test_doctor_fails_on_a_family_it_cannot_read(monkeypatch):
    """An unreadable family is not an uncommissioned one: the runtime proof
    refuses this speaker's graph, and the doctor must say which bound broke."""
    result = _doctor_result(
        monkeypatch,
        {"recomposition_snapshot": {"bass_extension": {"owner": "wrong shape"}}},
    )

    assert result.status == "fail"
    assert result.reason == doctor_audio.REASON_BASS_EXTENSION_MALFORMED
    assert BassExtensionRefusal.FIELD_MALFORMED in result.detail


def _setup(field) -> dict:
    return {"protected_profile": {"bass_extension": field}}


def test_the_owning_module_publishes_the_applied_family(monkeypatch, tmp_path):
    """`/state` carries thirteen keys and this is not one (ADR-0270 rule 1),
    so the bass tab and the doctor read the family from setup_status."""
    field = bass_extension_field()
    monkeypatch.setattr(
        setup_status, "BASS_EXTENSION_APPLY_INTENT_PATH", tmp_path / "absent.json"
    )

    section = setup_status.bass_extension_state(_setup(field))

    assert section["status"] == "accepted"
    assert section["runtime_eligible"] is True
    assert section["apply_recovery_required"] is False
    assert section["natural_hz"] == field["rungs"][-1]["target"]["fp_hz"]
    assert section["family"] == bass_extension_summary(field)


def test_the_family_is_none_without_an_applied_one(monkeypatch, tmp_path):
    monkeypatch.setattr(
        setup_status, "BASS_EXTENSION_APPLY_INTENT_PATH", tmp_path / "absent.json"
    )

    assert setup_status.bass_extension_state(_setup(None)) is None


def test_an_interrupted_apply_is_reported_without_a_family(monkeypatch, tmp_path):
    """The recovery banner outlives the profile it was applying."""
    intent = tmp_path / "apply_intent.json"
    intent.write_text("{}")
    monkeypatch.setattr(setup_status, "BASS_EXTENSION_APPLY_INTENT_PATH", intent)

    section = setup_status.bass_extension_state(_setup(None))

    assert section["commissioned"] is False
    assert section["apply_recovery_required"] is True
