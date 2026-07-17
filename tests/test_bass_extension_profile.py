# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from dataclasses import replace

import numpy as np
import pytest

from jasper.active_speaker.baseline_profile import (
    baseline_candidate_fingerprint,
    topology_config_fingerprint,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.bass_extension.adapters.base import TargetSpec
from jasper.bass_extension.profile import (
    BASS_EXTENSION_ALGORITHM_VERSION,
    BassExtensionProfile,
    BassExtensionRefusal,
    bass_extension_state_summary,
    evaluate_bass_extension_profile,
    load_bass_extension_profile,
    save_bass_extension_profile,
)
from jasper.bass_extension.targets import AnchorPoint
from jasper.output_topology import OutputTopology


def _topology(topology_id: str = "test-speaker") -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": topology_id,
        "name": "Test speaker",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "card": "sndrpihifiberry",
            "physical_output_count": 8,
        },
        "speaker_groups": [],
        "routing": {},
    })


def _applied_baseline(source_fingerprint: str = "source-a") -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "source": {"fingerprint": source_fingerprint},
        "recomposition_snapshot": {"filters": ["baseline"]},
    }


def _profile(
    *,
    topology: OutputTopology | None = None,
    applied_baseline: dict | None = None,
    status: str = "accepted",
) -> BassExtensionProfile:
    topology = topology or _topology()
    applied_baseline = applied_baseline or _applied_baseline()
    artifact = ArtifactIdentity(
        bundle_kind="jts_bass_extension_bundle",
        bundle_id="commissioning-1",
        relative_path="captures/rung-1.wav",
        sha256="a" * 64,
        byte_size=4096,
    )
    return BassExtensionProfile(
        created_at="2026-07-16T12:00:00Z",
        algorithm_version=BASS_EXTENSION_ALGORITHM_VERSION,
        baseline_fingerprint=baseline_candidate_fingerprint(applied_baseline),
        topology_id=topology.topology_id,
        topology_fingerprint=topology_config_fingerprint(topology),
        bass_owner={"kind": "woofer_way", "roles": ["woofer"], "channels": [0, 1]},
        enclosure={
            "adapter_id": "sealed_v1",
            "adapter_version": 1,
            "cabinet_fingerprint": "cabinet-a",
        },
        mic_calibration_id="minidsp-umik1-abc123",
        measurement_ids=(artifact,),
        natural={"f0_hz": 61.2, "q0": 0.72, "fit_rms_db": 0.4, "notes": []},
        targets=(
            TargetSpec(
                target_id="t31",
                fp_hz=31.0,
                qp=0.65,
                filters=({"type": "LinkwitzTransform", "freq": 31.0},),
                boost_headroom_db=8.0,
                limiter_threshold_dbfs=-5.2,
                subsonic={"type": "ButterworthHighpass", "freq": 22.0, "order": 4},
            ),
            TargetSpec(
                target_id="natural",
                fp_hz=61.2,
                qp=0.72,
                filters=(),
                boost_headroom_db=0.0,
                limiter_threshold_dbfs=-1.0,
                subsonic={"type": "ButterworthHighpass", "freq": 22.0, "order": 4},
            ),
        ),
        anchors=(AnchorPoint("t31", 50, "measured"),),
        margin="normal",
        digital_margin_db=3.0,
        clean_ceiling={"listening_level": 62, "limited_by": "compression"},
        sustain_test={
            "duration_s": 60.0,
            "fundamental_sag_db": 0.7,
            "fc_shift_pct": 2.1,
            "verdict": "passed",
        },
        impedance_import={
            "source": "rew_zma",
            "fc_hz": 60.4,
            "qtc": 0.74,
            "agreement_pct": 1.3,
        },
        status=status,
    )


def _save(tmp_path, profile: BassExtensionProfile | None = None):
    path = tmp_path / "bass_extension.json"
    save_bass_extension_profile(profile or _profile(), path)
    return path


