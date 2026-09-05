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

from types import SimpleNamespace

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
#: The ring transport's arming marker, spelled as a LITERAL for the same
#: reason the issue numbers below are: a case built from the constant it
#: checks would move both sides together under a rename and stop pinning
#: that the classifier reads the key outputd actually reads.
_LANE_ENV = "JASPER_OUTPUTD_DAC_CONTENT_LANE"
_ARMED = {OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1"}


def _stereo_plus_subwoofer() -> OutputTopology:
    """Passive stereo mains plus a subwoofer: roleful, but NOT active-crossover.

    ``requires_roleful_graph`` is True (the sub), and the ACTIVE ring width
    resolves at 3 — so this reaches class (c)'s site and is stopped only by
    the active-crossover narrowing.
    """
    from tests.test_active_speaker_runtime_contract import _topology

    return _topology(
        [
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
                "channels": [{"role": "subwoofer", "physical_output_index": 2}],
            },
        ],
        {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    )


def _left_only() -> OutputTopology:
    """A configured layout that is neither stereo nor mono — a half-finished
    commissioning save. No ring geometry of either kind, and no named class."""
    from tests.test_active_speaker_runtime_contract import _topology

    return _topology(
        [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            }
        ],
        {"main_left_group_id": "left"},
    )


def _mono_awaiting_its_output() -> OutputTopology:
    """A mono layout that still resolves NO ring — the class's live trigger.

    A CLEAN mono box is ring-eligible now, so what still reaches this class is
    a mono contract carrying ISSUES: here a full-range channel with no physical
    output yet. The class stays defined and stays exercised; what changed is
    which mono boxes trip it.
    """
    from tests.test_active_speaker_runtime_contract import _topology

    return _topology(
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


def _composite_stereo_plus_subwoofer() -> OutputTopology:
    """The ADR-0189 exception: a COMPOSITE sink whose ACTIVE width resolves.

    Same roleful-but-not-active-crossover shape as
    :func:`_stereo_plus_subwoofer`, on a two-child composite sink. The
    hardware reconciler arms the endpoint marker for this class, so (armed,
    no active modes) is its normal serving state rather than a mismatch.
    """
    return _composite_topology(
        [
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
                "channels": [{"role": "subwoofer", "physical_output_index": 2}],
            },
        ],
        routing={
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
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
        _mono_awaiting_its_output(),
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
        # BOTH steps. `baseline-reemit` moves the graph and writes no env; the
        # marker this park reads has one writer, jasper-audio-hardware-reconcile.
        # A one-step remedy would leave the operator re-running the doctor into
        # the identical park.
        "sudo /opt/jasper/.venv/bin/jasper-active-speaker baseline-reemit "
        "--endpoint ring && sudo systemctl start jasper-audio-hardware-reconcile",
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
def test_each_class_is_loud(topology, env, park_class, issue, remedy):
    state = transport_park.snapshot(topology, env)
    assert state["status"] == "parked"
    assert state["parked"] is True
    assert park_class in {park["park_class"] for park in state["parks"]}


# --- one kill test per class -------------------------------------------------


def test_ring_armed_roleful_composite_does_not_park():
    """THE kill test: jts.local's shape today — a ring-armed composite whose
    roleful program rides the ACTIVE ring. No class may bite it."""
    parks = transport_park.classify(_composite_active_2way(), _ARMED)
    assert parks == ()
    assert transport_park.snapshot(
        _composite_active_2way(), _ARMED
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


def test_a_clean_passive_mono_box_is_ring_eligible_and_does_not_park():
    """The flip. A mono cabinet rides the same 2-channel Ring B every passive
    box does — its mono-ness is the CamillaDSP graph's fold plus hard mute,
    downstream of every ring end — so nothing about it is unresolved and no
    class names it. The CLASS itself stays defined; retirement is a separate
    change, after hardware verification."""
    from jasper.active_speaker.runtime_contract import (
        RING_STEREO_PROGRAM_CHANNELS,
        ring_channels_for_topology,
        topology_supports_shm_ring,
    )

    topology = _full_range_mono()

    assert ring_channels_for_topology(topology) == RING_STEREO_PROGRAM_CHANNELS
    assert topology_supports_shm_ring(topology) is True
    assert transport_park.classify(topology, {}) == ()


def test_snapshot_separates_clean_mono_from_mono_with_issues():
    """The status pair the operator surfaces read. A clean mono box is `ok`;
    one still awaiting its physical output is `parked`."""
    clean = transport_park.snapshot(_full_range_mono(), {})
    assert clean["status"] == "ok"
    assert clean["parked"] is False
    assert clean["parks"] == []

    unassigned = transport_park.snapshot(_mono_awaiting_its_output(), {})
    assert unassigned["status"] == "parked"
    assert unassigned["parked"] is True
    assert PARK_MONO_FULL_RANGE in {
        park["park_class"] for park in unassigned["parks"]
    }


def test_active_crossover_mono_does_not_park_as_mono_full_range():
    """A roleful mono box is 2+ channels on the ACTIVE ring, not the
    1-channel full-range shape #3117 tracks."""
    parks = transport_park.classify(_active_topology("mono", "active_2_way"), _ARMED)
    assert PARK_MONO_FULL_RANGE not in _classes(parks)
    assert parks == ()


def test_a_passive_stereo_plus_subwoofer_box_does_not_park_on_the_endpoint():
    """`requires_roleful_graph` is True for a passive box that merely adds a
    sub, but there is no active-speaker baseline to re-emit — parking it would
    hand the household a remedy that cannot run."""
    parks = transport_park.classify(_stereo_plus_subwoofer(), {})
    assert PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED not in _classes(parks)


def test_converged_active_endpoint_does_not_park():
    parks = transport_park.classify(
        _active_topology("stereo", "active_2_way"), _ARMED
    )
    assert PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED not in _classes(parks)
    assert parks == ()


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({_FIFO_ENV: "/run/x.fifo"}, id="fifo_only"),
        pytest.param({_FIFO_ENV: "/run/x.fifo", _LANE_ENV: "1"}, id="fifo_and_marker"),
    ],
)
def test_the_legacy_fifo_spelling_arms_the_grouped_park(env):
    """THE FIFO SPELLING ALONE keeps this class, and its issue.

    The FIFO half still needs ``CONTENT_BRIDGE=direct``, which no writer emits
    and which outputd refuses beside the marker, so it has no producer and the
    box is silent. A box carrying BOTH still parks: the FIFO is the half that
    cannot run.
    """
    parks = transport_park.classify(_full_range_stereo(), env)
    assert _classes(parks) == {PARK_GROUPED_DAC_CONTENT_LANE}
    assert _by_class(parks, PARK_GROUPED_DAC_CONTENT_LANE).issue == "#3118"


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({_LANE_ENV: "1"}, id="marker_only"),
        pytest.param({_LANE_ENV: "on"}, id="marker_word"),
        pytest.param({_LANE_ENV: "1", _FIFO_ENV: ""}, id="marker_with_cleared_fifo"),
    ],
)
def test_a_marker_armed_member_is_served_and_does_not_park(env):
    """THE CUTOVER, from the park's side: a marker-armed member PLAYS.

    The grouping reconciler arms the marker on every dumb member it can serve,
    and outputd serves it — it selects the dac-content return ring as the box's
    sole content source. Parking that box would report a speaker that is audibly
    working, and hand its household "ungrouping it brings sound back".
    """
    assert transport_park.classify(_full_range_stereo(), env) == ()


