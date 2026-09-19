# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.active_speaker import baseline_profile
from jasper.web import sound_ab_listen as ab


def series(fp, pose, level, **extra):
    return dict(candidate_id=fp * 64, position={"deg": pose}, freqs_hz=[40, 1000, 15999],
                magnitude_db=[level] * 3, kind="measurement", role="summed", window="ungated",
                base=fp == "a", **extra)


def bank(root, name, curves, mtime=1):
    folder = root / name
    folder.mkdir()
    path = folder / "frequency_view.json"
    path.write_text(json.dumps({"schema": "jts_frequency_view/1", "runs": [{"series": curves}]}))
    os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize("case", ["levels", "extra", "ignored", "one", "unshared", "bounds", "bad"])
def test_round_levels_and_bounds(tmp_path, monkeypatch, case):
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: None)
    monkeypatch.setattr(ab, "configured_volume_floor_db", lambda: -50)
    curves = [series(fp, pose, -20 + pose + offset)
              for fp, offset in [("a", 0), ("b", 1)] for pose in [0, 10]]
    if case == "extra":
        curves += [series("b", 10, -9), series("b", 99, 50)]
    if case == "ignored":
        curves += [dict(series("c", 0, 70), **patch) for patch in
                   [{"window": "gated"}, {"role": "woofer"}, {"candidate_id": None},
                    {"kind": "preview"}, {"magnitude_db": [float("nan")] * 3}]]
    if case == "one":
        curves = curves[:2]
    if case == "unshared":
        curves = [series("a", 0, -20), series("b", 10, -19)]
    bank(tmp_path, "first", curves)
    if case in {"bounds", "bad"}:
        for i in range(30):
            path = bank(tmp_path, str(i), curves, i + 2)
            if case == "bad" or i >= 14:
                path.write_text('broken' if i % 2 else '{"schema":"other"}')
            else:
                (path.parent / "packet.json").write_text(json.dumps({"program": str(i)}))
                (path.parent / "provenance.json").write_text(json.dumps({"banked_at_utc": str(i)}))
    reads = []
    original = ab.read_json_mapping
    def read(path):
        reads.append(Path(path))
        return original(path)
    monkeypatch.setattr(ab, "read_json_mapping", read)
    rounds = ab.ab_listen_state_payload(tmp_path)["rounds"]
    view_reads = [p for p in reads if p.name == "frequency_view.json"]
    assert len(view_reads) <= ab.MAX_FILES
    assert len(rounds) <= ab.MAX_ROUNDS
    if case in {"one", "unshared", "bad"}:
        assert rounds == []
        if case == "bad":
            assert len(view_reads) == ab.MAX_FILES
    else:
        assert [t["level_db"] for t in rounds[0]["tunes"]] == [-15.0, -14.0]
        assert [t["base"] for t in rounds[0]["tunes"]] == [True, False]
        if case == "bounds":
            assert [r["round_id"] for r in rounds] == [str(i) for i in range(13, 5, -1)]
            assert len(view_reads) == ab.MAX_FILES
            assert all(r["program"] == r["banked_at"] == r["round_id"] for r in rounds)


def test_payload_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state", lambda: {
        "candidate_fingerprint": "compiled", "source": {"measured_candidate_fingerprint": "a" * 64}})
    monkeypatch.setattr(ab, "configured_volume_floor_db", lambda: -40)
    bank(tmp_path, "round", [series("a", 0, -20), series("b", 0, -19)])
    (tmp_path / "round" / "packet.json").write_text('{"program":"room/arm"}')
    (tmp_path / "round" / "provenance.json").write_text('{"banked_at_utc":"today"}')
    payload = ab.ab_listen_state_payload(tmp_path)
    assert set(payload) == {"applied_fingerprint", "apply_path", "volume_step_db", "rounds"}
    assert payload["applied_fingerprint"] == "a" * 64
    assert payload["apply_path"] == ab.APPLY_PATH
    assert payload["volume_step_db"] == pytest.approx(40 / 99)
    assert payload["rounds"][0]["program"] == "room/arm"
    assert payload["rounds"][0]["banked_at"] == "today"


def test_ab_listen_js():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    result = subprocess.run([node, "tests/js/ab_listen_test.mjs"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
