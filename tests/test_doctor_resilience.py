# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor resilience domain.

These checks surface state whose runtime readers are deliberately fail-open:
the daemons treat missing/corrupt as "default behaviour", which is right at
runtime but leaves a corrupt file or a parked unit invisible without a doctor
line. The tests drive the path-parameterized classifiers with tmp files.
"""
from __future__ import annotations

import json
import time

import pytest

from jasper import service_units
from jasper.cli.doctor import _evidence, _shared, resilience, web
from jasper.voice.provider_state import ActiveProviderState
from jasper.cli.doctor.resilience import (
    _REBOOT_STATE_FUTURE_SKEW_SEC,
    _classify_reboot_state,
    _classify_supervisor_snapshots,
    check_bootloop_guard,
    check_supervisor_runtime_snapshots,
    check_supply_voltage,
)

from .doctor_test_support import (
    _make_unit_states_fake,
    _registered_check_names,
)

# ------------------------------------------------- check_service_runtime_state


def _systemctl_show(monkeypatch, stdout: str):
    """Seed the evidence layer's unit-state batch from raw ``systemctl show``
    block text, reusing the real parser for fidelity."""
    parsed = service_units.parse_systemctl_show_units(stdout)
    monkeypatch.setattr(
        _evidence, "read_unit_states", lambda units, *, timeout: parsed,
    )


def _unit_block(unit: str, active: str, sub: str, restarts: int = 0) -> str:
    return (
        f"Id={unit}\n"
        "LoadState=loaded\n"
        f"ActiveState={active}\n"
        f"SubState={sub}\n"
        "Result=success\n"
        f"NRestarts={restarts}\n"
    )


@pytest.mark.parametrize(
    "blocks, status, reason",
    [
        (
            [("librespot.service", "failed", "failed", 5)],
            "fail",
            resilience.REASON_UNITS_FAILED_OR_UNSTABLE,
        ),
        # NRestarts is cumulative until reset-failed or a reboot, so a unit
        # that is up now must not latch a warn for the rest of the boot.
        (
            [("jasper-voice.service", "active", "running", 2)],
            "ok",
            resilience.REASON_UNITS_RESTARTED,
        ),
        # A parked coupling oneshot leaves its evidence only in
        # `systemctl --failed` plus the journal (#1233 follow-up).
        (
            [("jasper-fanin-coupling-auto.service", "failed", "failed", 0)],
            "fail",
            resilience.REASON_UNITS_FAILED_OR_UNSTABLE,
        ),
    ],
    ids=["failed-unit", "restart-count", "failed-oneshot"],
)
def test_check_service_runtime_state_verdicts(
    monkeypatch, blocks, status, reason
):
    _systemctl_show(monkeypatch, "\n".join(_unit_block(*b) for b in blocks))

    r = resilience.check_service_runtime_state()

    assert r.status == status
    assert r.reason == reason


def test_check_service_runtime_state_ignores_an_in_flight_oneshot(monkeypatch):
    """`activating` is a oneshot's NORMAL mid-run state (a reconcile pass in
    flight), not the stuck-start instability it signals on a long-running
    daemon — a tick the doctor races must not read as a failure."""
    _systemctl_show(
        monkeypatch,
        _unit_block("jasper-fanin-coupling-auto.service", "activating", "start"),
    )

    r = resilience.check_service_runtime_state()

    assert r.status == "ok"


def test_check_service_runtime_state_flags_a_non_oneshot_stuck_activating(
    monkeypatch,
):
    """The same `activating` state on a long-running daemon still is a
    finding — only the tracked oneshot is exempt."""
    _systemctl_show(
        monkeypatch, _unit_block("jasper-voice.service", "activating", "start"),
    )

    r = resilience.check_service_runtime_state()

    assert r.status == "fail"
    assert r.reason == resilience.REASON_UNITS_FAILED_OR_UNSTABLE


def test_runtime_state_units_track_the_coupling_reconciler_oneshot():
    assert "jasper-fanin-coupling-auto.service" in _shared._RUNTIME_STATE_UNITS


def test_a_failed_camilla_is_exactly_one_fail_row(monkeypatch):
    """One fact, one row: this check no longer tracks the units
    `_shared._service_state_failure` already owns, so the failed camilla is
    audio_runtime_camilla.check_camilla_service's row alone."""
    monkeypatch.setattr(
        _evidence, "read_unit_states",
        _make_unit_states_fake({"jasper-camilla.service": {
            "active_state": "failed", "sub_state": "failed", "result": "exit-code",
        }}),
    )

    assert resilience.check_service_runtime_state().status == "ok"


