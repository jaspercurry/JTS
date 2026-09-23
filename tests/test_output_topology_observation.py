# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import jasper.cli.output_hardware as output_hardware_cli
import jasper.output_hardware as output_hardware
import jasper.output_topology_observation as output_topology_observation
from jasper import output_topology_store as output_topology
from jasper.audio_hardware import dac, output_probe
from jasper.output_hardware import (
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    classify_output_cards,
)
from jasper.output_hardware import (
    write_state as write_output_hardware_state,
)
from jasper.output_topology import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    OutputHardware,
    OutputTopology,
    OutputTopologyError,
)
from jasper.output_topology_observation import (
    CLOCK_DOMAIN_REPORT_KIND,
    clock_domain_report,
    composite_serial_repin_plan,
    declared_hardware_mismatch,
    dual_apple_runtime_mapping,
    repin_composite_child_serials,
)
from jasper.output_topology_store import load_output_topology, new_topology_draft
from tests.test_active_speaker_runtime_contract import _active_topology, _full_range_stereo
from tests.output_topology_fixtures import (
    _dual_apple_hardware,
    _dual_apple_observation,
    _passive_main,
    _topology,
)


def _write_dual_apple_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    same_bus: bool = True,
) -> None:
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "output_hardware.json"),
    )
    write_output_hardware_state(
        _dual_apple_observation(same_bus=same_bus),
        path=tmp_path / "output_hardware.json",
    )


def test_clock_domain_report_records_single_device_boundary() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 0},
                {"role": "tweeter", "physical_output_index": 1},
            ],
        }
    ])

    report = clock_domain_report(topology, None)

    assert report["kind"] == CLOCK_DOMAIN_REPORT_KIND
    assert report["status"] == "single_device_clock"
    assert report["clock_domain_count"] == 1
    assert report["coherent_physical_output_count"] == 8
    assert report["multi_device_aggregate_supported"] is False
    assert report["sound_tests_allowed"] is False
    assert "one coherent multi-output DAC" in report["recommendation"]


@pytest.mark.parametrize("snapshot, disk, blocker", [
    (_dual_apple_observation(), _dual_apple_observation(same_bus=False), None),
    (_dual_apple_observation(same_bus=False), _dual_apple_observation(),
     "dual_apple_usb_topology_mismatch"),
    (None, _dual_apple_observation(), "dual_apple_observation_missing"),
])
def test_clock_and_mismatch_use_the_supplied_snapshot(
    monkeypatch, tmp_path, snapshot, disk, blocker,
) -> None:
    path = tmp_path / "hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(path))
    write_output_hardware_state(disk, path)
    topology = _dual_apple_active_topology()

    report = clock_domain_report(topology, snapshot)
    mismatch = declared_hardware_mismatch(topology, snapshot)

    assert report["observed_hardware"] == (
        snapshot.to_dict() if snapshot is not None else None
    )
    assert report["composite_clock_supported"] is (blocker is None)
    if blocker is None:
        assert mismatch is None
    else:
        assert mismatch is not None
        assert {issue["code"] for issue in mismatch["clock_blockers"]} == {blocker}


@pytest.mark.parametrize("saved, mapping_ok", [
    (None, True),
    ("{", False),
    ("[]", False),
    ('{"hardware": null}', False),
    ('{"hardware": {"device_id": "dual_apple_usb_c_dac_4ch", '
     '"child_devices": "invalid"}}', False),
])
def test_absent_and_corrupt_saved_intent_keep_distinct_policies(
    tmp_path, saved, mapping_ok,
) -> None:
    path = tmp_path / "topology.json"
    if saved is not None:
        path.write_text(saved)
    cards = (_apple_child("A", "left", "1-1"), _apple_child("B", "right", "1-2"))
    pair = classify_output_cards(cards)
    survivor = classify_output_cards(cards[:1])

    mapping = dual_apple_runtime_mapping(pair, topology_path=path)
    result = output_topology_observation.apply_saved_topology_policy(
        survivor, cards[:1], topology_path=path,
    )

    assert mapping.ok is mapping_ok
    assert mapping.reason == ("ok" if mapping_ok else "saved_topology_unreadable")
    assert mapping.order_source == ("observed_hardware" if mapping_ok else "")
    assert result == survivor


