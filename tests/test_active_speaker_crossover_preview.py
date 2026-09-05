# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from jasper.active_speaker import (
    CROSSOVER_PREVIEW_KIND,
    build_crossover_preview,
    crossover_preview_fingerprint,
    load_crossover_preview,
    save_crossover_preview,
)
from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology


def _topology(*, mode: str = "active_2_way", with_subwoofer: bool = False) -> OutputTopology:
    return mono_output_topology(
        mode=mode,
        with_subwoofer=with_subwoofer,
        card_id=None,
    )


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


def _manual_draft() -> dict:
    return build_design_draft(
        _topology(),
        driver_research=_research(),
        manual_settings={
            "drivers": [
                {"role": "woofer", "gain_offset_db": 0.0},
                {"role": "tweeter", "gain_offset_db": -6.0},
            ],
            "crossover_candidates": [{
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_ms": 0.4,
                "delay_target_role": "tweeter",
            }],
        },
        created_at="2026-06-10T12:00:00Z",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("frequency_hz", 2600),
        ("slope_db_per_octave", 48),
        ("upper_polarity", "inverted"),
        ("delay_ms", 0.5),
        ("gain_offset_db", -7.0),
    ],
    ids=("fc", "order", "polarity", "delay", "trim"),
)
def test_manual_candidate_mutation_invalidates_saved_preview(
    tmp_path: Path, field: str, value: object
) -> None:
    base = _manual_draft()
    path = tmp_path / "preview.json"
    saved = save_crossover_preview(
        base, path=path, created_at="2026-06-10T12:30:00Z"
    )
    changed_manual = deepcopy(base["manual_settings"])
    if field == "gain_offset_db":
        changed_manual["drivers"][1][field] = value
    else:
        changed_manual["crossover_candidates"][0][field] = value
    changed = build_design_draft(
        _topology(),
        driver_research=_research(),
        manual_settings=changed_manual,
        created_at="2026-06-10T12:00:00Z",
    )

    loaded = load_crossover_preview(path, current_design_draft=changed)
    current = build_crossover_preview(
        changed, created_at="2026-06-10T12:30:00Z"
    )

    assert loaded["status"] == "stale"
    assert "crossover_preview_stale_design_draft" in {
        issue["code"] for issue in loaded["issues"]
    }
    assert (
        saved["source"]["design_draft_fingerprint"]
        != current["source"]["design_draft_fingerprint"]
    )
    assert crossover_preview_fingerprint(saved) != crossover_preview_fingerprint(
        current
    )


def test_saved_preview_rejects_candidate_content_tampering(tmp_path: Path) -> None:
    draft = _manual_draft()
    path = tmp_path / "preview.json"
    saved = save_crossover_preview(
        draft, path=path, created_at="2026-06-10T12:30:00Z"
    )
    saved["groups"][0]["crossovers"][0]["candidate"]["frequency_hz"] = 2700
    path.write_text(json.dumps(saved), encoding="utf-8")

    loaded = load_crossover_preview(path, current_design_draft=draft)

    assert loaded["status"] == "stale"
    assert "crossover_preview_content_mismatch" in {
        issue["code"] for issue in loaded["issues"]
    }


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


def test_crossover_preview_warns_below_the_declared_driver_floor() -> None:
    """One message per fact (#2603).

    This used to assert ``crossover_below_recommended_driver_floor``, a second
    warning that read ``recommended_highpass_hz`` directly while
    ``crossover_below_declared_protection_floor`` read the protective high-pass
    for the same driver. Those are now the same number, so the duplicate is
    gone and the surviving disclosure carries the fact. The behaviour a
    household sees is unchanged: the operator value is KEPT and the conflict is
    named.
    """

    research = _research()
    research["crossover_candidates"][0]["frequency_hz"] = 1800

    payload = build_crossover_preview(
        _draft(research=research),
        created_at="2026-06-10T12:30:00Z",
    )
    crossover = payload["groups"][0]["crossovers"][0]

    assert payload["status"] == "ready_for_protected_staging"
    assert crossover["proposed_frequency_hz"] == 1800
    codes = {issue["code"] for issue in crossover["issues"]}
    assert "crossover_below_declared_protection_floor" in codes
    assert "crossover_below_recommended_driver_floor" not in codes


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


