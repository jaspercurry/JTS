# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path

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
    normalise_driver_research,
    normalise_manual_settings,
    _normalise_candidate,
    design_draft_view,
    declared_driver_spacing_m,
)
from jasper.active_speaker.declaration_vocabulary import (
    supported_declaration_filter_types,
    supported_declaration_slopes_db_per_octave,
)
from jasper.json_fields import CodedFieldError
from jasper.active_speaker.driver_pad import DriverPadError
from jasper.output_topology import OutputTopology
from jasper.active_speaker.installation import installation_evidence, normalise_installation
from tests.active_speaker_fixtures import current_research, mono_output_topology, research_design_draft


def _topology() -> OutputTopology:
    return mono_output_topology(card_id=None)


def test_installation_round_trips_without_changing_driver_authority(tmp_path):
    manual = {"drivers": [{"role": "woofer", "target_id": "mono:woofer", "model": "Test driver"}],
              "driver_spacing_mm": 150}
    before = save_design_draft(_topology(), manual_settings=manual, path=tmp_path / 'draft.json')
    facts = {"amplifier_model": "TPA3255", "amplifier_gain_control": "Fixed; volume in software", "supply_voltage_v": 36,
             "passive_radiator_model": "E180HE-PR", "passive_radiator_added_mass_g": 0}
    manual['drivers'][0]['installation'] = facts
    path = tmp_path / 'draft.json'
    saved = save_design_draft(_topology(), manual_settings=manual, path=path)
    loaded = load_design_draft(topology=_topology(), path=path)
    assert loaded['manual_settings']['drivers'][0]['installation'] == facts
    assert declared_driver_spacing_m(loaded) == pytest.approx(.15)
    assert saved['driver_safety_profile'] == before['driver_safety_profile']
    assert saved['permissions'] == before['permissions']
    evidence = installation_evidence(loaded)
    assert evidence['drivers'][0]['amplifier_estimate']['ideal_btl_rms_voltage_ceiling_v'] == pytest.approx(25.4558, abs=.0001)
    assert evidence['drivers'][0]['acoustic_limit']['status'] == 'not_estimated'
    assert 'installation' not in json.loads(path.read_text())
    assert normalise_installation({"amplifier_model": "TPA3255"}) == {"amplifier_model": "TPA3255"}


@pytest.mark.parametrize('facts,code', [
    ({'supply_voltage_v': -1}, 'field_not_positive'),
    ({'supply_voltage_v': True}, 'field_not_numeric'),
    ({'net_volume_l': float('inf')}, 'field_not_finite'),
    ({'passive_radiator_count': 1.5}, 'field_not_integer'),
    ({'passive_radiator_added_mass_g': -1}, 'field_negative'),
    ({'safe_boost_db': 15}, 'unknown_installation_fields'),
    ([], 'field_not_object'),
])
def test_installation_rejects_invalid_facts(facts, code):
    with pytest.raises(CodedFieldError) as caught:
        build_design_draft(_topology(), manual_settings={
            "drivers": [{"role": "woofer", "installation": facts}],
        })
    assert caught.value.code == code


