# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ADR-0178: the four named parks of the one-audio-transport rule.

Each class gets ONE parametrized pin (it parks, naming its tracked issue or
its remedy) and ONE kill test (the nearest working shape it must not bite) —
the latter being the whole risk of this feature: jts.local runs a ring-armed
roleful composite TODAY and must never see a composite park.

Structured fields only. The prose beside each class is presentation.
"""

from __future__ import annotations

import pytest

from jasper.control import transport_park
from jasper.control.transport_park import (
    PARK_GROUPED_DAC_CONTENT_LANE,
    PARK_MONO_FULL_RANGE,
    PARK_PASSIVE_STEREO_COMPOSITE,
    PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED,
)
from jasper.fanin_coupling import OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR
from jasper.output_topology import OutputTopology

from tests.test_active_speaker_runtime_contract import (
    _active_topology,
    _full_range_mono,
    _full_range_stereo,
    _subwoofer_topology,
)
from tests.test_composite_ring_arm_enabling import (
    _composite_active_2way,
    _composite_topology,
)
from tests.test_runtime_contract_ring import _dual_apple_stereo

_FIFO_ENV = "JASPER_OUTPUTD_DAC_CONTENT_FIFO"
_ARMED = {OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1"}


def _composite_subwoofer_only() -> OutputTopology:
    """A ROLEFUL composite whose ACTIVE ring width does not resolve.

    One subwoofer output makes the box roleful (so it is not the passive
    shape #2982 tracks) while leaving the driven width at 1, below the ring
    accept-set's floor — so ``active_ring_channels_for_topology`` answers
    ``None`` and this reaches the composite branch with nothing else stopping
    it. The narrowest shape that proves the passive-only discriminator is
    load-bearing rather than shadowed by the no-ring gate above it.
    """
    return _composite_topology(
        [
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [{"role": "subwoofer", "physical_output_index": 0}],
            }
        ],
        routing={"subwoofer_group_ids": ["sub"]},
    )


def _classes(parks) -> set[str]:
    return {park.park_class for park in parks}


def _by_class(parks, park_class):
    return next(park for park in parks if park.park_class == park_class)


# --- one pin per class -------------------------------------------------------

# (topology, env, expected class, expected issue, expected remedy)
#
# The issue numbers and the remedy command are spelled as LITERALS, never as
# the module's own constants: a case built from the constant it checks is
# tautological — renumbering an issue would move both sides together and the
# owner's ruling (#2982 / #3117 / #3118, and the one recorded command) would
# stop being pinned by anything.
_PARK_CASES = (
    pytest.param(
        _dual_apple_stereo(),
        {},
        PARK_PASSIVE_STEREO_COMPOSITE,
        "#2982",
        None,
        id="passive_stereo_composite",
    ),
    pytest.param(
        _full_range_mono(),
        {},
        PARK_MONO_FULL_RANGE,
        "#3117",
        None,
        id="mono_full_range",
    ),
    pytest.param(
        _active_topology("stereo", "active_2_way"),
        {},
        PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED,
        None,
        "jasper-active-speaker baseline-reemit --endpoint ring",
        id="roleful_active_endpoint_unconverged",
    ),
    pytest.param(
        _full_range_stereo(),
        {**_ARMED, _FIFO_ENV: "/run/jasper-grouping/member-content.fifo"},
        PARK_GROUPED_DAC_CONTENT_LANE,
        "#3118",
        None,
        id="grouped_dac_content_lane",
    ),
)


@pytest.mark.parametrize(
    "topology,env,park_class,issue,remedy", _PARK_CASES
)
def test_each_class_parks_naming_its_issue_or_remedy(
    topology, env, park_class, issue, remedy
):
    parks = transport_park.classify(topology, env)
    assert park_class in _classes(parks)
    park = _by_class(parks, park_class)
    assert park.issue == issue
    assert park.remedy == remedy
    # Every class carries exactly one of the two: a rebuild issue to wait on,
    # or a command to run. A class with neither would be a park an operator
    # cannot act on and cannot track.
    assert (park.issue is None) != (park.remedy is None)


@pytest.mark.parametrize(
    "topology,env,park_class,issue,remedy", _PARK_CASES
)
def test_each_class_is_loud_under_ring_only(
    topology, env, park_class, issue, remedy
):
    state = transport_park.snapshot(topology, env, ring_only=True)
    assert state["status"] == "parked"
    assert state["parked"] is True
    assert park_class in {park["park_class"] for park in state["parks"]}


@pytest.mark.parametrize(
    "topology,env,park_class,issue,remedy", _PARK_CASES
)
def test_each_class_is_pending_while_loopback_still_exists(
    topology, env, park_class, issue, remedy
):
    """The behaviour-preserving half: a box the loopback route still carries
    is disclosed, never reported silent."""
    state = transport_park.snapshot(topology, env, ring_only=False)
    assert state["status"] == "pending"
    assert state["parked"] is False
    assert park_class in {park["park_class"] for park in state["parks"]}


# --- one kill test per class -------------------------------------------------


def test_ring_armed_roleful_composite_does_not_park():
    """THE kill test: jts.local's shape today — a ring-armed composite whose
    roleful program rides the ACTIVE ring. No class may bite it."""
    parks = transport_park.classify(_composite_active_2way(), _ARMED)
    assert parks == ()
    assert transport_park.snapshot(
        _composite_active_2way(), _ARMED, ring_only=True
    )["status"] == "ok"


def test_a_roleful_composite_with_no_active_ring_is_not_the_passive_park():
    """A composite made roleful by one subwoofer output resolves no ACTIVE
    ring width, so it reaches the composite branch — and must still not be
    reported as the PASSIVE shape #2982 tracks. It is outside ADR-0178's four
    classes and this module names no park for it."""
    parks = transport_park.classify(_composite_subwoofer_only(), {})
    assert PARK_PASSIVE_STEREO_COMPOSITE not in _classes(parks)
    assert parks == ()


def test_a_roleful_non_composite_shape_is_not_the_mono_park():
    """A subwoofer-only box reaches the no-ring gate too. #3117 is the
    1-channel FULL-RANGE shape; anything else there gets no mono park."""
    parks = transport_park.classify(_subwoofer_topology(), {})
    assert PARK_MONO_FULL_RANGE not in _classes(parks)
    assert parks == ()


def test_active_crossover_mono_does_not_park_as_mono_full_range():
    """A roleful mono box is 2+ channels on the ACTIVE ring, not the
    1-channel full-range shape #3117 tracks."""
    parks = transport_park.classify(_active_topology("mono", "active_2_way"), _ARMED)
    assert PARK_MONO_FULL_RANGE not in _classes(parks)
    assert parks == ()


def test_converged_active_endpoint_does_not_park():
    parks = transport_park.classify(
        _active_topology("stereo", "active_2_way"), _ARMED
    )
    assert PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED not in _classes(parks)
    assert parks == ()


def test_cleared_dac_content_lane_does_not_park():
    """The grouping reconciler writes the FIFO key as an EMPTY string when
    this speaker is not an active member, so presence is not arming."""
    parks = transport_park.classify(_full_range_stereo(), {_FIFO_ENV: ""})
    assert parks == ()


def test_ring_eligible_stereo_box_does_not_park():
    assert transport_park.classify(_full_range_stereo(), {}) == ()


def test_unconfigured_topology_does_not_park():
    """An undeclared box holds silence through the speaker-setup park
    (#2135); re-reporting it here would double-count one fact."""
    from tests.test_active_speaker_runtime_contract import _topology

    assert transport_park.classify(_topology([]), {}) == ()


# --- the gate ----------------------------------------------------------------


def test_ring_only_is_derived_from_the_coupling_vocabulary(monkeypatch):
    """No flag to flip: the transport deletion removes the loopback coupling
    and this answers True with no second edit."""
    from jasper import fanin_coupling

    monkeypatch.setattr(
        fanin_coupling,
        "VALID_COUPLINGS",
        frozenset({fanin_coupling.COUPLING_SHM_RING}),
    )
    assert transport_park.ring_only_transport() is True

    monkeypatch.setattr(
        fanin_coupling,
        "VALID_COUPLINGS",
        frozenset({fanin_coupling.COUPLING_SHM_RING, fanin_coupling.COUPLING_LOOPBACK}),
    )
    assert transport_park.ring_only_transport() is False


def test_todays_tree_still_has_the_loopback_route():
    """Pins that this slice ships INERT on the fleet: while loopback is a
    legal coupling nothing new reports a speaker silent."""
    assert transport_park.ring_only_transport() is False


def test_a_box_in_two_classes_reports_both():
    """A bonded mono speaker waits on #3117 AND #3118; a first-match verdict
    would hide one of them from the operator who has to clear both."""
    parks = transport_park.classify(
        _full_range_mono(), {_FIFO_ENV: "/run/x.fifo"}
    )
    assert _classes(parks) == {PARK_MONO_FULL_RANGE, PARK_GROUPED_DAC_CONTENT_LANE}


def test_an_unreadable_input_is_not_a_healthy_box(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("topology unreadable")

    monkeypatch.setattr(transport_park, "classify", _boom)
    state = transport_park.snapshot(ring_only=True)
    assert state["status"] == "unavailable"
    assert state["parked"] is False


# --- doctor -----------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [("ok", "ok"), ("pending", "warn"), ("parked", "fail"), ("unavailable", "warn")],
)
def test_doctor_severity_follows_the_park_status(monkeypatch, status, expected):
    from jasper.cli.doctor.audio_runtime import check_ring_transport_park

    monkeypatch.setattr(
        transport_park,
        "snapshot",
        lambda *a, **k: {
            "status": status,
            "parked": status == "parked",
            "ring_only": status == "parked",
            "parks": [
                {
                    "park_class": PARK_MONO_FULL_RANGE,
                    "issue": "#3117",
                    "remedy": None,
                    "detail": "d",
                }
            ],
            "error": "unreadable",
        },
    )
    assert check_ring_transport_park().status == expected


# --- /state and the household card -------------------------------------------


def test_state_resilience_carries_the_park_reader():
    from jasper.control import state_aggregate

    assert state_aggregate.transport_park is transport_park


@pytest.mark.parametrize(
    "topology,env,park_class,issue,remedy", _PARK_CASES
)
def test_a_live_park_writes_one_household_incident_per_class(
    topology, env, park_class, issue, remedy
):
    from jasper.control.audio_health import _state_issues

    state = transport_park.snapshot(topology, env, ring_only=True)
    issues = _state_issues(
        {"warmup_active": True},
        None,
        {},
        {},
        None,
        transport_park=state,
    )
    keys = {issue_row["key"] for issue_row in issues}
    assert f"path.transport_park.{park_class}" in keys


@pytest.mark.parametrize(
    "topology,env,park_class,issue,remedy", _PARK_CASES
)
def test_a_pending_park_reaches_no_household_surface(
    topology, env, park_class, issue, remedy
):
    """The box plays on the loopback route; calling it silent would be the
    confusion ADR-0100 exists to prevent, pointed the wrong way."""
    from jasper.control.audio_health import (
        _state_issues,
        _transport_park_signal,
    )

    state = transport_park.snapshot(topology, env, ring_only=False)
    issues = _state_issues(
        {"warmup_active": True},
        None,
        {},
        {},
        None,
        transport_park=state,
    )
    assert not [
        row for row in issues if str(row["key"]).startswith("path.transport_park.")
    ]
    assert _transport_park_signal(state) is None


def test_a_live_park_takes_the_household_headline():
    from jasper.control.audio_health import PARKED_HEADLINE, _transport_park_signal

    state = transport_park.snapshot(_full_range_mono(), {}, ring_only=True)
    signal = _transport_park_signal(state)
    assert signal is not None
    assert signal["status"] == "issue"
    assert signal["headline"] == PARKED_HEADLINE