def test_clock_domain_report_accepts_measured_dual_apple_composite() -> None:
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology, _dual_apple_observation())
    hardware_payload = topology.to_dict()["hardware"]

    assert report["kind"] == CLOCK_DOMAIN_REPORT_KIND
    assert report["status"] == "dual_apple_composite_clock"
    assert report["clock_domain_count"] == 2
    assert report["coherent_physical_output_count"] == 4
    assert report["multi_device_aggregate_supported"] is False
    assert report["composite_clock_supported"] is True
    assert report["issues"] == []
    assert hardware_payload["child_devices"][0]["serial"] == "DWH53530FHL2FN3AC"


def test_clock_domain_report_blocks_wrong_observed_dual_apple_usb_bus() -> None:
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology, _dual_apple_observation(same_bus=False))

    assert report["status"] == "dual_apple_composite_clock_blocked"
    assert report["composite_clock_supported"] is False
    assert "dual_apple_usb_topology_mismatch" in {
        issue["code"] for issue in report["issues"]
    }


def test_clock_domain_report_requires_unique_pinned_dual_apple_child_serials() -> None:
    hardware = _dual_apple_hardware()
    hardware["child_devices"][1]["serial"] = hardware["child_devices"][0]["serial"]
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": hardware,
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology, None)

    assert report["status"] == "dual_apple_composite_clock_blocked"
    assert "dual_apple_child_serials_not_unique" in {
        issue["code"] for issue in report["issues"]
    }


def test_clock_domain_report_flags_unknown_output_clocking() -> None:
    topology = new_topology_draft(
        hardware=OutputHardware.from_mapping({
            "device_id": "mystery_usb_audio",
            "device_label": "Mystery USB audio",
            "physical_output_count": 2,
            "card_id": "Mystery",
        })
    )

    report = clock_domain_report(topology, None)

    assert report["status"] == "unknown_device_clock"
    assert report["coherent_physical_output_count"] == 0
    assert report["issues"][0]["code"] == "unknown_clock_domain"


def _dual_apple_active_topology() -> OutputTopology:
    """A fully commissioned dual-Apple pair: one active 2-way per side."""

    def group(group_id: str, kind: str, woofer: int, tweeter: int) -> dict:
        return {
            "id": group_id,
            "label": group_id.title(),
            "kind": kind,
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "driver_style": "sealed_cone",
                    "physical_output_index": woofer,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "driver_style": "compression_horn",
                    "physical_output_index": tweeter,
                    "identity_verified": True,
                    "protection_required": True,
                    "startup_muted": True,
                },
            ],
        }

    return _topology(
        groups=[group("left", "left", 0, 1), group("right", "right", 2, 3)],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
        hardware=_dual_apple_hardware(),
    )


def test_composite_repin_keeps_the_design_and_repins_only_the_swapped_child() -> None:
    """A swapped dongle re-pins its own identity and nothing else.

    The opposite of ``new_topology_draft``'s wipe: speaker groups, roles,
    driver styles, physical-output assignment, protection status, routing and
    the declaration-owned child fields all survive. Only the replaced unit's
    observed identity is rewritten.
    """

    before = _dual_apple_active_topology()
    observed = _dual_apple_observation(serial_b="NEW-DONGLE-SERIAL")

    plan = composite_serial_repin_plan(before, observed)
    assert plan is not None
    assert (plan.child_count, plan.replaced_child_count) == (2, 1)

    after = repin_composite_child_serials(before, observed)

    # Re-pinned: the swapped unit's observed identity, and nothing else.
    assert [child.serial for child in after.hardware.child_devices] == [
        "DWH53530FHL2FN3AC",
        "NEW-DONGLE-SERIAL",
    ]
    # Preserved: everything keyed to physical output index, not to a serial.
    assert [child.child_id for child in after.hardware.child_devices] == [
        "left_dac",
        "right_dac",
    ]
    assert [
        child.physical_output_indexes for child in after.hardware.child_devices
    ] == [(0, 1), (2, 3)]
    assert after.routing == before.routing
    assert [group.mode for group in after.speaker_groups] == ["active_2_way"] * 2
    assert [
        (channel.role, channel.driver_style, channel.physical_output_index,
         channel.startup_muted)
        for group in after.speaker_groups
        for channel in group.channels
    ] == [
        (channel.role, channel.driver_style, channel.physical_output_index,
         channel.startup_muted)
        for group in before.speaker_groups
        for channel in group.channels
    ]
    # A re-pin is not a second offer: the same hardware now matches the save.
    assert composite_serial_repin_plan(after, observed) is None


