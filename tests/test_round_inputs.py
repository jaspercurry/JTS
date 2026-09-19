# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
from datetime import datetime, timezone

import pytest

from jasper.active_speaker import bundles
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.commissioning_coordinator import next_program_action
from jasper.atomic_io import atomic_write_json
from jasper.json_fields import parse_utc_iso
from tests.test_active_speaker_commissioning_coordinator import _applied_anchor
from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds
from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS


def _bank_packet(directory, identity, program, **fields):
    (directory / "bundle" / directory.name).mkdir(parents=True)
    (directory / "packet.json").write_text(json.dumps({
        "applied": identity, "program": f"{program}/full", "result": "partial", **fields,
    }))


@pytest.mark.parametrize("has_room", [False, True])
@pytest.mark.parametrize("programs,limit,hits,applied_at,wanted", [
    (RUNNABLE_PROGRAMS, 32, {"speaker": 36, "rear": 37, "bass": 34, "room": 35}, None, None),
    (("speaker", "bass", "room"), 32, {"speaker": 36, "bass": 37, "room": 35}, None, ("speaker", "bass", "room")),
    (("speaker",), 32, {"speaker": 37}, None, None),
    (("speaker",), 32, {"speaker": 37}, None, ("speaker",)),
    (RUNNABLE_PROGRAMS, 2, {}, None, None),
    (("speaker",), 32, {"speaker": 39}, "1970-01-01T00:00:37Z", None),
])
def test_latest_banked_rounds_matches_identity_and_bounds_reads(monkeypatch, tmp_path, programs, limit, hits, applied_at, wanted, has_room):
    identity = {"candidate": "saved-speaker", "record": "abcdef012345", "applied_at": applied_at}
    alignment = {"saved": {"delay_us": 22}, "verification": {"residual_rms_db": .4, "repeat_noise_db": .2}}
    next_action = {"id": "continue"}
    root = tmp_path / "campaigns"
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    for index in range(40):
        directory = root / f"{index:02}"
        banked_identity = {**identity}
        if index > 37 and applied_at is None:
            banked_identity["candidate" if index == 39 else "record"] = "other"
        _bank_packet(directory, banked_identity, programs[index % len(programs)],
                     alignment_verdict=alignment, next_action=next_action,
                     room=[{"median": {"n_positions": 3}}] if has_room else [])
        if applied_at is not None:
            os.utime(directory, (index, index))
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
                            "round_id": f"{index:02}", "status": "partial",
                            "banked_at": (root / f"{index:02}").stat().st_mtime,
                            **({"alignment_verdict": alignment, "next_action": next_action}
                               if name == "speaker" else {})}
                     for name, index in hits.items()}
    assert opens.get("packet.json", 0) <= limit
    assert opens.get("provenance.json", 0) <= opens.get("packet.json", 0)


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
    action = next_program_action(profile, identity, before, programs=RUNNABLE_PROGRAMS)
    assert tuple(before) == ("room", "bass", "rear", "speaker")
    assert before["speaker"]["round_id"] == "speaker"
    assert before["room"]["banked_at"] == before["room"]["started_at"] == base + 5
    assert (action["program"], action["reason_code"]) == ("speaker", "complete")

    old = tmp_path / "campaigns" / "speaker-old"
    packet = old / "packet.json"
    atomic_write_json(packet, json.loads(packet.read_text()))
    os.utime(old, (base + 300, base + 300))

    after = latest_banked_rounds(identity)
    assert after == before
    assert tuple(after) == tuple(before)
    assert next_program_action(profile, identity, after, programs=RUNNABLE_PROGRAMS) == action