def _research() -> dict:
    return {
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


def _one_research_driver(driver: dict) -> dict:
    research = {"artifact_schema_version": 2, "kind": DRIVER_RESEARCH_KIND,
                "drivers": [{**driver, "target_id": f"mono:{driver['role']}"}]}
    return normalise_driver_research(research)["drivers"][0]


def test_design_draft_persists_research_without_authorizing_audio(tmp_path: Path):
    path = tmp_path / "active_speaker_design_draft.json"

    research, inputs = current_research(_topology(), _research())
    payload = save_design_draft(
        _topology(),
        driver_research=research,
        operator_inputs={
            **inputs,
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


def test_save_design_draft_durable_fsyncs_the_write(tmp_path: Path, monkeypatch) -> None:
    """#2292 scope 2: ``durable=True`` reaches ``atomic_write_text``'s fsync
    (file + parent directory); the default save does not fsync at all."""
    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))
    path = tmp_path / "active_speaker_design_draft.json"

    save_design_draft(_topology(), path=path)
    assert fsync_calls == []

    save_design_draft(_topology(), path=path, durable=True)
    assert len(fsync_calls) == 2  # file fsync + parent-directory fsync


def test_save_design_draft_refuses_a_non_finite_number_and_writes_nothing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Non-finite stored values fail before the atomic write."""
    from jasper.active_speaker import design_draft as design_draft_mod

    path = tmp_path / "active_speaker_design_draft.json"
    save_design_draft(_topology(), path=path)
    good = path.read_text(encoding="utf-8")

    real_build = design_draft_mod.build_design_draft

    def _build_with_a_nan(*args, **kwargs):
        draft = real_build(*args, **kwargs)
        draft["manual_settings"] = {"drivers": [{"recommended_highpass_hz": float("nan")}]}
        return draft

    monkeypatch.setattr(design_draft_mod, "build_design_draft", _build_with_a_nan)
    with pytest.raises(ValueError):
        save_design_draft(_topology(), path=path)

    assert path.read_text(encoding="utf-8") == good


def test_the_store_is_published_group_readable_so_the_wizard_can_read_it(
    tmp_path: Path,
) -> None:
    """The crossover-accept seam runs in the ROOT ``jasper-correction-web``
    process while ``/sound/`` reads this store as ``jasper-web`` (group
    ``jasper``), so 0640 is what keeps the design page from rendering empty
    against a store it cannot open -- the API reporting it "unreadable" at
    revision 0.
    """

    path = tmp_path / "active_speaker_design_draft.json"

    save_design_draft(_topology(), path=path)
    assert path.stat().st_mode & 0o777 == 0o640

    # And the durable accept-seam write keeps the same contract.
    save_design_draft(_topology(), path=path, durable=True)
    assert path.stat().st_mode & 0o777 == 0o640


def test_driver_research_cannot_weaken_human_review_requirements():
    raw = _research()
    raw["human_review"] = {
        "must_verify_wiring": False,
        "must_start_quiet": False,
        "needs_measurement_before_final": False,
    }

    payload = research_design_draft(_topology(), raw)

    assert payload["driver_research"]["human_review"] == {
        "must_verify_wiring": True,
        "must_start_quiet": True,
        "needs_measurement_before_final": True,
    }


def test_driver_research_notes_remain_bounded():
    raw = _research()
    raw["drivers"][1]["notes"] = "x" * 2048
    payload = research_design_draft(_topology(), raw)
    assert len(payload["driver_research"]["drivers"][1]["notes"]) == 2048

    raw["drivers"][1]["notes"] = "x" * 2049
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        research_design_draft(_topology(), raw)
    assert caught.value.code == "field_too_long"


def test_manual_driver_notes_use_same_bound():
    manual_settings = {
        "drivers": [
            {"role": "tweeter", "target_id": "mono:tweeter", "notes": "x" * 2048},
        ],
        "crossover_candidates": [],
    }

    payload = build_design_draft(_topology(), manual_settings=manual_settings)

    assert len(payload["manual_settings"]["drivers"][0]["notes"]) == 2048

    manual_settings["drivers"][0]["notes"] = "x" * 2049
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        build_design_draft(_topology(), manual_settings=manual_settings)
    assert caught.value.code == "field_too_long"


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
    research_driver = _one_research_driver(common)
    manual_driver = normalise_manual_settings({"drivers": [common]})["drivers"][0]

    assert research_driver["gain_offset_db_provenance"] == "research_estimate"
    assert manual_driver["gain_offset_db_provenance"] == "operator_pinned"
    assert research_driver["sources"] == ["https://example.test/woofer"]
    assert "sources" not in manual_driver
    for field in common.keys() - {"sources"}:
        assert research_driver[field] == manual_driver[field]


def test_research_requires_model_while_manual_driver_does_not() -> None:
    research, _ = current_research(_topology(), _research())
    research["drivers"][0].pop("model")
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        normalise_driver_research(research)
    assert caught.value.code == "field_required"

    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {
                    "role": "woofer", "target_id": "mono:woofer",
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
                    "role": "woofer", "target_id": "mono:woofer",
                    "model": "Epique E150HE-44",
                    "sensitivity_db_2v83_1m": 83.3,
                },
                {
                    "role": "tweeter", "target_id": "mono:tweeter",
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
                    "role": "tweeter", "target_id": "mono:tweeter",
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
        research_design_draft(_topology(), raw)


def test_load_design_draft_fails_soft_on_unsupported_schema(tmp_path: Path):
    path = tmp_path / "active_speaker_design_draft.json"
    path.write_text(
        json.dumps({"artifact_schema_version": 99, "kind": DESIGN_DRAFT_KIND}),
        encoding="utf-8",
    )

    payload = load_design_draft(path)

    assert payload["status"] == "unreadable"
    assert payload["issues"][0]["code"] == "design_draft_unsupported_schema"


def test_legacy_digests_are_ignored_on_read_and_dropped_on_save(tmp_path: Path) -> None:
    from tests.test_active_speaker_driver_safety import (
        _manual_settings, _operator_inputs, _research_result,
    )
    from jasper.active_speaker.driver_safety import build_driver_research_context

    path = tmp_path / "draft.json"
    topology = _topology()
    research = _research_result(build_driver_research_context(topology, _operator_inputs()))
    draft = save_design_draft(
        topology, driver_research=research, manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(), path=path,
    )
    old = json.loads(path.read_text())
    old["driver_research_request"] = {"targets": [{"operator_declared_context": {"operator_notes": "old"}}]}
    old["driver_research"].update(request_fingerprint="old", result_fingerprint="old")
    path.write_text(json.dumps(old))

    loaded = load_design_draft(path, topology=topology)
    assert loaded == draft
    saved = save_design_draft(
        topology, driver_research=loaded["driver_research"],
        manual_settings=loaded["manual_settings"], operator_inputs=loaded["operator_inputs"], path=path,
    )
    assert saved["driver_research"] == draft["driver_research"]
    assert saved["manual_settings"] == draft["manual_settings"]
    assert "driver_research_request" not in json.loads(path.read_text())


@pytest.mark.parametrize(("where", "update", "code"), [
    ("driver", {"horn_coverage_deg": 1}, "unknown_driver_fields"),
    ("driver", {"crossover_search_band_hz": 1}, "unknown_driver_fields"),
    ("driver", {"target_fingerprint": 1}, "unknown_driver_fields"),
    ("research", {"artifact_schema_version": 1, "retired_key": 1}, "research_version_unsupported"),
])
def test_stored_research_the_import_refuses_refuses_on_load(tmp_path: Path, where, update, code) -> None:
    from tests.test_active_speaker_driver_safety import _operator_inputs, _research_result
    from jasper.active_speaker.driver_safety import build_driver_research_context

    path = tmp_path / "draft.json"
    topology = _topology()
    research = _research_result(build_driver_research_context(topology, _operator_inputs()))
    save_design_draft(topology, driver_research=research, operator_inputs=_operator_inputs(), path=path)
    stored = json.loads(path.read_text())
    research = stored["driver_research"]
    (research["drivers"][0] if where == "driver" else research).update(update)
    path.write_text(json.dumps(stored))

    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        load_design_draft(path, topology=topology)
    assert caught.value.code == code


@pytest.mark.parametrize("author", ["driver_research", "manual_settings"])
def test_a_stored_cabinet_key_the_reader_does_not_know_refuses_on_load(tmp_path: Path, author) -> None:
    """A driver's size is its own radiating_diameter_mm, so a cabinet key the reader
    does not know refuses by its name, never dropped unseen (ADR-0384, #2902)."""
    from tests.test_active_speaker_driver_safety import _manual_settings, _operator_inputs, _research_result
    from jasper.active_speaker.driver_safety import build_driver_research_context

    path = tmp_path / "draft.json"
    topology = _topology()
    research = _research_result(build_driver_research_context(topology, _operator_inputs()))
    save_design_draft(topology, driver_research=research, manual_settings=_manual_settings(),
                      operator_inputs=_operator_inputs(), path=path)
    stored = json.loads(path.read_text())
    stored[author]["drivers"][0]["cabinet"]["radiator_diameter_mm"] = 132
    path.write_text(json.dumps(stored))

    with pytest.raises(CodedFieldError) as caught:
        load_design_draft(path, topology=topology)
    assert caught.value.code == "unknown_driver_fields"


def test_design_draft_revision_is_informational(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active_speaker_design_draft.json"
    first = save_design_draft(
        _topology(),
        operator_inputs={"notes": "first"},
        path=path,
        created_at="2026-07-13T12:00:00Z",
    )
    second = save_design_draft(
        _topology(),
        operator_inputs={"notes": "second"},
        path=path,
        created_at="2026-07-13T12:01:00Z",
    )

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert load_design_draft(path)["operator_inputs"]["notes"] == "second"


@pytest.mark.parametrize("revision", [True, "3"])
def test_legacy_draft_loads_as_revision_zero_and_invalid_revision_fails_soft(
    tmp_path: Path, revision,
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
                "revision": revision,
                "topology": _topology().to_dict(),
                "manual_settings": {"drivers": "nope"},
            }
        ),
        encoding="utf-8",
    )
    invalid = load_design_draft(path, topology=_topology())
    assert invalid["status"] == "unreadable"
    assert invalid["issues"][0]["code"] == "design_draft_revision_invalid"
    assert "driver_safety_profile" not in invalid


def test_two_manual_rows_for_one_target_are_rejected() -> None:
    duplicate = {
        "drivers": [
            {"target_id": "mono:woofer", "role": "woofer", "model": "A"},
            {"target_id": "mono:woofer", "role": "woofer", "model": "B"},
        ],
        "crossover_candidates": [],
    }
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        build_design_draft(_topology(), manual_settings=duplicate)
    assert caught.value.code == "duplicate_target_id"


def test_boolean_in_a_numeric_driver_field_is_rejected() -> None:
    manual_settings = {
        "drivers": [
            {
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "A",
                "nominal_impedance_ohm": 8,
            }
        ],
        "crossover_candidates": [],
    }
    payload = build_design_draft(_topology(), manual_settings=manual_settings)
    assert payload["manual_settings"]["drivers"][0]["nominal_impedance_ohm"] == 8

    manual_settings["drivers"][0]["nominal_impedance_ohm"] = True
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        build_design_draft(_topology(), manual_settings=manual_settings)
    assert caught.value.code == "field_not_numeric"


@pytest.mark.parametrize("inputs", [
    {"target_models": {"missing:woofer": "A"}},
    {"target_models": {"mono:woofer": "A", " mono:woofer ": "B"}},
])
def test_operator_target_bindings_are_checked(inputs) -> None:
    with pytest.raises(ActiveSpeakerDesignDraftError):
        build_design_draft(_topology(), operator_inputs=inputs)


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


# --- Entry-time crossover vocabulary ---------------------------------------
#
# A crossover the compiler cannot build is refused HERE, where whoever named it
# can still see the field, instead of reaching staging's
# ``crossover_preview_filter_unsupported`` blocker several screens later. The
# accepted set is the compiler's own, so it cannot drift from what compiles.


def test_normalise_candidate_accepts_every_offered_filter_type_and_slope():
    for filter_type in supported_declaration_filter_types():
        for slope in supported_declaration_slopes_db_per_octave():
            out = _normalise_candidate(
                _candidate(filter_type=filter_type, slope_db_per_octave=slope)
            )
            assert out["filter_type"] == filter_type
            assert out["slope_db_per_octave"] == slope


def test_normalise_candidate_accepts_a_household_spelling_the_compiler_reads():
    # Acceptance is wider than the OFFER on purpose — the offer is the
    # canonical spellings, a hand-written or researched declaration may not be.
    out = _normalise_candidate(_candidate(filter_type="LR", slope_db_per_octave=24))

    assert out["filter_type"] == "LR"


@pytest.mark.parametrize("overrides,code", [
    ({"filter_type": "Butterworth"}, "unsupported_filter_type"),
    # 18 dB/octave is an ordinary number and a third-order filter; no supported
    # Linkwitz-Riley order compiles to it.
    ({"slope_db_per_octave": 18}, "unsupported_slope"),
    ({"lower_polarity": "reversed"}, "unsupported_polarity"),
    ({"delay_ms": 25.0, "delay_target_role": "woofer"}, "delay_out_of_range"),
    ({"delay_ms": -1.0, "delay_target_role": "woofer"}, "delay_out_of_range"),
    ({"delay_ms": 0.2, "delay_target_role": "mid"}, "delay_target_role_invalid"),
    ({"delay_ms": 0.2}, "delay_target_role_required"),
])
def test_normalise_candidate_refuses_values_outside_the_vocabulary(overrides, code):
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        _normalise_candidate(_candidate(**overrides))
    assert caught.value.code == code


def test_normalise_candidate_leaves_an_undeclared_filter_and_slope_absent():
    # Absent-in -> absent-out: the preview fills its own default, and that
    # default is pinned to the offer in tests/test_crossover_declaration.py.
    out = _normalise_candidate(_candidate())

    assert "filter_type" not in out
    assert "slope_db_per_octave" not in out


def test_driver_research_crossover_vocabulary_is_refused_at_the_same_door():
    # The preview may choose a RESEARCH candidate over the operator's, so a
    # packet validated only for shape would walk the same value past the same
    # entry and into the same late blocker.
    research = _research()
    research["crossover_candidates"][0]["slope_db_per_octave"] = 18

    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        research_design_draft(_topology(), research)
    assert caught.value.code == "unsupported_slope"


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
    payload = research_design_draft(_topology(), _research())

    candidate = payload["driver_research"]["crossover_candidates"][0]
    assert "lower_polarity" not in candidate
    assert "upper_polarity" not in candidate
    assert "delay_ms" not in candidate
    assert "delay_target_role" not in candidate


# --- #1864: declared woofer<->tweeter acoustic-center spacing -------------


def test_declared_driver_spacing_m_reads_the_declaration():
    draft = {"manual_settings": {"driver_spacing_mm": 150}}
    assert declared_driver_spacing_m(draft) == pytest.approx(0.15)


def test_declared_driver_spacing_m_fails_soft_on_absent_or_malformed():
    # Absent means UNDECLARED, never a manufactured physical default (#1864).
    assert declared_driver_spacing_m(None) is None
    assert declared_driver_spacing_m({}) is None
    assert declared_driver_spacing_m({"manual_settings": None}) is None
    assert declared_driver_spacing_m({"manual_settings": {}}) is None
    for bad in ("far", True, float("nan"), 0, -10):
        assert declared_driver_spacing_m(
            {"manual_settings": {"driver_spacing_mm": bad}}
        ) is None


def test_declared_driver_spacing_mm_must_be_positive():
    payload = build_design_draft(
        _topology(), manual_settings={"driver_spacing_mm": 150},
    )
    assert declared_driver_spacing_m(payload) == pytest.approx(0.15)

    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        build_design_draft(_topology(), manual_settings={"driver_spacing_mm": 0})
    assert caught.value.code == "field_not_positive"


def test_declared_driver_spacing_m_survives_the_normalised_persisted_draft():
    # End-to-end through the REAL normaliser + draft builder: what
    # resolve_conductor_context reads is the persisted draft's
    # manual_settings, so pin the value's survival through that path.
    payload = build_design_draft(
        _topology(), manual_settings={"driver_spacing_mm": 150},
    )
    assert declared_driver_spacing_m(payload) == pytest.approx(0.15)


def test_build_design_draft_does_not_raise_with_driver_class_set():
    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {"role": "woofer", "target_id": "mono:woofer", "model": "A", "radiating_diameter_mm": 114},
                {
                    "role": "tweeter", "target_id": "mono:tweeter",
                    "model": "B",
                    "driver_class": "compression_horn",
                    "nominal_impedance_ohm": 8,
                    "sensitivity_db_2v83_1m": 108.0,
                    "pad": {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
                },
            ],
            "crossover_candidates": [],
        },
    )
    woofer, tweeter = payload["manual_settings"]["drivers"]
    assert woofer["radiating_diameter_mm"] == 114.0
    assert "driver_class" not in woofer
    assert tweeter["driver_class"] == "compression_horn"
    assert tweeter["pad"] == {
        "kind": "l_pad",
        "series_ohm": 6.8,
        "shunt_ohm": 2.0,
        "attenuation_db": -14.4,
        "effective_impedance_ohm": 8.4,
    }
    assert design_draft_view(payload)["driver_safety_profile"] is not None


