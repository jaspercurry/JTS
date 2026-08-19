# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The trigger: an operator states an angle walk and a session can take it.

Covers :mod:`jasper.active_speaker.angle_capture_spool` (the mailbox and its one
own rule) and :mod:`jasper.cli.angle_capture` (the operator's door). What is NOT
re-tested here is anything the #2732 seam already owns -- the angle bounds, the
pose round trip, the program identity, the mover parity: those live in
``tests/test_angle_capture_seam.py``, and re-asserting them here would create the
second validator this design exists to avoid. What IS asserted is that every
refusal the trigger produces comes FROM that seam, in the seam's own words.
"""

from __future__ import annotations

import json
import time

import pytest

from jasper.active_speaker import angle_capture_spool as spool
from jasper.active_speaker.angle_capture import (
    MOVER_ARM,
    MOVER_HUMAN,
    MOVERS,
    REGIME_PER_DRIVER,
    REGIME_SUMMED,
    REGIMES,
    AngleCaptureRequest,
    AngleStop,
    both_at,
    per_driver_at,
    position_angle_deg,
    resolve_request,
    summed_at,
)
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_TAP,
    POSITION_DEG_KEY,
    CrossoverV2FlowError,
)
from jasper.active_speaker.session_volume_plan import (
    DEFAULT_WALL_CLOCK_CEILING_S,
    SCHEMA_VERSION,
    STATE_KIND,
)
from jasper.cli import angle_capture as cli

CAMPAIGN_ANGLES = [0, 7, -7, 22, -22]


@pytest.fixture
def slot(tmp_path, monkeypatch):
    """A writable pending slot, and an idle speaker.

    The volume-state path is redirected too: without it every test in this file
    would read the real ``/var/lib/jasper`` state of whatever machine is running
    the suite, and a developer's box mid-measurement would fail the suite for a
    reason that has nothing to do with the code.
    """
    path = tmp_path / "angle_request.json"
    spool.set_angle_request_spool_path_for_tests(path)
    volume_state = tmp_path / "session_volume.json"
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        volume_state,
    )
    try:
        yield path, volume_state
    finally:
        spool.set_angle_request_spool_path_for_tests(None)


def _write_volume_state(path, *, status, opened_at, ceiling_s=DEFAULT_WALL_CLOCK_CEILING_S):
    path.write_text(
        json.dumps({
            "kind": STATE_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "reason": None if status == "active" else "restore_unconfirmed",
            "opened_at": opened_at,
            "wall_clock_ceiling_s": ceiling_s,
            "measurement_volume_db": -20.0,
            "original_main_volume_db": -6.0,
        }),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 1. the trigger resolves to the right (program x angles x mover) request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("regime", sorted(cli._REGIME_STOPS))
@pytest.mark.parametrize("mover", sorted(MOVERS))
def test_cli_resolves_every_regime_and_mover_through_the_seam(regime, mover):
    """Every ``--regime`` x ``--mover`` combination lands on the seam's answer.

    The assertion is EQUALITY against what the seam's own constructors produce
    for the same angles, not a restatement of what each combination should be:
    if the CLI ever grew its own opinion about a regime's stops or a mover's
    policy, that opinion would differ from the constructor's and this fails.
    """
    args = cli.build_parser().parse_args(
        ["plan", "--angles", "0,7,-7,22,-22", "--regime", regime, "--mover", mover]
    )
    built = cli._build_request(args)

    expected = {
        REGIME_PER_DRIVER: per_driver_at,
        REGIME_SUMMED: summed_at,
        "both": both_at,
    }[regime](CAMPAIGN_ANGLES, mover=mover)
    assert built == expected


def test_the_campaign_walk_plays_measure_at_five_angles_by_hand():
    """The exact walk the hardware night needs, end to end through the door."""
    args = cli.build_parser().parse_args(
        ["plan", "--angles", "0,7,-7,22,-22", "--regime", "per_driver",
         "--mover", "human"]
    )
    stops = resolve_request(cli._build_request(args))

    assert [s.angle_deg for s in stops] == CAMPAIGN_ANGLES
    # Every stop plays MEASURE's interleaved per-driver object -- the forward
    # model's input -- and none of them is a lateral pose.
    assert {s.program_phase for s in stops} == {"measure"}
    assert [s.index for s in stops] == [1, 2, 3, 4, 5]
    # Hand-guided: the tap is the settle signal and no gate key is emitted.
    assert {s.screen["auto_advance"] for s in stops} == {AUTO_ADVANCE_TAP}
    assert all(POSITION_DEG_KEY not in s.screen for s in stops)


def test_the_arm_walk_emits_the_commanded_bearing_for_the_gate():
    """The arm path declares ``position_deg`` per stop, signed as commanded.

    **A2: the commanded sign is the physical truth and a readback sign lies.**
    The number here is derived from the pose the request minted, never from any
    device -- so this also pins that the trigger consumes no readback: there is
    no seam here through which one could arrive.
    """
    stops = resolve_request(per_driver_at(CAMPAIGN_ANGLES, mover=MOVER_ARM))

    assert {s.screen["auto_advance"] for s in stops} == {AUTO_ADVANCE_COUNTDOWN}
    assert [int(s.screen[POSITION_DEG_KEY]) for s in stops] == CAMPAIGN_ANGLES
    # …and the gate's number is the pose's own bearing, not a copy of the
    # request: reading it back off the prompt is what makes that one fact.
    assert [position_angle_deg(s.prompt) for s in stops] == CAMPAIGN_ANGLES


def test_both_pairs_the_regimes_so_the_microphone_moves_once_per_angle():
    args = cli.build_parser().parse_args(
        ["plan", "--angles", "0,45", "--regime", "both", "--mover", "arm"]
    )
    stops = resolve_request(cli._build_request(args))

    assert [(s.angle_deg, s.regime) for s in stops] == [
        (0, REGIME_PER_DRIVER), (0, REGIME_SUMMED),
        (45, REGIME_PER_DRIVER), (45, REGIME_SUMMED),
    ]


def test_the_regime_table_covers_every_regime_the_seam_declares():
    """A regime added to the seam must gain a verb here, not be silently absent."""
    assert set(REGIMES) <= set(cli._REGIME_STOPS)


# --------------------------------------------------------------------------- #
# 2. refusals ride the seam's own validation -- there is no second validator
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "angles, fragment",
    [
        # The sharp row: 0.4 truncates to an ON-AXIS capture nobody asked for.
        ("0.4", "WHOLE degrees"),
        ("7.9", "WHOLE degrees"),
        ("7.5", "WHOLE degrees"),
        ("true", "WHOLE degrees"),
        ("95", "within +/-80 deg"),
        ("-95", "within +/-80 deg"),
        ("", "at least one stop"),
    ],
)
def test_the_cli_refuses_with_the_seams_own_sentence(angles, fragment):
    """Every refusal is raised BY the seam and reaches the operator verbatim.

    The fragments are quoted from ``_validated_angle`` and
    ``AngleCaptureRequest.__post_init__``. A CLI that had grown its own bounds
    check would produce its own wording and fail this -- which is the point:
    the assertion is about WHO refused, not merely that something did.
    """
    args = cli.build_parser().parse_args(["plan", "--angles", angles])
    with pytest.raises(CrossoverV2FlowError) as excinfo:
        cli._build_request(args)
    assert fragment in str(excinfo.value)


def test_a_fractional_angle_is_never_coerced_on_its_way_to_the_seam():
    """``_angle_field`` hands a non-integral field over UNCHANGED.

    ``int("0.4")`` raises, but ``int(0.4)`` is ``0``. Parsing to a float first
    and then to an int is one keystroke from a silent on-axis capture, so this
    pins that the string survives the parse and the seam is what judges it.
    """
    assert cli._angle_field("0.4") == "0.4"
    assert cli._angle_field("7") == 7
    assert cli._angle_field(" -22 ") == -22


def test_plan_exits_two_on_a_refusal_and_zero_on_a_walk(capsys):
    parser = cli.build_parser()
    assert cli._cmd_plan(parser.parse_args(["plan", "--angles", "0.4"])) == cli.EXIT_REFUSED
    assert "WHOLE degrees" in capsys.readouterr().err
    assert cli._cmd_plan(parser.parse_args(["plan", "--angles", "0,7"])) == cli.EXIT_OK


def test_plan_writes_nothing(slot):
    path, _ = slot
    cli._cmd_plan(cli.build_parser().parse_args(["plan", "--angles", "0,7,-7"]))
    assert not path.exists()
    assert not spool.staged_angle_request_pending()


# --------------------------------------------------------------------------- #
# 3. the receipt / banking shape
# --------------------------------------------------------------------------- #


def test_each_stop_banks_in_the_shipped_cloud_position_record_shape(slot):
    """The fields a stop determines are exactly the record's own fields.

    ``cloud_position_record`` is the shipped per-position receipt. The planner
    must not invent fields it has no evidence for, and must not misname the
    ones it does have -- so this asserts membership against that function's real
    signature rather than against a copied list.
    """
    import inspect

    from jasper.active_speaker.crossover_v2.spatial import cloud_position_record

    record_fields = set(inspect.signature(cloud_position_record).parameters)
    payload = cli._walk_payload(per_driver_at(CAMPAIGN_ANGLES))
    banked = payload["stops"][0]["banks_as"]

    # Every planned field is a real field of the record…
    assert {"role", "wide"} <= set(banked) & record_fields
    # …and the two that are NOT record fields are the pose's own geometry, which
    # the record carries through ``prompt``/``wide`` rather than as numbers.
    assert set(banked) - record_fields == {"offset_cm", "position_angle_deg"}
    # Nothing MEASURED is claimed: a planner that printed a placeholder for a
    # capture's own evidence would be inventing it.
    for measured in (
        "captured_at", "wav_sha256", "summed_ripple_db", "glitch_detected",
        "gate_window_ms", "validity_floor_hz", "position_id", "take_id",
    ):
        assert measured not in banked


def test_the_banked_pose_round_trips_the_commanded_bearing(slot):
    payload = cli._walk_payload(per_driver_at(CAMPAIGN_ANGLES))
    assert [s["banks_as"]["position_angle_deg"] for s in payload["stops"]] == (
        CAMPAIGN_ANGLES
    )
    # …and the wide class is DERIVED from the distance, exactly as the shipped
    # table assigns it: 22 deg off a 1 m mark is 40.4 cm, past the 30 cm edge.
    assert [s["banks_as"]["wide"] for s in payload["stops"]] == (
        [False, False, False, True, True]
    )


def test_the_staged_document_round_trips_the_whole_walk(slot):
    path, _ = slot
    request = both_at([0, 7, -7], mover=MOVER_ARM)
    spool.stage_angle_request(request)

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["kind"] == spool.SPOOL_KIND
    assert doc["artifact_schema_version"] == spool.SPOOL_SCHEMA_VERSION
    assert doc["mover"] == MOVER_ARM

    assert spool.take_staged_angle_request() == request


def test_the_staged_order_is_the_walk_order_not_a_sorted_rewrite(slot):
    """Order is the measurement's: a sorted or de-duplicated bank re-plans it."""
    request = per_driver_at([0, 22, -7, 7, -22])
    spool.stage_angle_request(request)
    assert [s.angle_deg for s in spool.take_staged_angle_request().stops] == (
        [0, 22, -7, 7, -22]
    )


def test_a_take_consumes_and_never_returns_a_reused_walk(slot):
    path, _ = slot
    spool.stage_angle_request(per_driver_at([0]))
    assert spool.take_staged_angle_request() is not None
    assert not spool.staged_angle_request_pending()
    assert spool.take_staged_angle_request() is None
    assert path.with_name(path.name + spool.CONSUMED_SUFFIX).is_file()


def test_no_walk_staged_is_a_quiet_none(slot):
    assert spool.take_staged_angle_request() is None


def test_a_staged_walk_edited_out_of_bounds_refuses_once_then_clears(slot):
    """A bad document must not refuse every session until someone deletes it."""
    path, _ = slot
    spool.stage_angle_request(per_driver_at([7]))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["stops"][0]["angle_deg"] = 95
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(CrossoverV2FlowError, match=r"within \+/-80 deg"):
        spool.take_staged_angle_request()
    assert spool.take_staged_angle_request() is None


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d.update(kind="something_else"), spool.SPOOL_MALFORMED),
        (lambda d: d.update(artifact_schema_version=99), spool.SPOOL_MALFORMED),
        (lambda d: d.update(stops=[]), spool.SPOOL_MALFORMED),
        (lambda d: d.update(stops="not-a-list"), spool.SPOOL_MALFORMED),
        (lambda d: d.update(stops=[1, 2]), spool.SPOOL_MALFORMED),
    ],
)
def test_a_corrupt_document_is_refused_by_shape(slot, mutate, expected):
    path, _ = slot
    spool.stage_angle_request(per_driver_at([7]))
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.take_staged_angle_request()
    assert excinfo.value.reason == expected


