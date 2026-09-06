# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Wake-corpus privacy-mute enforcement tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jasper.wake_corpus import recording_backend
from jasper.web import wake_corpus_setup

from tests._log_events import event_records
from tests.wake_corpus_setup_fixtures import (
    _mute_backend_fixture,
    _mute_path_fixture,
    _patch_udp,
    _write_mute,
)

_IMPORTED_FIXTURES = (
    _mute_backend_fixture,
    _mute_path_fixture,
    _patch_udp,
)

# ---------------------------------------------------------------------------
# Mic mute — the recorder must honor the household privacy switch
# (jasper/mic_mute_persistence.py) because it records the bridge's UDP
# legs while jasper-voice (the usual mute enforcer) is stopped.
# ---------------------------------------------------------------------------


def test_begin_session_refused_while_muted(
    mute_backend, mute_path: Path, caplog,
) -> None:
    _write_mute(mute_path, True)
    with pytest.raises(wake_corpus_setup.MicMutedError, match="muted"):
        mute_backend.begin_session("jasper")
    assert mute_backend.session_id() is None
    assert event_records(caplog, "wake_corpus.mute_refused")


def test_start_recording_refused_when_mute_flips_after_session_begin(
    mute_backend, mute_path: Path,
) -> None:
    mute_backend.begin_session("jasper")
    _write_mute(mute_path, True)
    with pytest.raises(wake_corpus_setup.MicMutedError, match="muted"):
        mute_backend.start_recording("quiet", "near")
    assert not mute_backend.is_recording()


def test_unmuted_or_missing_file_records_normally(
    mute_backend, mute_path: Path,
) -> None:
    # Missing file (fail-safe: unmuted) — recording works.
    mute_backend.begin_session("jasper")
    mute_backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = mute_backend.stop_recording()
    assert clip.mute_stopped is False


def test_mute_mid_recording_stops_clip_and_flags_it(
    monkeypatch, mute_backend, mute_path: Path, caplog,
) -> None:

    monkeypatch.setattr(recording_backend, "MUTE_POLL_INTERVAL_SEC", 0.05)
    _write_mute(mute_path, False)
    mute_backend.begin_session("jasper")
    mute_backend.start_recording("quiet", "near")
    _write_mute(mute_path, True)

    deadline = time.time() + 5.0
    while time.time() < deadline:
        clips = mute_backend.list_clips()
        if clips:
            break
        time.sleep(0.02)
    else:
        pytest.fail("mute did not stop the recording within 5s")

    assert not mute_backend.is_recording()
    assert clips[-1].mute_stopped is True
    # A privacy stop is NOT the duration-cap auto-stop — downstream
    # tools must be able to tell them apart.
    assert clips[-1].auto_stopped is False
    assert event_records(caplog, "wake_corpus.mute_stop")
    # The flag persists into the session metadata sidecar.
    # RecordingBackend.stop_recording appends to the in-memory clip list
    # under the lock, then calls _save_metadata() OUTSIDE the lock on the
    # same background thread — so the list_clips() poll above can observe
    # the new clip before the sidecar write lands. Poll the sidecar itself,
    # with the same deadline discipline as above, instead of assuming a
    # single read already caught up (#2044).
    meta_dir = mute_backend._metadata_dir
    deadline = time.time() + 5.0
    data = None
    last_seen: object = "no sidecar file found"
    while time.time() < deadline:
        matches = list(meta_dir.glob("enroll_*.json"))
        if matches:
            candidate = json.loads(matches[0].read_text())
            candidate_clips = candidate.get("clips") or []
            last_seen = candidate_clips[-1] if candidate_clips else "clips list empty"
            if candidate_clips and candidate_clips[-1].get("mute_stopped") is True:
                data = candidate
                break
        time.sleep(0.02)
    else:
        pytest.fail(
            "sidecar did not reflect the mute-stopped clip within 5s; "
            f"last sidecar state read: {last_seen!r}"
        )
    assert data is not None
    assert data["clips"][-1]["mute_stopped"] is True


def test_post_session_handler_refuses_while_muted(
    tmp_path: Path, mute_path: Path,
) -> None:
    """The wizard surfaces the refusal as an HTTP 409 BEFORE any
    bridge-output side effects (no backend loop needed)."""
    _write_mute(mute_path, True)
    backend = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        mic_mute_path=mute_path,
    )  # intentionally not started — refusal must come first
    handler_cls = wake_corpus_setup._make_handler_class(backend, "tok")
    handler = handler_cls.__new__(handler_cls)
    sent: dict[str, object] = {}

    def _capture(status: int, msg: str) -> None:
        sent["status"] = status
        sent["msg"] = msg

    handler._send_error_json = _capture  # type: ignore[method-assign]
    handler._post_session({"member": "jasper"})
    assert sent["status"] == 409
    assert "muted" in str(sent["msg"])
