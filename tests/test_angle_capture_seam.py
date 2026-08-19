# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The angle-capture seam: {per-driver | summed} x {angles} x {arm | human}.

Four things are pinned here, and the third is the one that matters most for
review: this feature is NOT a route around the paused lateral-walk statistic.

1. the angle round trip -- degrees in, degrees back out of the shipped derivation;
2. the seam's dispatch -- each combination resolves to the right program object,
   pose and advance policy, mutation-checked so a collapsed branch fails;
3. **the ruling** -- the seam neither reads nor needs ``STAGE1_INCLUDES_LATERAL``
   and never mints ``PHASE_LATERAL``, so the barred statistic stays off;
4. mover parity, and the record/receipt shape the shipped consumers read.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.programs import NoProgramForPhaseError
from jasper.active_speaker.crossover_v2.spatial import cloud_position_record

_SHIPPED_ANGLES = (0, 7, -7, 22, -22)


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

    That tag is what routes a capture into the walk's close, where the barred
    selector term is computed. This seam's captures cannot arrive there because
    they never carry the tag -- which is why the capability can ship while the
    statistic stays paused.
    """
    for request in (
        ac.per_driver_at(_SHIPPED_ANGLES),
        ac.summed_at(_SHIPPED_ANGLES),
        ac.both_at(_SHIPPED_ANGLES, mover=ac.MOVER_ARM),
    ):
        phases = {s.program_phase for s in ac.resolve_request(request)}
        assert PHASE_LATERAL not in phases
        assert PHASE_LATERAL not in set(ac.index_phase_map(request).values())


def test_the_seam_is_independent_of_the_paused_lateral_flag() -> None:
    """Flipping STAGE1_INCLUDES_LATERAL changes NOTHING here.

    **This is the mutation that states the ruling.** The pause exists because
    the lateral-walk STATISTIC was invalidated (pose-ratio cancellation, #2711);
    per-driver captures at angles as forward-model input are a different consumer
    with a different validity argument. If this seam's behaviour moved with that
    flag, the two would in fact be the same switch and the feature WOULD be a
    bar-dodge. It does not move -- asserted in both flag states -- so the
    capability and the barred statistic are genuinely separate.

    The converse is asserted too: the seam does not turn the walk on. Reading
    the shipped flag after exercising the seam still finds it False.
    """
    request = ac.both_at(_SHIPPED_ANGLES, mover=ac.MOVER_ARM)
    baseline = ac.resolve_request(request)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "STAGE1_INCLUDES_LATERAL", True)
        assert ac.resolve_request(request) == baseline
        mp.setattr(flow, "STAGE1_INCLUDES_LATERAL", False)
        assert ac.resolve_request(request) == baseline

    assert flow.STAGE1_INCLUDES_LATERAL is False


def test_the_seam_module_does_not_reference_the_paused_flag() -> None:
    """Static backstop for the ruling above.

    A future edit could reintroduce the coupling the test above rules out; this
    fails the moment the module names either symbol *as code*.

    **Parsed, never text-scanned** -- the discipline `test_lint_contracts.py`
    states for exactly this shape of rule: the module docstring discusses both
    names at length in prose (that discussion is the point), so a text scan
    would report the explanation as the violation. The AST sees only code, and
    prose is where these names belong.
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
    assert "STAGE1_INCLUDES_LATERAL" not in referenced
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
    """A person's tap IS the settle signal; no gate keys are emitted."""
    for stop in ac.resolve_request(ac.both_at([0, 22])):
        assert stop.screen == {"auto_advance": flow.AUTO_ADVANCE_TAP}
        assert flow.POSITION_DEG_KEY not in stop.screen


def test_arm_mover_pairs_the_countdown_with_the_position_gate() -> None:
    """Auto-advance and the gate are emitted TOGETHER -- they are a pair.

    A countdown without the gate fires into an arm still in motion; the gate
    without auto-advance waits for a tap nobody is there to give.
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
        captured_at=0.0, session_id="s", gate_window_ms=None,
        gate_floor_source=None, gate_disclosure=None, validity_floor_hz=None,
        gating_applied=False, summed_ripple_db=None, glitch_detected=False,
        wav_sha256=None,
    )
    assert record["take_id"] == "angle_01_a01"
    assert record["wide"] is True
    assert record["role"] == flow.POSITION_ROLE_OFFAX
    assert record["prompt"] == stop.prompt.text and record["prompt"]


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
    """Every stop sits at the SAME radius -- the geometric reason for degrees.

    The ratified design's own argument: a 40 cm lateral slide off a 1 m mark
    puts the microphone 107.7 cm out, ~0.64 dB of pure inverse-square level
    change with no acoustics in it. An angle-stated pose is a constant-radius
    arc, so that confound is structural rather than addressed in prose.
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
