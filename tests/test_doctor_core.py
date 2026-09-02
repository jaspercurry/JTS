# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor core domain."""

import asyncio
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace


from jasper.cli import doctor
from jasper.cli.doctor import CheckResult, render_json
from jasper.config import Config
from jasper.control.restart_broker import MANAGED_UNITS


def test_json_mode_reports_unhandled_check_exception(monkeypatch, capsys):
    """Machine-readable mode should stay machine-readable even if a
    diagnostic check raises unexpectedly."""
    monkeypatch.setattr(doctor, "_load_env_files", lambda: None)
    monkeypatch.setattr(Config, "from_env", staticmethod(lambda: object()))

    async def boom(_cfg):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(doctor, "run_async", boom)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--json"])

    try:
        doctor.main()
    except SystemExit as e:
        assert e.code == 1
    else:  # pragma: no cover - defensive, main() should always exit.
        raise AssertionError("main() did not exit")

    payload = json.loads(capsys.readouterr().out)
    assert payload["fails"] == 1
    assert payload["results"][0]["name"] == "jasper-doctor"
    assert "synthetic failure" in payload["error"]


def test_json_mode_endpoint_tier_does_not_require_voice_provider(
    monkeypatch,
    capsys,
):
    """Endpoint doctor must not build full voice Config before filtering.

    A freshly imaged dumb endpoint has no JASPER_VOICE_PROVIDER by design;
    it should still report endpoint health instead of failing at Config
    construction.
    """
    monkeypatch.setattr(doctor, "_load_env_files", lambda: None)
    monkeypatch.setattr(doctor, "read_install_profile", lambda: "endpoint")
    monkeypatch.setattr(
        Config,
        "from_env",
        staticmethod(
            lambda: (_ for _ in ()).throw(
                AssertionError("endpoint doctor must not construct full Config")
            )
        ),
    )
    monkeypatch.setenv("JASPER_USAGE_DB", "/tmp/jasper-endpoint-usage.db")

    async def fake_run_async(cfg):
        assert cfg.usage_db == "/tmp/jasper-endpoint-usage.db"
        return [doctor.CheckResult("endpoint smoke", "ok", "minimal cfg")]

    monkeypatch.setattr(doctor, "run_async", fake_run_async)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--json"])

    try:
        doctor.main()
    except SystemExit as e:
        assert e.code == 0
    else:  # pragma: no cover - defensive, main() should always exit.
        raise AssertionError("main() did not exit")

    payload = json.loads(capsys.readouterr().out)
    assert payload["fails"] == 0
    assert payload["results"] == [
        {
            "name": "endpoint smoke",
            "status": "ok",
            "detail": "minimal cfg",
        }
    ]


def test_doctor_check_exception_becomes_fail_result():
    def explode():
        raise RuntimeError("synthetic check failure")

    result = doctor._run_doctor_check(("explosive check", explode))

    assert result.name == "explosive check"
    assert result.status == "fail"
    assert "RuntimeError: synthetic check failure" in result.detail


def test_doctor_check_exception_redacts_secret_like_values():
    def explode():
        raise RuntimeError(
            "refresh_token=super-secret-refresh "
            "Bearer super-secret-access-token "
            "sk-super-secret-openai-key"
        )

    result = doctor._run_doctor_check(("sensitive check", explode))

    assert "super-secret-refresh" not in result.detail
    assert "super-secret-access-token" not in result.detail
    assert "sk-super-secret-openai-key" not in result.detail
    assert "refresh_token=<redacted>" in result.detail
    assert "Bearer <redacted>" in result.detail
    assert "sk-s...-key" in result.detail


def test_async_doctor_check_exception_becomes_fail_result():
    async def explode():
        raise RuntimeError("synthetic async failure")

    result = asyncio.run(
        doctor._run_async_doctor_check("async check", explode),
    )

    assert result.name == "async check"
    assert result.status == "fail"
    assert "RuntimeError: synthetic async failure" in result.detail


def test_legacy_endpoint_token_doctor_behaves_as_streambox(monkeypatch):
    """A persisted/legacy 'endpoint' token normalizes to streambox, so the
    doctor applies the streambox skip behaviour (voice/brain groups skipped,
    local audio kept)."""
    from jasper.cli.doctor._registry import RegisteredCheck

    ran: list[str] = []

    def env_check():
        ran.append("env")
        return doctor.CheckResult("env file", "ok", "ran")

    def voice_check(_cfg):
        ran.append("voice")
        return doctor.CheckResult("provider key", "fail", "should not run")

    def web_check():
        ran.append("web")
        return doctor.CheckResult("management surface", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "endpoint")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(order=0, group="env", func=env_check),
            RegisteredCheck(
                order=1,
                group="voice",
                func=voice_check,
                needs_cfg=True,
                label="provider key",
            ),
            RegisteredCheck(order=1.5, group="web", func=web_check),
        ],
    )

    results = asyncio.run(doctor.run_async(object()))

    assert ran == ["env", "web"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("env file", "ok", "ran"),
        ("provider key", "ok", "not installed (streambox profile)"),
        ("management surface", "ok", "ran"),
    ]


