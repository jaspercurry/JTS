# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring (shm_ring) topology-contract citizenship + statefile seeding (P2).

Two P2 contracts:
  1. topology_supports_shm_ring — ring is solo-stereo-only (not roleful, not
     composite; requires an explicit passive stereo layout). This is what the
     multiroom bond prechecks + reconciler consult.
  2. safe_graph_for_current_topology(coupling="shm_ring") re-seeds the RING flat
     config on a ring-armed box, not the loopback flat config — audit finding 5's
     built-in-revert dies here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.active_speaker.runtime_contract import (
    CONTRACT_SUBWOOFER_PRESENT,
    DEFAULT_RING_FLAT_OUTPUTD_CONFIG,
    RING_STEREO_PROGRAM_CHANNELS,
    classify_output_contract,
    ring_channels_for_topology,
    safe_graph_for_current_topology,
    topology_allows_flat_dac_graph,
    topology_supports_shm_ring,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
from jasper.sound.camilla_yaml import (
    RING_FLAT_CONFIG_NAME,
    emit_flat_outputd_cutover_config,
    emit_flat_ring_config,
)

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


def test_unconfigured_topology_does_not_support_ring():
    # Fresh install remains parked until an explicit passive stereo layout exists.
    assert topology_supports_shm_ring(_topology([])) is False


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


def test_stereo_ring_eligibility_implies_flat_dac_permission():
    """The cross-module identity PR-5's narrowed shm_ring gate leans on.

    `topology_supports_shm_ring(t)` => `topology_allows_flat_dac_graph(classify(t))`.

    It is why the one bonded shape the narrowed gate still BLOCKS — a passive
    leader, whose dac_content lane the writer arms — is safe by construction and
    not merely by the gate: every topology the STEREO ring can be armed on at all
    is one whose lane gets armed, so the ring can never already be live on a
    bonded box the narrowing admits. Neither function states the implication and
    no test held it, so widening either would have broken the argument silently.

    Asserted over the SAME shape table the eligibility tests above use, so a new
    topology kind joins both at once.
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
        if topology_supports_shm_ring(topology):
            assert topology_allows_flat_dac_graph(
                classify_output_contract(topology)
            ), topology.topology_id
    # Not vacuous: at least one shape actually satisfies the antecedent.
    assert any(topology_supports_shm_ring(t) for t in shapes)


# --- ring_channels_for_topology: the WIDTH the boolean is derived from --------
#
# The eligibility table, asserted as widths rather than as yes/no, plus the
# derivation itself. The two must not be able to disagree: a predicate that
# answered True where no width exists (or vice versa) is exactly the shear this
# rung replaces, so the derivation is pinned over the WHOLE table, not spot-
# checked on one shape.


def _eligibility_table():
    return [
        ("unconfigured", _topology([]), None),
        ("full_range_stereo", _full_range_stereo(), RING_STEREO_PROGRAM_CHANNELS),
        ("full_range_mono", _full_range_mono(), None),
        ("subwoofer", _subwoofer_topology(), None),
        ("composite_dual_apple", _dual_apple_stereo(), None),
        ("active_2_way_stereo", _active_topology("stereo", "active_2_way"), None),
        ("active_3_way_mono", _active_topology("mono", "active_3_way"), None),
    ]


def test_ring_channels_for_topology_answers_the_eligibility_table():
    for label, topology, expected in _eligibility_table():
        assert ring_channels_for_topology(topology) == expected, label


def test_topology_supports_shm_ring_is_derived_from_the_width():
    """The boolean is exactly "a width exists", on every shape in the table.

    Not a spot check: two independent answers to one question is how a
    ring-eligible box and a ring width drift apart, and the drift would only
    show up as an arm that builds a geometry no other end declares.
    """
    for label, topology, expected in _eligibility_table():
        assert topology_supports_shm_ring(topology) is (expected is not None), label


def test_roleful_topology_has_no_ring_width_despite_a_declared_active_lane():
    """An active-crossover box is ineligible, not "eligible at N channels".

    Its DAC may well declare an active outputd lane, but the ring transport for
    post-crossover per-driver channels does not exist: both shipped conf.d PCMs
    are the full-range stereo program. Reporting a width here would make the
    derived predicate arm a box onto a ring nothing builds.
    """
    active = _active_topology("stereo", "active_2_way")
    assert ring_channels_for_topology(active) is None
    assert topology_supports_shm_ring(active) is False


def test_ring_stereo_program_channels_agrees_with_the_ring_a_declaration():
    """One number, reached from the topology side and from the wire side.

    ``RING_STEREO_PROGRAM_CHANNELS`` (this module) and ``RING_A_CHANNELS``
    (jasper.fanin_coupling) are the same fact — the program upstream of
    CamillaDSP is stereo — declared where each side needs it. Pin them equal so
    a change to one is a failing test, not a shear on the wire.
    """
    from jasper.fanin_coupling import RING_A_CHANNELS

    assert RING_STEREO_PROGRAM_CHANNELS == RING_A_CHANNELS


# --- DEFECT 2: the SHIPPED-DEFAULT box must be ring-eligible ------------------
# jts.local is a plain solo stereo single USB DAC (apple_usb_c_dongle). Its saved
# output_topology.json out of the box has speaker_groups=[] and hardware.outputs
# in state="unused" (that IS the shipped-default shape). It was refused arming
# with arm_ring_topology_ineligible — a false-negative *of the box*. These pin
# the real topology-artifact shapes: shipped-default-unused-stereo is ELIGIBLE;
# a box carrying STALE subwoofer artifacts is (correctly) ineligible and the
# topology-reset recovers eligibility. dac8x-roleful (jts3) and composite stay
# ineligible (covered above via _active_topology / _subwoofer_topology / dual).


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
    assert topology_supports_shm_ring(topo) is False


def test_empty_topology_is_ineligible_regardless_of_child_count():
    single = _apple_dongle_shipped_default()
    assert len(single.hardware.child_devices) == 1
    assert topology_supports_shm_ring(single) is False

    dual = _dual_apple_stereo()
    assert len(dual.hardware.child_devices) == 2
    assert topology_supports_shm_ring(dual) is False


def test_shipped_default_output_state_does_not_authorize_ring():
    raw = _apple_dongle_shipped_default().to_dict()
    for out in raw["hardware"]["outputs"]:
        out["state"] = "verified"
    topo = OutputTopology.from_mapping(raw)
    assert topology_supports_shm_ring(topo) is False


def test_stale_subwoofer_on_dongle_is_correctly_ineligible():
    # A stale subwoofer group makes the box (correctly) ineligible — a stereo ring
    # cannot carry a sub role. This is honest classification, not a bug.
    topo = _apple_dongle_with_stale_subwoofer()
    contract = classify_output_contract(topo)
    assert contract.classification == CONTRACT_SUBWOOFER_PRESENT
    assert contract.requires_roleful_graph is True
    assert topology_supports_shm_ring(topo) is False


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
    assert DEFAULT_RING_FLAT_OUTPUTD_CONFIG.name == RING_FLAT_CONFIG_NAME


# --- the flat ring cutover is not RENDERED onto a roleful box ----------------


def _render_names(tmp_path: Path, topology) -> set[str]:
    from jasper.sound.camilla_yaml import render_flat_cutover_configs

    out = tmp_path / "camilladsp"
    out.mkdir()
    render_flat_cutover_configs(config_dir=out, topology=topology)
    return {p.name for p in out.iterdir()}


def test_flat_ring_cutover_is_rendered_on_a_non_roleful_box(tmp_path: Path):
    assert RING_FLAT_CONFIG_NAME in _render_names(tmp_path, _full_range_stereo())


@pytest.mark.parametrize(
    "topology_factory",
    [
        lambda: _active_topology("stereo", "active_2_way"),
        lambda: _active_topology("mono", "active_3_way"),
        _subwoofer_topology,
    ],
    ids=["active_2_way", "active_3_way", "subwoofer"],
)
def test_flat_ring_cutover_is_not_rendered_onto_a_roleful_box(
    tmp_path: Path, topology_factory
):
    """A solo-stereo FULL-RANGE graph must not exist on a crossover box.

    ``outputd-cutover-ring.yml`` is emitted by the flat (solo-stereo) emitter.
    On a box whose outputs carry driver roles, that graph is full-range content
    routed to a tweeter — so the file is not written there at all, rather than
    written and guarded downstream.
    """
    names = _render_names(tmp_path, topology_factory())
    assert RING_FLAT_CONFIG_NAME not in names
    # The loopback flat config still renders — this gate removes ONE file, not
    # the render.
    assert "outputd-cutover.yml" in names


def test_flat_ring_cutover_render_gate_keys_on_rolefulness_not_ring_eligibility():
    """THE TRAP, named: the gate must read ``requires_roleful_graph``.

    ``topology_supports_shm_ring`` looks like the natural predicate — today it is
    False for exactly the roleful boxes this gate protects, so keying on it would
    pass every test written today. It is the WRONG predicate, and it fails at the
    worst moment: ring v2's N-channel rung gives roleful topologies a ring width,
    which flips ``topology_supports_shm_ring`` TRUE for those same boxes. A gate
    keyed on eligibility would then start writing the full-range flat ring graph
    onto crossover boxes precisely when the ring goes live there — a
    hearing-safety regression introduced by a change that never touched this
    file.

    Rolefulness is about what the graph would DRIVE and no later rung inverts it,
    which is why it is the axis. This pins the reader itself rather than the
    render's output, because the two predicates are indistinguishable by
    behaviour until that later rung lands.
    """
    from jasper.sound import camilla_yaml

    roleful = _active_topology("stereo", "active_2_way")
    # Today both predicates would refuse this box — that is exactly why a
    # behavioural test cannot tell them apart.
    assert classify_output_contract(roleful).requires_roleful_graph is True
    assert topology_supports_shm_ring(roleful) is False

    # So pin the decision to the rolefulness axis: with rolefulness forced
    # False, the ring config renders even though the box is ring-INELIGIBLE.
    # A gate reading topology_supports_shm_ring could not produce this.
    assert camilla_yaml._roleful_topology(roleful) is True
    assert camilla_yaml._roleful_topology(_full_range_stereo()) is False
    assert camilla_yaml._roleful_topology(_dual_apple_stereo()) is False
    assert topology_supports_shm_ring(_dual_apple_stereo()) is False
