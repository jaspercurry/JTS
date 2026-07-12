# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

from jasper.active_speaker import (
    CROSSOVER_PREVIEW_KIND,
    build_crossover_preview,
    load_crossover_preview,
    save_crossover_preview,
)
from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology(*, mode: str = "active_2_way", with_subwoofer: bool = False) -> OutputTopology:
    channels = [
        {
            "role": "woofer",
            "physical_output_index": 0,
            "identity_verified": True,
        },
        {
            "role": "tweeter",
            "physical_output_index": 1,
            "identity_verified": True,
            "startup_muted": True,
            "protection_required": True,
            "protection_status": "software_guard_requested",
        },
    ]
    if mode == "active_3_way":
        channels = [
            {
                "role": "woofer",
                "physical_output_index": 0,
                "identity_verified": True,
            },
            {
                "role": "mid",
                "physical_output_index": 1,
                "identity_verified": True,
            },
            {
                "role": "tweeter",
                "physical_output_index": 2,
                "identity_verified": True,
                "startup_muted": True,
                "protection_required": True,
                "protection_status": "software_guard_requested",
            },
        ]
    if mode == "full_range_passive":
        channels = [
            {
                "role": "full_range",
                "physical_output_index": 0,
                "identity_verified": True,
            }
        ]
    groups = [
        {
            "id": "mono",
            "label": "Mono cabinet",
            "kind": "mono",
            "mode": mode,
            "channels": channels,
        }
    ]
    routing = {"mono_group_id": "mono"}
    if with_subwoofer:
        groups.append({
            "id": "sub",
            "label": "Subwoofer",
            "kind": "subwoofer",
            "mode": "subwoofer",
            "channels": [
                {
                    "role": "subwoofer",
                    "physical_output_index": 3 if mode == "active_3_way" else 2,
                    "identity_verified": True,
                    "startup_muted": True,
                }
            ],
        })
        routing["subwoofer_group_ids"] = ["sub"]
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono cabinet",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
        },
        "speaker_groups": groups,
        "routing": routing,
    })


def _research() -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [
            {
                "role": "woofer",
                "model": "Epique E150HE-44",
                "usable_frequency_range_hz": [45, 5000],
                "recommended_lowpass_hz": 2500,
                "sources": ["https://example.test/woofer"],
            },
            {
                "role": "tweeter",
                "model": "F110M-8",
                "recommended_highpass_hz": 2500,
                "do_not_test_below_hz": 1200,
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
            }
        ],
    }


_DEFAULT_RESEARCH = object()


def _draft(
    *,
    topology: OutputTopology | None = None,
    research: dict | None | object = _DEFAULT_RESEARCH,
) -> dict:
    return build_design_draft(
        topology or _topology(),
        driver_research=_research() if research is _DEFAULT_RESEARCH else research,
        created_at="2026-06-10T12:00:00Z",
    )


def test_crossover_preview_builds_no_audio_filter_intent() -> None:
    payload = build_crossover_preview(
        _draft(),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert payload["kind"] == CROSSOVER_PREVIEW_KIND
    assert payload["status"] == "ready_for_protected_staging"
    assert payload["permissions"]["may_prepare_protected_startup_config"] is True
    assert payload["permissions"]["may_not_emit_camilla_yaml"] is True
    assert payload["safety"]["no_audio"] is True
    assert payload["safety"]["loads_camilla"] is False
    assert payload["safety"]["applies_filters"] is False
    assert payload["drivers"]["tweeter"]["model"] == "F110M-8"
    assert crossover["proposed_frequency_hz"] == 2500
    assert [item["filter"] for item in crossover["filters"]] == ["lowpass", "highpass"]
    assert crossover["filters"][1]["channel"]["startup_muted"] is True


def test_crossover_preview_does_not_require_optional_subwoofer_research() -> None:
    draft = build_design_draft(
        _topology(with_subwoofer=True),
        driver_research=_research(),
        created_at="2026-06-10T12:00:00Z",
    )

    payload = build_crossover_preview(
        draft,
        created_at="2026-06-10T12:30:00Z",
    )

    assert draft["status"] == "ready_for_review"
    assert draft["summary"]["topology_roles"] == ["woofer", "tweeter", "subwoofer"]
    assert draft["summary"]["required_driver_info_roles"] == ["woofer", "tweeter"]
    assert draft["summary"]["missing_driver_info_roles"] == []
    assert payload["status"] == "ready_for_protected_staging"
    assert payload["permissions"]["may_prepare_protected_startup_config"] is True


def test_crossover_preview_blocks_missing_research() -> None:
    payload = build_crossover_preview(
        _draft(research=None),
        created_at="2026-06-10T12:30:00Z",
    )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_prepare_protected_startup_config"] is False
    assert "driver_research_missing" in {issue["code"] for issue in payload["issues"]}


def test_crossover_preview_carries_polarity_and_delay_from_candidate() -> None:
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_research(),
            manual_settings={
                "drivers": [],
                "crossover_candidates": [{
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2200,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                    "lower_polarity": "non-inverted",
                    "upper_polarity": "inverted",
                    "delay_ms": 0.4,
                    "delay_target_role": "tweeter",
                }],
            },
            created_at="2026-06-10T12:00:00Z",
        ),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert crossover["lower_polarity"] == "non-inverted"
    assert crossover["upper_polarity"] == "inverted"
    assert crossover["delay_ms"] == 0.4
    assert crossover["delay_target_role"] == "tweeter"


