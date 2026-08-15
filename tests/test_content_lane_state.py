# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The content-lane park surface (#2285 P9-C).

`deploy/bin/jasper-outputd-failure-reconcile` has written
`/run/jasper-outputd-content-lane.state` since the park landed, and until now
NOTHING read it: a parked speaker was silent on every operator surface. These
tests pin the reader, the doctor verdict it feeds, and — most importantly —
that the shell writer and the Python reader actually agree on the record's
shape, by running the real script and parsing its real output.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.cli.doctor import audio_runtime
from jasper.control import content_lane_state

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "deploy" / "bin" / "jasper-outputd-failure-reconcile"


def _shell_assignment(name: str) -> tuple[str, str]:
    """`(env_override, default)` from a `NAME="${ENV:-default}"` assignment."""
    text = _SCRIPT.read_text(encoding="utf-8")
    m = re.search(
        rf'^{name}="\$\{{([A-Z_]+):-([^}}]+)\}}"$', text, re.MULTILINE
    )
    assert m, f"{name} is no longer a ${{ENV:-default}} assignment"
    return m.group(1), m.group(2)


# --------------------------------------------------------------------------
# The literals the shell writer and the Python reader BOTH own
# --------------------------------------------------------------------------

def test_state_path_matches_the_writer():
    """A path duplicated across a shell writer and a Python reader is exactly
    the pair that drifts. Pin the default AND the override name — the reader
    honours the same env hook, so the tests and the operator both get one
    knob rather than two that can disagree."""
    env, default = _shell_assignment("CONTENT_LANE_STATE")
    assert content_lane_state.DEFAULT_STATE_PATH == default
    assert env == "JASPER_OUTPUTD_CONTENT_LANE_STATE"
    source = Path(content_lane_state.__file__).read_text(encoding="utf-8")
    assert env in source


def test_window_matches_the_writer():
    """'Currently failing' must mean the same thing on both sides."""
    env, default = _shell_assignment("CONTENT_LANE_WINDOW_SEC")
    assert str(content_lane_state.DEFAULT_WINDOW_SEC) == default
    assert env == "JASPER_OUTPUTD_CONTENT_LANE_WINDOW_SEC"
    source = Path(content_lane_state.__file__).read_text(encoding="utf-8")
    assert env in source


def test_park_reason_matches_the_writer():
    text = _SCRIPT.read_text(encoding="utf-8")
    assert f'reason={content_lane_state.PARK_REASON}' in text


def test_writer_records_the_lane():
    """The lane must be in the RECORD, not only the journal line — the reader
    surfaces it, and inferring it from the remedy prose would break on a
    reword."""
    text = _SCRIPT.read_text(encoding="utf-8")
    assert '"lane=${lane}"' in text


# --------------------------------------------------------------------------
# Reader verdicts
# --------------------------------------------------------------------------

def test_absent_record(tmp_path):
    snap = content_lane_state.snapshot(str(tmp_path / "nope.state"))
    assert snap == {"status": "absent", "parked": False}


def test_unreadable_record_is_not_absent(tmp_path):
    """A permissions regression must degrade loudly, never as a healthy
    speaker. A directory in the file's place raises IsADirectoryError without
    needing permission games that differ under root in CI."""
    target = tmp_path / "content-lane.state"
    target.mkdir()
    snap = content_lane_state.snapshot(str(target))
    assert snap["status"] == "unreadable"
    assert snap["parked"] is False
    assert snap["error"]


def test_parked_record_surfaces_lane_and_remedy(tmp_path):
    target = tmp_path / "content-lane.state"
    target.write_text(
        "count=4\n"
        "last_failure_epoch=1000\n"
        "parked_utc=2026-08-14T12:00:00Z\n"
        "reason=content_lane_open_repeated\n"
        "lane=active\n"
        "detail=jasper-outputd could not open its content lane on 4 consecutive starts\n"
        "action=re-arm the ACTIVE ring: jasper-active-speaker baseline-reemit\n"
        "re_arm=a deploy, a reboot, or an operator restart\n",
        encoding="utf-8",
    )
    snap = content_lane_state.snapshot(str(target))
    assert snap["status"] == "present"
    assert snap["parked"] is True
    assert snap["lane"] == "active"
    assert snap["count"] == 4
    assert "baseline-reemit" in snap["action"]
    assert "reboot" in snap["re_arm"]


