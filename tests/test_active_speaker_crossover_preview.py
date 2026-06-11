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


def _topology(*, mode: str = "active_2_way") -> OutputTopology:
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
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": mode,
                "channels": channels,
            }
        ],
        "routing": {"mono_group_id": "mono"},
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


def test_crossover_preview_blocks_missing_research() -> None:
    payload = build_crossover_preview(
        _draft(research=None),
        created_at="2026-06-10T12:30:00Z",
    )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_prepare_protected_startup_config"] is False
    assert "driver_research_missing" in {issue["code"] for issue in payload["issues"]}


def test_crossover_preview_raises_frequency_to_driver_floor() -> None:
    research = _research()
    research["crossover_candidates"][0]["frequency_hz"] = 1800

    payload = build_crossover_preview(
        _draft(research=research),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert payload["status"] == "ready_for_protected_staging"
    assert crossover["proposed_frequency_hz"] == 2500
    assert "crossover_frequency_raised_for_driver_floor" in {
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