def test_composite_repin_is_offered_only_for_the_same_shape() -> None:
    """Same profile, same lanes, same ports — differing only in which units.

    Each rejection below is a case where nothing reliable says which physical
    unit landed on which lanes, so the full declaration ladder stays the honest
    answer.
    """

    topology = _dual_apple_active_topology()

    # The very same two units: there is nothing to re-pin.
    assert composite_serial_repin_plan(topology, _dual_apple_observation()) is None
    # A replacement DAC in a DIFFERENT port, still a ready pair: the port
    # anchor is gone, so nothing says which unit owns which lanes.
    moved = _dual_apple_observation(serial_b="NEW", port_b="usb1/1-3")
    assert moved.status == "ready"
    assert composite_serial_repin_plan(topology, moved) is None
    # Two DACs on different USB buses: the classifier already blocks the pair.
    assert composite_serial_repin_plan(
        topology,
        _dual_apple_observation(serial_b="NEW", same_bus=False),
    ) is None
    # A different profile and physical output count entirely.
    assert composite_serial_repin_plan(
        topology,
        classify_output_cards([
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="NEW",
                usb_path="usb1/1-2",
            ),
        ]),
    ) is None
    # Hardware the classifier refuses to call ready is never adopted.
    ready = _dual_apple_observation(serial_b="NEW")
    assert ready.status == "ready"
    assert composite_serial_repin_plan(topology, replace(ready, status="partial")) is None
    assert composite_serial_repin_plan(topology, None) is None
    # A single-DAC topology has no serial-keyed pairing contract to repair.
    assert composite_serial_repin_plan(
        _topology(groups=[_passive_main("mono", "mono", 0)]),
        _dual_apple_observation(serial_b="NEW"),
    ) is None


def test_declared_hardware_mismatch_is_none_when_declared_matches_observed() -> None:
    """#2812 B1: an already-declared, already-armed box must not mismatch.

    Proven live: a declared, serial-bound dual-Apple speaker hitting an
    unrelated outputd fault was told to "finish setup" for a setup that
    already happened, because the detector that reads this result checked
    only whether the DETECTED hardware was usable, never whether it already
    matched the DECLARATION. Same fixtures ``composite_serial_repin_plan``
    uses to prove "nothing to re-pin" (the very same two units) — the outer
    conjunct must agree with the inner one that this box needs no action.
    """
    topology = _dual_apple_active_topology()
    observed = _dual_apple_observation()

    assert declared_hardware_mismatch(topology, observed) is None


def test_declared_hardware_mismatch_flags_an_unknown_declared_profile() -> None:
    """A DECLARED (saved) topology naming an unrecognized profile mismatches
    against a real detected one -- ordinary id-comparison behavior, distinct
    from the "nothing has ever been saved" case (see the auto-seed test
    below, and #2812 B2 for why those two are not interchangeable).
    """
    topology = _topology(
        groups=[],
        hardware={
            "device_id": "unknown",
            "device_label": "Unknown output device",
            "physical_output_count": 0,
        },
    )
    observed = _dual_apple_observation()

    mismatch = declared_hardware_mismatch(topology, observed)

    assert mismatch is not None
    assert "Saved topology expects" in mismatch["message"]
    assert "Dual Apple USB-C DAC 4-channel pair" in mismatch["message"]


