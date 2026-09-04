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
import logging
import math
import os
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

from jasper.active_speaker import angle_capture_spool as spool
from jasper.active_speaker import measurement_programs as mp
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
    request_for_program,
    resolve_request,
    summed_at,
)
from jasper.active_speaker.crossover_v2.contracts import (
    DRIVER_ROLE_TWEETER,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
)
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_TAP,
    POSITION_DEG_KEY,
    CrossoverV2FlowError,
)
from jasper.active_speaker import seat_level_reference as slr
from jasper.active_speaker.seat_level_reference import (
    SeatLevelTarget,
    write_seat_level_reference,
)
from jasper.active_speaker.session_volume_plan import (
    DEFAULT_WALL_CLOCK_CEILING_S,
    SCHEMA_VERSION,
    STATE_KIND,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    stage1_base_entries,
    wall_clock_ceiling_s,
)
from jasper.cli import angle_capture as cli
from jasper.identity import CROSSOVER_PAGE_PATH

CAMPAIGN_ANGLES = [0, 7, -7, 22, -22]

#: argparse's own usage exit, which is not this tool's exit vocabulary.
ARGPARSE_USAGE_EXIT = 2

# One banked anchor, and the vendor header that makes it absolute. The level
# the receipt must print is ``ANCHOR_DB_SPL`` -- a walk drives at the anchor.
ANCHOR_DB_SPL = 77.5
REFERENCE_VOLUME_DB = -18.0
# Stubbed so the receipt's level does not depend on the output topology of
# whatever machine runs the suite; every shipped program clears it.
CEILING_DB_SPL = 90.0
CAL_WITH_SENS = (
    '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n'
)


def _bank_an_anchor(tmp_path, monkeypatch):
    """A converged seat-level reference, and a readable calibration behind it.

    Only WHERE the mic's file is found is redirected: the real resolver parses
    a real vendor header, so the receipt's serial and the refusal that fires
    without one are the shipped ones.
    """
    reference = tmp_path / "seat_level_reference.json"
    write_seat_level_reference(
        reference_volume_db=REFERENCE_VOLUME_DB,
        measured_db_spl=ANCHOR_DB_SPL,
        target=SeatLevelTarget(target_db_spl=ANCHOR_DB_SPL, tolerance_db=2.5),
        sensitivity={"sens_factor_db": -12.07, "serial": "8108494"},
        max_main_volume_db=-6.0,
        state_path=reference,
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE", str(reference)
    )
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)
    from jasper.audio_measurement import calibration

    real = calibration.resolve_mic_sensitivity
    monkeypatch.setattr(
        calibration,
        "resolve_mic_sensitivity",
        lambda **_kw: real(calibration_file=str(cal)),
    )
    monkeypatch.setattr(slr, "_ceiling_db_spl", lambda: CEILING_DB_SPL)


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
    _bank_an_anchor(tmp_path, monkeypatch)
    volume_state = tmp_path / "session_volume.json"
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        volume_state,
    )
    # No jasper-control in this suite, so the door falls back to the durable
    # statefile above. Pinned rather than left to whether something happens to
    # be listening on 8780 on the machine running the suite; the tests that
    # exercise the reachable-control path patch this themselves.
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.read_measurement_hold",
        lambda: None,
    )
    # ...and the box's standing ``jasper-declare-geometry`` declaration, which
    # ``stage`` echoes so the operator sees what a round will bank: a
    # developer's box that has one must not change what these walks print.
    monkeypatch.setattr(cli, "DECLARED_GEOMETRY_PATH", tmp_path / "declared.json")
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


def test_plan_echoes_the_delay_coordinate_when_stated(capsys):
    """A stated ``(delayed_role, delay_us)`` reaches the graph: before this,
    confirming that was true meant tracing
    ``measure_spec.measurement_delays_for`` by hand -- nothing in the preview
    said so.
    """
    parser = cli.build_parser()
    args = parser.parse_args(
        ["plan", "--angles", "0", "--delayed-role", "tweeter", "--delay-us", "128.588"]
    )
    assert cli._cmd_plan(args) == cli.EXIT_OK
    human = capsys.readouterr().out
    assert "tweeter" in human
    assert "128.588" in human

    payload = cli._walk_payload(cli._build_request(args), cli._resolved_level())
    assert payload["delayed_role"] == "tweeter"
    assert payload["delay_us"] == 128.588

    # The ordinary walk (no delay stated) prints no delay line at all -- the
    # same "only when it is not the ordinary walk" contract the polarity line
    # already keeps.
    plain = parser.parse_args(["plan", "--angles", "0"])
    assert cli._cmd_plan(plain) == cli.EXIT_OK
    assert "delay:" not in capsys.readouterr().out


