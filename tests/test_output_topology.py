# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from jasper import output_topology as output_topology_mod
from jasper.output_hardware import (
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    write_state as write_output_hardware_state,
)
from jasper.output_topology import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    CHANNEL_IDENTITY_REPORT_KIND,
    CLOCK_DOMAIN_REPORT_KIND,
    DEFAULT_PAIRING_INTENT,
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    PAIRING_INTENTS,
    OutputHardware,
    OutputTopology,
    OutputTopologyError,
    SpeakerChannel,
    channel_identity_report,
    clock_domain_report,
    composite_serial_repin_plan,
    declared_hardware_mismatch,
    unknown_output_hardware,
    load_output_topology,
    load_output_topology_snapshot,
    load_output_topology_strict,
    new_topology_draft,
    repin_composite_child_serials,
    save_output_topology,
    set_channel_identity_verified,
    set_channel_protection_status,
    topology_is_passive_mains,
    topology_is_subless_passive_mains,
)


def _base_hardware() -> dict:
    return {
        "device_id": "hifiberry_dac8x",
        "device_label": "HiFiBerry DAC8x",
        "physical_output_count": 8,
    }


def _dual_apple_hardware() -> dict:
    hardware = {
        "device_id": DUAL_APPLE_ACTIVE_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left_dac",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FHL2FN3AC",
                "card_id": "A",
                "usb_path": "usb1/1-2",
                "controller": "xhci-hcd.0",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right_dac",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FLL2FN3A3",
                "card_id": "A_1",
                "usb_path": "usb1/1-1",
                "controller": "xhci-hcd.0",
                "physical_output_indexes": [2, 3],
            },
        ],
    }
    return hardware


def _dual_apple_observation(
    *,
    serial_a: str = "DWH53530FHL2FN3AC",
    serial_b: str = "DWH53530FLL2FN3A3",
    port_b: str = "usb1/1-1",
    same_bus: bool = True,
) -> OutputHardwareState:
    """Classify the two attached Apple dongles the saved fixture pins.

    ``port_b`` moves the second dongle to another port on the SAME USB bus, so
    the classifier still calls the pair ready — that is the case a re-pin's
    port anchor must reject on its own merits rather than inheriting a refusal
    from ``same_bus=False`` (which the classifier already blocks).
    """

    return classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial=serial_a,
            usb_path="usb1/1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
        ),
        OutputCardFact(
            card_id="A_1",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial=serial_b,
            usb_path=port_b if same_bus else "usb3/3-1",
            busnum="1" if same_bus else "3",
            controller="xhci-hcd.0" if same_bus else "xhci-hcd.1",
            endpoint_sync="SYNC",
        ),
    ])


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


def _topology(
    *,
    groups: list[dict],
    routing: dict | None = None,
    hardware: dict | None = None,
) -> OutputTopology:
    raw = {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": hardware or _base_hardware(),
        "speaker_groups": groups,
        "routing": routing or {},
    }
    return OutputTopology.from_mapping(raw)


def _passive_main(
    group_id: str, kind: str, index: int, *, identity_verified: bool = False
) -> dict:
    return {
        "id": group_id,
        "label": group_id.title(),
        "kind": kind,
        "mode": "full_range_passive",
        "channels": [{
            "role": "full_range",
            "physical_output_index": index,
            "identity_verified": identity_verified,
        }],
    }


def _sub_group(index: int) -> dict:
    return {
        "id": "sub",
        "label": "Subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        "channels": [{"role": "subwoofer", "physical_output_index": index}],
    }


def test_passive_shape_predicates_separate_subless_from_with_sub() -> None:
    """The ONE owner of "these mains carry no inter-driver crossover".

    ``topology_is_subless_passive_mains`` and the with-sub shape are built on
    it; the setup ladder terminates for the subless one only.
    """

    mono = _topology(groups=[_passive_main("mono", "mono", 0)])
    stereo = _topology(groups=[
        _passive_main("left", "left", 0),
        _passive_main("right", "right", 1),
    ])
    with_sub = _topology(
        groups=[_passive_main("mono", "mono", 0), _sub_group(1)],
        routing={"mono_group_id": "mono", "subwoofer_group_ids": ["sub"]},
    )
    active = _topology(groups=[{
        "id": "mono",
        "label": "Mono",
        "kind": "mono",
        "mode": "active_2_way",
        "channels": [
            {"role": "woofer", "physical_output_index": 0},
            {"role": "tweeter", "physical_output_index": 1},
        ],
    }])

    assert topology_is_subless_passive_mains(mono) is True
    assert topology_is_subless_passive_mains(stereo) is True
    # A sub means bass management, which IS an active split — different shape.
    assert topology_is_passive_mains(with_sub) is True
    assert topology_is_subless_passive_mains(with_sub) is False
    assert topology_is_passive_mains(active) is False
    assert topology_is_subless_passive_mains(active) is False
    # One passive main plus one active main is not a passive speaker.
    mixed = _topology(groups=[
        _passive_main("left", "left", 0),
        {
            "id": "right",
            "label": "Right",
            "kind": "right",
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 1},
                {"role": "tweeter", "physical_output_index": 2},
            ],
        },
    ])
    assert topology_is_subless_passive_mains(mixed) is False
    # No mains at all is not a passive speaker either (fail-closed).
    assert topology_is_subless_passive_mains(_topology(groups=[])) is False