def test_the_marker_beside_a_declared_bridge_parks_under_its_own_name():
    """OUTPUTD REFUSES THIS PAIR, so the box is silent with every unit green.

    Reachable rather than theoretical: `jasper-fanin-coupling-auto` writes the
    bridge into the FIRST env layer on every pass, so a member whose grouping
    layer failed to clear it lands here (ADR-0220).
    """
    parks = transport_park.classify(
        _full_range_stereo(),
        {"JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring", _LANE_ENV: "1"},
    )
    assert _classes(parks) == {transport_park.PARK_DAC_CONTENT_MARKER_BESIDE_BRIDGE}
    park = _by_class(parks, transport_park.PARK_DAC_CONTENT_MARKER_BESIDE_BRIDGE)
    assert park.issue == "#3118"
    assert park.remedy == transport_park.BRIDGE_BESIDE_MARKER_REMEDY


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="neither_key"),
        pytest.param({_FIFO_ENV: ""}, id="fifo_cleared"),
        pytest.param({_FIFO_ENV: "", _LANE_ENV: ""}, id="ungrouped_clears_both"),
        pytest.param({_FIFO_ENV: "   "}, id="fifo_whitespace_only"),
        pytest.param({_LANE_ENV: "0"}, id="marker_off"),
    ],
)
def test_an_unarmed_lane_parks_under_neither_spelling(env):
    """THE kill test for this class.

    The FIFO is read as a non-empty PATH, because the grouping reconciler writes
    it as an EMPTY string on every branch — and stripped, because a
    whitespace-only value is not a path and
    ``transport_park._assess`` — now the only reader of that key — strips it too.
    """
    assert transport_park.classify(_full_range_stereo(), env) == ()