def test_the_preview_says_who_arms_the_gate_for_both_movers(capsys):
    """The `plan` dry run is the operator's ONLY preview, so it may not state
    an arm-only fact as the whole truth (#2879 gate S1).

    It printed nothing about a gate for a person's walk, which read as "this
    walk has no hold" -- true before the pose-statement axis was split off the
    advance axis, and false after: a hand-walked round on the wired source
    holds every begin at exactly the bearing this preview lists.
    """
    parser = cli.build_parser()

    assert cli._cmd_plan(
        parser.parse_args(["plan", "--angles", "0,-7", "--mover", "human"])
    ) == cli.EXIT_OK
    human = capsys.readouterr().out
    assert "the SESSION decides the gate" in human
    assert "wired round holds every begin" in human
    # The per-stop target column stays EMPTY for a person, and that is the
    # seam's contract rather than an omission: whether the begins are held is
    # the session's fact, so the request may not guess one.
    assert "gate " not in human
    assert "advance tap" in human

    assert cli._cmd_plan(
        parser.parse_args(["plan", "--angles", "0,-7", "--mover", "arm"])
    ) == cli.EXIT_OK
    arm = capsys.readouterr().out
    assert "position gate armed" in arm
    assert "gate -7 deg" in arm


def test_the_mover_help_does_not_claim_the_gate_is_the_arms_alone(capsys):
    """The same claim, in the flag's own help — the other half of gate S1.

    Read off the `plan` SUBPARSER, which is where `--mover` lives; the
    top-level help never lists it.
    """
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["plan", "--help"])
    mover = " ".join(capsys.readouterr().out.split()).split("--mover", 1)[1]
    assert "the session holds it at that bearing when it is a wired round" in mover


def test_plan_writes_nothing(slot):
    path, _ = slot
    cli._cmd_plan(cli.build_parser().parse_args(["plan", "--angles", "0,7,-7"]))
    assert not path.exists()
    assert not spool.staged_angle_request_pending()


# --------------------------------------------------------------------------- #
# 3. the receipt
# --------------------------------------------------------------------------- #


def test_the_staged_document_round_trips_the_whole_walk(slot):
    path, _ = slot
    request = both_at([0, 7, -7], mover=MOVER_ARM)
    spool.stage_angle_request(request)

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["kind"] == spool.SPOOL_KIND
    assert doc["artifact_schema_version"] == spool.SPOOL_SCHEMA_VERSION
    assert doc["mover"] == MOVER_ARM

    assert spool.take_staged_angle_request() == request


def test_the_polarity_pair_rides_the_document_and_an_older_one_reads_as_normal(slot):
    """R-1's walk-level pair, from the operator's words to the banked walk.

    Walk-level, beside ``mover``, because the reverse-null is one act at one
    place: the pair names what the session's design-axis MEASURE capture rides,
    not what happens at a stop. ADDITIVE at the same schema version, so a
    document staged before the keys existed reads as a normal-polarity walk
    rather than as a refusal an operator has to re-stage past.
    """
    path, _ = slot
    args = cli.build_parser().parse_args([
        "stage", "--angles", "0",
        "--polarity", POLARITY_INVERTED,
        "--inverted-role", DRIVER_ROLE_TWEETER,
    ])
    assert cli._cmd_stage(args) == cli.EXIT_OK

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["polarity"] == POLARITY_INVERTED
    assert doc["inverted_role"] == DRIVER_ROLE_TWEETER
    assert spool.take_staged_angle_request() == AngleCaptureRequest(
        stops=(AngleStop(0, REGIME_PER_DRIVER),),
        polarity=POLARITY_INVERTED,
        inverted_role=DRIVER_ROLE_TWEETER,
    )

    older = {k: v for k, v in doc.items() if k not in ("polarity", "inverted_role")}
    assert older["artifact_schema_version"] == spool.SPOOL_SCHEMA_VERSION
    path.write_text(json.dumps(older), encoding="utf-8")
    taken = spool.take_staged_angle_request()
    assert (taken.polarity, taken.inverted_role) == (POLARITY_NORMAL, "")
    assert taken.stops == (AngleStop(0, REGIME_PER_DRIVER),)


