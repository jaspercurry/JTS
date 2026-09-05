# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
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
    declared_driver_sensitivities,
    declared_effective_driver_sensitivities,
)
from jasper.active_speaker.declaration_vocabulary import (
    supported_declaration_filter_types,
    supported_declaration_slopes_db_per_octave,
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
    """#2839: the writer fails, not the packet.

    ``evidence_packet._drivers_block`` copies ``driver_safety_profile
    .confirmation`` out of this draft WHOLE, and
    ``evidence_identity.json_fingerprint`` refuses a non-finite number — so a
    NaN banked here costs the round its whole evidence packet, at a reader,
    long after the code that produced it returned. ``allow_nan=False`` moves
    the failure to this writer.

    Driven by replacing the composed draft rather than by feeding a NaN through
    a real input, because this is a GUARD at the write boundary: it must hold
    for whatever a future composer puts in the draft, not only for the fields
    today's normalisers happen to leave open.

    Nothing half-written, and that is structural: ``json.dumps`` raises while
    evaluating an ARGUMENT, so ``atomic_write_text`` is never entered and the
    prior revision is still on disk afterwards.
    """
    from jasper.active_speaker import design_draft as design_draft_mod

    path = tmp_path / "active_speaker_design_draft.json"
    save_design_draft(_topology(), path=path)
    good = path.read_text(encoding="utf-8")

    real_build = design_draft_mod.build_design_draft

    def _build_with_a_nan(*args, **kwargs):
        draft = real_build(*args, **kwargs)
        draft["driver_safety_profile"] = {"confirmation": float("nan")}
        return draft

    monkeypatch.setattr(design_draft_mod, "build_design_draft", _build_with_a_nan)
    with pytest.raises(ValueError):
        save_design_draft(_topology(), path=path)

    assert path.read_text(encoding="utf-8") == good


def test_the_store_publishes_under_its_parent_group_so_the_wizard_can_read_it(
    tmp_path: Path,
) -> None:
    """The 2026-08-21 jts3 defect, pinned at the write site.

    The crossover-accept seam runs in the ROOT ``jasper-correction-web``
    process; ``/sound/`` reads this store as ``jasper-web`` (group ``jasper``).
    ``/var/lib/jasper`` is group ``jasper`` but NOT setgid, so a root write
    that kept its own group published ``root:root 0640`` and the design page
    rendered empty against a store it could not open -- the API reporting it
    "unreadable" at revision 0.
    """

    import stat

    from jasper.active_speaker import design_draft as design_draft_mod

    real_write = design_draft_mod.atomic_write_text
    calls: list[dict] = []

    def recording_write(path, text, **kwargs):
        calls.append(kwargs)
        real_write(path, text, **kwargs)

    path = tmp_path / "active_speaker_design_draft.json"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(design_draft_mod, "atomic_write_text", recording_write)
        save_design_draft(_topology(), path=path)
        # And the durable accept-seam write keeps the same contract.
        save_design_draft(_topology(), path=path, durable=True)

    assert calls and all(call.get("mode") == 0o640 for call in calls)

    published = path.stat()
    assert stat.S_IMODE(published.st_mode) == 0o640
    assert published.st_gid == tmp_path.stat().st_gid


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


def test_load_demotes_legacy_hidden_note_request_and_preserves_manual_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active_speaker_design_draft.json"
    manual_settings = {
        "drivers": [
            {
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "Example Woofer",
                "notes": "Legacy installed-configuration value",
            }
        ],
        "crossover_candidates": [],
    }
    path.write_text(
        json.dumps(
            {
                "artifact_schema_version": 1,
                "kind": DESIGN_DRAFT_KIND,
                "status": "ready_for_review",
                "revision": 4,
                "operator_inputs": {},
                "manual_settings": manual_settings,
                "driver_research_request": {
                    "targets": [
                        {
                            "target_id": "mono:woofer",
                            "operator_declared_context": {
                                "operator_notes": (
                                    "Legacy installed-configuration value"
                                )
                            },
                        }
                    ]
                },
                "driver_research": {
                    "artifact_schema_version": 2,
                    "kind": DRIVER_RESEARCH_KIND,
                    "request_fingerprint": "a" * 64,
                    "drivers": [],
                    "crossover_candidates": [],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_design_draft(path)

    assert loaded["revision"] == 4
    assert loaded["driver_research_request"] is None
    assert loaded["driver_research"] is None
    assert loaded["manual_settings"] == manual_settings

    saved = save_design_draft(
        _topology(),
        driver_research_request=loaded["driver_research_request"],
        driver_research=loaded["driver_research"],
        manual_settings=loaded["manual_settings"],
        operator_inputs=loaded["operator_inputs"],
        expected_revision=loaded["revision"],
        path=path,
        created_at="2026-07-28T12:00:00Z",
    )
    assert saved["revision"] == 5
    assert saved["driver_research_request"] is None
    assert saved["manual_settings"]["drivers"][0]["notes"] == (
        "Legacy installed-configuration value"
    )


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


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (
            {"manual_settings": {"drivers": [], "crossover_candidates": [], "typo": 1}},
            "manual_settings has unknown fields: typo",
        ),
        (
            {
                "manual_settings": {
                    "drivers": [{"role": "woofer", "model": "A", "typo": 1}],
                    "crossover_candidates": [],
                }
            },
            "manual_settings.driver has unknown fields: typo",
        ),
        (
            {
                "manual_settings": {
                    "drivers": [],
                    "crossover_candidates": [{
                        "between_roles": ["woofer", "tweeter"],
                        "frequency_hz": 2500,
                        "typo": 1,
                    }],
                }
            },
            "crossover_candidate has unknown fields: typo",
        ),
        (
            {"operator_inputs": {"woofer": "A", "typo": "ignored before"}},
            "operator_inputs has unknown fields: typo",
        ),
        (
            {"operator_inputs": {"target_models": {"missing:woofer": "A"}}},
            "unknown physical targets: missing:woofer",
        ),
        (
            {
                "operator_inputs": {
                    "target_models": {"mono:woofer": "A", " mono:woofer ": "B"}
                }
            },
            "duplicate target mono:woofer",
        ),
    ],
)
def test_nested_design_inputs_reject_unknown_fields(kwargs, match: str) -> None:
    with pytest.raises(ActiveSpeakerDesignDraftError, match=match):
        build_design_draft(_topology(), **kwargs)


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


def test_normalise_candidate_rejects_a_filter_the_compiler_cannot_build():
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="crossover_candidate.filter_type must be one of: Linkwitz-Riley",
    ):
        _normalise_candidate(_candidate(filter_type="Butterworth"))


def test_normalise_candidate_rejects_a_slope_no_supported_order_builds():
    # 18 dB/octave is an ordinary number and a third-order filter; no supported
    # Linkwitz-Riley order compiles to it.
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match=r"crossover_candidate.slope_db_per_octave must be one of: "
        r"12, 24, 48 dB/octave",
    ):
        _normalise_candidate(_candidate(slope_db_per_octave=18))


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

    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="crossover_candidate.slope_db_per_octave must be one of",
    ):
        build_design_draft(_topology(), driver_research=research)


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