def test_count_only_record_inside_window_is_a_live_streak(tmp_path):
    target = tmp_path / "content-lane.state"
    target.write_text("count=2\nlast_failure_epoch=1000\n", encoding="utf-8")
    snap = content_lane_state.snapshot(str(target), now=1010)
    assert snap["parked"] is False
    assert snap["streak_live"] is True
    assert snap["count"] == 2


def test_count_only_record_outside_window_is_stale(tmp_path):
    """The writer clears the record on a clean STOP, not on a successful
    start, so a stale record can outlive the problem it recorded."""
    target = tmp_path / "content-lane.state"
    target.write_text("count=2\nlast_failure_epoch=1000\n", encoding="utf-8")
    snap = content_lane_state.snapshot(
        str(target), now=1000 + content_lane_state.DEFAULT_WINDOW_SEC + 1
    )
    assert snap["parked"] is False
    assert snap["streak_live"] is False


def test_malformed_record_never_raises(tmp_path):
    target = tmp_path / "content-lane.state"
    target.write_text("this is not = = key value\n\x00\n", encoding="utf-8")
    snap = content_lane_state.snapshot(str(target))
    assert snap["parked"] is False


def test_unintelligible_record_is_not_reported_as_recovered(tmp_path):
    """An unparseable non-park record gets the SAME treatment as an unreadable
    one. Before this, it produced `ok | ... a stale record of None failure(s)
    remains (Nones old ...)` — unintelligible text and the opposite verdict
    from its own sibling branch."""
    target = tmp_path / "content-lane.state"
    target.write_text("count=banana\nlast_failure_epoch=nope\n", encoding="utf-8")
    snap = content_lane_state.snapshot(str(target))
    assert snap["status"] == "unintelligible"
    assert snap["parked"] is False
    assert snap["count"] is None
    # It must NOT claim a streak verdict it cannot support.
    assert "streak_live" not in snap


def test_truncated_record_is_unintelligible(tmp_path):
    """The reachable shape: a partial write that still renamed (ENOSPC)."""
    target = tmp_path / "content-lane.state"
    target.write_text("cou", encoding="utf-8")
    snap = content_lane_state.snapshot(str(target))
    assert snap["status"] == "unintelligible"


def test_park_record_without_lane_is_still_a_park(tmp_path):
    """BACK-COMPAT: /run wipes at boot, not on deploy, so a fleet box can
    carry a pre-`lane=` record across the upgrade. A park must still read as
    a park, with the lane reported as unknown rather than dropped."""
    target = tmp_path / "content-lane.state"
    target.write_text(
        "count=4\n"
        "last_failure_epoch=1000\n"
        "parked_utc=2026-08-14T12:00:00Z\n"
        "reason=content_lane_open_repeated\n"
        "detail=could not open its content lane on 4 consecutive starts\n"
        "action=match the content-lane width on both halves of the pair\n"
        "re_arm=a deploy, a reboot, or an operator restart\n",
        encoding="utf-8",
    )
    snap = content_lane_state.snapshot(str(target))
    assert snap["parked"] is True
    assert snap["lane"] is None


# --------------------------------------------------------------------------
# Doctor verdicts
# --------------------------------------------------------------------------

@pytest.fixture()
def state_file(monkeypatch, tmp_path):
    target = tmp_path / "content-lane.state"
    monkeypatch.setenv("JASPER_OUTPUTD_CONTENT_LANE_STATE", str(target))
    return target


def test_doctor_ok_when_no_record(state_file):
    result = audio_runtime.check_outputd_content_lane_park()
    assert result.status == "ok"
    assert "no content-lane failure record" in result.detail