def test_the_level_match_rides_the_document_and_an_older_one_reads_unmatched(slot):
    """The whole operator hop for the level match: flag, request, document,
    read-back — and a document staged before the key existed reading as an
    unmatched walk rather than as a refusal to re-stage past.

    A BOOLEAN travels, never numbers: the trims belong to the speaker and are
    resolved on the box when the host adopts the walk, so an operator cannot
    carry one cabinet's level match to another.
    """
    path, _ = slot
    args = cli.build_parser().parse_args([
        "stage", "--angles", "0", "--level-matched",
    ])
    assert cli._cmd_stage(args) == cli.EXIT_OK

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["level_matched"] is True
    # The document states WHETHER, never the dB.
    assert not [key for key in doc if key.endswith("_db")]
    assert spool.take_staged_angle_request() == AngleCaptureRequest(
        stops=(AngleStop(0, REGIME_PER_DRIVER),),
        level_matched=True,
    )

    older = {k: v for k, v in doc.items() if k != "level_matched"}
    assert older["artifact_schema_version"] == spool.SPOOL_SCHEMA_VERSION
    path.write_text(json.dumps(older), encoding="utf-8")
    taken = spool.take_staged_angle_request()
    assert taken.level_matched is False
    assert taken.stops == (AngleStop(0, REGIME_PER_DRIVER),)


def test_the_candidate_each_stop_measures_rides_the_document(slot):
    """The cycle's label, from the stated walk to the banked one.

    Per-STOP and not walk-level, because a candidate cycle is adjacent stops at
    one pose. ADDITIVE at the same schema version, so a document staged before
    the key existed reads as the walk that measures the speaker as it stands.
    """
    path, _ = slot
    request = AngleCaptureRequest(
        stops=(
            AngleStop(0, REGIME_PER_DRIVER, 0, "fp-a"),
            AngleStop(0, REGIME_PER_DRIVER, 0, "fp-b"),
        ),
    )
    spool.stage_angle_request(request)

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert [stop["candidate_id"] for stop in doc["stops"]] == ["fp-a", "fp-b"]
    assert spool.take_staged_angle_request() == request

    older = dict(doc, stops=[
        {k: v for k, v in stop.items() if k != "candidate_id"}
        for stop in doc["stops"]
    ])
    assert older["artifact_schema_version"] == spool.SPOOL_SCHEMA_VERSION
    path.write_text(json.dumps(older), encoding="utf-8")
    taken = spool.take_staged_angle_request()
    assert [stop.candidate_id for stop in taken.stops] == ["", ""]


def test_an_ordinary_walk_asks_for_no_level_match(slot):
    """The flag is opt-in: without it the staged document is what it was."""
    path, _ = slot
    args = cli.build_parser().parse_args(["stage", "--angles", "0"])
    assert cli._cmd_stage(args) == cli.EXIT_OK

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["level_matched"] is False
    assert spool.take_staged_angle_request().level_matched is False


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
    # …and it WAS consumed, so a bad document refuses once rather than forever.
    assert not spool.staged_angle_request_pending()


def test_an_unreadable_slot_refuses_without_consuming(slot, monkeypatch):
    """The documented exception to consume-on-refusal, pinned rather than claimed.

    There is no document here -- the bytes were never read -- and the fault is
    in the filesystem, not in what somebody staged. A rename that happened to
    succeed would destroy the only evidence of a permissions mistake, so this
    arm refuses loudly and repeatedly until the permissions are fixed. Every
    OTHER refusal consumes, which is what the assertion pair above pins.
    """
    path, _ = slot
    spool.stage_angle_request(per_driver_at([7]))

    real_read_bytes = type(path).read_bytes

    def _deny(self, *a, **k):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(type(path), "read_bytes", _deny)
    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.take_staged_angle_request()
    assert excinfo.value.reason == spool.SPOOL_MALFORMED
    assert "could not be read" in excinfo.value.detail
    # The slot is UNTOUCHED: no `.consumed` copy, and the pending file remains.
    assert spool.staged_angle_request_pending()
    assert not path.with_name(path.name + spool.CONSUMED_SUFFIX).exists()


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


