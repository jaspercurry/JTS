# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
from datetime import datetime, timezone

import pytest

from jasper.active_speaker import bundles
from jasper.active_speaker.applied_identity import BASE_LAYER, applied_identity
from jasper.active_speaker.commissioning_coordinator import next_program_action
from jasper.platform.atomic_io import atomic_write_json
from jasper.platform.json_fields import parse_utc_iso
from tests.test_active_speaker_commissioning_coordinator import _applied_anchor
from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds, packet_purposes, take_artifact_name
from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS, run_purpose


def _bank_packet(directory, identity, program, **fields):
    (directory / "bundle" / directory.name).mkdir(parents=True)
    (directory / "packet.json").write_text(json.dumps({
        "applied": identity, "preset": program, "result": "partial", "sets": [{"takes": [{"selected": True}]}],
        **fields,
    }))


@pytest.mark.parametrize("has_room", [False, True])
@pytest.mark.parametrize("programs,limit,hits,wanted", [
    (RUNNABLE_PROGRAMS, 32, {"speaker": 36, "rear": 37, "bass": 34, "room": 35}, None),
    (("speaker", "bass", "room"), 32, {"speaker": 36, "bass": 37, "room": 35}, ("speaker", "bass", "room")),
    (("speaker",), 32, {"speaker": 37}, None),
    (("speaker",), 32, {"speaker": 37}, ("speaker",)),
    (RUNNABLE_PROGRAMS, 2, {}, None),
])
def test_latest_banked_rounds_matches_identity_and_bounds_reads(monkeypatch, tmp_path, programs, limit, hits, wanted, has_room):
    identity = {"candidate": "saved-speaker", "record": "abcdef012345", "layer_fingerprints": {BASE_LAYER: "base"}}
    alignment = {"saved": {"delay_us": 22}, "verification": {"residual_rms_db": .4, "repeat_noise_db": .2}}
    next_action = {"id": "continue"}
    root = tmp_path / "campaigns"
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    for index in range(40):
        layers = {BASE_LAYER: "other"} if index > 37 else identity["layer_fingerprints"]
        _bank_packet(root / f"{index:02}", {**identity, "layer_fingerprints": layers}, programs[index % len(programs)],
                     alignment_verdict=alignment, next_action=next_action,
                     room=[{"median": {"n_positions": 3}}] if has_room else [])
    opens = {}
    original_open = Path.open

    def counted_open(path, *args, **kwargs):
        opens[path.name] = opens.get(path.name, 0) + 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    session_dir = root / "39" / "bundle" / "39" if limit == 2 else None
    kwargs = {"programs": wanted} if wanted is not None else {}
    found = latest_banked_rounds(identity, session_dir=session_dir, limit=limit, **kwargs)
    wanted = RUNNABLE_PROGRAMS if wanted is None else wanted
    hits = {**hits, **({"room": max(hits.values())} if has_room and hits and "room" in wanted else {})}
    assert found == {name: {"round_dir": str(root / f"{index:02}"),
                            "started_at": (root / f"{index:02}").stat().st_mtime,
                            "round_id": f"{index:02}", "status": "partial", "stale": False, "stale_by": [],
                            "banked_at": (root / f"{index:02}").stat().st_mtime,
                            **({"alignment_verdict": alignment, "next_action": next_action}
                               if name == "speaker" else {})}
                     for name, index in hits.items()}
    assert opens.get("packet.json", 0) <= limit
    assert opens.get("provenance.json", 0) <= opens.get("packet.json", 0)


@pytest.mark.parametrize("preset,cleared,change,stale_by", [
    ("speaker/mark", (), "corrections", ["speaker"]),
    ("speaker/mark", (), "rear_calibration", []),
    ("speaker/mark", (), "bass_extension", []),
    ("speaker/mark", (), "room_correction", []),
    ("nearfield/each", (), "corrections", []),
    ("nearfield/each", (), "rear_calibration", []),
    ("nearfield/each", (), "bass_extension", []),
    ("nearfield/each", (), "room_correction", []),
    ("rear/express", (), "linearization", ["speaker"]),
    ("rear/express", (), "rear_calibration", ["rear"]),
    ("rear/pair", ("rear_calibration",), "rear_calibration", []),
    ("rear/express", (), "bass_extension", []),
    ("bass/axis", ("room_correction",), "bass_extension", ["bass"]),
    ("bass/axis", ("room_correction",), "room_correction", []),
    ("room/seat", (), "rear_calibration", ["rear"]),
    ("room/seat", (), "bass_extension", ["bass"]),
    ("room/seat", (), "room_correction", ["room"]),
    ("room/seat", (), "preference", []),
])
def test_a_round_goes_stale_only_when_a_layer_under_it_changes(tmp_path, monkeypatch, preset, cleared, change, stale_by):
    """A round goes stale when a layer at or under its program changes, unless every kept take played that
    layer cleared. A near-field round plays no applied layer, and a preference EQ save changes none (ADR-0420)."""
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    before = _applied_anchor(layers=RUNNABLE_PROGRAMS)
    before["recomposition_snapshot"]["corrections"] = {"woofer": {"gain_db": 0.0}}
    after = json.loads(json.dumps(before))
    if change == "preference":
        after["config"].update(sha256="fedcba987654" * 5 + "fedc", sound_layer={"profile": {"bass_db": 3.0}})
    else:
        after["recomposition_snapshot"][change] = {"changed": True}
    takes = [{"selected": True, "cleared_layers": list(cleared), "pose": {"driver": "woofer" if preset == "nearfield/each" else None}}]
    _bank_packet(tmp_path / "campaigns" / "round", applied_identity(before), preset, sets=[{"takes": takes}])
    program = run_purpose(preset)

    found = latest_banked_rounds(applied_identity(after), programs=(program,), include_stale=True)[program]

    assert (found["stale"], found["stale_by"]) == (bool(stale_by), stale_by)


