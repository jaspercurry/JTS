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

import pytest


import jasper.mic_presence as mic_presence_module
from jasper.cli import doctor
# `main` and `run_async` resolve these names in their own module's
# namespace, so a patch aimed at the package would not apply.
from jasper.cli.doctor import _cli, _harness, _shared
from jasper.cli.doctor import CheckResult, render_json, renderers
from jasper.cli.doctor import voice as doctor_voice
from jasper.cli.doctor._registry import RegisteredCheck
from jasper.config import Config, VoiceProviderNotConfigured
from jasper.control.restart_broker import MANAGED_UNITS
from jasper.mic_presence import MicPresence


def _reg(func, *, module="env", **kw) -> RegisteredCheck:
    """A registry entry for the harness tests, which stub `registered_checks`."""
    return RegisteredCheck(module=module, func=func, **kw)


def test_json_mode_reports_unhandled_check_exception(monkeypatch, capsys):
    """Machine-readable mode should stay machine-readable even if a
    diagnostic check raises unexpectedly."""
    monkeypatch.setattr(_cli, "_load_env_files", lambda: None)
    monkeypatch.setattr(Config, "from_env", staticmethod(lambda: object()))

    async def boom(_cfg, **_scope):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(_cli, "run_async", boom)
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
    assert payload["results"][0]["reason"] == doctor.REASON_DOCTOR_CRASHED
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
    monkeypatch.setattr(_cli, "_load_env_files", lambda: None)
    monkeypatch.setattr(_cli, "read_install_profile", lambda: "endpoint")
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

    async def fake_run_async(cfg, **_scope):
        assert cfg.usage_db == "/tmp/jasper-endpoint-usage.db"
        return [doctor.CheckResult("endpoint smoke", "ok", "minimal cfg")]

    monkeypatch.setattr(_cli, "run_async", fake_run_async)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--json"])

    try:
        doctor.main()
    except SystemExit as e:
        assert e.code == 0
    else:  # pragma: no cover - defensive, main() should always exit.
        raise AssertionError("main() did not exit")

    payload = json.loads(capsys.readouterr().out)
    assert payload["fails"] == 0
    assert payload["speaker_silent"] is False
    assert [(r["name"], r["status"]) for r in payload["results"]] == [
        ("endpoint smoke", "ok")
    ]


def test_doctor_check_exception_becomes_fail_result():
    def explode():
        raise RuntimeError("synthetic check failure")

    result = _shared._run_doctor_check(("explosive check", explode))

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

    result = _shared._run_doctor_check(("sensitive check", explode))

    assert "super-secret-refresh" not in result.detail
    assert "super-secret-access-token" not in result.detail
    assert "sk-super-secret-openai-key" not in result.detail
    assert "refresh_token=<redacted>" in result.detail
    assert "Bearer <redacted>" in result.detail
    assert result.detail.count("<redacted>") == 3


def test_check_result_redacts_a_credential_left_in_its_own_detail():
    """`CheckResult.__post_init__` is the point every row — a real check's
    own detail, not only a crash's — passes through on its way to
    `jasper-doctor --json` and jasper-control's /system/diagnostics; a check
    that forgot to redact its own detail must not leak past it."""
    result = CheckResult(
        "some check", "warn",
        "found OPENAI_API_KEY=sk-live-abc123 in environment",
        reason="some_reason",
    )

    assert "sk-live-abc123" not in result.detail
    assert "<redacted>" in result.detail


def test_async_doctor_check_exception_becomes_fail_result():
    async def explode():
        raise RuntimeError("synthetic async failure")

    result = asyncio.run(
        _shared._run_async_doctor_check("async check", explode),
    )

    assert result.name == "async check"
    assert result.status == "fail"
    assert "RuntimeError: synthetic async failure" in result.detail


