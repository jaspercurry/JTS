# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor correction domain."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.cli import doctor
from jasper.correction import bundles

from .correction_bundle_fixtures import write_golden_correction_bundle

from .doctor_test_support import (
    _registered_check_names,
)


def test_check_correction_web_service_ok_when_socket_active(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        unit = cmd[-1]
        out = "active\n" if unit.endswith(".socket") else "inactive\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_web_service()
    assert r.status == "ok"
    assert "socket active" in r.detail


# ---------- #2134: a start-limited jasper-correction-web must not read as ok
#
# Every fixture below is a two-unit snapshot, because the .service and the
# .socket disagree in the failure mode and an earlier revision of this check
# read the wrong one. `_systemctl_show_pair` serves whichever unit the check
# asks for and refuses to invent an answer for a unit the fixture does not
# model, so a check that queries something unmodelled fails loudly here
# instead of silently reading "".

_CORRECTION_WEB_SERVICE = "jasper-correction-web.service"
_CORRECTION_WEB_SOCKET = "jasper-correction-web.socket"

# MEASURED on lab Pi jts4 (systemd 257 / Debian Trixie) by the PR #2216
# adversarial gate, driving transient units that mirror
# deploy/jasper-correction-web.{service,socket}. At the instant the start
# limit is hit systemd's journal reads:
#
#     <unit>.service: Start request repeated too quickly.
#     <unit>.service: Failed with result 'exit-code'.
#     <unit>.socket:  Failed with result 'service-start-limit-hit'.
#
# and `ss -ltn` shows the listener UNBOUND (NRestarts=20, /correction/ dead).
# These are the `systemctl show` bodies for that one real state — not
# hand-written states chosen to match an assumption, which is precisely how
# the first revision of this check shipped blind to its own failure mode.
_JTS4_MEASURED_START_LIMITED = {
    _CORRECTION_WEB_SERVICE: "ActiveState=failed\nResult=exit-code\n",
    _CORRECTION_WEB_SOCKET: "ActiveState=failed\nResult=service-start-limit-hit\n",
}

# The normal socket-activated lifecycle: socket bound and listening, service
# idle-exited between sessions.
_IDLE_BETWEEN_SESSIONS = {
    _CORRECTION_WEB_SERVICE: "ActiveState=inactive\nResult=success\n",
    _CORRECTION_WEB_SOCKET: "ActiveState=active\nResult=success\n",
}

# A household has /correction/ open right now.
_CURRENTLY_SERVING = {
    _CORRECTION_WEB_SERVICE: "ActiveState=active\nResult=success\n",
    _CORRECTION_WEB_SOCKET: "ActiveState=active\nResult=success\n",
}

# A crash still INSIDE the Restart=on-failure budget. MEASURED on jts4 by the
# #2216 delta re-review, capturing systemd's D-Bus PropertiesChanged signals
# (polling misses these: 190 samples over 22 restarts saw `activating` only —
# a sampling artefact). Across 17 ordinary retries the service reported:
#
#     ActiveState: 99 "activating"  34 "failed"  2 "inactive"
#     SubState:    34 "failed-before-auto-restart"
#     socket:      ActiveState=active  ActiveExitTimestampMonotonic=0
#
# Both service ActiveStates therefore occur during routine retrying, and both
# are modelled below. The socket never left `active` at any duration
# (ActiveExitTimestampMonotonic=0, confirmed twice) — which is the whole
# reason this check reads the socket.
_CRASH_INSIDE_RESTART_BUDGET = {
    _CORRECTION_WEB_SERVICE: "ActiveState=activating\nResult=exit-code\n",
    _CORRECTION_WEB_SOCKET: "ActiveState=active\nResult=success\n",
}

# The same routine retry, sampled inside its `failed-before-auto-restart`
# window — the state that makes the SERVICE unreadable for this purpose. A
# check reading the service with `ActiveState == "failed"` would false-fail
# here 34 times in 20 seconds; reading the socket, it is correctly `ok`.
_CRASH_MID_RESTART_FAILED_WINDOW = {
    _CORRECTION_WEB_SERVICE: (
        "ActiveState=failed\nSubState=failed-before-auto-restart\n"
        "Result=exit-code\n"
    ),
    _CORRECTION_WEB_SOCKET: "ActiveState=active\nResult=success\n",
}

# MEASURED by the gate on jts4: `systemctl show` on a unit that does not exist
# answers rc=0 with these values rather than erroring.
_UNIT_NOT_INSTALLED = {
    _CORRECTION_WEB_SERVICE: "ActiveState=inactive\nResult=success\n",
    _CORRECTION_WEB_SOCKET: "ActiveState=inactive\nResult=success\n",
}


def _systemctl_show_pair(states, returncode=0, stderr=""):
    """Fake ``_run`` serving a ``systemctl show`` per-unit state snapshot."""

    def fake_run(cmd, timeout=5.0):
        assert cmd[:2] == ["systemctl", "show"], cmd
        unit = cmd[2]
        assert unit in states, f"check queried an unmodelled unit: {unit}"
        return subprocess.CompletedProcess(
            cmd, returncode, stdout=states[unit], stderr=stderr,
        )

    return fake_run


def test_check_correction_web_start_limited_registered_in_sync_checks():
    assert "check_correction_web_start_limited" in _registered_check_names()


def test_check_correction_web_start_limited_fails_on_measured_start_limited_state(
    monkeypatch,
):
    """The state a real start-limited speaker is actually in.

    #2216 blocker 1: the first revision gated on the SERVICE reporting
    ``Result=start-limit-hit``, a combination the gate could not produce in
    four probes — systemd stamps ``service-start-limit-hit`` on the SOCKET and
    leaves the service holding ``exit-code``. Fed the measured pair, that
    revision returned ``ok`` while ``/correction/`` was refusing connections.
    """
    monkeypatch.setattr(
        doctor.correction, "_run",
        _systemctl_show_pair(_JTS4_MEASURED_START_LIMITED),
    )
    r = doctor.check_correction_web_start_limited()
    assert r.status == "fail"
    assert "Result=service-start-limit-hit" in r.detail
    assert "StartLimitBurst" in r.detail
    # reset-failed clears BOTH units' state; start rebinds the listener.
    assert (
        "sudo systemctl reset-failed jasper-correction-web.socket "
        "jasper-correction-web.service && "
        "sudo systemctl start jasper-correction-web.socket" in r.detail
    )


def test_check_correction_web_start_limited_fails_on_any_failed_socket(monkeypatch):
    """``Result`` is reported, never gated on.

    #2216 nit 2: the first revision cited ``check_service_runtime_state`` as
    prior art, then swapped its ``ActiveState == "failed"`` predicate for a
    narrower match on one systemd ``Result`` string — and that narrowing is
    what created blocker 1. A socket that failed for some other reason (here
    ``trigger-limit-hit``, systemd's other way to kill a socket) is just as
    unbound, so it must still fail rather than depend on one literal.
    """
    monkeypatch.setattr(
        doctor.correction, "_run",
        _systemctl_show_pair({
            _CORRECTION_WEB_SOCKET: "ActiveState=failed\nResult=trigger-limit-hit\n",
        }),
    )
    r = doctor.check_correction_web_start_limited()
    assert r.status == "fail"
    assert "Result=trigger-limit-hit" in r.detail


@pytest.mark.parametrize(
    "states, why",
    [
        (_IDLE_BETWEEN_SESSIONS, "service idle-exited between sessions"),
        (_CURRENTLY_SERVING, "a household has /correction/ open"),
        (_CRASH_INSIDE_RESTART_BUDGET, "crash still inside the Restart budget"),
        (
            _CRASH_MID_RESTART_FAILED_WINDOW,
            "routine retry, sampled in failed-before-auto-restart",
        ),
        (_UNIT_NOT_INSTALLED, "unit not installed on this profile"),
    ],
)
def test_check_correction_web_start_limited_ok_on_healthy_states(
    monkeypatch, states, why,
):
    """Every state that is NOT a wedged socket stays ``ok``.

    The crash-inside-budget rows are the PR's original scoping intent, kept
    because it survives contact with the measurement: the socket rides that
    window out still bound. What did NOT survive was the fixture the first
    revision used for it — service ``ActiveState=failed`` / ``Result=exit-code``
    is the measured TERMINAL state (NRestarts=20, listener unbound), not a
    unit that "will retry on its own" (#2216 blocker 2).

    The ``failed-before-auto-restart`` row is why this check reads the socket
    at all, and it is the guard against "just apply the generic
    ``ActiveState == 'failed'`` predicate to the service": measured, the
    service enters that state 34 times in 20 seconds of ordinary retrying, so
    a service-side read would false-fail on every one of them.
    """
    monkeypatch.setattr(doctor.correction, "_run", _systemctl_show_pair(states))
    r = doctor.check_correction_web_start_limited()
    assert r.status == "ok", why
    assert "ActiveState=failed" not in r.detail  # #2216 nit 1


def test_check_correction_web_start_limited_fails_when_systemctl_errors(monkeypatch):
    """#2216 should-fix 2: an unreadable probe must not render as healthy."""
    monkeypatch.setattr(
        doctor.correction, "_run",
        _systemctl_show_pair(
            {_CORRECTION_WEB_SOCKET: ""},
            returncode=1,
            stderr="Failed to connect to bus: No such file or directory",
        ),
    )
    r = doctor.check_correction_web_start_limited()
    assert r.status == "fail"
    assert "rc=1" in r.detail
    assert "Failed to connect to bus" in r.detail


def test_check_correction_web_start_limited_fails_when_systemctl_errors_yet_parses(
    monkeypatch,
):
    """A non-zero ``systemctl`` is a failed read even when its body parses.

    Without this the ``rc != 0`` clause is dead weight: the sibling test above
    happens to also trip the empty-``ActiveState`` clause, so it would stay
    green with the return-code check deleted. systemctl telling us it failed
    is reason enough to distrust whatever it printed.
    """
    monkeypatch.setattr(
        doctor.correction, "_run",
        _systemctl_show_pair(
            {_CORRECTION_WEB_SOCKET: "ActiveState=active\nResult=success\n"},
            returncode=1,
            stderr="Failed to get properties: Connection timed out",
        ),
    )
    r = doctor.check_correction_web_start_limited()
    assert r.status == "fail"
    assert "rc=1" in r.detail


def test_check_correction_web_start_limited_fails_on_unparseable_output(monkeypatch):
    """rc=0 with a body that carries no ``ActiveState`` is still a failed read."""
    monkeypatch.setattr(
        doctor.correction, "_run",
        _systemctl_show_pair({
            _CORRECTION_WEB_SOCKET: "Failed to get properties: Access denied\n",
        }),
    )
    r = doctor.check_correction_web_start_limited()
    assert r.status == "fail"
    assert "Access denied" in r.detail


def test_check_correction_web_start_limited_skips_without_systemctl(monkeypatch):
    """Dev host: no systemd to be unhealthy. Matches every sibling's wording."""

    def fake_run(cmd, timeout=5.0):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_web_start_limited()
    assert r.status == "ok"
    assert "skipped" in r.detail


# ---------- #1860: jasper-doctor check for long-outstanding idle-exit holds


def test_check_correction_idle_exit_holds_registered_in_sync_checks():
    assert "check_correction_idle_exit_holds" in _registered_check_names()


def test_check_correction_idle_exit_holds_skips_when_service_not_active(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        assert cmd[:2] == ["systemctl", "is-active"]
        return subprocess.CompletedProcess(cmd, 0, stdout="inactive\n", stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "not running" in r.detail


def test_check_correction_idle_exit_holds_skips_when_journalctl_unavailable(
    monkeypatch,
):
    """N-B1: the exception path must not collapse to a generic, indistinguishable
    "unavailable" — the actual exception text must be visible so a broken
    invocation reads differently from a clean no-findings run."""

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        raise FileNotFoundError("journalctl not found")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "journalctl" in r.detail
    assert "journalctl not found" in r.detail
    assert "FileNotFoundError" in r.detail


def test_check_correction_idle_exit_holds_skips_on_nonzero_journalctl_returncode(
    monkeypatch,
):
    """N-B1: a malformed invocation (e.g. a bad --since) exits non-zero without
    raising — that stderr must reach the detail string too, or this is
    indistinguishable from a clean no-findings run forever."""

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="journalctl: invalid option -- since\n",
        )

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "rc=1" in r.detail
    assert "invalid option" in r.detail


def test_check_correction_idle_exit_holds_ok_with_no_deferred_warning(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        assert "warning" in cmd  # -p warning: only escalated lines are fetched
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "no long-outstanding holds" in r.detail


def test_check_correction_idle_exit_holds_warns_on_leaked_hold(monkeypatch):
    """Mirrors the exact rendered line from _systemd.py's _log_deferred_exit
    once HOLD_LEAK_WARN_AFTER_SEC is crossed (#1856's escalation), proving
    this check surfaces the SAME evidence at the doctor surface (#1860)."""
    leaked_line = (
        "systemd idle-exit deferred: 1 active requests/holds after 7530s "
        "idle, busy for 7530s (threshold 600s, holds: relay:level_ramp:room) "
        "— busy past 7200s, so this is a LEAKED hold, not a long session: "
        "the process can no longer idle-exit and its on-idle-exit hook "
        "cannot run"
    )

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        return subprocess.CompletedProcess(cmd, 0, stdout=leaked_line + "\n", stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "warn"
    assert "7530s" in r.detail
    assert "relay:level_ramp:room" in r.detail


def test_check_correction_idle_exit_holds_reports_the_latest_of_several_lines(
    monkeypatch,
):
    """journalctl returns oldest-first; an older (possibly since-resolved)
    line must not shadow the most recent evidence."""
    older = (
        "systemd idle-exit deferred: 1 active requests/holds after 7300s "
        "idle, busy for 7300s (threshold 600s, holds: relay:crossover_v2:session) "
        "— busy past 7200s, so this is a LEAKED hold, not a long session: "
        "the process can no longer idle-exit and its on-idle-exit hook "
        "cannot run"
    )
    newer = (
        "systemd idle-exit deferred: 1 active requests/holds after 7830s "
        "idle, busy for 7830s (threshold 600s, holds: relay:level_ramp:crossover) "
        "— busy past 7200s, so this is a LEAKED hold, not a long session: "
        "the process can no longer idle-exit and its on-idle-exit hook "
        "cannot run"
    )

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"{older}\n{newer}\n",
            stderr="",
        )

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "warn"
    assert "7830s" in r.detail
    assert "relay:level_ramp:crossover" in r.detail
    assert "7300s" not in r.detail


def test_check_correction_https_assets_registered_in_sync_checks():
    assert "check_correction_https_assets" in _registered_check_names()


def _web_root_with_app_css(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text("/* x */", encoding="utf-8")
    return tmp_path


def test_check_correction_https_assets_ok_on_200(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
    monkeypatch.setattr(
        doctor.correction, "_probe_https_status", lambda *a, **k: (200, "")
    )
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "200" in r.detail


def test_check_correction_https_assets_warns_on_http_downgrade(monkeypatch, tmp_path):
    # The bug signature: an HTTPS asset downgrade to http:// → browsers
    # mixed-content-block it.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
    monkeypatch.setattr(
        doctor.correction,
        "_probe_https_status",
        lambda *a, **k: (308, "http://jts.local/assets/app.css"),
    )
    r = doctor.check_correction_https_assets()
    assert r.status == "warn"
    assert "mixed-content" in r.detail.lower()
    assert "443" in r.detail


def test_check_correction_https_assets_skips_without_web_root(monkeypatch, tmp_path):
    # Dev checkout: no /usr/share/jasper-web/assets/app.css → skip, never probes.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("must not probe when the web root is absent")

    monkeypatch.setattr(doctor.correction, "_probe_https_status", _boom)
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "skip" in r.detail.lower()


def test_check_correction_https_assets_skips_when_443_unreachable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))

    def _refused(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.correction, "_probe_https_status", _refused)
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "not reachable" in r.detail.lower()


def test_check_correction_state_dirs_warns_on_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(tmp_path / "missing"))
    r = doctor.check_correction_state_dirs()
    assert r.status == "warn"
    assert "missing" in r.detail


def test_check_correction_uploaded_calibration_sign_flags_only_uploads(
    monkeypatch,
    tmp_path,
):
    """Vendor records are repaired automatically on deploy; an UPLOADED
    record's convention is the household's own declaration, so the doctor
    surfaces it for review instead of anyone flipping it silently."""
    from jasper.audio_measurement import calibration as cal

    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    cal.store_calibration(
        text="20 -1\n1000 0\n20000 2\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        sign_convention="correction",
        root=tmp_path,
    )
    cal.store_calibration(  # a vendor record: not this check's business
        text="20 -1\n1000 0\n20000 2\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="UMIK-2",
        source="vendor_lookup",
        serial="810-8494",
        sign_convention="correction",
        root=tmp_path,
    )

    r = doctor.check_correction_uploaded_calibration_sign()
    assert r.status == "warn"
    assert "Lab mic" in r.detail
    assert "1 uploaded calibration(s)" in r.detail
    assert "/correction/" in r.detail

    # An upload that already declares the response convention is clean, and
    # the check never fails the doctor either way.
    cal.store_calibration(
        text="20 -3\n1000 0\n20000 4\n",
        provider="manual_upload",
        model="other",
        label="Other mic",
        source="uploaded:other.txt",
        sign_convention="response",
        root=tmp_path,
    )
    assert doctor.check_correction_uploaded_calibration_sign().status == "warn"

    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "empty"))
    clean = doctor.check_correction_uploaded_calibration_sign()
    assert clean.status == "ok"


