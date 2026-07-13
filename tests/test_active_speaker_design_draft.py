# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jasper.active_speaker import (
    DESIGN_DRAFT_KIND,
    DRIVER_RESEARCH_KIND,
    ActiveSpeakerDesignDraftError,
    build_design_draft,
    load_design_draft,
    save_design_draft,
)
from jasper.active_speaker.design_draft import (
    ActiveSpeakerDesignDraftRevisionConflict,
    _normalise_candidate,
)
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology


def _topology() -> OutputTopology:
    return mono_output_topology(card_id=None)


def _research() -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [
            {
                "role": "woofer",
                "model": "Epique E150HE-44",
                "manufacturer": "Dayton Audio",
                "nominal_impedance_ohm": 4,
                "usable_frequency_range_hz": [45, 5000],
                "recommended_lowpass_hz": 2500,
                "sources": ["https://example.test/woofer"],
            },
            {
                "role": "tweeter",
                "model": "F110M-8",
                "manufacturer": "Eminence",
                "nominal_impedance_ohm": 8,
                "recommended_highpass_hz": 2500,
                "do_not_test_below_hz": 1200,
                "gain_offset_db": -18.5,
                "sources": ["https://example.test/tweeter"],
            },
        ],
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
                "rationale": "conservative starting point",
                "warnings": ["verify acoustic response before final use"],
            }
        ],
        "human_review": {
            "must_verify_wiring": True,
            "must_start_quiet": True,
            "needs_measurement_before_final": True,
        },
    }


def test_design_draft_persists_research_without_authorizing_audio(tmp_path: Path):
    path = tmp_path / "active_speaker_design_draft.json"

    payload = save_design_draft(
        _topology(),
        driver_research=_research(),
        operator_inputs={
            "woofer": "Dayton Epique E150HE-44",
            "tweeter": "Eminence F110M-8",
            "notes": "bench bring-up",
        },
        path=path,
        created_at="2026-06-10T12:00:00Z",
    )
    loaded = load_design_draft(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert payload["kind"] == DESIGN_DRAFT_KIND
    assert payload["status"] == "ready_for_review"
    assert payload["summary"]["driver_count"] == 2
    assert payload["summary"]["crossover_candidate_count"] == 1
    assert payload["summary"]["missing_research_roles"] == []
    assert payload["driver_research"]["drivers"][1]["gain_offset_db"] == -18.5
    assert (
        payload["driver_research"]["drivers"][1]["gain_offset_db_provenance"]
        == "research_estimate"
    )
    assert payload["permissions"]["may_not_load_camilla"] is True
    assert payload["permissions"]["may_not_emit_audio"] is True
    assert payload["safety"]["no_audio"] is True
    assert payload["safety"]["applies_filters"] is False
    assert loaded["status"] == "ready_for_review"
    assert raw["operator_inputs"]["tweeter"] == "Eminence F110M-8"


def test_driver_research_cannot_weaken_human_review_requirements():
    raw = _research()
    raw["human_review"] = {
        "must_verify_wiring": False,
        "must_start_quiet": False,
        "needs_measurement_before_final": False,
    }

    payload = build_design_draft(_topology(), driver_research=raw)

    assert payload["driver_research"]["human_review"] == {
        "must_verify_wiring": True,
        "must_start_quiet": True,
        "needs_measurement_before_final": True,
    }


def test_driver_research_notes_allow_detailed_safety_summary():
    raw = _research()
    raw["drivers"][1]["notes"] = "x" * 2048

    payload = build_design_draft(_topology(), driver_research=raw)

    assert len(payload["driver_research"]["drivers"][1]["notes"]) == 2048


def test_driver_research_notes_remain_bounded():
    raw = _research()
    raw["drivers"][1]["notes"] = "x" * 2049

    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="driver.notes must be <= 2048 chars",
    ):
        build_design_draft(_topology(), driver_research=raw)