def test_declared_hardware_mismatch_cannot_see_new_topology_drafts_auto_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#2812 B2: a never-saved draft auto-seeds FROM ready observed hardware,
    so this function alone reports NO mismatch for a genuinely undeclared
    box -- the false model an earlier version of the test above encoded, by
    hand-building a ``device_id="unknown"`` topology instead of calling the
    real missing-file loader. That is not what
    ``new_topology_draft``/``load_output_topology``/
    ``load_output_topology_snapshot`` actually return once the observed
    hardware is ready: they auto-seed ``hardware`` FROM it, so the "declared"
    and "observed" sides match by construction and this function correctly,
    but unhelpfully, reports no mismatch.

    ``new_topology_draft`` is called for real here, against the SAME
    sandboxed observed-hardware path ``_write_dual_apple_observation``
    writes, so this cannot silently drift from what the real loaders
    produce. Callers that need "was anything ever persisted?" must ask
    ``load_output_topology_snapshot`` directly (``revision == "missing"``)
    instead of inferring it from this function's verdict — see
    ``jasper.control.audio_signal_path._undeclared_hardware_signal``.
    """
    _write_dual_apple_observation(monkeypatch, tmp_path)
    observed = _dual_apple_observation()

    draft = new_topology_draft()

    assert draft.hardware.device_id == DUAL_APPLE_ACTIVE_DEVICE_ID
    assert declared_hardware_mismatch(draft, observed) is None


def test_declared_hardware_mismatch_flags_an_output_count_change() -> None:
    """A declared single dongle vs. a now-attached dual-Apple pair mismatches.

    Mirrors #2812's own live repro: a second Apple dongle hot-plugged next
    to an already-declared single dongle.
    """
    topology = _topology(
        groups=[],
        hardware={
            "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
            "device_label": "Apple USB-C audio adapter",
            "physical_output_count": 2,
        },
    )
    observed = _dual_apple_observation()

    mismatch = declared_hardware_mismatch(topology, observed)

    assert mismatch is not None
    assert mismatch["saved_count"] == 2
    assert mismatch["current_count"] == 4


def test_declared_hardware_mismatch_flags_an_unobserved_dual_apple_clock() -> None:
    """A missing snapshot blocks the declared dual-Apple clock."""
    topology = _dual_apple_active_topology()
    observed = None

    mismatch = declared_hardware_mismatch(topology, observed)

    assert mismatch is not None
    assert any(
        issue["code"] == "dual_apple_observation_missing"
        for issue in mismatch["clock_blockers"]
    )


def test_declared_hardware_mismatch_is_none_with_nothing_observed_or_declared() -> None:
    """A box with no hardware ever observed and nothing dual-Apple declared
    stays quiet -- there is genuinely nothing to compare yet."""
    topology = _topology(
        groups=[],
        hardware={
            "device_id": "unknown",
            "device_label": "Unknown output device",
            "physical_output_count": 0,
        },
    )

    assert declared_hardware_mismatch(topology, None) is None


def test_composite_repin_refuses_to_mutate_without_an_offer() -> None:
    """The mutation is not a second, laxer door onto the same change."""

    topology = _dual_apple_active_topology()
    with pytest.raises(OutputTopologyError):
        repin_composite_child_serials(topology, _dual_apple_observation())


def test_dual_apple_runtime_mapping_uses_saved_topology_order(
    tmp_path: Path,
) -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="B",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="right",
            usb_path="1-1",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=B,DEV=0",
        ),
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="left",
            usb_path="1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=A,DEV=0",
        ),
    ])
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "dual_apple",
            "name": "Dual Apple",
            "status": "ready",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "outputs": [],
                "child_devices": [
                    {
                        "child_id": "left",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "left",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "right",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "right",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    mapping = dual_apple_runtime_mapping(state, topology_path=topology_path)

    assert mapping.ok is True
    assert mapping.order_source == "saved_topology"
    assert [child.pcm for child in mapping.child_devices] == [
        "hw:CARD=A,DEV=0",
        "hw:CARD=B,DEV=0",
    ]


def test_dual_apple_runtime_mapping_uses_physical_usb_path_without_serials(
    tmp_path: Path,
) -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="B",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            stable_path="/sys/devices/platform/xhci-hcd.0/usb1/1-1",
            usb_path="1-1",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=B,DEV=0",
        ),
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            stable_path="/sys/devices/platform/xhci-hcd.0/usb1/1-2",
            usb_path="1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=A,DEV=0",
        ),
    ])
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "dual_apple",
            "name": "Dual Apple",
            "status": "ready",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "outputs": [],
                "child_devices": [
                    {
                        "child_id": "left",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "stable_path": "/sys/devices/platform/xhci-hcd.0/usb1/1-2",
                        "usb_path": "1-2",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "right",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "stable_path": "/sys/devices/platform/xhci-hcd.0/usb1/1-1",
                        "usb_path": "1-1",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    mapping = dual_apple_runtime_mapping(state, topology_path=topology_path)

    assert mapping.ok is True
    assert mapping.order_source == "saved_topology"
    assert [child.pcm for child in mapping.child_devices] == [
        "hw:CARD=A,DEV=0",
        "hw:CARD=B,DEV=0",
    ]


def test_dual_apple_runtime_mapping_blocks_saved_topology_identity_mismatch(
    tmp_path: Path,
) -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="observed-a",
            usb_path="1-1",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=A,DEV=0",
        ),
        OutputCardFact(
            card_id="B",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="observed-b",
            usb_path="1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=B,DEV=0",
        ),
    ])
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "dual_apple",
            "name": "Dual Apple",
            "status": "ready",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "outputs": [],
                "child_devices": [
                    {
                        "child_id": "old-a",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "old-a",
                        "usb_path": "9-1",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "old-b",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "old-b",
                        "usb_path": "9-2",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    mapping = dual_apple_runtime_mapping(state, topology_path=topology_path)

    assert mapping.ok is False
    assert mapping.reason == "saved_topology_child_identity_mismatch"


@pytest.mark.parametrize("use_env", [False, True])
def test_raw_hardware_and_saved_topology_read_the_same_selected_file(
    monkeypatch, tmp_path, use_env,
) -> None:
    default = _dual_apple_active_topology()
    override = _full_range_stereo()
    default_path = tmp_path / "default.json"
    override_path = tmp_path / "override.json"
    output_topology.save_output_topology(default, default_path)
    output_topology.save_output_topology(override, override_path)
    monkeypatch.setattr(output_topology, "DEFAULT_TOPOLOGY_PATH", default_path)
    monkeypatch.delenv("JASPER_OUTPUT_TOPOLOGY_PATH", raising=False)
    if use_env:
        monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(override_path))
    expected = override if use_env else default

    assert output_topology_observation._read_topology_hardware() == (
        True, expected.hardware.to_dict(),
    )
    assert load_output_topology().to_dict() == expected.to_dict()
    assert output_topology_observation._saved_topology_requires_roleful_graph() is (
        not use_env
    )


def _apple_child(card_id: str, serial: str, usb_path: str) -> OutputCardFact:
    return OutputCardFact(
        card_id=card_id,
        device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        serial=serial,
        usb_path=usb_path,
        busnum="1",
        controller="xhci-hcd.0",
        endpoint_sync="SYNC",
        pcm=f"hw:CARD={card_id},DEV=0",
    )


def _saved_topology(tmp_path: Path, base, hardware: dict) -> Path:
    """Save ``base``'s speaker groups under ``hardware``.

    The groups come from the runtime-contract suite's own fixtures, so
    "roleful" and "passive" here mean exactly what ``classify_output_contract``
    means by them rather than a shape hand-written in this file — which is the
    whole point, since rolefulness is now what gates the park.
    """
    raw = base.to_dict()
    raw["hardware"] = hardware
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(json.dumps(raw), encoding="utf-8")
    return topology_path


def _assert_saved_rolefulness(topology_path: Path, expected: bool) -> None:
    """Prove the fixture is what its name claims before a test relies on it.

    Not ceremony: an invalid topology loads soft to an empty draft, which is
    NOT roleful, so a malformed fixture silently turns a park test into a
    no-op that still passes for the wrong reason. That happened once here —
    `"outputs": []` against `physical_output_count: 4` failed validation, and
    the tests only caught it because the park assertions were downstream.

    The group check is the other half, and it is the half that matters on the
    `expected=False` side: an empty draft is not roleful either, so asserting
    "not roleful" alone would pass for the very malformation this guard exists
    to catch — leaving the passive fixture, which is what pins the SF-1 carve
    out, unprotected.
    """

    topology = load_output_topology(topology_path)
    assert topology.speaker_groups, (
        f"{topology_path} loaded with no speaker groups — the fixture is "
        "malformed and soft-loaded to an empty draft"
    )
    assert output_topology_observation._saved_topology_requires_roleful_graph(
        topology_path
    ) is expected


def _roleful_composite_topology(tmp_path: Path) -> Path:
    """A commissioned active 2-way across both children — needs per-driver DSP."""

    path = _saved_topology(
        tmp_path,
        _active_topology("stereo", "active_2_way"),
        _saved_composite_hardware(),
    )
    _assert_saved_rolefulness(path, True)
    return path


def _passive_composite_topology(tmp_path: Path) -> Path:
    """A composite whose declared speakers all sit on child A's outputs.

    The live jts.local shape: `kind == "composite"` but full-range passive, so
    `requires_roleful_graph` is False and child B carries no declared channel.
    Unplugging B must not park a working stereo.
    """

    path = _saved_topology(
        tmp_path,
        _full_range_stereo(),
        _saved_composite_hardware(),
    )
    _assert_saved_rolefulness(path, False)
    return path


def _saved_composite_hardware() -> dict:
    return {
        "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "left",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "right",
                "physical_output_indexes": [2, 3],
            },
        ],
    }


def test_saved_composite_with_one_child_present_is_never_ready(
    tmp_path: Path,
) -> None:
    """A declared composite missing a child must not read as a stereo DAC.

    The surviving dongle classifies on its own as an ordinary full-range
    two-output device, and ``ready`` is the one word every consumer reads as
    "safe to drive this speaker with". Downstream layers do refuse a flat
    full-range graph on a roleful topology, so today's outcome is quiet rather
    than dangerous — but nothing states a reason. The record has to fail closed
    and name the child that is gone.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)
    assert observed.status == "ready"
    assert observed.profile_id == APPLE_USB_C_DONGLE_DEVICE_ID

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state.status == "partial"
    assert state.profile_id == APPLE_USB_C_DONGLE_DEVICE_ID
    blockers = [
        issue for issue in state.issues if issue["severity"] == "blocker"
    ]
    assert [issue["code"] for issue in blockers] == [
        "saved_composite_partially_present"
    ]
    # The reason names the child that is gone, not just that something is.
    assert "right" in blockers[0]["message"]
    assert "left" not in blockers[0]["message"]
    # `/state` and the wizard's adopt affordance both read this record.
    assert output_hardware.detected_hardware_adoption_precondition(
        state
    )["allowed"] is False