def test_unknown_output_hardware_declares_no_outputs() -> None:
    unknown = unknown_output_hardware()

    assert unknown.device_id == "unknown"
    assert unknown.physical_output_count == 0
    assert unknown.outputs == ()


@pytest.mark.parametrize(
    "hardware,clock_domain_id",
    [
        (
            {
                "device_id": "hifiberry_dac8x",
                "device_label": "HiFiBerry DAC8x",
                "physical_output_count": 8,
                "card_id": "sndrpihifiberry",
            },
            "alsa:sndrpihifiberry",
        ),
        (
            {
                "device_id": "dual_apple_usb_c_dac_4ch",
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
            },
            "profile:dual-apple-usb-c-dac-4ch",
        ),
    ],
)
def test_loaded_hardware_derives_clock_domain_and_output_labels(
    hardware, clock_domain_id,
) -> None:
    loaded = OutputHardware.from_mapping(hardware)

    assert loaded.clock_domain_id == clock_domain_id
    assert [output.human_label for output in loaded.outputs] == [
        f"DAC output {index + 1}" for index in range(hardware["physical_output_count"])
    ]


def test_topology_draft_reads_the_reconciler_record_never_the_env(monkeypatch) -> None:
    """With no usable record the draft is unknown hardware, whatever the env
    publication says — the env copy can outlive the DAC it names."""
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "hifiberry_dac8x")
    monkeypatch.setenv("JASPER_AUDIO_DAC_CARD", "sndrpihifiberry")

    draft = new_topology_draft()

    assert draft.hardware.device_id == "unknown"
    assert draft.hardware.physical_output_count == 0


def test_empty_topology_draft_is_honest_and_no_audio_allowed() -> None:
    topology = new_topology_draft(hardware=OutputHardware.from_mapping(_base_hardware()))
    payload = topology.to_dict(include_evaluation=True)

    assert payload["status"] == "draft"
    assert payload["hardware"]["physical_output_count"] == 8
    assert payload["safety"]["sound_tests_allowed"] is False
    assert payload["evaluation"]["warnings"][0]["code"] == "no_speaker_groups"


def test_direct_channel_construction_requires_explicit_protection_state() -> None:
    with pytest.raises(TypeError):
        SpeakerChannel(role="tweeter")  # type: ignore[call-arg]

    channel = SpeakerChannel(
        role="tweeter",
        protection_required=True,
        protection_status="required_missing",
    )

    assert channel.protection_required is True
    assert channel.protection_status == "required_missing"


def test_persisted_status_hint_cannot_override_derived_status() -> None:
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "verified",
        "hardware": _base_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    assert topology.status == "draft"
    assert topology.to_dict()["status"] == "draft"


def test_passive_stereo_topology_can_be_valid_before_identity_verified() -> None:
    topology = _topology(
        groups=[
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "position": {"x": -0.5, "y": 1.0},
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "full_range_passive",
                "position": {"x": 0.5, "y": 1.0},
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
        ],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
    )
    evaluation = topology.evaluation()
    payload = topology.to_dict()

    assert evaluation["status"] == "valid"
    assert evaluation["assigned_output_count"] == 2
    assert evaluation["unused_output_count"] == 6
    assert {issue["code"] for issue in evaluation["warnings"]} == {
        "identity_unverified"
    }
    assert "Verify physical output identity" in evaluation["safety"]["next_step"]
    assert payload["speaker_groups"][0]["channels"][0]["human_output_label"] == (
        "DAC output 1"
    )
    assert payload["safety"]["sound_tests_allowed"] is False


def test_channel_identity_report_tracks_assigned_verification_progress() -> None:
    topology = _topology(
        groups=[
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{
                    "role": "full_range",
                    "physical_output_index": 1,
                    "identity_verified": True,
                }],
            },
        ],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
    )

    report = channel_identity_report(topology)

    assert report["kind"] == CHANNEL_IDENTITY_REPORT_KIND
    assert report["status"] == "needs_verification"
    assert report["assigned_channel_count"] == 2
    assert report["verified_channel_count"] == 1
    assert report["unverified_channel_count"] == 1
    assert report["sound_tests_allowed"] is False


def test_tweeter_protection_status_can_be_marked_present() -> None:
    topology = _topology(
        groups=[
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "required_missing",
                    },
                ],
            }
        ],
        routing={"mono_group_id": "mono"},
    )

    blocked = topology.evaluation()
    updated = set_channel_protection_status(
        topology,
        speaker_group_id="mono",
        role="tweeter",
        protection_status="present",
    )
    report = channel_identity_report(updated)

    assert "tweeter_protection_unverified" in {
        issue["code"] for issue in blocked["blockers"]
    }
    tweeter = updated.speaker_groups[0].channels[1]
    assert tweeter.protection_required is True
    assert tweeter.protection_status == "present"
    assert tweeter.startup_muted is True
    assert "tweeter_protection_unverified" not in {
        code
        for target in report["targets"]
        for code in target["sound_test_blockers"]
    }
    targets = {target["id"]: target for target in report["targets"]}
    assert targets["mono:woofer"]["sound_test_blockers"] == ["identity_unverified"]
    assert targets["mono:tweeter"]["sound_test_blockers"] == ["identity_unverified"]


