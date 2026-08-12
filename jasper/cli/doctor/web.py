# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — web domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...control import control_token
from ._registry import doctor_check
from ._shared import CheckResult, _run

def _manifest_entries(manifest: Path) -> list[str]:
    """Relative asset paths from the installer-written manifest.

    install_web_assets (deploy/lib/install/web-assets.sh) writes one
    assets/-relative path per line. Tolerate anything else — a blank,
    comment, absolute, or path-traversing line is dropped rather than
    letting one bad byte distort the check."""
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

    install_web_assets records each copied asset (app.css, fonts,
    per-page CSS + ES modules, the shared cross-page modules) in
    assets/.install-manifest, and this check verifies the installed
    tree against it — no hand list to drift as pages migrate. A
    missing stylesheet renders unstyled-but-visible; a missing JS
    module blanks the page — and a missing shared module blanks every
    importing page at once. A missing *manifest* means the asset tree
    predates the manifest-writing installer (or an install died before
    reaching it), so the tree can't be verified at all — warn rather
    than guess from a stale built-in list, which could pass a partial
    tree as green. All admin-only and non-fatal: redeploy fixes each
    case. On a non-Pi checkout (no /usr/share/jasper-web) there's
    nothing to verify."""
    web_root = Path(os.environ.get("JASPER_WEB_SHARE_DIR", "/usr/share/jasper-web"))
    if not web_root.is_dir():
        return CheckResult("web design assets", "ok", "not installed (skipped)")
    assets_root = web_root / "assets"
    manifest = assets_root / ".install-manifest"
    if not manifest.is_file():
        return CheckResult(
            "web design assets", "warn",
            f"{manifest} missing — the installed assets predate the "
            "manifest-writing installer (or the install was interrupted); "
            "redeploy to write it and verify the asset tree",
        )
    # app.css is in the manifest, but pin it explicitly too: it is the
    # design system itself, and one hardcoded path can't drift.
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
        )
    return CheckResult(
        "web design assets", "ok",
        f"{len(required)} assets verified against {manifest.name}",
    )


# Probe target for check_management_surface. Module constants so tests can
# point them at fixtures; the URL is loopback on purpose — the probe runs
# on-Pi and exercises nginx → wizard → jasper-control, not LAN reachability.
NGINX_SITE = Path("/etc/nginx/sites-enabled/jasper.conf")
MANAGEMENT_PROBE_URL = "http://127.0.0.1/system/data.json"


@doctor_check(order=24.5, group="web")
def check_management_surface() -> CheckResult:
    """The management UI must answer through nginx under the speaker's
    real hostname.

    Probes /system/data.json on loopback nginx with `Host:
    <JASPER_HOSTNAME>` — the exact path a browser takes (nginx →
    socket-activated system wizard → jasper-control behind its
    management-host guard). Pins the 2026-06-11 regression class
    closed: the wizard's control client carried `Host: 0.0.0.0:8780`
    from a seeded bind value and every dashboard poll 403ed, with
    nothing on the Pi noticing. Any break in the nginx → wizard →
    control chain (guard rejection, wizard socket misbind, control
    down) fails here with the layer named. Skips on a non-Pi checkout
    (no installed nginx site)."""
    import urllib.error
    import urllib.request

    label = "management surface (/system/)"
    if not NGINX_SITE.exists():
        return CheckResult(label, "ok", "nginx site not installed (skipped)")
    host = (os.environ.get("JASPER_HOSTNAME") or "jts.local").strip()
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
        )
    if status == 200:
        return CheckResult(label, "ok", f"200 via nginx as Host: {host}")
    detail = body.decode("utf-8", "replace").strip()[:120]
    if status == 403:
        hint = (
            " — the management-host guard rejected the request; check "
            "`journalctl -u jasper-control | grep event=http.reject` and "
            "JASPER_CONTROL_HOST / JASPER_MANAGEMENT_ALLOWED_HOSTS in the env"
        )
    elif status == 502:
        hint = (
            " — wizard answered but jasper-control is unreachable "
            "(systemctl status jasper-control)"
        )
    else:
        hint = ""
    return CheckResult(label, "fail", f"HTTP {status} ({detail}){hint}")


@doctor_check(order=24.6, group="web")
def check_control_token() -> CheckResult:
    """Report the control-token gate posture (never the secret).

    The control token (SECURITY.md) gates jasper-control's high-impact
    mutations (/system/poweroff, /system/reboot, /mic/mute,
    /grouping/set) behind an X-JTS-Token header. Production startup normally
    auto-generates it; disabled means the token file is currently absent or
    unreadable. This is a *posture* line, not a health failure: ok either way.
    Strictly secret-free — it reports only whether a non-empty token file exists,
    never reads or echoes the value."""
    if control_token.token_enforced():
        return CheckResult(
            "control token gate", "ok",
            "ENABLED (mutations require X-JTS-Token)",
        )
    return CheckResult(
        "control token gate", "ok",
        "disabled (token absent/unreadable; jasper-control startup normally "
        "recreates it; see SECURITY.md)",
    )


@doctor_check(order=24.7, group="web")
def check_tool_catalog() -> CheckResult:
    """Report the /tools/ catalog the wizard serves: present, tool count,
    how many the household disabled, and whether a voice restart is pending.

    Skip-if-not-configured: with no voice provider jasper-voice doesn't run
    (so it never writes the catalog) — ok/skipped, not a failure. With a
    provider set but no catalog on disk, warn: the wizard renders a "not
    ready" state and toggles won't take effect until the daemon writes it.
    Reads the same light view (jasper.tool_catalog_view) the wizard + /state
    use — never imports the heavy registry."""
    from ...tool_catalog_view import summary
    from ...voice.provider_state import read_active_provider

    label = "tool catalog"
    if not read_active_provider():
        return CheckResult(
            label, "ok",
            "not configured (skipped — jasper-voice writes the catalog only "
            "once a voice provider is set at http://jts.local/voice/)",
        )
    s = summary()
    if not s["catalog_present"]:
        return CheckResult(
            label, "warn",
            "not written at /run/jasper/tools.json — jasper-voice may not be "
            "running; the /tools/ page shows 'not ready'. Check the System "
            "page / `journalctl -u jasper-voice`.",
        )
    pending = " — restart pending" if s["pending"] else ""
    return CheckResult(
        label, "ok",
        f"{s['count']} tools, {s['disabled_count']} disabled{pending}",
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
        return CheckResult(
            label,
            "ok",
            "capture disabled (skipped)",
        )
    store = ConversationStore(settings.db_path, read_only=True)
    try:
        if not store.available:
            return CheckResult(
                label,
                "warn",
                f"capture enabled but {settings.db_path} is unavailable",
            )
        stats = store.stats()
        if stats is None:
            return CheckResult(
                label,
                "warn",
                f"capture enabled but {settings.db_path} could not be read",
            )
        last = stats.last_write_ts_utc or "never"
        return CheckResult(
            label,
            "ok",
            f"{stats.turn_count} turns, last write {last}",
        )
    finally:
        store.close()


# Probe target for check_camillagui_loopback (#2319). Module constant,
# mirroring MANAGEMENT_PROBE_URL above, so tests can point elsewhere;
# matches deploy/systemd/camillagui.socket's ListenStream port.
CAMILLAGUI_PORT = 5005
_LOOPBACK_BIND_ADDRESSES = frozenset({"127.0.0.1", "[::1]"})


def _camillagui_listen_addresses() -> list[str] | None:
    """Local bind addresses of any live LISTEN socket on CAMILLAGUI_PORT.

    Reads the kernel's actual socket table via `ss`, not the unit file.
    `systemctl show camillagui.socket -p Listen` reflects the *parsed
    configuration* — it updates the instant `daemon-reload` re-reads a
    changed ListenStream=, even before the socket unit is restarted — so it
    cannot distinguish a genuinely-rebound listener from one still holding
    its pre-upgrade fd open. That gap is exactly the failure mode this
    check exists to catch: install.sh's `install_camillagui` restarts (not
    just starts/enables) the socket on every deploy specifically so a
    ListenStream= change actually re-binds; `ss` is the ground truth that
    proves the restart worked rather than trusting it did.

    Returns None when the `ss` probe itself fails (not found, not
    executable, a transient fork/resource failure, non-zero exit) so the
    caller can report "can't verify" rather than silently reading a failed
    probe as "nothing listening". `OSError` (not just `FileNotFoundError`)
    is caught so a non-executable `ss` (PermissionError) or a fork failure
    under memory pressure (OSError ENOMEM) also degrades to warn rather
    than crashing the whole doctor run — the package's majority convention
    for subprocess probes like this one (see jasper/cli/doctor/memory.py,
    correction.py). Returns [] when the probe ran cleanly and found no
    LISTEN socket on the port — covers both "never installed" and
    "administratively stopped" (e.g. a hardware runbook's precautionary
    `systemctl stop camillagui.socket`); either way there is no live
    exposure to warn about."""
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
        # "Local Address:Port" — rpartition on the LAST colon so a
        # bracketed IPv6 address (which contains colons of its own, e.g.
        # "[::1]:5005") still splits into the right (addr, port) pair.
        addr, _, port = fields[3].rpartition(":")
        if port == str(CAMILLAGUI_PORT):
            addresses.append(addr)
    return addresses


@doctor_check(order=24.81, group="web")
def check_camillagui_loopback() -> CheckResult:
    """CamillaGUI's externally-reachable socket must bind loopback-only.

    #2319: camillagui.socket used to bind 0.0.0.0:5005 directly — an
    unauthenticated, root-backed listener (ReadWritePaths=/etc/camilladsp)
    that can author new CamillaDSP configs naming any device and live-apply
    a graph over CamillaDSP's websocket, reachable from any device on the
    LAN. The shipped unit now binds 127.0.0.1:5005 instead; reach the GUI
    with `ssh -L 5005:localhost:5005 <pi-host>` then browse
    http://localhost:5005/ from the laptop.

    Probes the LIVE kernel-level bind via `ss` (see
    `_camillagui_listen_addresses`), not the unit file, so a restart that
    silently fails to re-bind is caught rather than masked. Warn, not fail
    — matches `check_household_secret_readable`'s convention for a security
    posture that has silently degraded without breaking product function
    (jasper-doctor's exit code is driven by fails, not warns). Not
    currently listening is ok, not a failure: it covers both "never
    installed" (an arch without a CamillaGUI bundle) and "administratively
    stopped" (e.g. the jts3 hardware runbooks' precaution of stopping the
    socket for a measurement sequence) — neither is a live exposure."""
    label = "CamillaGUI socket bind"
    addresses = _camillagui_listen_addresses()
    if addresses is None:
        return CheckResult(
            label, "warn", "`ss` probe failed — can't verify bind posture",
        )
    if not addresses:
        return CheckResult(label, "ok", "not currently listening")
    non_loopback = sorted({a for a in addresses if a not in _LOOPBACK_BIND_ADDRESSES})
    if non_loopback:
        shown = ", ".join(f"{a}:{CAMILLAGUI_PORT}" for a in non_loopback)
        return CheckResult(
            label, "warn",
            f"listening on {shown}, not loopback-only — an unauthenticated, "
            "root-backed CamillaDSP config editor is LAN-reachable (#2319); "
            "redeploy, or `systemctl restart camillagui.socket`, to "
            "re-apply the shipped 127.0.0.1 bind",
        )
    # Derived from the observed bind, not hardcoded to "127.0.0.1" — an
    # [::1]-only bind is equally loopback-only and must not be misreported
    # as an IPv4 address that isn't actually listening.
    shown = ", ".join(f"{a}:{CAMILLAGUI_PORT}" for a in sorted(set(addresses)))
    return CheckResult(label, "ok", f"loopback-only ({shown})")
