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
from jasper.control import heal_supervisor
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
    _bootloop_marker,
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
        # #2802 item 3: a dead grouping/source-intent reconciler used to stay
        # doctor-invisible except indirectly (via USB combo consistency).
        (
            [
                ("jasper-grouping-reconcile.service", "failed", "failed", 0),
                ("jasper-source-intent-reconcile.service", "failed", "failed", 0),
            ],
            "fail",
            resilience.REASON_UNITS_FAILED_OR_UNSTABLE,
        ),
    ],
    ids=["failed-unit", "restart-count", "failed-oneshot", "failed-reconcilers"],
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


def test_runtime_state_units_are_queryable_on_the_doctor_roster():
    """#2802 item 3: `evidence.unit_states()` queries only
    `service_units.DOCTOR_UNIT_ROSTER`, so a unit in `_RUNTIME_STATE_UNITS`
    but missing from the roster never appears in the batch and this check
    silently no-ops on it (the bug that motivated tracking these two)."""
    for unit in _shared._RUNTIME_STATE_UNITS:
        assert unit in service_units.DOCTOR_UNIT_ROSTER, unit


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


# ----------------------------------------------------- check_accessory_bridges


def test_check_accessory_bridges_warns_on_restart_loop(monkeypatch):
    monkeypatch.setattr(
        resilience.accessory_status, "snapshot",
        lambda: {
            "published": True,
            "bridges": {
                "hid": {"restarts": 3, "last_error": "ConnectionError"},
                "wiim_remote_mic": {"restarts": 0, "last_error": None},
            },
        },
    )

    result = resilience.check_accessory_bridges()

    assert (result.status, result.reason) == (
        "warn", resilience.REASON_ACCESSORY_BRIDGE_RESTART_LOOP,
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
    "uptime_sec, resilience_state, expected_status, expected_reason",
    [
        (
            5.0,
            {},
            "ok",
            resilience.REASON_SUPERVISOR_COUNTERS_RESET,
        ),
        (
            3600.0,
            {},
            "ok",
            "",
        ),
        (
            5.0,
            {"shairport": {"enabled": True, "restart_count": 2}},
            "warn",
            resilience.REASON_SUPERVISOR_ISSUES,
        ),
        (
            None,
            {},
            "ok",
            "",
        ),
    ],
    ids=[
        "quiet-within-reset-window",
        "quiet-settled",
        "nonzero-counter-always-warns",
        "uptime-property-absent",
    ],
)
def test_supervisor_snapshots_check_uses_control_uptime_for_counter_reset(
    monkeypatch, uptime_sec, resilience_state, expected_status, expected_reason,
):
    """A jasper-control restart zeroes every supervisor counter with no
    marker of its own. A QUIET row within jasper-control's own unit uptime
    (`ActiveEnterTimestampMonotonic`, in the doctor's shared unit-state
    batch) says the quiet reading only covers time since that restart. A
    nonzero counter always `warn`s regardless of uptime: the shairport
    supervisor alone needs a 60s cold start plus 3x30s probe failures
    before it restarts anything, so it cannot be benign accumulation."""
    monkeypatch.setattr(resilience, "_read_resilience_state", lambda: resilience_state)
    overrides = {}
    if uptime_sec is not None:
        now_us = time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6
        started_us = int(now_us - uptime_sec * 1e6)
        overrides = {
            "jasper-control.service": {
                "active_enter_timestamp_monotonic": started_us,
            },
        }
    monkeypatch.setattr(_evidence, "read_unit_states", _make_unit_states_fake(overrides))

    res = check_supervisor_runtime_snapshots()

    assert res.status == expected_status
    assert res.reason == expected_reason


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


def test_check_supply_voltage_reports_a_stale_sampler_distinctly(monkeypatch):
    """A reachable but wedged sampler must not masquerade as
    REASON_SNAPSHOT_UNAVAILABLE (ADR-0226) — that reason is for jasper-
    control itself being unreachable."""
    import jasper.platform.control_client as control

    monkeypatch.setattr(resilience, "_read_system_metrics_current", lambda: None)
    monkeypatch.setattr(
        control, "get_system_snapshot",
        lambda **kw: {"metrics": {"last_sample_at": 0}},
    )

    result = check_supply_voltage()

    assert result.status == "warn"
    assert result.reason == resilience.REASON_SUPPLY_VOLTAGE_SAMPLER_STALE


@pytest.mark.parametrize(
    "check_name",
    [
        "check_bootloop_guard",
        "check_heal_recency",
        "check_outputd_failure_reconcile_park",
        "check_required_units_active",
        "check_speaker_silence",
        "check_supervisor_runtime_snapshots",
        "check_supply_voltage",
        "check_voice_unit_running",
    ],
)
def test_resilience_checks_are_registered(check_name):
    assert check_name in _registered_check_names()


# ---------------------------------------------------------- check_heal_recency


_HEAL_STALE_AFTER_SEC = 3 * 600.0


def test_the_stale_window_is_three_supervisor_ticks():
    assert (
        resilience._HEAL_STALE_TICKS * heal_supervisor.TICK_INTERVAL_SEC
        == _HEAL_STALE_AFTER_SEC
    )


