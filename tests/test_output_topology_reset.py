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


def test_reconcile_reports_converging_when_the_wait_times_out_but_job_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that outlives trigger_reconcile's 15s wait is not a failure.

    jasper-audio-hardware-reconcile measures 25.5-26s on a Pi Zero 2 W
    (restart_broker.py), past the 15s budget manage_units is given here. When
    that happens, systemd's own job is still running underneath the timed-out
    client wait -- ActiveState reads "activating" -- and that must read as
    still-converging, never as the needs_attention a real failure would be
    (#3094).
    """
    from jasper.web import _unit_snapshot

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda unit, **_kwargs: {
            "ok": False,
            "error": "systemctl invocation failed: timed out after 15.0 seconds",
        },
    )
    monkeypatch.setattr(
        _unit_snapshot,
        "probe_unit_snapshot",
        lambda units: _unit_snapshot.UnitSnapshot(
            {u: _unit_snapshot.UnitState(active_state="activating") for u in units}
        ),
    )

    result = topology_runtime.trigger_reconcile(reason="test")

    assert result["ok"] is False
    assert result["converging"] is True


def test_reconcile_stays_failed_when_the_probe_shows_no_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine failure (unit not activating) keeps the plain not-ok verdict."""
    from jasper.web import _unit_snapshot

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda unit, **_kwargs: {"ok": False, "error": "grouping failed"},
    )
    monkeypatch.setattr(
        _unit_snapshot,
        "probe_unit_snapshot",
        lambda units: _unit_snapshot.UnitSnapshot(
            {u: _unit_snapshot.UnitState(active_state="failed") for u in units}
        ),
    )

    result = topology_runtime.trigger_reconcile(reason="test")

    assert result == {"ok": False, "error": "grouping failed"}
    assert "converging" not in result


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


@pytest.mark.parametrize(
    ("reconcile", "expected_line"),
    [
        pytest.param(
            {"ok": None, "skipped": True},
            "reconcile: skipped (--no-reconcile)",
            id="skipped",
        ),
        pytest.param(
            {"ok": True},
            f"reconcile: completed {topology_runtime.RECONCILE_UNIT}",
            id="ok",
        ),
        pytest.param(
            {"ok": False, "converging": True},
            "reconcile: still converging; audio should come up shortly",
            id="converging",
        ),
        pytest.param(
            {"ok": False, "error": "grouping failed"},
            "reconcile: did not complete; audio remains parked",
            id="plain_failure",
        ),
    ],
)
def test_cli_print_summary_reconcile_line_is_honest_about_converging(
    reconcile: dict, expected_line: str, capsys: pytest.CaptureFixture[str],
) -> None:
    """A still-running job must not print as a failure, and a genuine
    failure must still print as one (#3094)."""
    from jasper.cli import output_topology_reset as cli_reset

    result = {
        "topology_path": "/tmp/output_topology.json",
        "before": {"readable": True, "name": "old", "speaker_groups": []},
        "after": {"readable": True, "name": "new", "speaker_groups": []},
        "reconcile": reconcile,
    }

    cli_reset._print_summary(result, dry_run=False)

    assert expected_line in capsys.readouterr().out