def test_software_guard_request_is_warning_not_topology_blocker() -> None:
    topology = _topology(
        groups=[
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        routing={"mono_group_id": "mono"},
    )

    evaluation = topology.evaluation()
    report = channel_identity_report(topology)
    tweeter = next(target for target in report["targets"] if target["role"] == "tweeter")

    assert evaluation["status"] == "valid"
    assert "tweeter_software_guard_requested" in {
        issue["code"] for issue in evaluation["warnings"]
    }
    assert "tweeter_software_guard_requested" not in tweeter["sound_test_blockers"]
    assert report["sound_tests_allowed"] is False


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

    report = clock_domain_report(topology)

    assert report["kind"] == CLOCK_DOMAIN_REPORT_KIND
    assert report["status"] == "single_device_clock"
    assert report["clock_domain_count"] == 1
    assert report["coherent_physical_output_count"] == 8
    assert report["multi_device_aggregate_supported"] is False
    assert report["sound_tests_allowed"] is False
    assert "one coherent multi-output DAC" in report["recommendation"]


def test_clock_domain_report_accepts_measured_dual_apple_composite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_dual_apple_observation(monkeypatch, tmp_path)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology)
    hardware_payload = topology.to_dict()["hardware"]

    assert report["kind"] == CLOCK_DOMAIN_REPORT_KIND
    assert report["status"] == "dual_apple_composite_clock"
    assert report["clock_domain_count"] == 2
    assert report["coherent_physical_output_count"] == 4
    assert report["multi_device_aggregate_supported"] is False
    assert report["composite_clock_supported"] is True
    assert report["issues"] == []
    assert hardware_payload["child_devices"][0]["serial"] == "DWH53530FHL2FN3AC"


def test_clock_domain_report_blocks_wrong_observed_dual_apple_usb_bus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_dual_apple_observation(monkeypatch, tmp_path, same_bus=False)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple",
        "name": "Dual Apple active pair",
        "hardware": _dual_apple_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    report = clock_domain_report(topology)

    assert report["status"] == "dual_apple_composite_clock_blocked"
    assert report["composite_clock_supported"] is False
    assert "dual_apple_usb_topology_mismatch" in {
        issue["code"] for issue in report["issues"]
    }


def test_dual_apple_hardware_requires_exact_four_physical_outputs() -> None:
    hardware = _dual_apple_hardware()
    hardware["physical_output_count"] = 5
    hardware["outputs"] = [
        {"index": index, "human_label": f"Output {index + 1}"}
        for index in range(5)
    ]

    with pytest.raises(ValueError, match="exactly 4 physical outputs"):
        OutputTopology.from_mapping({
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "dual_apple",
            "name": "Dual Apple active pair",
            "hardware": hardware,
            "speaker_groups": [],
            "routing": {},
        })


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

    report = clock_domain_report(topology)

    assert report["status"] == "dual_apple_composite_clock_blocked"
    assert "dual_apple_child_serials_not_unique" in {
        issue["code"] for issue in report["issues"]
    }


def _verified_channel(role: str, index: int) -> dict:
    """A channel with every non-cross-child gate already satisfied.

    Lets the cross-child tests below assert on that ONE verdict without the
    identity/protection warnings and blockers standing in for it.
    """

    channel = {
        "role": role,
        "physical_output_index": index,
        "identity_verified": True,
        "startup_muted": True,
    }
    if role == "tweeter":
        channel["protection_required"] = True
        channel["protection_status"] = "present"
    return channel


def _two_way_group(group_id: str, kind: str, label: str, woofer: int, tweeter: int) -> dict:
    return {
        "id": group_id,
        "label": label,
        "kind": kind,
        "mode": "active_2_way",
        "channels": [
            _verified_channel("woofer", woofer),
            _verified_channel("tweeter", tweeter),
        ],
    }


def test_cross_child_speaker_group_is_named_and_disclosed_not_blocked() -> None:
    """A crossover straddling two child DACs is disclosed, never refused.

    Fidelity concern, not hearing safety: both dongles drive, so per the
    never-nanny ruling the topology still reaches ``verified`` and the save is
    not refused. The verdict names the group and the children as DATA so a
    caller does not have to parse the message.
    """

    topology = _topology(
        hardware=_dual_apple_hardware(),
        # Woofer on the left dongle (outputs 1-2), tweeter on the right one
        # (outputs 3-4): one speaker, two uncorrected clocks.
        groups=[_two_way_group("mono", "mono", "Mono", woofer=0, tweeter=2)],
        routing={"mono_group_id": "mono"},
    )

    evaluation = topology.evaluation()
    verdicts = [
        issue for issue in evaluation["warnings"]
        if issue["code"] == output_topology_mod.CROSS_CHILD_GROUP_CODE
    ]

    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict["severity"] == "warning"
    assert verdict["group_id"] == "mono"
    assert verdict["group_label"] == "Mono"
    assert verdict["child_ids"] == ["left_dac", "right_dac"]
    assert "one DAC" in verdict["message"]
    # Disclose + recommend, never block.
    assert evaluation["blockers"] == []
    assert evaluation["status"] == "verified"
    # The standalone reader returns the same verdicts the evaluation carries,
    # so a later consumer never re-derives the child boundary.
    assert output_topology_mod.cross_child_group_verdicts(topology) == verdicts