def test_saved_passive_composite_missing_a_child_still_plays(
    tmp_path: Path,
) -> None:
    """`kind == "composite"` is not permission to park a working stereo.

    A passive composite can put every declared speaker on one child's outputs,
    which is the live jts.local shape. The other child carries no declared
    channel, so losing it costs the household nothing — and `jasper-doctor`
    already calibrates exactly this mismatch as warn rather than fail
    (`check_active_speaker_output_hardware`). Rolefulness, not compositeness,
    is the gate.
    """

    topology_path = _passive_composite_topology(tmp_path)
    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.status == "ready"
    assert state.issues == ()


def test_saved_composite_with_both_children_present_is_untouched(
    tmp_path: Path,
) -> None:
    topology_path = _roleful_composite_topology(tmp_path)
    cards = [
        _apple_child("A", "left", "1-1"),
        _apple_child("A_1", "right", "1-2"),
    ]
    observed = classify_output_cards(cards)

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert state.status == "ready"


def _saved_single_hardware() -> dict:
    return {
        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
        "device_label": "Apple USB-C audio adapter",
        "physical_output_count": 2,
        "card_id": "A",
    }


def test_saved_single_topology_keeps_full_range_stereo_ready(
    tmp_path: Path,
) -> None:
    """Full-range stereo is a legal shape for a saved passive/solo speaker."""


    topology_path = _saved_topology(
        tmp_path, _full_range_stereo(), _saved_single_hardware()
    )
    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.status == "ready"


