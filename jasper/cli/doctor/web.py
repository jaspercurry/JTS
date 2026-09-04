# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — web domain."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...control import control_token
from ...identity import resolve_hostname
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import REASON_SYSTEMCTL_UNAVAILABLE, CheckResult, _run

# Machine-stable codes naming which branch of a web check produced a result
# (AGENTS.md: tests pin status + reason, never detail prose).
REASON_WEB_ASSETS_NOT_INSTALLED = "web_assets_not_installed"
REASON_WEB_ASSETS_MANIFEST_MISSING = "web_assets_manifest_missing"
REASON_WEB_ASSETS_MISSING = "web_assets_missing"
REASON_WEB_ASSETS_VERIFIED = "web_assets_verified"

REASON_MANAGEMENT_NOT_INSTALLED = "management_surface_not_installed"
REASON_MANAGEMENT_NO_ANSWER = "management_surface_no_answer"
REASON_MANAGEMENT_HTTP_ERROR = "management_surface_http_error"

REASON_CONTROL_TOKEN_ENABLED = "control_token_enabled"
REASON_CONTROL_TOKEN_DISABLED = "control_token_disabled"

REASON_TOOL_CATALOG_NOT_CONFIGURED = "tool_catalog_not_configured"
REASON_TOOL_CATALOG_ABSENT = "tool_catalog_absent"
REASON_TOOL_CATALOG_PRESENT = "tool_catalog_present"

REASON_HISTORY_DISABLED = "conversation_history_capture_disabled"
REASON_HISTORY_STORE_UNAVAILABLE = "conversation_history_store_unavailable"
REASON_HISTORY_STATS_UNREADABLE = "conversation_history_stats_unreadable"

REASON_CAMILLAGUI_PROBE_FAILED = "camillagui_probe_failed"
REASON_CAMILLAGUI_NOT_LISTENING = "camillagui_not_listening"
REASON_CAMILLAGUI_EXPOSED = "camillagui_exposed_non_loopback"
REASON_CAMILLAGUI_LOOPBACK_ONLY = "camillagui_loopback_only"

REASON_WIZARD_SOCKET_FINDING = "wizard_socket_finding"

def _manifest_entries(manifest: Path) -> list[str]:
    """Relative asset paths from the installer-written manifest.

    install_web_assets (deploy/lib/install/web-assets.sh) writes one
    assets/-relative path per line; a blank, comment, absolute, or
    path-traversing line is dropped rather than distorting the check."""
    entries: list[str] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "/")) or ".." in line:
            continue
        entries.append(line)
    return entries


@doctor_check(order=24, group="web")
def check_web_design_assets() -> CheckResult:
    """Every installed management-UI static asset must be present.

    Verified against assets/.install-manifest, which install_web_assets
    writes, so no hand list drifts as pages migrate. A missing stylesheet
    renders unstyled-but-visible; a missing JS module blanks the page, and a
    missing shared module blanks every importing page. A missing *manifest*
    warns rather than guessing from a built-in list that could pass a partial
    tree as green. All admin-only and non-fatal: redeploy fixes each case."""
    web_root = Path(os.environ.get("JASPER_WEB_SHARE_DIR", "/usr/share/jasper-web"))
    if not web_root.is_dir():
        return CheckResult(
            "web design assets", "skipped", "not installed",
            reason=REASON_WEB_ASSETS_NOT_INSTALLED,
        )
    assets_root = web_root / "assets"
    manifest = assets_root / ".install-manifest"
    if not manifest.is_file():
        return CheckResult(
            "web design assets", "warn",
            f"{manifest} missing — the installed assets predate the "
            "manifest-writing installer (or the install was interrupted); "
            "redeploy to write it and verify the asset tree",
            reason=REASON_WEB_ASSETS_MANIFEST_MISSING,
        )
    # app.css is in the manifest, but pinned explicitly too: it is the design
    # system itself, and one hardcoded path cannot drift.
    seen: dict[Path, None] = {assets_root / "app.css": None}
    for entry in _manifest_entries(manifest):
        seen.setdefault(assets_root / entry, None)
    required = tuple(seen)
    missing = [str(p.relative_to(web_root)) for p in required if not p.is_file()]
    if missing:
        shown = sorted(missing)[:12]
        overflow = len(missing) - len(shown)
        if overflow:
            shown.append(f"(+{overflow} more)")
        return CheckResult(
            "web design assets", "warn",
            "missing: " + ", ".join(shown)
            + " — redeploy to install (missing CSS renders unstyled; a "
            "missing JS module blanks the page)",
            reason=REASON_WEB_ASSETS_MISSING,
        )
    return CheckResult(
        "web design assets", "ok",
        f"{len(required)} assets verified against {manifest.name}",
        reason=REASON_WEB_ASSETS_VERIFIED,
    )


