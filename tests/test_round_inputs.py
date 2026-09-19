# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path

import pytest

from jasper.active_speaker import bundles
from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds
from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS


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
        (directory / "bundle" / str(index)).mkdir(parents=True)
        banked_identity = {**identity}
        if index > 37 and applied_at is None:
            banked_identity["candidate" if index == 39 else "record"] = "other"
        (directory / "packet.json").write_text(json.dumps({
            "applied": banked_identity, "program": f"{programs[index % len(programs)]}/full", "result": "partial",
            "alignment_verdict": alignment, "next_action": next_action,
            "room": [{"median": {"n_positions": 3}}] if has_room else [],
        }))
        if applied_at is not None:
            os.utime(directory, (index, index))
    opens = 0
    original_open = Path.open

    def counted_open(path, *args, **kwargs):
        nonlocal opens
        opens += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    session_dir = root / "39" / "bundle" / "39" if limit == 2 else None
    kwargs = {"programs": wanted} if wanted is not None else {}
    found = latest_banked_rounds(identity, session_dir=session_dir, limit=limit, **kwargs)
    wanted = RUNNABLE_PROGRAMS if wanted is None else wanted
    hits = {**hits, **({"room": max(hits.values())} if has_room and hits and "room" in wanted else {})}
    assert found == {name: {"round_dir": str(root / f"{index:02}"),
                            "started_at": (root / f"{index:02}").stat().st_mtime,
                            **({"alignment_verdict": alignment, "next_action": next_action}
                               if name == "speaker" else {})}
                     for name, index in hits.items()}
    assert opens <= limit
    if applied_at is not None:
        assert opens == 2
    if len(found) == len(wanted):
        assert opens <= len(wanted) + 2
