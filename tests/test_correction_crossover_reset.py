# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Scoped "start over" for the crossover flow (``POST /crossover/reset``).

Pins the KEEP/CLEAR split for the in-flow reset that restarts the guided
measurement journey without losing driver research or disturbing whatever
audio graph is currently applied/loaded — see
``jasper.active_speaker.reset.clear_active_speaker_measurement_journey`` and
docs/HANDOFF-correction.md "Scoped crossover reset".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.web import correction_crossover_backend as backend
from jasper.web import correction_crossover_flow as flow

_JOURNEY_ENVS = {
    "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE": "preview.json",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "staged.json",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path-safety.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE": "commission-load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE": "commission-ramp.json",
    "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE": "measurements.json",
}
_KEPT_ENVS = {
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design.json",
    "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE": "startup-load.json",
    "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE": "baseline.json",
}


def _seed(monkeypatch, tmp_path: Path) -> None:
    for env_name, filename in {**_JOURNEY_ENVS, **_KEPT_ENVS}.items():
        path = tmp_path / filename
        path.write_text('{"seed": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))


def test_reset_measurement_journey_clears_journey_keeps_driver_and_applied_state(
    monkeypatch, tmp_path: Path,
) -> None:
    _seed(monkeypatch, tmp_path)
    fresh_lease = backend.CrossoverLevelLease()
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    result = backend.reset_measurement_journey()

    assert result["status"] == "cleared"
    # cleared_ids reflects the ACTUAL unlinks (all six existed), not the
    # static intent; missing/errors are empty on a clean clear.
    assert sorted(result["cleared_ids"]) == [
        "commission_load",
        "commission_ramp",
        "crossover_preview",
        "measurements",
        "path_safety",
        "staged_config",
    ]
    assert result["missing_ids"] == []
    assert result["error_ids"] == []
    assert sorted(result["kept_ids"]) == [
        "baseline_profile",
        "design_draft",
        "startup_load",
    ]
    for filename in _JOURNEY_ENVS.values():
        assert not (tmp_path / filename).exists()
    for filename in _KEPT_ENVS.values():
        assert (tmp_path / filename).exists()


def test_reset_measurement_journey_reports_actual_outcome_not_static_intent(
    monkeypatch, tmp_path: Path,
) -> None:
    """An already-absent journey file lands in missing_ids, not cleared_ids —
    the summary is the real outcome, so a partial state can never be painted
    as a full green clear (adversarial-review N1)."""
    _seed(monkeypatch, tmp_path)
    # Remove one journey file before the reset so it is already absent.
    (tmp_path / _JOURNEY_ENVS["JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE"]).unlink()
    fresh_lease = backend.CrossoverLevelLease()
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    result = backend.reset_measurement_journey()

    assert result["status"] == "cleared"  # already-absent is not an error
    assert "measurements" not in result["cleared_ids"]
    assert result["missing_ids"] == ["measurements"]
    assert result["error_ids"] == []


def test_reset_measurement_journey_refuses_when_volume_safety_unresolved(
    monkeypatch, tmp_path: Path,
) -> None:
    _seed(monkeypatch, tmp_path)
    fresh_lease = backend.CrossoverLevelLease()
    fresh_lease._volume_safety_state = {"status": "unresolved"}
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    with pytest.raises(backend.MeasurementJourneyResetRefused) as exc_info:
        backend.reset_measurement_journey()

    assert exc_info.value.reason == "crossover_volume_safety_unresolved"
    # Fail-closed: nothing was cleared.
    for filename in _JOURNEY_ENVS.values():
        assert (tmp_path / filename).exists()


def test_reset_measurement_journey_refuses_while_level_match_still_running(
    monkeypatch, tmp_path: Path,
) -> None:
    _seed(monkeypatch, tmp_path)
    fresh_lease = backend.CrossoverLevelLease()
    fresh_lease._running = object()  # sentinel: a level match is in flight
    monkeypatch.setattr(backend, "level_lease", lambda: fresh_lease)

    with pytest.raises(backend.MeasurementJourneyResetRefused) as exc_info:
        backend.reset_measurement_journey()

    assert exc_info.value.reason == "measurement_in_progress"
    for filename in _JOURNEY_ENVS.values():
        assert (tmp_path / filename).exists()


def test_handle_reset_maps_refusal_to_409(monkeypatch) -> None:
    def fake_reset() -> dict:
        raise backend.MeasurementJourneyResetRefused(
            "a crossover measurement is still stopping; try Start over again "
            "in a moment",
            reason="measurement_in_progress",
        )

    monkeypatch.setattr(backend, "reset_measurement_journey", fake_reset)

    payload, status = flow.handle_reset()

    assert status == 409
    assert payload["status"] == "refused"
    assert payload["reason"] == "measurement_in_progress"


def test_handle_reset_returns_fresh_envelope_with_honest_reset_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        backend,
        "reset_measurement_journey",
        lambda: {
            "status": "partial",
            "cleared_ids": ["crossover_preview"],
            "missing_ids": ["staged_config"],
            "error_ids": ["measurements"],
            "kept_ids": ["design_draft", "baseline_profile", "startup_load"],
        },
    )
    monkeypatch.setattr(flow, "handle_status", lambda *, relay=None: ({}, 200))
    monkeypatch.setattr(flow, "_active_group_member", lambda: False)
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_envelope.build_crossover_envelope_logged",
        lambda status: {
            "screen": "start",
            "active": True,
            "steps": [],
            "nudges": [],
        },
    )

    payload, status = flow.handle_reset()

    assert status == 200
    assert payload["screen"] == "start"
    assert payload["grouping_member"] is False
    # The honest outcome is surfaced verbatim, including the partial status
    # and the errored file — the page branches on status != "cleared".
    assert payload["reset"] == {
        "status": "partial",
        "cleared": ["crossover_preview"],
        "missing": ["staged_config"],
        "errors": ["measurements"],
        "kept": ["design_draft", "baseline_profile", "startup_load"],
    }


