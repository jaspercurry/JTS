# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Effect-boundary tests for topology runtime convergence."""

from __future__ import annotations

import asyncio
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


def test_park_uses_proved_inline_graph_without_repointing_persisted_path(
    tmp_path: Path,
) -> None:
    controller = _Controller(tmp_path / "graph.lock")

    result = runtime_convergence.park_for_topology(
        _topology([]), controller_factory=lambda: controller
    )

    assert result.ok is True
    assert result.statefile_written is False
    assert controller.path == "/tmp/prior.yml"
    assert controller.path_sets == []
    assert len(controller.raw_sets) == 1
    assert "active-speaker PARKED config" in controller.raw_sets[0]


def test_park_rejection_is_not_a_successful_convergence(tmp_path: Path) -> None:
    controller = _Controller(tmp_path / "graph.lock", apply_raw=False)

    result = runtime_convergence.park_for_topology(
        _topology([]), controller_factory=lambda: controller
    )

    assert result.ok is False
    assert result.live_applied is False
    assert "rejected" in str(result.error)


def test_nonpersistent_apply_skips_identical_running_raw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    topology = _topology([])
    candidate = tmp_path / "candidate.yml"
    candidate.write_text("---\nfilters: {}\n", encoding="utf-8")
    decision = replace(
        parked_safe_graph_decision(topology),
        status="preserve_current",
        selected_config_path=str(candidate),
    )
    controller = _Controller(tmp_path / "graph.lock")
    controller.active_raw = candidate.read_text(encoding="utf-8")
    monkeypatch.setattr(
        runtime_convergence,
        "materialise_safe_graph_decision",
        lambda *_args, **_kwargs: None,
    )

    result = runtime_convergence.apply_runtime_graph_decision(
        decision,
        topology=topology,
        controller_factory=lambda: controller,
        persist_statefile=False,
    )

    assert result.ok is True
    assert controller.raw_sets == []
    assert controller.path_sets == []


def test_nonpersistent_apply_reloads_when_persisted_path_matches_but_raw_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    topology = _topology([])
    candidate = tmp_path / "candidate.yml"
    candidate.write_text("---\nfilters: candidate\n", encoding="utf-8")
    decision = replace(
        parked_safe_graph_decision(topology),
        status="preserve_current",
        selected_config_path=str(candidate),
    )
    controller = _Controller(tmp_path / "graph.lock")
    controller.path = str(candidate)
    controller.active_raw = "---\nfilters: parked\n"
    monkeypatch.setattr(
        runtime_convergence,
        "materialise_safe_graph_decision",
        lambda *_args, **_kwargs: None,
    )

    result = runtime_convergence.apply_runtime_graph_decision(
        decision,
        topology=topology,
        controller_factory=lambda: controller,
        persist_statefile=False,
    )

    assert result.ok is True
    assert controller.path_sets == [str(candidate)]
    assert controller.raw_sets == []


def test_commit_failure_restores_prior_graph_inside_transaction(
    tmp_path: Path,
) -> None:
    topology = _topology([])
    controller = _Controller(tmp_path / "graph.lock")

    with pytest.raises(ValueError, match="commit failed"):
        runtime_convergence.park_and_commit_topology(
            topology,
            lambda: (_ for _ in ()).throw(ValueError("commit failed")),
            controller_factory=lambda: controller,
        )

    assert controller.path_sets == ["/tmp/prior.yml"]
    assert len(controller.raw_sets) == 1


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


def test_commit_and_restore_failure_is_reported_honestly(tmp_path: Path) -> None:
    topology = _topology([])
    controller = _Controller(tmp_path / "graph.lock")

    async def reject_path(_path, *, best_effort=False):
        return False

    controller.set_config_file_path = reject_path
    with pytest.raises(
        runtime_convergence.TopologyCommitRestoreError,
        match="could not be restored",
    ):
        runtime_convergence.park_and_commit_topology(
            topology,
            lambda: (_ for _ in ()).throw(ValueError("commit failed")),
            controller_factory=lambda: controller,
        )


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