# ------------------------------------------------ check_required_units_active


def test_every_required_unit_has_an_owner_for_its_failed_state():
    """The row judges only `inactive` and defers every other state: the
    services to check_service_runtime_state, the wizard sockets to
    web.check_wizard_socket_start_limits. A required unit neither of those
    reads falls through every row when it fails."""
    owned = set(_shared._RUNTIME_STATE_UNITS) | {
        f"{unit}.socket" for unit in web.WIZARD_UNITS
    }
    assert set(resilience._REQUIRED_ACTIVE_UNITS) <= owned


@pytest.mark.parametrize(
    "overrides, status, reason",
    [
        ({}, "ok", ""),
        # The gap this row closes: `inactive` is neither failed nor unstable,
        # so check_service_runtime_state saw nothing while the HID accessory
        # bridge was simply gone.
        (
            {"jasper-input.service": {"active_state": "inactive"}},
            "fail", resilience.REASON_REQUIRED_UNIT_INACTIVE,
        ),
        # An install that did not finish reads inactive/not-found, never failed.
        (
            {"jasper-accessory-reconcile.path": {
                "active_state": "inactive", "load_state": "not-found",
            }},
            "fail", resilience.REASON_REQUIRED_UNIT_INACTIVE,
        ),
        # Every other state is someone else's: `failed` belongs to
        # check_service_runtime_state (one down unit is one finding, not two),
        # and a healthy unit mid-reload is no finding at all.
        (
            {"jasper-input.service": {"active_state": "failed"}}, "ok", "",
        ),
        (
            {"jasper-input.service": {"active_state": "reloading"}}, "ok", "",
        ),
        # check_wizard_socket_start_limits reads an inactive wizard socket as
        # "not installed on this profile", so a stopped listener on a profile
        # that DOES install it is only ever this row's finding.
        (
            {"jasper-web.socket": {"active_state": "inactive"}},
            "fail", resilience.REASON_REQUIRED_UNIT_INACTIVE,
        ),
    ],
    ids=[
        "all-active", "inactive", "not-found", "failed", "reloading",
        "wizard-socket-inactive",
    ],
)
def test_check_required_units_active_verdicts(
    monkeypatch, overrides, status, reason,
):
    monkeypatch.setattr(
        _evidence, "read_unit_states", _make_unit_states_fake(overrides),
    )

    result = resilience.check_required_units_active()

    assert (result.status, result.reason) == (status, reason)


def test_check_required_units_active_skips_without_systemctl(monkeypatch):
    monkeypatch.setattr(
        _evidence, "read_unit_states", _make_unit_states_fake(unavailable=True),
    )

    result = resilience.check_required_units_active()

    assert (result.status, result.reason) == (
        "skipped", _shared.REASON_SYSTEMCTL_UNAVAILABLE,
    )


# --------------------------------------------------- check_voice_unit_running


def _stub_provider_state(monkeypatch, status: str) -> None:
    """Stub the SSOT provider reader at the doctor's own call site."""
    state = ActiveProviderState(
        "gemini" if status == "configured" else "", None, status,
        "/var/lib/jasper/voice_provider.env",
    )
    monkeypatch.setattr(
        resilience, "read_active_provider_state", lambda: state,
    )


