"""jasper-doctor checks — web domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import os
from pathlib import Path
from ._registry import doctor_check
from ._shared import CheckResult

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