def test_streambox_doctor_config_does_not_require_voice_provider(monkeypatch):
    monkeypatch.delenv("JASPER_VOICE_PROVIDER", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts4.local")
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS4")

    cfg = doctor._doctor_config_from_env("streambox")

    assert cfg.usage_db
    assert cfg.camilla_host == "127.0.0.1"
    assert cfg.camilla_port == 1234
    assert cfg.spotify_enabled is False
    assert cfg.spotify_device_name == "JTS4"
    assert cfg.spotify_setup_url == "http://jts4.local/spotify"


def test_streambox_doctor_skips_voice_brain_but_keeps_local_audio_checks():
    by_name = {entry.func.__name__: entry for entry in doctor.registered_checks()}

    assert (
        doctor._doctor_skip_reason(by_name["check_provider_key"], "streambox")
        == "not installed (streambox profile)"
    )
    assert doctor._doctor_skip_reason(
        by_name["check_openwakeword_model"],
        "streambox",
    )
    assert doctor._doctor_skip_reason(
        by_name["check_aec_bridge_running"],
        "streambox",
    )
    assert doctor._doctor_skip_reason(by_name["check_mic_capture"], "streambox")
    assert doctor._doctor_skip_reason(by_name["check_tts_open"], "streambox")
    assert not doctor._doctor_skip_reason(
        by_name["check_camilla_websocket"],
        "streambox",
    )
    assert not doctor._doctor_skip_reason(
        by_name["check_librespot_running"],
        "streambox",
    )
    assert not doctor._doctor_skip_reason(
        by_name["check_correction_web_service"],
        "streambox",
    )


def test_streambox_profile_doctor_keeps_local_audio_groups(monkeypatch):
    from jasper.cli.doctor._registry import RegisteredCheck

    ran: list[str] = []

    def voice_check(_cfg):
        ran.append("voice")
        return doctor.CheckResult("provider key", "fail", "should not run")

    def check_mic_capture(_cfg):
        ran.append("mic")
        return doctor.CheckResult("mic capture", "fail", "should not run")

    def renderer_check(_cfg):
        ran.append("renderers")
        return doctor.CheckResult("librespot.service", "ok", "ran")

    def correction_check():
        ran.append("correction")
        return doctor.CheckResult("room correction service", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(
                order=0,
                group="voice",
                func=voice_check,
                needs_cfg=True,
                label="provider key",
            ),
            RegisteredCheck(
                order=1,
                group="audio",
                func=check_mic_capture,
                needs_cfg=True,
                label="mic capture",
            ),
            RegisteredCheck(
                order=2,
                group="renderers",
                func=renderer_check,
                needs_cfg=True,
                label="librespot.service",
            ),
            RegisteredCheck(order=3, group="correction", func=correction_check),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert ran == ["renderers", "correction"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("provider key", "ok", "not installed (streambox profile)"),
        ("mic capture", "ok", "not installed (streambox profile)"),
        ("librespot.service", "ok", "ran"),
        ("room correction service", "ok", "ran"),
    ]


def test_run_async_parallelizes_blocking_checks_but_preserves_order(
    monkeypatch,
):
    from jasper.cli.doctor._registry import RegisteredCheck

    active = 0
    max_active = 0
    lock = threading.Lock()

    def make_check(name: str, delay: float):
        def check():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(delay)
            with lock:
                active -= 1
            return doctor.CheckResult(name, "ok", "ran")

        return check

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "full")
    monkeypatch.setenv("JASPER_DOCTOR_MAX_CONCURRENCY", "3")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(order=0, group="test", func=make_check("a", 0.15)),
            RegisteredCheck(order=1, group="test", func=make_check("b", 0.15)),
            RegisteredCheck(order=2, group="test", func=make_check("c", 0.15)),
            RegisteredCheck(order=3, group="test", func=make_check("d", 0.15)),
            RegisteredCheck(order=4, group="test", func=make_check("e", 0.15)),
            RegisteredCheck(order=5, group="test", func=make_check("f", 0.15)),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert [r.name for r in results] == ["a", "b", "c", "d", "e", "f"]
    assert max_active == 3


def test_run_async_serializes_checks_in_same_exclusive_group(monkeypatch):
    from jasper.cli.doctor._registry import RegisteredCheck

    active = 0
    max_active = 0
    lock = threading.Lock()

    def exclusive(name: str):
        def check():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return doctor.CheckResult(name, "ok", "ran")

        return check

    def ordinary():
        time.sleep(0.05)
        return doctor.CheckResult("c", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "full")
    monkeypatch.setenv("JASPER_DOCTOR_MAX_CONCURRENCY", "3")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(
                order=0,
                group="test",
                func=exclusive("a"),
                exclusive_group="audio-probe",
            ),
            RegisteredCheck(
                order=1,
                group="test",
                func=exclusive("b"),
                exclusive_group="audio-probe",
            ),
            RegisteredCheck(order=2, group="test", func=ordinary),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert [r.name for r in results] == ["a", "b", "c"]
    assert max_active == 1


def _summary_line(results, capsys) -> tuple[int, str]:
    exit_code = doctor.render(results)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return exit_code, lines[-1]


def test_summary_never_calls_a_silent_speaker_non_critical(capsys):
    """#2471: the household hears nothing — that is not "non-critical".

    The severity rides the WORDS. The colour and the exit code keep their one
    meaning (a parked box is silent, not broken, and must stay deployable per
    #2145), so this pins the wording, not a new exit status.
    """

    exit_code, line = _summary_line(
        [
            doctor.CheckResult("transit", "warn", "no MTA key"),
            doctor.CheckResult(
                "active speaker runtime graph",
                "warn",
                "parked silent",
                speaker_silent=True,
            ),
        ],
        capsys,
    )

    assert exit_code == 0
    assert "the speaker is silent" in line
    assert "non-critical" not in line
    assert "2 warning(s)" in line


def test_summary_still_calls_cosmetic_warnings_non_critical(capsys):
    """The inverse half: with nothing silent, the old wording is correct."""

    exit_code, line = _summary_line(
        [doctor.CheckResult("transit", "warn", "no MTA key")], capsys
    )

    assert exit_code == 0
    assert line.endswith("1 warning(s) — non-critical.\x1b[0m")
    assert "silent" not in line


def test_summary_names_the_silence_alongside_a_failure(capsys):
    """A fail does not excuse burying it: "1 failed" says nothing about sound."""

    exit_code, line = _summary_line(
        [
            doctor.CheckResult("nginx", "fail", "down"),
            doctor.CheckResult("blockers", "warn", "parked", speaker_silent=True),
        ],
        capsys,
    )

    assert exit_code == 1
    assert "1 failed" in line
    assert "the speaker is silent" in line


def test_summary_stays_green_when_nothing_is_wrong(capsys):
    exit_code, line = _summary_line(
        [doctor.CheckResult("nginx", "ok", "up")], capsys
    )

    assert exit_code == 0
    assert "all checks passed." in line
    assert "silent" not in line


def test_a_silent_failure_alone_does_not_count_itself_among_the_warnings(capsys):
    """The `fail` half of the contract, which was unpinned and wrong.

    No check sets the flag on a `fail` today, but `speaker_silent` exists to be
    extended, and `check_active_speaker_runtime_graph`'s own `fail` branches
    ("statefile points at missing config", "Camilla graph is unsafe...") are
    plausible next members. Before this, that reachable state printed
    "1 failed, 0 warning(s), 1 of them: the speaker is silent" — one of zero.

    The silence LEADS the line now, so it has no "them" to be counted out of.
    """

    exit_code, line = _summary_line(
        [doctor.CheckResult("graph", "fail", "unsafe", speaker_silent=True)],
        capsys,
    )

    assert exit_code == 1
    assert "the speaker is silent" in line
    assert "of them" not in line
    # The counts stay behind the fact, and stay literal.
    assert "1 failed, 0 warning(s)" in line


def test_a_silent_failure_does_not_pin_its_silence_on_a_cosmetic_warning(capsys):
    """The misattribution cell: the silence belongs to the FAIL, not the warn.

    The old wording spliced ", 1 of them: the speaker is silent" into a
    sentence whose "them" was the warning count, so this exact input read as
    though the missing transit key had silenced the speaker.
    """

    exit_code, line = _summary_line(
        [
            doctor.CheckResult("graph", "fail", "unsafe", speaker_silent=True),
            doctor.CheckResult("transit", "warn", "no MTA key"),
        ],
        capsys,
    )

    assert exit_code == 1
    assert line.endswith(
        "the speaker is silent — 1 failed, 1 warning(s).\x1b[0m"
    )
    assert "of them" not in line


def test_an_ok_result_claiming_silence_is_ignored_rather_than_believed(capsys):
    """`CheckResult` says the flag is read on warn/fail only — pin that.

    A check that returned `ok` while asserting silence is contradicting
    itself, and the summary must not repeat the contradiction. Previously this
    was an argument in a comment with no test behind it.

    The `ok` result is paired with a COSMETIC WARN on purpose. Alone it proves
    nothing: with no warn and no fail the composer takes the all-passed branch,
    which never reads the flag, so a version that counted `ok` results would
    pass anyway — measured, by mutating the scoping and watching the
    ok-only input survive. Only this pairing reaches the branch that reads it.
    """

    exit_code, line = _summary_line(
        [
            doctor.CheckResult("graph", "ok", "legal", speaker_silent=True),
            doctor.CheckResult("transit", "warn", "no MTA key"),
        ],
        capsys,
    )

    assert exit_code == 0
    assert line.endswith("1 warning(s) — non-critical.\x1b[0m")
    assert "silent" not in line


def test_a_lone_ok_result_claiming_silence_still_reports_all_passed(capsys):
    """The other half of the same contradiction: nothing to warn about."""

    exit_code, line = _summary_line(
        [doctor.CheckResult("graph", "ok", "legal", speaker_silent=True)],
        capsys,
    )

    assert exit_code == 0
    assert line.endswith("all checks passed.\x1b[0m")
    assert "silent" not in line


# ================== the root-fidelity /system/diagnostics capture path ========
#
# jasper-doctor is a root tool and jasper-control is non-root, so running the
# doctor in-process from jasper-control makes ~7 hardware checks fail on
# permissions (false red on the dashboard). The report is produced by the root
# jasper-doctor-json.service oneshot (started via jasper-control's polkit
# manage-units grant), which writes JSON to a group-readable file
# jasper-control serves.

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/jasper-doctor-json.service"


def test_render_json_out_writes_0640_and_returns_zero(tmp_path):
    """`--out` writes the report atomically at 0640 (group-readable, not world)
    and returns 0 — a "report with failures" must not flip the oneshot to
    `failed`, since the failures are inside the JSON."""
    results = [
        CheckResult("ok-check", "ok", "fine"),
        CheckResult("bad-check", "fail", "broken"),
    ]
    out = tmp_path / "result.json"
    rc = render_json(results, out_path=str(out))

    assert rc == 0, "out-path render must return 0 even with failing checks"
    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o640, f"expected 0640 group-readable, got {oct(mode)}"
    import json
    payload = json.loads(out.read_text())
    assert payload["fails"] == 1
    assert {r["name"] for r in payload["results"]} == {"ok-check", "bad-check"}


def test_render_json_stdout_keeps_exit_semantics(capsys):
    """Without --out, the operator CLI contract is unchanged: exit 1 on a fail."""
    assert render_json([CheckResult("x", "fail", "")]) == 1
    assert render_json([CheckResult("x", "warn", "")]) == 0
    assert render_json([CheckResult("x", "ok", "")]) == 0


def test_oneshot_unit_runs_doctor_with_out():
    assert UNIT.is_file(), f"missing {UNIT}"
    text = UNIT.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    # root (no User=) for full fidelity; Group=jasper so the result is readable.
    assert not any(
        ln.strip().startswith("User=") for ln in text.splitlines()
    ), "the doctor oneshot must run as root (no User=) for full-fidelity checks"
    assert "Group=jasper" in text
    assert "jasper-doctor --json --out /run/jasper-control/doctor-result.json" in text
    # On-demand only — never enabled (no [Install] section header).
    assert "[Install]" not in [ln.strip() for ln in text.splitlines()]


def test_oneshot_in_managed_units_and_polkit_allowlist():
    """The non-root jasper-control `systemctl start`s the oneshot — so it must be
    in MANAGED_UNITS (and therefore the polkit allowlist, pinned set-equal by
    test_polkit_jasper_control)."""
    assert "jasper-doctor-json.service" in MANAGED_UNITS


def test_endpoint_uses_cached_oneshot_not_inprocess_doctor():
    """The /system/diagnostics handler must schedule the root oneshot + read the
    cached result file — NOT spawn jasper-doctor in-process (which would run
    non-root and report false failures)."""
    server = (ROOT / "jasper/control/server.py").read_text(encoding="utf-8")
    assert "jasper-doctor-json.service" in server
    assert '"--no-block"' in server
    assert "/run/jasper-control/doctor-result.json" in server
    # The old in-process spawn must be gone.
    assert '"/opt/jasper/.venv/bin/jasper-doctor", "--json"' not in server