def test_the_grouped_park_reads_the_key_the_ring_module_owns():
    """The classifier and the ring identity must name ONE key.

    The literal above is what pins it: if the owner module's value drifts, the
    classifier would silently watch a key nothing writes and the park would go
    quiet.
    """
    from jasper.multiroom.dac_content_ring import DAC_CONTENT_LANE_ENV

    assert DAC_CONTENT_LANE_ENV == _LANE_ENV


def test_ring_eligible_stereo_box_does_not_park():
    assert transport_park.classify(_full_range_stereo(), {}) == ()


def test_unconfigured_topology_does_not_park():
    """An undeclared box holds silence through the speaker-setup park
    (#2135); re-reporting it here would double-count one fact."""
    from tests.test_active_speaker_runtime_contract import _topology

    assert transport_park.classify(_topology([]), {}) == ()


# --- the honest silence ------------------------------------------------------


@pytest.mark.parametrize(
    "topology",
    [
        pytest.param(_left_only(), id="left_only_half_saved"),
        pytest.param(_subwoofer_topology(), id="subwoofer_only"),
        pytest.param(_composite_subwoofer_only(), id="composite_subwoofer_only"),
    ],
)
def test_configured_but_unnamed_is_disclosed_not_called_servable(topology):
    """No ring geometry of either kind and no class names it. Saying "ok" here
    would tell an operator the ring can serve a box it demonstrably cannot."""
    state = transport_park.snapshot(topology, {})
    assert state["status"] == "unclassified"
    assert state["parked"] is False
    assert state["parks"] == []


def test_unclassified_reaches_no_household_surface():
    from jasper.control.audio_health import _state_issues, _transport_park_signal

    state = transport_park.snapshot(_left_only(), {})
    assert _transport_park_signal(state) is None
    assert not _state_issues(
        {"warmup_active": True}, None, {}, {}, None, transport_park=state
    )


def test_a_ring_eligible_box_still_reports_ok():
    """The ok arm must stay reachable — otherwise `unclassified` has quietly
    become the answer for everything."""
    assert transport_park.snapshot(
        _full_range_stereo(), {}
    )["status"] == "ok"


def test_a_box_in_two_classes_reports_both():
    """A bonded mono speaker waits on #3117 AND #3118; a first-match verdict
    would hide one of them from the operator who has to clear both."""
    parks = transport_park.classify(
        _mono_awaiting_its_output(), {_FIFO_ENV: "/run/x.fifo"}
    )
    assert _classes(parks) == {PARK_MONO_FULL_RANGE, PARK_GROUPED_DAC_CONTENT_LANE}


def test_a_corrupt_topology_file_is_not_a_healthy_box(tmp_path, monkeypatch):
    """A REAL corrupt file, not a monkeypatched raise: the fail-soft loader
    degrades corruption to an empty draft, which classifies as not-configured
    and would report a rotted box as healthy on all three surfaces. The strict
    loader is what makes `unavailable` reachable."""
    from jasper import output_topology as ot

    corrupt = tmp_path / "output_topology.json"
    corrupt.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setattr(ot, "topology_path", lambda _p=None: corrupt)

    state = transport_park.snapshot(env={})
    assert state["status"] == "unavailable"
    assert state["parked"] is False
    assert state["parks"] == []
    assert state["endpoint_armed_without_active_modes"] is False


def test_a_missing_topology_is_still_not_configured(tmp_path, monkeypatch):
    """Missing is NOT corrupt: a fresh box must reach `ok`, never `unavailable`."""
    from jasper import output_topology as ot

    monkeypatch.setattr(
        ot, "topology_path", lambda _p=None: tmp_path / "absent.json"
    )
    assert transport_park.snapshot(env={})["status"] == "ok"


# --- doctor -----------------------------------------------------------------