@pytest.mark.parametrize(
    "profile, unit, marker, remote, status, reason",
    [
        # The gap: `inactive` is neither failed nor unstable, so
        # check_service_runtime_state sees nothing while no wake gets an
        # answer.
        (
            "full", {"active_state": "inactive", "sub_state": "dead"}, False,
            False, "fail", resilience.REASON_VOICE_UNIT_INACTIVE,
        ),
        # ConditionPathExists=!/var/lib/jasper/voice-input-absent parks the
        # unit on a box with neither a local nor an accessory mic: hardware,
        # not a fault.
        (
            "full", {"active_state": "inactive", "sub_state": "dead"}, True,
            False, "skipped", resilience.REASON_VOICE_UNIT_PARKED_NO_INPUT,
        ),
        (
            "full", {"active_state": "active", "sub_state": "running"}, False,
            False, "ok", "",
        ),
        # A streambox runs the assistant only while a mic-bearing remote is
        # paired (ADR-0217): with none paired, inactive is the correct state.
        (
            "streambox", {"active_state": "inactive", "sub_state": "dead"},
            False, False, "skipped",
            resilience.REASON_VOICE_UNIT_NOT_FULL_PROFILE,
        ),
        # With one paired, the remote's talk button gets no answer — a warn,
        # because the reconciler that owns the lifecycle may still be mid-pass.
        (
            "streambox", {"active_state": "inactive", "sub_state": "dead"},
            False, True, "warn",
            resilience.REASON_VOICE_UNIT_INACTIVE_PAIRED_REMOTE,
        ),
        (
            "streambox", {"active_state": "active", "sub_state": "running"},
            False, True, "ok", "",
        ),
        # A unit systemd cannot load is not an inactive one.
        (
            "full",
            {"active_state": "inactive", "load_state": "not-found"},
            False, False, "skipped", resilience.REASON_VOICE_UNIT_UNOBSERVED,
        ),
    ],
    ids=[
        "full-inactive", "parked-no-mic", "active", "streambox-no-remote",
        "streambox-remote-paired", "streambox-remote-answered", "not-found",
    ],
)
def test_check_voice_unit_running_verdicts(
    monkeypatch, tmp_path, profile, unit, marker, remote, status, reason,
):
    monkeypatch.setattr(_shared, "read_install_profile", lambda: profile)
    absent = tmp_path / "voice-input-absent"
    if marker:
        absent.write_text("")
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(absent))
    # The accessory owner's published file is the one "a mic-bearing remote is
    # paired" fact; write a real one so the real reader answers.
    mic_env = tmp_path / "accessory-mics.env"
    if remote:
        mic_env.write_text("JASPER_MANUAL_MIC_SOURCES=wiim_remote_2=hw:WiiM\n")
    monkeypatch.setenv("JASPER_ACCESSORY_MIC_ENV_FILE", str(mic_env))
    _stub_provider_state(monkeypatch, "configured")
    monkeypatch.setattr(
        _evidence, "read_unit_states",
        _make_unit_states_fake({"jasper-voice.service": unit}),
    )

    result = resilience.check_voice_unit_running()

    assert (result.status, result.reason) == (status, reason)


def test_an_inactive_voice_unit_does_not_claim_playback_silence(
    monkeypatch, tmp_path,
):
    """`speaker_silent` means the speaker emits NOTHING. Music keeps playing
    with the voice daemon down — what is silent is the assistant."""
    monkeypatch.setattr(_shared, "read_install_profile", lambda: "full")
    monkeypatch.setenv(
        "JASPER_VOICE_INPUT_ABSENT_MARKER", str(tmp_path / "absent"),
    )
    _stub_provider_state(monkeypatch, "configured")
    monkeypatch.setattr(
        _evidence, "read_unit_states",
        _make_unit_states_fake(
            {"jasper-voice.service": {"active_state": "inactive"}},
        ),
    )

    result = resilience.check_voice_unit_running()

    assert result.status == "fail"
    assert result.speaker_silent is False


@pytest.mark.parametrize(
    "profile, provider_status, status, reason",
    [
        ("full", "unset", "skipped", resilience.REASON_VOICE_UNIT_NO_PROVIDER),
        # The same box on the other tier: nothing about an unchosen provider
        # is the accessory reconciler's fault, so the paired-remote warn —
        # which points the operator at that reconciler — must not win here.
        (
            "streambox", "missing", "skipped",
            resilience.REASON_VOICE_UNIT_NO_PROVIDER,
        ),
        ("full", "configured", "fail", resilience.REASON_VOICE_UNIT_INACTIVE),
        # A bad READ is not a box that has yet to choose: demoting it would
        # hide a real 66-park behind an unprivileged doctor run.
        ("full", "unreadable", "fail", resilience.REASON_VOICE_UNIT_INACTIVE),
    ],
    ids=["full-unset", "streambox-missing", "configured", "unreadable"],
)
def test_voice_unit_parked_for_want_of_a_provider_is_not_a_failure(
    monkeypatch, tmp_path, profile, provider_status, status, reason,
):
    """A box with a mic and no provider parks jasper-voice on EX_CONFIG by
    design (RestartPreventExitStatus), so the state is configuration, not
    breakage — the last row ADR-0173's removal condition named for --core."""
    monkeypatch.setattr(_shared, "read_install_profile", lambda: profile)
    monkeypatch.setenv(
        "JASPER_VOICE_INPUT_ABSENT_MARKER", str(tmp_path / "absent"),
    )
    mic_env = tmp_path / "accessory-mics.env"
    mic_env.write_text("JASPER_MANUAL_MIC_SOURCES=wiim_remote_2=hw:WiiM\n")
    monkeypatch.setenv("JASPER_ACCESSORY_MIC_ENV_FILE", str(mic_env))
    _stub_provider_state(monkeypatch, provider_status)
    monkeypatch.setattr(
        _evidence, "read_unit_states",
        _make_unit_states_fake(
            {"jasper-voice.service": {"active_state": "inactive"}},
        ),
    )

    result = resilience.check_voice_unit_running()

    assert (result.status, result.reason) == (status, reason)


