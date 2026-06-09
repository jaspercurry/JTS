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

@doctor_check(order=24, group="web")
def check_web_design_assets() -> CheckResult:
    """The shared management-UI stylesheet must be installed.

    The redesigned wizards and the landing page link /assets/app.css for
    the canonical design system; nginx serves it from
    /usr/share/jasper-web/assets/. If it's missing, pages render
    unstyled — visible but not fatal, so warn (redeploy). On a non-Pi
    checkout (no /usr/share/jasper-web) there's nothing to verify."""
    web_root = Path(os.environ.get("JASPER_WEB_SHARE_DIR", "/usr/share/jasper-web"))
    if not web_root.is_dir():
        return CheckResult("web design assets", "ok", "not installed (skipped)")
    # Static assets for the redesigned pages (/system/, /sound/): the shared
    # stylesheet, each page's own stylesheet, each page's ES module entry, and
    # the shared cross-page <dialog> helper module. A missing stylesheet renders
    # unstyled-but-visible; a missing JS module blanks the page — both admin-only
    # and non-fatal, so warn (redeploy).
    app_css = web_root / "assets" / "app.css"
    fonts = web_root / "assets" / "fonts"
    required = (
        app_css,
        web_root / "assets" / "system-status" / "system.css",
        web_root / "assets" / "system-status" / "js" / "main.js",
        web_root / "assets" / "sound-profile" / "sound.css",
        web_root / "assets" / "sound-profile" / "js" / "main.js",
        web_root / "assets" / "correction" / "correction.css",
        web_root / "assets" / "correction" / "js" / "main.js",
        web_root / "assets" / "shared" / "js" / "dialog.js",
    )
    missing = [str(p.relative_to(web_root)) for p in required if not p.is_file()]
    if not fonts.is_dir():
        missing.append("assets/fonts/")
    if missing:
        return CheckResult(
            "web design assets", "warn",
            "missing: " + ", ".join(sorted(missing))
            + " — redeploy to install (missing CSS renders unstyled; a "
            "missing JS module blanks the page)",
        )
    return CheckResult("web design assets", "ok", str(app_css))