def test_one_child_dac_per_speaker_has_no_cross_child_verdict() -> None:
    """The supported dual-Apple shape: each speaker's crossover inside one DAC."""

    topology = _topology(
        hardware=_dual_apple_hardware(),
        groups=[
            _two_way_group("left", "left", "Left", woofer=0, tweeter=1),
            _two_way_group("right", "right", "Right", woofer=2, tweeter=3),
        ],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
    )

    evaluation = topology.evaluation()

    assert output_topology_mod.cross_child_group_verdicts(topology) == []
    assert output_topology_mod.CROSS_CHILD_GROUP_CODE not in {
        issue["code"] for issue in evaluation["warnings"]
    }
    assert evaluation["status"] == "verified"


def test_subwoofer_group_inside_one_child_has_no_cross_child_verdict() -> None:
    """A sub owning one lane of one child is not a cross-child crossover."""

    topology = _topology(
        hardware=_dual_apple_hardware(),
        groups=[
            _two_way_group("left", "left", "Left", woofer=0, tweeter=1),
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [_verified_channel("subwoofer", 2)],
            },
        ],
        routing={"main_left_group_id": "left", "subwoofer_group_ids": ["sub"]},
    )

    assert output_topology_mod.cross_child_group_verdicts(topology) == []
    assert output_topology_mod.CROSS_CHILD_GROUP_CODE not in {
        issue["code"] for issue in topology.evaluation()["warnings"]
    }


def test_single_child_hardware_never_reports_a_cross_child_verdict() -> None:
    """No child devices, or only one, means there is no boundary to cross.

    A plain DAC8x lists no children at all; the one-child case pins the
    boundary of the predicate so a future single-child composite cannot start
    reporting a split against itself.
    """

    dac8x = _topology(
        groups=[_two_way_group("mono", "mono", "Mono", woofer=0, tweeter=4)],
        routing={"mono_group_id": "mono"},
    )
    assert dac8x.hardware.child_devices == ()
    assert output_topology_mod.cross_child_group_verdicts(dac8x) == []
    assert dac8x.evaluation()["status"] == "verified"

    single_child_hardware = _base_hardware()
    single_child_hardware["child_devices"] = [
        {
            "child_id": "only_dac",
            "device_id": HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
            "device_label": "HiFiBerry DAC8x",
            "physical_output_indexes": [0, 1, 2, 3, 4, 5, 6, 7],
        }
    ]
    single_child = _topology(
        hardware=single_child_hardware,
        groups=[_two_way_group("mono", "mono", "Mono", woofer=0, tweeter=4)],
        routing={"mono_group_id": "mono"},
    )
    assert len(single_child.hardware.child_devices) == 1
    assert output_topology_mod.cross_child_group_verdicts(single_child) == []


def test_clock_domain_report_flags_unknown_output_clocking() -> None:
    topology = new_topology_draft(
        hardware=OutputHardware.from_mapping({
            "device_id": "mystery_usb_audio",
            "device_label": "Mystery USB audio",
            "physical_output_count": 2,
            "card_id": "Mystery",
        })
    )

    report = clock_domain_report(topology)

    assert report["status"] == "unknown_device_clock"
    assert report["coherent_physical_output_count"] == 0
    assert report["issues"][0]["code"] == "unknown_clock_domain"


def test_set_channel_identity_verified_updates_one_channel_only() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 0},
                {
                    "role": "tweeter",
                    "physical_output_index": 1,
                    "protection_required": True,
                    "protection_status": "required_missing",
                },
            ],
        }
    ])

    updated = set_channel_identity_verified(
        topology,
        speaker_group_id="left",
        role="woofer",
        identity_verified=True,
    )
    group = updated.to_dict()["speaker_groups"][0]

    assert group["channels"][0]["identity_verified"] is True
    assert group["channels"][1]["identity_verified"] is False
    assert updated.evaluation()["status"] == "blocked"
    assert "tweeter_protection_unverified" in {
        issue["code"] for issue in updated.evaluation()["blockers"]
    }


def test_posted_human_output_label_is_rederived_from_hardware() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [
                {
                    "role": "full_range",
                    "physical_output_index": 0,
                    "human_output_label": "DAC output 8",
                }
            ],
        }
    ])

    channel = topology.to_dict()["speaker_groups"][0]["channels"][0]

    assert channel["physical_output_index"] == 0
    assert channel["human_output_label"] == "DAC output 1"


def test_active_two_way_topology_requires_tweeter_protection() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left active speaker",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": 0,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "physical_output_index": 1,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "unknown",
                },
            ],
        }
    ])

    evaluation = topology.evaluation()

    assert evaluation["status"] == "blocked"
    assert evaluation["safety"]["requires_tweeter_protection"] is True
    assert "tweeter_protection_unverified" in {
        issue["code"] for issue in evaluation["blockers"]
    }