from jasper.cli.doctor.audio_runtime_ring import (  # noqa: E402
    REASON_TRANSPORT_CONVERGE_REFUSED as _REASON_CONVERGE_REFUSED,
    REASON_TRANSPORT_ENDPOINT_ARMED_WITHOUT_ACTIVE_MODE as _REASON_ARMED_NO_MODES,
    REASON_TRANSPORT_ENDPOINT_UNPROVEN as _REASON_UNPROVEN,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("ok", "ok"),
        ("parked", "fail"),
        ("unavailable", "skipped"),
        ("unclassified", "warn"),
    ],
)
def test_doctor_severity_follows_the_park_status(monkeypatch, status, expected):
    from jasper.cli.doctor.audio_runtime_ring import check_ring_transport_park

    monkeypatch.setattr(
        transport_park,
        "snapshot",
        lambda *a, **k: {
            "status": status,
            "parked": status == "parked",
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


# --- ADR-0184: the coverage seam that is not a park --------------------------


@pytest.mark.parametrize(
    "topology,env,unproven,expected_reason",
    [
        (_stereo_plus_subwoofer(), {}, True, _REASON_UNPROVEN),
        (_stereo_plus_subwoofer(), _ARMED, False, _REASON_ARMED_NO_MODES),
        (_active_topology("stereo", "active_2_way"), _ARMED, False, ""),
        (_full_range_stereo(), {}, False, ""),
    ],
    ids=["seam_unarmed", "armed_no_modes", "armed_active_crossover", "plain_stereo"],
)
def test_an_unproven_endpoint_is_named_without_naming_a_park(
    monkeypatch, topology, env, unproven, expected_reason
):
    """ADR-0184: a width resolves, nothing armed the endpoint, no class names it.

    ``requires_roleful_graph`` is True for a subwoofer box, so the ACTIVE width
    resolves and every topology class is skipped; ``active_modes`` is empty, so
    the endpoint class is scoped out. The doctor's greenest verdict said nothing
    at all about it. It now carries a REASON instead — an operator signal its
    own reader declares to be neither a park nor a household claim, so the row
    stays ``ok`` and the reason is what a consumer branches on.

    ``armed_no_modes`` names a DIFFERENT boolean (ADR-0189), which is why
    ``unproven`` is False on that row: ADR-0184's seam keeps its unarmed-marker
    condition, so the two never describe one box.
    """
    from jasper.cli.doctor.audio_runtime_ring import check_ring_transport_park

    state = transport_park.snapshot(topology, env)
    assert state["unproven_endpoint"] is unproven
    assert state["status"] == "ok"
    assert state["parks"] == []

    monkeypatch.setattr(transport_park, "snapshot", lambda *a, **k: state)
    result = check_ring_transport_park()
    assert result.status == "ok", result
    assert result.reason == expected_reason


@pytest.mark.parametrize(
    "topology,env,park_class,issue,remedy", _PARK_CASES
)
def test_no_named_park_also_reports_an_unproven_endpoint(
    topology, env, park_class, issue, remedy
):
    """The seam signal is the COMPLEMENT of class (c) on the same two inputs,
    so a named park and the signal cannot describe one box — the double-report
    ADR-0178 refuses, refused again."""
    state = transport_park.snapshot(topology, env)
    assert state["unproven_endpoint"] is False


def test_an_unproven_endpoint_reaches_no_household_surface():
    """Operator-only. The box plays today and is unproven rather than named
    silent afterwards; there is no household action either way."""
    from jasper.control.audio_health import _state_issues, _transport_park_signal

    state = transport_park.snapshot(_stereo_plus_subwoofer(), {})
    assert state["unproven_endpoint"] is True
    assert _transport_park_signal(state) is None
    issues = _state_issues(
        {"warmup_active": True}, None, {}, {}, None, transport_park=state
    )
    assert not [
        row for row in issues if str(row["key"]).startswith("path.transport_park.")
    ]


# --- the fifth shape: ring-eligible, converge refused ------------------------


def _loaded_graph(monkeypatch, *, note="", converged=True, detail="elsewhere"):
    """Stand in for the loaded CamillaDSP graph the refusal signal reads.

    Returns the call log, so a test can prove the read did NOT happen — the
    gate is the claim, and a signal that read the graph on every box would
    cost every box a file read to answer a question about none of them.
    """
    from jasper.fanin import ring_health

    reads: list[object] = []

    def _read(*args, **kwargs):
        reads.append(args)
        return SimpleNamespace(note=note)

    monkeypatch.setattr(ring_health, "read_loaded_camilla_graph", _read)
    monkeypatch.setattr(
        ring_health,
        "graph_at_active_ring_endpoint",
        lambda graph: (converged, detail),
    )
    return reads


def test_an_armed_endpoint_whose_graph_never_moved_names_itself(monkeypatch):
    """The shape neither ADR-0178 nor ADR-0184 covers: ring-eligible, marker
    ARMED, program never moved onto the endpoint. jasper/fanin/converge.py
    refuses such a box every pass and leaves it as found, logging and keeping
    nothing — so every surface read `parked: false` about a box going nowhere.
    """
    _loaded_graph(monkeypatch, converged=False, detail="plays hw:0,0")
    state = transport_park.snapshot(
        _active_topology("stereo", "active_2_way"), _ARMED
    )
    # NOT a park: a refusal leaves the loaded graph running, so the "emits
    # nothing" claim `parked` makes would be false.
    assert state["status"] == "ok"
    assert state["parks"] == []
    assert state["parked"] is False
    assert state["unproven_endpoint"] is False
    assert "plays hw:0,0" in state["converge_refused"]


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"converged": True}, id="graph_is_at_the_endpoint"),
        pytest.param({"note": "no statefile", "converged": False}, id="unreadable"),
    ],
)
def test_a_converged_or_unreadable_graph_claims_no_refusal(monkeypatch, kwargs):
    """Only a graph this box positively read and found elsewhere is a refusal.
    An unreadable graph is unknown, and the surfaces that own THAT shape
    (`active_speaker_parked`, `camilla_recover`) are already loud about it."""
    _loaded_graph(monkeypatch, **kwargs)
    state = transport_park.snapshot(
        _active_topology("stereo", "active_2_way"), _ARMED
    )
    assert state["converge_refused"] is None