def test_crossover_preview_reversed_candidate_between_roles_realigns_polarity() -> None:
    # The candidate declares its pair as [tweeter, woofer] — reversed from this
    # function's own (lower_role, upper_role)=(woofer, tweeter) convention for
    # this group's mode. _build_crossover must realign so "lower_polarity" in
    # the emitted preview always describes the woofer, not whichever role
    # happened to be listed first in the candidate.
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_research(),
            manual_settings={
                "drivers": [],
                "crossover_candidates": [{
                    "between_roles": ["tweeter", "woofer"],
                    "frequency_hz": 2200,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                    "lower_polarity": "inverted",
                    "upper_polarity": "non-inverted",
                }],
            },
            created_at="2026-06-10T12:00:00Z",
        ),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert crossover["between_roles"] == ["woofer", "tweeter"]
    # The candidate's lower_polarity (="inverted") described its own
    # between_roles[0]=tweeter, which is THIS function's upper_role.
    assert crossover["upper_polarity"] == "inverted"
    assert crossover["lower_polarity"] == "non-inverted"


def test_crossover_preview_omits_polarity_and_delay_when_candidate_lacks_them() -> None:
    payload = build_crossover_preview(_draft(), created_at="2026-06-10T12:30:00Z")
    crossover = payload["groups"][0]["crossovers"][0]

    assert "lower_polarity" not in crossover
    assert "upper_polarity" not in crossover
    assert "delay_ms" not in crossover
    assert "delay_target_role" not in crossover


def test_crossover_preview_no_audio_invariant_holds_with_polarity_and_delay() -> None:
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_research(),
            manual_settings={
                "drivers": [],
                "crossover_candidates": [{
                    "between_roles": ["woofer", "tweeter"],
                    "frequency_hz": 2200,
                    "filter_type": "Linkwitz-Riley",
                    "slope_db_per_octave": 24,
                    "confidence": "medium",
                    "lower_polarity": "inverted",
                    "delay_ms": 0.4,
                    "delay_target_role": "woofer",
                }],
            },
            created_at="2026-06-10T12:00:00Z",
        ),
        created_at="2026-06-10T12:30:00Z",
    )

    assert payload["safety"]["no_audio"] is True
    assert payload["safety"]["loads_camilla"] is False
    assert payload["safety"]["applies_filters"] is False
    assert payload["safety"]["emits_camilla_yaml"] is False
    assert payload["safety"]["authorizes_playback"] is False
    assert payload["permissions"]["may_not_emit_camilla_yaml"] is True
    assert payload["permissions"]["may_not_load_camilla"] is True
    assert payload["permissions"]["may_not_emit_audio"] is True
    assert payload["permissions"]["may_not_authorize_playback"] is True


def test_crossover_preview_prefers_manual_settings_over_imported_research() -> None:
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_research(),
            manual_settings={
                "drivers": [
                    {"role": "woofer", "model": "Manual woofer"},
                    {
                        "role": "tweeter",
                        "model": "Manual tweeter",
                        "do_not_test_below_hz": 1800,
                    },
                ],
                "crossover_candidates": [
                    {
                        "between_roles": ["woofer", "tweeter"],
                        "frequency_hz": 3200,
                        "filter_type": "Linkwitz-Riley",
                        "slope_db_per_octave": 24,
                        "confidence": "medium",
                    }
                ],
            },
            created_at="2026-06-10T12:00:00Z",
        ),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert payload["status"] == "ready_for_protected_staging"
    assert payload["drivers"]["tweeter"]["model"] == "Manual tweeter"
    assert crossover["source"] == "manual_settings"
    assert crossover["proposed_frequency_hz"] == 3200