def test_active_two_way_topology_can_be_verified_when_all_guards_pass() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left active speaker",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": 0,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "physical_output_index": 1,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "present",
                },
            ],
        }
    ])

    evaluation = topology.evaluation()

    assert evaluation["status"] == "verified"
    assert evaluation["blockers"] == []
    assert evaluation["warnings"] == []
    assert evaluation["safety"]["sound_tests_allowed"] is False
    assert "separate safe session" in evaluation["safety"]["next_step"]


def test_stereo_plus_subwoofer_topology_tracks_sub_routes() -> None:
    topology = _topology(
        groups=[
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [{"role": "subwoofer", "physical_output_index": 4}],
            },
        ],
        routing={
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    )

    payload = topology.to_dict(include_evaluation=True)

    assert payload["routing"]["subwoofer_group_ids"] == ["sub"]
    assert payload["evaluation"]["assigned_output_count"] == 3
    assert payload["evaluation"]["unused_output_count"] == 5


def test_duplicate_physical_output_is_blocked_not_silently_reused() -> None:
    topology = _topology(groups=[
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
        {
            "id": "right",
            "label": "Right speaker",
            "kind": "right",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
    ])

    evaluation = topology.evaluation()

    assert evaluation["status"] == "blocked"
    assert "duplicate_physical_output" in {
        issue["code"] for issue in evaluation["blockers"]
    }


def test_save_and_load_output_topology_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    save_output_topology(topology, path)
    loaded = load_output_topology(path)

    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == (
        OUTPUT_TOPOLOGY_KIND
    )
    assert loaded.topology_id == "living_room"
    assert loaded.to_dict()["speaker_groups"][0]["channels"][0][
        "human_output_label"
    ] == "DAC output 3"


# --- gap 1: pure-data pairing intent ------------------------------------------
#
# Slice 1 invariants 2 and 7 (topology layer): the pairing field round-trips and
# defaults to solo (absent == solo, non-breaking), and it records design intent
# ONLY — it must not change any safety/validity behavior the topology drives.


def test_pairing_intent_defaults_to_solo_when_absent() -> None:
    # inv 2: topology JSON written before gap 1 (no pairing_intent key) loads as
    # "solo", so the new field cannot break an existing speaker's saved topology.
    raw = {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": _base_hardware(),
        "speaker_groups": [],
        "routing": {},
    }
    assert "pairing_intent" not in raw
    assert OutputTopology.from_mapping(raw).pairing_intent == DEFAULT_PAIRING_INTENT
    assert DEFAULT_PAIRING_INTENT == "solo"


@pytest.mark.parametrize("intent", sorted(PAIRING_INTENTS))
def test_pairing_intent_round_trips_through_to_dict(intent: str) -> None:
    # inv 2: every supported value survives to_dict -> from_mapping. The field
    # serializes unconditionally (always-emit), so the key is present even for
    # the "solo" default.
    topology = replace(
        _topology(groups=[
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 2}],
            }
        ]),
        pairing_intent=intent,
    )
    payload = topology.to_dict()
    assert payload["pairing_intent"] == intent
    assert OutputTopology.from_mapping(payload).pairing_intent == intent


def test_pairing_intent_rejects_unknown_value() -> None:
    raw = {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": _base_hardware(),
        "speaker_groups": [],
        "routing": {},
        "pairing_intent": "bogus",
    }
    with pytest.raises(OutputTopologyError):
        OutputTopology.from_mapping(raw)


@pytest.mark.parametrize("intent", sorted(PAIRING_INTENTS))
def test_pairing_intent_is_inert_for_evaluation(intent: str) -> None:
    # inv 7 (topology layer): pairing intent is pure design-intent data. It must
    # not move any safety/validity verdict, so a solo speaker's evaluation is
    # byte-identical regardless of pairing intent (no behavior yet).
    groups = [
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
        {
            "id": "right",
            "label": "Right speaker",
            "kind": "right",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 1}],
        },
    ]
    routing = {"main_left_group_id": "left", "main_right_group_id": "right"}
    solo = _topology(groups=groups, routing=routing)
    variant = replace(solo, pairing_intent=intent)
    assert variant.evaluation() == solo.evaluation()
    assert variant.to_dict()["safety"] == solo.to_dict()["safety"]


def test_save_output_topology_cleans_temp_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    def fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("jasper.atomic_io.os.replace", fail_replace)

    with pytest.raises(OSError):
        save_output_topology(topology, path)

    assert not path.exists()
    assert list(tmp_path.glob(".output_topology.json.*.tmp")) == []


def test_save_output_topology_publishes_group_readable_0640(
    tmp_path: Path,
) -> None:
    # /var/lib/jasper is group jasper but not setgid, so the non-root
    # jasper-group management daemons read this file by its group bits.
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    save_output_topology(topology, path)

    assert path.stat().st_mode & 0o777 == 0o640
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == OUTPUT_TOPOLOGY_KIND


def test_load_output_topology_fails_soft_to_detected_draft(tmp_path: Path) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    loaded = load_output_topology(path)

    assert loaded.status == "draft"
    assert loaded.speaker_groups == ()


# --------------------------------------------------------------------------- #
# Fail-soft load WARN is rate-limited (#2140).
#
# `load_output_topology` is called every 60 s by jasper-control's audio_health
# route sampler, so an unguarded WARN is ~1,440 identical journal lines/day in
# an already-degraded state. What is pinned here is the promise: transitions
# and due reminders are logged, steady repeats are not.
#
# Each test uses its own `tmp_path`, which is also the rate-limiter's key, so
# these need no reset of the module-level state.
# --------------------------------------------------------------------------- #


