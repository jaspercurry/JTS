# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The angle-capture seam: {per-driver | summed} x {angles} x {arm | human}.

Four things are pinned here, and the third is the one that matters most for
review: this feature is NOT a route around the retired lateral-walk statistic.

1. the angle round trip -- degrees in, degrees back out of the shipped derivation;
2. the seam's dispatch -- each combination resolves to the right program object,
   pose and advance policy, mutation-checked so a collapsed branch fails;
3. **the ruling** -- the seam never mints ``PHASE_LATERAL`` itself, so it cannot
   be a route back to the retired statistic;
4. mover parity, and the record/receipt shape the shipped consumers read;
5. the ELEVATION axis -- the same construction one plane over, its per-mover
   reach, and the one clause it adds to what a household reads;
6. the PROGRAM door -- a named table becomes a walk, in the table's order.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import measurement_programs as mp
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.contracts import (
    POLARITY_INVERTED,
    POLARITY_NORMAL,
)
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_CANDIDATE
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.programs import NoProgramForPhaseError
from jasper.audio_measurement import gating
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.active_speaker.crossover_v2.spatial import cloud_position_record

_SHIPPED_ANGLES = (0, 7, -7, 22, -22)
_FC_HZ = 2000.0
_ROLES_BANDS = (
    RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
    RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
)


# --------------------------------------------------------------------------- #
# 1. the one new primitive: degrees round-trip through the cm-primary pose
# --------------------------------------------------------------------------- #


def test_pose_at_angle_is_the_exact_inverse_of_position_angle_deg() -> None:
    """Every whole degree the seam accepts survives the round trip.

    This is the contract that lets degrees be an INPUT without minting a second
    statement of the geometry: the pose banks in centimetres, exactly as a
    hand-walked pose does, and reads back as the angle that was asked for.
    """
    for degrees in range(-ac.MAX_ANGLE_DEG, ac.MAX_ANGLE_DEG + 1):
        pose = ac.pose_at_angle(degrees)
        assert flow.position_angle_deg(pose) == degrees, degrees


def test_pose_at_angle_reproduces_the_shipped_bearings() -> None:
    """+-7 deg and +-22 deg land on the shipped walk's own offsets.

    The lateral table states 12 cm and 40 cm and `position_angle_deg` reads
    +-7 deg and +-22 deg off them; this seam asked in the opposite direction has
    to arrive at the same place, or the two vocabularies describe different
    poses. The small excess over 12.0 / 40.0 is the CHORD-VERSUS-ARC gap
    `position_angle_deg`'s own docstring names: a tape-measured lateral slide
    cuts the chord, while an angle is the constant-radius arc. Asserting the arc
    is correct here -- it is the geometry both the arm and a taut string have.
    """
    assert ac.pose_at_angle(7).offset_cm == pytest.approx(12.278, abs=0.01)
    assert ac.pose_at_angle(22).offset_cm == pytest.approx(40.403, abs=0.01)
    assert ac.pose_at_angle(-7).lateral_sign == -1
    assert ac.pose_at_angle(7).lateral_sign == 1
    assert ac.pose_at_angle(0).lateral_sign == 0


def test_pose_role_derives_from_the_shipped_wide_class() -> None:
    """`role` follows WIDE_OFFSET_MIN_CM rather than a second table.

    Pinned because the shipped cloud table assigns the same way (12/25 cm onax,
    40/60 cm offax) and a divergence here would make an angle-requested pose
    answer a different question than the hand-walked pose at the same place.
    """
    inside = ac.pose_at_angle(7)
    outside = ac.pose_at_angle(22)
    assert inside.offset_cm < flow.WIDE_OFFSET_MIN_CM <= outside.offset_cm
    assert inside.role == flow.POSITION_ROLE_ONAX and not inside.wide
    assert outside.role == flow.POSITION_ROLE_OFFAX and outside.wide


@pytest.mark.parametrize("bad", [90, -90, 81, -81, 180])
def test_pose_at_angle_refuses_an_unmeasurable_bearing(bad: int) -> None:
    """The tangent's own bound, refused loudly rather than banked absurdly."""
    with pytest.raises(flow.CrossoverV2FlowError, match="design axis"):
        ac.pose_at_angle(bad)


@pytest.mark.parametrize("bad", [7.5, "7"])
def test_angle_stop_refuses_a_non_whole_degree(bad: object) -> None:
    """Whole degrees is the resolution the placement is honest at."""
    with pytest.raises(flow.CrossoverV2FlowError, match="WHOLE degrees"):
        ac.AngleStop(angle_deg=bad, regime=ac.REGIME_PER_DRIVER)  # type: ignore[arg-type]


# --- the whole-degree contract binds EVERY door, not just two --------------- #
#
# The three request constructors used to coerce with ``int(a)`` BEFORE the
# validator ran, so `per_driver_at([0.4])` silently produced an on-axis capture.
# These pin all three doors against the truncation cases.

_DOORS = (
    pytest.param(lambda a: ac.per_driver_at([a]), id="per_driver_at"),
    pytest.param(lambda a: ac.summed_at([a]), id="summed_at"),
    pytest.param(lambda a: ac.both_at([a]), id="both_at"),
)
_TRUNCATING = [7.9, -7.9, 0.4, "45", None]


@pytest.mark.parametrize("door", _DOORS)
@pytest.mark.parametrize("bad", _TRUNCATING)
def test_every_door_refuses_a_non_whole_degree(door: object, bad: object) -> None:
    """No constructor rounds, truncates, or parses its way to an angle."""
    with pytest.raises(flow.CrossoverV2FlowError, match="WHOLE degrees"):
        door(bad)  # type: ignore[operator]


