# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest

from jasper.active_speaker import bundles
from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds


@pytest.mark.parametrize("programs,limit,hits", [
    (("speaker", "room", "bass"), 32, {"speaker": 36, "room": 37, "bass": 35}),
    (("speaker",), 32, {"speaker": 37}),
    (("speaker", "room", "bass"), 2, {}),
])
def test_latest_banked_rounds_matches_identity_and_bounds_reads(monkeypatch, tmp_path, programs, limit, hits):
    identity = {"candidate": "saved-speaker", "record": "abcdef012345"}
    root = tmp_path / "campaigns"
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    for index in range(40):
        directory = root / f"{index:02}"
        (directory / "bundle" / str(index)).mkdir(parents=True)
        banked_identity = {**identity}
        if index > 37:
            banked_identity["candidate" if index == 39 else "record"] = "other"
        (directory / "packet.json").write_text(json.dumps({
            "applied": banked_identity, "program": f"{programs[index % len(programs)]}/full", "result": "partial",
        }))
    opens = 0
    original_open = Path.open

    def counted_open(path, *args, **kwargs):
        nonlocal opens
        opens += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    session_dir = root / "39" / "bundle" / "39" if limit == 2 else None
    found = latest_banked_rounds(identity, session_dir=session_dir, limit=limit)
    assert found == {name: {"round_dir": str(root / f"{index:02}"),
                            "started_at": (root / f"{index:02}").stat().st_mtime}
                     for name, index in hits.items()}
    assert opens <= limit
    if len(found) == 3:
        assert opens <= 5