def test_manual_driver_notes_use_same_bound():
    manual_settings = {
        "drivers": [
            {"role": "tweeter", "notes": "x" * 2048},
        ],
        "crossover_candidates": [],
    }

    payload = build_design_draft(_topology(), manual_settings=manual_settings)

    assert len(payload["manual_settings"]["drivers"][0]["notes"]) == 2048

    manual_settings["drivers"][0]["notes"] = "x" * 2049
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="manual_settings.driver.notes must be <= 2048 chars",
    ):
        build_design_draft(_topology(), manual_settings=manual_settings)


def test_research_and_manual_drivers_share_field_normalisation() -> None:
    common = {
        "role": "woofer",
        "model": "Shared Model",
        "manufacturer": "Example Audio",
        "nominal_impedance_ohm": 4,
        "sensitivity_db_2v83_1m": 88.5,
        "usable_frequency_range_hz": [40, 4500],
        "recommended_highpass_hz": 35,
        "recommended_lowpass_hz": 2600,
        "do_not_test_below_hz": 25,
        "gain_offset_db": -2.5,
        "notes": "same normalized fields",
        "sources": ["https://example.test/woofer"],
    }
    research = _research()
    research["drivers"] = [common]
    research["crossover_candidates"] = []
    research_driver = build_design_draft(
        _topology(),
        driver_research=research,
    )["driver_research"]["drivers"][0]
    manual_driver = build_design_draft(
        _topology(),
        manual_settings={"drivers": [common], "crossover_candidates": []},
    )["manual_settings"]["drivers"][0]

    assert research_driver["gain_offset_db_provenance"] == "research_estimate"
    assert manual_driver["gain_offset_db_provenance"] == "operator_pinned"
    assert research_driver["sources"] == ["https://example.test/woofer"]
    assert "sources" not in manual_driver
    for field in common.keys() - {"sources"}:
        assert research_driver[field] == manual_driver[field]


def test_research_requires_model_while_manual_driver_does_not() -> None:
    research = _research()
    research["drivers"][0].pop("model")
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match=r"^driver\.model is required$",
    ):
        build_design_draft(_topology(), driver_research=research)

    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {
                    "role": "woofer",
                    "notes": "operator knows the installed driver",
                    "sources": ["https://example.test/not-retained"],
                }
            ],
            "crossover_candidates": [],
        },
    )
    manual_driver = payload["manual_settings"]["drivers"][0]
    assert manual_driver["role"] == "woofer"
    assert manual_driver["notes"] == "operator knows the installed driver"
    assert "model" not in manual_driver
    assert "sources" not in manual_driver


def test_manual_crossover_settings_can_replace_ai_research():
    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {
                    "role": "woofer",
                    "model": "Epique E150HE-44",
                    "sensitivity_db_2v83_1m": 83.3,
                },
                {
                    "role": "tweeter",
                    "model": "Eminence F110M-8",
                    "sensitivity_db_2v83_1m": 108.0,
                    "do_not_test_below_hz": 1800,
                    "gain_offset_db": -24.7,
                },
            ],
            "crossover_candidates": [
                {
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2200,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                }
            ],
        },
    )

    assert payload["status"] == "ready_for_review"
    assert payload["driver_research"] is None
    assert payload["summary"]["manual_driver_count"] == 2
    assert payload["summary"]["manual_crossover_candidate_count"] == 1
    assert payload["summary"]["missing_driver_info_roles"] == []
    assert payload["summary"]["missing_crossover_candidate_pairs"] == []
    assert "driver_research_missing" in {issue["code"] for issue in payload["issues"]}
    assert (
        payload["manual_settings"]["drivers"][1]["gain_offset_db_provenance"]
        == "operator_pinned"
    )


def test_ui_suggested_gain_provenance_survives_normalisation():
    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {
                    "role": "tweeter",
                    "model": "F110M-8",
                    "gain_offset_db": -24.7,
                    "gain_offset_db_provenance": "sensitivity_estimate",
                }
            ],
            "crossover_candidates": [],
        },
    )

    assert (
        payload["manual_settings"]["drivers"][0]["gain_offset_db_provenance"]
        == "sensitivity_estimate"
    )