@pytest.mark.parametrize("timestamp_source", ["provenance", "finalized_at", "started_at", "session"])
def test_rewriting_old_packet_preserves_banked_order_and_next_action(tmp_path, monkeypatch, timestamp_source):
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    profile = _applied_anchor(layers=RUNNABLE_PROGRAMS)
    identity = applied_identity(profile)
    base = parse_utc_iso(identity["applied_at"])
    for age, name in enumerate(("speaker-old", "speaker", "rear", "bass", "room"), 1):
        directory = tmp_path / "campaigns" / name
        timestamp = base + age
        fields = ({"session": {"started_at": timestamp}} if timestamp_source == "session" else
                  {timestamp_source: timestamp} if timestamp_source != "provenance" else
                  {"finalized_at": timestamp + 100, "started_at": timestamp + 200})
        _bank_packet(directory, identity, name.split("-")[0], **fields)
        if timestamp_source == "provenance":
            (directory / "provenance.json").write_text(json.dumps({
                "banked_at_utc": datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }))
        os.utime(directory, (timestamp, timestamp))
    before = latest_banked_rounds(identity)
    action = next_program_action(profile, before, programs=RUNNABLE_PROGRAMS)
    assert tuple(before) == ("room", "bass", "rear", "speaker")
    assert before["speaker"]["round_id"] == "speaker"
    assert before["room"]["banked_at"] == before["room"]["started_at"] == base + 5
    assert (action["program"], action["reason_code"]) == (None, "complete")

    stale = tmp_path / "campaigns" / "speaker-stale"
    _bank_packet(stale, {**identity, "layer_fingerprints": {BASE_LAYER: "previous"}}, "speaker", finalized_at=base + 6)
    history = latest_banked_rounds(identity, include_stale=True)
    assert (history["speaker"]["round_id"], history["speaker"]["stale"]) == ("speaker-stale", True)
    assert history["room"]["stale"] is False

    old = tmp_path / "campaigns" / "speaker-old"
    packet = old / "packet.json"
    atomic_write_json(packet, json.loads(packet.read_text()))
    os.utime(old, (base + 300, base + 300))

    after = latest_banked_rounds(identity)
    assert after == before
    assert tuple(after) == tuple(before)
    assert next_program_action(profile, after, programs=RUNNABLE_PROGRAMS) == action


@pytest.mark.parametrize("drivers,counts", [(("woofer", "tweeter"), False), (("woofer", ""), True)],
                         ids=["every-take-one-driver", "one-summed-take"])
def test_a_round_of_only_one_driver_takes_is_not_its_programs_latest(tmp_path, monkeypatch, drivers, counts):
    """A speaker round of only one-driver takes holds no MEASURE take, so until
    #5696 it is not the latest speaker round, the one its next action and the
    room's staleness read; a reference round of them stays reference
    (ADR-0360 §2)."""
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    identity = {"candidate": "saved-speaker", "record": "abcdef012345", "applied_at": None}
    for name, finalized_at, poses in (("older", 1.0, [{"driver": None}]), ("newer", 2.0, [
            {"driver": driver or None} for driver in drivers])):
        _bank_packet(tmp_path / "campaigns" / name, identity, "speaker", finalized_at=finalized_at,
                     sets=[{"takes": [{"pose": pose, "selected": True} for pose in poses]}])
    assert latest_banked_rounds(identity)["speaker"]["round_id"] == ("newer" if counts else "older")
    assert packet_purposes({"preset": "nearfield/each",
                            "sets": [{"takes": [{"pose": {"driver": "woofer"}, "selected": True}]}]}) == ("reference",)


@pytest.mark.parametrize("sets,expected", [
    ([], ("run_program", "layer_not_applied")),
    ([{"takes": []}], ("run_program", "layer_not_applied")),
    ([{"takes": [{"selected": False}]}], ("run_program", "layer_not_applied")),
    ([{"takes": [{"selected": True}]}], ("copy_prompt", "round_available")),
], ids=["no sets", "no takes", "no kept take", "a kept take"])
def test_a_round_that_kept_no_take_is_not_available_to_its_program(tmp_path, monkeypatch, sets, expected):
    """A run that ended before it kept a take (an expired hold) holds nothing its program reads,
    so the next action offers a run, not a round to copy."""
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    profile = _applied_anchor(layers=("speaker",))
    identity = applied_identity(profile)
    _bank_packet(tmp_path / "campaigns" / "expired", identity, "rear", sets=sets)
    programs = RUNNABLE_PROGRAMS

    action = next_program_action(profile, latest_banked_rounds(identity, programs=programs), programs=programs)

    assert (action["id"], action["reason_code"], action["program"]) == (*expected, "rear")


def test_a_take_artifact_names_a_rear_target_without_a_colon():
    assert take_artifact_name("impulse.json", "take-1", "woofer:rear") == "impulse-take-1-woofer_rear.json"