@pytest.mark.parametrize(
    "heal, status, reason",
    [
        (None, "skipped", resilience.REASON_CONTROL_UNAVAILABLE),
        ({"enabled": False}, "skipped", resilience.REASON_HEAL_UNOBSERVED),
        ({"age": _HEAL_STALE_AFTER_SEC + 60}, "warn", resilience.REASON_HEAL_STALE),
        ({"age": 60.0}, "ok", resilience.REASON_HEAL_RECENT),
        (
            {"age": 60.0, "would_act": {"case": "silent", "action": "restart-audio"}},
            "ok", resilience.REASON_HEAL_RECENT,
        ),
    ],
)
def test_check_heal_recency_verdicts(monkeypatch, heal, status, reason):
    """``age`` is how long ago the supervisor published its last tick."""
    snapshot = None if heal is None else dict(heal)
    if snapshot is not None and "age" in snapshot:
        snapshot["last_tick"] = time.time() - snapshot.pop("age")
    monkeypatch.setattr(
        resilience, "_read_resilience_state",
        lambda: None if snapshot is None else {"heal": snapshot},
    )

    result = resilience.check_heal_recency()

    assert (result.status, result.reason) == (status, reason)


# --------------------------------------- check_outputd_failure_reconcile_park


def _seed_signal_path(code: str | None, *, warmup: bool = False) -> None:
    """Seed jasper-control's /system/snapshot with one signal-path code, or
    with the transport error that means the daemon is unreachable."""
    payload = (
        None if code is None
        else {"audio_health": {
            "signal_path": {
                "code": code, "headline": "headline", "status": "issue",
            },
            "technical": {"sampler": {"warmup_active": warmup}},
        }}
    )
    _evidence.evidence.seed(
        "control_system_snapshot",
        _evidence.StatusRead(payload, None if payload else OSError("refused")),
    )


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
    """outputd owns the DAC write loop, so both fail branches prove silence —
    here with jasper-control unreachable, which is when these rows carry it."""

    _seed_signal_path(None)
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


# ------------------------------------------------------------ speaker silence


def test_the_signal_path_vocabulary_is_partitioned():
    """`SIGNAL_PATH_CODES` is a closed vocabulary and the doctor's silence lead
    projects it, so every member sits in exactly one of the doctor's three
    sets — a code added there fails here until it is classified."""
    from jasper.control.audio_health import SIGNAL_PATH_CODES

    playing = _shared._SIGNAL_PATH_PLAYING_CODES
    unknown = _shared._SIGNAL_PATH_UNKNOWN_CODES
    silent = _shared._SIGNAL_PATH_SILENT_CODES

    assert playing | unknown | silent == SIGNAL_PATH_CODES
    assert len(playing) + len(unknown) + len(silent) == len(SIGNAL_PATH_CODES)


@pytest.mark.parametrize(
    "code, warmup, status, reason, silent",
    [
        ("camilla_stopped", False, "warn", "camilla_stopped", True),
        ("clean", False, "ok", "clean", False),
        (
            "path_unreported", False, "skipped",
            resilience.REASON_SIGNAL_PATH_UNOBSERVED, False,
        ),
        (None, False, "skipped", resilience.REASON_SIGNAL_PATH_UNOBSERVED, False),
        (
            "a_code_from_a_newer_control", False, "skipped",
            resilience.REASON_SIGNAL_PATH_UNOBSERVED, False,
        ),
        ("clean", True, "skipped", resilience.REASON_SIGNAL_PATH_UNOBSERVED, False),
    ],
    ids=["silent", "playing", "cannot-tell", "unreachable", "off-vocabulary", "warming"],
)
def test_speaker_silence_projects_the_control_signal_path(
    code, warmup, status, reason, silent,
):
    """The doctor's silence lead IS jasper-control's signal-path verdict, so
    the /system dashboard headline and the doctor cannot disagree. Anything
    control cannot classify — a code the doctor does not know, an unreachable
    daemon, the warmup window — leaves the row skipped, claiming neither way."""
    _seed_signal_path(code, warmup=warmup)

    result = resilience.check_speaker_silence()

    assert (result.status, result.reason, result.speaker_silent) == (
        status, reason, silent,
    )


@pytest.mark.parametrize(
    "code, warmup, silent",
    [
        (None, False, True),
        ("path_unreported", False, True),
        ("clean", True, True),
        ("clean", False, False),
        ("output_deaf", False, False),
    ],
    ids=["unreachable", "cannot-tell", "warming", "playing", "already-led"],
)
def test_a_down_audio_unit_leads_with_silence_only_without_a_control_verdict(
    monkeypatch, code, warmup, silent,
):
    """The fallback: with no usable verdict from jasper-control — including its
    warmup window, where it reports `clean` for a dead CamillaDSP — the
    doctor's own unit-state rows are the only evidence of silence there is."""
    _seed_signal_path(code, warmup=warmup)
    monkeypatch.setattr(
        _evidence, "read_unit_states",
        _make_unit_states_fake({"jasper-outputd.service": {
            "active_state": "inactive", "result": "success",
        }}),
    )

    result = _shared._service_state_failure(
        "jasper-outputd", "jasper-outputd.service",
        missing="m", not_enabled="n", inactive="i",
    )

    assert result is not None
    assert (result.reason, result.speaker_silent) == ("i", silent)