@pytest.mark.parametrize(
    "topology,env",
    [
        pytest.param(_stereo_plus_subwoofer(), _ARMED, id="not_active_crossover"),
        pytest.param(
            _active_topology("stereo", "active_2_way"), {}, id="marker_unarmed"
        ),
        pytest.param(_full_range_stereo(), _ARMED, id="no_active_ring"),
    ],
)
def test_the_refusal_signal_reads_no_graph_off_its_own_gate(
    monkeypatch, topology, env
):
    """A box outside the gate never pays for the graph read.

    ``not_active_crossover`` — a resolved width, no active modes, marker
    ARMED — is answered by ADR-0189's own boolean, not by this signal, and
    that boolean reads no graph. So the refusal gate stays silent here and
    pays nothing, which is what this test pins.
    """
    reads = _loaded_graph(monkeypatch, converged=False)
    state = transport_park.snapshot(topology, env)
    assert state["converge_refused"] is None
    assert reads == []


# --- ADR-0189: the seam's mirror, scoped by sink class ----------------------


@pytest.mark.parametrize(
    "topology,env,armed_without_modes,expected_reason",
    [
        pytest.param(
            _stereo_plus_subwoofer(), _ARMED, True, _REASON_ARMED_NO_MODES,
            id="non_composite_armed",
        ),
        pytest.param(
            _composite_stereo_plus_subwoofer(),
            _ARMED,
            False,
            "",
            id="composite_armed_is_served",
        ),
        pytest.param(
            _stereo_plus_subwoofer(), {}, False, _REASON_UNPROVEN,
            id="unarmed_is_the_0184_seam",
        ),
        pytest.param(
            _active_topology("stereo", "active_2_way"),
            _ARMED,
            False,
            "",
            id="active_crossover_is_the_refusal_gate",
        ),
        pytest.param(_full_range_stereo(), _ARMED, False, "", id="no_active_ring"),
    ],
)
def test_an_armed_endpoint_under_no_active_modes_discloses_off_composite(
    monkeypatch, topology, env, armed_without_modes, expected_reason
):
    """ADR-0189: the fourth combination stops reporting the greenest verdict.

    The composite row is the load-bearing one. Its sink is served with the
    marker armed and no active modes of its own, so a class-blind read would
    name every healthy composite box; it must stay reasonless on the same env
    that names the non-composite row directly above it.
    """
    from jasper.cli.doctor.audio_runtime_ring import check_ring_transport_park

    state = transport_park.snapshot(topology, env)
    assert state["endpoint_armed_without_active_modes"] is armed_without_modes
    assert state["status"] == "ok"
    assert state["parks"] == []

    monkeypatch.setattr(transport_park, "snapshot", lambda *a, **k: state)
    result = check_ring_transport_park()
    assert result.status == "ok", result
    assert result.reason == expected_reason