def test_profile_round_trip_uses_wave_one_types_and_atomic_mode(tmp_path):
    profile = _profile()
    path = _save(tmp_path, profile)

    assert load_bass_extension_profile(path) == profile
    assert BassExtensionProfile.from_dict(profile.to_dict()) == profile
    assert profile.profile_id.startswith("bex-")
    assert len(profile.profile_id) == 16
    assert os.stat(path).st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    ("adapter_id", "extra"),
    [
        ("ported_v1", {}),
        ("passive_radiator_v1", {"notch_hz": 24.5}),
    ],
)
def test_vented_profiles_retain_the_96_point_natural_curve(
    tmp_path, adapter_id, extra
):
    natural_curve = {
        "freqs_hz": np.geomspace(10.0, 500.0, 96).tolist(),
        "magnitude_db": [0.0] * 96,
    }
    profile = replace(
        _profile(),
        enclosure={
            "adapter_id": adapter_id,
            "adapter_version": 1,
            "cabinet_fingerprint": "cabinet-a",
        },
        natural={
            "fb_hz": 43.1,
            "knee_hz": 55.0,
            "knee_slope_db_oct": 21.0,
            "fit_rms_db": 0.4,
            "natural_curve": natural_curve,
            "notes": [],
            **extra,
        },
    )

    loaded = load_bass_extension_profile(_save(tmp_path, profile))

    assert loaded == profile
    assert loaded is not None
    assert len(loaded.natural["natural_curve"]["freqs_hz"]) == 96


def test_profile_id_excludes_created_at_but_binds_content():
    profile = _profile()
    assert replace(profile, created_at="2026-07-17T00:00:00Z").profile_id == profile.profile_id
    assert replace(profile, margin="conservative").profile_id != profile.profile_id


def test_profile_detaches_and_deeply_freezes_content_addressed_inputs():
    raw = _profile().to_dict()
    profile = BassExtensionProfile.from_dict(raw)
    original = profile.to_dict()

    raw["bass_owner"]["channels"].append(7)
    raw["targets"][0]["filters"][0]["freq"] = 99.0
    raw["natural"]["notes"].append("mutated alias")

    with pytest.raises(TypeError):
        profile.enclosure["adapter_version"] = 99
    with pytest.raises(AttributeError):
        profile.bass_owner["channels"].append(7)
    with pytest.raises(TypeError):
        profile.targets[0].filters[0]["freq"] = 99.0
    with pytest.raises(TypeError):
        profile.targets[0].subsonic["freq"] = 99.0

    assert profile.to_dict() == original
    assert profile.profile_id == BassExtensionProfile.from_dict(original).profile_id


def test_from_dict_rejects_unknown_key():
    raw = _profile().to_dict()
    raw["surprise"] = True
    with pytest.raises(ValueError, match="unknown or missing fields"):
        BassExtensionProfile.from_dict(raw)


def test_from_dict_rejects_wrong_kind():
    raw = _profile().to_dict()
    raw["kind"] = "other"
    with pytest.raises(ValueError, match="kind is unsupported"):
        BassExtensionProfile.from_dict(raw)


def test_from_dict_rejects_wrong_schema_version():
    raw = _profile().to_dict()
    raw["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version is unsupported"):
        BassExtensionProfile.from_dict(raw)


def test_from_dict_rejects_nan_anchor():
    raw = _profile().to_dict()
    raw["anchors"][0]["max_listening_level"] = float("nan")
    with pytest.raises(ValueError, match="max_listening_level"):
        BassExtensionProfile.from_dict(raw)


def test_from_dict_rejects_missing_natural_last_target():
    raw = _profile().to_dict()
    raw["targets"][-1]["target_id"] = "not-natural"
    with pytest.raises(ValueError, match="last target must be natural"):
        BassExtensionProfile.from_dict(raw)


def test_from_dict_rejects_targets_that_are_not_deepest_first():
    raw = _profile().to_dict()
    shallower = {**raw["targets"][0], "target_id": "t50", "fp_hz": 50.0}
    raw["targets"].insert(0, shallower)
    with pytest.raises(ValueError, match="ordered deepest first"):
        BassExtensionProfile.from_dict(raw)


def test_from_dict_rejects_natural_target_with_filters_or_boost():
    raw = _profile().to_dict()
    raw["targets"][-1]["filters"] = [{"type": "Peaking"}]
    raw["targets"][-1]["boost_headroom_db"] = 0.1
    with pytest.raises(ValueError, match="empty filters and 0.0 boost"):
        BassExtensionProfile.from_dict(raw)