def test_an_oversized_walk_is_refused_WITHOUT_being_read(slot, monkeypatch):
    """The cap is applied on the STAT, so the pathological file is never loaded.

    A cap applied after the read has already paid what it exists to avoid, and
    this gate runs on a 1 GB Pi. The pin is the honest one: ``read_bytes`` is
    made to explode if it is ever called on the slot, so a refusal that reached
    it fails here rather than merely being slow. The refusal must also report
    ``st_size`` — the number this arm actually judged.
    """
    path, _ = slot
    oversize = spool.SPOOL_MAX_BYTES + 4096
    path.write_bytes(b"{" + b"x" * (oversize - 1))

    def _explode(self, *a, **k):
        if self == path:
            raise AssertionError(
                "the oversized document was READ — the cap ran after the load"
            )
        return b""

    monkeypatch.setattr(type(path), "read_bytes", _explode)
    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        spool.take_staged_angle_request()

    assert excinfo.value.reason == spool.SPOOL_TOO_LARGE
    assert str(oversize) in excinfo.value.detail
    # …and it WAS consumed: an oversized document is still a document that has
    # had its session.
    assert not spool.staged_angle_request_pending()


def test_a_document_at_the_cap_is_read_normally(slot):
    """The positive control for the stat gate: the boundary is not off by one."""
    path, _ = slot
    spool.stage_angle_request(per_driver_at([7]))
    assert path.stat().st_size <= spool.SPOOL_MAX_BYTES
    assert spool.take_staged_angle_request() is not None


def test_consume_falls_back_to_unlink_when_the_rename_fails(slot, monkeypatch, caplog):
    """Single-use rests on the slot emptying, and this spool has no ordinal.

    ``prescription_spool`` can afford a purely best-effort rename because its
    ordinal check refuses a leftover anyway. A walk carries no ordinal, so a
    slot the rename left full is the same walk running again next session —
    which is why the unlink backstop is here and pinned rather than inherited
    as a comment.
    """
    path, _ = slot
    spool.stage_angle_request(per_driver_at([7]))

    def _no_rename(self, *a, **k):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(type(path), "replace", _no_rename)
    with caplog.at_level(
        logging.WARNING, logger="jasper.active_speaker.angle_capture_spool"
    ):
        assert spool.take_staged_angle_request() is not None

    # The slot IS empty — which is the property — even though the document
    # could not be filed for the operator.
    assert not spool.staged_angle_request_pending()
    assert not path.with_name(path.name + spool.CONSUMED_SUFFIX).exists()
    # …and losing the document is not silent: the operator is told the slot was
    # emptied the destructive way rather than the recoverable one.
    assert "event=angle_capture.request_consume_unlinked" in caplog.text