# --- declared_driver_sensitivities: the declaration is the sensitivity SSOT ----
#
# W6.5 (2026-07-19 gate): sensitivity is a declared physical property whose one
# owner is the declaration (manual_settings) — the confirmed safety profile
# never carries a second copy, and JTS3's persisted draft (83.3 / 108.5 under
# sensitivity_db_2v83_1m) makes the derived HF ceiling fire with no migration.


def test_declared_driver_sensitivities_reads_the_declaration():
    draft = {
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": 83.3},
                {"role": "tweeter", "sensitivity_db_2v83_1m": 108.5},
                {"role": "mid"},  # declared but no sensitivity — omitted
            ],
            "crossover_candidates": [],
        },
    }
    assert declared_driver_sensitivities(draft) == {
        "woofer": 83.3,
        "tweeter": 108.5,
    }


def test_declared_driver_sensitivities_fails_soft_on_absent_or_malformed():
    assert declared_driver_sensitivities(None) == {}
    assert declared_driver_sensitivities({}) == {}
    assert declared_driver_sensitivities({"manual_settings": None}) == {}
    assert declared_driver_sensitivities(
        {"manual_settings": {"drivers": "not-a-list"}}
    ) == {}
    # Non-numeric / boolean / non-finite values are skipped, not raised on —
    # this reader runs inside the conductor-context resolution.
    draft = {
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": "loud"},
                {"role": "mid", "sensitivity_db_2v83_1m": True},
                {"role": "tweeter", "sensitivity_db_2v83_1m": float("nan")},
            ],
        },
    }
    assert declared_driver_sensitivities(draft) == {}