def test_each_binding_mismatch_has_a_specific_stale_refusal(tmp_path):
    topology = _topology()
    applied = _applied_baseline()
    profile = _profile(topology=topology, applied_baseline=applied)

    baseline_path = _save(tmp_path, profile)
    baseline = evaluate_bass_extension_profile(
        path=baseline_path,
        topology=topology,
        applied_baseline_state=_applied_baseline("different"),
    )
    assert baseline.status == "stale"
    assert baseline.refusals == (BassExtensionRefusal.BASELINE_NOT_APPLIED,)

    topology_path = _save(tmp_path, profile)
    topology_result = evaluate_bass_extension_profile(
        path=topology_path,
        topology=_topology("different-speaker"),
        applied_baseline_state=applied,
    )
    assert topology_result.status == "stale"
    assert topology_result.refusals == (BassExtensionRefusal.TOPOLOGY_MISMATCH,)

    adapter_path = _save(tmp_path, replace(
        profile,
        enclosure={**profile.enclosure, "adapter_version": 99},
    ))
    adapter = evaluate_bass_extension_profile(
        path=adapter_path, topology=topology, applied_baseline_state=applied
    )
    assert adapter.status == "stale"
    assert adapter.refusals == (BassExtensionRefusal.ENCLOSURE_UNSUPPORTED,)

    algorithm_path = _save(tmp_path, replace(profile, algorithm_version="old"))
    algorithm = evaluate_bass_extension_profile(
        path=algorithm_path, topology=topology, applied_baseline_state=applied
    )
    assert algorithm.status == "stale"
    assert algorithm.refusals == (BassExtensionRefusal.PROFILE_STALE,)


def test_multiple_binding_mismatches_accumulate(tmp_path):
    profile = replace(
        _profile(),
        algorithm_version="old",
        enclosure={
            "adapter_id": "sealed_v1",
            "adapter_version": 99,
            "cabinet_fingerprint": "cabinet-a",
        },
    )
    result = evaluate_bass_extension_profile(
        path=_save(tmp_path, profile),
        topology=_topology("different-speaker"),
        applied_baseline_state=_applied_baseline("different"),
    )
    assert result.status == "stale"
    assert result.refusals == (
        BassExtensionRefusal.BASELINE_NOT_APPLIED,
        BassExtensionRefusal.TOPOLOGY_MISMATCH,
        BassExtensionRefusal.ENCLOSURE_UNSUPPORTED,
        BassExtensionRefusal.PROFILE_STALE,
    )
    assert "baseline fingerprint mismatch" in result.detail
    assert "algorithm version mismatch" in result.detail


def test_missing_garbage_and_bypassed_statuses(tmp_path):
    missing = evaluate_bass_extension_profile(
        path=tmp_path / "missing.json",
        topology=_topology(),
        applied_baseline_state=_applied_baseline(),
    )
    assert missing.status == "missing"

    garbage_path = tmp_path / "garbage.json"
    garbage_path.write_bytes(b"not-json\x00")
    malformed = evaluate_bass_extension_profile(
        path=garbage_path,
        topology=_topology(),
        applied_baseline_state=_applied_baseline(),
    )
    assert malformed.status == "malformed"
    assert malformed.profile is None
    assert load_bass_extension_profile(garbage_path) is None

    bypassed = evaluate_bass_extension_profile(
        path=_save(tmp_path, _profile(status="bypassed")),
        topology=_topology(),
        applied_baseline_state=_applied_baseline(),
    )
    assert bypassed.status == "bypassed"


def test_state_summary_is_fail_soft_and_projects_commissioned_profile(tmp_path):
    profile = _profile()
    path = _save(tmp_path, profile)
    assert bass_extension_state_summary(path) == {
        "commissioned": True,
        "status": "accepted",
        "profile_id": profile.profile_id,
        "deepest_hz": 31.0,
        "natural_hz": 61.2,
        "margin": "normal",
        "anchors": [{
            "target_id": "t31",
            "max_listening_level": 50,
            "evidence": "measured",
        }],
    }
    assert bass_extension_state_summary(tmp_path) is None


def test_profile_json_has_no_non_finite_values(tmp_path):
    raw = _profile().to_dict()
    raw["targets"][0]["filters"][0]["freq"] = float("nan")
    path = tmp_path / "nonfinite.json"
    path.write_text(json.dumps(raw))
    result = evaluate_bass_extension_profile(
        path=path,
        topology=_topology(),
        applied_baseline_state=_applied_baseline(),
    )
    assert result.status == "malformed"
    assert "non-finite" in result.detail