def test_legacy_endpoint_token_doctor_behaves_as_streambox(monkeypatch):
    """A persisted/legacy 'endpoint' token normalizes to streambox, so the
    doctor applies the streambox skip behaviour (wake/brain groups skipped,
    local audio kept)."""
    ran: list[str] = []

    def env_check():
        ran.append("env")
        return doctor.CheckResult("env file", "ok", "ran")

    def wake_check(_cfg):
        ran.append("wake")
        return doctor.CheckResult("wake model", "fail", "should not run")

    def web_check():
        ran.append("web")
        return doctor.CheckResult("management surface", "ok", "ran")

    monkeypatch.setattr(_harness, "read_install_profile", lambda: "endpoint")
    monkeypatch.setattr(
        _harness,
        "registered_checks",
        lambda **_scope: [
            _reg(env_check),
            _reg(wake_check, module="wake", needs_cfg=True, label="wake model"),
            _reg(web_check, module="web"),
        ],
    )

    results = asyncio.run(doctor.run_async(object()))

    assert ran == ["env", "web"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("env file", "ok", "ran"),
        ("wake model", "skipped", "not installed (streambox profile)"),
        ("management surface", "ok", "ran"),
    ]


def test_streambox_doctor_config_does_not_require_voice_provider(monkeypatch):
    monkeypatch.delenv("JASPER_VOICE_PROVIDER", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts4.local")
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS4")

    cfg = _cli._doctor_config_from_env("streambox")

    assert cfg.usage_db
    assert cfg.camilla_host == "127.0.0.1"
    assert cfg.camilla_port == 1234
    assert cfg.spotify_enabled is False
    assert cfg.spotify_device_name == "JTS4"
    assert cfg.spotify_setup_url == "http://jts4.local/spotify"


def test_streambox_doctor_skips_voice_brain_but_keeps_local_audio_checks():
    by_name = {entry.func.__name__: entry for entry in doctor.registered_checks()}

    assert (
        _harness._doctor_skip_detail(by_name["check_provider_key"], "streambox")
        == "not installed (streambox profile)"
    )
    assert (
        _harness._doctor_skip_detail(by_name["check_spend_cap"], "streambox")
        == "not installed (streambox profile)"
    )
    # The other 4 voice checks self-gate instead — see test_doctor_voice.py.
    for name in (
        "check_provider_importable", "check_voice_provider_ids_manifest",
        "check_tool_packs", "check_pricing",
    ):
        assert not _harness._doctor_skip_detail(by_name[name], "streambox"), name
    assert _harness._doctor_skip_detail(
        by_name["check_openwakeword_model"],
        "streambox",
    )
    assert _harness._doctor_skip_detail(
        by_name["check_aec_bridge_running"],
        "streambox",
    )
    assert _harness._doctor_skip_detail(by_name["check_mic_capture"], "streambox")
    assert _harness._doctor_skip_detail(by_name["check_tts_open"], "streambox")
    assert _harness._doctor_skip_detail(
        by_name["check_crossover_v2_cloud_pipeline"],
        "streambox",
    )
    assert _harness._doctor_skip_detail(
        by_name["check_crossover_v2_applied_is_graded"],
        "streambox",
    )
    assert not _harness._doctor_skip_detail(
        by_name["check_camilla_websocket"],
        "streambox",
    )
    assert not _harness._doctor_skip_detail(
        by_name["check_librespot_running"],
        "streambox",
    )
    assert not _harness._doctor_skip_detail(
        by_name["check_correction_web_service"],
        "streambox",
    )
    assert not _harness._doctor_skip_detail(
        by_name["check_correction_cert_hostname"],
        "streambox",
    ), "the correction module's cert-SAN check still applies on streambox"


def test_streambox_profile_doctor_keeps_local_audio_groups(monkeypatch):
    """`check_provider_key` is named individually in
    STREAMBOX_OMITTED_DOCTOR_CHECKS, not by its module — see that set's
    comment in _registry.py."""
    ran: list[str] = []

    def check_provider_key(_cfg):
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

    def check_crossover_v2_cloud_pipeline():
        ran.append("crossover_v2")
        return doctor.CheckResult(
            "crossover v2 cloud pipeline", "fail", "should not run",
        )

    monkeypatch.setattr(_harness, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(
        _harness,
        "registered_checks",
        lambda **_scope: [
            _reg(check_provider_key, module="voice", needs_cfg=True, label="provider key"),
            _reg(
                check_mic_capture,
                module="audio",
                needs_cfg=True,
                label="mic capture",
            ),
            _reg(
                renderer_check,
                module="renderers",
                needs_cfg=True,
                label="librespot.service",
            ),
            _reg(correction_check, module="correction"),
            _reg(check_crossover_v2_cloud_pipeline, module="correction"),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert ran == ["renderers", "correction"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("provider key", "skipped", "not installed (streambox profile)"),
        ("mic capture", "skipped", "not installed (streambox profile)"),
        ("librespot.service", "ok", "ran"),
        ("room correction service", "ok", "ran"),
        ("crossover v2 cloud pipeline", "skipped", "not installed (streambox profile)"),
    ]
    crossover_result = next(
        r for r in results if r.name == "crossover v2 cloud pipeline"
    )
    assert crossover_result.reason == _harness.REASON_NOT_INSTALLED


def test_run_async_parallelizes_blocking_checks_but_preserves_order(
    monkeypatch,
):
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

    monkeypatch.setattr(_harness, "read_install_profile", lambda: "full")
    monkeypatch.setattr(
        _harness,
        "registered_checks",
        lambda **_scope: [
            _reg(make_check(name, 0.15)) for name in "abcdef"
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace(), max_concurrency=3))

    assert [r.name for r in results] == ["a", "b", "c", "d", "e", "f"]
    assert max_active == 3


def test_run_async_serializes_checks_in_same_exclusive_group(monkeypatch):
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

    monkeypatch.setattr(_harness, "read_install_profile", lambda: "full")
    monkeypatch.setattr(
        _harness,
        "registered_checks",
        lambda **_scope: [
            _reg(exclusive("a"), exclusive_group="audio-probe"),
            _reg(exclusive("b"), exclusive_group="audio-probe"),
            _reg(ordinary),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace(), max_concurrency=3))

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
            doctor.CheckResult("transit", "warn", "no MTA key", reason="mta_key_missing"),
            doctor.CheckResult(
                "active speaker runtime graph",
                "warn",
                "parked silent",
                speaker_silent=True,
                reason="graph_parked",
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
        [doctor.CheckResult("transit", "warn", "no MTA key", reason="mta_key_missing")],
        capsys,
    )

    assert exit_code == 0
    assert line.endswith("1 warning(s) — non-critical.\x1b[0m")
    assert "silent" not in line


def test_summary_names_the_silence_alongside_a_failure(capsys):
    """A fail does not excuse burying it: "1 failed" says nothing about sound."""

    exit_code, line = _summary_line(
        [
            doctor.CheckResult("nginx", "fail", "down", reason="nginx_down"),
            doctor.CheckResult(
                "blockers", "warn", "parked",
                speaker_silent=True, reason="blockers_parked",
            ),
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
        [
            doctor.CheckResult(
                "graph", "fail", "unsafe",
                speaker_silent=True, reason="graph_unsafe",
            )
        ],
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
            doctor.CheckResult(
                "graph", "fail", "unsafe",
                speaker_silent=True, reason="graph_unsafe",
            ),
            doctor.CheckResult("transit", "warn", "no MTA key", reason="mta_key_missing"),
        ],
        capsys,
    )

    assert exit_code == 1
    assert line.endswith(
        "the speaker is silent — 1 failed, 1 warning(s).\x1b[0m"
    )
    assert "of them" not in line


def test_an_ok_result_cannot_claim_the_speaker_is_silent():
    """`speaker_silent` is warn/fail-only, enforced at construction.

    A check returning `ok` while asserting silence contradicts itself. The
    contract rejects the row rather than leaving every consumer to re-filter
    on status (ADR-0233 rule 3)."""
    with pytest.raises(ValueError):
        CheckResult("graph", "ok", "legal", speaker_silent=True)
    with pytest.raises(ValueError):
        CheckResult("graph", "skipped", "not applicable", speaker_silent=True)


def test_a_warn_or_fail_without_a_reason_is_rejected():
    """Every warn/fail carries a machine-stable reason (ADR-0233 rule 3)."""
    with pytest.raises(ValueError):
        CheckResult("graph", "warn", "something")
    with pytest.raises(ValueError):
        CheckResult("graph", "fail", "something")
    # ok/skipped rows may omit it.
    assert CheckResult("graph", "ok", "fine").reason == ""
    assert CheckResult("graph", "skipped", "n/a").reason == ""


def test_an_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        CheckResult("graph", "green", "fine")


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
        CheckResult("bad-check", "fail", "broken", reason="thing_broken"),
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
    assert render_json([CheckResult("x", "fail", "", reason="broke")]) == 1
    assert render_json([CheckResult("x", "warn", "", reason="iffy")]) == 0
    assert render_json([CheckResult("x", "ok", "")]) == 0


def test_oneshot_unit_runs_doctor_with_out():
    assert UNIT.is_file(), f"missing {UNIT}"
    lines = [ln.strip() for ln in UNIT.read_text(encoding="utf-8").splitlines()]
    settings: dict[str, str] = {}
    for line in lines:
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        settings[key] = value
    assert settings["Type"] == "oneshot"
    # root (no User=) for full fidelity; Group=jasper so the result is readable.
    assert "User" not in settings, (
        "the doctor oneshot must run as root (no User=) for full-fidelity checks"
    )
    assert settings["Group"] == "jasper"
    # The writer's --out and the reader's default are one path: rename one
    # only and /system/diagnostics stats a file nothing writes.
    from jasper.control.server import _DIAGNOSTICS_RESULT_PATH

    assert settings["ExecStart"].endswith(
        f"jasper-doctor --json --out {_DIAGNOSTICS_RESULT_PATH}"
    )
    # Bounded so a wedged probe cannot leave the oneshot activating forever,
    # and capped so a runaway one cannot join a global OOM cascade.
    assert int(settings["TimeoutStartSec"]) > 0
    assert settings["MemoryMax"].endswith("M")
    # On-demand only — never enabled (no [Install] section header).
    assert "[Install]" not in lines


def test_oneshot_in_managed_units_and_polkit_allowlist():
    """The non-root jasper-control `systemctl start`s the oneshot — so it must be
    in MANAGED_UNITS (and therefore the polkit allowlist, pinned set-equal by
    test_polkit_jasper_control)."""
    assert "jasper-doctor-json.service" in MANAGED_UNITS



def _cfg_attrs_read_by(func) -> set[str]:
    """Every ``cfg.<attr>`` the check's source reads."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "cfg"
    }


def _main_json(monkeypatch, capsys, argv, *, from_env, run_async):
    """Drive `main()` down the full-profile JSON path; `(exit code, payload)`."""
    monkeypatch.setattr(_cli, "_load_env_files", lambda: None)
    monkeypatch.setattr(_cli, "read_install_profile", lambda: "full")
    monkeypatch.setattr(Config, "from_env", staticmethod(from_env))
    monkeypatch.setattr(_cli, "run_async", run_async)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", *argv, "--json"])

    with pytest.raises(SystemExit) as exit_info:
        doctor.main()

    return exit_info.value.code, json.loads(capsys.readouterr().out)


async def _one_core_row(_cfg, **_scope):
    return [doctor.CheckResult("required units active", "ok", "ran")]


@pytest.mark.parametrize("argv, core_only", [([], False), (["--core"], True)])
def test_core_flag_reaches_the_harness_and_keeps_json(
    monkeypatch, capsys, argv, core_only,
):
    """`--core` is a scope, not a second code path: the same harness call, the
    same JSON report, one flag through."""
    seen: dict[str, object] = {}

    async def fake_run_async(cfg, **scope):
        seen.update(scope)
        return await _one_core_row(cfg, **scope)

    code, payload = _main_json(
        monkeypatch, capsys, argv,
        from_env=lambda: SimpleNamespace(), run_async=fake_run_async,
    )

    assert code == 0
    assert seen["core_only"] is core_only
    assert payload["fails"] == 0
    assert [row["name"] for row in payload["results"]] == ["required units active"]


def test_core_scope_does_not_build_the_voice_config(monkeypatch, capsys):
    """A full box that has not been through /voice/ has no provider, so
    `Config.from_env()` raises — and every --core check is `needs_cfg=False`,
    so the deploy subset must never build it (ADR-0233 rule 5)."""

    def unconfigured():
        raise VoiceProviderNotConfigured("no provider")

    code, payload = _main_json(
        monkeypatch, capsys, ["--core"],
        from_env=unconfigured, run_async=_one_core_row,
    )

    assert code == 0
    assert [row["name"] for row in payload["results"]] == [
        "required units active"
    ]
    assert _shared.REASON_CONFIG_ERROR not in {
        row["reason"] for row in payload["results"]
    }


def test_only_flag_reaches_the_harness(monkeypatch, capsys):
    """`--only` threads the module name into the harness the same way
    `--core` threads a bool, and the run it produces is exactly the
    registry's own filtered slice — not a display-side narrowing."""

    async def fake_run_async(cfg, **scope):
        return [
            CheckResult(_harness._registered_check_name(entry), "ok", "ran")
            for entry in doctor.registered_checks(only=scope.get("only"))
        ]

    code, payload = _main_json(
        monkeypatch, capsys, ["--only", "renderers"],
        from_env=lambda: SimpleNamespace(), run_async=fake_run_async,
    )

    assert code == 0
    expected = {
        _harness._registered_check_name(entry)
        for entry in doctor.registered_checks(only="renderers")
    }
    assert expected and {row["name"] for row in payload["results"]} == expected


def test_only_rejects_a_module_the_roster_does_not_name(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--only", "bogus"])

    with pytest.raises(SystemExit) as exit_info:
        doctor.main()

    assert exit_info.value.code != 0


def test_only_outside_core_is_rejected_before_running(monkeypatch):
    """`network` is a real module but not in CORE_MODULES: --only network
    --core must not silently run zero checks and report success (#4253)."""
    monkeypatch.setattr(
        sys, "argv", ["jasper-doctor", "--only", "network", "--core"],
    )

    with pytest.raises(SystemExit) as exit_info:
        doctor.main()

    assert exit_info.value.code != 0


def test_only_voice_on_streambox_yields_skipped_rows_not_an_empty_run(
    monkeypatch,
):
    """voice is a real, valid module — --only voice must still produce
    voice's rows on a streambox with no accessory paired, every one
    `skipped`, never a silent zero-row run. Reason codes differ per check;
    which gets which is pinned in test_doctor_voice.py and
    test_streambox_doctor_skips_voice_brain_but_keeps_local_audio_checks."""
    # run_async() itself calls evidence.reset(), so a pre-seeded memo will
    # not survive to when the checks run — patch the underlying readers.
    monkeypatch.setattr(_harness, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(_shared, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(
        mic_presence_module, "read_mic_presence",
        lambda *a, **kw: MicPresence(present=False),
    )
    cfg = _cli._doctor_config_from_env("streambox")

    results = asyncio.run(doctor.run_async(cfg, only="voice"))

    assert results
    assert all(r.status == "skipped" for r in results)
    assert {r.reason for r in results} == {
        _harness.REASON_NOT_INSTALLED,
        doctor_voice.REASON_VOICE_UNIT_NOT_FULL_PROFILE,
        doctor_voice.REASON_MANIFEST_NOT_RENDERED,
    }


def test_failing_flag_keeps_the_full_run_summary(capsys):
    """`--failing` narrows only the printed rows: the summary counts (here,
    the deploy.health event's) must still reflect the unfiltered run."""
    results = [
        CheckResult("a", "ok", "up"),
        CheckResult("b", "warn", "flaky", reason="b_flaky"),
        CheckResult("c", "fail", "down", reason="c_down"),
    ]

    code = doctor.render(list(results), core=True, failing=True)
    [health_line] = [
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("event=deploy.health ")
    ]

    assert code == 1
    assert dict(kv.split("=", 1) for kv in health_line.split()[1:]) == {
        "status": "fail", "fail": "1", "warn": "1", "rows": "3",
        "speaker_silent": "false",
    }


def test_failing_flag_hides_skipped_rows(capsys):
    """A streambox's skipped rows are dim, not red: --failing must not
    resurrect them as if they were a problem (#4253)."""
    results = [
        CheckResult("alpha", "ok", "up"),
        CheckResult("bravo", "skipped", "not installed", reason="not_installed"),
        CheckResult("charlie", "fail", "down", reason="c_down"),
    ]

    doctor.render(list(results), failing=True)
    out = capsys.readouterr().out

    assert "bravo" not in out
    assert "charlie" in out


@pytest.mark.parametrize(
    "results, core, fields",
    [
        (
            [CheckResult("a", "ok", "up")],
            True,
            {
                "status": "ok", "fail": "0", "warn": "0", "rows": "1",
                "speaker_silent": "false",
            },
        ),
        (
            [
                CheckResult("a", "fail", "down", reason="a_down"),
                CheckResult(
                    "b", "warn", "parked",
                    speaker_silent=True, reason="b_parked",
                ),
            ],
            True,
            {
                "status": "fail", "fail": "1", "warn": "1", "rows": "2",
                "speaker_silent": "true",
            },
        ),
        ([CheckResult("a", "ok", "up")], False, None),
    ],
    ids=["core-green", "core-one-fail", "full-doctor-stays-quiet"],
)
def test_core_text_mode_emits_one_deploy_health_event(
    capsys, results, core, fields,
):
    """The deploy gates on the exit code; the journal gets this line. Only
    --core emits it, so the dashboard's full report is unchanged."""
    doctor.render(results, core=core)

    lines = [
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("event=deploy.health ")
    ]
    if fields is None:
        assert lines == []
        return
    assert len(lines) == 1
    assert dict(kv.split("=", 1) for kv in lines[0].split()[1:]) == fields


@pytest.mark.parametrize(
    "entry",
    [
        entry
        for entry in doctor.registered_checks()
        if entry.needs_cfg and not _harness._doctor_skip_detail(entry, "streambox")
    ],
    ids=lambda entry: entry.func.__name__,
)
def test_streambox_cfg_carries_every_attribute_its_checks_read(entry):
    """The streambox cfg stub is a contract, not a convenience: a check that
    survives the profile filter must find every attribute it reads, or it
    crashes and the dashboard shows a red row on a healthy box."""
    cfg = _cli._doctor_config_from_env("streambox")
    missing = {
        attr for attr in _cfg_attrs_read_by(entry.func) if not hasattr(cfg, attr)
    }
    assert not missing


def test_librespot_check_reports_ok_on_streambox_cfg(monkeypatch):
    monkeypatch.setattr(renderers, "_parked_follower_result", lambda _label: None)
    monkeypatch.setattr(renderers, "source_intent_enabled", lambda source: True)
    monkeypatch.setattr(renderers.os.path, "isfile", lambda p: True)
    renderers.evidence.seed(
        "units", {"librespot.service": {"active_state": "active"}},
    )

    result = renderers.check_librespot_running(
        _cli._doctor_config_from_env("streambox")
    )

    assert result.status == "ok"