def test_crossover_preview_warns_below_recommended_driver_floor() -> None:
    research = _research()
    research["crossover_candidates"][0]["frequency_hz"] = 1800

    payload = build_crossover_preview(
        _draft(research=research),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert payload["status"] == "ready_for_protected_staging"
    assert crossover["proposed_frequency_hz"] == 1800
    assert "crossover_below_recommended_driver_floor" in {
        issue["code"] for issue in crossover["issues"]
    }


def test_crossover_preview_prefers_usable_candidate_over_missing_frequency() -> None:
    research = _research()
    research["crossover_candidates"].insert(0, {
        "between_roles": ["woofer", "tweeter"],
        "filter_type": "Linkwitz-Riley",
        "slope_db_per_octave": 24,
        "confidence": "high",
    })

    payload = build_crossover_preview(
        _draft(research=research),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert payload["status"] == "ready_for_protected_staging"
    assert crossover["proposed_frequency_hz"] == 2500
    assert "crossover_candidate_frequency_missing" not in {
        issue["code"] for issue in crossover["issues"]
    }


def test_crossover_preview_blocks_incomplete_active_three_way() -> None:
    research = _research()
    research["drivers"].append({
        "role": "mid",
        "model": "Example mid",
        "usable_frequency_range_hz": [250, 4000],
    })

    payload = build_crossover_preview(
        _draft(topology=_topology(mode="active_3_way"), research=research),
        created_at="2026-06-10T12:30:00Z",
    )

    assert payload["status"] == "blocked"
    assert payload["summary"]["active_crossover_count"] == 2
    assert "crossover_candidate_missing" in {issue["code"] for issue in payload["issues"]}


def test_crossover_preview_is_not_applicable_to_passive_full_range() -> None:
    research = {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [{"role": "full_range", "model": "Example full range"}],
        "crossover_candidates": [],
    }

    payload = build_crossover_preview(
        _draft(topology=_topology(mode="full_range_passive"), research=research),
        created_at="2026-06-10T12:30:00Z",
    )

    assert payload["status"] == "not_applicable"
    assert payload["summary"]["active_crossover_count"] == 0
    assert payload["permissions"]["may_prepare_protected_startup_config"] is False


def test_save_and_load_crossover_preview_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "crossover_preview.json"

    saved = save_crossover_preview(
        _draft(),
        path=path,
        created_at="2026-06-10T12:30:00Z",
    )
    loaded = load_crossover_preview(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert saved["status"] == "ready_for_protected_staging"
    assert loaded["kind"] == CROSSOVER_PREVIEW_KIND
    assert raw["safety"]["authorizes_playback"] is False


def test_load_crossover_preview_marks_changed_design_draft_stale(tmp_path: Path) -> None:
    path = tmp_path / "crossover_preview.json"
    stale_draft = _draft()
    stale_draft["driver_research"]["crossover_candidates"][0]["frequency_hz"] = 3200

    save_crossover_preview(
        _draft(),
        path=path,
        created_at="2026-06-10T12:30:00Z",
    )
    loaded = load_crossover_preview(path, current_design_draft=stale_draft)

    assert loaded["status"] == "stale"
    assert loaded["permissions"]["may_prepare_protected_startup_config"] is False
    assert "crossover_preview_stale_design_draft" in {
        issue["code"] for issue in loaded["issues"]
    }


def test_load_crossover_preview_fails_soft_on_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "crossover_preview.json"
    path.write_text("{", encoding="utf-8")

    payload = load_crossover_preview(path)

    assert payload["status"] == "unreadable"
    assert payload["issues"][0]["code"] == "crossover_preview_unreadable"


# --- Compression-driver protection-floor gate (do-not-test vs recommended_highpass) ---
#
# Regression cover for the DE250-on-a-horn commissioning path: the preview must
# preserve operator-entered crossover values instead of silently raising them,
# while still failing closed at or below the tweeter's do-not-test line.


def _de250_research(
    *,
    candidate_hz: float,
    recommended_highpass_hz: float | None = 2000,
    do_not_test_below_hz: float | None = 1600,
    confidence: str = "medium",
) -> dict:
    tweeter: dict = {
        "role": "tweeter",
        "model": "DE250-8",
        "sensitivity_db_2v83_1m": 108.5,
        "usable_frequency_range_hz": [1000, 18000],
    }
    if recommended_highpass_hz is not None:
        tweeter["recommended_highpass_hz"] = recommended_highpass_hz
    if do_not_test_below_hz is not None:
        tweeter["do_not_test_below_hz"] = do_not_test_below_hz
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [
            {
                "role": "woofer",
                "model": "Epique E150HE-44",
                "sensitivity_db_2v83_1m": 83.3,
                "usable_frequency_range_hz": [30, 4000],
                "recommended_lowpass_hz": 2000,
            },
            tweeter,
        ],
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": candidate_hz,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": confidence,
            }
        ],
    }


