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


def _point_grouping_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    # bass_management calls the REAL load_config with its default path; its
    # default is bound at def-time, so we wrap it to feed our path. The wrapper
    # still runs the real env-file parser + validation (real-shape).
    import jasper.multiroom.config as config_mod

    real_load = config_mod.load_config
    monkeypatch.setattr(
        config_mod, "load_config", lambda p=str(path): real_load(p)
    )


def _no_topology(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_topology_at(monkeypatch, tmp_path / "does_not_exist_topology.json")


def _no_grouping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_grouping_at(monkeypatch, tmp_path / "does_not_exist_grouping.env")


def test_no_state_resolves_to_no_bass_management(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_topology(monkeypatch, tmp_path)
    _no_grouping(monkeypatch, tmp_path)

    state = bm.resolve_bass_management()
    assert state.corner_hz is None
    assert state.owner is None
    assert state.sub_present is False
    assert bm.active_crossover_corner_hz() is None


def test_local_dac_sub_corner_is_read_from_topology(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    topo = tmp_path / "topology.json"
    _write_topology(topo, sub_corner=120.0)
    _point_topology_at(monkeypatch, topo)
    _no_grouping(monkeypatch, tmp_path)

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
    _no_grouping(monkeypatch, tmp_path)

    state = bm.resolve_bass_management()
    assert state.corner_hz == BASS_MANAGEMENT_CORNER_HZ_DEFAULT
    assert state.owner == bm.OWNER_ACTIVE_SPEAKER_LOCAL


def _write_wireless_sub_bond(path: Path, *, corner: float) -> None:
    """A real grouping.env for a leader main bonded to a wireless sub."""
    path.write_text(
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        f"JASPER_GROUPING_CROSSOVER_HZ={corner}\n"
        "JASPER_GROUPING_SUBWOOFER_PRESENT=1\n"
        "JASPER_GROUPING_ROSTER=192.168.1.8|Sub|sub\n"
    )


def test_wireless_sub_corner_is_read_when_no_local_sub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_topology(monkeypatch, tmp_path)
    grouping = tmp_path / "grouping.env"
    _write_wireless_sub_bond(grouping, corner=90.0)
    _point_grouping_at(monkeypatch, grouping)

    state = bm.resolve_bass_management()
    assert state.corner_hz == 90.0
    assert state.owner == bm.OWNER_WIRELESS_SUB
    assert state.sub_present is True
    assert state.mains_highpass_enabled is True


def test_precedence_local_dac_sub_wins_over_wireless_sub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§6: with BOTH a local-DAC active sub AND a wireless-sub bond, the
    active-speaker LOCAL config owns the corner; the wireless path defers."""
    topo = tmp_path / "topology.json"
    _write_topology(topo, sub_corner=110.0)  # local-DAC sub at 110
    _point_topology_at(monkeypatch, topo)
    grouping = tmp_path / "grouping.env"
    _write_wireless_sub_bond(grouping, corner=70.0)  # wireless sub at 70
    _point_grouping_at(monkeypatch, grouping)

    state = bm.resolve_bass_management()
    # The LOCAL corner (110) wins, not the wireless one (70).
    assert state.corner_hz == 110.0
    assert state.owner == bm.OWNER_ACTIVE_SPEAKER_LOCAL


def test_wireless_bond_without_sub_is_no_bass_management(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_topology(monkeypatch, tmp_path)
    grouping = tmp_path / "grouping.env"
    # A plain stereo pair — no sub anywhere.
    grouping.write_text(
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
    )
    _point_grouping_at(monkeypatch, grouping)

    state = bm.resolve_bass_management()
    assert state.corner_hz is None
    assert state.owner is None


def test_wireless_dumb_member_reports_toggle_with_no_unwired_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DUMB member (not an active box) actually wires the wireless mains-HP,
    so the toggle is the whole truth — no unwired reason."""
    _no_topology(monkeypatch, tmp_path)
    grouping = tmp_path / "grouping.env"
    _write_wireless_sub_bond(grouping, corner=90.0)
    _point_grouping_at(monkeypatch, grouping)

    state = bm.resolve_bass_management()
    assert state.mains_highpass_enabled is True
    assert state.mains_highpass_unwired_reason is None


def test_fourth_quadrant_active_main_with_wireless_only_sub_is_honest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fourth quadrant (known gap, HANDOFF-distributed-active "Remaining"):
    an ACTIVE-speaker main bonded to a wireless-only sub gets mains-HP applied
    ZERO times — the reconciler clears the wireless HP env for an active
    endpoint, and the Layer-A graph only folds a mains HP for a LOCAL sub. The
    resolver must report that truth (enabled=False + the unwired reason), never
    the bond toggle's "on". Real-shape: the active-box signal runs through the
    REAL reconcile.is_active_speaker_box -> load_output_topology chain against
    a real topology JSON declaring an active_2_way main and NO subwoofer group.
    """
    topo = tmp_path / "topology.json"
    _write_topology(topo, sub_corner=None, with_sub=False, with_active_main=True)
    _point_topology_at(monkeypatch, topo)
    grouping = tmp_path / "grouping.env"
    _write_wireless_sub_bond(grouping, corner=90.0)  # bond toggle says HP on
    _point_grouping_at(monkeypatch, grouping)

    state = bm.resolve_bass_management()
    # The corner still comes from the wireless bond (the room designer's
    # no-boost rule still applies there) ...
    assert state.corner_hz == 90.0
    assert state.owner == bm.OWNER_WIRELESS_SUB
    assert state.sub_present is True
    # ... but the mains-HP claim is honest for THIS box: not wired.
    assert state.mains_highpass_enabled is False
    assert (
        state.mains_highpass_unwired_reason
        == bm.MAINS_HP_UNWIRED_ACTIVE_ENDPOINT
    )


def test_sub_member_passes_bond_toggle_through_even_on_an_active_box(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A channel="sub" member never carries mains-HP itself — for it the toggle
    describes the bond's MAINS, so it passes through unchanged even when the
    box itself is active (no fourth-quadrant rewrite for the sub)."""
    topo = tmp_path / "topology.json"
    _write_topology(topo, sub_corner=None, with_sub=False, with_active_main=True)
    _point_topology_at(monkeypatch, topo)
    grouping = tmp_path / "grouping.env"
    grouping.write_text(
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_LEADER_ADDR=192.168.1.50\n"
        "JASPER_GROUPING_CHANNEL=sub\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_CROSSOVER_HZ=90.0\n"
    )
    _point_grouping_at(monkeypatch, grouping)

    state = bm.resolve_bass_management()
    assert state.owner == bm.OWNER_WIRELESS_SUB
    assert state.mains_highpass_enabled is True  # the bond default, unchanged
    assert state.mains_highpass_unwired_reason is None


def test_corrupt_topology_json_resolves_to_no_bass_management(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The module docstring's own promise: any load/parse failure resolves to
    no bass management. Corrupt JSON on disk, real reader."""
    topo = tmp_path / "topology.json"
    topo.write_text('{"artifact_schema_version": 1, "kind": "jts_output_')
    _point_topology_at(monkeypatch, topo)
    _no_grouping(monkeypatch, tmp_path)

    state = bm.resolve_bass_management()
    assert state == bm._NO_BASS_MANAGEMENT
    assert bm.active_crossover_corner_hz() is None


def test_garbage_grouping_env_resolves_to_no_bass_management(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Garbage grouping.env: binary noise, then an enabled-but-invalid bond
    (bogus role). Both resolve to no bass management, never a raise."""
    _no_topology(monkeypatch, tmp_path)
    grouping = tmp_path / "grouping.env"

    grouping.write_bytes(b"\x00\xff\xfenot=env\x00garbage\n\xf0\x9f\x94\x8a")
    _point_grouping_at(monkeypatch, grouping)
    assert bm.resolve_bass_management() == bm._NO_BASS_MANAGEMENT

    grouping.write_text(
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=bogus\n"  # fail-LOUD error in load_config
        "JASPER_GROUPING_CHANNEL=sub\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
    )
    assert bm.resolve_bass_management() == bm._NO_BASS_MANAGEMENT
    assert bm.active_crossover_corner_hz() is None
