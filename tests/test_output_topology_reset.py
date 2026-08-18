# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the unconfigured, parked topology reset command."""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper import output_topology_runtime as topology_runtime
from jasper.output_hardware import OutputHardwareState, write_state
from jasper.output_topology import OutputTopology, load_output_topology_strict, save_output_topology


class _ParkResult:
    ok = True
    live_applied = True

    def to_dict(self):
        return {"ok": True, "live_applied": True}


@pytest.fixture
def topo_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "output_topology.json"
    hardware_path = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(hardware_path))
    write_state(
        OutputHardwareState(
            profile_id="apple_usb_c_dongle",
            profile_label="Apple USB-C audio adapter",
            status="ready",
            physical_output_count=2,
        ),
        path=hardware_path,
    )
    return path


def _active_topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "living_room",
        "name": "Mono active 2-way",
        "status": "verified",
        "hardware": {
            "device_id": "apple_usb_c_dongle",
            "device_label": "Apple USB-C audio adapter",
            "physical_output_count": 2,
        },
        "speaker_groups": [{
            "id": "main",
            "label": "Main",
            "kind": "mono",
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 0},
                {"role": "tweeter", "physical_output_index": 1,
                 "startup_muted": True, "protection_required": True,
                 "protection_status": "present"},
            ],
        }],
    })


def _stub_park(monkeypatch: pytest.MonkeyPatch, seen: list[OutputTopology]) -> None:
    def park_and_commit(topology, commit, **_kwargs):
        seen.append(topology)
        commit()
        return type(
            "_MutationResult",
            (),
            {
                "parked": _ParkResult(),
                "convergence": _ParkResult(),
            },
        )()

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )


def test_reset_parks_before_saving_empty_topology(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _active_topology()
    save_output_topology(stale)
    seen: list[OutputTopology] = []
    _stub_park(monkeypatch, seen)

    result = topology_runtime.reset_to_unconfigured(reconcile=False)

    assert seen == [stale]
    assert result["after"]["speaker_groups"] == []
    assert result["parked"]["ok"] is True
    assert load_output_topology_strict().speaker_groups == ()


def test_reset_recovers_from_corrupt_topology(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    topo_path.write_text("{not-json")
    seen: list[OutputTopology] = []
    _stub_park(monkeypatch, seen)

    result = topology_runtime.reset_to_unconfigured(reconcile=False)

    assert result["before"]["readable"] is False
    assert len(seen) == 1
    assert seen[0].speaker_groups == ()
    assert load_output_topology_strict().speaker_groups == ()


def test_reset_does_not_write_when_parking_fails(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _active_topology()
    save_output_topology(stale)

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        lambda _topology, _commit, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("could not park")
        ),
    )

    with pytest.raises(RuntimeError, match="park"):
        topology_runtime.reset_to_unconfigured(reconcile=False)

    assert load_output_topology_strict() == stale


def test_reset_runs_root_reconciler_after_save(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[OutputTopology] = []
    _stub_park(monkeypatch, seen)
    monkeypatch.setattr(
        topology_runtime, "trigger_reconcile", lambda: {"ok": True}
    )

    result = topology_runtime.reset_to_unconfigured()

    assert result["reconcile"] == {"ok": True}
    assert load_output_topology_strict().speaker_groups == ()


def test_grouping_reconcile_failure_blocks_hardware_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_grouping(unit, **_kwargs):
        calls.append(unit)
        return {"ok": False, "error": "grouping failed"}

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        fail_grouping,
    )

    result = topology_runtime.trigger_reconcile(reason="test")

    assert result == {"ok": False, "error": "grouping failed"}
    assert calls == [topology_runtime.GROUPING_RECONCILE_UNIT]


def test_cleanup_failure_keeps_new_topology_and_does_not_restore_old_graph(
    topo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _active_topology()
    save_output_topology(stale)
    events: list[str] = []

    def park_and_commit(_topology, commit, **_kwargs):
        events.append("park")
        commit()
        events.append("converge-new-graph")
        return type(
            "_MutationResult",
            (),
            {
                "parked": _ParkResult(),
                "convergence": _ParkResult(),
            },
        )()

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_convergence.park_and_commit_topology",
        park_and_commit,
    )

    def fail_cleanup():
        events.append("cleanup")
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        "jasper.active_speaker.reset.clear_active_speaker_setup_state",
        fail_cleanup,
    )

    result = topology_runtime.reset_to_unconfigured(reconcile=False)

    assert events == ["park", "cleanup", "converge-new-graph"]
    assert load_output_topology_strict().speaker_groups == ()
    assert result["active_speaker_reset"] == {
        "status": "partial",
        "error": "OSError: cleanup failed",
    }