@pytest.mark.parametrize("door", _DOORS)
def test_a_fractional_angle_never_becomes_an_on_axis_capture(door: object) -> None:
    """The sharp row: 0.4 must REFUSE, never truncate to 0.

    Truncating 0.4 to 0 turns a request for a pose just off the design axis
    into an on-axis capture at ``offset_cm=0.0`` -- which also routes around
    `position_angle_deg`'s zero-sign guard, the one that exists to stop a pose
    recording "an offset the microphone never had". A silent 0 is therefore
    worse than a loud refusal, not a lenient version of it.
    """
    with pytest.raises(flow.CrossoverV2FlowError):
        door(0.4)  # type: ignore[operator]


@pytest.mark.parametrize("door", _DOORS)
def test_every_door_accepts_a_numpy_integer(door: object) -> None:
    """np.int64 is an integer, and arm/numpy-derived schedules produce them.

    Refusing it would push exactly those callers back into the `int()` coercion
    this contract removes. It normalizes to a plain `int` so no numpy scalar
    reaches a record or an equality check.
    """
    request = door(np.int64(45))  # type: ignore[operator]
    stop = ac.resolve_request(request)[0]
    assert stop.angle_deg == 45
    assert type(stop.angle_deg) is int
    assert flow.position_angle_deg(stop.prompt) == 45


@pytest.mark.parametrize("door", _DOORS)
@pytest.mark.parametrize("bad", [np.float64(45.0), True, False])
def test_every_door_refuses_floats_and_bools(door: object, bad: object) -> None:
    """np.float64 is not an integer; bool is one in Python and must still refuse.

    `True` would otherwise sail through as a perfectly valid +1 deg bearing --
    a real angle and an obvious caller error at the same time.
    """
    with pytest.raises(flow.CrossoverV2FlowError, match="WHOLE degrees"):
        door(bad)  # type: ignore[operator]


