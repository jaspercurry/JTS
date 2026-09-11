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
6. the PROGRAM door -- a named table becomes a walk, in the table's order;
7. CATEGORIZED poses -- a seat or close take says where it was stated from,
   and every bearing resolves byte-identically to before they existed.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from dataclasses import replace
import math
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import angle_capture_spool as spool
from jasper.active_speaker import measurement_programs as mp
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    POSITION_BATCH_CONFIG_KEY,
    POSITION_BATCH_SIZE_KEY,
    POSITION_BATCH_START_KEY,
)
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
    POLARITY_INVERTED,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
from jasper.active_speaker.crossover_v2.position_gate import PositionGate
from jasper.active_speaker.crossover_v2.programs import NoProgramForPhaseError
from jasper.audio_measurement import gating
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.active_speaker.crossover_v2.spatial import (
    cloud_position_record,
    pose_kind_fields,
)
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.cli import angle_capture as cli
from jasper.cli import measure as measure_cli
from tests.crossover_v2_banked_round import bank_seat_round

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
    with pytest.raises(flow.CrossoverV2FlowError):
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
    with pytest.raises(flow.CrossoverV2FlowError):
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
    with pytest.raises(flow.CrossoverV2FlowError):
        door(bad)  # type: ignore[operator]


def test_angle_stop_and_pose_share_the_numpy_and_bool_rules() -> None:
    """The other two doors agree with the constructors -- one validator, not three."""
    assert ac.AngleStop(np.int64(22), ac.REGIME_SUMMED).angle_deg == 22
    assert flow.position_angle_deg(ac.pose_at_angle(np.int64(22))) == 22
    for bad in (np.float64(22.0), True, 22.5, "22"):
        with pytest.raises(flow.CrossoverV2FlowError):
            ac.AngleStop(bad, ac.REGIME_SUMMED)  # type: ignore[arg-type]
        with pytest.raises(flow.CrossoverV2FlowError):
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
    """The walk is the caller's order, indexed 1-based like the capture drives it."""
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
    """Sessions without summed support refuse summed and mixed requests."""
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
def test_the_capacity_gate_admits_exactly_what_the_plan_accepts(
    plans_cloud_group: bool,
) -> None:
    from jasper.active_speaker.crossover_v2.sweep_spec import CaptureSpecError, _validate_capture_plan

    shape = flow.resolve_plan_shape(flow.TIER_FULL)
    base_entries = len(flow.build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=plans_cloud_group,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    ))

    def capture_takes(stops: int) -> bool:
        plan = flow.build_v2_capture_plan(
            _ROLES_BANDS, _FC_HZ, plan_shape=shape,
            include_cloud_measure=plans_cloud_group,
            include_lateral=True,
            include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            lateral_prompts=tuple(ac.pose_at_angle(0) for _ in range(stops)),
        )
        try:
            _validate_capture_plan(plan)
        except CaptureSpecError:
            return False
        return True

    def gate_takes(stops: int) -> bool:
        try:
            ac.session_lateral_walk(
                ac.per_driver_at([0] * stops),
                externally_positioned=False,
                base_entries=base_entries,
                plans_cloud_group=plans_cloud_group,
            )
        except ac.LateralWalkRefused:
            return False
        return True

    for stops in (1, 24, 33, 99, 110, 111, 120, 121, 128, 129):
        assert gate_takes(stops) == capture_takes(stops), (
            f"gate and plan disagree at {stops} stops "
            f"(plans_cloud_group={plans_cloud_group})"
        )
    # ...and the boundary is really in range, so the loop is not vacuous.
    assert capture_takes(1) and not capture_takes(140)