def test_design_draft_without_research_is_honest_needs_research():
    payload = build_design_draft(
        _topology(),
        operator_inputs={"woofer": "Epique", "tweeter": "F110M-8"},
        created_at="2026-06-10T12:00:00Z",
    )

    assert payload["status"] == "needs_research"
    assert payload["driver_research"] is None
    assert payload["summary"]["missing_driver_info_roles"] == ["woofer", "tweeter"]
    assert "driver_research_missing" in {issue["code"] for issue in payload["issues"]}


def test_design_draft_rejects_unsupported_research_shape():
    raw = _research()
    raw["kind"] = "not_jts"

    with pytest.raises(ActiveSpeakerDesignDraftError):
        build_design_draft(_topology(), driver_research=raw)


def test_load_design_draft_fails_soft_on_unsupported_schema(tmp_path: Path):
    path = tmp_path / "active_speaker_design_draft.json"
    path.write_text(
        json.dumps({"artifact_schema_version": 99, "kind": DESIGN_DRAFT_KIND}),
        encoding="utf-8",
    )

    payload = load_design_draft(path)

    assert payload["status"] == "unreadable"
    assert payload["issues"][0]["code"] == "design_draft_unsupported_schema"


def test_design_draft_revision_is_monotonic_and_refuses_stale_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active_speaker_design_draft.json"
    first = save_design_draft(
        _topology(),
        operator_inputs={"notes": "first"},
        expected_revision=0,
        path=path,
        created_at="2026-07-13T12:00:00Z",
    )
    second = save_design_draft(
        _topology(),
        operator_inputs={"notes": "second"},
        expected_revision=1,
        path=path,
        created_at="2026-07-13T12:01:00Z",
    )

    assert first["revision"] == 1
    assert second["revision"] == 2
    with pytest.raises(ActiveSpeakerDesignDraftRevisionConflict) as caught:
        save_design_draft(
            _topology(),
            operator_inputs={"notes": "stale"},
            expected_revision=1,
            path=path,
            created_at="2026-07-13T12:02:00Z",
        )
    assert caught.value.current_draft["revision"] == 2
    assert load_design_draft(path)["operator_inputs"]["notes"] == "second"


def test_concurrent_design_draft_writes_allow_exactly_one_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active_speaker_design_draft.json"
    barrier = threading.Barrier(2)

    def writer(label: str) -> tuple[str, object]:
        barrier.wait(timeout=5)
        try:
            return (
                "saved",
                save_design_draft(
                    _topology(),
                    operator_inputs={"notes": label},
                    expected_revision=0,
                    path=path,
                    created_at="2026-07-13T12:00:00Z",
                ),
            )
        except ActiveSpeakerDesignDraftRevisionConflict as exc:
            return ("conflict", exc.current_draft)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(writer, ("left", "right")))

    assert sorted(status for status, _ in results) == ["conflict", "saved"]
    saved = next(payload for status, payload in results if status == "saved")
    conflict = next(payload for status, payload in results if status == "conflict")
    assert isinstance(saved, dict)
    assert isinstance(conflict, dict)
    assert saved["revision"] == 1
    assert conflict["revision"] == 1
    loaded = load_design_draft(path)
    assert loaded["revision"] == 1
    assert loaded["operator_inputs"] == saved["operator_inputs"]


def test_legacy_draft_loads_as_revision_zero_and_boolean_revision_fails_soft(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active_speaker_design_draft.json"
    path.write_text(
        json.dumps(
            {
                "artifact_schema_version": 1,
                "kind": DESIGN_DRAFT_KIND,
                "status": "ready_for_review",
            }
        ),
        encoding="utf-8",
    )
    assert load_design_draft(path)["revision"] == 0

    path.write_text(
        json.dumps(
            {
                "artifact_schema_version": 1,
                "kind": DESIGN_DRAFT_KIND,
                "status": "ready_for_review",
                "revision": True,
            }
        ),
        encoding="utf-8",
    )
    invalid = load_design_draft(path)
    assert invalid["status"] == "unreadable"
    assert invalid["issues"][0]["code"] == "design_draft_revision_invalid"


def test_duplicate_manual_target_and_boolean_numeric_value_are_rejected() -> None:
    duplicate = {
        "drivers": [
            {"target_id": "mono:woofer", "role": "woofer", "model": "A"},
            {"target_id": "mono:woofer", "role": "woofer", "model": "B"},
        ],
        "crossover_candidates": [],
    }
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="manual_settings.drivers contains duplicate target_id",
    ):
        build_design_draft(_topology(), manual_settings=duplicate)

    boolean_numeric = {
        "drivers": [
            {
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "A",
                "nominal_impedance_ohm": True,
            }
        ],
        "crossover_candidates": [],
    }
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="nominal_impedance_ohm must be numeric",
    ):
        build_design_draft(_topology(), manual_settings=boolean_numeric)


