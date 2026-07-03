# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring (shm_ring) topology-contract citizenship + statefile seeding (P2).

Two P2 contracts:
  1. topology_supports_shm_ring — ring is solo-stereo-only (not roleful, not
     composite; unconfigured/flat-stereo eligible). This is what the multiroom
     bond prechecks + reconciler consult.
  2. safe_graph_for_current_topology(coupling="shm_ring") re-seeds the RING flat
     config on a ring-armed box, not the loopback flat config — audit finding 5's
     built-in-revert dies here.
"""

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.runtime_contract import (
    DEFAULT_RING_FLAT_OUTPUTD_CONFIG,
    safe_graph_for_current_topology,
    topology_supports_shm_ring,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config, emit_flat_ring_config

# Reuse the topology builders from the main runtime-contract suite.
from tests.test_active_speaker_runtime_contract import (
    _active_topology,
    _full_range_mono,
    _full_range_stereo,
    _subwoofer_topology,
    _topology,
)


def _dual_apple_stereo() -> OutputTopology:
    """A composite (dual-Apple) stereo topology — child_devices present."""
    return OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "dual",
            "name": "Dual Apple",
            "status": "draft",
            "hardware": {
                "device_id": "dual_apple_usb_c_dac_4ch",
                "device_label": "Dual Apple",
                "physical_output_count": 4,
                "child_devices": [
                    {
                        "child_id": "a",
                        "device_id": "apple_usb_c_dongle",
                        "device_label": "Apple A",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "b",
                        "device_id": "apple_usb_c_dongle",
                        "device_label": "Apple B",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [
                {
                    "id": "left",
                    "label": "Left",
                    "kind": "left",
                    "mode": "full_range_passive",
                    "channels": [{"role": "full_range", "physical_output_index": 0}],
                },
                {
                    "id": "right",
                    "label": "Right",
                    "kind": "right",
                    "mode": "full_range_passive",
                    "channels": [{"role": "full_range", "physical_output_index": 2}],
                },
            ],
            "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
        }
    )


# --- topology_supports_shm_ring ----------------------------------------------


def test_unconfigured_topology_supports_ring():
    # Fresh install (no groups) uses the flat stereo graph — ring's exact target.
    assert topology_supports_shm_ring(_topology([])) is True


def test_full_range_stereo_supports_ring():
    assert topology_supports_shm_ring(_full_range_stereo()) is True


def test_full_range_mono_does_not_support_ring():
    # A stereo ring cannot drive an explicit mono full-range topology.
    assert topology_supports_shm_ring(_full_range_mono()) is False


def test_subwoofer_topology_does_not_support_ring():
    # Roleful (sub) -> ring is stereo-pinned, cannot carry the sub role.
    assert topology_supports_shm_ring(_subwoofer_topology()) is False


def test_active_topologies_do_not_support_ring():
    for layout in ("mono", "stereo"):
        for mode in ("active_2_way", "active_3_way"):
            assert topology_supports_shm_ring(_active_topology(layout, mode)) is False


def test_composite_dual_apple_does_not_support_ring():
    # Composite (4-ch across two child DACs) is P8's ring-v2 problem, not P2.
    assert topology_supports_shm_ring(_dual_apple_stereo()) is False


# --- statefile seeding re-seeds the ring config on a ring-armed box ----------


def _write_ring_and_loopback(tmp_path: Path) -> tuple[Path, Path]:
    loop = tmp_path / "outputd-cutover.yml"
    emit_flat_outputd_cutover_config(out_path=loop)
    ring = tmp_path / "outputd-cutover-ring.yml"
    emit_flat_ring_config(out_path=ring)
    return loop, ring


def test_ring_armed_box_reseeds_ring_flat_config(tmp_path: Path):
    loop, ring = _write_ring_and_loopback(tmp_path)
    decision = safe_graph_for_current_topology(
        _full_range_stereo(),
        flat_config_path=loop,
        ring_flat_config_path=ring,
        coupling="shm_ring",
        staged_config={},
    )
    assert decision.ok
    assert decision.status == "select_flat"
    # THE finding-5 fix: an armed box selects the RING config, not loopback.
    assert Path(decision.selected_config_path) == ring
    assert "ring-armed" in decision.reason


def test_loopback_box_reseeds_loopback_flat_config(tmp_path: Path):
    loop, ring = _write_ring_and_loopback(tmp_path)
    decision = safe_graph_for_current_topology(
        _full_range_stereo(),
        flat_config_path=loop,
        ring_flat_config_path=ring,
        coupling="loopback",
        staged_config={},
    )
    assert decision.ok
    assert Path(decision.selected_config_path) == loop


def test_no_coupling_arg_defaults_to_loopback_flat(tmp_path: Path):
    loop, ring = _write_ring_and_loopback(tmp_path)
    decision = safe_graph_for_current_topology(
        _full_range_stereo(),
        flat_config_path=loop,
        ring_flat_config_path=ring,
        staged_config={},
    )
    assert decision.ok
    assert Path(decision.selected_config_path) == loop


def test_ring_armed_falls_back_to_loopback_when_ring_config_missing(tmp_path: Path):
    # Ring config not written (P1 assets not staged / emitter hasn't run) — seeding
    # fails SAFE to the loopback flat config. NB: on an armed box this does NOT
    # restore audio (outputd still reads Ring B); its value is no camilla
    # crash-loop + doctor visibility so the operator can disarm.
    loop = tmp_path / "outputd-cutover.yml"
    emit_flat_outputd_cutover_config(out_path=loop)
    missing_ring = tmp_path / "outputd-cutover-ring.yml"  # never created
    decision = safe_graph_for_current_topology(
        _full_range_stereo(),
        flat_config_path=loop,
        ring_flat_config_path=missing_ring,
        coupling="shm_ring",
        staged_config={},
    )
    assert decision.ok
    assert Path(decision.selected_config_path) == loop


def test_ring_armed_composite_box_does_not_seed_ring_config(tmp_path: Path):
    # SF4: the seeder ring branch must consult topology_supports_shm_ring, not just
    # `not requires_roleful_graph`. A composite (dual-Apple) box is NOT roleful but
    # is NOT ring-eligible (the stereo ring cannot drive a 4-ch composite sink). A
    # stale coupling=shm_ring on such a box must fall back to the loopback flat
    # config, never seed a stereo-ring config it cannot play.
    loop, ring = _write_ring_and_loopback(tmp_path)
    decision = safe_graph_for_current_topology(
        _dual_apple_stereo(),
        flat_config_path=loop,
        ring_flat_config_path=ring,
        coupling="shm_ring",
        staged_config={},
    )
    assert decision.ok
    # Falls back to the LOOPBACK flat config — the ring path was refused.
    assert Path(decision.selected_config_path) == loop
    assert "ring-armed" not in decision.reason


def test_default_ring_flat_config_path_is_named_next_to_loopback():
    assert str(DEFAULT_RING_FLAT_OUTPUTD_CONFIG) == "/etc/camilladsp/outputd-cutover-ring.yml"