def test_a_document_that_is_not_json_is_refused_rather_than_ignored(slot):
    path, _ = slot
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.take_staged_angle_request()
    assert excinfo.value.reason == spool.SPOOL_MALFORMED


def test_a_walk_longer_than_the_bound_is_refused_at_both_ends(slot):
    path, _ = slot
    long_walk = AngleCaptureRequest(
        stops=tuple(
            AngleStop(a, REGIME_PER_DRIVER)
            for a in range(-spool.MAX_STOPS // 2, spool.MAX_STOPS // 2 + 2)
        )
    )
    assert len(long_walk.stops) > spool.MAX_STOPS
    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.stage_angle_request(long_walk)
    assert excinfo.value.reason == spool.SPOOL_TOO_MANY_STOPS

    # …and the take refuses it too, for a document that arrived another way.
    spool.stage_angle_request(per_driver_at([7]))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["stops"] = doc["stops"] * (spool.MAX_STOPS + 1)
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.take_staged_angle_request()
    assert excinfo.value.reason == spool.SPOOL_TOO_MANY_STOPS


def test_staging_twice_is_last_wins_and_withdraw_clears(slot):
    spool.stage_angle_request(per_driver_at([7]))
    spool.stage_angle_request(summed_at([22, -22]))
    taken = spool.take_staged_angle_request()
    assert [(s.angle_deg, s.regime) for s in taken.stops] == [
        (22, REGIME_SUMMED), (-22, REGIME_SUMMED),
    ]

    assert spool.withdraw_staged_angle_request() is False
    spool.stage_angle_request(per_driver_at([0]))
    assert spool.withdraw_staged_angle_request() is True
    assert spool.take_staged_angle_request() is None


# --------------------------------------------------------------------------- #
# 4. a walk cannot be staged while a measurement session holds the speaker
# --------------------------------------------------------------------------- #


def test_an_idle_speaker_with_no_volume_state_stages_cleanly(slot):
    _, volume_state = slot
    assert not volume_state.exists()
    assert spool.live_measurement_session() is None
    assert spool.stage_angle_request(per_driver_at([0])).is_file()


def test_a_live_session_refuses_the_stage(slot):
    """The durable measurement-volume state is the cross-process fact."""
    _, volume_state = slot
    _write_volume_state(volume_state, status="active", opened_at=time.time())

    assert "already running" in (spool.live_measurement_session() or "")
    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.stage_angle_request(per_driver_at(CAMPAIGN_ANGLES))
    assert excinfo.value.reason == spool.SESSION_ALREADY_LIVE
    assert not spool.staged_angle_request_pending()


def test_an_unresolved_volume_refuses_the_stage(slot):
    """A session that would refuse to open is not a session to queue for."""
    _, volume_state = slot
    _write_volume_state(volume_state, status="unresolved", opened_at=time.time())

    assert "unresolved" in (spool.live_measurement_session() or "")
    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.stage_angle_request(per_driver_at([0]))
    assert excinfo.value.reason == spool.SESSION_ALREADY_LIVE


def test_a_stale_active_session_is_a_crash_and_does_not_block(slot):
    """A crashed session must not make this door permanently unusable.

    The flow's own open path force-drains a stale-active state, so refusing on
    it here would block exactly the walk that is about to be legal.
    """
    _, volume_state = slot
    _write_volume_state(
        volume_state,
        status="active",
        opened_at=time.time() - (DEFAULT_WALL_CLOCK_CEILING_S + 60.0),
    )

    assert spool.live_measurement_session() is None
    assert spool.stage_angle_request(per_driver_at([0])).is_file()


def test_the_guard_reads_a_real_path_not_an_empty_plan(slot):
    """A ``SessionVolumePlan()`` with no path reads nothing and answers 'idle'.

    That is the failure mode this guard cannot have -- it would look like a
    guard and check nothing -- so the live-session case above is only meaningful
    if the default path is what gets read. This pins that the function reaches
    the module default rather than constructing a pathless plan.
    """
    _, volume_state = slot
    _write_volume_state(volume_state, status="active", opened_at=time.time())
    assert spool.live_measurement_session() is not None
    # …and an explicitly-empty path is the pathless plan, which sees nothing.
    assert spool.live_measurement_session(state_path=volume_state.parent / "nope") is None


def test_the_cli_stage_surfaces_the_busy_refusal(slot, capsys):
    _, volume_state = slot
    _write_volume_state(volume_state, status="active", opened_at=time.time())

    args = cli.build_parser().parse_args(
        ["stage", "--angles", "0,7,-7,22,-22", "--json"]
    )
    assert cli._cmd_stage(args) == cli.EXIT_REFUSED
    out = capsys.readouterr()
    body = json.loads(out.out)
    assert body == {
        "ok": False,
        "reason": spool.SESSION_ALREADY_LIVE,
        "detail": body["detail"],
    }
    assert "already running" in body["detail"]
    assert "already running" in out.err


def test_the_cli_stage_banks_the_walk_when_the_speaker_is_idle(slot, capsys):
    path, _ = slot
    args = cli.build_parser().parse_args(
        ["stage", "--angles", "0,7,-7,22,-22", "--regime", "per_driver",
         "--mover", "human", "--json"]
    )
    assert cli._cmd_stage(args) == cli.EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True
    assert body["staged_at_path"] == str(path)
    assert [s["angle_deg"] for s in body["stops"]] == CAMPAIGN_ANGLES

    assert spool.take_staged_angle_request() == per_driver_at(
        CAMPAIGN_ANGLES, mover=MOVER_HUMAN
    )


def test_a_filesystem_failure_is_its_own_exit_code(slot, monkeypatch, capsys):
    """``3`` means fix the speaker's filesystem; ``2`` means fix the request."""
    def _boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(spool, "atomic_write_text", _boom)
    args = cli.build_parser().parse_args(["stage", "--angles", "0"])
    assert cli._cmd_stage(args) == cli.EXIT_STAGE_FAILED
    assert "read-only file system" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 5. mutation on the dispatch
# --------------------------------------------------------------------------- #
#
# Each case below is the SHAPE of a plausible wrong implementation, asserted
# against directly rather than by editing the module: the tests above already
# fail on each of these, and these state WHY in one place so a reader can see
# which defect each guard is holding.


def test_mutation_regime_dispatch_cannot_collapse_to_one_arm():
    """Collapsing the regime table would make two verbs the same walk."""
    angles = [0, 22]
    per = resolve_request(per_driver_at(angles))
    summed = resolve_request(summed_at(angles))
    both = resolve_request(both_at(angles))

    assert [s.program_phase for s in per] != [s.program_phase for s in summed]
    assert len(both) == len(per) + len(summed)


def test_mutation_mover_cannot_be_ignored():
    """Always-tap would make the arm's gate silently absent."""
    human = resolve_request(per_driver_at([22], mover=MOVER_HUMAN))[0]
    arm = resolve_request(per_driver_at([22], mover=MOVER_ARM))[0]

    assert human.screen["auto_advance"] != arm.screen["auto_advance"]
    assert POSITION_DEG_KEY in arm.screen and POSITION_DEG_KEY not in human.screen
    # …and the mover changes ONLY the advance policy: same pose, same program.
    assert human.prompt == arm.prompt
    assert human.program_phase == arm.program_phase


def test_mutation_angle_sign_cannot_be_dropped():
    """A magnitude-only walk would send the microphone to one side twice."""
    stops = resolve_request(per_driver_at([7, -7], mover=MOVER_ARM))
    assert stops[0].prompt != stops[1].prompt
    assert int(stops[0].screen[POSITION_DEG_KEY]) == -int(
        stops[1].screen[POSITION_DEG_KEY]
    )


def test_mutation_the_staged_mover_cannot_default_away(slot):
    """Dropping ``mover`` from the document would silently hand-walk an arm rig."""
    path, _ = slot
    spool.stage_angle_request(per_driver_at([0], mover=MOVER_ARM))
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["mover"] == MOVER_ARM
    del doc["mover"]
    path.write_text(json.dumps(doc), encoding="utf-8")

    # It does not fall back to the human default: the request refuses, because
    # ``AngleCaptureRequest`` is handed ``"None"`` and knows no such mover.
    with pytest.raises(CrossoverV2FlowError, match="mover must be one of"):
        spool.take_staged_angle_request()


def test_mutation_the_busy_guard_cannot_be_removed(slot):
    """With the guard bypassed the stage would succeed under a live session."""
    _, volume_state = slot
    _write_volume_state(volume_state, status="active", opened_at=time.time())
    with pytest.raises(spool.AngleRequestRefused):
        spool.stage_angle_request(per_driver_at([0]))

    # The positive control: the SAME request, the same slot, an idle speaker.
    volume_state.unlink()
    assert spool.stage_angle_request(per_driver_at([0])).is_file()