def test_check_voice_unit_running_skips_without_systemctl(monkeypatch, tmp_path):
    monkeypatch.setattr(_shared, "read_install_profile", lambda: "full")
    monkeypatch.setenv(
        "JASPER_VOICE_INPUT_ABSENT_MARKER", str(tmp_path / "absent"),
    )
    monkeypatch.setattr(
        _evidence, "read_unit_states", _make_unit_states_fake(unavailable=True),
    )

    result = resilience.check_voice_unit_running()

    assert result.status == "skipped"
    assert result.reason == resilience.REASON_VOICE_UNIT_UNOBSERVED


# ------------------------------------------------------- supervisor reboot state


@pytest.mark.parametrize(
    "payload, offset, status, reason",
    [
        (None, None, "ok", resilience.REASON_REBOOT_STATE_ABSENT),
        # A corrupt file must name itself so the operator knows what to delete.
        ("{ not json", None, "warn", resilience.REASON_REBOOT_STATE_CORRUPT),
        (
            json.dumps({"last_reboot_at": "nope"}), None, "warn",
            resilience.REASON_REBOOT_STATE_CORRUPT,
        ),
        (None, -7200, "ok", resilience.REASON_REBOOT_STATE_ARMED),
        # fake-hwclock + NTP routinely produce small negative ages at boot.
        (None, 60, "ok", resilience.REASON_REBOOT_STATE_ARMED),
        (
            None, _REBOOT_STATE_FUTURE_SKEW_SEC * 2, "warn",
            resilience.REASON_REBOOT_STATE_FUTURE_DATED,
        ),
    ],
    ids=["absent", "corrupt", "wrong-shape", "recent", "small-skew", "large-skew"],
)
def test_classify_reboot_state_verdicts(
    tmp_path, payload, offset, status, reason
):
    p = tmp_path / "reboot.json"
    now = time.time()
    if offset is not None:
        p.write_text(json.dumps({"last_reboot_at": now + offset}), encoding="utf-8")
    elif payload is not None:
        p.write_text(payload, encoding="utf-8")

    res = _classify_reboot_state(p, now=now)

    assert res.status == status
    assert res.reason == reason


# ---------------------------------------------------------- boot-loop guard


def _bootloop_marker(monkeypatch, tmp_path, payload) -> None:
    p = tmp_path / "bootloop-state.json"
    monkeypatch.setenv("JASPER_BOOTLOOP_MARKER_FILE", str(p))
    if payload is not None:
        p.write_text(payload, encoding="utf-8")


_ARMED = {
    "tripped": False,
    "boots_in_window": 1,
    "threshold": 3,
    "window_sec": 3600,
    "checked_at": 1000,
    "reason": "systemd",
    "units": ["jasper-camilla.service"],
}


@pytest.mark.parametrize(
    "payload, reason",
    [
        # guard never ran this boot (dev host, fresh install)
        (None, resilience.REASON_BOOTLOOP_GUARD_NOT_RUN),
        (json.dumps(_ARMED), resilience.REASON_BOOTLOOP_GUARD_ARMED),
        # The reader is fail-soft ({'ran': False}) and the guard is fail-open,
        # so a torn marker reads as "never ran" — armed, not broken.
        ("{torn", resilience.REASON_BOOTLOOP_GUARD_NOT_RUN),
    ],
    ids=["absent", "untripped", "corrupt"],
)
def test_bootloop_guard_reports_armed(monkeypatch, tmp_path, payload, reason):
    _bootloop_marker(monkeypatch, tmp_path, payload)

    res = check_bootloop_guard()

    assert res.status == "ok"
    assert res.reason == reason


