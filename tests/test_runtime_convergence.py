# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Effect-boundary tests for topology runtime convergence."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from jasper.active_speaker import baseline_reemit, measurement_emit
from jasper.active_speaker.baseline_record import prepare_applied_baseline_profile
from jasper.active_speaker.candidate_bank import publish_authored_candidate
from jasper.active_speaker.environment import read_camilla_statefile_config_path
from tests.active_speaker_fixtures import (
    declared_graph_fixture, mono_output_topology, standard_design_draft,
)

from jasper.active_speaker import runtime_convergence
from jasper.active_speaker.runtime_contract import parked_safe_graph_decision
from jasper.output_topology import OutputTopology
from jasper.output_topology_store import (
    read_topology_fingerprint_stamp,
    statefile_topology_stamp_path,
    statefile_unproved_stamp_path,
    topology_fingerprint_stamp,
    load_output_topology,
    save_output_topology,
)
from tests.test_active_speaker_runtime_contract import (
    _active_yaml, _flat_yaml, _staged_metadata, _topology, _under_charged_boosted_baseline,
)


class _Controller:
    def __init__(self, lock_path: Path, *, apply_raw: bool = True) -> None:
        self._graph_mutation_lock_path = lock_path
        self.path = "/tmp/prior.yml"
        self.active_raw: str | None = None
        self.apply_raw = apply_raw
        self.path_sets: list[str] = []
        self.raw_sets: list[str] = []

    async def get_config_file_path(self, *, best_effort=False):
        return self.path

    async def set_config_file_path(self, path, *, best_effort=False):
        self.path_sets.append(path)
        self.path = path
        return True

    async def set_active_config_raw(self, raw, *, best_effort=False):
        self.raw_sets.append(raw)
        if self.apply_raw:
            self.active_raw = raw
        return self.apply_raw

    async def get_active_config_raw(self, *, best_effort=False):
        return self.active_raw

    async def normalize_config_raw(self, raw, *, best_effort=False):
        return raw


@pytest.fixture(autouse=True)
def _successful_outputd_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda *_units, **_kwargs: {"ok": True},
    )


def test_commit_failure_keeps_proved_parked_graph_inside_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _topology([])
    controller = _Controller(tmp_path / "graph.lock")
    events: list[str] = []

    def stop_outputd(*units, **kwargs):
        assert units == (runtime_convergence.OUTPUTD_UNIT,)
        assert kwargs["verb"] == "stop"
        assert kwargs["no_block"] is False
        events.append("stop-outputd")
        return {"ok": True}

    def fail_commit():
        events.append("commit")
        raise ValueError("commit failed")

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        stop_outputd,
    )

    with pytest.raises(ValueError, match="commit failed"):
        runtime_convergence.park_and_commit_topology(
            topology,
            fail_commit,
            controller_factory=lambda: controller,
        )

    assert events == ["stop-outputd", "commit"]
    assert controller.path_sets == []
    assert len(controller.raw_sets) == 1


def test_outputd_stop_failure_prevents_park_and_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology = _topology([])
    controller = _Controller(tmp_path / "graph.lock")
    committed = False

    def commit():
        nonlocal committed
        committed = True
        return topology

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda *_units, **_kwargs: {"ok": False, "error": "stop failed"},
    )

    with pytest.raises(RuntimeError, match="stop failed"):
        runtime_convergence.park_and_commit_topology(
            topology,
            commit,
            controller_factory=lambda: controller,
        )

    assert committed is False
    assert controller.raw_sets == []