def test_the_two_endpoint_signals_are_never_both_true():
    """ADR-0184's seam needs the marker UNarmed; ADR-0189's needs it ARMED.

    Mutually exclusive by construction, which is what keeps ADR-0178's
    double-report objection from applying to the pair.
    """
    for env in ({}, _ARMED):
        state = transport_park.snapshot(
            _stereo_plus_subwoofer(), env
        )
        assert not (
            state["unproven_endpoint"]
            and state["endpoint_armed_without_active_modes"]
        )


@pytest.mark.parametrize(
    "refusal,expected_reason",
    [
        pytest.param(
            "the loaded graph plays hw:0,0", _REASON_CONVERGE_REFUSED, id="refused"
        ),
        pytest.param(None, "", id="converged"),
    ],
)
def test_the_doctor_names_a_converge_refusal(monkeypatch, refusal, expected_reason):
    """Parity with the ADR-0184 seam's branch beside it.

    The doctor is one of the surfaces ``transport_park``'s docstring promises
    cannot disagree; reading ``unproven_endpoint`` but not ``converge_refused``
    made ``ok`` speak for a box the converge pass keeps refusing. The refusal
    SENTENCE is the snapshot's own, carried through rather than re-composed,
    so this surface cannot describe it differently from `/state` and the card.
    """
    from jasper.cli.doctor.audio_runtime_ring import check_ring_transport_park

    state = {
        "status": "ok",
        "parked": False,
        "parks": [],
        "unproven_endpoint": False,
        "converge_refused": refusal,
    }
    monkeypatch.setattr(transport_park, "snapshot", lambda *a, **k: state)
    result = check_ring_transport_park()
    assert result.status == "ok", result
    assert result.reason == expected_reason
    # Never a park: the graph it already had keeps playing.
    assert result.speaker_silent is False


def test_a_converge_refusal_reaches_no_household_surface(monkeypatch):
    """Operator-only, like the ADR-0184 seam: the box is not claimed silent,
    and there is no household action either way."""
    from jasper.control.audio_health import _state_issues, _transport_park_signal

    _loaded_graph(monkeypatch, converged=False)
    state = transport_park.snapshot(
        _active_topology("stereo", "active_2_way"), _ARMED
    )
    assert state["converge_refused"]
    assert _transport_park_signal(state) is None
    issues = _state_issues(
        {"warmup_active": True}, None, {}, {}, None, transport_park=state
    )
    assert not [
        row for row in issues if str(row["key"]).startswith("path.transport_park.")
    ]


# --- /state and the household card -------------------------------------------


def test_the_remedy_converges_the_marker_it_reads():
    """The park reads the endpoint marker, whose single writer is
    jasper-audio-hardware-reconcile. A remedy that stops at baseline-reemit
    would not clear the park it is recorded against."""
    assert "jasper-audio-hardware-reconcile" in transport_park.ACTIVE_ENDPOINT_REMEDY