def test_declared_driver_sensitivities_drops_conflicting_role_rows():
    # Two rows for one role with DISAGREEING values (e.g. stereo declarations
    # that drifted apart): ambiguity derives nothing for that role, failing
    # toward the conservative class-default ceiling. Agreeing duplicates keep
    # the value.
    draft = {
        "manual_settings": {
            "drivers": [
                {"role": "tweeter", "target_id": "left:tweeter",
                 "sensitivity_db_2v83_1m": 108.5},
                {"role": "tweeter", "target_id": "right:tweeter",
                 "sensitivity_db_2v83_1m": 95.0},
                {"role": "woofer", "target_id": "left:woofer",
                 "sensitivity_db_2v83_1m": 83.3},
                {"role": "woofer", "target_id": "right:woofer",
                 "sensitivity_db_2v83_1m": 83.3},
            ],
        },
    }
    assert declared_driver_sensitivities(draft) == {"woofer": 83.3}


def test_declared_sensitivities_survive_the_normalised_persisted_draft():
    # End-to-end through the REAL normaliser + draft builder: what
    # resolve_conductor_context reads is the persisted draft's
    # manual_settings, so pin the values' survival through that path.
    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": 83.3},
                {"role": "tweeter", "sensitivity_db_2v83_1m": 108.5},
            ],
            "crossover_candidates": [],
        },
    )
    assert declared_driver_sensitivities(payload) == {
        "woofer": 83.3,
        "tweeter": 108.5,
    }


# --- #1665 component entry: driver_class / radiating_diameter_mm / pad -----
#
# Gotcha #1 (coordinator brief): design_draft._MANUAL_DRIVER_FIELDS,
# driver_safety._MANUAL_DRIVER_FIELDS, driver_safety._V2_RESEARCH_DRIVER_FIELDS,
# and _validate_v2_research_prefill's `comparable` set must ALL accept the
# component-entry keys, or build_design_draft 500s at save time (driver_safety.py
# re-validates the SAME normalised manual_settings record design_draft.py just
# produced). The guard test below pins the regression signature directly.
#
# A fourth key, horn_coverage_deg, was deleted by #2872. It is now a tolerated
# legacy key at every one of those gates -- the same lockstep requirement, in
# the opposite direction: a gate that forgot to tolerate it 500s the save of a
# draft written before the deletion. See
# test_legacy_horn_coverage_deg_draft_still_saves_and_drops_the_key.


def test_build_design_draft_does_not_raise_with_driver_class_set():
    """Gotcha #1's regression signature: a save-time 500 from a driver-safety
    allowlist that wasn't updated in lockstep with design_draft.py's own."""

    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {"role": "woofer", "model": "A", "radiating_diameter_mm": 114},
                {
                    "role": "tweeter",
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
    # driver_safety_profile is also built from the SAME manual_settings
    # record (build_design_draft always tries it when an active crossover
    # pair exists) -- confirm it too survived the re-validation.
    assert payload["driver_safety_profile"] is not None


def test_driver_class_rejects_unsupported_value():
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match=r"driver\.driver_class must be one of",
    ):
        build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [{"role": "woofer", "model": "A", "driver_class": "ceramic"}],
                "crossover_candidates": [],
            },
        )


