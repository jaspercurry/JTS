# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.baseline_profile import (
    baseline_candidate_fingerprint,
    topology_config_fingerprint,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.evidence_identity import ExactDspStateIdentity
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GraphSafety,
)
from jasper.bass_extension import (
    BassExtensionApplyError,
    apply_bass_extension,
    bypass_bass_extension,
    recover_pending_bass_extension_apply,
)
import jasper.bass_extension as bass_extension_module
import jasper.bass_extension.profile as profile_module
import jasper.active_speaker.runtime_contract as runtime_contract_module
import jasper.sound.graph_carrier as graph_carrier_module
import jasper.multiroom.config as multiroom_config_module
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
        "adapter_id": "sealed_v1",
        "runtime_eligible": True,
        "runtime_deferred_reason": None,
        "apply_recovery_required": False,
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


class _TransactionCamilla:
    def __init__(self, selected: Path) -> None:
        self.selected = selected
        self.active = selected.read_text(encoding="utf-8")
        self.reload_count = 0
        self.path_reads = 0
        self.fail_path_when_reload_count = None

    async def reload(self, *, best_effort=False):
        self.reload_count += 1
        self.active = self.selected.read_text(encoding="utf-8")
        return True

    async def get_config_file_path(self, *, best_effort=False):
        self.path_reads += 1
        if self.reload_count == self.fail_path_when_reload_count:
            return str(self.selected.with_name("wrong.yml"))
        return str(self.selected)

    async def get_active_config_raw(self, *, best_effort=False):
        return self.active


class _AdmissionAwareTransactionCamilla(_TransactionCamilla):
    def __init__(self, selected: Path, intent_path: Path) -> None:
        super().__init__(selected)
        self.intent_path = intent_path

    async def reload(self, *, best_effort=False):
        from jasper.dsp_apply import camilla_graph_mutation

        async with camilla_graph_mutation(
            source="test.transaction_camilla.reload",
            lock_path=self.selected.parent / ".dsp_apply.lock",
            bass_extension_intent_path=self.intent_path,
        ):
            return await super().reload(best_effort=best_effort)


def _transaction_harness(monkeypatch, tmp_path, *, predecessor=None):
    topology = _topology()
    applied = {**_applied_baseline(), "status": "applied"}
    selected = tmp_path / "configs" / "active_speaker_baseline.yml"
    selected.parent.mkdir()
    graph_kind = (
        "sealed"
        if predecessor is not None
        and predecessor.status == "accepted"
        and predecessor.enclosure["adapter_id"] == "sealed_v1"
        else "plain"
    )
    selected.write_text(f"---\ngraph: {graph_kind}\n", encoding="utf-8")
    selected.chmod(0o664)
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {selected}\n", encoding="utf-8")
    applied_path = tmp_path / "applied.json"
    applied_path.write_text(json.dumps(applied), encoding="utf-8")
    profile_path = tmp_path / "bass.json"
    if predecessor is not None:
        save_bass_extension_profile(predecessor, profile_path)
    intent_path = tmp_path / "intent.json"
    staged_path = tmp_path / "staged.json"
    cam = _TransactionCamilla(selected)

    def recompose(_topology, *, desired_profile, **_kwargs):
        sealed = bool(
            desired_profile is not None
            and desired_profile.status == "accepted"
            and desired_profile.enclosure["adapter_id"] == "sealed_v1"
        )
        return f"---\ngraph: {'sealed' if sealed else 'plain'}\n"

    async def active_proof(**_kwargs):
        return None

    monkeypatch.setattr(
        bass_extension_module, "_bonded_or_driver_carrier", lambda _text: False
    )
    monkeypatch.setattr(
        bass_extension_module, "_active_proof", active_proof
    )
    monkeypatch.setattr(
        graph_carrier_module,
        "recompose_active_baseline_for_bass_extension",
        recompose,
    )
    monkeypatch.setattr(
        runtime_contract_module,
        "classify_bass_extension_graph",
        lambda *_args, **_kwargs: GraphSafety(
            classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=True,
        ),
    )
    paths = {
        "topology": topology,
        "controller": cam,
        "statefile_path": statefile,
        "applied_baseline_path": applied_path,
        "profile_path": profile_path,
        "intent_path": intent_path,
        "staged_metadata_path": staged_path,
        "config_dir": selected.parent,
        "validate": lambda path: SimpleNamespace(ok_to_apply=True),
    }
    return applied, selected, profile_path, intent_path, statefile, cam, paths