def test_doctor_fails_on_a_park(state_file):
    """A park means the speaker emits NOTHING and no automatic path recovers
    it. That is operator-action class — `fail`, not `warn`."""
    state_file.write_text(
        "count=4\n"
        "last_failure_epoch=1000\n"
        "parked_utc=2026-08-14T12:00:00Z\n"
        "reason=content_lane_open_repeated\n"
        "lane=active\n"
        "detail=could not open its content lane on 4 consecutive starts\n"
        "action=re-arm the ACTIVE ring: jasper-active-speaker baseline-reemit\n"
        "re_arm=a deploy, a reboot, or an operator restart\n",
        encoding="utf-8",
    )
    result = audio_runtime.check_outputd_content_lane_park()
    assert result.status == "fail"
    assert "PARKED" in result.detail
    assert "active-lane" in result.detail
    # The record's own lane-specific remedy is surfaced VERBATIM, not restated.
    assert "baseline-reemit" in result.detail
    assert "RE-ARM:" in result.detail


def test_doctor_warns_on_a_live_streak(state_file, monkeypatch):
    import time as _time

    state_file.write_text("count=2\nlast_failure_epoch=1000\n", encoding="utf-8")
    monkeypatch.setattr(_time, "time", lambda: 1010.0)
    result = audio_runtime.check_outputd_content_lane_park()
    assert result.status == "warn"
    assert "2 consecutive failure(s)" in result.detail


def test_doctor_ok_on_a_stale_record(state_file, monkeypatch):
    import time as _time

    state_file.write_text("count=2\nlast_failure_epoch=1000\n", encoding="utf-8")
    monkeypatch.setattr(
        _time,
        "time",
        lambda: 1000.0 + content_lane_state.DEFAULT_WINDOW_SEC + 5,
    )
    result = audio_runtime.check_outputd_content_lane_park()
    assert result.status == "ok"
    assert "recovered" in result.detail


def test_doctor_warns_when_unreadable(state_file):
    state_file.mkdir()
    result = audio_runtime.check_outputd_content_lane_park()
    assert result.status == "warn"
    assert "could not" in result.detail


def test_doctor_warns_on_an_unintelligible_record(state_file):
    """N2: garbage must not read as a healthy speaker, and must never put
    `None` into operator text."""
    state_file.write_text("count=banana\nlast_failure_epoch=nope\n", encoding="utf-8")
    result = audio_runtime.check_outputd_content_lane_park()
    assert result.status == "warn"
    assert "do not parse" in result.detail
    assert "None" not in result.detail


def test_doctor_names_an_unknown_lane_on_a_pre_lane_record(state_file):
    """SF3/2: the back-compat path for a record written before `lane=` existed.
    Reachable on any fleet box upgraded without a reboot."""
    state_file.write_text(
        "count=4\n"
        "last_failure_epoch=1000\n"
        "parked_utc=2026-08-14T12:00:00Z\n"
        "reason=content_lane_open_repeated\n"
        "detail=could not open its content lane on 4 consecutive starts\n"
        "action=match the content-lane width on both halves of the pair\n"
        "re_arm=a deploy, a reboot, or an operator restart\n",
        encoding="utf-8",
    )
    result = audio_runtime.check_outputd_content_lane_park()
    assert result.status == "fail"
    assert "unknown-lane" in result.detail
    assert "None" not in result.detail


def test_doctor_check_is_registered():
    from jasper.cli.doctor import _registry

    names = [c.func.__name__ for c in _registry.registered_checks()]
    assert "check_outputd_content_lane_park" in names


# --------------------------------------------------------------------------
# /state wiring
# --------------------------------------------------------------------------

def test_state_aggregate_uses_the_same_reader():
    """One reader, two surfaces — doctor and /state cannot disagree."""
    source = (
        _REPO_ROOT / "jasper" / "control" / "state_aggregate.py"
    ).read_text(encoding="utf-8")
    assert '"content_lane": content_lane_state.snapshot()' in source
    doctor_source = (
        _REPO_ROOT / "jasper" / "cli" / "doctor" / "audio_runtime.py"
    ).read_text(encoding="utf-8")
    assert "content_lane_state.snapshot()" in doctor_source