def test_check_correction_current_config_reports_missing_config(
    monkeypatch,
    tmp_path,
):
    statefile = tmp_path / "statefile.yml"
    missing = tmp_path / "does-not-exist.yml"
    statefile.write_text(f"config_path: {missing}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    r = doctor.check_correction_current_config()
    assert r.status == "fail"
    assert "missing config" in r.detail


def test_check_correction_current_config_reports_flat_base(
    monkeypatch,
    tmp_path,
):
    statefile = tmp_path / "statefile.yml"
    base = tmp_path / "v1.yml"
    base.write_text("# base\n")
    statefile.write_text(f"config_path: {base}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    r = doctor.check_correction_current_config()
    assert r.status == "warn"
    assert "custom/non-JTS" in r.detail


def test_check_correction_current_config_reports_jts_sound_config(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "sound_current.yml"
    generated.write_text(
        "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
        "filters:\n"
        "  flat:\n"
        "    type: Gain\n"
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS sound preference" in r.detail
    assert "no room correction" in r.detail.lower()


def test_check_correction_current_config_reports_active_speaker_baseline(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "active_speaker_baseline.yml"
    generated.write_text(
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_baseline_config\n"
        "filters:\n"
        "  active_baseline_headroom:\n"
        "    type: Gain\n",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS active-speaker baseline" in r.detail
    assert "managed by the active-speaker" in r.detail


def test_check_correction_current_config_reports_active_leader_program_bake(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "grouping_active_leader_bake.yml"
    generated.write_text(
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_program_bake_config\n"
        "devices:\n"
        "  playback:\n"
        "    type: File\n",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS active-leader program bake" in r.detail
    assert "managed by the active-speaker" in r.detail


def test_check_correction_current_config_reports_generated_correction(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "correction_abc_1700000000.yml"
    generated.write_text("filters:\n  room_peq_1:\n    type: Biquad\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "session=abc" in r.detail
    assert "peqs=1" in r.detail


def test_check_camilla_volume_limit_ok(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n  volume_limit: 0.0\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "ok"
    assert "volume_limit=0.0" in r.detail


def test_check_camilla_volume_limit_fails_when_missing(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "fail"
    assert "omits devices.volume_limit" in r.detail


@pytest.mark.parametrize(
    "text",
    [
        "devices:\n  playback:\n    volume_limit: 0.0\n",
        "devices:\n  volume_limit: 0.0\ndevices: {volume_limit: 9.0}\n",
        "devices:\n  volume_limit: 0.0\n  volume_limit: 9.0\n",
    ],
)
def test_check_camilla_volume_limit_fails_when_ownership_is_ambiguous(
    monkeypatch,
    tmp_path,
    text,
):
    config = tmp_path / "v1.yml"
    config.write_text(text)
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    result = doctor.check_camilla_volume_limit()

    assert result.status == "fail"
    assert "omits devices.volume_limit" in result.detail


def test_check_camilla_volume_limit_fails_when_positive(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n  volume_limit: 6.0\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "fail"
    assert "expected <=" in r.detail


def test_check_camilla_volume_limit_registered_in_sync_checks():
    assert "check_camilla_volume_limit" in _registered_check_names()


def test_active_speaker_runtime_graph_registered_in_sync_checks():
    assert "check_active_speaker_runtime_graph" in _registered_check_names()


def test_active_speaker_output_hardware_match_registered_in_sync_checks():
    assert "check_active_speaker_output_hardware_match" in _registered_check_names()


def test_active_speaker_runtime_graph_ok_without_topology(monkeypatch, tmp_path):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _flat_yaml, _topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_topology([]), path=topology_path)
    config = tmp_path / "outputd-cutover.yml"
    config.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "ok"
    assert "no roleful/protected outputs" in r.detail


def test_active_speaker_runtime_graph_fails_corrupt_saved_topology(
    monkeypatch,
    tmp_path,
):
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "saved output topology is unavailable or invalid" in r.detail
    assert "not valid JSON" in r.detail


def test_active_speaker_runtime_graph_fails_flat_graph_on_tweeter_topology(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _flat_yaml,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    config = tmp_path / "outputd-cutover.yml"
    config.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "DAC output 2" in r.detail
    assert "flat full-range graph" in r.detail


def test_active_speaker_runtime_graph_accepts_staged_active_startup(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _active_yaml,
        _staged_metadata,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    config = tmp_path / "active_speaker_staged_startup.yml"
    config.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(
        json.dumps(_staged_metadata(topology, config)),
        encoding="utf-8",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH", str(metadata))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "ok"
    assert "all_muted_active_startup" in r.detail


def test_active_speaker_runtime_graph_warns_when_parked(monkeypatch, tmp_path):
    # #2135: a declared-but-uncommissioned roleful topology is seeded a parked
    # (DAC-less, all-muted) graph so the deploy can finish. Nothing is broken and
    # nothing is audible, so this is a WARN, not a fail — and it must name the two
    # exits verbatim rather than restating graph internals.
    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_EXITS,
        build_parked_muted_graph,
    )
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "active_speaker_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "warn"
    assert PARKED_MUTED_EXITS in r.detail


def test_check_sound_profile_reports_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing.json"))

    r = doctor.check_sound_profile()

    assert r.status == "ok"
    assert "default Flat" in r.detail


def test_check_sound_profile_warns_when_saved_profile_not_active(
    monkeypatch,
    tmp_path,
):
    profile = tmp_path / "sound_profile.json"
    profile.write_text(
        json.dumps(
            {
                "enabled": True,
                "curve_id": "harman",
                "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
            }
        )
    )
    statefile = tmp_path / "statefile.yml"
    base = tmp_path / "v1.yml"
    base.write_text("# base\n")
    statefile.write_text(f"config_path: {base}\n")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_sound_profile()

    assert r.status == "warn"
    assert "curve=harman" in r.detail
    assert "not reflected" in r.detail


def test_check_sound_profile_fails_on_corrupt_json(monkeypatch, tmp_path):
    profile = tmp_path / "sound_profile.json"
    profile.write_text("{not json")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = doctor.check_sound_profile()

    assert r.status == "fail"
    assert "could not read" in r.detail


def test_check_dsp_apply_state_reports_success(monkeypatch, tmp_path):
    state = tmp_path / "dsp_apply_state.json"
    state.write_text(
        json.dumps(
            {
                "op_id": "abcdef123456",
                "source": "sound",
                "phase": "done",
                "result": "success",
                "candidate_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
            }
        )
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state))

    r = doctor.check_dsp_apply_state()

    assert r.status == "ok"
    assert "source=sound" in r.detail
    assert "result=success" in r.detail


def test_check_dsp_apply_state_fails_on_rollback_failure(monkeypatch, tmp_path):
    state = tmp_path / "dsp_apply_state.json"
    state.write_text(
        json.dumps(
            {
                "op_id": "abcdef123456",
                "source": "correction",
                "phase": "load",
                "result": "load_failed_rollback_failed",
                "rollback_attempted": True,
                "rollback_succeeded": False,
            }
        )
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state))

    r = doctor.check_dsp_apply_state()

    assert r.status == "fail"
    assert "rollback_failed" in r.detail


# --- #1666: canonical active-speaker baseline durability -------------------


def test_check_active_speaker_baseline_canonical_registered_in_sync_checks():
    assert "check_active_speaker_baseline_canonical" in _registered_check_names()


def test_active_speaker_baseline_canonical_ok_when_live_is_canonical(
    monkeypatch,
    tmp_path,
):
    canonical = tmp_path / "active_speaker_baseline.yml"
    canonical.write_text("# whatever is currently loaded\n", encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {canonical}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(canonical))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert "live config is the canonical file" in r.detail


def test_active_speaker_baseline_canonical_ok_when_not_applicable(
    monkeypatch,
    tmp_path,
):
    """A live config that is not an active-speaker baseline candidate at all
    (a plain stereo/flat topology's own generated config) is not applicable
    -- never a false warning on the majority of the fleet that has no active
    baseline commissioned."""
    live = tmp_path / "outputd-cutover.yml"
    live.write_text("devices:\n  samplerate: 48000\n", encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert "not applicable" in r.detail


def test_active_speaker_baseline_canonical_ok_when_statefile_unreadable(
    monkeypatch,
    tmp_path,
):
    """N2 (#1666 review): a missing/unreadable outputd statefile is already
    surfaced as a real failure by check_active_speaker_runtime_graph (when a
    roleful topology needs it); this check's own scope -- comparing canonical
    to the live baseline -- cannot be evaluated at all here, so it is not
    applicable rather than a redundant false warning."""
    monkeypatch.setenv(
        "JASPER_CAMILLA_STATEFILE", str(tmp_path / "missing-statefile.yml")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert "not applicable" in r.detail


async def test_active_speaker_baseline_canonical_ok_when_content_matches_live(
    monkeypatch,
    tmp_path,
):
    """#1666: the common case -- every successful apply's post-success
    promote keeps canonical's content equal to whatever candidate is
    actually live."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        _prior_payload,
        run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    live_path = Path(run8_payload["profile"]["config"]["path"])
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live_path}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(config_path))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert str(config_path) in r.detail


async def test_active_speaker_baseline_canonical_warns_on_divergence(
    monkeypatch,
    tmp_path,
):
    """#1666: canonical currently holds run-8's promoted bytes (the fixture's
    most recent apply); simulating CamillaDSP still running the PRIOR
    candidate produces a genuine content mismatch -- warn, since the live
    graph is the audible truth and is correct, but the canonical file is
    stale for other readers."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        prior_payload,
        _run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    live_path = Path(prior_payload["profile"]["config"]["path"])
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live_path}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(config_path))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "warn"
    assert "does not match" in r.detail
    assert str(config_path) in r.detail
    assert str(live_path) in r.detail


def test_active_speaker_baseline_canonical_warns_on_missing_canonical(
    monkeypatch,
    tmp_path,
):
    """#1666: a live applied-candidate sibling with no canonical file yet (a
    box whose last promote never ran, or hasn't run since this fix
    deployed) warns rather than failing -- the next apply/restore
    re-promotes it."""
    canonical = tmp_path / "active_speaker_baseline.yml"
    live = tmp_path / "active_speaker_baseline_candidate_abc123def456.yml"
    live.write_text("# a real applied candidate, never promoted\n", encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(canonical))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "warn"
    assert "missing" in r.detail


def test_check_correction_latest_bundle_warns_without_calibration(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "abc"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "abc",
            "state": "ready",
            "started_at": 1000,
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "result.json",
        {"bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION},
        kind="analysis_result",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="tests.test_doctor",
        dependencies=["info.json"],
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "warn"
    assert "no calibrated mic" in r.detail


def test_check_correction_latest_bundle_warns_when_failed(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "failed"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "failed",
            "state": "failed",
            "started_at": 1000,
            "error": "analysis failed: capture clipped",
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "warn"
    assert "capture clipped" in r.detail


def test_check_correction_latest_bundle_reports_bundle_collection(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "old", started_at=1000)
    write_golden_correction_bundle(sessions, "new", started_at=2000)
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "ok"
    assert "session=new" in r.detail
    assert "bundles=2" in r.detail
    assert "storage=" in r.detail
    assert "private_raw=8/" in r.detail
    assert "evidence=complete(" in r.detail
    assert "old raw recordings present (8 files)" in r.detail


def test_correction_doctor_checks_registered():
    names = _registered_check_names()
    assert "check_correction_web_service" in names
    assert "check_correction_state_dirs" in names
    assert "check_correction_current_config" in names
    assert "check_sound_profile" in names
    assert "check_dsp_apply_state" in names
    assert "check_correction_latest_bundle" in names
    assert "check_crossover_v2_cloud_pipeline" in names


def test_check_crossover_v2_cloud_pipeline_ok_when_never_run(monkeypatch):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(v2host, "load_v2_state", lambda: None)

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert "no cloud-measurement session" in r.detail


def test_check_crossover_v2_cloud_pipeline_reports_per_group_verdict(monkeypatch):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": True},
                    "pipeline": {
                        "available": True,
                        "spec": {
                            "overall_passed": True,
                            "bands": [
                                {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": True},
                            ],
                        },
                        "merged_excluded_bands_hz": [[8000.0, 9000.0]],
                    },
                },
                "cloud_verify": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": False, "bands": []},
                        "merged_excluded_bands_hz": [],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    # cloud_verify (the post-apply, household-actionable grade) failed its
    # spec -- WARN, not FAIL (an out-of-spec speaker is a measurement
    # finding, not a broken daemon). BLOCKER review finding (2026-07-27):
    # only cloud_verify's own verdict gates the warn -- cloud_measure's PASS
    # here is incidental to this fixture, not what drives the status.
    assert r.status == "warn"
    assert (
        "cloud_measure: spec=pass excluded_intervals=1 geometry_locked=True" in r.detail
    )
    assert (
        "cloud_verify: spec=fail excluded_intervals=0 geometry_locked=False" in r.detail
    )


def test_check_crossover_v2_cloud_pipeline_ok_when_pre_apply_fails_but_post_apply_passes(
    monkeypatch,
):
    """BLOCKER review finding (2026-07-27): ``cloud_measure`` is the
    PRE-APPLY cloud -- the uncorrected baseline that EXISTS in order to be
    out of spec. Gating the warn on ANY phase's verdict meant a perfectly
    corrected speaker warned forever (reviewer reproduced against the real
    S0 corpus through this exact doctor check: the pre-apply grade never
    changes no matter how good the fix is). Only ``cloud_verify`` -- the
    post-apply, household-actionable grade -- may gate the warn;
    ``cloud_measure``'s own verdict still appears in the detail text, never
    hidden."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": False, "bands": []},
                        "merged_excluded_bands_hz": [[8000.0, 9000.0]],
                    },
                },
                "cloud_verify": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": True, "bands": []},
                        "merged_excluded_bands_hz": [],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert "cloud_measure: spec=fail" in r.detail
    assert "cloud_verify: spec=pass" in r.detail


def _v2_applied_state(**overrides):
    state = {"applied": True, "session_id": "sess-graded"}
    state.update(overrides)
    return state


def test_applied_profile_with_no_post_apply_grade_warns(monkeypatch):
    """PR-L4 item 6: the silence its sibling check structurally cannot see.

    ``check_crossover_v2_cloud_pipeline`` warns on a FAILING post-apply grade
    and is deliberately gated that way; a MISSING grade renders as no phase at
    all, so it printed a clean tick over a speaker running an unverified
    correction — the 2026-07-27 shape."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(v2host, "load_v2_state", _v2_applied_state)
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "warn"
    assert "never graded" in r.detail


def test_applied_profile_graded_by_verify_alone_is_ok(monkeypatch):
    """Express tier omits the post-apply position group entirely, so a passing
    VERIFY outcome is the whole grade and must satisfy the check on its own."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: _v2_applied_state(verify={"outcome": "pass"}),
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "ok"
    assert "applied and graded" in r.detail


def test_applied_profile_with_inconclusive_verify_warns(monkeypatch):
    """Checked, and the check could not decide — a distinct silence from
    "never checked", and reported as such."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: _v2_applied_state(verify={"outcome": "inconclusive"}),
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "warn"
    assert "inconclusive" in r.detail
    assert "never graded" not in r.detail


# --- R19 honest grading: the green tick over a failed gauge (#2160/#2098) ---


def _r19_cloud_verify(*, passed, flatness):
    return {
        "cloud_verify": {
            "geometry": {"locked": False},
            "pipeline": {
                "available": True,
                "spec": {"overall_passed": passed, "bands": []},
                "merged_excluded_bands_hz": [
                    [1400.0, 1900.0], [3000.0, 3200.0],
                    [5000.0, 5400.0], [9000.0, 9600.0],
                ],
                "flatness": flatness,
            },
        },
    }


_R19_FAILED_GAUGE = {
    "max_db": -4.628, "max_hz": 1650.0, "max_band_hz": [1250.0, 2000.0],
    "tolerance_db": 1.5, "rms_db": 1.9, "n_bins": 700, "n_excluded": 40,
    "evaluable": True, "passed": False,
}


def _r19_doctor_state(monkeypatch, **overrides):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host, "load_v2_state", lambda: _v2_applied_state(**overrides)
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )


def test_a_failed_spatial_grade_is_not_a_green_tick(monkeypatch):
    """#2160, measured on jts3 2026-08-07: this line printed ``applied and
    graded (state=graded, verify=pass)`` while the cloud line one row up read
    ``spec=fail worst=-4.63dB excluded_intervals=4 geometry_locked=False``.
    ``state`` answers "was it checked" and ``graded`` is an honest answer to
    that, so keying ok on it reported a failure as a pass. Grade and disclose,
    never gate: still a warn, and the tune stays on the speaker."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "pass"},
        cloud=_r19_cloud_verify(passed=False, flatness=_R19_FAILED_GAUGE),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "spatial grade FAILED" in r.detail
    # The number rides the verdict, from the same gauge the cloud line prints.
    assert "-4.63dB" in r.detail
    assert "@ 1650Hz" in r.detail
    # The ruling, in the words a household reads: nothing was reverted.
    assert "stays on the speaker" in r.detail


def test_a_full_session_verified_only_at_the_mark_warns(monkeypatch):
    """#2098's own field evidence — Full, ``verify.outcome=pass``, no cloud.
    The local pass is real and is preserved in the wording; what changes is
    that it stops being reported as the claim Full promised."""
    _r19_doctor_state(monkeypatch, tier="full", verify={"outcome": "pass"})

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "verified at the mark" in r.detail
    assert "never closed" in r.detail
    assert "full-tier" in r.detail


def test_an_express_session_verified_at_the_mark_stays_ok(monkeypatch):
    """The mark IS express's whole promise. Warning here would fire on every
    express session ever run — the mirror of the defect being fixed."""
    _r19_doctor_state(monkeypatch, tier="express", verify={"outcome": "pass"})

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "scope=mark" in r.detail


def test_a_group_that_could_not_be_graded_is_not_reported_as_a_miss(monkeypatch):
    """``passed=False, evaluable=False`` means "could not be measured", not
    "failed" — ``SpecFlatness.passed``'s own read-it-with-evaluable rule."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "pass"},
        cloud=_r19_cloud_verify(passed=False, flatness={
            **_R19_FAILED_GAUGE,
            "max_db": None, "max_hz": None, "evaluable": False,
        }),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "could not be measured" in r.detail
    assert "FAILED" not in r.detail


def test_a_closed_and_passing_spatial_grade_is_ok(monkeypatch):
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "pass"},
        cloud=_r19_cloud_verify(passed=True, flatness={
            **_R19_FAILED_GAUGE, "max_db": 0.9, "passed": True,
        }),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "scope=spatial" in r.detail


def test_an_unknown_spatial_word_from_a_later_build_does_not_invent_a_verdict(
    monkeypatch,
):
    """The same degrade rule ``state`` already follows: saying what the state
    said beats inventing a warning about a word this build cannot read. The
    grade is injected directly because no producer path can emit it — that is
    the point of the case."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "crossover_v2_status_block",
        lambda: {
            "tier": "full",
            "post_apply_grade": {
                "state": v2host.GRADE_GRADED,
                "graded": True,
                "verify_outcome": "pass",
                "scope": "hemispherical-2027",
                "spatial": "graded_from_orbit",
                "complete": True,
            },
        },
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "applied and graded" in r.detail


def test_unapplied_session_is_not_asked_for_a_grade(monkeypatch):
    """Nothing on the speaker, nothing to grade — never a manufactured warn."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(v2host, "load_v2_state", lambda: {"applied": False})
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "ok"
    assert "no applied measured crossover" in r.detail


def test_check_crossover_v2_cloud_pipeline_ok_when_every_group_passes(monkeypatch):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": True, "bands": []},
                        "merged_excluded_bands_hz": [],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert "cloud_measure: spec=pass" in r.detail


def test_check_crossover_v2_cloud_pipeline_reports_n_a_when_pipeline_unavailable(
    monkeypatch,
):
    """N4 review finding (2026-07-26): a group that CLOSED (geometry
    decided) but whose honest-instrument pipeline never became available
    (``combine_failed`` / ``pipeline_failed``) must read as ``spec=n/a`` —
    distinct from both ``pass`` and ``fail`` — and must not flip the
    overall check status to warn (an unavailable reading is not itself a
    spec failure). ``excluded_intervals=n/a``, not ``=0`` (SF-1 review
    finding, 2026-07-27): ``0`` would read as "the pipeline looked and found
    no interference" — a fabricated-clean claim for a pipeline that never
    ran at all."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": True},
                    "pipeline": {"available": False, "reason": "combine_failed"},
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert (
        "cloud_measure: spec=n/a excluded_intervals=n/a geometry_locked=True"
        in r.detail
    )