def test_save_crossover_preview_durable_fsyncs_the_write(
    tmp_path: Path, monkeypatch,
) -> None:
    """#2292 scope 2: ``durable=True`` reaches ``atomic_write_text``'s fsync
    (file + parent directory); the default save does not fsync at all."""
    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))
    path = tmp_path / "crossover_preview.json"

    save_crossover_preview(_draft(), path=path, created_at="2026-06-10T12:30:00Z")
    assert fsync_calls == []

    save_crossover_preview(
        _draft(), path=path, created_at="2026-06-10T12:31:00Z", durable=True,
    )
    assert len(fsync_calls) == 2  # file fsync + parent-directory fsync


def test_save_crossover_preview_publishes_under_its_parent_group(
    tmp_path: Path, monkeypatch,
) -> None:
    """The 2026-08-21 jts3 defect, pinned at this writer too.

    The crossover-accept seam re-prepares this preview from the ROOT
    ``jasper-correction-web`` process, three lines after it writes the design
    draft; ``/sound/`` reads both as ``jasper-web`` (group ``jasper``).
    ``/var/lib/jasper`` is group ``jasper`` but NOT setgid, so a root write
    that kept its own group published ``root:root 0640`` and the preview
    silently disappeared from the page that just asked for it.
    """

    from jasper.active_speaker import crossover_preview as preview_mod

    real_write = preview_mod.atomic_write_text
    calls: list[dict] = []

    def recording_write(path, text, **kwargs):
        calls.append(kwargs)
        real_write(path, text, **kwargs)

    monkeypatch.setattr(preview_mod, "atomic_write_text", recording_write)
    path = tmp_path / "crossover_preview.json"

    save_crossover_preview(_draft(), path=path, created_at="2026-06-10T12:30:00Z")
    # And the durable accept-seam write keeps the same contract.
    save_crossover_preview(
        _draft(), path=path, created_at="2026-06-10T12:31:00Z", durable=True,
    )

    assert calls and all(call.get("mode") == 0o640 for call in calls)

    published = path.stat()
    assert published.st_mode & 0o777 == 0o640
    assert published.st_gid == tmp_path.stat().st_gid


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
    recommended_highpass_hz: float | None = 1600,
    do_not_test_below_hz: float | None = None,
    confidence: str = "medium",
) -> dict:
    """The DE250, declared the way #2603 ruled it must be.

    This fixture used to carry the split itself: ``recommended_highpass_hz``
    2000 alongside a ``do_not_test_below_hz`` of 1600 -- two numbers for one
    driver's low limit, and INVERTED, since 1.6 kHz is B&C's published
    Recommended Crossover and 2000 is not a published figure for this driver at
    all. The owner is now the published 1600, and ``do_not_test_below_hz`` is
    retired: still accepted so old drafts load, read by nothing.
    """
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


def test_crossover_above_the_declared_low_limit_is_kept_and_emits_filters() -> None:
    # 1800 Hz sits above the DE250's published 1600 Hz minimum, so it is simply
    # legal. Under the old split it ALSO drew a
    # `crossover_below_recommended_driver_floor` warning, because a second
    # declaration claimed the minimum was 2000 -- a number B&C never published.
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
    assert crossover["declared_protection_floor_hz"] == 1600
    codes = {issue["code"] for issue in crossover["issues"]}
    assert "crossover_below_declared_protection_floor" not in codes
    assert [item["filter"] for item in crossover["filters"]] == ["lowpass", "highpass"]
    assert all(item["frequency_hz"] == 1800 for item in crossover["filters"])


def test_a_corner_exactly_at_the_declared_low_limit_is_legal() -> None:
    """The manufacturer's minimum recommended crossover is a value it RECOMMENDS.

    Crossing at exactly 1600 Hz is what B&C's own datasheet sanctions for the
    DE250, so this page neither blocks it nor warns about it. The old split
    refused it, because the separate do-not-test line happened to sit on the
    same number and its comparison was "strictly above".
    """

    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_de250_research(candidate_hz=1600),
            created_at="2026-06-19T12:00:00Z",
        ),
        created_at="2026-06-19T12:30:00Z",
    )
    crossover = _crossover(payload)

    assert payload["status"] == "ready_for_protected_staging"
    assert crossover["proposed_frequency_hz"] == 1600
    codes = {issue["code"] for issue in crossover["issues"]}
    assert "crossover_below_declared_protection_floor" not in codes


