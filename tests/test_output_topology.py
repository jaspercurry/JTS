# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace

import pytest

from jasper.active_speaker.profile import (
    SUB_CROSSOVER_HZ_HI as PROFILE_HI,
    SUB_CROSSOVER_HZ_LO as PROFILE_LO,
)
from jasper.camilla_emit import (
    BASS_MANAGEMENT_CORNER_HZ_HI as SHARED_HI,
    BASS_MANAGEMENT_CORNER_HZ_LO as SHARED_LO,
)

from jasper import output_topology as output_topology_mod
from jasper.audio_hardware import dac
from jasper.output_hardware import (
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputHardwareState,
)
from jasper.output_topology import (
    DEFAULT_PAIRING_INTENT,
    HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    PAIRING_INTENTS,
    SUB_CROSSOVER_HZ_HI,
    SUB_CROSSOVER_HZ_LO,
    OutputHardware,
    OutputTopology,
    OutputTopologyError,
    SpeakerChannel,
    topology_config_fingerprint,
    topology_hardware_from_state,
    topology_is_passive_mains,
    topology_is_subless_passive_mains,
    unknown_output_hardware,
)
from jasper.output_topology_store import new_topology_draft
from tests.output_topology_fixtures import (
    _base_hardware,
    _dual_apple_hardware,
    _dual_apple_observation,
    _fingerprint_topology,
    _passive_main,
    _passive_sub_topology_raw,
    _topology,
)


def _sub_group(index: int) -> dict:
    return {
        "id": "sub",
        "label": "Subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        "channels": [{"role": "subwoofer", "physical_output_index": index}],
    }


@pytest.mark.parametrize("count", [float("inf"), float("-inf"), float("nan")])
def test_topology_rejects_nonfinite_physical_output_count(count: float) -> None:
    raw = new_topology_draft().to_dict(include_evaluation=False)
    raw["hardware"]["physical_output_count"] = count
    with pytest.raises(OutputTopologyError) as caught:
        OutputTopology.from_mapping(raw)
    assert caught.value.code == "field_not_integer"


@pytest.mark.parametrize("version", [[], {}, [1], {"version": 1}])
def test_topology_rejects_container_schema_version(version: object) -> None:
    raw = new_topology_draft().to_dict(include_evaluation=False)
    raw["artifact_schema_version"] = version
    with pytest.raises(OutputTopologyError):
        OutputTopology.from_mapping(raw)


@pytest.mark.parametrize("routing", [None, [], ["mono"], "", "mono", 0, False])
def test_topology_rejects_non_mapping_routing(routing: object) -> None:
    raw = new_topology_draft().to_dict(include_evaluation=False)
    raw["routing"] = routing
    with pytest.raises(OutputTopologyError) as caught:
        OutputTopology.from_mapping(raw)
    assert caught.value.code == "field_not_object"


@pytest.mark.parametrize("omitted", [True, False])
def test_topology_accepts_omitted_or_empty_routing(omitted: bool) -> None:
    draft = new_topology_draft()
    raw = draft.to_dict(include_evaluation=False)
    raw.pop("routing")
    if not omitted:
        raw["routing"] = {}
    assert OutputTopology.from_mapping(raw).routing == draft.routing


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


def test_direct_channel_construction_requires_explicit_protection_state() -> None:
    with pytest.raises(TypeError):
        SpeakerChannel(role="tweeter")  # type: ignore[call-arg]

    channel = SpeakerChannel(
        role="tweeter",
        protection_required=True,
    )

    assert channel.protection_required is True


def test_persisted_status_hint_cannot_override_derived_status() -> None:
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "valid",
        "hardware": _base_hardware(),
        "speaker_groups": [],
        "routing": {},
    })

    assert topology.status == "draft"
    assert topology.to_dict()["status"] == "draft"


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
    assert evaluation["status"] == "valid"
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
    assert evaluation["status"] == "valid"


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
    assert dac8x.evaluation()["status"] == "valid"

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


# --------------------------------------------------------------------------- #
# User-settable subwoofer crossover corner (crossover_fc_hz).
# --------------------------------------------------------------------------- #


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

    assert SUB_CROSSOVER_HZ_LO == PROFILE_LO == SHARED_LO == 40.0
    assert SUB_CROSSOVER_HZ_HI == PROFILE_HI == SHARED_HI == 200.0


def test_a_reworded_warning_cannot_move_the_config_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #2500 contract: the fingerprint reads no evaluation output.

    `to_dict()` embeds the evaluation's `status` and `safety` on every caller,
    so hashing it made a warning's PROSE — which determines no filter — rotate
    every persisted anchor. This asserts the drift source moves and the
    fingerprint does not.
    """
    topology = _fingerprint_topology()
    before = topology_config_fingerprint(topology)
    real = output_topology_mod.evaluate_output_topology

    def _reworded(target: OutputTopology) -> dict:
        evaluation = real(target)
        safety = dict(evaluation["safety"])
        safety["warnings"] = [
            *safety.get("warnings", []),
            {"severity": "warning", "code": "invented", "message": "new prose"},
        ]
        return {**evaluation, "status": "invented", "safety": safety}

    monkeypatch.setattr(output_topology_mod, "evaluate_output_topology", _reworded)

    assert topology.to_dict()["safety"]["warnings"][-1]["code"] == "invented"
    assert topology_config_fingerprint(topology) == before


@pytest.mark.parametrize(
    ("mutate", "moves"),
    [
        # Identity and label determine no emitted filter; every anchor site
        # compares `topology_id` on its own.
        (lambda t: replace(t, topology_id="other"), False),
        (lambda t: replace(t, name="Kitchen"), False),
        # Everything that reaches the DSP config does move it.
        (lambda t: replace(t, routing=replace(t.routing, mono_group_id="left")), True),
        (
            lambda t: replace(
                t, hardware=replace(t.hardware, physical_output_count=4)
            ),
            True,
        ),
        (lambda t: replace(t, speaker_groups=t.speaker_groups[:1]), True),
    ],
)
def test_the_config_fingerprint_moves_for_config_and_nothing_else(
    mutate, moves: bool,
) -> None:
    topology = _fingerprint_topology()

    changed = topology_config_fingerprint(mutate(topology))

    assert (changed != topology_config_fingerprint(topology)) is moves


@pytest.mark.parametrize("profile", [*dac.all_profiles(), None])
def test_output_labels_use_registry_with_unknown_fallback(profile) -> None:
    profile_id = profile.id if profile else "unregistered_dac"
    state = OutputHardwareState.from_mapping({
        "profile_id": profile_id,
        "child_devices": [{"card_id": "DAC", "device_id": profile_id, "label": "Card"}],
    })

    assert state.profile_label == (profile.label if profile else profile_id)
    hardware = topology_hardware_from_state(state)
    assert hardware["child_devices"][0]["device_label"] == (
        profile.label if profile else "Card"
    )


def test_dual_apple_projection_keeps_the_four_declared_lanes() -> None:
    hardware = topology_hardware_from_state(_dual_apple_observation())

    assert hardware["device_id"] == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert hardware["child_devices"][0]["physical_output_indexes"] == [0, 1]
    assert hardware["child_devices"][1]["physical_output_indexes"] == [2, 3]