# --- Persisted working-crossover values (Slice 0): polarity/delay on a
# crossover candidate -----------------------------------------------------


def _candidate(**overrides) -> dict:
    base = {
        "between_roles": ["woofer", "tweeter"],
        "frequency_hz": 2000,
    }
    base.update(overrides)
    return base


def test_normalise_candidate_accepts_polarity_and_delay():
    out = _normalise_candidate(
        _candidate(
            lower_polarity="inverted",
            upper_polarity="non-inverted",
            delay_ms=0.35,
            delay_target_role="woofer",
        )
    )

    assert out["lower_polarity"] == "inverted"
    assert out["upper_polarity"] == "non-inverted"
    assert out["delay_ms"] == 0.35
    assert out["delay_target_role"] == "woofer"


def test_normalise_candidate_polarity_delay_omitted_fields_stay_absent():
    out = _normalise_candidate(_candidate())

    assert "lower_polarity" not in out
    assert "upper_polarity" not in out
    assert "delay_ms" not in out
    assert "delay_target_role" not in out


def test_normalise_candidate_zero_delay_ms_is_not_dropped():
    out = _normalise_candidate(_candidate(delay_ms=0.0, delay_target_role="tweeter"))

    assert out["delay_ms"] == 0.0
    assert out["delay_target_role"] == "tweeter"


def test_normalise_candidate_rejects_unsupported_polarity():
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="lower_polarity must be one of",
    ):
        _normalise_candidate(_candidate(lower_polarity="reversed"))


def test_normalise_candidate_rejects_delay_ms_out_of_range():
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="delay_ms must be between 0 and 20 ms",
    ):
        _normalise_candidate(_candidate(delay_ms=25.0, delay_target_role="woofer"))

    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="delay_ms must be between 0 and 20 ms",
    ):
        _normalise_candidate(_candidate(delay_ms=-1.0, delay_target_role="woofer"))


def test_normalise_candidate_delay_target_role_must_be_in_between_roles():
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="delay_target_role must be one of between_roles",
    ):
        _normalise_candidate(_candidate(delay_ms=0.2, delay_target_role="mid"))


def test_normalise_candidate_delay_ms_requires_delay_target_role():
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="delay_target_role is required when delay_ms is set",
    ):
        _normalise_candidate(_candidate(delay_ms=0.2))


def test_manual_crossover_settings_carry_polarity_and_delay_through_draft():
    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [],
            "crossover_candidates": [
                {
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2200,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                    "lower_polarity": "non-inverted",
                    "upper_polarity": "inverted",
                    "delay_ms": 0.4,
                    "delay_target_role": "tweeter",
                }
            ],
        },
    )

    candidate = payload["manual_settings"]["crossover_candidates"][0]
    assert candidate["upper_polarity"] == "inverted"
    assert candidate["delay_ms"] == 0.4
    assert candidate["delay_target_role"] == "tweeter"


def test_existing_draft_fixtures_stay_byte_identical_without_polarity_delay():
    # Every pre-existing crossover-candidate fixture in this file omits the
    # new fields; confirm normalisation doesn't inject them.
    payload = build_design_draft(_topology(), driver_research=_research())

    candidate = payload["driver_research"]["crossover_candidates"][0]
    assert "lower_polarity" not in candidate
    assert "upper_polarity" not in candidate
    assert "delay_ms" not in candidate
    assert "delay_target_role" not in candidate