def _load_failure_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines()
        if "event=output_topology.load_failed" in line
    ]


def test_load_output_topology_warns_once_for_a_repeated_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="jasper.output_topology"):
        for _ in range(5):
            assert load_output_topology(path).status == "draft"

    lines = _load_failure_lines(caplog.text)
    assert len(lines) == 1, caplog.text
    assert "repeat_suppression_sec=3600" in lines[0]


def test_load_output_topology_rewarns_when_the_failure_changes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="jasper.output_topology"):
        load_output_topology(path)
        load_output_topology(path)
        # A different corruption is a different failure: it must not inherit
        # the suppression window of the one before it.
        path.write_text(json.dumps({"kind": "not_a_topology"}), encoding="utf-8")
        load_output_topology(path)

    assert len(_load_failure_lines(caplog.text)) == 2, caplog.text


def test_load_output_topology_rewarns_after_the_reminder_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")
    clock = [1_000.0]
    monkeypatch.setattr(output_topology_mod, "_now", lambda: clock[0])

    with caplog.at_level(logging.WARNING, logger="jasper.output_topology"):
        load_output_topology(path)
        clock[0] += output_topology_mod.LOAD_FAILURE_REMINDER_SEC - 1.0
        load_output_topology(path)
        assert len(_load_failure_lines(caplog.text)) == 1, caplog.text
        clock[0] += 1.0
        load_output_topology(path)

    assert len(_load_failure_lines(caplog.text)) == 2, caplog.text


def test_load_output_topology_logs_recovery_once_after_a_logged_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="jasper.output_topology"):
        load_output_topology(path)
        save_output_topology(new_topology_draft(), path)
        load_output_topology(path)
        load_output_topology(path)

    assert caplog.text.count("event=output_topology.load_recovered") == 1


def test_load_output_topology_is_silent_when_it_never_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    save_output_topology(new_topology_draft(), path)

    with caplog.at_level(logging.INFO, logger="jasper.output_topology"):
        load_output_topology(path)
        load_output_topology(path)

    assert "event=output_topology.load_" not in caplog.text


def test_load_failure_state_is_bounded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A caller passing many paths cannot grow the limiter without bound."""
    for index in range(output_topology_mod._LOAD_FAILURE_STATE_MAX + 4):
        path = tmp_path / f"topology-{index}.json"
        path.write_text("{not json", encoding="utf-8")
        load_output_topology(path)

    assert (
        output_topology_mod._load_failures.tracked()
        <= output_topology_mod._LOAD_FAILURE_STATE_MAX
    )


def test_load_output_topology_strict_rejects_corrupt_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(OutputTopologyError, match="not valid JSON"):
        load_output_topology_strict(path)


def test_load_output_topology_strict_rejects_non_utf8_bytes(
    tmp_path: Path,
) -> None:
    """SD-card bit rot fails CLOSED, like every other unreadable shape.

    `read_text(encoding="utf-8")` raises UnicodeDecodeError BEFORE json.loads
    runs, and UnicodeDecodeError is not an OSError — so without the loader's own
    clause it escapes as a bare ValueError past every caller's fail-closed
    handling. In the grouping reconciler that degrades route_mode to "unknown",
    which never blocks: a bonded box with corrupted topology bytes would be
    ADMITTED where an unreadable one is refused.
    """
    path = tmp_path / "output_topology.json"
    path.write_bytes(b'{"kind": "\xff\xfe not utf-8"}')

    with pytest.raises(OutputTopologyError, match="could not read"):
        load_output_topology_strict(path)


@pytest.mark.parametrize("device_id", [5, True, 1.5, ["hifiberry_dac8x"], {"id": 1}])
def test_load_output_topology_strict_rejects_a_non_string_device_id(
    tmp_path: Path, device_id: object
) -> None:
    """The strict loader's contract is that EVERY malformed artifact leaves as
    its one typed error.

    A truthy non-string `device_id` reached `normalize_output_device_id`'s
    `.strip()` and raised `AttributeError` — which is not a `ValueError`, so it
    escaped the loader's own handling and every caller written to that contract.
    The topology is otherwise valid, so nothing earlier can refuse it.
    """
    path = tmp_path / "output_topology.json"
    path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "living_room",
            "name": "Living room",
            "status": "draft",
            "hardware": {**_base_hardware(), "device_id": device_id},
            "speaker_groups": [],
            "routing": {},
        }),
        encoding="utf-8",
    )

    with pytest.raises(OutputTopologyError):
        load_output_topology_strict(path)


def test_load_output_topology_strict_allows_missing_as_unconfigured(
    tmp_path: Path,
) -> None:
    loaded = load_output_topology_strict(tmp_path / "missing.json")

    assert loaded.status == "draft"
    assert loaded.speaker_groups == ()


def test_topology_snapshot_revision_hashes_the_exact_loaded_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "output_topology.json"
    topology = new_topology_draft(name="Exact bytes")
    save_output_topology(topology, path)
    data = path.read_bytes()

    snapshot = load_output_topology_snapshot(path)

    assert snapshot.topology == topology
    assert snapshot.revision == "sha256:" + hashlib.sha256(data).hexdigest()


def test_mutation_save_returns_revision_without_post_write_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "output_topology.json"
    topology = new_topology_draft(name="Published once")

    with output_topology_mod.output_topology_mutation(path) as mutation:
        monkeypatch.setattr(
            output_topology_mod,
            "load_output_topology_snapshot",
            lambda *_args, **_kwargs: pytest.fail(
                "save must not read after publication"
            ),
        )
        revision = mutation.save(topology)

    data = path.read_bytes()
    assert revision == "sha256:" + hashlib.sha256(data).hexdigest()
    assert json.loads(data)["name"] == "Published once"


@pytest.mark.parametrize(
    ("recorded", "claimed_group", "claimed_index", "claimed", "stored"),
    [
        (False, "mono", 0, True, False),
        (True, "mono", 0, True, True),
        (True, "mono", 0, False, False),
        # The audition names one lane: neither re-pinning it to another output
        # nor renaming its group carries the proof over.
        (True, "mono", 1, True, False),
        (True, "renamed", 0, True, False),
    ],
)
def test_saved_channel_identity_is_server_owned(
    tmp_path: Path,
    recorded: bool,
    claimed_group: str,
    claimed_index: int,
    claimed: bool,
    stored: bool,
) -> None:
    """A save payload may clear a lane's audition, never claim one."""

    path = tmp_path / "output_topology.json"
    save_output_topology(_topology(groups=[_passive_main("mono", "mono", 0)]), path)
    if recorded:
        with output_topology_mod.output_topology_mutation(path) as mutation:
            mutation.save(
                set_channel_identity_verified(
                    mutation.snapshot().topology,
                    speaker_group_id="mono",
                    role="full_range",
                    identity_verified=True,
                )
            )

    with output_topology_mod.output_topology_mutation(path) as mutation:
        mutation.save(
            _topology(
                groups=[
                    _passive_main(
                        claimed_group,
                        "mono",
                        claimed_index,
                        identity_verified=claimed,
                    )
                ]
            )
        )

    assert [
        channel.identity_verified
        for group in load_output_topology_snapshot(path).topology.speaker_groups
        for channel in group.channels
    ] == [stored]