def test_driver_class_accepts_every_hoisted_value():
    from jasper.active_speaker._common import DRIVER_CLASSES

    for value in DRIVER_CLASSES:
        payload = build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [{"role": "woofer", "model": "A", "driver_class": value}],
                "crossover_candidates": [],
            },
        )
        assert payload["manual_settings"]["drivers"][0]["driver_class"] == value


def test_legacy_horn_coverage_deg_draft_still_saves_and_drops_the_key(
    tmp_path: Path,
) -> None:
    """#2872: a draft written before the deletion must still round-trip.

    ``horn_coverage_deg`` was a wizard-collected number that reached no
    consumer, so it was deleted rather than kept alive.  An operator who typed
    a coverage angle before that has the key sitting in their saved draft, on a
    field /sound/ no longer shows them.  Refusing that record would strand them
    on a save they cannot fix from the page, so every gate that re-validates a
    stored driver TOLERATES the key and every normaliser DROPS it: the draft
    saves, and the value does not come back.
    """

    path = tmp_path / "active_speaker_design_draft.json"
    legacy_manual = {
        "drivers": [
            {
                "target_id": "mono:tweeter",
                "role": "tweeter",
                "model": "Legacy Horn",
                "driver_class": "compression_horn",
                "horn_coverage_deg": 90,
            }
        ],
        "crossover_candidates": [],
    }
    # Written the way an older build wrote it -- by hand, because today's
    # save path can no longer produce this file.
    path.write_text(
        json.dumps(
            {
                "artifact_schema_version": 1,
                "kind": DESIGN_DRAFT_KIND,
                "status": "ready_for_review",
                "revision": 3,
                "operator_inputs": {},
                "manual_settings": legacy_manual,
            }
        ),
        encoding="utf-8",
    )

    loaded = load_design_draft(path)
    assert loaded["manual_settings"]["drivers"][0]["horn_coverage_deg"] == 90

    # The re-save the browser performs on the next visit: no refusal, and the
    # retired key is gone from what lands on disk.
    saved = save_design_draft(
        _topology(),
        manual_settings=loaded["manual_settings"],
        operator_inputs=loaded["operator_inputs"],
        expected_revision=loaded["revision"],
        path=path,
        created_at="2026-08-22T12:00:00Z",
    )
    tweeter = saved["manual_settings"]["drivers"][0]
    assert "horn_coverage_deg" not in tweeter
    assert tweeter["driver_class"] == "compression_horn"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert all(
        "horn_coverage_deg" not in driver
        for driver in on_disk["manual_settings"]["drivers"]
    )
    # ...and the safety profile built from the same record survived its own
    # re-validation, which is the second allowlist the key has to clear.
    assert saved["driver_safety_profile"] is not None


def test_unknown_driver_field_is_still_refused():
    """The legacy tolerance is one named key, not an open door.

    ``_reject_unknown_keys`` is what catches an extension-by-typo before it
    silently becomes a value nothing reads -- exactly the defect #2872 deleted.
    """

    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match=r"manual_settings\.driver has unknown fields: horn_coverage_degrees",
    ):
        build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [
                    {"role": "tweeter", "model": "B", "horn_coverage_degrees": 90}
                ],
                "crossover_candidates": [],
            },
        )


def test_radiating_diameter_mm_must_be_positive():
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match=r"radiating_diameter_mm must be > 0",
    ):
        build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [
                    {"role": "woofer", "model": "A", "radiating_diameter_mm": 0}
                ],
                "crossover_candidates": [],
            },
        )