def test_driver_class_rejects_unsupported_value():
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [{"role": "woofer", "model": "A", "driver_class": "ceramic"}],
                "crossover_candidates": [],
            },
        )
    assert caught.value.code == "unsupported_driver_class"


def test_driver_class_accepts_every_hoisted_value():
    from jasper.active_speaker._common import DRIVER_CLASSES

    for value in DRIVER_CLASSES:
        payload = build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [{"role": "woofer", "target_id": "mono:woofer", "model": "A", "driver_class": value}],
                "crossover_candidates": [],
            },
        )
        assert payload["manual_settings"]["drivers"][0]["driver_class"] == value


@pytest.mark.parametrize("shape", ["document", "driver", "candidate", "provenance"])
def test_pasted_research_refuses_unknown_fields(shape):
    research, _ = current_research(_topology(), _research())
    driver = research["drivers"][0]
    driver["field_provenance"] = {"sensitivity_db_2v83_1m": {
        "confidence": "high", "basis": "datasheet",
    }}
    node = {
        "document": research, "driver": driver,
        "candidate": research["crossover_candidates"][0],
        "provenance": driver["field_provenance"]["sensitivity_db_2v83_1m"],
    }[shape]
    node["typo"] = True
    with pytest.raises(CodedFieldError) as caught:
        normalise_driver_research(research)
    assert caught.value.code == "unknown_driver_fields"


