# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Effect-boundary tests for topology runtime convergence."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from jasper.active_speaker import runtime_convergence
from jasper.active_speaker.runtime_contract import parked_safe_graph_decision
from tests.test_active_speaker_runtime_contract import _topology


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


def test_transaction_resolves_persisted_coupling_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jasper.fanin import coupling_reconcile

    topology = _topology([])
    controller = _Controller(tmp_path / "graph.lock")
    coupling_reads: list[str] = []
    selected_couplings: list[str | None] = []
    real_select = runtime_convergence.safe_graph_for_current_topology

    def read_coupling() -> str:
        coupling_reads.append("read")
        return "shm_ring"

    def capture_coupling(*args, **kwargs):
        selected_couplings.append(kwargs.get("coupling"))
        return real_select(*args, **kwargs)

    monkeypatch.setattr(coupling_reconcile, "read_persisted_coupling", read_coupling)
    monkeypatch.setattr(
        runtime_convergence, "safe_graph_for_current_topology", capture_coupling
    )
    monkeypatch.setattr(
        runtime_convergence,
        "materialise_safe_graph_decision",
        lambda *_args, **_kwargs: None,
    )

    result = runtime_convergence.park_and_commit_topology(
        topology,
        lambda: topology,
        controller_factory=lambda: controller,
    )

    assert result.convergence.ok is True
    assert coupling_reads == ["read"]
    assert selected_couplings == ["shm_ring"]


def test_post_publication_fsync_failure_does_not_restore_old_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jasper.output_topology import load_output_topology, save_output_topology

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

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="simulated directory fsync failure"):
        runtime_convergence.park_and_commit_topology(
            old_topology,
            commit,
            controller_factory=lambda: controller,
        )

    assert load_output_topology(topology_path) == new_topology
    assert controller.path_sets == []
    assert len(controller.raw_sets) == 1


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
            coupling=None,
        )
    )

    assert result.ok is True
    assert calls == [str(flat)]
    assert controller.path_sets == [str(composed)]
    assert str(flat) not in controller.path_sets


def test_stay_parked_skips_selection_so_a_re_pin_cannot_resume_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The postcondition behind #2814's copy: a re-pin leaves the box silent.

    ``safe_graph_for_current_topology`` is identity-blind by design — it proves
    a graph legal for the saved SHAPE, and a DAC re-pin changes no shape. So on
    an ALREADY-ARMED box every rung it can reach resumes audio, through DACs
    whose per-lane identity nobody has re-confirmed; on a roleful topology that
    is the full-range-into-a-tweeter hazard class. The identity gates guard the
    next ARM, not the graph already playing, which is why parking has to be the
    committing caller's job.

    Both halves are asserted from ONE stubbed selector: without ``stay_parked``
    it is consulted and its playing graph is loaded (the hazard, demonstrated
    rather than assumed); with it the selector is never called and the durable
    parked graph is loaded instead.
    """

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
            coupling=None,
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
            coupling=None,
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