def test_bootloop_guard_warns_on_a_reload_failure(monkeypatch, tmp_path):
    _bootloop_marker(
        monkeypatch,
        tmp_path,
        json.dumps({**_ARMED, "reload_ok": False, "boots_in_window": 3}),
    )

    res = check_bootloop_guard()

    assert res.status == "warn"
    assert res.reason == resilience.REASON_BOOTLOOP_GUARD_RELOAD_FAILED


def test_bootloop_guard_tripped_names_the_units_and_the_recovery(
    monkeypatch, tmp_path
):
    """StartLimitAction=none parks the sick unit failed; reset-failed + start
    is what actually recovers it."""
    _bootloop_marker(
        monkeypatch,
        tmp_path,
        json.dumps(
            {
                **_ARMED,
                "tripped": True,
                "boots_in_window": 3,
                "units": ["jasper-camilla.service", "jasper-voice.service"],
            }
        ),
    )

    res = check_bootloop_guard()

    assert res.status == "warn"
    assert res.reason == resilience.REASON_BOOTLOOP_GUARD_TRIPPED


# ------------------------------------------------- supervisor runtime snapshots


def test_supervisor_snapshots_quiet_is_ok():
    res = _classify_supervisor_snapshots(
        {
            "shairport": {"enabled": True, "consecutive_failures": 0},
            "grouping_supervisor": {
                "enabled": True,
                "last_poll_starved": False,
                "consecutive_starved": 0,
                "kick_count": 0,
                "rate_limited_count": 0,
                "binding": {"failed_total": 0},
                "reassert": {"failed_total": 0, "last_ok": True},
            },
            "system_supervisor": {"enabled": True, "consecutive_failures": 0},
        }
    )

    assert res.status == "ok"


@pytest.mark.parametrize(
    "grouping_supervisor",
    [
        {"enabled": True, "last_poll_starved": True, "consecutive_starved": 4},
        {"enabled": True, "kick_count": 2},
        {"enabled": True, "binding": {"failed_total": 1}},
        {
            "enabled": True,
            "reassert": {
                "failed_total": 1,
                "last_ok": False,
                "last_detail": "connection refused",
            },
        },
    ],
    ids=["starved", "kicks", "binding-failed", "reassert-failed"],
)
def test_supervisor_snapshots_warn_on_every_non_converging_signal(
    grouping_supervisor,
):
    res = _classify_supervisor_snapshots(
        {"grouping_supervisor": grouping_supervisor},
    )

    assert res.status == "warn"
    assert res.reason == resilience.REASON_SUPERVISOR_ISSUES


def test_supervisor_snapshots_check_skips_when_state_unavailable(monkeypatch):
    monkeypatch.setattr(resilience, "_read_resilience_state", lambda: None)

    res = check_supervisor_runtime_snapshots()

    assert res.status == "skipped"
    assert res.reason == resilience.REASON_CONTROL_UNAVAILABLE


# ------------------------------------------------------- check_supply_voltage


@pytest.mark.parametrize(
    "current, status, reason",
    [
        # No jasper-control /system/snapshot reachable: n/a, not a failure.
        (None, "skipped", resilience.REASON_SNAPSHOT_UNAVAILABLE),
        # Bits absent/wrong-typed from a stale or malformed snapshot: n/a.
        (
            {"throttled_now": None, "throttled_history": None}, "skipped",
            resilience.REASON_THROTTLED_BITS_UNREPORTED,
        ),
        # Clean box: neither bit set. Both fields are already the shifted
        # nibbles jasper.control.system_metrics._read_throttled() publishes
        # (raw & 0xF, (raw >> 16) & 0xF) -- never a raw 0x50005-style value.
        ({"throttled_now": 0x0, "throttled_history": 0x0}, "ok", ""),
        # Bit 0 of throttled_now: under-voltage right now outranks history.
        (
            {"throttled_now": 0x5, "throttled_history": 0x5},
            "fail",
            resilience.REASON_UNDERVOLTAGE_NOW,
        ),
        # Bit 0 of throttled_history only (raw bit 16): happened since boot,
        # not now. The firmware latches it until the next reboot and there is
        # nothing left to act on, so it reports rather than warns.
        (
            {"throttled_now": 0x0, "throttled_history": 0x1},
            "ok",
            resilience.REASON_UNDERVOLTAGE_HISTORY,
        ),
        # Other throttled bits set (frequency cap, temp limit) but neither
        # under-voltage bit: not this check's concern.
        ({"throttled_now": 0x2, "throttled_history": 0x2}, "ok", ""),
    ],
)
def test_check_supply_voltage_verdicts(monkeypatch, current, status, reason):
    monkeypatch.setattr(resilience, "_read_system_metrics_current", lambda: current)

    result = check_supply_voltage()

    assert result.status == status
    if reason:
        assert result.reason == reason


