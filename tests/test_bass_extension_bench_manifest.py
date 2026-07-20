# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator manifest authoring — complete inputs pass, a missing input refuses."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

from jasper.bass_extension.bench.manifest import (
    STIMULUS_ROLES,
    CampaignManifest,
    ManifestRefusal,
    author_campaign_manifest,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _request() -> dict[str, Any]:
    return {
        "requested_stimulus_band_hz": [30.0, 200.0],
        "requested_stimulus_effective_peak_dbfs": -30.0,
        "requested_commanded_main_volume_db": -35.0,
        "requested_hold_duration_s": 12.0,
        "requested_cooldown_s": 4.0,
        "requested_repeat_count": 2,
        "stimulus_generator_identity": "bench-generator-v1",
    }


def _inputs(*target_ids: str) -> dict[str, Any]:
    return {
        "driver_safety_fingerprint": _sha("driver-safety"),
        "margin_policy_name": "conservative",
        "margin_policy_fingerprint": _sha("margin"),
        "requests": {
            target_id: {role: _request() for role in STIMULUS_ROLES}
            for target_id in target_ids
        },
    }


def test_complete_operator_inputs_author_a_manifest() -> None:
    manifest = author_campaign_manifest(_inputs("deep", "natural"), target_ids=("deep", "natural"))
    assert isinstance(manifest, CampaignManifest)
    assert manifest.margin_policy_name == "conservative"
    assert set(manifest.requests) == {"deep", "natural"}
    assert set(manifest.requests["deep"]) == set(STIMULUS_ROLES)
    request = manifest.requests["deep"]["sweep_transparency"]
    assert request.requested_stimulus_band_hz == (30.0, 200.0)
    assert request.requested_repeat_count == 2
    payload = manifest.to_dict()
    assert payload["kind"] == "jts_bass_extension_bench_campaign_manifest"
    assert payload["requests"]["deep"]["sweep_transparency"]["requested_repeat_count"] == 2


def test_missing_scalar_input_refuses_and_names_the_path() -> None:
    inputs = _inputs("deep")
    del inputs["requests"]["deep"]["sustain_stress"]["requested_hold_duration_s"]
    with pytest.raises(ManifestRefusal) as excinfo:
        author_campaign_manifest(inputs, target_ids=("deep",))
    assert (
        "requests.deep.sustain_stress.requested_hold_duration_s"
        in excinfo.value.missing_paths
    )


def test_missing_top_level_fingerprint_refuses() -> None:
    inputs = _inputs("deep")
    del inputs["driver_safety_fingerprint"]
    with pytest.raises(ManifestRefusal) as excinfo:
        author_campaign_manifest(inputs, target_ids=("deep",))
    assert "driver_safety_fingerprint" in excinfo.value.missing_paths


def test_missing_role_refuses() -> None:
    inputs = _inputs("deep")
    del inputs["requests"]["deep"]["digital_transfer_probe"]
    with pytest.raises(ManifestRefusal) as excinfo:
        author_campaign_manifest(inputs, target_ids=("deep",))
    assert any(
        "digital_transfer_probe" in path for path in excinfo.value.missing_paths
    )


def test_no_default_is_ever_filled_for_a_malformed_value() -> None:
    inputs = _inputs("deep")
    inputs["requests"]["deep"]["sweep_transparency"]["requested_repeat_count"] = 0
    with pytest.raises(ManifestRefusal) as excinfo:
        author_campaign_manifest(inputs, target_ids=("deep",))
    assert (
        "requests.deep.sweep_transparency.requested_repeat_count"
        in excinfo.value.missing_paths
    )


def test_absent_target_requests_refuse() -> None:
    inputs = _inputs("deep")
    with pytest.raises(ManifestRefusal) as excinfo:
        author_campaign_manifest(inputs, target_ids=("deep", "natural"))
    assert "requests.natural" in excinfo.value.missing_paths


def test_refusal_paths_are_sorted_and_unique() -> None:
    inputs = copy.deepcopy(_inputs("deep"))
    del inputs["requests"]["deep"]["sweep_transparency"]["requested_cooldown_s"]
    del inputs["requests"]["deep"]["sustain_stress"]["requested_cooldown_s"]
    with pytest.raises(ManifestRefusal) as excinfo:
        author_campaign_manifest(inputs, target_ids=("deep",))
    paths = excinfo.value.missing_paths
    assert list(paths) == sorted(set(paths))
