# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Route tests for ``jasper.control.handlers.system``.

/healthz, /state (and the ``state_aggregate`` composition behind it),
/system/snapshot, /system/diagnostics, and the /system/* actions.
``jasper.control.single_flight.SingleFlightTTLCache``'s own pure unit
tests live in ``test_control_single_flight.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]

import pytest

from jasper.control import state_aggregate, usb_gadget_forensics
from jasper.control.server import _make_handler

from tests._cue_spy import SpyCues
from tests._librespot_state import write_librespot_state
from tests._wake_loop import wake_loop_for_tests
from tests.control_server_fixtures import (
    _explicit_passive_output_topology,
    _get,
    _isolate_household_secret,
    _post,
    _record_broker,
    _recording_popen,
    server_with_coordinator,
)

_IMPORTED_FIXTURES = (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    server_with_coordinator,
)


@pytest.fixture
def diagnostics_snapshot(monkeypatch, tmp_path):
    """Point /system/diagnostics at a private snapshot with no run in flight.

    The refresh window is process-global state, so every diagnostics test
    starts from "nothing running" or its own start would be swallowed.
    """
    import jasper.control.handlers.system as system_routes

    path = tmp_path / "doctor-result.json"
    monkeypatch.setattr(system_routes, "DOCTOR_RESULT_PATH", str(path))
    monkeypatch.setattr(system_routes, "_diagnostics_refresh_started_at", None)

    def write(payload: dict, *, age_seconds: float = 0.0) -> Path:
        body = dict(payload)
        body.setdefault("generated_at_epoch", time.time() - age_seconds)
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    write.path = path  # type: ignore[attr-defined]
    return write


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def _record_systemctl(
    monkeypatch, proc: type = _FakeProc, *, active_state: str = "activating",
) -> list[list[str]]:
    """Record every `systemctl` call, answering the diagnostics oneshot's
    ActiveState probe with ``active_state``."""
    import jasper.control.server as srv_mod

    started: list[list[str]] = []

    def fake_run(cmd: list[str], *_args: object, **_kwargs: object) -> object:
        started.append(cmd)
        if "--property=ActiveState" in cmd:
            return SimpleNamespace(returncode=0, stdout=active_state, stderr="")
        return proc()

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)
    return started


def _starts(recorded: list[list[str]]) -> list[list[str]]:
    return [cmd for cmd in recorded if "start" in cmd]


def test_diagnostics_serves_the_cached_oneshot_and_runs_no_doctor(
    server_with_coordinator, monkeypatch, diagnostics_snapshot,
):
    """A fresh snapshot is served verbatim and nothing runs the doctor here.

    jasper-control is non-root; the report is the ROOT
    jasper-doctor-json.service oneshot's. A doctor run from this process —
    in-process or by spawning the binary — reports ~7 permission failures as
    real ones.
    """
    from jasper.cli import doctor as doctor_mod
    from jasper.cli.doctor import _harness as doctor_harness
    import jasper.control.server as srv_mod

    cached = {
        "fails": 2,
        "warns": 1,
        "results": [
            {"name": "renderer alsa device", "status": "fail", "detail": "d1"},
            {"name": "camilla config", "status": "fail", "detail": "d2"},
            {"name": "journal", "status": "warn", "detail": "d3"},
        ],
    }
    diagnostics_snapshot(cached)

    ran: list[str] = []

    def _record(name: str):
        def _spy(*_args: object, **_kwargs: object) -> _FakeProc:
            ran.append(name)
            return _FakeProc()
        return _spy

    monkeypatch.setattr(srv_mod.subprocess, "run", _record("subprocess.run"))
    monkeypatch.setattr(srv_mod.subprocess, "Popen", _record("subprocess.Popen"))
    for name in ("main", "render_json"):
        monkeypatch.setattr(doctor_mod, name, _record(f"doctor.{name}"))
    # `run_async` resolves this in `_harness`'s own globals, so a
    # package-level patch would never be reached.
    monkeypatch.setattr(
        doctor_harness, "_build_doctor_checks", _record("doctor._build_doctor_checks"),
    )

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")

    assert status == 200
    assert ran == []
    assert {key: body[key] for key in cached} == cached
    assert body["stale"] is False


# (snapshot age, seconds since this process last started the oneshot,
#  starts issued by the request, reported `refreshing`).
_REFRESH_CASES = [
    # Fresh evidence: nothing to do.
    (10.0, None, 0, False),
    # Stale and nothing running: start one run.
    (120.0, None, 1, True),
    # The dashboard polls every 2 s; a run takes tens of seconds. The
    # snapshot is still older than that run, so the run is still going.
    (120.0, 5.0, 0, True),
    # A snapshot younger than the elapsed run IS that run's output: it
    # landed, went stale again, and a new run is due.
    (120.0, 300.0, 1, True),
    # Past the unit's start timeout nothing can still be activating.
    (700.0, 601.0, 1, True),
]


@pytest.mark.parametrize(
    "age_seconds,started_ago,expected_starts,expected_refreshing",
    _REFRESH_CASES,
)
def test_diagnostics_refresh_starts_only_while_none_is_in_flight(
    server_with_coordinator,
    monkeypatch,
    diagnostics_snapshot,
    age_seconds: float,
    started_ago: float | None,
    expected_starts: int,
    expected_refreshing: bool,
):
    import jasper.control.handlers.system as srv_mod

    diagnostics_snapshot(
        {"fails": 0, "warns": 0, "results": []}, age_seconds=age_seconds,
    )
    if started_ago is not None:
        monkeypatch.setattr(
            srv_mod,
            "_diagnostics_refresh_started_at",
            time.monotonic() - started_ago,
        )
    started = _record_systemctl(monkeypatch)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")

    assert status == 200
    assert body["refreshing"] is expected_refreshing
    assert _starts(started) == expected_starts * [[
        "systemctl", "--no-block", "start", "jasper-doctor-json.service",
    ]]


def test_a_run_that_died_without_writing_reopens_the_refresh_window(
    server_with_coordinator, monkeypatch, diagnostics_snapshot,
):
    """A oneshot OOM-killed or crashed before it wrote leaves no snapshot, so
    the elapsed-time test alone would hold the window for its full ceiling with
    no retry. Systemd is the authority on whether it is still running."""
    import jasper.control.handlers.system as srv_mod

    diagnostics_snapshot({"fails": 0, "warns": 0, "results": []}, age_seconds=120.0)
    monkeypatch.setattr(
        srv_mod, "_diagnostics_refresh_started_at", time.monotonic() - 5.0,
    )
    started = _record_systemctl(monkeypatch, active_state="failed")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")

    assert status == 200
    assert body["refreshing"] is True
    assert _starts(started) == [[
        "systemctl", "--no-block", "start", "jasper-doctor-json.service",
    ]]


@pytest.mark.parametrize("from_report", [True, False])
def test_diagnostics_age_comes_from_the_doctors_own_clock(
    server_with_coordinator, monkeypatch, diagnostics_snapshot, from_report: bool,
):
    """The doctor stamps `generated_at_epoch` when the run STARTED writing;
    mtime is only the fallback for a report that carries no stamp. Mixing the
    two made `cache_age_seconds` and `stale` disagree with the stamp the same
    payload reports."""
    payload: dict = {"fails": 0, "warns": 0, "results": []}
    if not from_report:
        payload["generated_at_epoch"] = None
    path = diagnostics_snapshot(payload, age_seconds=5.0)
    # mtime says something else entirely, so only one clock can be in use.
    mtime = time.time() - 4000.0
    os.utime(path, (mtime, mtime))
    _record_systemctl(monkeypatch)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")

    assert status == 200
    if from_report:
        assert body["cache_age_seconds"] < 60.0
        assert body["stale"] is False
    else:
        assert body["cache_age_seconds"] > 3000.0
        assert body["stale"] is True
        assert body["generated_at_epoch"] == pytest.approx(mtime)


def test_diagnostics_stale_cache_refresh_failure_is_visible(
    server_with_coordinator, monkeypatch, diagnostics_snapshot,
):
    """A stale snapshot is still served, but a failed background refresh must
    become a table row so the dashboard does not silently show old evidence."""
    from jasper.doctor_contract import REASON_REFRESH_FAILED

    diagnostics_snapshot(
        {
            "fails": 0,
            "warns": 0,
            "results": [{"name": "cached", "status": "ok", "detail": "old"}],
        },
        age_seconds=120.0,
    )

    class FailedProc:
        returncode = 1
        stdout = ""
        stderr = "Interactive authentication required."

    _record_systemctl(monkeypatch, FailedProc)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")

    assert status == 200
    assert body["stale"] is True
    assert body["refreshing"] is False
    assert body["fails"] == 1
    assert [r["name"] for r in body["results"]] == [
        "cached",
        "jasper-doctor refresh",
    ]
    refresh_row = body["results"][-1]
    assert refresh_row["status"] == "fail"
    assert refresh_row["reason"] == REASON_REFRESH_FAILED


def test_diagnostics_placeholder_when_snapshot_missing(
    server_with_coordinator, monkeypatch, diagnostics_snapshot,
):
    from jasper.doctor_contract import REASON_SNAPSHOT_PENDING

    _record_systemctl(monkeypatch)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")

    assert status == 200
    assert body["stale"] is True
    assert body["refreshing"] is True
    assert body["results"][0]["name"] == "jasper-doctor"
    assert body["results"][0]["reason"] == REASON_SNAPSHOT_PENDING


def test_diagnostics_fail_row_when_refresh_start_fails(
    server_with_coordinator, monkeypatch, diagnostics_snapshot,
):
    """A polkit denial / hard start failure should be visible in the
    diagnostics table without making the dashboard request itself a 502."""
    from jasper.doctor_contract import REASON_SNAPSHOT_UNAVAILABLE

    class FailedProc:
        returncode = 1
        stdout = ""
        stderr = "Interactive authentication required."

    _record_systemctl(monkeypatch, FailedProc)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")
    assert status == 200
    assert body["fails"] == 1
    assert body["refreshing"] is False
    assert body["results"][0]["status"] == "fail"
    assert body["results"][0]["reason"] == REASON_SNAPSHOT_UNAVAILABLE


def test_system_audio_quality_applies_and_try_restarts_renderers(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.handlers.system as system_mod

    applied: list[str] = []

    def fake_apply(converter: str) -> dict:
        applied.append(converter)
        return {
            "converter": converter,
            "active_converter": converter,
            "label": "Best",
            "summary": "Maximum ultrasonic-band fidelity.",
            "options": [],
        }

    monkeypatch.setattr(system_mod, "apply_requested_converter", fake_apply)
    calls = _record_broker(monkeypatch)

    status, body = _post(
        f"{base}/system/audio-quality",
        {"converter": "best"},
    )

    assert status == 202
    assert body["ok"] is True
    assert body["status"] == "accepted"
    assert applied == ["samplerate_best"]
    assert body["audio_quality"]["converter"] == "samplerate_best"
    # `intent_saved` is the refusal's consolation prize ("we kept your
    # choice, the restart did not happen"). On an ok it reads as a hedge.
    assert "intent_saved" not in body
    from jasper.local_sources import local_source_audio_refresh_units

    assert calls == [
        ("try-restart", [unit]) for unit in local_source_audio_refresh_units()
    ]
    assert body["accepted_units"] == list(local_source_audio_refresh_units())


def test_system_audio_quality_502s_when_the_renderer_restart_is_refused(
    monkeypatch,
    server_with_coordinator,
):
    """A broker refusal (allowlist denial, broker down, nonzero systemctl) must
    reach the caller, not be papered over with ok:true."""
    base, _ = server_with_coordinator
    import jasper.control.handlers.system as system_mod

    monkeypatch.setattr(
        system_mod,
        "apply_requested_converter",
        lambda converter: {"converter": converter, "active_converter": converter},
    )
    _record_broker(monkeypatch, ok=False)

    status, body = _post(f"{base}/system/audio-quality", {"converter": "best"})

    assert status == 502
    assert body["code"] == "audio_quality_restart_failed"
    assert body["intent_saved"] is True
    assert body.get("ok") is not True


def test_system_audio_quality_rejects_unknown_converter(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.handlers.system as system_mod

    def fail_apply(_converter: str) -> dict:
        raise AssertionError("invalid converter should not apply")

    monkeypatch.setattr(system_mod, "apply_requested_converter", fail_apply)

    status, body = _post(
        f"{base}/system/audio-quality",
        {"converter": "linear"},
    )

    assert status == 400
    assert "unsupported ALSA rate converter" in body["error"]


def test_system_audio_quality_rejects_missing_converter(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.handlers.system as system_mod

    def fail_apply(_converter: str) -> dict:
        raise AssertionError("missing converter should not apply")

    monkeypatch.setattr(system_mod, "apply_requested_converter", fail_apply)

    status, body = _post(f"{base}/system/audio-quality", {})

    assert status == 400
    assert body["error"] == "converter is required"


def test_system_usb_latency_applies_fixed_mode(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.handlers.system as system_mod
    import jasper.control.server as srv_mod

    applied: list[str] = []
    marked: list[str] = []
    monkeypatch.setattr(
        system_mod,
        "apply_requested_mode",
        lambda mode: applied.append(mode),
    )
    monkeypatch.setattr(
        srv_mod,
        "_mark_usb_latency_applying",
        lambda mode: marked.append(mode),
    )

    status, body = _post(f"{base}/system/usb-latency", {"mode": "medium"})

    assert status == 200
    assert applied == ["medium"]
    assert marked == ["medium"]
    assert body == {"ok": True, "action": "usb-latency", "mode": "medium"}


def test_system_usb_latency_surfaces_apply_failure(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.handlers.system as system_mod

    def fail(_mode: str) -> None:
        raise system_mod.LatencyApplyError("fan-in restart failed")

    monkeypatch.setattr(system_mod, "apply_requested_mode", fail)

    status, body = _post(f"{base}/system/usb-latency", {"mode": "high"})

    assert status == 502
    assert body["selected_mode"] == "high"
    assert "fan-in restart failed" in body["error"]


def test_usb_forensics_persists_intent_and_queues_fixed_action(
    monkeypatch, tmp_path, server_with_coordinator,
):
    from jasper.control import usb_gadget_forensics as forensics

    enabled = tmp_path / "forensics.env"
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setattr(forensics, "ENABLED_FILE", str(enabled))
    monkeypatch.setattr(forensics, "RUNTIME_DIR", str(runtime))
    base, _ = server_with_coordinator

    status, body = _post(
        f"{base}/usb-forensics", {"action": "set_enabled", "enabled": True},
    )
    assert status == 200
    assert body["enabled"] is True
    assert "JASPER_USB_GADGET_FORENSICS=1" in enabled.read_text()

    (runtime / "status.json").write_text(json.dumps({
        "running": True, "last_sample_at": time.time(), "sample_count": 2,
    }))
    status, body = _post(f"{base}/usb-forensics", {"action": "capture"})
    assert status == 202
    assert body["running"] is True
    assert body["pending_action"] == "capture"
    assert (runtime / "request.capture").exists()


@pytest.mark.parametrize(
    "elapsed_sec, expect_running",
    [
        # A legally slow repair (deploy/usbsink/jasper-usbgadget-snapshot's
        # REPAIR_RESTART_TIMEOUT_SEC-bounded restart plus its two
        # jasper_usbgadget_refresh_consumers try-restarts) blocks status.json
        # from being rewritten for up to REPAIR_WORST_CASE_SEC. That must
        # still read "running", not false-alarm.
        (usb_gadget_forensics.REPAIR_WORST_CASE_SEC - 10.0, True),
        # Genuine staleness beyond the widened window must still be caught.
        (usb_gadget_forensics.REPAIR_WORST_CASE_SEC + 40.0, False),
    ],
)
def test_usb_forensics_freshness_tolerates_a_slow_repair(
    monkeypatch, tmp_path, elapsed_sec, expect_running,
):
    monkeypatch.setattr(usb_gadget_forensics, "ENABLED_FILE", str(tmp_path / "forensics.env"))
    (tmp_path / "forensics.env").write_text("JASPER_USB_GADGET_FORENSICS=1\n")
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setattr(usb_gadget_forensics, "RUNTIME_DIR", str(runtime))
    (runtime / "status.json").write_text(json.dumps({
        "running": True,
        "sample_interval_sec": 10,
        "last_sample_at": time.time() - elapsed_sec,
    }))

    assert usb_gadget_forensics.snapshot()["running"] is expect_running


def test_usb_forensics_rejects_malformed_toggle(
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    status, body = _post(
        f"{base}/usb-forensics", {"action": "set_enabled", "enabled": "yes"},
    )
    assert status == 400
    assert body["error"] == "enabled must be a boolean"


@pytest.mark.parametrize("verb", ["reboot", "poweroff"])
def test_system_action_reboot_audits_and_asks_the_broker(
    monkeypatch,
    server_with_coordinator,
    caplog,
    verb,
):
    """A destructive /system/ action emits an `event=system.action` audit line
    (so a dashboard-triggered reboot/poweroff is distinguishable from a
    watchdog/crash reset when debugging "the speaker restarted on its own")
    and reaches systemd only through the restart broker — never its own
    subprocess. The box goes down before any verdict, so the answer is 202
    accepted, never a claim that the action happened."""
    import logging

    base, _ = server_with_coordinator
    broker_calls = _record_broker(monkeypatch)

    with caplog.at_level(logging.INFO, logger="jasper.control"):
        status, body = _post(f"{base}/system/{verb}", {})

    assert status == 202
    assert body["action"] == verb
    assert body["status"] == "accepted"
    assert broker_calls == [(verb, [])]
    assert any(
        f"event=system.action action={verb}" in rec.getMessage()
        for rec in caplog.records
    ), f"{verb} must emit an event=system.action audit line"


@pytest.mark.parametrize("verb", ["reboot", "poweroff"])
def test_system_action_reboot_survives_a_dead_broker(
    monkeypatch,
    server_with_coordinator,
    verb,
):
    """The broker's socket bind is deliberately non-fatal, so the dashboard
    reboot/poweroff must not hard-depend on it: with the socket unreachable
    jasper-control still spawns the action itself and answers 202."""
    import jasper.control.restart_broker as rb

    base, _ = server_with_coordinator
    popens: list[list[str]] = []

    def _no_socket(*_a, **_kw):
        raise rb.BrokerUnavailable("no such file")

    monkeypatch.setattr(rb, "request_restart", _no_socket)
    monkeypatch.setattr(rb.subprocess, "Popen", _recording_popen(popens))
    # The dead-broker fallback is gated the same way as the broker itself;
    # grant this process's own euid rather than relying on the runner being
    # root or jasper-control.
    monkeypatch.setattr(rb, "_power_verb_uids", lambda: (os.geteuid(),))

    status, body = _post(f"{base}/system/{verb}", {})

    assert status == 202
    assert body["status"] == "accepted"
    assert popens == [["systemctl", verb]]


def test_system_snapshot_audio_quality_fails_soft(
    monkeypatch,
):
    from jasper.control.handlers import system as system_handlers

    def fail_state() -> dict:
        raise ValueError("unsupported ALSA rate converter 'linear'")

    monkeypatch.setattr(system_handlers, "_read_audio_quality_state", fail_state)
    monkeypatch.setattr(
        system_handlers,
        "_read_active_audio_converter",
        lambda: "samplerate_medium",
    )

    body = system_handlers._safe_audio_quality_state()

    assert body["converter"] == "samplerate_medium"
    assert body["active_converter"] == "samplerate_medium"
    assert "unsupported ALSA rate converter" in body["error"]


def test_system_snapshot_legacy_endpoint_token_reports_streambox_caps(
    monkeypatch,
    server_with_coordinator,
):
    # A persisted legacy "endpoint" marker normalizes to streambox; the
    # capabilities payload reflects streambox, not a removed third role.
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "endpoint")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/snapshot")

    assert status == 200
    caps = body["system_capabilities"]
    assert caps["install_profile"] == "endpoint"  # raw token preserved
    assert caps["role"] == "streambox"            # normalized role
    assert caps["voice_brain"] is True
    assert caps["wake_detection"] is False
    assert caps["developer_tools"] is False
    assert caps["network_settings"] is True
    assert caps["reboot"] is True
    assert caps["poweroff"] is True
    assert "unavailable_reason" not in caps


def test_system_snapshot_reports_full_capabilities(
    monkeypatch,
    server_with_coordinator,
):
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "full")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/snapshot")

    assert status == 200
    caps = body["system_capabilities"]
    assert caps["install_profile"] == "full"
    assert caps["role"] == "full"
    assert caps["voice_brain"] is True
    assert caps["developer_tools"] is True


def test_system_snapshot_reports_streambox_capabilities(
    monkeypatch,
    server_with_coordinator,
):
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "streambox")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/snapshot")

    assert status == 200
    caps = body["system_capabilities"]
    assert caps["install_profile"] == "streambox"
    assert caps["role"] == "streambox"
    assert caps["local_sources"] is True
    assert caps["content_dsp"] is True
    assert caps["voice_brain"] is True
    assert caps["wake_detection"] is False
    assert caps["audio_quality"] is True
    assert caps["restart_voice"] is True
    assert caps["restart_audio"] is True
    assert caps["network_settings"] is True
    assert caps["speaker_settings"] is True
    assert caps["pair_management"] is True
    assert caps["developer_tools"] is False
    assert caps["reboot"] is True
    assert caps["poweroff"] is True
    assert "unavailable_reason" not in caps


# --- routes ---


def test_healthz(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body == {"ok": True}


# --- /state aggregation ---


def test_sound_runtime_status_flags_base_config_mismatch() -> None:
    runtime = state_aggregate._sound_runtime_status(
        {
            "enabled": True,
            "filter_count": 3,
            "last_dsp_apply": {
                "result": "success",
                "active_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
            },
        },
        "/etc/camilladsp/outputd-cutover.yml",
    )

    assert runtime["state"] == "base"
    assert runtime["active"] is False
    assert runtime["matches_last_apply"] is False
    assert "not the active" in runtime["warning"]


def test_state_returns_snapshot_with_fail_soft_sections(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """GET /state aggregates across daemons. In a unit test no daemon
    is reachable (no camilla, no shairport, no voice UDS), so each
    section comes back as null/None — but the response is still 200
    with a stable top-level shape."""
    base, _ = server_with_coordinator

    state_path = tmp_path / "speaker_volume.json"
    state_path.write_text(
        '{"listening_level": 73, "main_volume_db": -13.5}',
    )
    dsp_apply = tmp_path / "dsp_apply_state.json"
    dsp_apply.write_text(json.dumps({
        "source": "sound",
        "phase": "done",
        "result": "success",
    }))
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(state_path))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(dsp_apply))
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing_sound.json"))
    # Provider comes from the wizard-owned SSOT file, read fresh — NOT from
    # os.environ. Write the file AND set a *different* stale env value to
    # prove the file is authoritative: jasper-control keeps a frozen
    # JASPER_VOICE_PROVIDER across a switch (it isn't restarted), so the
    # file must win. This is the regression guard for the stale-/system/
    # bug.
    provider_file = tmp_path / "voice_provider.env"
    provider_file.write_text(
        "JASPER_VOICE_PROVIDER=openai\nJASPER_OPENAI_MODEL=gpt-realtime-2\n"
        # Per-provider barge-in flag lives in the same wizard-owned SSOT
        # file; /state must read it fresh (jasper-control isn't restarted
        # on a toggle). openai=on, gemini absent — proves per-provider.
        "JASPER_BARGE_IN_OPENAI=1\n"
    )
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(provider_file))
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")  # stale env, must be ignored
    # Point librespot state at a missing file → empty dict.
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "missing.env"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    assert "ts" in body
    assert body["voice"]["provider"] == "openai"
    assert body["voice"]["provider_status"] == "configured"
    assert body["voice"]["provider_error"] is None
    assert body["voice"]["reachable"] is False
    assert body["voice"]["session_active"] is False
    assert "music_dbfs" in body["voice"]
    # /state.voice is hand-curated, NOT a session_status pass-through, so a
    # new session_status field is silently dropped if it isn't pulled
    # through in _get_state. wake_legs (jasper-doctor's runtime cross-check
    # source) is exactly such a field — guard that its key is present.
    assert "wake_legs" in body["voice"]
    # tool_packs is the same shape of curated pull-through (jasper-doctor's
    # check_tool_packs cross-checks it against the static registry).
    assert "tool_packs" in body["voice"]
    # endpointer is the third such field: which mechanism closed the turn's
    # user input (push_to_talk / silero_aec). Pinned HERE, at
    # runtime and under its published name, because that is what a client
    # actually reads — a source-level check passes even when the key ships
    # misspelled.
    assert "endpointer" in body["voice"]
    # barge_in.enabled is read FRESH per active provider (openai) from the
    # same wizard file — the regression guard for the fresh-reader rationale.
    # Voice is unreachable here, so the firing stats are null.
    assert body["voice"]["barge_in"]["enabled"] is True
    assert body["voice"]["barge_in"]["count_session"] is None
    assert body["voice"]["barge_in"]["last_at"] is None
    assert body["audio"]["listening_level_percent"] == 73
    # Camilla isn't reachable from the test → main_volume_db None.
    assert body["audio"]["main_volume_db"] is None
    assert body["audio"]["playback_rms_dbfs"] is None
    assert body["audio"]["playback_peak_dbfs"] is None
    assert body["audio"]["clipped_samples"] is None
    assert body["audio"]["sound"]["curve_id"] == "flat"
    assert body["audio"]["sound"]["filter_count"] == 0
    assert body["audio"]["sound"]["last_dsp_apply"]["result"] == "success"
    assert body["audio"]["sound"]["runtime_state"] == "unknown"
    assert body["audio"]["camilla_active_config_path"] is None
    assert body["outputd"] is None
    assert body["active_source"] in {"idle", "airplay"}
    assert "satellites" not in body


def _pinned_state_keys() -> set[str]:
    from tests.test_wire_contracts import _STATE_KEY_SETS

    return _STATE_KEY_SETS[()]


@pytest.mark.parametrize("refuse_spawns", [False, True])
def test_state_wire_key_set_is_the_pinned_set(
    server_with_coordinator, monkeypatch, tmp_path, refuse_spawns,
):
    """What a client receives is what tests/test_wire_contracts.py pins.

    The aggregate builds every top-level key, so a key bolted on in the
    handler instead would reach consumers unpinned — the nesting-drift class
    the key-set pin exists for. The second case refuses every spawn primitive:
    no per-request process may be load-bearing for the payload (ADR-0233
    rule 2), so a probe that forks and lets the failure escape takes it to
    502. Retire with the key-set pin.
    """
    if refuse_spawns:
        def refuse(*_args, **_kwargs):
            raise AssertionError("/state must not depend on a spawned process")

        monkeypatch.setattr(subprocess, "run", refuse)
        monkeypatch.setattr(subprocess, "Popen", refuse)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
        monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse)
    base, _ = server_with_coordinator
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.env"))

    status, body = _get(f"{base}/state")

    assert status == 200
    assert set(body) == _pinned_state_keys()


async def test_state_section_read_past_the_deadline_reports_unavailable(
    monkeypatch, tmp_path,
):
    """One deadline bounds the whole payload, not just the daemon fan-out.

    A section read that never returns used to park the compute, and with it
    every /state client behind the single-flight cache. It now costs that
    section only: the key stays, its value is the same null every other
    fail-soft section serves. Retire with the deadline in _get_state.
    """
    from tests.test_wire_contracts import _state_payload

    released = threading.Event()

    def wedged_sound_profile():
        released.wait(timeout=30)
        return {}

    monkeypatch.setattr(
        state_aggregate, "_read_sound_profile", wedged_sound_profile,
    )
    monkeypatch.setattr(state_aggregate, "_STATE_AGGREGATE_BUDGET_SEC", 1.0)
    started = time.monotonic()
    try:
        payload = await _state_payload(monkeypatch, tmp_path)
    finally:
        released.set()

    assert time.monotonic() - started < 15.0
    assert set(payload) == _pinned_state_keys()
    assert payload["audio"]["sound"] is None


@pytest.mark.parametrize(
    "overall_status, expected",
    [("ok", "usbsink"), ("unknown", "idle")],
)
async def test_state_active_source_is_the_health_samplers_verdict(
    monkeypatch, tmp_path, overall_status, expected,
):
    """One active_source on the wire (ADR-0233 rule 2).

    The audio-health sampler and the aggregate's renderer ladder are two
    candidate answers in one response, free to name different sources unless
    one of them defers. A stale sampler keeps its last lane verbatim under
    `status: unknown`, which must not outrank the ladder's live answer.
    Retire when the sampler stops publishing a source.
    """
    from tests.test_wire_contracts import _state_payload

    payload = await _state_payload(
        monkeypatch, tmp_path,
        audio_health_snapshot=lambda: {
            "overall": {"status": overall_status, "active_source": "usbsink"},
        },
    )

    assert payload["active_source"] == expected
    assert payload["audio_health"]["overall"]["active_source"] == "usbsink"


async def test_state_outputd_section_drops_the_chip_ref_write_ring():
    """~25 KB of every response that no /state consumer reads: jasper-aec-init
    takes the ring off outputd's socket. Retire if a /state reader needs it.
    """
    async def status(_path, *_args, **_kwargs):
        return {"reference_outputs": {"chip_ref_writer": {
            "active": True,
            "recent_writes": [{"frames_written": 128}],
            "recent_writes_capacity": 256,
        }}}

    body = await state_aggregate._outputd_status(local_status_json=status)

    writer = body["reference_outputs"]["chip_ref_writer"]
    assert "recent_writes" not in writer
    assert writer["recent_writes_capacity"] == 256


def test_state_voice_wake_legs_flows_from_session_status(
    server_with_coordinator, monkeypatch,
):
    """Regression for the curated-vs-passthrough drop: /state.voice is
    hand-built in _get_state, so a session_status field (here wake_legs —
    the runtime-armed legs jasper-doctor cross-checks against configured
    intent) only reaches /state if it's explicitly pulled through. Before
    that pull-through, wake_legs lived in session_status but was absent
    from /state.voice, silently disabling the doctor's runtime check."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    async def fake_status(socket_path, cmd, timeout=None):  # noqa: ARG001
        return {
            "state": "WAKE", "input_ended": False, "spend_allowed": True,
            "connection_paused": False, "mic_muted": False,
            "duck_active": False, "music_dbfs": -32.0,
            "wake_legs": ["on", "off", "dtln"],
        }
    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_status)

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["voice"]["reachable"] is True
    assert body["voice"]["wake_legs"] == ["on", "off", "dtln"]
    # Same pull-through, for the per-turn latency deltas.
    assert "last_turn_ms" in body["voice"]


def test_state_voice_classifies_every_session_status_field():
    from jasper.control import state_aggregate

    # With a cue manager attached, so a field only a configured daemon
    # reports is inside the closed set rather than skipped past it.
    wl = wake_loop_for_tests(cues=SpyCues())
    status_keys = frozenset(wl.session_status())
    published = state_aggregate._VOICE_STATUS_PUBLISHED_KEYS
    withheld = state_aggregate._VOICE_STATUS_WITHHELD_KEYS

    assert published.isdisjoint(withheld)
    assert status_keys == published | withheld


def test_state_audio_projects_temporary_mute_as_zero(
    server_with_coordinator,
    monkeypatch,
    tmp_path,
):
    """The dashboard aggregate consumes the same effective projection as /volume."""
    base, _ = server_with_coordinator
    state_path = tmp_path / "speaker_volume.json"
    state_path.write_text(json.dumps({
        "listening_level": 60,
        "pre_mute_level": 60,
        "mute_token": "remote-mute",
        "main_volume_db": -10.0,
    }))
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(state_path))

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["audio"]["listening_level_percent"] == 0


def test_state_voice_tool_packs_flows_from_session_status(
    server_with_coordinator, monkeypatch,
):
    """Same curated-vs-passthrough regression as wake_legs, for tool_packs:
    jasper-voice's session_status reports per-pack registration outcomes,
    and /state.voice must pull the field through for jasper-doctor's
    check_tool_packs to see runtime truth (a pack that failed to build)."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    packs = [
        {"name": "audio", "status": "registered", "tool_count": 5,
         "error": None},
        {"name": "spotify", "status": "failed", "tool_count": 0,
         "error": "ImportError('spotipy')"},
    ]

    async def fake_status(socket_path, cmd, timeout=None):  # noqa: ARG001
        return {
            "state": "WAKE", "input_ended": False, "spend_allowed": True,
            "connection_paused": False, "mic_muted": False,
            "duck_active": False, "music_dbfs": -32.0,
            "wake_legs": ["on"], "tool_packs": packs,
        }
    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_status)

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["voice"]["reachable"] is True
    assert body["voice"]["tool_packs"] == packs


def test_state_voice_push_to_talk_only_flows_from_session_status(
    server_with_coordinator, monkeypatch,
):
    """Same curated-vs-passthrough regression as wake_legs/tool_packs, for
    push_to_talk_only: jasper-voice's session_status reports whether this
    box has no room mic of its own (every turn opened by an accessory
    button), and /state.voice must pull the field through — jasper-doctor's
    `Wake legs` check does not read it: the doctor re-derives the same fact
    from the published env + accessory file to report `n/a` instead of a
    permanent yellow on such a box. Pinned here at the aggregator seam
    specifically: _get_state hand-curates /state.voice field-by-field, so a
    key silently dropped from that dict literal is invisible to daemon-side
    coverage of session_status() (tests/test_voice_daemon_wake_triple_stream.py)
    and to source-level checks that the key is merely present somewhere in
    the module."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    async def fake_status(socket_path, cmd, timeout=None):  # noqa: ARG001
        return {
            "state": "WAKE", "input_ended": False, "spend_allowed": True,
            "connection_paused": False, "mic_muted": False,
            "duck_active": False, "music_dbfs": -32.0,
            "wake_legs": [], "push_to_talk_only": True,
        }
    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_status)

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["voice"]["reachable"] is True
    assert body["voice"]["push_to_talk_only"] is True


def test_state_audio_metrics_sanitize_non_finite_values(
    server_with_coordinator, monkeypatch, tmp_path,
):
    import jasper.camilla as camilla_mod

    class FakeCamilla:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def get_volume_db(self, *, best_effort=False):  # noqa: ARG002
            return -12.345

        async def get_playback_rms_all(self, *, best_effort=False):  # noqa: ARG002
            return [float("-inf"), -32.1234]

        async def get_playback_peak_all(self, *, best_effort=False):  # noqa: ARG002
            return [float("nan"), -3.456]

        async def get_clipped_samples(self, *, best_effort=False):  # noqa: ARG002
            return 7

    base, _ = server_with_coordinator
    state_path = tmp_path / "speaker_volume.json"
    state_path.write_text(
        '{"listening_level": 73, "main_volume_db": -13.5}',
    )
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(state_path))
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "missing.env"),
    )
    monkeypatch.setattr(camilla_mod, "CamillaController", FakeCamilla)

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["audio"]["main_volume_db"] == -12.35
    assert body["audio"]["playback_rms_dbfs"] == [None, -32.12]
    assert body["audio"]["playback_peak_dbfs"] == [None, -3.46]
    assert body["audio"]["clipped_samples"] == 7


def test_state_audio_metrics_publish_every_playback_channel(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """#2668: a 4-output active-crossover box must publish four levels.

    The fixture is the live reading taken off CamillaDSP's websocket during
    the §8.7 dummy-load sweep, mid-woofer-ramp: outputs 0/2 are the two
    woofers at the audible floor, 1/3 the two tweeters at digital silence.
    Publishing only the front pair hides an entire speaker.
    """
    import jasper.camilla as camilla_mod

    class FakeCamilla:
        def __init__(self, *args, **kwargs):
            pass

        async def get_volume_db(self, *, best_effort=False):
            return -25.757576

        async def get_playback_rms_all(self, *, best_effort=False):
            return [-108.03, -1000.0, -108.03, -1000.0]

        async def get_playback_peak_all(self, *, best_effort=False):
            return [-105.81, -1000.0, -105.81, -1000.0]

        async def get_clipped_samples(self, *, best_effort=False):
            return 0

    base, _ = server_with_coordinator
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "speaker_volume.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(camilla_mod, "CamillaController", FakeCamilla)

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["audio"]["playback_rms_dbfs"] == [-108.03, -1000.0, -108.03, -1000.0]
    assert body["audio"]["playback_peak_dbfs"] == [-105.81, -1000.0, -105.81, -1000.0]


def test_state_prefers_mux_winner_over_raw_renderer_probe(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """Mux owns the audible source; /state should not fall back to raw
    renderer priority when mux reports an auto winner."""
    import jasper.control.server as srv_mod

    base, _ = server_with_coordinator
    spotify_state = write_librespot_state(
        tmp_path / "spotify.env",
        playing=True, session_active=True, uri="spotify:track:test",
    )
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(spotify_state))
    monkeypatch.setenv(
        "JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"),
    )

    async def fake_mux_status(cmd: str, **kwargs):  # noqa: ARG001
        assert cmd == "STATUS"
        return {
            "mode": "auto",
            "selected_source": None,
            "winner": "airplay",
            "active_source": "airplay",
            "sources": {
                "airplay": {"playing": True},
                "spotify": {"playing": True},
                "bluetooth": {"playing": False},
                "usbsink": {"playing": False},
            },
        }

    monkeypatch.setattr(srv_mod, "_mux_socket_command", fake_mux_status)

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["active_source"] == "airplay"
    assert body["source_selection"]["winner"] == "airplay"


async def test_state_audio_volume_policy_surfaces_push_guard(
    monkeypatch, tmp_path,
):
    from jasper import volume_diagnostics
    from jasper.control import server as srv_mod

    spotify_state = write_librespot_state(
        tmp_path / "spotify.env",
        playing=True, session_active=True, uri="spotify:track:test",
    )
    volume_state = tmp_path / "speaker_volume.json"
    volume_state.write_text(json.dumps({
        "listening_level": 100,
        "main_volume_db": -12.5,
    }))
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(spotify_state))
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(volume_state))
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    volume_diagnostics.record_source_push(
        "spotify",
        level=100,
        ok=False,
        reason=volume_diagnostics.PUSH_WRITE_FAILED,
    )
    volume_diagnostics.record_push_guard(
        "spotify",
        level=100,
        guard_db=-12.5,
        previous_db=0.0,
        reason=volume_diagnostics.GUARD_PUSH_WRITE_FAILED,
        context="dispatch_spotify_degraded",
    )

    body = await srv_mod._get_state(
        camilla_host="127.0.0.1",
        camilla_port=1234,
        voice_socket_path="/nonexistent.sock",
    )

    policy = body["audio"]["volume_policy"]
    assert policy["active_source"] == "spotify"
    assert policy["source"] == "spotify"
    assert policy["volume_mode"] == "push"
    assert policy["carrier"] == "camilla_guard"
    assert policy["push_guard_active"] is True
    assert policy["guard_db"] == -12.5
    assert policy["guard_reason"] == "push_write_failed"
    assert policy["previous_db"] == 0.0
    assert policy["last_source_push_result"]["reason"] == "write_failed"


def test_state_active_source_resolves_to_usbsink_when_only_usb_playing(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """active_source ranks usbsink above idle but below the named
    renderers — when nothing else is playing and USB is, the field
    surfaces as 'usbsink' so the dashboard renders correctly."""
    import jasper.control.server as srv_mod

    base, _ = server_with_coordinator

    async def fake_status(path, **_kwargs):
        if "jasper-fanin" in path:
            return {
                "inputs": [{
                    "label": "usbsink",
                    "source": "direct",
                    "rms_dbfs": -10.0,
                    "muted": False,
                }],
            }
        return None

    monkeypatch.setattr(srv_mod, "_local_status_json", fake_status)
    monkeypatch.setenv(
        "JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"),
    )
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.env"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["active_source"] == "usbsink"


def test_state_combo_active_source_still_driven_by_mux_selection(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """Mux selection remains authoritative when fan-in STATUS is unavailable."""
    import jasper.control.server as srv_mod
    base, _ = server_with_coordinator

    async def fake_mux_status(*args, **kwargs):
        return {
            "mode": "manual",
            "selected_source": "usbsink",
            "winner": "usbsink",
            "active_source": "usbsink",
        }

    monkeypatch.setattr(srv_mod, "_mux_socket_command", fake_mux_status)
    monkeypatch.setenv(
        "JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"),
    )
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.env"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["active_source"] == "usbsink"


def test_state_502_when_aggregator_raises(
    server_with_coordinator, monkeypatch,
):
    """If _get_state itself blows up — not a fail-soft section, but
    something unexpected like a JSON serialization error — the route
    surfaces 502 instead of crashing the server."""
    import jasper.control.server as srv_mod

    async def boom(**kwargs):  # noqa: ARG001
        raise RuntimeError("aggregator broken")

    monkeypatch.setattr(srv_mod, "_get_state", boom)
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/state")
    assert status == 502
    assert "error" in body


def test_state_concurrent_requests_share_one_aggregate(monkeypatch):
    """Burst polls should collapse to one cross-daemon fan-out."""
    import jasper.control.server as srv_mod

    started = threading.Event()
    release = threading.Event()
    calls = 0

    async def fake_get_state(**kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2), "test did not release state aggregate"
        return {"ok": True, "calls": calls}

    monkeypatch.setattr(srv_mod, "_get_state", fake_get_state)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    results: list[tuple[int, dict]] = []
    try:
        t1 = threading.Thread(
            target=lambda: results.append(_get(f"{base}/state")),
            daemon=True,
        )
        t2 = threading.Thread(
            target=lambda: results.append(_get(f"{base}/state")),
            daemon=True,
        )
        t1.start()
        assert started.wait(timeout=1)
        t2.start()
        time.sleep(0.05)
        assert calls == 1
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)

    assert len(results) == 2
    assert all(item[0] == 200 for item in results)
    assert all(item[1]["ok"] is True and item[1]["calls"] == 1 for item in results)
    assert calls == 1


def test_state_camilla_probe_times_out_fail_soft(
    server_with_coordinator, monkeypatch, tmp_path, caplog,
):
    """A wedged-but-listening CamillaDSP (TCP accepted, websocket read
    stalled) must not hang /state: the camilla probe self-bounds and its
    section reports null, while the rest of the aggregate still resolves
    — the same fail-soft contract its sibling probes already honor."""
    import jasper.camilla as camilla_mod
    import jasper.control.state_aggregate as sa

    class HangingCamilla:
        def __init__(self, *a, **k):
            pass

        async def _hang(self, *, best_effort=False):
            await asyncio.sleep(5)

        get_volume_db = _hang
        get_playback_rms_all = _hang
        get_playback_peak_all = _hang
        get_clipped_samples = _hang
        get_config_file_path = _hang

    monkeypatch.setattr(camilla_mod, "CamillaController", HangingCamilla)
    monkeypatch.setattr(sa, "_CAMILLA_PROBE_TIMEOUT_SEC", 0.05)
    monkeypatch.delenv("JASPER_HA_URL", raising=False)
    monkeypatch.delenv("JASPER_HA_TOKEN", raising=False)
    base, _ = server_with_coordinator
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.env"))

    with caplog.at_level("DEBUG", logger="jasper.control.state_aggregate"):
        status, body = _get(f"{base}/state")

    assert status == 200
    audio = body["audio"]
    assert audio["main_volume_db"] is None
    assert audio["playback_rms_dbfs"] is None
    assert audio["clipped_samples"] is None
    # Fail-soft: the camilla stall didn't take down the whole snapshot.
    assert "source_selection" in body
    assert "event=state.camilla_probe_failed" in caplog.text


async def test_state_aggregate_budget_fails_loud_on_runaway_probe(
    monkeypatch, caplog,
):
    """If a probe blows past its own ceiling, the aggregate liveness
    budget converts the hang into a logged failure (the handler turns it
    into a 502) rather than parking a bounded worker forever — so an
    overload can't manufacture a T5.2 reboot via a wedged /state."""
    import jasper.camilla as camilla_mod
    import jasper.control.state_aggregate as sa

    class HangingCamilla:
        def __init__(self, *a, **k):
            pass

        async def _hang(self, *, best_effort=False):
            await asyncio.sleep(5)

        get_volume_db = _hang
        get_playback_rms_all = _hang
        get_playback_peak_all = _hang
        get_clipped_samples = _hang
        get_config_file_path = _hang

    monkeypatch.setattr(camilla_mod, "CamillaController", HangingCamilla)
    # Camilla's own ceiling is high, so the OUTER aggregate budget is what
    # fires — that's the path under test.
    monkeypatch.setattr(sa, "_CAMILLA_PROBE_TIMEOUT_SEC", 30.0)
    monkeypatch.setattr(sa, "_STATE_AGGREGATE_BUDGET_SEC", 0.1)

    with caplog.at_level("WARNING", logger="jasper.control.state_aggregate"):
        with pytest.raises(asyncio.TimeoutError):
            await sa._get_state(
                camilla_host="127.0.0.1",
                camilla_port=1234,
                voice_socket_path="/nonexistent.sock",
            )

    assert any(
        "event=state.aggregate_timeout" in r.getMessage()
        for r in caplog.records
    ), "aggregate timeout must emit a greppable event= line"


@pytest.mark.parametrize("playing", [True, False, None])
async def test_state_airplay_row_and_active_source_come_from_the_injected_reader(
    playing, monkeypatch, tmp_path,
):
    """`/state` derives `active_source` from the AirPlay health sampler's
    held PlaybackStatus — no second reader."""
    async def no_status(*_args, **_kwargs):
        return None

    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.env"))

    body = await state_aggregate._get_state(
        camilla_host="127.0.0.1",
        camilla_port=1234,
        voice_socket_path=str(tmp_path / "voice.sock"),
        voice_socket_command=no_status,
        mux_socket_command=no_status,
        local_status_json=no_status,
        airplay_playing_snapshot=lambda: playing,
    )

    assert body["active_source"] == ("airplay" if playing else "idle")


def test_state_home_assistant_unconfigured(server_with_coordinator, monkeypatch):
    """With JASPER_HA_URL/TOKEN unset, /system/snapshot.home_assistant
    returns configured=false with no error — fail-soft for the dashboard."""
    base, _ = server_with_coordinator
    monkeypatch.delenv("JASPER_HA_URL", raising=False)
    monkeypatch.delenv("JASPER_HA_TOKEN", raising=False)

    status, body = _get(f"{base}/system/snapshot")
    assert status == 200
    ha = body["home_assistant"]
    assert ha["configured"] is False
    assert ha["connected"] is False
    assert ha["error"] is None


def test_state_home_assistant_connected(server_with_coordinator, monkeypatch):
    """Configured + reachable: /system/snapshot.home_assistant carries
    instance_name + version from the injected child-cache status provider."""
    import jasper.home_assistant as ha_mod
    base, _ = server_with_coordinator

    async def should_not_run():
        raise AssertionError("the snapshot must not import/probe HA in-process")

    monkeypatch.setattr(ha_mod, "probe_status_from_env", should_not_run)
    monkeypatch.setenv(
        "JASPER_TEST_HA_STATUS_JSON",
        json.dumps({
            "configured": True,
            "connected": True,
            "url": "http://homeassistant.local:8123",
            "instance_name": "Brooklyn House",
            "version": "2026.5.1",
            "error": None,
        }),
    )

    status, body = _get(f"{base}/system/snapshot")
    assert status == 200
    ha = body["home_assistant"]
    assert ha["configured"] is True
    assert ha["connected"] is True
    assert ha["instance_name"] == "Brooklyn House"
    assert ha["version"] == "2026.5.1"


def test_state_home_assistant_unreachable_fails_soft(server_with_coordinator, monkeypatch):
    """Configured but probe fails: response still 200 with the rest of
    the snapshot intact; home_assistant carries the error string."""
    base, _ = server_with_coordinator

    monkeypatch.setenv(
        "JASPER_TEST_HA_STATUS_JSON",
        json.dumps({
            "configured": True,
            "connected": False,
            "url": "http://homeassistant.local:8123",
            "instance_name": None,
            "version": None,
            "error": "Couldn't reach Home Assistant - check the URL and token.",
        }),
    )

    status, body = _get(f"{base}/system/snapshot")
    assert status == 200
    ha = body["home_assistant"]
    assert ha["configured"] is True
    assert ha["connected"] is False
    assert ha["error"]
    # Other snapshot sections still populated despite HA failure
    assert "build" in body
    assert "audio_health" in body


def test_system_restart_voice_409s_while_parked(monkeypatch, server_with_coordinator):
    """The dashboard's restart-voice button must not boot the parked
    daemon on a bonded follower — refuse with the pair story."""
    import jasper.control.handlers.peering as srv_mod

    monkeypatch.setattr(srv_mod, "_pair_follower_leader_addr", lambda: "jts.local")
    base, _fake = server_with_coordinator
    status, body = _post(f"{base}/system/restart/voice", {})
    assert status == 409
    assert "parked" in body["error"]


def test_system_restart_audio_uses_local_source_registry(
    monkeypatch, server_with_coordinator,
):
    """restart-audio restarts core audio but only try-restarts local sources."""
    from jasper.local_sources import local_source_audio_refresh_units

    calls = _record_broker(monkeypatch)
    base, _fake = server_with_coordinator
    status, body = _post(f"{base}/system/restart/audio", {})
    assert status == 202
    assert body["ok"] is True
    assert body["status"] == "accepted"
    # One try-restart call per unit: a batch reports one verdict for the whole
    # set, so a box missing one renderer would fail the rest with it.
    assert calls == [("restart", ["jasper-camilla.service"])] + [
        ("try-restart", [unit]) for unit in local_source_audio_refresh_units()
    ]
    assert body["accepted_units"] == [
        "jasper-camilla.service", *local_source_audio_refresh_units(),
    ]
    assert body["skipped_units"] == []


def test_restart_audio_carries_the_topology_gate_refusal_into_its_202(
    monkeypatch, server_with_coordinator, tmp_path,
):
    """A 202 is "systemd took the job", not "the speaker plays".

    jasper-camilla's ExecCondition refuses a graph proved against a different
    topology (ADR-0283), and a refusal survives a restart — so pressing the
    button again cannot clear it and a bare 202 sends the household chasing
    audio that will not come back. The record rides along.
    """
    record = tmp_path / "gate.state"
    record.write_text(
        "refused_utc=2026-09-10T00:00:00Z\nreason=topology_mismatch\n"
        "action=run the hardware reconciler\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CAMILLA_TOPOLOGY_GATE_STATE", str(record))
    _record_broker(monkeypatch)
    base, _fake = server_with_coordinator

    status, body = _post(f"{base}/system/restart/audio", {})

    assert status == 202
    assert body["topology_gate"]["reason"] == "topology_mismatch"
    assert body["topology_gate"]["action"] == "run the hardware reconciler"

    # Cleared, the field is absent rather than a null the dashboard must read.
    record.unlink()
    _status, healthy = _post(f"{base}/system/restart/audio", {})
    assert "topology_gate" not in healthy


def test_system_restart_audio_502s_when_the_broker_refuses(
    monkeypatch, server_with_coordinator,
):
    """A refused restart answers 502 — the dashboard must not show "Sent" for
    a restart systemd never accepted."""
    _record_broker(monkeypatch, ok=False)
    base, _fake = server_with_coordinator
    status, body = _post(f"{base}/system/restart/audio", {})
    assert status == 502
    assert body["code"] == "system_restart_failed"
    assert body["action"] == "restart-audio"
    assert body["failed_verb"] == "restart"
    assert body["failed_units"] == ["jasper-camilla.service"]
    assert body.get("ok") is not True


@pytest.mark.parametrize(
    ("rc", "expected_status", "group"),
    [(5, 202, "skipped_units"), (1, 502, "failed_units")],
)
def test_system_restart_audio_reports_each_renderer_on_its_own(
    monkeypatch, server_with_coordinator, rc, expected_status, group,
):
    """A renderer this box never installed (rc 5) is skipped, not a failure;
    a renderer that really refused fails alone. Either way the core restart
    that already went through stays in `accepted_units` — a partial success
    reported as a total failure sends the household chasing working audio."""
    from jasper.local_sources import local_source_audio_refresh_units

    odd_one = local_source_audio_refresh_units()[0]
    _record_broker(monkeypatch, unit_rc={odd_one: rc})
    base, _fake = server_with_coordinator

    status, body = _post(f"{base}/system/restart/audio", {})

    assert status == expected_status
    assert body[group] == [odd_one]
    assert "jasper-camilla.service" in body["accepted_units"]
    assert odd_one not in body["accepted_units"]


def test_system_restart_audio_keeps_parked_renderers_parked(
    monkeypatch, server_with_coordinator,
):
    """restart-audio on a follower touches only the units the profile
    keeps alive (camilla) — never parked source resources."""
    import jasper.control.handlers.peering as srv_mod

    monkeypatch.setattr(srv_mod, "_pair_follower_leader_addr", lambda: "jts.local")
    calls = _record_broker(monkeypatch)
    base, _fake = server_with_coordinator
    status, _body = _post(f"{base}/system/restart/audio", {})
    assert status == 202
    flat = [unit for _verb, units in calls for unit in units]
    assert "jasper-camilla.service" in flat
    assert "librespot.service" not in flat
    assert "shairport-sync.service" not in flat
    assert "jasper-usbsink.service" not in flat
    # The hardware-gated composite gadget may carry the USB management network;
    # it is infrastructure, not an audio-refresh renderer. A dashboard audio
    # restart must not recompose it and blip an available management link.
    assert "jasper-usbgadget.service" not in flat