@pytest.mark.parametrize(
    "profile_id,status,topology_factory,expect_normal_remedy,expect_overlay_check_named",
    [
        pytest.param(
            "hifiberry_dac8x", "ready",
            lambda: _active_topology("stereo", "active_2_way"),
            True, False, id="recognized_and_ready",
        ),
        pytest.param(
            # active_profile_id needs ready+card-selected; observed_profile_id
            # does not — a recognized-but-not-ready DAC keeps the normal
            # remedy, never the reconciler's DRIVEN question.
            "hifiberry_dac8x", "blocked",
            lambda: _active_topology("stereo", "active_2_way"),
            True, False, id="recognized_but_not_ready",
        ),
        pytest.param(
            "unknown", "unknown",
            lambda: _active_topology("stereo", "active_2_way"),
            False, True, id="not_recognized_saved_dac_is_i2s",
        ),
        pytest.param(
            "unknown", "unknown",
            _composite_active_2way,
            False, False, id="not_recognized_saved_dac_is_usb",
        ),
    ],
)
def test_the_active_endpoint_remedy_names_the_overlay_check_only_for_an_unrecognized_i2s_dac(
    monkeypatch, tmp_path, profile_id, status, topology_factory,
    expect_normal_remedy, expect_overlay_check_named,
):
    """#2575: the recorded remedy re-emits onto a ring endpoint and converges
    it — neither step has a DAC to drive while none is RECOGNIZED. Every
    reader of the park record (doctor, /state, the web card) shares this one
    text, read at the snapshot altitude transport_park's docstring makes the
    one place surfaces read the answer from."""
    from jasper.output_hardware import OutputHardwareState, write_state

    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH", str(tmp_path / "output_hardware.json")
    )
    write_state(
        OutputHardwareState(
            profile_id=profile_id,
            profile_label=profile_id,
            status=status,
            physical_output_count=8,
        )
    )

    state = transport_park.snapshot(topology_factory(), {})
    assert state["status"] == "parked"
    [park] = [
        p for p in state["parks"]
        if p["park_class"] == PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED
    ]
    remedy = park["remedy"]

    assert (remedy == transport_park.ACTIVE_ENDPOINT_REMEDY) is expect_normal_remedy
    assert (
        transport_park.I2S_DAC_OVERLAY_CHECK_NAME in remedy
    ) is expect_overlay_check_named


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

    state = transport_park.snapshot(topology, env)
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


def test_a_live_park_takes_the_household_headline():
    from jasper.control.audio_health import PARKED_HEADLINE, _transport_park_signal

    state = transport_park.snapshot(_mono_awaiting_its_output(), {})
    signal = _transport_park_signal(state)
    assert signal is not None
    assert signal["status"] == "issue"
    assert signal["headline"] == PARKED_HEADLINE


@pytest.mark.parametrize(
    "topology,env,park_class,issue,remedy", _PARK_CASES
)
def test_a_live_park_says_which_shape_parked_the_box(
    topology, env, park_class, issue, remedy
):
    """Owner ruling 2026-08-27: a real message, not one canned sentence.

    Both household writers compose from the same table, so the incident row
    and the card cannot say different things about one park.
    """
    from jasper.control.audio_health import (
        PARKED_DETAIL,
        _state_issues,
        _transport_park_signal,
    )

    state = transport_park.snapshot(topology, env)
    rows = {
        row["key"]: row
        for row in _state_issues(
            {"warmup_active": True}, None, {}, {}, None, transport_park=state
        )
    }
    row = rows[f"path.transport_park.{park_class}"]
    assert row["detail"] != PARKED_DETAIL
    assert row["detail"] in _transport_park_signal(state)["detail"]
    if issue is not None:
        assert issue in row["detail"]
    else:
        # The one class carrying a recorded command instead of a tracked issue
        # sends the household to diagnostics for it. The command itself is a
        # `sudo` line — the register #2472 took off this card, and the operator
        # surfaces that already print it do not need a second copy here.
        assert remedy not in row["detail"]


def test_each_park_class_gets_its_own_household_sentence():
    from jasper.control.audio_health import PARKED_DETAIL, _park_detail

    details = set()
    for case in _PARK_CASES:
        topology, env, park_class = case.values[:3]
        state = transport_park.snapshot(topology, env)
        details.add(_park_detail(
            [park for park in state["parks"] if park["park_class"] == park_class]
        ))
    assert len(details) == len(_PARK_CASES)
    assert PARKED_DETAIL not in details


def test_an_unnamed_park_class_keeps_the_canned_sentence():
    """A fifth class the classifier grows before the message table does.

    It degrades to what every class said before this table existed, which is
    the one thing a park must never do: go quiet.
    """
    from jasper.control.audio_health import (
        PARKED_DETAIL,
        PARKED_HEADLINE,
        _state_issues,
        _transport_park_signal,
    )

    state = {
        "status": "parked",
        "parked": True,
        "parks": [{
            "park_class": "a_shape_with_no_message",
            "issue": "#9999",
            "remedy": None,
            "detail": "operator evidence",
        }],
    }
    signal = _transport_park_signal(state)
    assert signal["headline"] == PARKED_HEADLINE
    assert signal["detail"] == PARKED_DETAIL
    rows = _state_issues(
        {"warmup_active": True}, None, {}, {}, None, transport_park=state
    )
    assert [
        row["detail"]
        for row in rows
        if str(row["key"]).startswith("path.transport_park.")
    ] == [PARKED_DETAIL]