@pytest.mark.parametrize(
    ("claimed_device_id", "stored"),
    [
        ("hifiberry_dac8x", True),
        # The audition named the OLD device on this lane: a same-key save
        # under a DIFFERENT declared device is a different device wearing the
        # old key, not the same lane resaved.
        ("other_dac", False),
    ],
)
def test_saved_channel_identity_dies_with_a_changed_hardware_device(
    tmp_path: Path,
    claimed_device_id: str,
    stored: bool,
) -> None:
    """#3109: a same-key save carries the audition forward only onto the same device."""

    path = tmp_path / "output_topology.json"
    save_output_topology(_topology(groups=[_passive_main("mono", "mono", 0)]), path)
    with output_topology_mod.output_topology_mutation(path) as mutation:
        mutation.save(
            set_channel_identity_verified(
                mutation.snapshot().topology,
                speaker_group_id="mono",
                role="full_range",
                identity_verified=True,
            )
        )

    with output_topology_mod.output_topology_mutation(path) as mutation:
        mutation.save(
            _topology(
                groups=[_passive_main("mono", "mono", 0, identity_verified=True)],
                hardware={
                    "device_id": claimed_device_id,
                    "device_label": "Claimed hardware",
                    "physical_output_count": 8,
                },
            )
        )

    assert [
        channel.identity_verified
        for group in load_output_topology_snapshot(path).topology.speaker_groups
        for channel in group.channels
    ] == [stored]


def test_channel_identity_authorization_cannot_arrive_in_a_payload() -> None:
    """Only the server can mark a lane's audition, so no payload may say it."""

    topology = _topology(groups=[{
        "id": "mono",
        "label": "Mono",
        "kind": "mono",
        "mode": "full_range_passive",
        "channels": [{
            "role": "full_range",
            "physical_output_index": 0,
            "identity_verified": True,
            "identity_verified_authorized": True,
        }],
    }])
    channel = topology.speaker_groups[0].channels[0]

    assert channel.identity_verified_authorized is False


def test_topology_publication_uses_durable_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture(path, data, **kwargs):
        captured.update(path=path, data=data, kwargs=kwargs)

    monkeypatch.setattr(output_topology_mod, "atomic_write_text", capture)

    save_output_topology(new_topology_draft(), tmp_path / "topology.json")

    assert captured["kwargs"]["durable"] is True


# --------------------------------------------------------------------------- #
# User-settable subwoofer crossover corner (crossover_fc_hz).
# --------------------------------------------------------------------------- #


def _passive_sub_topology_raw(fc: object) -> dict:
    sub_channel: dict = {"role": "subwoofer", "physical_output_index": 2}
    if fc is not None:
        sub_channel["crossover_fc_hz"] = fc
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "L",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "R",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
            {
                "id": "sub",
                "label": "Sub",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [sub_channel],
            },
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    }


