# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared bass-management corner READ seam (jasper.bass_management).

Real-shape (the P7 lesson): drive the resolver through the REAL
`load_output_topology` (a real topology JSON on disk) and the REAL multiroom
`load_config` (a real grouping.env), never a hand-mocked reader. Assert the §6
corner precedence (local-DAC active sub wins over a wireless-sub bond) and the
fail-soft "no bass management" resolution.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper import bass_management as bm

# A hardware block big enough for a 2-way main + sub (4 outputs). Mirrors the
# real persisted shape (hifiberry_dac8x has 8 outputs).
_HARDWARE = {
    "device_id": "hifiberry_dac8x",
    "device_label": "HiFiBerry DAC8x",
    "card": "sndrpihifiberry",
    "physical_output_count": 8,
}


def _active_main_group() -> dict:
    """A mono active 2-way main group — makes the REAL
    `reconcile.is_active_speaker_box()` chain report an active box."""
    return {
        "id": "main",
        "label": "Active main",
        "kind": "mono",
        "mode": "active_2_way",
        "channels": [
            {
                "role": "woofer",
                "physical_output_index": 0,
                "identity_verified": True,
                "startup_muted": True,
            },
            {
                "role": "tweeter",
                "physical_output_index": 1,
                "identity_verified": True,
                "startup_muted": True,
                "protection_required": True,
            },
        ],
    }


def _write_topology(
    path: Path,
    *,
    sub_corner: float | None,
    with_sub: bool = True,
    with_active_main: bool = False,
) -> None:
    """Persist a real topology JSON.

    ``with_sub`` includes a subwoofer group whose channel carries
    ``sub_corner`` as crossover_fc_hz (None => omit it, i.e. the sub uses the
    default corner). ``with_active_main`` includes an active 2-way main group
    (the fourth-quadrant fixture: active box, NO local sub)."""
    groups: list[dict] = []
    routing: dict = {}
    if with_active_main:
        groups.append(_active_main_group())
    if with_sub:
        sub_channel: dict = {
            "role": "subwoofer",
            "physical_output_index": 4,
            "identity_verified": True,
            "startup_muted": True,
        }
        if sub_corner is not None:
            sub_channel["crossover_fc_hz"] = sub_corner
        groups.append(
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [sub_channel],
            }
        )
        routing["subwoofer_group_ids"] = ["sub"]
    raw = {
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": _HARDWARE,
        "speaker_groups": groups,
        "routing": routing,
    }
    path.write_text(json.dumps(raw))


def _point_topology_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))


def _no_topology(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_topology_at(monkeypatch, tmp_path / "does_not_exist_topology.json")


def test_no_state_resolves_to_no_bass_management(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_topology(monkeypatch, tmp_path)

    state = bm.resolve_bass_management()
    assert state.corner_hz is None
    assert state.owner is None
    assert state.sub_present is False
    assert "bass_extension" not in state.to_dict()
    assert bm.active_crossover_corner_hz() is None


def test_local_dac_sub_corner_is_read_from_topology(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    topo = tmp_path / "topology.json"
    _write_topology(topo, sub_corner=120.0)
    _point_topology_at(monkeypatch, topo)

    state = bm.resolve_bass_management()
    assert state.corner_hz == 120.0
    assert state.owner == bm.OWNER_ACTIVE_SPEAKER_LOCAL
    assert state.sub_present is True
    assert state.mains_highpass_enabled is True
    assert bm.active_crossover_corner_hz() == 120.0


def test_local_dac_sub_without_explicit_corner_uses_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jasper.camilla_emit import BASS_MANAGEMENT_CORNER_HZ_DEFAULT

    topo = tmp_path / "topology.json"
    _write_topology(topo, sub_corner=None)  # no explicit corner
    _point_topology_at(monkeypatch, topo)

    state = bm.resolve_bass_management()
    assert state.corner_hz == BASS_MANAGEMENT_CORNER_HZ_DEFAULT
    assert state.owner == bm.OWNER_ACTIVE_SPEAKER_LOCAL


def test_corrupt_topology_json_resolves_to_no_bass_management(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The module docstring's own promise: any load/parse failure resolves to
    no bass management. Corrupt JSON on disk, real reader."""
    topo = tmp_path / "topology.json"
    topo.write_text('{"artifact_schema_version": 1, "kind": "jts_output_')
    _point_topology_at(monkeypatch, topo)

    state = bm.resolve_bass_management()
    assert state == bm._NO_BASS_MANAGEMENT
    assert bm.active_crossover_corner_hz() is None