def test_pad_error_surfaces_as_design_draft_error():
    # driver_pad.DriverPadError is caught and re-raised as
    # ActiveSpeakerDesignDraftError -- the same pattern as
    # DriverSafetyProfileError, so callers only need to catch one exception.
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match=r"requires nominal_impedance_ohm",
    ):
        build_design_draft(
            _topology(),
            manual_settings={
                "drivers": [
                    {
                        "role": "tweeter",
                        "model": "B",
                        "pad": {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
                    }
                ],
                "crossover_candidates": [],
            },
        )


def test_regenerate_crossover_preview_path_re_normalises_a_saved_pad_without_raising():
    # #1665 follow-up bug + fix: normalise_pad wrote attenuation_db /
    # effective_impedance_ohm into the pad record it returned, then rejected
    # those same fields as unknown input when that returned record was fed
    # back in as fresh input. This reproduces the exact live JTS3 failure
    # shape: crossover-v2 session-start calls
    # web_commissioning.regenerate_crossover_preview_from_current_draft,
    # which reloads the saved design draft and rebuilds it via (read
    # verbatim from that function, ~line 276-284 as of #1665):
    #
    #     draft = build_design_draft(
    #         topology,
    #         driver_research_request=draft.get("driver_research_request"),
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
                {"role": "woofer", "model": "A"},
                {
                    "role": "tweeter",
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
        driver_research_request=saved.get("driver_research_request"),
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

    for field in ("driver_class", "radiating_diameter_mm"):
        assert research_driver[field] == manual_driver[field] == common[field]


# --- declared_effective_driver_sensitivities: sensitivity with pad folded in -


def test_declared_effective_driver_sensitivities_folds_the_pad():
    draft = {
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": 83.3},
                {
                    "role": "tweeter",
                    "sensitivity_db_2v83_1m": 108.0,
                    "pad": {"kind": "direct_db", "attenuation_db": -14.4},
                },
            ],
            "crossover_candidates": [],
        },
    }
    assert declared_effective_driver_sensitivities(draft) == {
        "woofer": 83.3,
        "tweeter": pytest.approx(93.6),
    }
    # Without folding, the tweeter would still read 108.0 -- confirm the two
    # readers genuinely disagree once a pad is declared.
    assert declared_driver_sensitivities(draft)["tweeter"] == 108.0


def test_declared_effective_driver_sensitivities_matches_naked_reader_without_a_pad():
    draft = {
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": 83.3},
                {"role": "tweeter", "sensitivity_db_2v83_1m": 108.5},
            ],
        },
    }
    assert declared_effective_driver_sensitivities(draft) == declared_driver_sensitivities(
        draft
    )


def test_declared_effective_driver_sensitivities_fails_soft_on_absent_or_malformed():
    assert declared_effective_driver_sensitivities(None) == {}
    assert declared_effective_driver_sensitivities({}) == {}
    assert declared_effective_driver_sensitivities({"manual_settings": None}) == {}
    assert (
        declared_effective_driver_sensitivities(
            {"manual_settings": {"drivers": "not-a-list"}}
        )
        == {}
    )


def test_declared_effective_driver_sensitivities_drops_conflicting_pad_rows():
    # Same naked sensitivity, but the pads disagree -- the EFFECTIVE figure is
    # ambiguous even though the naked reader (declared_driver_sensitivities)
    # would see no conflict at all.
    draft = {
        "manual_settings": {
            "drivers": [
                {
                    "role": "tweeter", "target_id": "left:tweeter",
                    "sensitivity_db_2v83_1m": 108.0,
                    "pad": {"kind": "direct_db", "attenuation_db": -14.4},
                },
                {
                    "role": "tweeter", "target_id": "right:tweeter",
                    "sensitivity_db_2v83_1m": 108.0,
                    "pad": {"kind": "direct_db", "attenuation_db": -6.0},
                },
            ],
        },
    }
    assert declared_driver_sensitivities(draft) == {"tweeter": 108.0}
    assert declared_effective_driver_sensitivities(draft) == {}


def test_declared_effective_driver_sensitivities_survives_the_normalised_persisted_draft():
    payload = build_design_draft(
        _topology(),
        manual_settings={
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": 83.3},
                {
                    "role": "tweeter",
                    "sensitivity_db_2v83_1m": 108.0,
                    "nominal_impedance_ohm": 8,
                    "pad": {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
                },
            ],
            "crossover_candidates": [],
        },
    )
    assert declared_effective_driver_sensitivities(payload) == {
        "woofer": 83.3,
        "tweeter": pytest.approx(93.6),
    }