def test_sub_crossover_fc_round_trips() -> None:
    topology = OutputTopology.from_mapping(_passive_sub_topology_raw(120.0))
    sub_channel = topology.speaker_groups[2].channels[0]
    assert sub_channel.crossover_fc_hz == 120.0

    again = OutputTopology.from_mapping(topology.to_dict())
    assert again.speaker_groups[2].channels[0].crossover_fc_hz == 120.0


def test_sub_crossover_fc_absent_is_none_and_omitted() -> None:
    # An unset corner stays None and is omitted from to_dict (a subless topology
    # and a sub-with-default corner serialize byte-identically to before).
    topology = OutputTopology.from_mapping(_passive_sub_topology_raw(None))
    sub_channel = topology.speaker_groups[2].channels[0]
    assert sub_channel.crossover_fc_hz is None
    assert "crossover_fc_hz" not in topology.to_dict()["speaker_groups"][2]["channels"][0]


def test_sub_crossover_fc_in_range_is_not_a_blocker() -> None:
    topology = OutputTopology.from_mapping(_passive_sub_topology_raw(120.0))
    codes = {b["code"] for b in topology.evaluation()["blockers"]}
    assert "subwoofer_crossover_out_of_range" not in codes


@pytest.mark.parametrize("fc", [39.0, 201.0, 0.0, -5.0])
def test_sub_crossover_fc_out_of_range_is_loud_blocker(fc: float) -> None:
    topology = OutputTopology.from_mapping(_passive_sub_topology_raw(fc))
    codes = {b["code"] for b in topology.evaluation()["blockers"]}
    assert "subwoofer_crossover_out_of_range" in codes


def test_sub_crossover_bounds_mirror_profile() -> None:
    # output_topology, the active-speaker profile, AND the one shared corner
    # home (jasper.camilla_emit) must all agree — since P5 they are bound to the
    # same constant, not three independent numbers.
    from jasper.active_speaker.profile import (
        SUB_CROSSOVER_HZ_HI as PROFILE_HI,
        SUB_CROSSOVER_HZ_LO as PROFILE_LO,
    )
    from jasper.camilla_emit import (
        BASS_MANAGEMENT_CORNER_HZ_HI as SHARED_HI,
        BASS_MANAGEMENT_CORNER_HZ_LO as SHARED_LO,
    )
    from jasper.output_topology import (
        SUB_CROSSOVER_HZ_HI,
        SUB_CROSSOVER_HZ_LO,
    )

    assert SUB_CROSSOVER_HZ_LO == PROFILE_LO == SHARED_LO == 40.0
    assert SUB_CROSSOVER_HZ_HI == PROFILE_HI == SHARED_HI == 200.0


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
                    "protection_status": "present",
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
    observed identity is rewritten, and only ITS lanes lose identity
    confirmation.
    """

    before = _dual_apple_active_topology()
    observed = _dual_apple_observation(serial_b="NEW-DONGLE-SERIAL")

    plan = composite_serial_repin_plan(before, observed)
    assert plan is not None
    # Exactly one of the two was replaced, and the lanes name WHICH: 2-3 belong
    # to the second child, so the untouched child's lanes stay out of it.
    assert (plan.child_count, plan.replaced_child_count) == (2, 1)
    assert plan.reverify_output_indexes == (2, 3)

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
         channel.protection_status, channel.startup_muted)
        for group in after.speaker_groups
        for channel in group.channels
    ] == [
        (channel.role, channel.driver_style, channel.physical_output_index,
         channel.protection_status, channel.startup_muted)
        for group in before.speaker_groups
        for channel in group.channels
    ]
    # Identity is cleared for the REPLACED child's lanes only.
    assert {
        (group.id, channel.role): channel.identity_verified
        for group in after.speaker_groups
        for channel in group.channels
    } == {
        ("left", "woofer"): True,
        ("left", "tweeter"): True,
        ("right", "woofer"): False,
        ("right", "tweeter"): False,
    }
    assert channel_identity_report(after)["unverified_channel_count"] == 2
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


def test_declared_hardware_mismatch_is_none_when_declared_matches_observed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#2812 B1: an already-declared, already-armed box must not mismatch.

    Proven live: a declared, serial-bound dual-Apple speaker hitting an
    unrelated outputd fault was told to "finish setup" for a setup that
    already happened, because the detector that reads this result checked
    only whether the DETECTED hardware was usable, never whether it already
    matched the DECLARATION. Same fixtures ``composite_serial_repin_plan``
    uses to prove "nothing to re-pin" (the very same two units) — the outer
    conjunct must agree with the inner one that this box needs no action.
    """
    _write_dual_apple_observation(monkeypatch, tmp_path)
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
    ``jasper.control.audio_health._undeclared_hardware_signal``.
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


def test_declared_hardware_mismatch_flags_an_unobserved_dual_apple_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A declared dual-Apple pair with no fresh hardware observation mismatches.

    Deliberately omits ``_write_dual_apple_observation``: ``clock_domain_report``
    re-reads the observed record itself, and with no record at all it raises
    ``dual_apple_observation_missing`` — a blocker this function must also
    treat as "needs attention" even when the caller's own ``observed``
    argument happens to be a matching pair.
    """
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH",
        str(tmp_path / "absent-output-hardware.json"),
    )
    topology = _dual_apple_active_topology()
    observed = _dual_apple_observation()

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