def test_a_consume_that_cannot_clear_the_slot_at_all_says_so(slot, monkeypatch, caplog):
    """A slot that will not clear can re-run a session's worth of captures."""
    path, _ = slot
    spool.stage_angle_request(per_driver_at([7]))

    monkeypatch.setattr(
        type(path), "replace", lambda self, *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    monkeypatch.setattr(
        type(path), "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("also nope"))
    )
    with caplog.at_level(logging.WARNING, logger="jasper.active_speaker.angle_capture_spool"):
        assert spool.take_staged_angle_request() is not None
    assert "event=angle_capture.request_consume_failed" in caplog.text


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
    """With jasper-control unreachable, the durable state is the fallback fact.

    The authority is jasper-control's measurement hold; this is the documented
    degradation for when it cannot be asked, and it is the answer this door
    gave before the hold existed.
    """
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
    assert cli._cmd_stage(args) == cli.EXIT_WRITE_FAILED
    assert "read-only file system" in capsys.readouterr().err


def test_withdraw_honors_the_same_exit_code_contract(slot, monkeypatch, capsys):
    """Every verb that touches the slot answers ``3``, or the contract is false.

    An unwritable slot directory made ``withdraw`` exit ``1`` with a
    ``PermissionError`` traceback, which tells a script neither "fix the
    request" nor "fix the speaker's filesystem" — the two things this module's
    documented exit codes exist to separate.
    """
    path, _ = slot
    spool.stage_angle_request(per_driver_at([0]))

    def _deny(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(path), "unlink", _deny)
    args = cli.build_parser().parse_args(["withdraw", "--json"])
    assert cli._cmd_withdraw(args) == cli.EXIT_WRITE_FAILED
    out = capsys.readouterr()
    assert json.loads(out.out)["reason"] == "stage_failed"
    assert "Permission denied" in out.err


def test_withdraw_is_quiet_and_zero_when_nothing_is_staged(slot, capsys):
    """The control for the arm above: an empty slot is not a failure."""
    args = cli.build_parser().parse_args(["withdraw", "--json"])
    assert cli._cmd_withdraw(args) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"ok": True, "withdrawn": False}


# --------------------------------------------------------------------------- #
# 4b. the program door and its receipt
# --------------------------------------------------------------------------- #


def test_stage_banks_a_named_program_with_its_receipt(slot, capsys):
    """The door a driver reads: one name in, a walk plus a price out.

    The two counts are asserted against the program row's own derived counts,
    not transcribed, so the table stays their one owner. ``ceiling_min`` is a
    literal because it prices the SESSION that takes this walk -- the base
    entries plus these stops -- which is not a number the row carries.
    """
    express = mp.program("baseline", "express")
    args = cli.build_parser().parse_args(
        ["stage", "--program", "baseline", "--size", "express", "--json"]
    )
    assert cli._cmd_stage(args) == cli.EXIT_OK
    body = json.loads(capsys.readouterr().out)

    assert body["program"] == "baseline/express"
    assert len(body["stops"]) == express.capture_count == 8
    assert body["price"] == {
        "mic_moves": express.mic_move_count,
        "captures": express.capture_count,
        "ceiling_min": 46,
    }
    assert body["handoff_url"].startswith("http://")
    assert body["handoff_url"].endswith(CROSSOVER_PAGE_PATH)

    taken = spool.take_staged_angle_request()
    assert taken == request_for_program(express)
    assert taken.program == "baseline/express"


def test_the_receipt_states_the_absolute_level_the_walk_drives_at(slot, capsys):
    """A named program's level is the banked anchor, in dB SPL, on the receipt.

    Every field beside it is what makes the number absolute, so a reader never
    has to guess whether it was measured or defaulted.
    """
    args = cli.build_parser().parse_args(
        ["stage", "--program", "baseline", "--size", "express", "--json"]
    )
    assert cli._cmd_stage(args) == cli.EXIT_OK

    assert json.loads(capsys.readouterr().out)["level"] == {
        "resolved": True,
        "anchor_db_spl": ANCHOR_DB_SPL,
        "reference_volume_db": REFERENCE_VOLUME_DB,
        "mic_serial": "8108494",
    }
    # Not written to the mailbox: nothing downstream reads a level yet, and a
    # field no consumer reads is a second copy waiting to go stale.
    assert "level" not in json.loads(pathlib.Path(slot[0]).read_text())


def test_stage_refuses_by_name_when_no_anchor_is_banked(
    slot, tmp_path, monkeypatch, capsys
):
    """§1a runs jasper-seat-level before anything measures; this names it."""
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE", str(tmp_path / "gone.json")
    )
    args = cli.build_parser().parse_args(
        ["stage", "--program", "baseline", "--size", "express", "--json"]
    )

    assert cli._cmd_stage(args) == cli.EXIT_REFUSED
    body = json.loads(capsys.readouterr().out)

    assert body["ok"] is False
    assert body["reason"] == slr.ANCHOR_UNUSABLE
    assert not spool.staged_angle_request_pending()

    # ``plan`` is the dry run that SHOWS what is missing rather than refusing.
    plan = cli.build_parser().parse_args(
        ["plan", "--program", "baseline", "--size", "express", "--json"]
    )
    assert cli._cmd_plan(plan) == cli.EXIT_OK
    unresolved = json.loads(capsys.readouterr().out)["level"]
    assert unresolved["resolved"] is False
    assert unresolved["reason"] == slr.ANCHOR_UNUSABLE
    # One slug for the three ways an anchor goes unusable, so the remedy is
    # only ever in the detail.
    assert "jasper-seat-level" in unresolved["detail"]