def test_angle_stop_and_pose_share_the_numpy_and_bool_rules() -> None:
    """The other two doors agree with the constructors -- one validator, not three."""
    assert ac.AngleStop(np.int64(22), ac.REGIME_SUMMED).angle_deg == 22
    assert flow.position_angle_deg(ac.pose_at_angle(np.int64(22))) == 22
    for bad in (np.float64(22.0), True, 22.5, "22"):
        with pytest.raises(flow.CrossoverV2FlowError, match="WHOLE degrees"):
            ac.AngleStop(bad, ac.REGIME_SUMMED)  # type: ignore[arg-type]
        with pytest.raises(flow.CrossoverV2FlowError, match="WHOLE degrees"):
            ac.pose_at_angle(bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 2. the seam's dispatch: program x angle x mover
# --------------------------------------------------------------------------- #


def test_regime_selects_the_program_phase() -> None:
    """Per-driver plays MEASURE's object; summed plays the position groups'.

    The mutation this guards: collapsing `_REGIME_PROGRAM_PHASE` to one arm.
    Both directions are asserted, so a collapse in either direction fails.
    """
    per_driver, = ac.resolve_request(ac.per_driver_at([7]))
    summed, = ac.resolve_request(ac.summed_at([7]))
    assert per_driver.program_phase == PHASE_MEASURE
    assert summed.program_phase == PHASE_CLOUD_VERIFY
    assert per_driver.program_phase != summed.program_phase


def test_program_for_stop_returns_the_shipped_object_by_identity() -> None:
    """A per-driver stop is handed the very SAME MEASURE object, not an equal one.

    Identity is the contract: a pose measured with a different sweep or at a
    different level makes every cross-angle comparison uninterpretable, which is
    why `program_for_phase` answers by identity and this delegates rather than
    branching.
    """
    check, measure, verify, cloud = object(), object(), object(), object()
    programs = {"check": check, "measure": measure, "verify": verify, "cloud": cloud}

    per_driver, summed = ac.resolve_request(ac.both_at([22]))
    assert ac.program_for_stop(per_driver, **programs) is measure
    assert ac.program_for_stop(summed, **programs) is cloud


def test_per_driver_stop_refuses_before_the_gain_solve() -> None:
    """No MEASURE program yet ⇒ the shipped refusal, uncaught."""
    stop, = ac.resolve_request(ac.per_driver_at([0]))
    with pytest.raises(NoProgramForPhaseError):
        ac.program_for_stop(
            stop, check=object(), measure=None, verify=object(), cloud=object(),
        )


def test_both_at_pairs_the_regimes_so_the_microphone_moves_once_per_angle() -> None:
    """Position-major: two regimes at one angle are ADJACENT stops."""
    stops = ac.resolve_request(ac.both_at([0, 7]))
    assert [(s.angle_deg, s.regime) for s in stops] == [
        (0, ac.REGIME_PER_DRIVER), (0, ac.REGIME_SUMMED),
        (7, ac.REGIME_PER_DRIVER), (7, ac.REGIME_SUMMED),
    ]
    assert [s.index for s in stops] == [1, 2, 3, 4]


def test_requested_angle_order_is_the_running_order() -> None:
    """The walk is the caller's order, indexed 1-based like the relay drives it."""
    stops = ac.resolve_request(ac.per_driver_at([0, 22, -7, 45]))
    assert [s.angle_deg for s in stops] == [0, 22, -7, 45]
    assert [s.index for s in stops] == [1, 2, 3, 4]


def test_arbitrary_angles_are_reachable() -> None:
    """The point of the seam: an angle outside the shipped six-pose table.

    45 deg is not expressible today -- `LATERAL_POSE_PROMPTS` is a fixed tuple
    derived from two hard-coded cm offsets behind an import-time length guard.
    """
    stop, = ac.resolve_request(ac.per_driver_at([45]))
    assert flow.position_angle_deg(stop.prompt) == 45
    shipped = {flow.position_angle_deg(p) for p in flow.LATERAL_POSE_PROMPTS}
    assert 45 not in shipped


def test_empty_and_unknown_requests_are_refused() -> None:
    with pytest.raises(flow.CrossoverV2FlowError, match="at least one stop"):
        ac.AngleCaptureRequest(stops=())
    with pytest.raises(flow.CrossoverV2FlowError, match="mover"):
        ac.AngleCaptureRequest(stops=(ac.AngleStop(0, ac.REGIME_SUMMED),), mover="robot")
    with pytest.raises(flow.CrossoverV2FlowError, match="regime"):
        ac.AngleStop(0, "sine")


# --------------------------------------------------------------------------- #
# 3. THE RULING: this is not a route around the paused lateral statistic
# --------------------------------------------------------------------------- #


def test_the_seam_never_mints_the_lateral_phase() -> None:
    """No stop is ever tagged PHASE_LATERAL, in any combination.

    What this pins is a SEPARATION OF CONCERNS, not the bar. This module answers
    "what does this stop play"; a session host answers "which phase runs at this
    index", and since #2732's take it does tag a staged walk's indexes
    PHASE_LATERAL. The bar is held one layer down, on that group's declared
    consumer -- see ``tests/test_crossover_v2_lateral_evidence.py``.
    """
    for request in (
        ac.per_driver_at(_SHIPPED_ANGLES),
        ac.summed_at(_SHIPPED_ANGLES),
        ac.both_at(_SHIPPED_ANGLES, mover=ac.MOVER_ARM),
    ):
        phases = {s.program_phase for s in ac.resolve_request(request)}
        assert PHASE_LATERAL not in phases
        assert PHASE_LATERAL not in set(ac.index_phase_map(request).values())


def test_the_seam_module_does_not_reference_phase_lateral_as_code() -> None:
    """Static backstop for the ruling in the module docstring.

    A future edit could have this module mint ``PHASE_LATERAL`` itself, which
    is exactly the coupling that would make it a route back to the retired
    statistic; this fails the moment the module names that symbol *as code*.

    **Parsed, never text-scanned** -- the discipline `test_lint_contracts.py`
    states for exactly this shape of rule: the module docstring discusses the
    name at length in prose (that discussion is the point), so a text scan
    would report the explanation as the violation. The AST sees only code, and
    prose is where the name belongs.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(ac.__file__).read_text(encoding="utf-8"))
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "PHASE_LATERAL" not in referenced
    # Positive control: the scan does see the names this module DOES use, so a
    # vacuous pass (an empty or mis-walked tree) cannot masquerade as a clean one.
    assert {"PHASE_MEASURE", "PHASE_CLOUD_VERIFY", "position_angle_deg"} <= referenced


# --------------------------------------------------------------------------- #
# 4. mover parity, and the shapes the shipped consumers read
# --------------------------------------------------------------------------- #


def test_mover_changes_the_advance_policy_and_nothing_else() -> None:
    """Same stops by arm and by hand: identical pose, prompt and program.

    "The remote tier is a different OPERATOR, not a different measurement",
    taken all the way: the ONLY difference between the two walks is how each
    stop begins. Everything a consumer reads -- the pose, its copy, the
    program -- is equal, so the two movers' evidence is comparable without a
    per-mover branch anywhere downstream.
    """
    angles = [0, 7, -22]
    by_hand = ac.resolve_request(ac.per_driver_at(angles, mover=ac.MOVER_HUMAN))
    by_arm = ac.resolve_request(ac.per_driver_at(angles, mover=ac.MOVER_ARM))

    for hand, arm in zip(by_hand, by_arm, strict=True):
        assert hand.angle_deg == arm.angle_deg
        assert hand.regime == arm.regime
        assert hand.program_phase == arm.program_phase
        assert hand.prompt == arm.prompt          # pose AND copy
        assert hand.screen != arm.screen          # ...only this differs
    # Stated as the whole-object claim too, so a field added to ResolvedStop
    # later cannot quietly become mover-dependent without failing here.
    assert [dataclasses.replace(s, screen={}) for s in by_hand] == [
        dataclasses.replace(s, screen={}) for s in by_arm
    ]


def test_the_string_and_protractor_combination_is_reachable() -> None:
    """Degrees PLUS a tap -- the combination no shipped tier can express.

    This is the weld this seam exists to break. In the shipped flow
    `_positioned_prompt` restates a pose as an angle only when
    `externally_positioned`, which also forces the countdown and the position
    gate: the tiers offer (centimetres, tap) or (degrees, gate), never
    (degrees, tap). The ratified household method -- a string swung to a
    protractor angle -- is exactly that third combination.
    """
    stop, = ac.resolve_request(ac.per_driver_at([22], mover=ac.MOVER_HUMAN))
    assert "22" in stop.prompt.headline                       # degrees...
    assert stop.screen["auto_advance"] == flow.AUTO_ADVANCE_TAP  # ...and a tap
    # The shipped hand-walked pose at the same place states centimetres instead.
    shipped_at_40cm = next(
        p for p in flow.LATERAL_POSE_PROMPTS if p.offset_cm == 40.0
    )
    assert "22" not in shipped_at_40cm.headline


def test_human_mover_taps_and_declares_no_position() -> None:
    """A person's tap IS the settle signal, and this REQUEST declares no target.

    Whether a person's walk is HELD is the session's fact, not the request's
    (``V2PlanShape.positions_gated``) -- a session that gates one builds its own
    entries through ``_entry_policy`` off its own shape. This bag is the dry
    run's, so guessing a target from the mover alone would be a second answer
    to a question this seam cannot see.
    """
    for stop in ac.resolve_request(ac.both_at([0, 22])):
        assert stop.screen == {"auto_advance": flow.AUTO_ADVANCE_TAP}
        assert flow.POSITION_DEG_KEY not in stop.screen


def test_arm_mover_pairs_the_countdown_with_the_position_gate() -> None:
    """An ARM's auto-advance and its target are emitted TOGETHER.

    A countdown without the gate fires into an arm still in motion. The
    converse is not a pair: a gate with no countdown is a person holding the
    tape, released by their own tap -- which is exactly the shape
    ``V2PlanShape.positions_gated`` exists to say apart from this one.
    """
    for stop in ac.resolve_request(ac.both_at([0, -22], mover=ac.MOVER_ARM)):
        assert stop.screen["auto_advance"] == flow.AUTO_ADVANCE_COUNTDOWN
        assert stop.screen["countdown_s"] == str(flow.AUTO_ADVANCE_COUNTDOWN_S)
        assert stop.screen[flow.POSITION_DEG_KEY] == str(stop.angle_deg)
        assert stop.screen[flow.POSITION_ROLE_KEY] == stop.prompt.role


def test_the_gate_angle_is_read_back_off_the_pose() -> None:
    """The number the gate acts on is the number the banked pose carries.

    Not copied from the request: one fact, one source. The round trip is what
    would otherwise hide a defect between them.
    """
    for stop in ac.resolve_request(ac.per_driver_at([0, 7, -7, 22, -22, 45],
                                                    mover=ac.MOVER_ARM)):
        assert int(stop.screen[flow.POSITION_DEG_KEY]) == flow.position_angle_deg(
            stop.prompt
        )


def test_a_resolved_stop_banks_in_the_shipped_record_shape() -> None:
    """A stop's pose feeds `cloud_position_record` unchanged.

    The receipt/banking contract: an angle-requested capture retains the same
    keys, the same `take_id` convention and the same derived `wide` as a
    hand-walked one, so one replay path covers both and the attribution stage
    reads them alike.
    """
    stop, = ac.resolve_request(ac.per_driver_at([22]))
    record = cloud_position_record(
        position_id="angle_01", phase="measure", index=stop.index, attempt=1,
        prompt=stop.prompt.text, wide=stop.prompt.wide, role=stop.prompt.role,
        geometry=flow.position_geometry(stop.prompt),
        captured_at=0.0, session_id="s", gate_window_ms=None,
        gate_floor_source=None, gate_disclosure=None, gate_moved_rms_db=None,
        gate_reflection_delay_ms=None,
        gate_entanglement_floor_hz=None,
        gate_entanglement_floor_source=gating.ENTANGLEMENT_SOURCE_UNKNOWN,
        validity_floor_hz=None,
        gating_applied=False, summed_ripple_db=None, glitch_detected=False,
        wav_sha256=None,
    )
    assert record["take_id"] == "angle_01_a01"
    assert record["wide"] is True
    assert record["role"] == flow.POSITION_ROLE_OFFAX
    assert record["prompt"] == stop.prompt.text and record["prompt"]
    # The bearing the stop was RESOLVED at is the bearing the record banks —
    # ONE derivation off the pose, so a staged angle walk cannot bank a spot
    # that disagrees with the one it asked for.
    assert record["position_deg"] == flow.position_angle_deg(stop.prompt) == 22
    assert record["position_axis"] == "horizontal"
    assert record["mark_distance_m"] == flow.MARK_DISTANCE_M


def test_announced_indexes_delegates_to_the_shipped_owner() -> None:
    """One owner for "what will the household hear".

    Empty for every request today, and the module docstring says why that is
    correct rather than an oversight: neither regime's program phase is a
    session opener, so an angle walk inside an announced session announces
    nothing -- the shipped behaviour for every capture after the first.
    """
    request = ac.both_at(_SHIPPED_ANGLES)
    assert ac.announced_indexes(request) == ()
    assert ac.announced_indexes(request) == flow.announced_capture_indexes(
        ac.index_phase_map(request)
    )


def test_a_resolved_stop_is_actually_frozen() -> None:
    """`frozen=True` means the screen bag too, not just the fields.

    A caller holding a resolved walk must not be able to edit the angle the
    position gate is waiting for. It still compares equal to a plain dict, so
    reading it is unchanged.
    """
    stop, = ac.resolve_request(ac.per_driver_at([7], mover=ac.MOVER_ARM))
    with pytest.raises(TypeError):
        stop.screen["position_deg"] = "45"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        stop.angle_deg = 45  # type: ignore[misc]
    assert stop.screen == dict(stop.screen)


def test_index_phase_map_matches_the_resolved_walk() -> None:
    """The map and the stops cannot describe different walks."""
    request = ac.both_at([0, 7], mover=ac.MOVER_ARM)
    stops = ac.resolve_request(request)
    assert ac.index_phase_map(request) == {
        s.index: s.program_phase for s in stops
    }
    assert sorted(ac.index_phase_map(request)) == list(range(1, len(stops) + 1))


def test_the_arc_removes_the_inverse_square_confound() -> None:
    """The ratified design's inverse-square argument, and its OPEN question.

    The argument: a 40 cm lateral slide off a 1 m mark puts the microphone
    107.7 cm out, ~0.64 dB of pure inverse-square level change with no
    acoustics in it. Stating a pose as an ANGLE is meant to make that confound
    structural rather than addressed in prose.

    **This docstring used to open "Every stop sits at the SAME radius", and the
    body below has always said otherwise** — it asserts
    ``radius == mark / cos(theta)``, which is 1.078 m at 22°, not constant.
    What the body pins is the TANGENT construction ``pose_at_angle`` actually
    performs; whether the physical rig swings a constant-radius arc is a
    hardware fact no test can settle, and the owner's tape measure decides it:
    `#2932 <https://github.com/jaspercurry/JTS/issues/2932>`_. The assertions
    are unchanged — only the sentence that contradicted them.
    """
    for degrees in (0, 7, -7, 22, -22, 45):
        pose = ac.pose_at_angle(degrees)
        radius_m = math.hypot(pose.offset_cm / 100.0, flow.MARK_DISTANCE_M)
        chord_radius_m = math.hypot(0.40, flow.MARK_DISTANCE_M)
        assert radius_m == pytest.approx(
            flow.MARK_DISTANCE_M / math.cos(math.radians(abs(degrees))), abs=1e-9
        )
        # the shipped 40 cm slide is the confound this replaces
        assert chord_radius_m == pytest.approx(1.077, abs=0.001)


# --------------------------------------------------------------------------- #
# 5. composing a walk INTO a session (#2732 P2)
# --------------------------------------------------------------------------- #
#
# The three refusals are properties of the PAIR (this walk, this session), which
# is why they live in the seam and not in the spool's document validation: the
# same document is fine against a differently-shaped session.


def _capture_ceiling() -> int:
    from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS

    return MAX_CAPTURE_PLAN_ATTEMPTS


def test_a_composed_walk_is_the_stops_in_order_as_poses() -> None:
    """The happy path: poses, in the request's stop order, in the vocabulary
    every shipped prompted walk is already stated in."""
    prompts = ac.session_lateral_walk(
        ac.per_driver_at(list(_SHIPPED_ANGLES)),
        externally_positioned=False,
        base_entries=3,
        plans_cloud_group=False,
    )
    assert len(prompts) == len(_SHIPPED_ANGLES)
    assert [flow.position_angle_deg(p) for p in prompts] == list(_SHIPPED_ANGLES)
    # Poses, not stops: what the plan builders and the conductor consume.
    assert all(isinstance(p, flow.CloudPositionPrompt) for p in prompts)


def test_a_summed_stop_refuses_rather_than_being_measured_per_driver() -> None:
    """A session lateral group plays MEASURE's per-driver object at every pose.

    Taking a summed stop would therefore bank a per-driver capture under a
    request for the system response — a silent narrowing, which is the one
    outcome worse than a refusal.
    """
    for request in (
        ac.summed_at([0]),
        ac.both_at([7]),  # mixed: one per-driver stop is not enough
    ):
        with pytest.raises(ac.LateralWalkRefused) as excinfo:
            ac.session_lateral_walk(
                request, externally_positioned=False, base_entries=3,
                plans_cloud_group=False,
            )
        assert excinfo.value.reason == ac.WALK_REGIME_UNSUPPORTED
        assert ac.REGIME_SUMMED in excinfo.value.detail


def test_a_mover_mismatch_refuses_in_both_directions() -> None:
    """The mover is the one axis a request and a session must already agree on.

    An arm walk in a hand-walked session auto-advances into a microphone nobody
    is moving; a hand walk in an arm session waits on a position gate no driver
    will satisfy. Both are stalls, so neither is silently coerced.
    """
    for mover, session_positioned in (
        (ac.MOVER_ARM, False),
        (ac.MOVER_HUMAN, True),
    ):
        with pytest.raises(ac.LateralWalkRefused) as excinfo:
            ac.session_lateral_walk(
                ac.per_driver_at([7], mover=mover),
                externally_positioned=session_positioned,
                base_entries=3,
                plans_cloud_group=False,
            )
        assert excinfo.value.reason == ac.WALK_MOVER_MISMATCH
    # ...and both matched pairs compose.
    for mover, session_positioned in (
        (ac.MOVER_ARM, True),
        (ac.MOVER_HUMAN, False),
    ):
        assert ac.session_lateral_walk(
            ac.per_driver_at([7], mover=mover),
            externally_positioned=session_positioned,
            base_entries=3,
            plans_cloud_group=False,
        )


@pytest.mark.parametrize("plans_cloud_group", [False, True])
def test_the_capacity_gate_admits_exactly_what_the_relay_accepts(
    plans_cloud_group: bool,
) -> None:
    """The gate's verdict IS the relay's, at every stop count, on both shapes.

    Stated as agreement with ``_validate_capture_plan`` rather than as its own
    arithmetic, because the first version of this gate had its own and was
    wrong in the direction that costs a capability: it added the geometry-retry
    budget unconditionally, while a plan only budgets geometry retakes when a
    cloud group is planned, so it refused the two largest LEGAL walks (23 and
    24 stops on the shipped cloud-less shape — the relay accepts both).

    **What this catches is DRIFT between the producer and its two readers**, and
    that is the whole of what it claims. Both sides here reach
    ``stage1_plan_max_attempts``, so a producer that became wrong in a way BOTH
    inherit would move them together and this would stay green. That case is
    carried by the two siblings below, which pin absolute numbers rather than
    agreement: ``…is_never_refused_for_capacity`` (24 stops fit at base 3) and
    ``…is_where_the_capacity_gate_bites`` (base 11, and 19 stops do not). Read
    the three together — this one says they agree, those two say about what.
    """
    from jasper.active_speaker.crossover_v2.sweep_spec import CaptureSpecError, _validate_capture_plan

    shape = flow.resolve_plan_shape(flow.TIER_FULL)
    base_entries = len(flow.build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=plans_cloud_group,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    ))

    def relay_takes(stops: int) -> bool:
        plan = flow.build_v2_capture_plan(
            _ROLES_BANDS, _FC_HZ, plan_shape=shape,
            include_cloud_measure=plans_cloud_group,
            include_lateral=True,
            include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            lateral_prompts=tuple(ac.pose_at_angle(d) for d in range(1, stops + 1)),
        )
        try:
            _validate_capture_plan(plan)
        except CaptureSpecError:
            return False
        return True

    def gate_takes(stops: int) -> bool:
        try:
            ac.session_lateral_walk(
                ac.per_driver_at(list(range(1, stops + 1))),
                externally_positioned=False,
                base_entries=base_entries,
                plans_cloud_group=plans_cloud_group,
            )
        except ac.LateralWalkRefused:
            return False
        return True

    # 1..30 covers both boundaries on both shapes.
    for stops in range(1, 31):
        assert gate_takes(stops) == relay_takes(stops), (
            f"gate and relay disagree at {stops} stops "
            f"(plans_cloud_group={plans_cloud_group})"
        )
    # ...and the boundary is really in range, so the loop is not vacuous.
    assert relay_takes(1) and not relay_takes(30)


def test_a_legal_staged_walk_is_never_refused_for_capacity() -> None:
    """The spool's own ceiling fits the shipped session exactly — and only just.

    ``MAX_STOPS`` is a wall-clock bound on the walk; the relay's blob-index
    space is a bound on the session it lands in. On the shipped stage-1 shape
    the two meet exactly at 24, so every walk an operator can legally stage is
    admitted, with ZERO slack: one more base entry and ``MAX_STOPS`` becomes
    unreachable. That is the number to re-derive if a fourth stage-1 capture is
    ever added, and this test is where it fails.
    """
    from jasper.active_speaker.angle_capture_spool import MAX_STOPS

    shape = flow.resolve_plan_shape(flow.TIER_FULL)
    base_entries = len(flow.build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    ))
    assert base_entries == 3

    fits = ac.session_lateral_walk(
        ac.per_driver_at(list(range(1, MAX_STOPS + 1))),
        externally_positioned=False,
        base_entries=base_entries,
        plans_cloud_group=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
    )
    assert len(fits) == MAX_STOPS
    # Zero slack: one more stop than the spool can bank would not fit.
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.session_lateral_walk(
            ac.per_driver_at(list(range(1, MAX_STOPS + 2))),
            externally_positioned=False,
            base_entries=base_entries,
            plans_cloud_group=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        )
    assert excinfo.value.reason == ac.WALK_OVER_CAPTURE_CAPACITY
    # The arithmetic rides in the message: the operator's only lever is to stage
    # fewer stops, and they need the numbers to pick a count.
    detail = excinfo.value.detail
    for number in (base_entries, MAX_STOPS + 1, _capture_ceiling()):
        assert str(number) in detail


def test_a_cloud_bearing_session_is_where_the_capacity_gate_bites() -> None:
    """The refusal is reachable, not theoretical — just not on the shipped shape.

    With the pre-apply cloud on, the base entries alone take 11 of the relay's
    32 indexes and the plan budgets geometry retakes too, so the walk that fits
    is far shorter than anything the spool would refuse to bank.

    Pinned from BOTH sides at the exact boundary (14 fits, 15 does not) rather
    than as "some long walk refuses". A one-directional refusal assertion is
    satisfied by ANY budget at least this tight, including a wrong one: a
    mutation that doubled the retake allowance left the one-sided version green,
    which is how this test came to have a lower edge.
    """
    shape = flow.resolve_plan_shape(flow.TIER_FULL)
    base_entries = len(flow.build_v2_cloud_index_phase_map(
        plan_shape=shape, include_cloud_measure=True, include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    ))
    assert base_entries == 11

    # 11 entries + 14 stops + 2 geometry retakes + 5 spare = 32, the ceiling.
    fits = ac.session_lateral_walk(
        ac.per_driver_at(list(range(1, 15))),
        externally_positioned=False,
        base_entries=base_entries,
        plans_cloud_group=True,
    )
    assert len(fits) == 14

    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.session_lateral_walk(
            ac.per_driver_at(list(range(1, 16))),
            externally_positioned=False,
            base_entries=base_entries,
            plans_cloud_group=True,
        )
    assert excinfo.value.reason == ac.WALK_OVER_CAPTURE_CAPACITY


def test_the_pose_record_states_the_seams_own_regime_word() -> None:
    """One vocabulary for "what was played", across an import cycle.

    ``spatial.lateral_pose_record`` writes the regime onto every banked pose,
    and it cannot import this module (this one imports the flow, and the flow
    imports that one). So the word is a literal there and this is the pin that
    keeps the two spellings one fact.
    """
    from jasper.active_speaker.crossover_v2.spatial import LATERAL_POSE_REGIME

    assert LATERAL_POSE_REGIME == ac.REGIME_PER_DRIVER


def test_composing_a_walk_returns_poses_and_no_journey_vocabulary() -> None:
    """Section 3's ruling, re-asserted over the NEW entry point.

    ``session_lateral_walk`` is the first function here whose whole purpose is
    to feed a measurement session, which is exactly the shape that would invite
    it to return indexed, phase-tagged stops. It returns POSES, and the caller
    tags indexes -- so the thing to assert is the RETURN TYPE, not the absence
    of a string.

    Asserting "no field contains 'lateral'" would be near-vacuous (a pose has
    no phase field to contain it); asserting the exact shipped type is what
    would fail if this ever grew a ``ResolvedStop`` or a ``(index, phase)``
    pair.
    """
    prompts = ac.session_lateral_walk(
        ac.per_driver_at([0, 22]), externally_positioned=False, base_entries=3,
        plans_cloud_group=False,
    )
    assert isinstance(prompts, tuple)
    assert [type(p) for p in prompts] == [flow.CloudPositionPrompt] * 2
    # A pose carries geometry and copy, and nothing that names a journey.
    assert not (
        {f.name for f in dataclasses.fields(flow.CloudPositionPrompt)}
        & {"phase", "index", "program_phase"}
    )


# --------------------------------------------------------------------------- #
# 5. the ELEVATION axis: the same construction, one plane over
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("angle_deg", [0, 22, -45])
def test_elevation_round_trips_and_leaves_the_bearing_alone(angle_deg: int) -> None:
    """The azimuth's own contract, asserted on the orthogonal axis.

    The two are INDEPENDENT numbers about one pose, so a compound pose reads
    both back: the elevation through the shipped `position_elevation_deg` and
    the bearing through `position_angle_deg`, neither disturbed by the other.
    A raised pose is not `POSITION_ROLE_XOVR` -- that role means a pose
    commanding no bearing at all, and this one commands one.
    """
    for elevation in range(-ac.MAX_ELEVATION_DEG, ac.MAX_ELEVATION_DEG + 1):
        pose = ac.pose_at_angle(angle_deg, elevation)
        assert flow.position_elevation_deg(pose) == elevation, elevation
        assert flow.position_angle_deg(pose) == angle_deg, elevation
        assert pose.role != flow.POSITION_ROLE_XOVR


@pytest.mark.parametrize(
    "mover, angle_deg, elevation_deg",
    [
        # The arm ROTATES about the rig's vertical axis and nothing on it
        # tilts, so ANY rise is past its reach -- including one asked at a
        # bearing it can serve.
        (ac.MOVER_ARM, 0, 1),
        (ac.MOVER_ARM, 22, -10),
        (ac.MOVER_ARM, ac.ARM_ENVELOPE_DEG + 1, 0),
        (ac.MOVER_HUMAN, 0, ac.MAX_ELEVATION_DEG + 1),
        (ac.MOVER_HUMAN, 7, -ac.MAX_ELEVATION_DEG - 1),
    ],
)
def test_a_stop_past_a_movers_reach_on_either_axis_refuses_at_staging(
    mover: str, angle_deg: int, elevation_deg: int,
) -> None:
    """Reach is per-mover AND per-axis, judged where the walk is STATED.

    Refused at statement time for the reason `MOVER_MAX_ANGLE_DEG` already
    gives about the bearing: a live session would publish a target this mover
    cannot reach and then spend its whole hold budget per stop waiting for a
    report that cannot come.
    """
    with pytest.raises(ac.LateralWalkRefused) as caught:
        ac.AngleCaptureRequest(
            stops=(ac.AngleStop(angle_deg, ac.REGIME_PER_DRIVER, elevation_deg),),
            mover=mover,
        )
    assert caught.value.reason == ac.WALK_OVER_MOVER_ENVELOPE


@pytest.mark.parametrize(
    "elevation_deg",
    [0, 7, -7, ac.MAX_ELEVATION_DEG, -ac.MAX_ELEVATION_DEG],
)
def test_a_person_may_be_asked_to_raise_within_reach(elevation_deg: int) -> None:
    """Everything inside the person's own bound stages AND resolves.

    The bound covers the plan's baseline vertical walk with margin, and the
    resolved stop carries the elevation on BOTH statements of it -- its own
    field and the pose it resolved to -- so the number a session gates on is
    the number the request asked for.
    """
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(22, ac.REGIME_PER_DRIVER, elevation_deg),),
        mover=ac.MOVER_HUMAN,
    )
    stop, = ac.resolve_request(request)

    assert stop.elevation_deg == elevation_deg
    assert flow.position_elevation_deg(stop.prompt) == elevation_deg
    assert flow.position_angle_deg(stop.prompt) == 22


@pytest.mark.parametrize("angle_deg", [0, 7, -22])
def test_a_pose_at_mark_height_is_the_pose_the_seam_already_shipped(
    angle_deg: int,
) -> None:
    """Elevation 0 changes NOTHING, field for field.

    Additive on the whole pose rather than only on the copy: an operator's
    horizontal walk composes exactly what it composed before this axis was
    sayable, so nothing downstream that reads a pose can tell the two apart --
    and the household is told about a rise it was never asked to make.
    """
    pose = ac.pose_at_angle(angle_deg)

    assert ac.pose_at_angle(angle_deg, 0) == pose
    assert (pose.vertical_sign, pose.vertical_offset_cm) == (0, 0.0)
    assert "mark height" not in pose.headline


@pytest.mark.parametrize("elevation_deg, word", [(10, "ABOVE"), (-10, "BELOW")])
def test_a_raised_pose_gains_exactly_one_elevation_clause(
    elevation_deg: int, word: str,
) -> None:
    """The composed household sentence, asserted whole.

    Prompt copy IS the externally observable behaviour here -- it is the
    instruction a person follows, and there is no structured field between
    them and it. The clause EXTENDS the shipped bearing sentence rather than
    replacing it or adding a second one: a household asked to swing and to
    rise gets one instruction, not two that could be followed in either order.
    """
    flat = ac.pose_at_angle(22)
    raised = ac.pose_at_angle(22, elevation_deg)

    assert flat.headline == (
        "Turn the microphone to +22° (22° RIGHT of the design axis)."
    )
    assert raised.headline == (
        "Turn the microphone to +22° (22° RIGHT of the design axis), and "
        f"{abs(elevation_deg)}° {word} mark height."
    )
    # The supporting clause is about DISTANCE and is unchanged by a rise.
    assert raised.detail == flat.detail


@pytest.mark.parametrize("elevation_deg, word", [(10, "ABOVE"), (-10, "BELOW")])
def test_a_rise_on_the_design_axis_does_not_say_LEAVE_the_microphone(
    elevation_deg: int, word: str,
) -> None:
    """A stand-still verb in front of a move instruction is a contradiction.

    The 0° bearing's shipped copy is "LEAVE the microphone on the design axis",
    which is a whole instruction on its own: do not move it. A pose that also
    asks for a rise has to state the bearing as something to HOLD instead, or
    the household is told not to move and then to move in one sentence.
    """
    assert ac.pose_at_angle(0).headline == (
        "Leave the microphone on the design axis (0°)."
    )
    assert ac.pose_at_angle(0, elevation_deg).headline == (
        "Keep the microphone on the design axis (0°), and "
        f"{abs(elevation_deg)}° {word} mark height."
    )


# --------------------------------------------------------------------------- #
# 6. the program door: a named table becomes a walk, and nothing else does
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "program",
    [
        mp.program("baseline", "express"),
        mp.program("baseline", "full"),
        mp.spot_program(22, 10),
    ],
    ids=["baseline/express", "baseline/full", "spot"],
)
def test_a_program_becomes_its_own_walk_in_table_order(
    program: mp.MeasurementProgram,
) -> None:
    """The table IS the walk: order, repeats, regime and provenance.

    Asserted against the program's own derived counts rather than against
    transcribed numbers, so the table stays the single owner of the geometry
    and this test cannot drift from it.
    """
    request = ac.request_for_program(program)

    assert len(request.stops) == program.capture_count
    assert {(s.angle_deg, s.elevation_deg) for s in request.stops} == {
        (p.azimuth_deg, p.elevation_deg) for p in program.poses
    }
    assert len({(s.angle_deg, s.elevation_deg) for s in request.stops}) == (
        program.mic_move_count
    )
    assert all(stop.regime == ac.REGIME_PER_DRIVER for stop in request.stops)
    # Table order, with each pose's repeats ADJACENT: the microphone moves once
    # per distinct pose, so a repeat that drifted apart would be a second trip.
    assert [(s.angle_deg, s.elevation_deg) for s in request.stops] == [
        (pose.azimuth_deg, pose.elevation_deg)
        for pose in program.poses
        for _ in range(pose.repeats)
    ]
    assert request.program == (
        "spot" if program.program_id == "spot"
        else f"{program.program_id}/{program.size}"
    )


def test_a_program_beyond_the_arms_reach_refuses_at_statement_time() -> None:
    """A program says WHERE to measure; the mover says what it can reach."""
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.request_for_program(
            mp.program("baseline", "express"), mover=ac.MOVER_ARM,
        )

    assert excinfo.value.reason == ac.WALK_OVER_MOVER_ENVELOPE


# --------------------------------------------------------------------------- #
# 8. the candidate cycle: what a stop measures, and what it may not play
# --------------------------------------------------------------------------- #


def _fake_preset(*, upper: str = "tweeter") -> object:
    """A preset shaped only as ``preset_crossover_geometry`` reads one."""
    return SimpleNamespace(crossover_regions=(SimpleNamespace(
        fc_hz=2000.0,
        target_type="LinkwitzRiley",
        order=4,
        lower_driver="woofer",
        upper_driver=upper,
    ),))


def _fake_candidate(
    *, linearization: dict | None = None, preset: object | None = None,
    polarity: str | None = None, delay_role: str | None = None,
    delay_us: float | None = None,
) -> object:
    return SimpleNamespace(
        fingerprint="fp-a",
        linearization=linearization or {},
        source_preset=_fake_preset() if preset is None else preset,
        alignment=SimpleNamespace(
            polarity=polarity, delay_role=delay_role, delay_us=delay_us,
        ),
    )


@pytest.mark.parametrize(
    ("program", "candidates"),
    [
        (mp.program("tournament", "express"), ()),
        (mp.program("tournament", "express"), ("fp-a", "fp-b")),
        (mp.program("tournament", "full"), ("fp-a", "fp-b", "fp-c")),
        (mp.program("baseline", "express"), ("fp-a", "fp-b")),
    ],
    ids=["no-cycle", "one-pose", "three-poses", "with-repeats"],
)
def test_candidates_expand_pose_major_candidate_minor(
    program: mp.MeasurementProgram, candidates: tuple[str, ...],
) -> None:
    """A cycle costs CAPTURES and never travel.

    The variants at one pose are adjacent stops, so the microphone still moves
    once per distinct pose and two candidates are only ever compared from the
    same place.
    """
    request = ac.request_for_program(program, candidates=candidates)
    cycle = candidates or ("",)

    assert len(request.stops) == program.capture_count * len(cycle)
    # Candidate-minor: the cycle repeats intact under every capture.
    assert [s.candidate_id for s in request.stops] == (
        list(cycle) * program.capture_count
    )
    # Pose-major: one contiguous run per table row, so nothing walks twice.
    assert len(request.stops) > 0
    runs = [
        key for key, _ in itertools.groupby(
            (s.angle_deg, s.elevation_deg) for s in request.stops
        )
    ]
    assert len(runs) == len(program.poses)
    assert len(set(runs)) == program.mic_move_count


def test_a_candidate_implies_the_alignment_axes_a_measure_pose_can_play() -> None:
    """The MEASURE graph carries alignment and nothing else, so that is all a
    banked candidate may change about a stop -- and the flipped branch is the
    region's upper driver, the convention the candidate was minted under."""
    axes = ac.candidate_measure_axes(
        _fake_candidate(polarity="invert", delay_role="woofer", delay_us=250.0)
    )

    assert axes == {
        "polarity": POLARITY_INVERTED,
        "inverted_role": "tweeter",
        "delayed_role": "woofer",
        "delay_us": 250.0,
    }
    # A candidate minted with no alignment plays the ordinary graph.
    assert ac.candidate_measure_axes(_fake_candidate())["polarity"] == (
        POLARITY_NORMAL
    )


def test_a_branch_no_measurement_graph_carries_refuses_by_name() -> None:
    """The flipped branch is read off the candidate's own crossover, so a
    region whose upper driver is not a branch the graph carries would reach
    ``MeasureSpec`` as a pair it refuses."""
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.candidate_measure_axes(
            _fake_candidate(polarity="invert", preset=_fake_preset(upper="horn"))
        )

    assert excinfo.value.reason == ac.WALK_CANDIDATE_NOT_MEASURABLE


def test_a_polarity_only_candidate_states_no_half_delay() -> None:
    """The candidate model allows ``(0.0, role)`` and ``MeasureSpec`` refuses
    it, so an alignment that delays nothing must reach the spec naming nothing
    -- otherwise a walk stages and then dies at the open with its document
    already consumed."""
    axes = ac.candidate_measure_axes(
        _fake_candidate(polarity="invert", delay_role="woofer", delay_us=0.0)
    )

    assert (axes["delayed_role"], axes["delay_us"]) == ("", 0.0)
    # The whole point: the spec these axes are for accepts them.
    spec = MeasureSpec(kind=MEASURE_KIND_CANDIDATE, **axes)
    assert (spec.polarity, spec.inverted_role) == (POLARITY_INVERTED, "tweeter")


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (
            _fake_candidate(linearization={"tweeter": {"filters": [{}]}}),
            ac.WALK_CANDIDATE_NOT_MEASURABLE,
        ),
        (
            _fake_candidate(delay_role="horn", delay_us=250.0),
            ac.WALK_CANDIDATE_NOT_MEASURABLE,
        ),
        (
            _fake_candidate(preset=SimpleNamespace(crossover_regions=())),
            ac.WALK_CANDIDATE_NOT_MEASURABLE,
        ),
    ],
    ids=["linearization", "unknown-delayed-branch", "unreadable"],
)
def test_a_candidate_this_graph_cannot_play_refuses_in_the_walk_vocabulary(
    candidate: object, reason: str,
) -> None:
    """Both refusals are walk refusals, so an adopting session and the staging
    door report them under one vocabulary."""
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.candidate_measure_axes(candidate)

    assert excinfo.value.reason == reason
    assert reason in ac.WALK_REFUSAL_REASONS
