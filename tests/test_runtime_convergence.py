# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Effect-boundary tests for topology runtime convergence."""

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker import runtime_convergence
from tests.test_active_speaker_runtime_contract import _topology


def test_park_for_topology_uses_camilla_persistence_not_direct_statefile_write(
    monkeypatch, tmp_path: Path
) -> None:
    """jasper-web may park audio without access to Camilla's statefile dir."""

    materialised = []
    loaded = []

    def materialise(decision, *, topology):
        materialised.append((decision, topology))

    def forbidden_statefile_write(*_args, **_kwargs):
        raise AssertionError("park must not open CamillaDSP's statefile")

    class Controller:
        async def set_config_file_path(self, path, *, best_effort=False):
            loaded.append((path, best_effort))
            return True

    monkeypatch.setattr(
        runtime_convergence, "materialise_safe_graph_decision", materialise
    )
    monkeypatch.setattr(
        runtime_convergence,
        "apply_safe_graph_decision_to_statefile",
        forbidden_statefile_write,
    )

    result = runtime_convergence.park_for_topology(
        _topology([]),
        statefile_path=tmp_path / "root-owned-statefile.yml",
        controller_factory=Controller,
    )

    assert result.ok is True
    assert result.statefile_written is False
    assert len(materialised) == 1
    assert loaded == [(result.decision.selected_config_path, True)]


def test_apply_live_failure_is_not_a_successful_convergence(monkeypatch) -> None:
    """A topology writer must refuse to continue when Camilla rejects park."""

    monkeypatch.setattr(
        runtime_convergence,
        "materialise_safe_graph_decision",
        lambda *_args, **_kwargs: None,
    )

    class Controller:
        async def set_config_file_path(self, _path, *, best_effort=False):
            return False

    result = runtime_convergence.park_for_topology(
        _topology([]), controller_factory=Controller
    )

    assert result.ok is False
    assert result.live_applied is False
    assert result.error == "CamillaDSP unreachable or rejected the proved graph"