@pytest.mark.asyncio
async def test_apply_and_bypass_commit_natural_graph_then_profile(monkeypatch, tmp_path):
    applied = {**_applied_baseline(), "status": "applied"}
    desired = _profile(applied_baseline=applied)
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        statefile,
        cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path)

    await apply_bass_extension(desired, **paths)

    assert selected.read_text(encoding="utf-8") == "---\ngraph: sealed\n"
    assert load_bass_extension_profile(profile_path) == desired
    assert not intent_path.exists()
    assert statefile.read_text(encoding="utf-8") == f"config_path: {selected}\n"
    assert cam.reload_count == 2

    await bypass_bass_extension(**paths)

    assert selected.read_text(encoding="utf-8") == "---\ngraph: plain\n"
    assert load_bass_extension_profile(profile_path).status == "bypassed"
    assert cam.reload_count == 4


@pytest.mark.asyncio
async def test_no_block_replacement_skips_redundant_desired_reload(monkeypatch, tmp_path):
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied, status="bypassed")
    desired = replace(
        _profile(applied_baseline=applied),
        enclosure={
            "adapter_id": "ported_v1",
            "adapter_version": 1,
            "cabinet_fingerprint": "cabinet-a",
        },
        natural={
            "fb_hz": 43.1,
            "knee_hz": 55.0,
            "knee_slope_db_oct": 21.0,
            "fit_rms_db": 0.4,
            "natural_curve": {
                "freqs_hz": np.geomspace(10.0, 500.0, 96).tolist(),
                "magnitude_db": [0.0] * 96,
            },
            "notes": [],
        },
    )
    *_, cam, paths = _transaction_harness(
        monkeypatch, tmp_path, predecessor=predecessor
    )

    await apply_bass_extension(desired, **paths)

    assert cam.reload_count == 1
    assert load_bass_extension_profile(paths["profile_path"]) == desired


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["profile_publish", "final_proof", "cancel"])
async def test_apply_failure_restores_exact_graph_and_profile(
    monkeypatch, tmp_path, failure
):
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        _cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    predecessor_graph = selected.read_bytes()
    predecessor_profile = profile_path.read_bytes()
    proof_calls = 0

    if failure in {"profile_publish", "cancel"}:
        def fail_save(*_args, **_kwargs):
            if failure == "cancel":
                raise asyncio.CancelledError
            raise OSError("profile publish failed")

        monkeypatch.setattr(profile_module, "save_bass_extension_profile", fail_save)
    else:
        async def fail_final(**_kwargs):
            nonlocal proof_calls
            proof_calls += 1
            if proof_calls == 2:
                raise BassExtensionApplyError("final proof failed")

        monkeypatch.setattr(bass_extension_module, "_active_proof", fail_final)

    expected = asyncio.CancelledError if failure == "cancel" else Exception
    with pytest.raises(expected):
        await apply_bass_extension(desired, **paths)

    assert selected.read_bytes() == predecessor_graph
    assert profile_path.read_bytes() == predecessor_profile
    assert stat.S_IMODE(selected.stat().st_mode) == 0o664
    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_post_intent_rollback_reacquires_writer_lock_before_real_reload(
    monkeypatch,
    tmp_path,
) -> None:
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        _cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    controller = _AdmissionAwareTransactionCamilla(selected, intent_path)
    paths["controller"] = controller
    predecessor_graph = selected.read_bytes()
    predecessor_profile = profile_path.read_bytes()
    proof_calls = 0

    async def fail_final(**_kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 2:
            raise BassExtensionApplyError("final proof failed")

    monkeypatch.setattr(bass_extension_module, "_active_proof", fail_final)

    with pytest.raises(BassExtensionApplyError, match="final proof failed"):
        await apply_bass_extension(desired, **paths)

    assert controller.reload_count == 3
    assert selected.read_bytes() == predecessor_graph
    assert stat.S_IMODE(selected.stat().st_mode) == 0o664
    assert profile_path.read_bytes() == predecessor_profile
    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_delayed_rollback_cannot_replay_consumed_intent_over_new_commit(
    monkeypatch,
    tmp_path,
) -> None:
    from contextlib import asynccontextmanager

    import jasper.dsp_apply as dsp_apply_module

    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        _cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    real_lock = dsp_apply_module.dsp_writer_lock
    rollback_waiting = asyncio.Event()
    release_rollback = asyncio.Event()
    delayed = False

    @asynccontextmanager
    async def delayed_lock(config_dir, *, source, **kwargs):
        nonlocal delayed
        if source == "bass_extension.apply_rollback" and not delayed:
            delayed = True
            rollback_waiting.set()
            await release_rollback.wait()
        async with real_lock(config_dir, source=source, **kwargs):
            yield

    proof_calls = 0

    async def fail_first_final(**_kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 2:
            raise BassExtensionApplyError("first final proof failed")

    monkeypatch.setattr(dsp_apply_module, "dsp_writer_lock", delayed_lock)
    monkeypatch.setattr(bass_extension_module, "_active_proof", fail_first_final)

    first = asyncio.create_task(apply_bass_extension(desired, **paths))
    await rollback_waiting.wait()
    await apply_bass_extension(desired, **paths)
    assert selected.read_text(encoding="utf-8") == "---\ngraph: plain\n"
    assert load_bass_extension_profile(profile_path) == desired

    release_rollback.set()
    with pytest.raises(BassExtensionApplyError, match="first final proof failed"):
        await first

    assert selected.read_text(encoding="utf-8") == "---\ngraph: plain\n"
    assert load_bass_extension_profile(profile_path) == desired
    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_delayed_rollback_refuses_different_newer_pending_intent(
    monkeypatch,
    tmp_path,
) -> None:
    from contextlib import asynccontextmanager

    import jasper.dsp_apply as dsp_apply_module

    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        _selected,
        _profile_path,
        intent_path,
        _statefile,
        _cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    real_lock = dsp_apply_module.dsp_writer_lock
    first_rollback_waiting = asyncio.Event()
    second_rollback_waiting = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    rollback_count = 0

    @asynccontextmanager
    async def delayed_lock(config_dir, *, source, **kwargs):
        nonlocal rollback_count
        if source == "bass_extension.apply_rollback":
            rollback_count += 1
            if rollback_count == 1:
                first_rollback_waiting.set()
                await release_first.wait()
            elif rollback_count == 2:
                second_rollback_waiting.set()
                await release_second.wait()
        async with real_lock(config_dir, source=source, **kwargs):
            yield

    proof_calls = 0

    async def fail_each_final(**_kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 2:
            raise BassExtensionApplyError("first final proof failed")
        if proof_calls == 5:
            raise BassExtensionApplyError("second final proof failed")

    monkeypatch.setattr(dsp_apply_module, "dsp_writer_lock", delayed_lock)
    monkeypatch.setattr(bass_extension_module, "_active_proof", fail_each_final)

    first = asyncio.create_task(apply_bass_extension(desired, **paths))
    await first_rollback_waiting.wait()
    second = asyncio.create_task(apply_bass_extension(desired, **paths))
    await second_rollback_waiting.wait()
    newer_intent = intent_path.read_bytes()

    release_first.set()
    with pytest.raises(
        BassExtensionApplyError,
        match="rollback intent ownership changed",
    ):
        await first
    assert intent_path.read_bytes() == newer_intent

    release_second.set()
    with pytest.raises(BassExtensionApplyError, match="second final proof failed"):
        await second
    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_rollback_drains_one_restore_task(
    monkeypatch,
    tmp_path,
) -> None:
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        _cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    predecessor_graph = selected.read_bytes()
    predecessor_profile = profile_path.read_bytes()
    proof_started = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    proof_calls = 0
    restore_tasks: list[asyncio.Task] = []
    real_restore = bass_extension_module._restore_locked

    async def active_proof(**_kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 2:
            proof_started.set()
            await asyncio.Event().wait()

    async def controlled_restore(*args, **kwargs):
        restore_task = asyncio.current_task()
        assert restore_task is not None
        restore_tasks.append(restore_task)
        restore_started.set()
        await release_restore.wait()
        return await real_restore(*args, **kwargs)

    monkeypatch.setattr(bass_extension_module, "_active_proof", active_proof)
    monkeypatch.setattr(
        bass_extension_module,
        "_restore_locked",
        controlled_restore,
    )

    apply_task = asyncio.create_task(apply_bass_extension(desired, **paths))
    await proof_started.wait()
    apply_task.cancel()
    await restore_started.wait()
    for _ in range(2):
        apply_task.cancel()
        await asyncio.sleep(0)
        assert len(restore_tasks) == 1
        assert restore_tasks[0].cancelled() is False
    release_restore.set()

    with pytest.raises(asyncio.CancelledError):
        await apply_task

    assert len(restore_tasks) == 1
    assert restore_tasks[0].cancelled() is False
    assert selected.read_bytes() == predecessor_graph
    assert stat.S_IMODE(selected.stat().st_mode) == 0o664
    assert profile_path.read_bytes() == predecessor_profile
    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_rollback_failure_wins_over_repeated_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    *_, intent_path, _statefile, _cam, paths = _transaction_harness(
        monkeypatch,
        tmp_path,
        predecessor=predecessor,
    )
    proof_started = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    proof_calls = 0
    restore_tasks: list[asyncio.Task] = []

    async def active_proof(**_kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 2:
            proof_started.set()
            await asyncio.Event().wait()

    async def failing_restore(*_args, **_kwargs):
        restore_task = asyncio.current_task()
        assert restore_task is not None
        restore_tasks.append(restore_task)
        restore_started.set()
        await release_restore.wait()
        raise BassExtensionApplyError("rollback proof failed")

    monkeypatch.setattr(bass_extension_module, "_active_proof", active_proof)
    monkeypatch.setattr(
        bass_extension_module,
        "_restore_locked",
        failing_restore,
    )

    apply_task = asyncio.create_task(apply_bass_extension(desired, **paths))
    await proof_started.wait()
    apply_task.cancel()
    await restore_started.wait()
    for _ in range(2):
        apply_task.cancel()
        await asyncio.sleep(0)
        assert len(restore_tasks) == 1
        assert restore_tasks[0].cancelled() is False
    release_restore.set()

    with pytest.raises(BassExtensionApplyError, match="rollback proof failed"):
        await apply_task

    assert restore_tasks[0].cancelled() is False
    assert intent_path.exists()
    assert bass_extension_state_summary(
        paths["profile_path"],
        intent_path=intent_path,
    )["apply_recovery_required"] is True


@pytest.mark.asyncio
async def test_rollback_failure_wins_over_forward_failure(
    monkeypatch,
    tmp_path,
) -> None:
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    *_, intent_path, _statefile, _cam, paths = _transaction_harness(
        monkeypatch,
        tmp_path,
        predecessor=predecessor,
    )
    proof_calls = 0

    async def fail_final(**_kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 2:
            raise BassExtensionApplyError("forward proof failed")

    async def fail_restore(*_args, **_kwargs):
        raise BassExtensionApplyError("rollback proof failed")

    monkeypatch.setattr(bass_extension_module, "_active_proof", fail_final)
    monkeypatch.setattr(bass_extension_module, "_restore_locked", fail_restore)

    with pytest.raises(BassExtensionApplyError, match="rollback proof failed"):
        await apply_bass_extension(desired, **paths)

    assert intent_path.exists()
    assert bass_extension_state_summary(
        paths["profile_path"],
        intent_path=intent_path,
    )["apply_recovery_required"] is True


@pytest.mark.asyncio
async def test_dsp_readback_failure_restores_both_authorities(monkeypatch, tmp_path):
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    graph_before = selected.read_bytes()
    profile_before = profile_path.read_bytes()
    cam.fail_path_when_reload_count = 2

    with pytest.raises(BassExtensionApplyError, match="readback"):
        await apply_bass_extension(desired, **paths)

    assert selected.read_bytes() == graph_before
    assert profile_path.read_bytes() == profile_before
    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_failed_apply_restores_predecessor_profile_absence(monkeypatch, tmp_path):
    applied = {**_applied_baseline(), "status": "applied"}
    desired = _profile(applied_baseline=applied)
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        _cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path)
    graph_before = selected.read_bytes()

    monkeypatch.setattr(
        profile_module,
        "save_bass_extension_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("publish")),
    )

    with pytest.raises(OSError, match="publish"):
        await apply_bass_extension(desired, **paths)

    assert selected.read_bytes() == graph_before
    assert not profile_path.exists()
    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_recovery_restores_exact_predecessor_bytes_mode_and_profile(
    monkeypatch, tmp_path
):
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        statefile,
        cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    predecessor_graph = selected.read_bytes()
    predecessor_profile = profile_path.read_bytes()
    desired_graph = b"---\ngraph: plain\n"
    identity = ExactDspStateIdentity(
        {"active_raw": selected.read_text(encoding="utf-8"), "config_path": str(selected)}
    )
    intent = bass_extension_module._intent_payload(
        predecessor_identity=identity,
        predecessor_profile_bytes=predecessor_profile,
        desired_profile_bytes=bass_extension_module._profile_bytes(desired),
        selected_path=selected,
        selected_mode=0o664,
        predecessor_graph_bytes=predecessor_graph,
        desired_graph_bytes=desired_graph,
        selector_target=selected,
    )
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    selected.write_bytes(b"truncated")
    selected.chmod(0o600)
    profile_path.write_text("{}", encoding="utf-8")

    recovered = await recover_pending_bass_extension_apply(
        **{key: value for key, value in paths.items() if key != "validate"}
    )

    assert recovered is True
    assert selected.read_bytes() == predecessor_graph
    assert stat.S_IMODE(selected.stat().st_mode) == 0o664
    assert profile_path.read_bytes() == predecessor_profile
    assert statefile.read_text(encoding="utf-8") == f"config_path: {selected}\n"
    assert cam.reload_count == 1
    assert not intent_path.exists()
    assert await recover_pending_bass_extension_apply(
        **{key: value for key, value in paths.items() if key != "validate"}
    ) is False


@pytest.mark.asyncio
async def test_failed_recovery_retains_intent_and_reports_required(
    monkeypatch, tmp_path
):
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        _cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    predecessor_graph = selected.read_bytes()
    predecessor_profile = profile_path.read_bytes()
    identity = ExactDspStateIdentity(
        {"active_raw": selected.read_text(encoding="utf-8"), "config_path": str(selected)}
    )
    intent = bass_extension_module._intent_payload(
        predecessor_identity=identity,
        predecessor_profile_bytes=predecessor_profile,
        desired_profile_bytes=bass_extension_module._profile_bytes(desired),
        selected_path=selected,
        selected_mode=0o664,
        predecessor_graph_bytes=predecessor_graph,
        desired_graph_bytes=b"---\ngraph: plain\n",
        selector_target=selected,
    )
    intent_path.write_text(json.dumps(intent), encoding="utf-8")

    async def fail_proof(**_kwargs):
        raise BassExtensionApplyError("proof unavailable")

    monkeypatch.setattr(bass_extension_module, "_active_proof", fail_proof)

    with pytest.raises(BassExtensionApplyError, match="proof unavailable"):
        await recover_pending_bass_extension_apply(
            **{key: value for key, value in paths.items() if key != "validate"}
        )

    assert intent_path.exists()
    assert bass_extension_state_summary(
        profile_path, intent_path=intent_path
    )["apply_recovery_required"] is True


@pytest.mark.asyncio
async def test_recovery_validates_desired_record_before_mutating_predecessor(
    monkeypatch, tmp_path
):
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    identity = ExactDspStateIdentity(
        {"active_raw": selected.read_text(encoding="utf-8"), "config_path": str(selected)}
    )
    intent = bass_extension_module._intent_payload(
        predecessor_identity=identity,
        predecessor_profile_bytes=profile_path.read_bytes(),
        desired_profile_bytes=bass_extension_module._profile_bytes(desired),
        selected_path=selected,
        selected_mode=0o664,
        predecessor_graph_bytes=selected.read_bytes(),
        desired_graph_bytes=b"---\ngraph: plain\n",
        selector_target=selected,
    )
    intent["config"]["desired_sha256"] = "0" * 64
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    selected.write_text("truncated", encoding="utf-8")

    with pytest.raises(BassExtensionApplyError, match="identity"):
        await recover_pending_bass_extension_apply(
            **{key: value for key, value in paths.items() if key != "validate"}
        )

    assert selected.read_text(encoding="utf-8") == "truncated"
    assert intent_path.exists()
    assert cam.reload_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["leader", "follower"])
async def test_bonded_owner_refuses_before_recovery_or_any_mutation(
    monkeypatch, tmp_path, role
):
    real_bond_check = bass_extension_module._bonded_or_driver_carrier
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    graph_before = selected.read_bytes()
    profile_before = profile_path.read_bytes()
    intent_path.write_text("malformed older intent", encoding="utf-8")
    monkeypatch.setattr(
        bass_extension_module, "_bonded_or_driver_carrier", real_bond_check
    )
    monkeypatch.setattr(
        multiroom_config_module,
        "load_config",
        lambda: SimpleNamespace(enabled=True, error=None, role=role),
    )

    with pytest.raises(BassExtensionApplyError, match="bonded"):
        await apply_bass_extension(desired, **paths)

    assert selected.read_bytes() == graph_before
    assert profile_path.read_bytes() == profile_before
    assert intent_path.read_text(encoding="utf-8") == "malformed older intent"
    assert cam.reload_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config",
        "jasper.active_speaker.camilla_yaml.emit_active_speaker_driver_domain_config",
    ],
)
async def test_bonded_carrier_refuses_before_any_mutation(
    monkeypatch, tmp_path, source
):
    real_bond_check = bass_extension_module._bonded_or_driver_carrier
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied)
    desired = replace(predecessor, status="bypassed")
    (
        _applied,
        selected,
        profile_path,
        intent_path,
        _statefile,
        cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    selected.write_text(f"---\n# Source: {source}\nfilters: {{}}\n", encoding="utf-8")
    second_graph = tmp_path / "bonded-other-camilla.yml"
    second_graph.write_text("---\nfilters: { untouched: true }\n", encoding="utf-8")
    graph_before = selected.read_bytes()
    second_graph_before = second_graph.read_bytes()
    profile_before = profile_path.read_bytes()
    intent_path.write_text("malformed older intent", encoding="utf-8")
    monkeypatch.setattr(
        bass_extension_module, "_bonded_or_driver_carrier", real_bond_check
    )
    monkeypatch.setattr(
        multiroom_config_module,
        "load_config",
        lambda: SimpleNamespace(enabled=False, error=None, role="leader"),
    )

    with pytest.raises(BassExtensionApplyError, match="bonded"):
        await apply_bass_extension(desired, **paths)

    assert selected.read_bytes() == graph_before
    assert second_graph.read_bytes() == second_graph_before
    assert profile_path.read_bytes() == profile_before
    assert intent_path.read_text(encoding="utf-8") == "malformed older intent"
    assert cam.reload_count == 0


@pytest.mark.asyncio
async def test_missing_sealed_subsonic_refuses_before_mutation(monkeypatch, tmp_path):
    applied = {**_applied_baseline(), "status": "applied"}
    predecessor = _profile(applied_baseline=applied, status="bypassed")
    desired = _profile(applied_baseline=applied)
    targets = tuple(
        replace(target, subsonic=None)
        if target.target_id == "natural"
        else target
        for target in desired.targets
    )
    desired = replace(desired, targets=targets)
    (
        _applied,
        selected,
        profile_path,
        _intent_path,
        _statefile,
        cam,
        paths,
    ) = _transaction_harness(monkeypatch, tmp_path, predecessor=predecessor)
    graph_before = selected.read_bytes()
    profile_before = profile_path.read_bytes()

    with pytest.raises(BassExtensionApplyError, match="subsonic"):
        await apply_bass_extension(desired, **paths)

    assert selected.read_bytes() == graph_before
    assert profile_path.read_bytes() == profile_before
    assert cam.reload_count == 0


def test_profile_publication_is_power_loss_durable(monkeypatch, tmp_path):
    calls = []

    def write(path, text, **kwargs):
        calls.append((Path(path), text, kwargs))

    monkeypatch.setattr(profile_module, "atomic_write_text", write)
    target = tmp_path / "bass.json"

    save_bass_extension_profile(_profile(), target)

    assert calls[0][0] == target
    assert calls[0][2]["durable"] is True
    assert calls[0][2]["mode"] == 0o640


def test_state_summary_reports_deferred_adapter_and_pending_recovery(tmp_path):
    profile = replace(
        _profile(),
        enclosure={
            "adapter_id": "ported_v1",
            "adapter_version": 1,
            "cabinet_fingerprint": "cabinet-a",
        },
        natural={
            "fb_hz": 43.1,
            "knee_hz": 55.0,
            "knee_slope_db_oct": 21.0,
            "fit_rms_db": 0.4,
            "natural_curve": {
                "freqs_hz": np.geomspace(10.0, 500.0, 96).tolist(),
                "magnitude_db": [0.0] * 96,
            },
            "notes": [],
        },
    )
    path = _save(tmp_path, profile)
    intent = tmp_path / "intent.json"
    intent.write_text("{}", encoding="utf-8")

    summary = bass_extension_state_summary(path, intent_path=intent)

    assert summary["adapter_id"] == "ported_v1"
    assert summary["runtime_eligible"] is False
    assert summary["runtime_deferred_reason"] == "fixed_graph_not_defined"
    assert summary["apply_recovery_required"] is True


def test_state_summary_reports_pending_recovery_with_absent_predecessor(tmp_path):
    profile_path = tmp_path / "missing-profile.json"
    intent = tmp_path / "intent.json"
    intent.write_text("{}", encoding="utf-8")

    summary = bass_extension_state_summary(profile_path, intent_path=intent)

    assert summary == {
        "commissioned": False,
        "status": None,
        "profile_id": None,
        "adapter_id": None,
        "runtime_eligible": False,
        "runtime_deferred_reason": None,
        "apply_recovery_required": True,
    }