def _crossover(payload: dict) -> dict:
    return payload["groups"][0]["crossovers"][0]


def test_crossover_keeps_operator_value_above_do_not_test_floor() -> None:
    # The reported case: 1800 Hz is below the recommended 2000 Hz highpass but
    # above the 1600 Hz do-not-test line. Keep the operator value and warn.
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_de250_research(candidate_hz=1800),
            created_at="2026-06-19T12:00:00Z",
        ),
        created_at="2026-06-19T12:30:00Z",
    )
    crossover = _crossover(payload)

    assert payload["status"] == "ready_for_protected_staging"
    assert crossover["proposed_frequency_hz"] == 1800
    assert crossover["do_not_test_below_hz"] == 1600
    codes = {issue["code"] for issue in crossover["issues"]}
    assert "crossover_below_recommended_driver_floor" in codes
    assert "crossover_below_do_not_test_floor" not in codes
    assert [item["filter"] for item in crossover["filters"]] == ["lowpass", "highpass"]
    assert all(item["frequency_hz"] == 1800 for item in crossover["filters"])


def test_crossover_blocks_at_do_not_test_floor_without_safe_highpass() -> None:
    # No recommended_highpass to rescue it: a crossover sitting on the
    # do_not_test line must fail closed — blocker, no filter intent emitted.
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_de250_research(
                candidate_hz=1600,
                recommended_highpass_hz=None,
                do_not_test_below_hz=1600,
                confidence="low",
            ),
            created_at="2026-06-19T12:00:00Z",
        ),
        created_at="2026-06-19T12:30:00Z",
    )
    crossover = _crossover(payload)

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_prepare_protected_startup_config"] is False
    assert crossover["status"] == "blocked"
    assert crossover["proposed_frequency_hz"] is None
    assert crossover["filters"] == []
    assert "crossover_below_do_not_test_floor" in {
        issue["code"] for issue in crossover["issues"]
    }


def test_crossover_hard_floor_blocks_below_operator_value() -> None:
    # Pathological research where recommended_highpass sits below do_not_test:
    # the protection line remains the final authority.
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_de250_research(
                candidate_hz=1400,
                recommended_highpass_hz=1500,
                do_not_test_below_hz=1600,
            ),
            created_at="2026-06-19T12:00:00Z",
        ),
        created_at="2026-06-19T12:30:00Z",
    )
    crossover = _crossover(payload)

    assert payload["status"] == "blocked"
    assert crossover["proposed_frequency_hz"] is None
    assert crossover["filters"] == []
    assert "crossover_below_do_not_test_floor" in {
        issue["code"] for issue in crossover["issues"]
    }


def test_crossover_persisted_low_value_blocks_instead_of_overriding() -> None:
    # Reproduces the exact persisted draft from the bug: the form saved the
    # low-confidence 1600 candidate into manual_settings (which outranks the
    # research candidates), alongside the research candidates [2000 medium,
    # 1600 low]. The preview must not silently replace the persisted manual
    # value with 2000 Hz; it should block until the operator changes the value.
    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research={
                "artifact_schema_version": 1,
                "kind": DRIVER_RESEARCH_KIND,
                "drivers": _de250_research(candidate_hz=2000)["drivers"],
                "crossover_candidates": [
                    {
                        "between_roles": ["woofer", "tweeter"],
                        "frequency_hz": 2000,
                        "filter_type": "Linkwitz-Riley",
                        "slope_db_per_octave": 24,
                        "confidence": "medium",
                    },
                    {
                        "between_roles": ["woofer", "tweeter"],
                        "frequency_hz": 1600,
                        "filter_type": "Linkwitz-Riley",
                        "slope_db_per_octave": 24,
                        "confidence": "low",
                    },
                ],
            },
            manual_settings={
                "crossover_candidates": [
                    {
                        "between_roles": ["woofer", "tweeter"],
                        "frequency_hz": 1600,
                        "filter_type": "Linkwitz-Riley",
                        "slope_db_per_octave": 24,
                        "confidence": "medium",
                    }
                ],
            },
            created_at="2026-06-19T12:00:00Z",
        ),
        created_at="2026-06-19T12:30:00Z",
    )
    crossover = _crossover(payload)

    assert payload["status"] == "blocked"
    assert crossover["proposed_frequency_hz"] is None
    assert crossover["filters"] == []
    assert "crossover_below_do_not_test_floor" in {
        issue["code"] for issue in crossover["issues"]
    }