def test_a_spot_stages_one_raised_pose(slot, capsys):
    args = cli.build_parser().parse_args(
        ["stage", "--program", "spot", "--azimuth", "22", "--elevation", "10",
         "--json"]
    )
    assert cli._cmd_stage(args) == cli.EXIT_OK
    body = json.loads(capsys.readouterr().out)

    assert body["program"] == "spot"
    assert [(s["angle_deg"], s["elevation_deg"]) for s in body["stops"]] == [(22, 10)]
    assert spool.take_staged_angle_request().stops == (
        AngleStop(22, REGIME_PER_DRIVER, 10),
    )


def test_a_free_form_walk_is_unnamed_and_priced_by_the_same_rule(slot, capsys):
    """One receipt shape, whichever door the walk came through."""
    args = cli.build_parser().parse_args(["stage", "--angles", "0,7", "--json"])
    assert cli._cmd_stage(args) == cli.EXIT_OK
    body = json.loads(capsys.readouterr().out)

    assert body["program"] == ""
    assert body["price"] == {
        "mic_moves": 2,
        "captures": 2,
        "ceiling_min": math.ceil(
            wall_clock_ceiling_s(stage1_base_entries() + 2) / 60
        ),
    }
    assert spool.take_staged_angle_request().program == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["--program", "baseline", "--angles", "0"],
        ["--program", "baseline", "--azimuth", "5"],
        ["--program", "baseline", "--regime", "summed"],
        ["--program", "spot"],
        ["--angles", "0", "--candidates", "fp-a"],
        [],
    ],
    ids=["both-doors", "geometry-beside-a-row", "regime-beside-a-program",
         "spot-without-a-bearing", "candidates-beside-a-free-form-walk",
         "neither-door"],
)
def test_a_malformed_invocation_is_a_usage_error(argv):
    """Exit 2, from argparse, before anything is built or banked.

    ``--program`` and ``--angles`` are exclusive and one is required, and the
    geometry flags belong to exactly one of them; a walk assembled from a
    contradictory invocation would be a walk nobody stated.
    """
    with pytest.raises(SystemExit) as excinfo:
        # Parsed AND built, because the two doors are separated by argparse
        # itself while the geometry rules need the parsed values to judge.
        cli._build_request(cli.build_parser().parse_args(["plan", *argv]))

    # argparse's OWN usage exit, not this tool's vocabulary: the invocation
    # never reached a door that could refuse it.
    assert excinfo.value.code == ARGPARSE_USAGE_EXIT


def test_mutation_the_two_doors_cannot_both_be_open(slot):
    """The guard above only means something if the valid forms still parse."""
    parser = cli.build_parser()
    assert parser.parse_args(["plan", "--program", "baseline"]).angles is None
    assert parser.parse_args(["plan", "--angles", "0"]).program is None


def test_an_unknown_program_refuses_rather_than_walking_a_size_nobody_offers(
    slot, capsys
):
    """``--size`` carries no argparse ``choices``, so the registry refuses it.

    Which pairs the refusal names is the registry's own contract
    (``UnknownProgramError.choices``, pinned in
    ``tests/test_measurement_programs.py``); this door only has to reach it.
    """
    args = cli.build_parser().parse_args(
        ["plan", "--program", "baseline", "--size", "huge", "--json"]
    )
    assert cli._cmd_plan(args) == cli.EXIT_REFUSED
    body = json.loads(capsys.readouterr().out)

    assert (body["ok"], body["reason"]) == (False, cli.UNKNOWN_PROGRAM)


