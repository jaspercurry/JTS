# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor web domain.

Verdicts plus the remediation targets a reader acts on (the missing path,
the exposed address, the guard's error code). Detail prose is not pinned.
"""
from __future__ import annotations

import io
import re
import subprocess
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from jasper import tool_catalog_view
from jasper.cli import doctor
from jasper.cli.doctor import web as doctor_web
from jasper.control import control_token
from jasper.conversation_history import (
    CAPTURE_ENABLED_ENV,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    make_turn_id,
)
from jasper.voice import provider_state

from .doctor_test_support import _registered_check_names

ROOT = Path(__file__).resolve().parents[1]

# ------------------------------------------------------- web design assets


def _assets(tmp_path: Path, *, manifest=None, app_css=True, present=()) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    if app_css:
        (assets / "app.css").write_text("/* css */")
    if manifest is not None:
        (assets / ".install-manifest").write_text("\n".join(manifest) + "\n")
    for rel in present:
        target = assets / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// asset")
    return assets


@pytest.mark.parametrize(
    "manifest, app_css, present, status, reason",
    [
        # No manifest: the tree is unverifiable, so warn rather than pass a
        # partial deploy from a stale built-in list.
        (None, True, (), "warn", doctor_web.REASON_WEB_ASSETS_MANIFEST_MISSING),
        (
            ["wifi/wifi.css", "wifi/js/main.js", "shared/js/escape.js"],
            True,
            ("wifi/wifi.css", "wifi/js/main.js", "shared/js/escape.js"),
            "ok",
            doctor_web.REASON_WEB_ASSETS_VERIFIED,
        ),
        (
            ["wake/js/main.js", "wake/wake.css"],
            True,
            ("wake/wake.css",),
            "warn",
            doctor_web.REASON_WEB_ASSETS_MISSING,
        ),
        # Blank/comment/absolute/traversal manifest rows are dropped, so only
        # app.css plus the one sane entry are counted.
        (
            ["", "# comment", "/etc/passwd", "a/../../escape", "voice/js/main.js"],
            True,
            ("voice/js/main.js",),
            "ok",
            doctor_web.REASON_WEB_ASSETS_VERIFIED,
        ),
        (
            ["voice/js/main.js"], False, ("voice/js/main.js",), "warn",
            doctor_web.REASON_WEB_ASSETS_MISSING,
        ),
    ],
    ids=["no-manifest", "all-present", "entry-missing", "malformed-rows", "no-app-css"],
)
def test_web_design_assets_verdicts(
    monkeypatch, tmp_path: Path, manifest, app_css, present, status, reason
):
    _assets(tmp_path, manifest=manifest, app_css=app_css, present=present)
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))

    r = doctor.check_web_design_assets()

    assert r.status == status
    assert r.reason == reason


def test_web_design_assets_caps_the_missing_list(monkeypatch, tmp_path: Path):
    """A wiped asset tree warns with a bounded list, not journal spam."""
    _assets(tmp_path, manifest=[f"page{i}/js/main.js" for i in range(20)])
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))

    r = doctor.check_web_design_assets()

    assert r.status == "warn"
    assert "(+8 more)" in r.detail
    assert r.detail.count("js/main.js") == 12


def test_web_design_assets_skips_when_not_installed(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path / "nope"))

    r = doctor.check_web_design_assets()
    assert r.status == "skipped"
    assert r.reason == doctor_web.REASON_WEB_ASSETS_NOT_INSTALLED


# ------------------------------------------------- CamillaGUI socket bind
#
# camillagui.socket used to bind 0.0.0.0:5005 — an unauthenticated,
# root-backed listener reachable from any LAN device (#2319). The unit file
# itself is pinned by tests/test_camillagui_systemd.py; these read the LIVE
# kernel bind via a mocked `ss` so a botched restart is caught, not masked.


def _fake_ss(*lines: str, returncode: int = 0):
    stdout = "\n".join(lines) + "\n" if lines else ""

    def run(cmd, timeout=5.0):
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=""
        )

    return run


@pytest.mark.parametrize(
    "rows, status, reason",
    [
        (
            ("LISTEN 0 128   127.0.0.1:5005   0.0.0.0:*",), "ok",
            doctor_web.REASON_CAMILLAGUI_LOOPBACK_ONLY,
        ),
        (
            ("LISTEN 0 128     0.0.0.0:5005   0.0.0.0:*",), "warn",
            doctor_web.REASON_CAMILLAGUI_EXPOSED,
        ),
        # A loopback row alongside a wildcard one (mid-restart) still warns:
        # "inspect only the first row" would pass this.
        (
            (
                "LISTEN 0 128   127.0.0.1:5005   0.0.0.0:*",
                "LISTEN 0 128     0.0.0.0:5005   0.0.0.0:*",
            ),
            "warn",
            doctor_web.REASON_CAMILLAGUI_EXPOSED,
        ),
        (
            ("LISTEN 0 128        [::1]:5005      [::]:*",), "ok",
            doctor_web.REASON_CAMILLAGUI_LOOPBACK_ONLY,
        ),
        (
            ("LISTEN 0 128         [::]:5005      [::]:*",), "warn",
            doctor_web.REASON_CAMILLAGUI_EXPOSED,
        ),
        # Not listening at all — never installed, or administratively stopped.
        # Neither is a live exposure.
        ((), "ok", doctor_web.REASON_CAMILLAGUI_NOT_LISTENING),
        # Non-loopback listeners on OTHER ports must not trip the check.
        (
            (
                "LISTEN 0 128   127.0.0.1:5006   0.0.0.0:*",
                "LISTEN 0 128     0.0.0.0:8780   0.0.0.0:*",
            ),
            "ok",
            doctor_web.REASON_CAMILLAGUI_NOT_LISTENING,
        ),
        # Malformed/short rows and non-LISTEN states must not crash the parser.
        (
            ("", "garbage", "ESTAB 0 0   127.0.0.1:5005   127.0.0.1:9"), "ok",
            doctor_web.REASON_CAMILLAGUI_NOT_LISTENING,
        ),
    ],
    ids=[
        "loopback",
        "wildcard",
        "both",
        "ipv6-loopback",
        "ipv6-wildcard",
        "silent",
        "other-ports",
        "malformed",
    ],
)
def test_camillagui_loopback_verdicts(monkeypatch, rows, status, reason):
    monkeypatch.setattr(doctor_web, "_run", _fake_ss(*rows))

    r = doctor_web.check_camillagui_loopback()

    assert r.status == status
    # The reason discriminates the two `ok` branches (genuinely loopback-only
    # vs. nothing listening) from one another, same as it discriminated on
    # bind address before the reason vocabulary existed.
    assert r.reason == reason


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("ss not found"),
        # PermissionError stands in for every plain OSError the narrower
        # (SubprocessError, FileNotFoundError) clause would miss — including
        # a fork failure (ENOMEM) under memory pressure.
        PermissionError("ss not executable"),
    ],
    ids=["absent", "oserror"],
)
def test_camillagui_loopback_degrades_when_ss_unusable(monkeypatch, failure):
    def raises(cmd, timeout=5.0):
        raise failure

    monkeypatch.setattr(doctor_web, "_run", raises)

    assert doctor_web.check_camillagui_loopback().status == "warn"


def test_camillagui_loopback_warns_when_ss_exits_nonzero(monkeypatch):
    monkeypatch.setattr(doctor_web, "_run", _fake_ss(returncode=1))

    assert doctor_web.check_camillagui_loopback().status == "warn"


# --------------------------------------------------------- control token


@pytest.mark.parametrize("present", [False, True], ids=["disabled", "enabled"])
def test_control_token_posture_is_ok_and_never_echoes_the_secret(
    monkeypatch, tmp_path, present
):
    secret = "super-secret-token-value-xyz"
    path = tmp_path / "control_token"
    if present:
        path.write_text(secret + "\n")
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))

    r = doctor_web.check_control_token()

    assert r.status == "ok"
    assert r.reason == (
        doctor_web.REASON_CONTROL_TOKEN_ENABLED if present
        else doctor_web.REASON_CONTROL_TOKEN_DISABLED
    )
    assert secret not in r.detail
    assert secret not in r.name


# ----------------------------------------------------------- tool catalog


@pytest.mark.parametrize(
    "provider, summary, status, reason",
    [
        ("", None, "skipped", doctor_web.REASON_TOOL_CATALOG_NOT_CONFIGURED),
        (
            "gemini",
            {
                "catalog_present": False,
                "count": 0,
                "disabled": [],
                "disabled_count": 0,
                "pending": False,
            },
            "warn",
            doctor_web.REASON_TOOL_CATALOG_ABSENT,
        ),
        (
            "gemini",
            {
                "catalog_present": True,
                "count": 28,
                "disabled": ["get_weather"],
                "disabled_count": 1,
                "pending": True,
            },
            "ok",
            doctor_web.REASON_TOOL_CATALOG_PRESENT,
        ),
    ],
    ids=["no-provider", "catalog-absent", "catalog-present"],
)
def test_tool_catalog_verdicts(monkeypatch, provider, summary, status, reason):
    monkeypatch.setattr(provider_state, "read_active_provider", lambda: provider)
    if summary is not None:
        monkeypatch.setattr(tool_catalog_view, "summary", lambda: summary)

    r = doctor_web.check_tool_catalog()
    assert r.status == status
    assert r.reason == reason


# ---------------------------------------------------- management surface


def _install_nginx_site(monkeypatch, tmp_path):
    site = tmp_path / "jasper.conf"
    site.write_text("# nginx site\n")
    monkeypatch.setattr(doctor_web, "NGINX_SITE", site)


@contextmanager
def _urlopen_returns(status: int, body: bytes):
    class _Resp:
        def __init__(self):
            self.status = status

        def read(self, n=-1):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()) as m:
        yield m


def test_management_surface_skips_when_nginx_site_not_installed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(doctor_web, "NGINX_SITE", tmp_path / "absent.conf")

    r = doctor_web.check_management_surface()
    assert r.status == "skipped"
    assert r.reason == doctor_web.REASON_MANAGEMENT_NOT_INSTALLED


def test_management_surface_probes_as_the_speaker_hostname(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")

    with _urlopen_returns(200, b"{}") as m:
        r = doctor_web.check_management_surface()

    assert r.status == "ok"
    # Carrying the speaker hostname as Host is the whole point of the check.
    assert m.call_args[0][0].get_header("Host") == "jts3.local"


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(
            doctor_web.MANAGEMENT_PROBE_URL,
            403,
            "Forbidden",
            None,
            io.BytesIO(b'{"error": "host_not_allowed"}'),
        ),
        urllib.error.HTTPError(
            doctor_web.MANAGEMENT_PROBE_URL,
            502,
            "Bad Gateway",
            None,
            io.BytesIO(b'{"error": "jasper-control unreachable: ..."}'),
        ),
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    ],
    ids=["host-guard", "upstream-502", "nginx-down"],
)
def test_management_surface_reports_every_upstream_break_as_fail(
    monkeypatch, tmp_path, failure
):
    _install_nginx_site(monkeypatch, tmp_path)

    with patch("urllib.request.urlopen", side_effect=failure):
        r = doctor_web.check_management_surface()

    assert r.status == "fail"
    expected_reason = (
        doctor_web.REASON_MANAGEMENT_NO_ANSWER
        if isinstance(failure, urllib.error.URLError)
        and not isinstance(failure, urllib.error.HTTPError)
        else doctor_web.REASON_MANAGEMENT_HTTP_ERROR
    )
    assert r.reason == expected_reason


# ------------------------------------------------------ conversation history


def _history_settings(monkeypatch, tmp_path, *, enabled: str, db_path=None) -> Path:
    settings = tmp_path / "conversation_history.env"
    body = f"{CAPTURE_ENABLED_ENV}={enabled}\n"
    if db_path is not None:
        body += f"{DB_PATH_ENV}={db_path}\n"
    settings.write_text(body, encoding="utf-8")
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings))
    return settings


def test_conversation_history_is_ok_when_capture_is_intentionally_off(
    monkeypatch, tmp_path,
):
    """Capture off is the operator's own setting and it WAS observed, so the
    row is `ok` carrying the fact — not a skip (ADR-0228 rule 3)."""
    _history_settings(monkeypatch, tmp_path, enabled="0")

    r = doctor_web.check_conversation_history()
    assert r.status == "ok"
    assert r.reason == doctor_web.REASON_HISTORY_DISABLED


def test_conversation_history_warns_when_enabled_db_missing(monkeypatch, tmp_path):
    db_path = tmp_path / "missing.db"
    _history_settings(monkeypatch, tmp_path, enabled="1", db_path=db_path)

    r = doctor_web.check_conversation_history()

    assert r.status == "warn"
    assert r.reason == doctor_web.REASON_HISTORY_STORE_UNAVAILABLE


def test_conversation_history_ok_with_existing_db(monkeypatch, tmp_path):
    db_path = tmp_path / "conversation_history.db"
    _history_settings(monkeypatch, tmp_path, enabled="1", db_path=db_path)
    store = ConversationStore(str(db_path))
    store.add(
        ConversationTurn(
            id=make_turn_id("2026-06-19T20:15:00Z", 1),
            ts_utc="2026-06-19T20:15:00Z",
            provider="gemini",
            user_text="hello",
            assistant_text="hi",
            tool_calls_json=None,
            data_json=None,
            session_id=1,
        ),
    )
    store.close()

    r = doctor_web.check_conversation_history()

    assert r.status == "ok"


# ------------------------------------------- wizard socket start limits
#
# #2134/#2216 established the shape on jasper-correction-web; #2465 is the
# rest of the family, which had no doctor coverage at all — a start-limited
# wizard hung every socket connection while doctor read clean.
#
# `systemctl show` bodies below are the states MEASURED on lab Pi jts4
# (systemd 257 / Debian Trixie) by the PR #2216 adversarial gate, driving
# transient units that mirror deploy/jasper-correction-web.{service,socket} —
# not hand-written states chosen to match an assumption, which is precisely
# how the first revision of this check shipped blind to its own failure mode.

def _installer_wizard_units() -> set[str]:
    text = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^WIZARD_UNITS=\(\n(.*?)^\)", text, re.M | re.S)
    assert match, "WIZARD_UNITS array not found in systemd-units.sh"
    return {line.strip() for line in match.group(1).splitlines() if line.strip()}


# The family these tests demand coverage of, read from the installer rather
# than from the constant under test: parametrizing over doctor_web.WIZARD_UNITS
# would make narrowing that tuple DESELECT the cases instead of failing them,
# so the sweep could be reverted to one wizard with every per-unit case still
# green.
_WIZARDS = sorted(_installer_wizard_units())

_BOUND = "ActiveState=active\nResult=success\n"
_START_LIMITED = "ActiveState=failed\nResult=service-start-limit-hit\n"
# `systemctl show` on a unit that does not exist answers rc=0 with these.
_NOT_INSTALLED = "ActiveState=inactive\nResult=success\n"


def _socket_states(overrides=None, *, returncode=0, stderr=""):
    """Fake ``_run`` serving a ``systemctl show`` body per wizard socket.

    Every wizard socket is modelled, so a check that queries something
    unmodelled fails loudly here instead of silently reading "". Queries are
    asserted to be socket-side: reading the SERVICE is the #2216 blocker-1
    regression (measured, the service enters ``ActiveState=failed`` 34 times
    in 20 seconds of ordinary retrying while the socket never leaves
    ``active``), so a revision that reaches for it fails here.
    """
    states = {f"{unit}.socket": _BOUND for unit in _WIZARDS}
    for unit, body in (overrides or {}).items():
        states[f"{unit}.socket"] = body

    def fake_run(cmd, timeout=5.0):
        assert cmd[:2] == ["systemctl", "show"], cmd
        unit = cmd[2]
        assert unit.endswith(".socket"), f"check read the service: {unit}"
        assert unit in states, f"check queried an unmodelled unit: {unit}"
        return subprocess.CompletedProcess(
            cmd, returncode, stdout=states[unit], stderr=stderr,
        )

    return fake_run


@pytest.mark.parametrize("unit", _WIZARDS)
def test_start_limited_wizard_socket_fails_with_its_own_remedy(monkeypatch, unit):
    """Each wizard's wedged socket is named, with the command that clears it.

    Parametrized from the installer's family, so narrowing the sweep back to
    jasper-correction-web FAILS the other four rather than deselecting them.
    """
    monkeypatch.setattr(
        doctor_web, "_run", _socket_states({unit: _START_LIMITED}),
    )

    r = doctor_web.check_wizard_socket_start_limits()

    assert r.status == "fail"
    assert r.reason == doctor_web.REASON_WIZARD_SOCKET_FINDING


@pytest.mark.parametrize("unit", _WIZARDS)
def test_absent_wizard_unit_is_ok(monkeypatch, unit):
    """A wizard the profile does not install is not a finding.

    A streambox installs no jasper-chat-web, and takes its jasper-web pair
    from deploy/jasper-web-streambox.* under the jasper-web name.
    """
    monkeypatch.setattr(
        doctor_web, "_run", _socket_states({unit: _NOT_INSTALLED}),
    )

    r = doctor_web.check_wizard_socket_start_limits()
    assert r.status == "ok"


@pytest.mark.parametrize(
    "body, why",
    [
        (_BOUND, "listening, service idle-exited between sessions"),
        ("ActiveState=activating\nResult=success\n", "socket still binding"),
        (_NOT_INSTALLED, "not installed on this profile"),
    ],
)
def test_healthy_wizard_sockets_are_ok(monkeypatch, body, why):
    monkeypatch.setattr(
        doctor_web, "_run", _socket_states({unit: body for unit in _WIZARDS}),
    )

    r = doctor_web.check_wizard_socket_start_limits()

    assert r.status == "ok", why


def test_any_failed_wizard_socket_fails_not_just_the_start_limit_marker(
    monkeypatch,
):
    """``Result`` is reported, never gated on.

    #2216 nit 2: narrowing the predicate from ``ActiveState == "failed"`` to
    one literal ``Result`` string is what created the original blind spot. A
    socket killed by ``trigger-limit-hit`` is just as unbound.
    """
    monkeypatch.setattr(
        doctor_web, "_run",
        _socket_states({
            "jasper-web": "ActiveState=failed\nResult=trigger-limit-hit\n",
        }),
    )

    r = doctor_web.check_wizard_socket_start_limits()

    assert r.status == "fail"
    assert r.reason == doctor_web.REASON_WIZARD_SOCKET_FINDING


@pytest.mark.parametrize(
    "body, returncode, stderr",
    [
        # #2216 should-fix 2: an unreadable probe must not render as healthy.
        ("", 1, "Failed to connect to bus"),
        # A non-zero systemctl is a failed read even when its body parses —
        # otherwise the rc clause is dead weight behind the empty-state one.
        (_BOUND, 1, "Failed to get properties: Connection timed out"),
        # rc=0 with a body carrying no ActiveState is still a failed read.
        ("Failed to get properties: Access denied\n", 0, ""),
    ],
    ids=["bus-down", "nonzero-yet-parses", "unparseable"],
)
def test_unreadable_wizard_socket_probe_fails(
    monkeypatch, body, returncode, stderr,
):
    monkeypatch.setattr(
        doctor_web, "_run",
        _socket_states(
            {unit: body for unit in _WIZARDS},
            returncode=returncode, stderr=stderr,
        ),
    )

    r = doctor_web.check_wizard_socket_start_limits()

    assert r.status == "fail"
    assert r.reason == doctor_web.REASON_WIZARD_SOCKET_FINDING


def test_one_timed_out_wizard_probe_still_reads_the_rest(monkeypatch):
    """A wedged D-Bus hangs these reads — the sweep must survive the first one.

    Uncaught, ``TimeoutExpired`` aborts into the harness's generic crashed-check
    result (empty ``reason``) and loses every per-unit finding, on exactly the
    failure this check exists to diagnose — so pinning ``REASON_WIZARD_SOCKET_
    FINDING`` here (not just ``status == "fail"``) is what proves the sweep
    survived rather than merely crashed.
    """
    hangs, wedged = _WIZARDS[0], _WIZARDS[1]
    serve = _socket_states({wedged: _START_LIMITED})

    def fake_run(cmd, timeout=5.0):
        if cmd[2] == f"{hangs}.socket":
            raise subprocess.TimeoutExpired(cmd, 5.0)
        return serve(cmd, timeout=timeout)

    monkeypatch.setattr(doctor_web, "_run", fake_run)

    r = doctor_web.check_wizard_socket_start_limits()

    assert r.status == "fail"
    assert r.reason == doctor_web.REASON_WIZARD_SOCKET_FINDING


def test_wizard_socket_start_limits_skips_without_systemctl(monkeypatch):
    """Dev host: no systemd to be unhealthy. Matches every sibling's wording."""

    def raises(cmd, timeout=5.0):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr(doctor_web, "_run", raises)

    r = doctor_web.check_wizard_socket_start_limits()

    assert r.status == "skipped"
    assert r.reason == doctor_web.REASON_SYSTEMCTL_UNAVAILABLE


def test_swept_units_match_the_installers_wizard_family():
    """One source of truth: the installer's WIZARD_UNITS array.

    A wizard added there — and therefore installed, enabled, and parked as one
    — cannot silently drop out of this sweep.
    """
    assert set(doctor_web.WIZARD_UNITS) == _installer_wizard_units()


def test_every_shipped_wizard_socket_is_swept():
    """And a wizard socket shipped in deploy/ cannot skip the sweep either.

    Named exemption: deploy/jasper-web-streambox.socket is installed AS
    jasper-web.socket (install_streambox_web_unit_files), so it has no runtime
    unit name of its own and is covered by the jasper-web entry.
    """
    shipped = {p.stem for p in (ROOT / "deploy").glob("jasper-*.socket")}

    assert shipped - {"jasper-web-streambox"} == set(doctor_web.WIZARD_UNITS)


# -------------------------------------------------------------- registry


@pytest.mark.parametrize(
    "check_name",
    [
        "check_web_design_assets",
        "check_camillagui_loopback",
        "check_wizard_socket_start_limits",
    ],
)
def test_web_checks_are_registered_in_the_web_group(check_name):
    assert check_name in _registered_check_names()
    by_name = {c.func.__name__: c for c in doctor.registered_checks()}
    assert by_name[check_name].group == "web"