@pytest.mark.parametrize(
    "check_name",
    [
        "check_bootloop_guard",
        "check_required_units_active",
        "check_supervisor_runtime_snapshots",
        "check_supply_voltage",
        "check_voice_unit_running",
    ],
)
def test_resilience_checks_are_registered(check_name):
    assert check_name in _registered_check_names()


# --------------------------------------- check_outputd_failure_reconcile_park


def _park_check(monkeypatch, tmp_path, *, record: str | None, unit: dict):
    target = tmp_path / "failure-reconcile.park"
    if record is not None:
        target.write_text(record)
    monkeypatch.setenv("JASPER_OUTPUTD_RECONCILE_PARK_STATE", str(target))
    monkeypatch.setattr(
        _evidence, "read_unit_states",
        _make_unit_states_fake({"jasper-outputd.service": unit}),
    )
    return resilience.check_outputd_failure_reconcile_park()


_PARK = "parked_at=1000\nexit_status=78\nreason=recent\n"
_RUNNING = {"active_state": "active", "result": "success"}
_FAILED = {"active_state": "failed", "result": "exit-code"}
_ACTIVATING = {"active_state": "activating", "sub_state": "start", "result": "success"}


@pytest.mark.parametrize(
    "record, unit, status, reason, silent",
    [
        (None, _RUNNING, "ok", "", False),
        (None, _FAILED, "fail", resilience.REASON_OUTPUTD_UNIT_FAILED, True),
        (None, _ACTIVATING, "warn", resilience.REASON_OUTPUTD_UNIT_UNSTABLE, False),
        (_PARK, _FAILED, "fail", resilience.REASON_OUTPUTD_PARKED, True),
        (_PARK, _RUNNING, "warn",
         resilience.REASON_OUTPUTD_PARK_RECORD_STALE, False),
    ],
    ids=["healthy", "failed-no-record", "unstable-no-record", "parked", "stale-record"],
)
def test_outputd_failure_reconcile_park_verdicts(
    tmp_path, monkeypatch, record, unit, status, reason, silent,
):
    """outputd owns the DAC write loop, so both fail branches prove silence."""
    result = _park_check(monkeypatch, tmp_path, record=record, unit=unit)
    assert (result.status, result.reason) == (status, reason)
    assert result.speaker_silent is silent


def test_outputd_failure_reconcile_park_skips_without_systemctl(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "JASPER_OUTPUTD_RECONCILE_PARK_STATE",
        str(tmp_path / "failure-reconcile.park"),
    )
    monkeypatch.setattr(
        _evidence, "read_unit_states", _make_unit_states_fake(unavailable=True),
    )
    result = resilience.check_outputd_failure_reconcile_park()
    assert result.status == "skipped"
    assert result.reason == resilience.REASON_OUTPUTD_RECONCILE_UNOBSERVED


def test_a_failed_outputd_is_exactly_one_fail_row(tmp_path, monkeypatch):
    """One fact, one row: check_service_runtime_state no longer tracks outputd,
    so the park check is the only check that fails on it."""
    park = _park_check(monkeypatch, tmp_path, record=_PARK, unit=_FAILED)
    generic = resilience.check_service_runtime_state()
    assert park.status == "fail"
    assert generic.status == "ok"
    assert "jasper-outputd.service" not in _shared._RUNTIME_STATE_UNITS


@pytest.mark.parametrize(
    "parked_at, shown",
    [(1000, "clock unset"), (None, "unrecorded"), (1_800_000_000, "s ago")],
    ids=["pre-2020-clock", "lost-field", "real-age"],
)
def test_a_pre_2020_park_stamp_is_named_not_counted(parked_at, shown):
    """A Pi with no RTC stamps 1970 until NTP lands; "2000000000s ago" is
    worse than saying the clock was unset."""
    assert shown in resilience._parked_ago(parked_at, now=1_800_000_100.0)


def test_outputd_failure_reconcile_park_is_registered():
    assert "check_outputd_failure_reconcile_park" in _registered_check_names()