def test_handle_envelope_carries_grouping_member_flag(monkeypatch) -> None:
    """The polled envelope carries the grouping-member flag the grouping-aware
    Start-over confirm copy reads (adversarial-review S1b)."""
    monkeypatch.setattr(flow, "handle_status", lambda *, relay=None: ({}, 200))
    monkeypatch.setattr(flow, "_active_group_member", lambda: True)
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_envelope.build_crossover_envelope_logged",
        lambda status: {"screen": "start", "active": True, "steps": [], "nudges": []},
    )

    payload, status = flow.handle_envelope()

    assert status == 200
    assert payload["grouping_member"] is True


def test_active_group_member_reads_grouping_config(monkeypatch) -> None:
    """_active_group_member is a thin read of the declared grouping config:
    True for an active leader OR bonded follower, False for solo. load_config
    is total (never raises), so the only failure the helper guards is an
    ImportError of the multiroom module, which fails open to the solo copy."""
    import jasper.multiroom.config as grouping_config

    monkeypatch.setattr(grouping_config, "load_config", lambda: object())
    monkeypatch.setattr(grouping_config, "is_bonded_follower", lambda cfg: False)

    monkeypatch.setattr(grouping_config, "is_active_leader", lambda cfg: True)
    assert flow._active_group_member() is True

    monkeypatch.setattr(grouping_config, "is_active_leader", lambda cfg: False)
    assert flow._active_group_member() is False

    monkeypatch.setattr(grouping_config, "is_bonded_follower", lambda cfg: True)
    assert flow._active_group_member() is True

    # Fail-open to the solo copy if the multiroom module can't be imported
    # (the `from jasper.multiroom.config import ...` line is the only raiser).
    monkeypatch.setattr(grouping_config, "is_bonded_follower", lambda cfg: False)
    monkeypatch.delattr(grouping_config, "is_active_leader")
    assert flow._active_group_member() is False