def test_committed_unconfigured_topology_persists_parked_path_through_camilla(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    topology = _topology([])
    controller = _Controller(tmp_path / "graph.lock")
    materialised: list[str] = []
    monkeypatch.setattr(
        runtime_convergence,
        "materialise_safe_graph_decision",
        lambda decision, *, topology: materialised.append(
            str(decision.selected_config_path)
        ),
    )

    result = runtime_convergence.park_and_commit_topology(
        topology,
        lambda: topology,
        controller_factory=lambda: controller,
    )

    parked_path = str(result.convergence.decision.selected_config_path)
    assert result.convergence.ok is True
    assert materialised == [parked_path]
    assert controller.path_sets == [parked_path]
    assert controller.raw_sets  # temporary park happened before final path load


def test_post_publication_fsync_failure_does_not_restore_old_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    topology_path = tmp_path / "output_topology.json"
    old_topology = replace(_topology([]), name="Old layout")
    new_topology = replace(_topology([]), name="New layout")
    save_output_topology(old_topology, topology_path)
    controller = _Controller(tmp_path / "graph.lock")
    fsync_calls = 0

    def fail_directory_fsync(_fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("simulated directory fsync failure")

    def commit():
        save_output_topology(new_topology, topology_path)
        return new_topology

    materialised: list[str] = []
    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(
        runtime_convergence,
        "materialise_safe_graph_decision",
        lambda decision, *, topology: materialised.append(
            str(decision.selected_config_path)
        ),
    )

    result = runtime_convergence.park_and_commit_topology(
        old_topology,
        commit,
        controller_factory=lambda: controller,
    )

    parked_path = str(result.convergence.decision.selected_config_path)
    assert result.convergence.ok is True
    assert load_output_topology(topology_path) == new_topology
    assert materialised == [parked_path]
    assert controller.path_sets == [parked_path]


def test_graph_writer_cannot_enter_between_park_and_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jasper.dsp_apply import camilla_graph_mutation

    topology = _topology([])
    lock_path = tmp_path / "graph.lock"
    controller = _Controller(lock_path)
    monkeypatch.setattr(
        runtime_convergence,
        "materialise_safe_graph_decision",
        lambda *_args, **_kwargs: None,
    )
    commit_entered = threading.Event()
    release_commit = threading.Event()
    competitor_entered = threading.Event()

    def commit():
        commit_entered.set()
        assert release_commit.wait(2)
        return topology

    first = threading.Thread(
        target=lambda: runtime_convergence.park_and_commit_topology(
            topology, commit, controller_factory=lambda: controller
        )
    )

    async def compete():
        async with camilla_graph_mutation(source="test.competitor", lock_path=lock_path):
            competitor_entered.set()

    second = threading.Thread(target=lambda: asyncio.run(compete()))
    first.start()
    assert commit_entered.wait(2)
    second.start()
    assert not competitor_entered.wait(0.15)
    release_commit.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert competitor_entered.is_set()


def test_flat_fallback_is_composed_before_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    topology = _topology([])
    flat = tmp_path / "flat.yml"
    flat.write_text("flat", encoding="utf-8")
    composed = tmp_path / "sound_current.yml"
    composed.write_text("preferences plus room peq", encoding="utf-8")
    contract = parked_safe_graph_decision(topology).topology_contract
    decisions = [
        replace(
            parked_safe_graph_decision(topology),
            status="select_flat",
            selected_config_path=str(flat),
            topology_contract=contract,
        ),
        replace(
            parked_safe_graph_decision(topology),
            status="preserve_current",
            selected_config_path=str(composed),
            topology_contract=contract,
        ),
    ]
    monkeypatch.setattr(
        runtime_convergence,
        "safe_graph_for_current_topology",
        lambda *_args, **_kwargs: decisions.pop(0),
    )
    import jasper.sound.runtime as sound_runtime

    calls: list[str] = []
    monkeypatch.setattr(
        sound_runtime,
        "materialise_saved_dsp_on_carrier",
        lambda path, **_kwargs: calls.append(path) or composed,
    )
    controller = _Controller(tmp_path / "graph.lock")

    result = asyncio.run(
        runtime_convergence._converge_committed_topology(
            topology,
            controller=controller,
            prior_config_path="/tmp/prior.yml",
            profile_path=None,
            config_dir=None,
        )
    )

    assert result.ok is True
    assert calls == [str(flat)]
    assert controller.path_sets == [str(composed)]
    assert str(flat) not in controller.path_sets


def test_stay_parked_skips_selection_so_a_re_pin_cannot_resume_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR", tmp_path
    )
    topology = _topology([])
    playing = tmp_path / "approved_active_runtime.yml"
    playing.write_text("the graph this box was playing", encoding="utf-8")
    contract = parked_safe_graph_decision(topology).topology_contract
    resumed = replace(
        parked_safe_graph_decision(topology),
        status="preserve_current",
        selected_config_path=str(playing),
        topology_contract=contract,
    )
    consulted: list[str] = []

    def selector(*_args, **_kwargs):
        consulted.append("selected")
        return resumed

    monkeypatch.setattr(
        runtime_convergence, "safe_graph_for_current_topology", selector
    )

    resuming_controller = _Controller(tmp_path / "graph.lock")
    asyncio.run(
        runtime_convergence._converge_committed_topology(
            topology,
            controller=resuming_controller,
            prior_config_path=str(playing),
            profile_path=None,
            config_dir=None,
        )
    )

    assert consulted == ["selected"]
    assert resuming_controller.path_sets == [str(playing)]

    parked_controller = _Controller(tmp_path / "graph.lock")
    parked = asyncio.run(
        runtime_convergence._converge_committed_topology(
            topology,
            controller=parked_controller,
            prior_config_path=str(playing),
            profile_path=None,
            config_dir=None,
            stay_parked=True,
            parked_reason="confirm the re-pinned outputs before audio resumes",
        )
    )

    assert consulted == ["selected"], "stay_parked must not consult the selector"
    assert parked.ok is True
    assert parked.decision.status == "parked_muted"
    assert parked.decision.reason == (
        "confirm the re-pinned outputs before audio resumes"
    )
    assert parked_controller.path_sets == [
        str(parked_safe_graph_decision(topology).selected_config_path)
    ]
    assert str(playing) not in parked_controller.path_sets


def _boot_convergence_paths(tmp_path: Path) -> dict[str, Path]:
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    return {
        "statefile_path": tmp_path / "outputd-statefile.yml",
        "flat_config_path": flat,
        "applied_baseline_path": tmp_path / "applied-baseline.json",
        "staged_metadata_path": tmp_path / "staged-metadata.json",
    }


def _empty_topology() -> OutputTopology:
    """A declared-but-unassigned box: the selector resolves it, so a
    convergence over it succeeds."""
    return _topology([], {})


def _unassigned_passive_mono() -> OutputTopology:
    """A passive main whose identity is unverified: the selector REFUSES it,
    which is the commonest way a pass proves no graph."""
    return _topology(
        [{
            "id": "mono", "label": "Mono", "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        }],
        {"mono_group_id": "mono"},
    )


def test_a_convergence_that_wrote_a_statefile_retires_the_unproved_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The steady state jasper-camilla's startup gate reads as "start"."""
    paths = _boot_convergence_paths(tmp_path)
    unproved = statefile_unproved_stamp_path(paths["statefile_path"])
    unproved.write_text("stale\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime_convergence, "apply_safe_graph_decision_to_statefile",
        lambda *_args, **_kwargs: True,
    )

    result = runtime_convergence.converge_boot_statefile(
        topology=_empty_topology(), write_statefile=True, **paths
    )

    assert result.ok is True
    assert not unproved.exists()


@pytest.mark.parametrize("failure", ["refused", "raised"])
def test_a_convergence_that_proved_nothing_leaves_its_topology_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """What the gate refuses on. Both halves of "did not prove it": a decision
    that refused, and a write that blew up part way through."""
    topology = _empty_topology()
    paths = _boot_convergence_paths(tmp_path)
    if failure == "raised":
        def _explode(*_args, **_kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(
            runtime_convergence, "apply_safe_graph_decision_to_statefile", _explode,
        )
    else:
        topology = _unassigned_passive_mono()

    result = runtime_convergence.converge_boot_statefile(
        topology=topology, write_statefile=True, **paths
    )

    assert result.ok is False
    stamp = statefile_unproved_stamp_path(paths["statefile_path"])
    assert read_topology_fingerprint_stamp(stamp) == topology_fingerprint_stamp(
        topology
    )


def test_a_read_only_convergence_stamps_nothing(tmp_path: Path) -> None:
    """`write_statefile=False` callers own no statefile, so they may not move
    the proof a gate reads."""
    paths = _boot_convergence_paths(tmp_path)

    runtime_convergence.converge_boot_statefile(
        topology=_empty_topology(), write_statefile=False, **paths
    )

    assert not statefile_unproved_stamp_path(paths["statefile_path"]).exists()
    assert not statefile_topology_stamp_path(paths["statefile_path"]).exists()


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("case", [
    "heal", "heal_current", "read_only", "disabled", "missing", "bad_candidate", "unsafe_emit",
    "publish_error", "reselect_refusal", "already_safe", "current_startup", "blocked",
])
def test_boot_rebuilds_saved_tune_before_parking(tmp_path, monkeypatch, case, staged):
    topology = mono_output_topology()
    draft = standard_design_draft(topology)
    draft["manual_settings"] = {"drivers": [{
        "target_id": "mono:tweeter", "role": "tweeter", "recommended_highpass_hz": 2500,
    }], "crossover_candidates": []}
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(draft_path))
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "sound.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "sound-settings.json"))
    monkeypatch.setattr("jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(tmp_path / "canonical.yml"))
    _, base = declared_graph_fixture(topology, draft)
    declaration = measurement_emit.load_tuning_declaration(topology)
    candidate = replace(base, bass_extension={
        "low_boost_db": 4.0, "reference_level_db": -10.0,
        "detector_lowpass_hz": 120.0, "compressor_threshold_dbfs": -12.0,
    } if case != "blocked" else {})
    banked = publish_authored_candidate(candidate, root=tmp_path / "bank")
    paths = _boot_convergence_paths(tmp_path)
    startup = tmp_path / "startup.yml"
    startup.write_text(_active_yaml("mono", 2, frozenset()))
    if staged or case in {"current_startup"}:
        paths["staged_metadata_path"].write_text(json.dumps(_staged_metadata(topology, startup)))
    artifact = tmp_path / "baseline.yml"
    applied = prepare_applied_baseline_profile(
        banked, declaration=declaration, design_draft=draft, config_path=artifact,
    )
    applied["status"] = "applied"
    paths["applied_baseline_path"].write_text(json.dumps(applied))
    fresh = measurement_emit.compile_tuning_graph(declaration, candidate=candidate)
    old_payload = yaml.safe_load(fresh)
    if case != "blocked":
        # The saved graph carries ADR-0352's block: the Aux1 Loudness shelf where the boost biquad now plays.
        filters = old_payload["filters"]
        del filters["bass_ext_dynamic_boost"]
        filters["bass_ext_dynamic_loudness"] = {"type": "Loudness", "parameters": {
            "fader": "Aux1", "reference_level": -10.0, "high_boost": 0.0, "low_boost": 4.0, "attenuate_mid": False}}
        for step in old_payload["pipeline"]:
            if step.get("names") == ["bass_ext_dynamic_boost"]:
                step["names"] = ["bass_ext_dynamic_loudness"]
    old = "\n".join(line for line in fresh.splitlines() if line.startswith("#")) + "\n" + yaml.safe_dump(old_payload)
    artifact.write_text(fresh if case in {"already_safe"} else old)
    current = artifact if case == "heal_current" else tmp_path / "prior.yml"
    current.write_text(artifact.read_text())
    if case in {"current_startup"}:
        current = startup
    elif case == "blocked":
        artifact.write_text(_under_charged_boosted_baseline())
        current.write_text(artifact.read_text())
    paths["statefile_path"].write_text(f"config_path: {current}\nvolume: -18.0\nmute: false\n")
    if case == "missing":
        paths["applied_baseline_path"].unlink()
    elif case == "bad_candidate":
        banked.path.unlink()
    elif case == "unsafe_emit":
        monkeypatch.setattr(measurement_emit, "compile_tuning_graph", lambda *_a, **_kw: old)
    elif case == "publish_error":
        real_write = baseline_reemit.atomic_io.atomic_write_text
        def fail_artifact(path, *args, **kwargs):
            if Path(path) == artifact:
                raise OSError("write failed")
            return real_write(path, *args, **kwargs)
        monkeypatch.setattr(baseline_reemit.atomic_io, "atomic_write_text", fail_artifact)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    calls = []
    real_reemit = runtime_convergence.reemit_applied_baseline
    def reemit(parsed, saved, **kwargs):
        assert parsed is topology
        assert read_camilla_statefile_config_path(paths["statefile_path"]) == str(current)
        calls.append(parsed)
        statefile_stat = paths["statefile_path"].stat()
        result = real_reemit(parsed, saved, **kwargs)
        assert paths["statefile_path"].stat() == statefile_stat
        if case == "reselect_refusal":
            artifact.write_text(old)
        return result
    monkeypatch.setattr(runtime_convergence, "reemit_applied_baseline", reemit)
    result = runtime_convergence.converge_boot_statefile(
        topology=topology, topology_path=tmp_path / "must-not-read.json",
        current_config_path=current, write_statefile=case != "read_only",
        consider_applied_baseline=case != "disabled", **paths,
    )
    assert result.ok is (case != "blocked")
    assert len(calls) == (0 if case in {
        "read_only", "disabled", "missing", "already_safe", "current_startup", "blocked",
    } else 1)
    if case in {"heal", "heal_current"}:
        assert result.decision.status == ("select_active_baseline" if case == "heal" else "preserve_current")
        assert result.decision.preferred_graph.allowed
        if case == "heal":
            assert result.decision.current_graph.issues[0]["code"] == "bass_extension_block_invalid"
        assert read_camilla_statefile_config_path(paths["statefile_path"]) == str(artifact)
        healed = yaml.safe_load(artifact.read_text())
        assert healed["devices"]["volume_limit"] == 0.0
        assert "bass_ext_dynamic_boost" in healed["filters"]
        assert not any(filter_["type"] == "Loudness" for filter_ in healed["filters"].values())
        assert yaml.safe_load(paths["statefile_path"].read_text())["volume"] == -18.0
    elif case in {"already_safe", "current_startup", "blocked"}:
        assert result.decision.status == ("blocked" if case == "blocked" else "preserve_current")
        assert read_camilla_statefile_config_path(paths["statefile_path"]) == str(current)
        assert artifact.read_bytes() == before[artifact]
    else:
        assert result.decision.status == ("select_active_startup" if staged else "parked_muted")
        if case == "read_only":
            assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
        else:
            assert read_camilla_statefile_config_path(paths["statefile_path"]) == result.decision.selected_config_path
            assert yaml.safe_load(Path(result.decision.selected_config_path).read_text())["devices"]["volume_limit"] == 0.0
    if case in {"bad_candidate", "unsafe_emit", "publish_error"}:
        assert artifact.read_bytes() == before[artifact]
        assert not (tmp_path / "canonical.yml").exists()
    if case != "missing":
        assert paths["applied_baseline_path"].read_bytes() == before[paths["applied_baseline_path"]]