def test_saved_single_topology_is_untouched_when_the_observed_dac_differs(
    tmp_path: Path,
) -> None:
    """A ROLEFUL saved single whose hardware was swapped is still not ours.

    The sibling test above saves and observes the same profile, so it would
    pass even without the composite guard. This one mismatches a roleful saved
    topology against a different DAC — the case that makes the guard
    load-bearing. Saved/attached mismatch on a non-composite is
    `jasper-doctor`'s to report, not this policy's to park.

    The topology must be **roleful and valid** or this test proves nothing: a
    mono active 2-way fits the dongle's two outputs, where a stereo one would
    not, and an over-wide topology loads soft to a non-roleful empty draft that
    the rolefulness gate would catch instead of the composite guard.
    """


    topology_path = _saved_topology(
        tmp_path, _active_topology("mono", "active_2_way"), _saved_single_hardware()
    )
    _assert_saved_rolefulness(topology_path, True)
    cards = [
        OutputCardFact(
            card_id="DAC8",
            device_id=dac.HIFIBERRY_DAC8X_ID,
            label="HiFiBerry DAC8x",
            pcm="hw:CARD=DAC8,DEV=0",
        )
    ]
    observed = classify_output_cards(cards)
    assert observed.profile_id == dac.HIFIBERRY_DAC8X_ID

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.issues == ()


def test_unsaved_topology_keeps_todays_classification(tmp_path: Path) -> None:
    """A speaker with no saved topology yet still adopts what it sees."""

    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=tmp_path / "output_topology.json",
    )

    assert state == observed
    assert state.status == "ready"