def test_extra_manual_keys_are_ignored(tmp_path):
    manual = {
        "typo": True,
        "drivers": [{"role": "woofer", "target_id": "mono:woofer", "model": "A", "typo": True,
                     "cabinet": {"enclosure_kind": "sealed"},
                     "level_duration_limits": {"max_sweep_duration_s": 4, "typo": True}}],
        "crossover_candidates": [{"between_roles": ["woofer", "tweeter"],
                                  "frequency_hz": 2500, "typo": True}],
    }
    saved = save_design_draft(_topology(), manual_settings=manual,
                              operator_inputs={"woofer": "A", "typo": True},
                              path=tmp_path / "draft.json")
    assert "driver_fields" not in saved
    assert saved["operator_inputs"] == {"woofer": "A"}
    assert saved["manual_settings"]["drivers"][0]["cabinet"] == {
        "enclosure_kind": "sealed", "lf_reconstruction_capability": "refused_single_radiator_contract_not_proven",
    }
    assert "typo" not in saved["manual_settings"]
    assert "typo" not in saved["manual_settings"]["drivers"][0]
    assert "typo" not in saved["manual_settings"]["crossover_candidates"][0]


def test_radiating_diameter_mm_must_be_positive():
    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {"role": "woofer", "target_id": "mono:woofer", "model": "A", "radiating_diameter_mm": 114}
            ],
            "crossover_candidates": [],
        },
    )
    assert payload["manual_settings"]["drivers"][0]["radiating_diameter_mm"] == 114

    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [
                    {"role": "woofer", "target_id": "mono:woofer", "model": "A", "radiating_diameter_mm": 0}
                ],
                "crossover_candidates": [],
            },
        )
    assert caught.value.code == "field_not_positive"