# --------------------------------------------------------------------------- #
# 5. the staged event has to be observable where an operator looks
#
# The same pin #2728 (A10) carries for the prescriber CLI, for the same defect:
# `stage_angle_request` emits `event=angle_capture.request_staged` right after
# the atomic write, and if the entrypoint configures no logging then
# `logging.lastResort` (WARNING and above) drops it — the tool's one state
# transition reaches neither the journal nor the operator's terminal, and a
# walk can be banked, or silently REPLACE another, with nothing saying so.
#
# These run the entrypoint in a SUBPROCESS on purpose. pytest installs its own
# root handler for every test, so an in-process `caplog` assertion captures the
# record whether or not anything configured logging — it would pass against the
# broken shape, which is the one thing this pin may not do.
# --------------------------------------------------------------------------- #

#: Runs the REAL ``cli.main`` with nothing but the slot path redirected, so the
#: logging wiring under test is the shipped one. Nothing in this script
#: configures a logger: if the entrypoint does not, the event has nowhere to go.
_STAGE_IN_A_REAL_PROCESS = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from jasper.active_speaker import angle_capture_spool as spool
    from jasper.cli import angle_capture as cli

    slot, volume_state, cal = sys.argv[1:4]
    spool.set_angle_request_spool_path_for_tests(Path(slot))
    import jasper.active_speaker.session_volume_plan as svp
    svp.DEFAULT_SESSION_VOLUME_STATE_PATH = Path(volume_state)
    import jasper.active_speaker.seat_level_reference as slr
    from jasper.audio_measurement import calibration
    _real = calibration.resolve_mic_sensitivity
    calibration.resolve_mic_sensitivity = lambda **_kw: _real(calibration_file=cal)
    slr._ceiling_db_spl = lambda: 90.0
    raise SystemExit(
        cli.main(["stage", "--angles", "0,7,-7,22,-22", "--regime", "per_driver"])
    )
    """
)


def _stage_in_a_real_process(tmp_path) -> subprocess.CompletedProcess:
    root = pathlib.Path(__file__).resolve().parent.parent
    reference = tmp_path / "seat_level_reference.json"
    write_seat_level_reference(
        reference_volume_db=REFERENCE_VOLUME_DB,
        measured_db_spl=ANCHOR_DB_SPL,
        target=SeatLevelTarget(target_db_spl=ANCHOR_DB_SPL, tolerance_db=2.5),
        sensitivity={"sens_factor_db": -12.07, "serial": "8108494"},
        max_main_volume_db=-6.0,
        state_path=reference,
    )
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE": str(reference),
    }
    return subprocess.run(
        [
            sys.executable, "-c", _STAGE_IN_A_REAL_PROCESS,
            str(tmp_path / "staged.json"), str(tmp_path / "absent-volume.json"),
            str(cal),
        ],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )


def test_the_staged_event_reaches_stderr_from_the_real_entrypoint(tmp_path):
    """The one state transition this CLI performs is observable.

    Asserted on a real process's stderr, which is exactly what an operator sees
    over SSH and what systemd hands the journal. Without the entrypoint's
    logging configuration this line does not exist anywhere — the record is
    created and dropped, because ``logging.lastResort`` starts at WARNING.
    """
    result = _stage_in_a_real_process(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "event=angle_capture.request_staged" in result.stderr
    # The fields an operator needs to tell one staging from another: how long
    # the walk is, who moves the microphone, what it plays, and — the one that
    # matters most — whether it silently replaced a walk already staged.
    assert "stops=5" in result.stderr
    assert "mover=human" in result.stderr
    assert "regimes=per_driver" in result.stderr
    assert "replaced=false" in result.stderr


def test_the_staged_event_goes_to_stderr_and_never_to_stdout(tmp_path):
    """stdout is the machine channel; a log line there would corrupt ``--json``."""
    result = _stage_in_a_real_process(tmp_path)

    assert "event=angle_capture.request_staged" not in result.stdout
    assert "staged at" in result.stdout


def test_configuring_logging_does_not_silence_the_human_summary(tmp_path):
    """The control: the walk an operator reads still prints, on stdout."""
    result = _stage_in_a_real_process(tmp_path)

    assert "5 stops, moved by human" in result.stdout
    assert "+22 deg" in result.stdout


# --------------------------------------------------------------------------- #
# 6. mutation on the dispatch
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