def test_saved_composite_with_no_output_hardware_keeps_missing_status(
    tmp_path: Path,
) -> None:
    """Both children gone already parks; it gains the reason, not a new status.

    ``missing`` is a truer word than ``partial`` for "no output hardware at
    all", so the policy only downgrades a ``ready`` observation.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    observed = classify_output_cards([])

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        [],
        topology_path=topology_path,
    )

    assert state.status == "missing"
    codes = [issue["code"] for issue in state.issues]
    assert codes == ["saved_composite_partially_present"]
    assert "left" in state.issues[0]["message"]
    assert "right" in state.issues[0]["message"]


def test_reason_never_names_a_child_that_is_physically_present(
    tmp_path: Path,
) -> None:
    """The diagnosis is diffed against observed CARDS, not the record's children.

    Attach a registered single DAC beside both dongles and the classified
    record's `child_devices` is that DAC alone — diffing against it would
    report both Apple children missing while they are plugged in. The one
    surface this policy exists to produce must not misname hardware.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    cards = [
        _apple_child("A", "left", "1-1"),
        _apple_child("A_1", "right", "1-2"),
        OutputCardFact(
            card_id="DAC8",
            device_id=dac.HIFIBERRY_DAC8X_ID,
            label="HiFiBerry DAC8x",
            pcm="hw:CARD=DAC8,DEV=0",
        ),
    ]
    observed = classify_output_cards(cards)
    # The third DAC wins classification, so the record's children are not the
    # Apple pair at all — the exact shape that produced the false diagnosis.
    assert observed.profile_id == dac.HIFIBERRY_DAC8X_ID
    assert [child.card_id for child in observed.child_devices] == ["DAC8"]

    state = output_topology_observation.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    blockers = [
        issue for issue in state.issues
        if issue["code"] == "saved_composite_partially_present"
    ]
    assert len(blockers) == 1
    message = blockers[0]["message"]
    # Pin the TRUE sentence for this shape. Both children are attached, so the
    # remediation is to remove the interloper — NOT to reconnect anything, and
    # certainly not the inverse claim that nothing could be matched.
    assert "every declared child device is attached" in message
    assert "detach it" in message
    assert "missing child devices" not in message
    assert "no saved child device could be matched" not in message
    assert "left" not in message
    assert "right" not in message


def test_reason_says_so_when_the_topology_declares_no_children(
    tmp_path: Path,
) -> None:
    """A roleful composite with no declared children is its own sentence.

    Reachable: a composite `hardware` block with `child_devices` absent or
    empty still loads and is still roleful, so the park arm runs with nothing
    to diff. "No child matched" would be vacuously true here and actively
    misleading in the sibling shape above, which is why they are separate.
    """


    hardware = _saved_composite_hardware()
    del hardware["child_devices"]
    topology_path = _saved_topology(
        tmp_path, _active_topology("stereo", "active_2_way"), hardware
    )
    _assert_saved_rolefulness(topology_path, True)
    cards = [_apple_child("A", "left", "1-1")]

    state = output_topology_observation.apply_saved_topology_policy(
        classify_output_cards(cards),
        cards,
        topology_path=topology_path,
    )

    message = state.issues[-1]["message"]
    assert "declares no child devices" in message
    assert "missing child devices" not in message
    assert "every declared child device is attached" not in message