def test_a_stored_do_not_test_below_hz_changes_nothing() -> None:
    """The retirement, asserted as an absence rather than described as one.

    ``driver_protection``'s ruling block claims the key survives only so old
    drafts load, and that no policy, band, filter or gate derives from it. That
    is a claim about something NOT happening, which nothing else in the suite
    can fail on -- the other tests simply never set the key.

    So this sets it, at the exact value the retired blocker fired on: a
    do-not-test line sitting ON the candidate corner used to land
    ``crossover_below_do_not_test_floor`` and drop the filter intent. The two
    payloads must now agree, which is only true while the key has no consumer.
    """

    def draft(**extra: float) -> dict:
        return build_design_draft(
            _topology(),
            driver_research=_de250_research(candidate_hz=1800, **extra),
            created_at="2026-06-19T12:00:00Z",
        )

    def preview(source: dict) -> dict:
        return build_crossover_preview(source, created_at="2026-06-19T12:30:00Z")

    legacy_draft = draft(do_not_test_below_hz=1800)
    # Positive control. Without this the test passes just as happily if the key
    # were silently dropped on the way in, which would prove nothing about
    # whether a stored value is inert.
    assert "do_not_test_below_hz" in json.dumps(legacy_draft)

    without = preview(draft())
    with_legacy = preview(legacy_draft)

    assert with_legacy["status"] == without["status"]
    assert _crossover(with_legacy) == _crossover(without)
    # …and the filter intent specifically, since dropping it was the retired
    # blocker's whole payload.
    assert [item["filter"] for item in _crossover(with_legacy)["filters"]] == [
        "lowpass",
        "highpass",
    ]


def test_a_corner_below_the_declared_low_limit_is_disclosed_here_and_refused_at_load() -> None:
    """#2491's architecture, now reached through ONE floor instead of two.

    This page is the household's confirm surface and stays advisory: it names
    the conflict rather than blocking, and ``path_safety`` refuses the load
    (pinned in tests/test_active_speaker_protection_floor.py). Blocking here
    would make that gate unreachable, which is what removing the duplicate
    do-not-test blocker avoids.
    """

    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_de250_research(candidate_hz=1400),
            created_at="2026-06-19T12:00:00Z",
        ),
        created_at="2026-06-19T12:30:00Z",
    )
    crossover = _crossover(payload)

    codes = {issue["code"] for issue in crossover["issues"]}
    assert "crossover_below_declared_protection_floor" in codes
    assert crossover["proposed_frequency_hz"] == 1400
    assert crossover["declared_protection_floor_hz"] == 1600


def test_an_undeclared_low_limit_neither_blocks_nor_invents_a_floor() -> None:
    """The never-nanny boundary at the preview surface: with no owner declared
    there is no floor, so nothing is refused on a number nobody supplied."""

    payload = build_crossover_preview(
        build_design_draft(
            _topology(),
            driver_research=_de250_research(
                candidate_hz=1400, recommended_highpass_hz=None
            ),
            created_at="2026-06-19T12:00:00Z",
        ),
        created_at="2026-06-19T12:30:00Z",
    )
    crossover = _crossover(payload)

    assert crossover["declared_protection_floor_hz"] is None
    codes = {issue["code"] for issue in crossover["issues"]}
    assert "crossover_below_declared_protection_floor" not in codes


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

    # The persisted manual value still WINS over the research candidates -- that
    # is what this test has always been about. What changed with #2603 is the
    # consequence: 1600 sits at the DE250's declared minimum rather than under a
    # phantom 2000, so it is legal, and the manual value is carried through
    # instead of being dropped.
    assert crossover["proposed_frequency_hz"] == 1600
    assert all(item["frequency_hz"] == 1600 for item in crossover["filters"])
    assert "crossover_below_declared_protection_floor" not in {
        issue["code"] for issue in crossover["issues"]
    }
