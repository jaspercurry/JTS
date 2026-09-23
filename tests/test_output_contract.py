# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring-B eligibility of the saved topology's output contract.

The stereo ring is stereo-only: roleful, protected, subwoofer and composite
topologies resolve no Ring-B width, and neither does an unconfigured one. Only
an explicit clean passive mono or stereo layout resolves a width.
"""

from __future__ import annotations

from jasper.active_speaker.output_contract import (
    CONTRACT_SUBWOOFER_PRESENT,
    RING_STEREO_PROGRAM_CHANNELS,
    classify_output_contract,
    ring_channels_for_topology,
    topology_allows_flat_dac_graph,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

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


# --- Ring-B eligibility ------------------------------------------------------


def test_a_mono_topology_with_issues_still_supports_no_ring():
    # Eligibility is gated on a CLEAN contract for mono exactly as for stereo.
    unassigned = _topology(
        [
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range"}],
            }
        ],
        {"mono_group_id": "mono"},
    )

    assert classify_output_contract(unassigned).issues
    assert ring_channels_for_topology(unassigned) is None


def test_active_topologies_do_not_support_ring():
    for layout in ("mono", "stereo"):
        for mode in ("active_2_way", "active_3_way"):
            assert ring_channels_for_topology(_active_topology(layout, mode)) is None


def test_stereo_ring_eligibility_implies_flat_dac_permission():
    """A Ring-B width exists only where a flat program may reach the DAC.

    `ring_channels_for_topology(t) is not None` =>
    `topology_allows_flat_dac_graph(classify(t))`. Neither function states the
    implication, so widening either would break it silently.
    """
    shapes = [
        _topology([]),
        _full_range_stereo(),
        _full_range_mono(),
        _subwoofer_topology(),
        _dual_apple_stereo(),
    ] + [
        _active_topology(layout, mode)
        for layout in ("mono", "stereo")
        for mode in ("active_2_way", "active_3_way")
    ]
    for topology in shapes:
        if ring_channels_for_topology(topology) is not None:
            assert topology_allows_flat_dac_graph(
                classify_output_contract(topology)
            ), topology.topology_id
    # Not vacuous: at least one shape actually satisfies the antecedent.
    assert any(ring_channels_for_topology(t) is not None for t in shapes)


# --- ring_channels_for_topology: the eligibility table -----------------------
#
# Eligibility is asserted as a WIDTH rather than as yes/no, over the WHOLE
# table, not spot-checked on one shape.


def _eligibility_table():
    return [
        ("unconfigured", _topology([]), None),
        ("full_range_stereo", _full_range_stereo(), RING_STEREO_PROGRAM_CHANNELS),
        ("full_range_mono", _full_range_mono(), RING_STEREO_PROGRAM_CHANNELS),
        ("subwoofer", _subwoofer_topology(), None),
        ("composite_dual_apple", _dual_apple_stereo(), None),
        ("active_2_way_stereo", _active_topology("stereo", "active_2_way"), None),
        ("active_3_way_mono", _active_topology("mono", "active_3_way"), None),
    ]


def test_ring_channels_for_topology_answers_the_eligibility_table():
    for label, topology, expected in _eligibility_table():
        assert ring_channels_for_topology(topology) == expected, label


def test_roleful_topology_has_no_ring_width_despite_a_declared_active_lane():
    """An active-crossover box is ineligible, not "eligible at N channels".

    Its DAC may well declare an active outputd lane, but the ring transport for
    post-crossover per-driver channels does not exist: both shipped conf.d PCMs
    are the full-range stereo program. Reporting a width here would arm a box
    onto a ring nothing builds.
    """
    active = _active_topology("stereo", "active_2_way")
    assert ring_channels_for_topology(active) is None


# --- the SHIPPED-DEFAULT box ---------------------------------------------------
# jts.local is a plain solo stereo single USB DAC (apple_usb_c_dongle). Its saved
# output_topology.json out of the box has speaker_groups=[] and hardware.outputs
# in state="unused" (that IS the shipped-default shape). These pin the real
# topology-artifact shapes: the shipped default resolves no ring until a layout
# is saved, and a box carrying STALE subwoofer artifacts is (correctly)
# ineligible. dac8x-roleful (jts3) and composite stay ineligible (covered above
# via _active_topology / _subwoofer_topology / dual).


def _apple_dongle_shipped_default() -> OutputTopology:
    """The out-of-box shipped-default Apple USB-C dongle topology.

    Single stereo USB DAC, NO speaker_groups, hardware.outputs explicitly in
    state="unused" — exactly what ``new_topology_draft`` writes on a fresh box
    (and jts.local's real current hardware). state="unused" is orthogonal to the
    contract (the classifier reads speaker_groups + routing, never output state).

    CRITICAL: this fixture carries the single ``child_devices`` entry that
    ``topology_hardware_from_state`` (via ``new_topology_draft`` ->
    ``classify_output_cards``) ALWAYS records for a detected single DAC — the
    ``card_id="A"`` child with its serial identity. Earlier this fixture omitted
    it, which is exactly why the DEFECT-2 refusal escaped the test suite: the real
    jts.local artifact has ``child_devices=[{card_id: A, ...}]`` and the bare
    ``if child_devices:`` predicate refused it while this childless fixture passed.
    A single child MUST be ring-eligible (it is the one coherent L/R sink).
    """
    return OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "default",
            "name": "Speaker outputs",
            "status": "draft",
            "hardware": {
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_count": 2,
                "card_id": "A",
                "child_devices": [
                    {
                        "child_id": "apple_dac_1",
                        "device_id": "apple_usb_c_dongle",
                        "device_label": "Apple USB-C audio adapter",
                        "physical_output_indexes": [0, 1],
                        "serial": "DWH53530FLL2FN3A3",
                        "card_id": "A",
                    },
                ],
                "outputs": [
                    {"index": 0, "human_label": "Left", "terminal_label": "1",
                     "state": "unused"},
                    {"index": 1, "human_label": "Right", "terminal_label": "2",
                     "state": "unused"},
                ],
            },
            "speaker_groups": [],
            "routing": {},
        }
    )


def _apple_dongle_with_stale_subwoofer() -> OutputTopology:
    """A plain Apple dongle box carrying a STALE subwoofer speaker_group.

    jts.local ran the 2026-06 subwoofer campaign, so its saved topology may still
    declare a subwoofer role even though the real current hardware is a plain
    stereo dongle. The classifier HONESTLY reports subwoofer_present -> roleful ->
    ineligible: a stereo ring cannot drive a sub. The fix is the operator
    topology-reset, not weakening the predicate.

    Carries the single ``child_devices`` entry a detected dongle always records,
    so the ineligibility here is proven to be driven by the subwoofer ROLE
    (``requires_roleful_graph``), not by the child-device presence — the roleful
    check runs first and returns before the composite (>=2 children) check.
    """
    return OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "default",
            "name": "Speaker outputs",
            "status": "draft",
            "hardware": {
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_count": 2,
                "card_id": "A",
                "child_devices": [
                    {
                        "child_id": "apple_dac_1",
                        "device_id": "apple_usb_c_dongle",
                        "device_label": "Apple USB-C audio adapter",
                        "physical_output_indexes": [0, 1],
                        "serial": "DWH53530FLL2FN3A3",
                        "card_id": "A",
                    },
                ],
            },
            "speaker_groups": [
                {
                    "id": "sub",
                    "label": "Subwoofer",
                    "kind": "subwoofer",
                    "mode": "subwoofer",
                    "channels": [{"role": "subwoofer", "physical_output_index": 0}],
                }
            ],
            "routing": {"subwoofer_group_ids": ["sub"]},
        }
    )


def test_shipped_default_apple_dongle_is_parked_until_layout_is_saved():
    topo = _apple_dongle_shipped_default()
    assert all(o.state == "unused" for o in topo.hardware.outputs)
    assert topo.speaker_groups == ()
    # The fixture MUST carry the single child a detected dongle records — this is
    # the field whose omission masked the DEFECT-2 refusal. Pin it so the fixture
    # can never silently drift back to childless (which would make the assertion
    # below pass for the wrong reason).
    assert len(topo.hardware.child_devices) == 1
    assert ring_channels_for_topology(topo) is None


def test_empty_topology_is_ineligible_regardless_of_child_count():
    single = _apple_dongle_shipped_default()
    assert len(single.hardware.child_devices) == 1
    assert ring_channels_for_topology(single) is None

    dual = _dual_apple_stereo()
    assert len(dual.hardware.child_devices) == 2
    assert ring_channels_for_topology(dual) is None


def test_shipped_default_output_state_does_not_authorize_ring():
    raw = _apple_dongle_shipped_default().to_dict()
    for out in raw["hardware"]["outputs"]:
        out["state"] = "assigned"
    topo = OutputTopology.from_mapping(raw)
    assert ring_channels_for_topology(topo) is None


def test_stale_subwoofer_on_dongle_is_correctly_ineligible():
    # A stale subwoofer group makes the box (correctly) ineligible — a stereo ring
    # cannot carry a sub role. This is honest classification, not a bug.
    topo = _apple_dongle_with_stale_subwoofer()
    contract = classify_output_contract(topo)
    assert contract.classification == CONTRACT_SUBWOOFER_PRESENT
    assert contract.requires_roleful_graph is True
    assert ring_channels_for_topology(topo) is None