# Probe target for check_management_surface. Module constants so tests can
# point them at fixtures; loopback on purpose — the probe exercises
# nginx → wizard → jasper-control, not LAN reachability.
NGINX_SITE = Path("/etc/nginx/sites-enabled/jasper.conf")
MANAGEMENT_PROBE_URL = "http://127.0.0.1/system/data.json"

# Shared with check_usbnet_management_probe (jasper/cli/doctor/network.py),
# which probes the same /system/ chain over the USB fallback address.
MANAGEMENT_502_HINT = (
    " — two hops produce this identically: the socket-activated "
    "jasper-system-web wizard (nginx got no answer) or jasper-control behind "
    "it (the wizard answered but could not reach it). This run's `wizard "
    "socket start limits` check covers the commonest wizard cause; "
    "`systemctl status jasper-system-web.service jasper-control` separates "
    "the rest"
)


@doctor_check(order=24.5, group="web")
def check_management_surface() -> CheckResult:
    """The management UI must answer through nginx under the speaker's
    real hostname.

    Probes /system/data.json on loopback nginx with the resolved speaker
    hostname as `Host` — the exact path a browser takes (nginx →
    socket-activated system wizard → jasper-control behind its
    management-host guard), so any break in that chain (guard rejection,
    wizard socket misbind, control down) fails here. A 502 is the one status
    two of those hops share, so that branch names both candidates and hands
    the wizard one to :func:`check_wizard_socket_start_limits` rather than
    attributing it to jasper-control alone (#2465)."""
    import urllib.error
    import urllib.request

    label = "management surface (/system/)"
    if not NGINX_SITE.exists():
        return CheckResult(
            label, "skipped", "nginx site not installed",
            reason=REASON_MANAGEMENT_NOT_INSTALLED,
        )
    host = resolve_hostname()
    req = urllib.request.Request(MANAGEMENT_PROBE_URL, headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            status = resp.status
            body = resp.read(512)
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read(512) if e.fp else b""
    except (urllib.error.URLError, OSError) as e:
        return CheckResult(
            label, "fail",
            f"no answer from nginx on 127.0.0.1 for Host: {host} ({e}) — "
            "is nginx running? (systemctl status nginx)",
            reason=REASON_MANAGEMENT_NO_ANSWER,
        )
    if status == 200:
        return CheckResult(
            label, "ok", f"200 via nginx as Host: {host}",
        )
    detail = body.decode("utf-8", "replace").strip()[:120]
    if status == 403:
        hint = (
            " — the management-host guard rejected the request; check "
            "`journalctl -u jasper-control | grep event=http.reject` and "
            "JASPER_CONTROL_HOST / JASPER_MANAGEMENT_ALLOWED_HOSTS in the env"
        )
    elif status == 502:
        hint = MANAGEMENT_502_HINT
    else:
        hint = ""
    return CheckResult(
        label, "fail", f"HTTP {status} ({detail}){hint}",
        reason=REASON_MANAGEMENT_HTTP_ERROR,
    )


@doctor_check(order=24.6, group="web")
def check_control_token() -> CheckResult:
    """Report the control-token gate posture (never the secret).

    The control token (SECURITY.md) gates jasper-control's high-impact
    mutations behind an X-JTS-Token header; disabled means the token file is
    absent or unreadable. A *posture* line, not a health failure: ok either
    way, and strictly secret-free — only whether a non-empty file exists."""
    if control_token.token_enforced():
        return CheckResult(
            "control token gate", "ok",
            "ENABLED (mutations require X-JTS-Token)",
            reason=REASON_CONTROL_TOKEN_ENABLED,
        )
    return CheckResult(
        "control token gate", "ok",
        "disabled (token absent/unreadable; jasper-control startup normally "
        "recreates it; see SECURITY.md)",
        reason=REASON_CONTROL_TOKEN_DISABLED,
    )


@doctor_check(order=24.7, group="web")
def check_tool_catalog() -> CheckResult:
    """Report the /tools/ catalog the wizard serves: present, tool count,
    how many the household disabled, and whether a voice restart is pending.

    With no voice provider jasper-voice never writes the catalog, so that is
    a skip rather than a failure; with a provider set but no catalog on disk,
    warn — the wizard renders "not ready" and toggles do not take effect.
    Reads the light view (jasper.tool_catalog_view), never the heavy
    registry."""
    from ...tool_catalog_view import summary
    from ...voice.provider_state import read_active_provider

    label = "tool catalog"
    if not read_active_provider():
        return CheckResult(
            label, "skipped",
            "not configured (skipped — jasper-voice writes the catalog only "
            "once a voice provider is set at http://jts.local/voice/)",
            reason=REASON_TOOL_CATALOG_NOT_CONFIGURED,
        )
    s = summary()
    if not s["catalog_present"]:
        return CheckResult(
            label, "warn",
            "not written at /run/jasper/tools.json — jasper-voice may not be "
            "running; the /tools/ page shows 'not ready'. Check the System "
            "page / `journalctl -u jasper-voice`.",
            reason=REASON_TOOL_CATALOG_ABSENT,
        )
    pending = " — restart pending" if s["pending"] else ""
    return CheckResult(
        label, "ok",
        f"{s['count']} tools, {s['disabled_count']} disabled{pending}",
        reason=REASON_TOOL_CATALOG_PRESENT,
    )


@doctor_check(order=24.8, group="web")
def check_conversation_history() -> CheckResult:
    """Report whether the opt-in conversation-history store is usable.

    Capture is default-off, so an absent DB is normal until the household
    enables history. Once configured on, the read-side store must open cleanly
    or `/chat/` and `/state.chat` cannot show the log jasper-voice writes.
    """
    from ...conversation_history import ConversationStore, read_settings

    label = "conversation history"
    settings = read_settings()
    if not settings.capture_enabled:
        # Off by operator intent, and the off state WAS observed — `ok` with
        # a reason, not a skip (ADR-0228 rule 3).
        return CheckResult(
            label, "ok", "capture disabled", reason=REASON_HISTORY_DISABLED,
        )
    store = ConversationStore(settings.db_path, read_only=True)
    try:
        if not store.available:
            return CheckResult(
                label,
                "warn",
                f"capture enabled but {settings.db_path} is unavailable",
                reason=REASON_HISTORY_STORE_UNAVAILABLE,
            )
        stats = store.stats()
        if stats is None:
            return CheckResult(
                label,
                "warn",
                f"capture enabled but {settings.db_path} could not be read",
                reason=REASON_HISTORY_STATS_UNREADABLE,
            )
        last = stats.last_write_ts_utc or "never"
        return CheckResult(
            label,
            "ok",
            f"{stats.turn_count} turns, last write {last}",
        )
    finally:
        store.close()


# Probe target for check_camillagui_loopback; matches
# deploy/systemd/camillagui.socket's ListenStream port.
CAMILLAGUI_PORT = 5005
_LOOPBACK_BIND_ADDRESSES = frozenset({"127.0.0.1", "[::1]"})


def _camillagui_listen_addresses() -> list[str] | None:
    """Local bind addresses of any live LISTEN socket on CAMILLAGUI_PORT.

    Reads the kernel's socket table via `ss`, not the unit file:
    `systemctl show camillagui.socket -p Listen` reflects the parsed
    configuration and updates the instant `daemon-reload` re-reads a changed
    ListenStream=, so it cannot distinguish a rebound listener from one still
    holding its pre-upgrade fd — the exact failure this check exists to catch.

    None when the `ss` probe itself fails, so the caller reports "can't
    verify" rather than reading a failed probe as "nothing listening";
    `OSError` is caught, not just `FileNotFoundError`, so a non-executable
    `ss` or a fork failure under memory pressure degrades to a warn too.
    ``[]`` when the probe ran cleanly and found no LISTEN socket — "never
    installed" and "administratively stopped" alike, neither a live
    exposure."""
    try:
        proc = _run(["ss", "-H", "-ltn"], timeout=5.0)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    addresses: list[str] = []
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != "LISTEN":
            continue
        # "Local Address:Port" — rpartition on the LAST colon so a bracketed
        # IPv6 address ("[::1]:5005") still splits into (addr, port).
        addr, _, port = fields[3].rpartition(":")
        if port == str(CAMILLAGUI_PORT):
            addresses.append(addr)
    return addresses


@doctor_check(order=24.81, group="web")
def check_camillagui_loopback() -> CheckResult:
    """CamillaGUI's externally-reachable socket must bind loopback-only.

    A non-loopback bind is an unauthenticated, root-backed listener
    (ReadWritePaths=/etc/camilladsp) that can author CamillaDSP configs
    naming any device and live-apply a graph, reachable from any LAN device
    (#2319). The shipped unit binds 127.0.0.1:5005; reach the GUI with
    `ssh -L 5005:localhost:5005 <pi-host>`.

    Probes the live kernel-level bind via `_camillagui_listen_addresses`, not
    the unit file, so a restart that fails to re-bind is caught. Warn, not
    fail: a silently degraded security posture that has not broken product
    function. Not currently listening is ok — neither "never installed" nor
    "administratively stopped" is a live exposure."""
    label = "CamillaGUI socket bind"
    addresses = _camillagui_listen_addresses()
    if addresses is None:
        return CheckResult(
            label, "warn", "`ss` probe failed — can't verify bind posture",
            reason=REASON_CAMILLAGUI_PROBE_FAILED,
        )
    if not addresses:
        return CheckResult(
            label, "ok", "not currently listening",
            reason=REASON_CAMILLAGUI_NOT_LISTENING,
        )
    non_loopback = sorted({a for a in addresses if a not in _LOOPBACK_BIND_ADDRESSES})
    if non_loopback:
        shown = ", ".join(f"{a}:{CAMILLAGUI_PORT}" for a in non_loopback)
        return CheckResult(
            label, "warn",
            f"listening on {shown}, not loopback-only — an unauthenticated, "
            "root-backed CamillaDSP config editor is LAN-reachable (#2319); "
            "redeploy, or `systemctl restart camillagui.socket`, to "
            "re-apply the shipped 127.0.0.1 bind",
            reason=REASON_CAMILLAGUI_EXPOSED,
        )
    # Derived from the observed bind, not hardcoded to "127.0.0.1": an
    # [::1]-only bind is equally loopback-only.
    shown = ", ".join(f"{a}:{CAMILLAGUI_PORT}" for a in sorted(set(addresses)))
    return CheckResult(
        label, "ok", f"loopback-only ({shown})",
        reason=REASON_CAMILLAGUI_LOOPBACK_ONLY,
    )


# The socket-activated wizard family, by unit basename (both `<name>.service`
# and `<name>.socket` derive from each entry). Canonical membership is the
# installer's WIZARD_UNITS array (deploy/lib/install/systemd-units.sh);
# tests/test_doctor_web.py pins this tuple set-equal to it and to the shipped
# deploy/*.socket files. One name covers both profiles — a streambox installs
# its own web units under the same jasper-web names — so no profile branch.
WIZARD_UNITS = (
    "jasper-web",
    "jasper-bluetooth-web",
    "jasper-correction-web",
    "jasper-system-web",
    "jasper-chat-web",
)

# systemd's own name for "I gave up starting the service this socket
# triggers", as it appears in `systemctl show <socket> --property=Result`.
_START_LIMIT_RESULT = "service-start-limit-hit"


def _wizard_socket_state(unit: str) -> tuple[str, str] | None:
    """``(ActiveState, finding)`` for one wizard's ``.socket``, or ``None``
    when the per-run unit-state evidence is unavailable (systemctl absent),
    so the caller skips the whole sweep once.

    ``finding`` is empty when the listener is bound — including when the unit
    is not installed on this profile, which reads as ``ActiveState=inactive``
    rather than erroring.
    """
    socket_unit = f"{unit}.socket"
    state = evidence.unit_state(socket_unit)
    if state is None:
        return None
    active = state.get("active_state") or ""
    if not active:
        return "", (
            f"could not read {socket_unit} ActiveState from systemctl — a "
            "start-limited wizard is indistinguishable from a healthy one "
            "until this reads"
        )
    if active != "failed":
        return active, ""
    result = state.get("result") or "unknown"
    cause = (
        "its paired service exhausted StartLimitBurst"
        if result == _START_LIMIT_RESULT
        else "systemd reported the socket failed"
    )
    return active, (
        f"{socket_unit} is failed (ActiveState=failed, Result={result}) — "
        f"{cause}. Its listener is unbound, so the wizard refuses connections "
        "and nothing brings it back on its own. Run `sudo systemctl "
        f"reset-failed {socket_unit} {unit}.service && sudo systemctl start "
        f"{socket_unit}`."
    )


@doctor_check(order=24.82, group="web")
def check_wizard_socket_start_limits() -> CheckResult:
    """A failed ``.socket`` is what a start-limited wizard looks like.

    Reads each :data:`WIZARD_UNITS` member's ``.socket`` ActiveState (never
    the service — the service cycles through ``failed`` during ordinary
    ``Restart=on-failure`` retries, the socket does not): ``failed`` is a
    finding (listener unbound, wizard unreachable until an operator runs
    ``reset-failed``); ``inactive``/``Result=success`` is a unit this profile
    does not install, not a finding; anything else is healthy. ``Result`` is
    reported but never gated on, so a socket killed by any marker (not just
    ``service-start-limit-hit``) still surfaces. A read that fails outright
    (no ``ActiveState`` in the reply) is a ``fail``, never an ``ok`` standing
    in for "could not tell".
    """
    label = "wizard socket start limits"
    observed: list[str] = []
    findings: list[str] = []
    for unit in WIZARD_UNITS:
        result = _wizard_socket_state(unit)
        if result is None:
            return CheckResult(
                label, "skipped", "systemctl unavailable — skipped (not Linux?)",
                reason=REASON_SYSTEMCTL_UNAVAILABLE,
            )
        active, finding = result
        if finding:
            findings.append(finding)
        else:
            observed.append(f"{unit}.socket={active}")
    if findings:
        return CheckResult(
            label, "fail", " ".join(findings), reason=REASON_WIZARD_SOCKET_FINDING,
        )
    return CheckResult(
        label, "ok", ", ".join(observed),
    )