def test_published_record_carries_the_partial_composite_reason(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """`python -m jasper.cli.output_hardware --write` is the record's one writer.

    The reconciler and `/state` both read what this publishes, so the policy
    has to be applied here rather than by each consumer.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    state_file = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_file))
    monkeypatch.setattr(
        output_probe,
        "probe_system_cards",
        lambda **_kwargs: (_apple_child("A", "left", "1-1"),),
    )

    assert output_hardware_cli.main(["--write"]) == 0

    capsys.readouterr()
    published = json.loads(state_file.read_text(encoding="utf-8"))
    assert published["status"] == "partial"
    assert published["profile_id"] == APPLE_USB_C_DONGLE_DEVICE_ID
    assert [issue["code"] for issue in published["issues"]] == [
        "saved_composite_partially_present"
    ]


_ENV_CONTRACT_KEYS = {
    "OBSERVED_OUTPUT_PROFILE_ID",
    "OBSERVED_OUTPUT_PROFILE_STATUS",
    "OBSERVED_OUTPUT_PROFILE_KIND",
    "OBSERVED_OUTPUT_HEADPHONE_CONTROL",
    "OBSERVED_OUTPUT_SELECTED_CARD_ID",
    "OBSERVED_OUTPUT_CHILD_DEVICE_IDS",
    "OBSERVED_OUTPUT_APPLE_CARD_IDS",
    "OBSERVED_OUTPUT_BLOCKER_CODES",
    "OBSERVED_OUTPUT_RECORD_CHANGED",
    "OBSERVED_OUTPUT_USB_MANAGEMENT_TRANSPORT_AVAILABLE",
    "OBSERVED_OUTPUT_DUAL_MAPPING_OK",
    "OBSERVED_OUTPUT_DUAL_MAPPING_REASON",
    "OBSERVED_OUTPUT_DUAL_ORDER_SOURCE",
    "OBSERVED_OUTPUT_DUAL_DAC_A_PCM",
    "OBSERVED_OUTPUT_DUAL_DAC_B_PCM",
}


def _emitted_env(payload: str) -> dict[str, str]:
    emitted = {}
    for line in payload.splitlines():
        key, _, quoted = line.partition("=")
        parts = shlex.split(quoted)
        emitted[key] = parts[0] if parts else ""
    return emitted


@pytest.mark.parametrize(
    "card_id",
    ["A", "two words", "it's \"quoted\"", "$(touch pwned) `id` ; rm -rf /"],
    ids=["plain", "space", "quotes", "injection"],
)
def test_env_emitter_hands_bash_the_whole_contract_and_nothing_it_must_parse(
    card_id: str, tmp_path: Path,
) -> None:
    """What Python quotes, bash evals — for every key, whatever the card is.

    A card id is hardware-supplied text that reaches a shell owner, so the
    round trip is asserted through a real `bash -u` eval rather than by
    re-reading the quoting rule. `-u` also proves the emitter defines every
    key: a missing one is an unbound-variable failure, not an empty string.
    """
    card = OutputCardFact(
        card_id=card_id,
        label="Apple USB-C to 3.5mm Headphone Jack Adapter",
        device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        vendor_id="05ac",
        product_id="110a",
        pcm=f"hw:CARD={card_id},DEV=0",
    )
    state = classify_output_cards((card,))
    payload = output_hardware_cli.env_lines(state, (card,), record_changed=True)

    emitted = _emitted_env(payload)
    assert set(emitted) == _ENV_CONTRACT_KEYS
    assert emitted["OBSERVED_OUTPUT_SELECTED_CARD_ID"] == card_id
    assert emitted["OBSERVED_OUTPUT_APPLE_CARD_IDS"] == card_id
    assert emitted["OBSERVED_OUTPUT_RECORD_CHANGED"] == "1"

    keys = sorted(_ENV_CONTRACT_KEYS)
    reader = "; ".join(f'printf "%s\\n" "${{{key}}}"' for key in keys)
    seen = subprocess.run(
        ["bash", "-uc", f'eval "$1"; {reader}', "bash", payload],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert seen.returncode == 0, seen.stderr
    assert seen.stdout.splitlines() == [emitted[key] for key in keys]
    assert not (tmp_path / "pwned").exists()


def _apple_cards(*card_ids: str) -> tuple[OutputCardFact, ...]:
    return tuple(
        OutputCardFact(
            card_id=card_id,
            label="Apple USB-C to 3.5mm Headphone Jack Adapter",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            vendor_id="05ac",
            product_id="110a",
            pcm=f"hw:CARD={card_id},DEV=0",
        )
        for card_id in card_ids
    )


def _declared_percent_control(profile_id: str) -> str:
    """The registry row's own percent-pinned control name, re-derived here
    off the raw dataclass fields (a composite inherits its children's) so the
    pin cannot pass by agreeing with the accessor it is pinning."""
    profile = dac.by_id(profile_id)
    assert profile is not None
    rows = [profile] if profile.kind == "single" else [
        dac.by_id(child_id) for child_id in profile.child_profile_ids
    ]
    names = {
        control.name
        for row in rows
        if row is not None
        for control in row.mixer_controls
        if control.target_percent is not None
    }
    return names.pop() if len(names) == 1 else ""


@pytest.mark.parametrize(
    ("profile_id", "cards"),
    [
        pytest.param(
            APPLE_USB_C_DONGLE_DEVICE_ID, _apple_cards("A"), id="single-usb"
        ),
        pytest.param(
            DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
            _apple_cards("A", "A_1"),
            id="composite",
        ),
        pytest.param(
            dac.HIFIBERRY_DAC8X_ID,
            (
                OutputCardFact(
                    card_id="sndrpihifiberry",
                    label="HiFiBerry DAC8x",
                    device_id=dac.HIFIBERRY_DAC8X_ID,
                    pcm="hw:CARD=sndrpihifiberry,DEV=0",
                ),
            ),
            id="i2s-single",
        ),
    ],
)
def test_the_emitter_answers_shape_and_mixer_control_from_the_registry(
    profile_id: str, cards: tuple[OutputCardFact, ...],
) -> None:
    """The two facts the shell owner branches on: which DAC shape to route
    (a composite goes to the paired sink) and which mixer control the drift
    monitor pins (no control, no monitor). A composite answers both while
    still PARKED, which is exactly when the shell has to name it.
    """
    state = classify_output_cards(cards)
    emitted = _emitted_env(output_hardware_cli.env_lines(state, cards))
    profile = dac.by_id(profile_id)
    assert profile is not None

    assert state.profile_id == profile_id
    assert emitted["OBSERVED_OUTPUT_PROFILE_KIND"] == profile.kind
    assert emitted["OBSERVED_OUTPUT_HEADPHONE_CONTROL"] == (
        _declared_percent_control(profile_id)
    )