def test_a_cloud_bearing_session_is_where_the_capacity_gate_bites() -> None:
    shape = flow.resolve_plan_shape(flow.TIER_FULL)
    base_entries = len(flow.build_v2_cloud_index_phase_map(
        plan_shape=shape, include_cloud_measure=True, include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    ))
    assert base_entries == 11

    fits = ac.session_lateral_walk(
        ac.per_driver_at([0] * 110),
        externally_positioned=False,
        base_entries=base_entries,
        plans_cloud_group=True,
    )
    assert len(fits) == 110

    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.session_lateral_walk(
            ac.per_driver_at([0] * 111),
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

    It also states the rise as a LENGTH in the table's own both-units register,
    because the person holding the microphone has a tape measure and no
    protractor -- and it names the mark distance the conversion assumes, since
    the same angle is a different height at any other distance.
    """
    flat = ac.pose_at_angle(22)
    raised = ac.pose_at_angle(22, elevation_deg)

    assert flat.headline == (
        "Turn the microphone to +22° (22° RIGHT of the design axis)."
    )
    assert raised.headline == (
        "Turn the microphone to +22° (22° RIGHT of the design axis), and "
        f"{abs(elevation_deg)}° {word} mark height — that is 7 in (18 cm) "
        "at the declared 1 m."
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
    assert ac.pose_at_angle(0, elevation_deg).headline.startswith(
        "Keep the microphone on the design axis (0°), and "
        f"{abs(elevation_deg)}° {word} mark height"
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


@pytest.mark.parametrize("raw,cycle", [
    (None, ()),
    ("base", ("",)),
    (" base, fp-a,fp-b ", ("", "fp-a", "fp-b")),
    ("fp-a,fp-b", ("fp-a", "fp-b")),
])
def test_cli_resolves_only_banked_members_and_preserves_summed_base(monkeypatch, raw, cycle):
    resolved = []
    monkeypatch.setattr(cli, "find_banked_candidate", lambda fingerprint: resolved.append(fingerprint))
    argv = ["plan", "--program", "tournament", "--size", "full"]
    if raw is not None:
        argv += ["--candidates", raw]
    request = cli._build_request(cli.build_parser().parse_args(argv))
    program = mp.program("tournament", "full")

    assert resolved == [fingerprint for fingerprint in cycle if fingerprint]
    assert [stop.candidate_id for stop in request.stops] == list(cycle or ("",)) * program.capture_count
    assert {stop.regime for stop in request.stops} == {
        ac.REGIME_SUMMED if cycle else ac.REGIME_PER_DRIVER,
    }


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
    assert {stop.regime for stop in request.stops} == {
        ac.REGIME_SUMMED if candidates else ac.REGIME_PER_DRIVER,
    }


def _candidate_batch_plan():
    request = ac.request_for_program(
        mp.program("tournament", "full"), candidates=("", "fp-a", "fp-b"),
    )
    prompts = ac.session_lateral_walk(
        request, externally_positioned=False, base_entries=2,
        plans_cloud_group=False, supported_summed_candidates=True,
    )
    return flow.build_v2_session_spec(
        _ROLES_BANDS, _FC_HZ,
        acknowledgement_binding="candidate-batch-test",
        plan_shape=dataclasses.replace(flow.resolve_plan_shape("full"), hand_released_positions=True),
        include_lateral=True, include_cloud_measure=False,
        lateral_prompts=prompts,
        lateral_candidate_ids=tuple(stop.candidate_id for stop in request.stops),
    ).capture_plan


@pytest.mark.parametrize("candidates", [("",), ("", "fp-a", "fp-b"), ("fp-a",)])
def test_summed_candidate_walk_requires_the_supported_execution_path(candidates):
    request = ac.request_for_program(mp.program("tournament", "express"), candidates=candidates)
    with pytest.raises(ac.LateralWalkRefused) as exc:
        ac.session_lateral_walk(
            request, externally_positioned=False, base_entries=2, plans_cloud_group=False,
        )
    assert exc.value.reason == ac.WALK_REGIME_UNSUPPORTED
    assert len(ac.session_lateral_walk(
        request, externally_positioned=False, base_entries=2, plans_cloud_group=False,
        supported_summed_candidates=True,
    )) == len(request.stops)


def test_three_configs_at_three_poses_use_three_placement_grants():
    entries = [entry for entry in _candidate_batch_plan().entries if entry.kind_label == "lateral"]
    gate = PositionGate()
    grants = []
    for offset, entry in enumerate(entries):
        index = entry.index + 1
        if offset % 3 == 0:
            with pytest.raises(CaptureBeginDeferred):
                gate.gate(index, index, entry)
            pending = gate.pending()
            grants.append((pending["degrees"], pending["vertical_deg"]))
            gate.release(**{name: pending["action"]["body"][name] for name in ("index", "attempt")})
        gate.gate(index, index, entry)
        assert entry.screen[POSITION_BATCH_CONFIG_KEY] == str(offset % 3 + 1)
        assert entry.screen[POSITION_BATCH_SIZE_KEY] == "3"
        assert entry.screen[POSITION_BATCH_START_KEY] == str(index - offset % 3)
        assert entry.screen["candidate_id"] == ("", "fp-a", "fp-b")[offset % 3]
        if offset % 3:
            assert entry.screen["auto_advance"] == flow.AUTO_ADVANCE_COUNTDOWN
    assert len(grants) == len(set(grants)) == 3


def test_a_retake_or_recovery_needs_a_new_grant_and_rejects_stale_actions():
    first, second, third = [
        entry for entry in _candidate_batch_plan().entries if entry.kind_label == "lateral"
    ][:3]
    gate = PositionGate()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(3, 3, first)
    gate.release(3, 3)
    gate.gate(3, 3, first)
    gate.gate(4, 4, second)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(4, 5, second)
    assert gate.pending()["hand_released"] is True
    for index, attempt in ((3, 3), (4, 4), (4, None)):
        with pytest.raises(ValueError):
            gate.release(index, attempt)
    assert gate.pending()["attempt"] == 5
    gate.release(4, 5)
    gate.gate(4, 5, second)
    gate.abandon_hold()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(4, 5, second)
    gate.release(4, 5)
    gate.abandon_hold()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(5, 6, third)


# --------------------------------------------------------------------------- #
# 7. categorized poses: a seat is stated from the head, a close from the baffle
# --------------------------------------------------------------------------- #


@pytest.fixture
def spool_slot(tmp_path, monkeypatch):
    """A writable pending slot, and an idle speaker.

    Same shape and same reason as the take suite's: without the redirects a
    staged document would land in the real ``/var/lib/jasper`` and read
    whatever measurement state the machine running the suite happens to hold.
    """
    spool.set_angle_request_spool_path_for_tests(tmp_path / "angle_request.json")
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        tmp_path / "session_volume.json",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.read_measurement_hold", lambda: None,
    )
    try:
        yield
    finally:
        spool.set_angle_request_spool_path_for_tests(None)


@pytest.mark.parametrize(
    ("program_id", "size"),
    [("seat", "cube"), ("seat", "express"), ("close", "spot")],
    ids=["seat/cube", "seat/express", "close/spot"],
)
def test_a_categorized_program_walks_summed_whatever_the_candidates_say(
    program_id: str, size: str,
) -> None:
    """The room is measured THROUGH the speaker stage it sits on.

    So a seat or close pose is a SUMMED capture even with no candidate named,
    which for a bearing selects per-driver. The category, the standoff and the
    head offset ride from the table's pose onto the stop unchanged.
    """
    program = mp.program(program_id, size)
    request = ac.request_for_program(program, candidates=())

    assert {stop.regime for stop in request.stops} == {ac.REGIME_SUMMED}
    assert [(s.kind, s.distance_m, s.seat_offset_m) for s in request.stops] == [
        (p.kind, p.distance_m, p.seat_offset_m) for p in program.poses
    ]
    price = ac.walk_price(request)
    assert (price["mic_moves"], price["captures"]) == (
        program.mic_move_count, program.capture_count,
    )


@pytest.mark.parametrize(
    "stop",
    [
        ac.AngleStop(
            0, ac.REGIME_SUMMED,
            kind=mp.POSE_KIND_SEAT, seat_offset_m=(0.0, 0.0, 0.0),
        ),
        ac.AngleStop(0, ac.REGIME_SUMMED, kind=mp.POSE_KIND_CLOSE, distance_m=0.3),
    ],
    ids=["seat", "close"],
)
def test_an_arm_reaches_bearings_at_the_mark_and_nothing_else(
    stop: ac.AngleStop,
) -> None:
    """The arm TURNS at the mark, so no pose stated from anywhere else is its.

    Refused at statement time, in the same words a bearing past its envelope
    is refused in -- a person walks the same stop without argument.
    """
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.AngleCaptureRequest(stops=(stop,), mover=ac.MOVER_ARM)

    assert excinfo.value.reason == ac.WALK_OVER_MOVER_ENVELOPE
    assert ac.AngleCaptureRequest(stops=(stop,), mover=ac.MOVER_HUMAN).stops == (stop,)


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "nearfield"},
        {"kind": mp.POSE_KIND_SEAT},
        {"seat_offset_m": (0.0, 0.0, 0.0)},
        {"kind": mp.POSE_KIND_SEAT, "seat_offset_m": (0.0, 0.0)},
        {"distance_m": 0},
        {"distance_m": -1},
    ],
    ids=[
        "unknown-kind", "seat-with-no-offset", "bearing-with-an-offset",
        "an-offset-of-two", "no-distance", "a-distance-behind-the-speaker",
    ],
)
def test_a_stop_refuses_a_kind_it_cannot_state(fields: dict) -> None:
    """A stop states a place completely or refuses -- never half of one."""
    with pytest.raises(flow.CrossoverV2FlowError):
        ac.AngleStop(0, ac.REGIME_SUMMED, **fields)


def test_a_seat_stop_is_stated_from_the_head_not_the_mark() -> None:
    """Seven places around one head, each its own sentence and no mark at all.

    A seat pose has no bearing to be at, so ``at_mark`` is false for every one
    of them and the geometry claims no mark distance -- the take record says
    where the head was instead.
    """
    program = mp.program("seat", "cube")
    stops = ac.resolve_request(ac.request_for_program(program))

    assert len(stops) == len({stop.prompt.text for stop in stops}) == 7
    assert not any(stop.prompt.at_mark for stop in stops)
    for stop, pose in zip(stops, program.poses):
        geometry = flow.position_geometry(stop.prompt)
        assert (geometry.kind, geometry.degrees, geometry.mark_distance_m) == (
            mp.POSE_KIND_SEAT, 0, None,
        )
        assert geometry.seat_offset_m == pose.seat_offset_m


def test_a_close_stop_is_a_bearing_at_its_own_distance() -> None:
    """The close reference is on the design axis, at a standoff it declares."""
    stop, = ac.resolve_request(ac.request_for_program(mp.program("close", "spot")))
    geometry = flow.position_geometry(stop.prompt)

    assert (geometry.kind, geometry.degrees, geometry.seat_offset_m) == (
        mp.POSE_KIND_CLOSE, 0, None,
    )
    assert geometry.mark_distance_m == mp.CLOSE_DISTANCE_M
    assert flow.position_angle_deg(stop.prompt) == 0


#: The four sentences ``baseline/express`` prompts, transcribed from a walk
#: resolved BEFORE poses had a kind. Copy is the half of a walk a person acts
#: on, so it is stated here literally rather than re-derived.
_ON_AXIS = (
    "Leave the microphone on the design axis (0°). "
    "On the mark, 1 m out, pointed at the speaker."
)
_LEFT_20 = (
    "Turn the microphone to -20° (20° LEFT of the design axis). "
    "Keep it 1 m from the speaker and pointed at it."
)
_RIGHT_20 = (
    "Turn the microphone to +20° (20° RIGHT of the design axis). "
    "Keep it 1 m from the speaker and pointed at it."
)
_RAISED = (
    "Keep the microphone on the design axis (0°), and 10° {word} mark "
    "height — that is 7 in (18 cm) at the declared 1 m. "
    "On the mark, 1 m out, pointed at the speaker."
)

#: ``baseline/express``, as it resolved before ADR-0260's poses existed:
#: ``(angle_deg, elevation_deg, prompt text, degrees, vertical_deg)`` per stop,
#: in walk order and with the anchor's four repeats spelled out.
_GOLDEN_BASELINE_EXPRESS = (
    (0, 0, _ON_AXIS, 0, 0),
    (0, 0, _ON_AXIS, 0, 0),
    (0, 0, _ON_AXIS, 0, 0),
    (0, 0, _ON_AXIS, 0, 0),
    (-20, 0, _LEFT_20, -20, 0),
    (20, 0, _RIGHT_20, 20, 0),
    (0, -10, _RAISED.format(word="BELOW"), 0, -10),
    (0, 10, _RAISED.format(word="ABOVE"), 0, 10),
)


@pytest.mark.parametrize(
    ("candidates", "regime", "phase", "price"),
    [
        ((), ac.REGIME_PER_DRIVER, PHASE_MEASURE,
         {"mic_moves": 5, "captures": 8, "ceiling_min": 46,
          "stimulus_s": None}),
        (("", "fpA"), ac.REGIME_SUMMED, PHASE_CLOUD_VERIFY,
         {"mic_moves": 5, "captures": 16, "ceiling_min": 60,
          "stimulus_s": None}),
    ],
    ids=["no-cycle", "two-candidates"],
)
def test_the_shipped_programs_resolve_exactly_as_before(
    candidates: tuple[str, ...], regime: str, phase: str, price: dict,
) -> None:
    """The pose category is ADDITIVE: every bearing walk is what it always was.

    Transcribed from a walk captured before poses had a kind -- copy, order,
    repeats, advance policy, geometry and price -- so a categorized pose that
    leaked into the bearing path fails here rather than in a household's
    prompt. A bearing's geometry adds NO keys to the take record either.
    """
    request = ac.request_for_program(
        mp.program("baseline", "express"), candidates=candidates,
    )
    stops = ac.resolve_request(request)
    geometries = [flow.position_geometry(stop.prompt) for stop in stops]

    assert [
        (stop.angle_deg, stop.elevation_deg, stop.regime, stop.program_phase,
         dict(stop.screen), stop.prompt.text,
         geometry.axis, geometry.degrees, geometry.mark_distance_m,
         geometry.vertical_deg)
        for stop, geometry in zip(stops, geometries)
    ] == [
        (angle, elevation, regime, phase, {"auto_advance": "tap"}, text,
         "horizontal", degrees, 1.0, vertical)
        for angle, elevation, text, degrees, vertical in _GOLDEN_BASELINE_EXPRESS
        # Candidate-MINOR: the cycle repeats under each pose, in place.
        for _candidate in (candidates or ("",))
    ]
    assert [pose_kind_fields(geometry) for geometry in geometries] == [{"mark_distance_m": 1.0}] * len(stops)
    assert ac.walk_price(request) == price


def test_a_bearing_walk_stages_the_document_it_always_did(spool_slot) -> None:
    """The spooled stop is additive too, and reads back as what was staged.

    A bearing's entry carries the four keys it always carried; a categorized
    one adds ONLY what is true of it -- a seat has no standoff, a close has no
    head offset -- and both survive the round trip through the document.
    """
    bearing_keys = {"angle_deg", "regime", "elevation_deg", "candidate_id", "purpose"}
    for program_id, size, extra in (
        ("baseline", "express", set()),
        ("seat", "cube", {"kind", "seat_offset_m"}),
        ("close", "spot", {"kind", "distance_m"}),
    ):
        request = ac.request_for_program(mp.program(program_id, size))
        spool.stage_angle_request(request)
        document = json.loads(
            spool.angle_request_spool_path().read_text(encoding="utf-8")
        )

        assert [set(entry) for entry in document["stops"]] == (
            [bearing_keys | extra] * len(request.stops)
        )
        assert spool.peek_staged_angle_request().stops == request.stops
        taken = spool.take_staged_angle_request()
        assert taken.stops == request.stops
        assert all(
            stop.seat_offset_m is None
            or all(isinstance(metres, float) for metres in stop.seat_offset_m)
            for stop in taken.stops
        )


def test_the_seat_cube_banks_as_seven_distinct_ungated_seat_takes(
    tmp_path: Path,
) -> None:
    """The cube reaches the bundle as seven takes nothing can confuse.

    The evidence a later reader opens: each take says it is a seat take, says
    its response kept the room, claims no mark distance, and keys to its own
    place -- so seven poses at one bearing are seven poses, not one measured
    seven times.
    """
    bundle, = (bank_seat_round(tmp_path) / "bundle").iterdir()
    rows = bundle_measurements(bundle, phase=PHASE_LATERAL)
    takes = [
        json.loads(take_artifact_path(bundle, row.path).read_text(encoding="utf-8"))
        for row in rows
    ]

    assert len(takes) == 7
    assert {take["pose_kind"] for take in takes} == {mp.POSE_KIND_SEAT}
    assert {take["gating_applied"] for take in takes} == {False}
    assert {take["mark_distance_m"] for take in takes} == {None}
    assert {curve["role"] for take in takes for curve in take["curves"]} == {"summed"}
    assert len({doc_pose_key(take) for take in takes}) == 7


@pytest.mark.parametrize("mover,tier", [(ac.MOVER_HUMAN, "full"), (ac.MOVER_ARM, "remote")])
def test_plan_copy_survives_staging_and_reaches_the_measurement_screen(spool_slot, mover, tier):
    custom = dataclasses.replace(mp.program("room", "quick"), poses=(
        dataclasses.replace(mp.ProgramPose(-20, 0), headline="Left sample", detail="Keep the mic still."),
    ))
    request = ac.request_for_program(custom, mover=mover)
    spool.stage_angle_request(request)
    restored = spool.take_staged_angle_request()
    assert restored == request
    prompts = tuple(stop.prompt for stop in ac.resolve_request(restored))
    plan = flow.build_v2_session_spec(
        _ROLES_BANDS, _FC_HZ, acknowledgement_binding="measurement-plan-copy-test",
        plan_shape=dataclasses.replace(flow.resolve_plan_shape(tier), hand_released_positions=mover == ac.MOVER_HUMAN),
        include_lateral=True, include_cloud_measure=False, lateral_prompts=prompts,
        lateral_candidate_ids=("",),
    ).capture_plan
    entry, = [entry for entry in plan.entries if entry.kind_label == "lateral"]
    assert entry.screen["title"] == "Left sample"
    assert entry.screen["body"] == "Keep the mic still."
    assert flow.position_angle_deg(prompts[0]) == -20


@pytest.mark.parametrize("kind,purpose", [("bearing", "speaker"), ("seat", "room"), ("close", "reference")])
def test_legacy_staged_stops_keep_their_capture_purpose(spool_slot, kind, purpose):
    stop = ac.AngleStop(0, ac.REGIME_SUMMED, kind=kind,
                        seat_offset_m=(0, 0, 0) if kind == "seat" else None)
    spool.stage_angle_request(ac.AngleCaptureRequest((stop,)))
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text())
    doc["stops"][0].pop("purpose")
    path.write_text(json.dumps(doc))
    assert spool.take_staged_angle_request().stops[0].purpose == purpose


@pytest.mark.parametrize("size", ["cloud", "quick"])
def test_room_candidate_batch_needs_a_new_start_at_each_physical_position(size):
    program = mp.program("room", size)
    request = ac.request_for_program(program, candidates=("", "room-fp"))
    prompts = tuple(s.prompt for s in ac.resolve_request(request))
    plan = flow.build_v2_session_spec(
        _ROLES_BANDS, _FC_HZ, acknowledgement_binding="room-position-test",
        plan_shape=dataclasses.replace(flow.resolve_plan_shape("full"), hand_released_positions=True),
        include_lateral=True, include_cloud_measure=False, lateral_prompts=prompts,
        lateral_candidate_ids=tuple(s.candidate_id for s in request.stops),
    ).capture_plan
    entries = [e for e in plan.entries if e.kind_label == "lateral"]
    assert len(entries) == program.capture_count * 2
    for offset, entry in enumerate(entries):
        assert entry.screen[POSITION_BATCH_CONFIG_KEY] == str(offset % 2 + 1)
        assert entry.screen[POSITION_BATCH_SIZE_KEY] == "2"
        assert entry.screen["auto_advance"] == (flow.AUTO_ADVANCE_TAP if offset % 2 == 0 else flow.AUTO_ADVANCE_COUNTDOWN)
    assert len({e.screen[POSITION_BATCH_START_KEY] for e in entries}) == program.mic_move_count


# --------------------------------------------------------------------------- #
# 9. the stimulus/level-policy fields: request-level, matched by construction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("program_id", "size", "candidates", "sweep_s", "level_ladder_dbfs", "stimulus_s"),
    [
        ("tournament", "express", (), None, (), None),
        ("tournament", "express", ("fp-a", "fp-b"), 2.0, (), 4.0),
        ("room", "quick", ("", "room-fp"), 1.5, (-20.0, -14.0, -8.0), 27.0),
        ("room", "cloud", (), None, (), None),
    ],
    ids=[
        "tournament-express-no-cand-no-sweep",
        "tournament-express-two-cand-sweep",
        "room-quick-two-cand-ladder",
        "room-cloud-no-cand-no-sweep",
    ],
)
def test_walk_price_reports_stimulus_seconds_for_named_programs(
    program_id: str, size: str, candidates: tuple[str, ...],
    sweep_s: float | None, level_ladder_dbfs: tuple[float, ...],
    stimulus_s: float | None,
) -> None:
    """``stimulus_s`` is derived from the program's own counts, the same rule
    :func:`test_a_program_becomes_its_own_walk_in_table_order` pins for everything
    else ``walk_price`` reports -- so this cannot drift from the table either.
    ``None``, never ``0``, for a walk that picked no duration: the two are
    different statements, and a reader printing ``0 s`` states the wrong one.
    """
    program = mp.program(program_id, size)
    request = ac.request_for_program(
        program, candidates=candidates,
        template=ac.walk_template(
            kind=MEASURE_KIND_CANDIDATE, sweep_s=sweep_s,
            level_ladder_dbfs=level_ladder_dbfs,
        ),
    )
    cycle = candidates or ("",)
    price = ac.walk_price(request)

    assert price["mic_moves"] == program.mic_move_count
    assert price["captures"] == program.capture_count * len(cycle)
    assert price["stimulus_s"] == (None if stimulus_s is None else pytest.approx(stimulus_s))


def test_a_volume_series_prices_each_rung_as_another_capture_at_every_stop() -> None:
    """A rung is another capture where the microphone already stands, so the rungs
    multiply the captures and the session's wall clock and leave ``mic_moves``
    alone. Priced against the SAME stops each captured once more per rung rather
    than against the formula, which would only restate the code; the order the
    rungs play in is the executor's to pin.
    """
    stops = (
        ac.AngleStop(0, ac.REGIME_SUMMED),
        ac.AngleStop(7, ac.REGIME_SUMMED),
    )
    rungs = (-20.0, -14.0, -8.0)
    template = ac.walk_template(kind=MEASURE_KIND_CANDIDATE, sweep_s=2.0)

    series = ac.walk_price(ac.AngleCaptureRequest(
        stops=stops, level_mode=ac.LEVEL_SERIES, main_volume_series_db=rungs,
        template=template,
    ))
    assert series == ac.walk_price(
        ac.AngleCaptureRequest(stops=stops * len(rungs), template=template)
    )
    assert (series["mic_moves"], series["captures"]) == (len(stops), 6)
    assert series["stimulus_s"] == pytest.approx(12.0)


def test_the_whole_template_reads_back_whole(spool_slot) -> None:
    """ONE spec per walk, so every capture is matched by construction: the whole
    template crosses the document together, written and read by the spec itself.
    """
    request = replace(
        ac.summed_at([0, 7]),
        template=ac.walk_template(
            kind=MEASURE_KIND_CANDIDATE,
            sweep_band_hz=(200.0, 3000.0),
            sweep_s=2.5,
            level_ladder_dbfs=(-20.0, -14.0),
        ),
    )
    spool.stage_angle_request(request)

    banked = json.loads(spool.angle_request_spool_path().read_text())["template"]
    assert MeasureSpec.from_mapping(banked) == request.template
    assert spool.peek_staged_angle_request() == request
    assert spool.take_staged_angle_request() == request


def test_a_document_from_the_older_schema_refuses_rather_than_reading_short(
    spool_slot,
) -> None:
    """The nine walk-level spec keys became one ``template``; a document written
    under the old shape names no template at all, so it refuses instead of
    staging a walk with the spec silently defaulted. The slot is single-use and
    restaging is one command.
    """
    spool.stage_angle_request(
        ac.AngleCaptureRequest(stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),))
    )
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text())
    older = {
        **{key: value for key, value in doc.items() if key != "template"},
        "artifact_schema_version": 1,
        "polarity": "normal",
    }
    path.write_text(json.dumps(older))

    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.take_staged_angle_request()
    assert excinfo.value.reason == spool.SPOOL_MALFORMED


@pytest.mark.parametrize(
    "fields",
    [
        {"level_mode": "loud"},
        {"level_mode": ac.LEVEL_SERIES, "main_volume_series_db": ()},
        {"main_volume_series_db": (-20.0,)},
        {"level_mode": ac.LEVEL_ACQUIRE_AT_ANCHOR, "main_volume_series_db": (-20.0,)},
    ],
    ids=[
        "unknown-mode", "series-with-no-rungs",
        "rungs-under-the-default-mode", "rungs-under-acquire-at-anchor",
    ],
)
def test_a_bad_level_policy_refuses_at_statement_time(fields: dict) -> None:
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.AngleCaptureRequest(stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),), **fields)
    assert excinfo.value.reason == ac.WALK_LEVEL_POLICY_INVALID


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"sweep_band_hz": (3000.0,)}, ac.WALK_STIMULUS_NOT_ACCEPTED),
        ({"sweep_band_hz": (3000.0, 200.0)}, ac.WALK_STIMULUS_NOT_ACCEPTED),
        ({"sweep_band_hz": (200.0, 200.0)}, ac.WALK_STIMULUS_NOT_ACCEPTED),
        ({"sweep_band_hz": (200.0, math.inf)}, ac.WALK_STIMULUS_NOT_ACCEPTED),
        ({"sweep_band_hz": (math.nan, 3000.0)}, ac.WALK_STIMULUS_NOT_ACCEPTED),
        ({"sweep_band_hz": (200.0, 3000.0, 9000.0)}, ac.WALK_STIMULUS_NOT_ACCEPTED),
        ({"sweep_s": 0.0}, ac.WALK_STIMULUS_NOT_ACCEPTED),
        ({"polarity": POLARITY_INVERTED}, ac.WALK_POLARITY_NOT_ACCEPTED),
        ({"inverted_role": "tweeter"}, ac.WALK_POLARITY_NOT_ACCEPTED),
        ({"delayed_role": "tweater", "delay_us": 250.0},
         ac.WALK_DELAY_NOT_ACCEPTED),
        ({"delay_us": 250.0}, ac.WALK_DELAY_NOT_ACCEPTED),
    ],
    ids=["one-bound", "descending", "equal", "infinite", "nan", "three-bounds",
         "zero-duration", "polarity-with-no-branch", "branch-with-no-polarity",
         "unknown-delayed-branch", "delay-with-no-branch"],
)
def test_a_template_field_the_spec_refuses_names_the_half_it_refused(
    fields: dict, reason: str,
) -> None:
    """``MeasureSpec`` is the only judge of a spec field, and the refusal reaches
    an operator under the slug for the flag they got wrong -- with the spec's own
    sentence as the detail, compared here against what the spec actually raises
    rather than against a copy of its wording.
    """
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.walk_template(kind=MEASURE_KIND_CANDIDATE, **fields)

    assert excinfo.value.reason == reason
    with pytest.raises(ValueError) as spec_refusal:
        MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE, graph_scope=ac.TEMPLATE_SWEEP_SCOPE
            if fields.get("sweep_band_hz") or fields.get("sweep_s") is not None
            else "drivers",
            **fields,
        )
    assert excinfo.value.detail == str(spec_refusal.value)


@pytest.mark.parametrize(
    "fields",
    [{"sweep_band_hz": (200.0, 3000.0)}, {"sweep_s": 2.5}],
    ids=["band", "duration"],
)
def test_a_summed_sweep_on_a_walk_with_no_summed_stop_refuses_at_statement_time(
    fields: dict,
) -> None:
    """Per-driver stops play the program's own excitation, so a band or duration
    stated for a summed sweep would have nothing to ride; ``walk_price`` must not
    price a stimulus no stop plays.
    """
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.AngleCaptureRequest(
            stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),),
            template=ac.walk_template(kind=MEASURE_KIND_CANDIDATE, **fields),
        )
    assert excinfo.value.reason == ac.WALK_STIMULUS_NOT_ACCEPTED


@pytest.mark.parametrize(
    "identity",
    [{"positions": (7,)}, {"pose_prompts": ("turn it",)},
     {"candidate_id": "fp-a", "graph_scope": "candidate"}],
    ids=["positions", "pose_prompts", "candidate_id"],
)
def test_a_template_carrying_what_the_executor_assigns_refuses(identity: dict) -> None:
    """The template is replayed at every stop, so a pose or candidate stated on it
    would be silently replaced there and silently kept on the design-axis spec --
    a walk measuring somewhere other than the stops its receipt printed.
    """
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.AngleCaptureRequest(
            stops=(ac.AngleStop(0, ac.REGIME_SUMMED),),
            template=MeasureSpec(kind=MEASURE_KIND_CANDIDATE, **identity),
        )
    assert excinfo.value.reason == ac.WALK_TEMPLATE_NOT_ACCEPTED


def test_the_two_owners_place_the_template_at_the_scope_each_capture_plays() -> None:
    """One template, two readers: the design-axis spec drops the summed sweep the
    drivers scope cannot play and keeps the ladder and ceiling every scope can;
    each summed stop gets the template at ITS pose, prompt, candidate and scope,
    and a per-driver stop gets no spec at all (it plays the phase's own program).
    """
    template = ac.walk_template(
        kind=MEASURE_KIND_CANDIDATE, sweep_band_hz=(200.0, 3000.0), sweep_s=2.5,
        level_ladder_dbfs=(-20.0, -14.0), spl_ceiling_db_spl=100.0,
    )
    request = ac.AngleCaptureRequest(
        stops=(
            ac.AngleStop(0, ac.REGIME_PER_DRIVER),
            ac.AngleStop(20, ac.REGIME_SUMMED, 5, "fp-a"),
        ),
        template=template,
    )
    prompts = tuple(stop.prompt for stop in ac.resolve_request(request))

    assert ac.design_axis_spec(request) == replace(
        template, graph_scope="drivers", sweep_band_hz=(), sweep_s=None,
    )
    assert ac.stop_specs(
        request, candidate_scopes={"fp-a": "candidate"}, prompts=prompts,
    ) == (
        None,
        replace(
            template, positions=(20,), vertical_deg=5,
            pose_prompts=(prompts[1].text,), candidate_id="fp-a",
            graph_scope="candidate",
        ),
    )


@pytest.mark.parametrize(
    ("walk", "banked"),
    [
        ({"template": {"spl_ceiling_db_spl": 100.0}}, {"spl_ceiling_db_spl": 100.0}),
        ({"level_mode": ac.LEVEL_ACQUIRE_AT_ANCHOR}, {}),
        ({"level_mode": ac.LEVEL_SERIES, "main_volume_series_db": (-20.0, -14.0)}, {}),
    ],
    ids=["ceiling", "acquire-at-anchor", "series"],
)
def test_a_policy_no_player_honours_yet_prices_but_does_not_stage(
    spool_slot, walk: dict, banked: dict,
) -> None:
    """The dry run still says what the walk would cost; the SLOT only ever holds a
    walk something can play. Refused on the way in and on the way out, so a
    hand-written document is refused the same way a staged one is.
    """
    fields = {
        **walk,
        **({"template": ac.walk_template(kind=MEASURE_KIND_CANDIDATE, **walk["template"])}
           if "template" in walk else {}),
    }
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),), **fields,
    )
    assert ac.walk_price(request)["captures"] >= 1

    with pytest.raises(ac.LateralWalkRefused) as staging:
        spool.stage_angle_request(request)
    assert staging.value.reason == ac.WALK_POLICY_UNSUPPORTED_YET
    assert not spool.staged_angle_request_pending()

    hand_written = {
        "artifact_schema_version": spool.SPOOL_SCHEMA_VERSION,
        "kind": spool.SPOOL_KIND,
        "mover": request.mover,
        "stops": [{"angle_deg": 0, "regime": ac.REGIME_PER_DRIVER}],
        "template": request.template.to_dict() | banked,
        **{key: list(value) if isinstance(value, tuple) else value
           for key, value in walk.items() if key != "template"},
    }
    spool.angle_request_spool_path().write_text(json.dumps(hand_written))
    with pytest.raises(ac.LateralWalkRefused) as taking:
        spool.take_staged_angle_request()
    assert taking.value.reason == ac.WALK_POLICY_UNSUPPORTED_YET


@pytest.mark.parametrize(
    ("field", "banked"),
    [
        ("delay_us", "loud"),
        ("delay_us", "250"),
        ("sweep_s", "two and a half"),
        ("sweep_s", math.nan),
        ("spl_ceiling_db_spl", "loud"),
        ("sweep_band_hz", "200,3000"),
        ("level_ladder_dbfs", "-20"),
        ("level_ladder_dbfs", ["-20", "quiet"]),
        ("sweep_s", True),
        ("delay_us", ""),
        ("spl_ceiling_db_spl", []),
        ("candidate_id", 7),
        ("level_matched", "true"),
        ("no_such_field", 1),
    ],
    ids=["delay-word", "delay-numeral", "sweep-s-word", "sweep-s-nan",
         "ceiling-word", "band-string", "ladder-bare-string", "ladder-bad-rung",
         "sweep-s-bool", "delay-empty", "ceiling-list", "id-number",
         "matched-string", "unknown-key"],
)
def test_a_banked_template_field_that_is_not_one_refuses_as_malformed(
    spool_slot, field: str, banked: object,
) -> None:
    """The spec reads its own document, so a hand-edited field is judged exactly
    as a flag would be -- a numeral STRING included, since ``jasper-measure``
    refuses one and two doors onto one class may not disagree. Refused as this
    document's own slug naming the field, never as a ``ValueError``/``TypeError``
    past every caller that catches ``CrossoverV2FlowError``.
    """
    spool.stage_angle_request(
        ac.AngleCaptureRequest(stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),))
    )
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text())
    doc["template"][field] = banked
    path.write_text(json.dumps(doc))

    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.peek_staged_angle_request()
    assert excinfo.value.reason == spool.SPOOL_MALFORMED
    assert field in excinfo.value.detail


@pytest.mark.parametrize(
    "banked", ["-20", {"rung": -20}, [None], math.nan, ["-20", -14]],
    ids=["bare-string", "mapping", "null-rung", "nan", "numeral-rung"],
)
def test_a_banked_volume_rung_that_is_not_a_number_refuses_as_malformed(
    spool_slot, banked: object,
) -> None:
    """The walk's own rungs, the one banked list outside the template. A bare
    string is the one that bites quietly: iterated instead of refused, it reads
    back as one rung per character.
    """
    spool.stage_angle_request(
        ac.AngleCaptureRequest(stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),))
    )
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text())
    doc["main_volume_series_db"] = banked
    path.write_text(json.dumps(doc))

    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.peek_staged_angle_request()
    assert excinfo.value.reason == spool.SPOOL_MALFORMED
    assert "main_volume_series_db" in excinfo.value.detail


def test_the_stimulus_flags_are_one_spelling_in_both_clis() -> None:
    """``jasper-measure`` states the stimulus per take and ``jasper-angle-capture``
    states it once for a walk, but both name the same ``MeasureSpec`` fields -- so
    the same words parse to the same namespace on either parser.
    """
    words = [
        "--sweep-band-hz", "200", "3000", "--sweep-s", "2.5",
        "--level-dbfs", "-20", "--level-dbfs", "-14",
        "--spl-ceiling-db-spl", "100",
    ]
    fields = ("sweep_band_hz", "sweep_s", "level_dbfs", "spl_ceiling_db_spl")
    walk = cli.build_parser().parse_args(["plan", "--angles", "0", *words])
    take = measure_cli.build_parser().parse_args(["--kind", "candidate", *words])

    stated = [getattr(walk, field) for field in fields]
    assert stated == [getattr(take, field) for field in fields]
    assert stated == [[200.0, 3000.0], 2.5, [-20.0, -14.0], 100.0]


def test_cli_stimulus_flags_reach_the_staged_request(spool_slot) -> None:
    """The stimulus flags, through the CLI's own request builder, staged and read
    back -- the spool round trip is pinned separately above, so this pins the CLI
    WIRING.
    """
    argv = [
        "stage", "--angles", "0", "--regime", "summed",
        "--sweep-band-hz", "200", "3000",
        "--sweep-s", "2.5",
        "--level-dbfs", "-20", "--level-dbfs", "-14",
    ]
    request = cli._build_request(cli.build_parser().parse_args(argv))
    assert request.template.sweep_band_hz == (200.0, 3000.0)
    assert request.template.sweep_s == 2.5
    assert request.template.level_ladder_dbfs == (-20.0, -14.0)
    assert request.level_mode == ac.LEVEL_HOLD_REFERENCE

    spool.stage_angle_request(request)
    assert spool.take_staged_angle_request() == request


def test_the_walk_level_policy_flags_reach_the_request() -> None:
    """``--level-mode``/``--level-series``/``--spl-ceiling-db-spl`` are the WALK's own
    policy rather than a stop's, so they are read off the request; what stages is
    :func:`test_a_policy_no_player_honours_yet_prices_but_does_not_stage`'s subject.
    """
    request = cli._build_request(cli.build_parser().parse_args([
        "plan", "--angles", "0",
        "--level-mode", "series",
        "--level-series", "-20", "--level-series", "-14",
        "--spl-ceiling-db-spl", "100",
    ]))
    assert request.level_mode == ac.LEVEL_SERIES
    assert request.main_volume_series_db == (-20.0, -14.0)
    assert request.template.spl_ceiling_db_spl == 100.0


def test_cli_stimulus_flags_reach_the_printed_price(capsys) -> None:
    """``plan`` gates on nothing a bare box lacks (unlike ``stage``, which refuses
    with no banked seat-level anchor), so this drives the real verb and reads its
    actual stdout document -- the one JSON document ADR-0237 promises -- rather
    than re-deriving ``price`` by calling ``walk_price`` beside it.
    """
    argv = [
        "plan", "--program", "tournament", "--size", "express", "--candidates", "base",
        "--sweep-s", "2.0",
        "--level-dbfs", "-20", "--level-dbfs", "-14", "--level-dbfs", "-8",
    ]
    args = cli.build_parser().parse_args(argv)
    args.invocation = argv

    assert cli._cmd_plan(args) == cli.EXIT_OK
    document = json.loads(capsys.readouterr().out)

    price = document["price"]
    assert price["stimulus_s"] == pytest.approx(price["captures"] * 2.0 * 3)


@pytest.mark.parametrize(
    "overlay",
    [{"polarity": "inverted", "inverted_role": "tweeter"},
     {"delayed_role": "tweeter", "delay_us": 250.0}, {"level_matched": True}],
    ids=["polarity", "delay", "level-match"],
)
def test_a_summed_sweep_beside_an_overlay_is_refused_as_not_measurable(overlay: dict) -> None:
    """The two cannot share a template: a summed trial plays its own graph. Named
    for what it is at statement time, not for whichever half ``MeasureSpec``
    happened to refuse first.
    """
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.walk_template(kind=MEASURE_KIND_CANDIDATE, sweep_s=2.5, **overlay)
    assert excinfo.value.reason == ac.WALK_CANDIDATE_NOT_MEASURABLE


def test_the_design_axis_spec_is_always_the_candidate_kind() -> None:
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),),
        template=ac.walk_template(kind=MEASURE_KIND_VERIFY),
    )
    assert ac.design_axis_spec(request).kind == MEASURE_KIND_CANDIDATE


def test_a_template_that_is_not_a_spec_is_refused() -> None:
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.AngleCaptureRequest(stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),), template=None)
    assert excinfo.value.reason == ac.WALK_TEMPLATE_NOT_ACCEPTED