def test_pad_error_propagates_through_design_draft():
    driver = {
        "role": "tweeter", "target_id": "mono:tweeter",
        "model": "B",
        "nominal_impedance_ohm": 8,
        "pad": {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
    }
    payload = build_design_draft(
        _topology(), manual_settings={"drivers": [driver], "crossover_candidates": []},
    )
    assert payload["manual_settings"]["drivers"][0]["pad"]["attenuation_db"] == -14.4

    del driver["nominal_impedance_ohm"]
    with pytest.raises(DriverPadError) as caught:
        build_design_draft(
            _topology(), manual_settings={"drivers": [driver], "crossover_candidates": []},
        )
    assert caught.value.code == "field_required"


def test_regenerate_crossover_preview_path_re_normalises_a_saved_pad_without_raising():
    # #1665 follow-up bug + fix: normalise_pad wrote attenuation_db /
    # effective_impedance_ohm into the pad record it returned, then rejected
    # those same fields as unknown input when that returned record was fed
    # back in as fresh input. This reproduces the exact live JTS3 failure
    # shape: crossover-v2 session-start reloads the saved design draft and
    # rebuilds it via (read verbatim from that code path, ~line 276-284 as
    # of #1665):
    #
    #     draft = build_design_draft(
    #         topology,
    #         driver_research=draft.get("driver_research"),
    #         manual_settings=draft.get("manual_settings"),
    #         operator_inputs=draft.get("operator_inputs"),
    #         created_at=draft.get("created_at"),
    #     )
    #
    # -- feeding the ALREADY-NORMALISED manual_settings (derived pad fields
    # included) straight back in. Mirrored here exactly rather than
    # re-derived, per AGENTS.md on verifying at the user's surface: on the
    # pre-fix code this call raises ActiveSpeakerDesignDraftError(
    # "manual_settings.driver.pad has unknown fields: effective_impedance_ohm").
    topology = _topology()

    saved = build_design_draft(
        topology,
        manual_settings={
            "drivers": [
                {"role": "woofer", "target_id": "mono:woofer", "model": "A"},
                {
                    "role": "tweeter", "target_id": "mono:tweeter",
                    "model": "B",
                    "nominal_impedance_ohm": 8,
                    "sensitivity_db_2v83_1m": 108.0,
                    "pad": {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
                },
            ],
            "crossover_candidates": [],
        },
    )
    _woofer, tweeter = saved["manual_settings"]["drivers"]
    expected_pad = {
        "kind": "l_pad",
        "series_ohm": 6.8,
        "shunt_ohm": 2.0,
        "attenuation_db": -14.4,
        "effective_impedance_ohm": 8.4,
    }
    assert tweeter["pad"] == expected_pad

    # The regenerate-path rebuild call, verbatim in argument shape.
    regenerated = build_design_draft(
        topology,
        driver_research=saved.get("driver_research"),
        manual_settings=saved.get("manual_settings"),
        operator_inputs=saved.get("operator_inputs"),
        created_at=saved.get("created_at"),
    )
    _woofer2, tweeter2 = regenerated["manual_settings"]["drivers"]
    assert tweeter2["pad"] == expected_pad


def test_research_and_manual_drivers_share_the_new_fields_too():
    common = {
        "role": "tweeter",
        "model": "Shared Dome",
        "driver_class": "soft_dome",
        "radiating_diameter_mm": 25,
    }
    research_driver = _one_research_driver(common)
    manual_driver = normalise_manual_settings({"drivers": [common]})["drivers"][0]

    for field in ("driver_class", "radiating_diameter_mm"):
        assert research_driver[field] == manual_driver[field] == common[field]


@pytest.mark.parametrize("tweeter, effective", [
    ({"sensitivity_db_2v83_1m": 108.0, "pad": {"kind": "direct_db", "attenuation_db": -14.4}}, 93.6),
    ({"sensitivity_db_2v83_1m": 108.0, "nominal_impedance_ohm": 8,
      "pad": {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0}}, 93.6),
    ({}, None),
], ids=["direct_db", "l_pad", "undeclared"])
def test_each_computed_target_carries_its_declared_sensitivity_with_its_pad_folded_in(tweeter, effective):
    draft = build_design_draft(_topology(), manual_settings={"drivers": [
        {"role": "woofer", "target_id": "mono:woofer", "sensitivity_db_2v83_1m": 83.3},
        {"role": "tweeter", "target_id": "mono:tweeter", **tweeter},
    ], "crossover_candidates": []})
    targets = {target["role"]: target for target in design_draft_view(draft)["driver_safety_profile"]["targets"]}
    assert targets["woofer"]["effective_sensitivity_db_2v83_1m"] == 83.3
    assert targets["tweeter"].get("effective_sensitivity_db_2v83_1m") == (
        None if effective is None else pytest.approx(effective))


def test_a_draft_without_topology_drops_derived_fields():
    view = design_draft_view({"topology": None, "driver_safety_profile": {"obsolete": True},
                              "driver_protection_policy_view": {"obsolete": True}})
    assert {"driver_safety_profile", "driver_protection_policy_view"}.isdisjoint(view)
