# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install-time render of the static landing page and the area hubs.

The landing page is deploy/index.html with its placeholders substituted;
the `/sound/` and `/assistant/` hubs are rendered whole from the same
manifest and capability map (`nav.render_hub`).

nginx serves both straight from disk, so everything a daemon would compute
per request is substituted once, here, by install.sh:

  * the app.css cache-bust token, keyed on the build SHA the install records;
  * the install profile's capability map, as the JSON data island the
    landing module gates on (every gated section ships ``hidden``, so gating
    only ever reveals — the page is right with every backend daemon down);
  * the WS1 control token the assistant-pause button rides on POST /mic/mute
    (kept inside this process — never a shell argument or a log line);
  * the shared icon sprite, so the landing and the Python-rendered pages draw
    from one set (``_common.CANONICAL_ICON_SPRITE``);
  * the settings groups, rendered from the site-map manifest (``nav.NAV``)
    so the landing page and the pages it links share one row table.

An unsubstituted placeholder fails the install rather than shipping a page
with a literal ``__JTS_…__`` in it.
"""
from __future__ import annotations

import argparse
import sys
from html import escape
from pathlib import Path

from jasper.control.control_token import ensure_token
from jasper.install_profile import (
    read_install_profile,
    system_capabilities_for_profile,
)

from ._common import CANONICAL_ICON_SPRITE, json_island
from .nav import children, hub_paths, landing_groups_html, render_hub

def substitutions(
    *, app_css_version: str, caps: dict[str, object], control_token: str
) -> dict[str, str]:
    """Placeholder → replacement: the landing template's whole contract.

    `caps` is `system_capabilities_for_profile`'s map as-is: mostly booleans
    the page gates on, plus the `install_profile`/`role` strings — hence
    `object`, not `bool`.
    """
    return {
        "__APP_CSS_VERSION__": app_css_version,
        "__JTS_CAPS_ISLAND__": json_island("landing-caps", caps),
        "__JTS_CONTROL_TOKEN__": escape(control_token),
        "__JTS_ICON_SPRITE__": CANONICAL_ICON_SPRITE,
        "__JTS_NAV_GROUPS__": landing_groups_html(children("/")),
    }


def render_landing(
    template: str,
    *,
    app_css_version: str,
    caps: dict[str, object],
    control_token: str,
) -> str:
    """Substitute every landing-page placeholder, or raise ValueError."""
    subs = substitutions(
        app_css_version=app_css_version,
        caps=caps,
        control_token=control_token,
    )
    missing = [name for name in subs if name not in template]
    if missing:
        raise ValueError(f"landing page is missing {', '.join(missing)}")
    for name, value in subs.items():
        template = template.replace(name, value)
    return template


def write_hub_pages(
    web_root: Path, *, caps: dict[str, object], app_css_version: str
) -> None:
    """Write `<web_root>/<hub>/index.html` for every hub in the manifest."""
    for path in hub_paths():
        page = web_root / path.strip("/") / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            render_hub(path, caps=caps, app_css_version=app_css_version),
            encoding="utf-8",
        )
        # nginx (www-data) reads these; the install shell's umask must not
        # decide whether the household gets a 403.
        page.parent.chmod(0o755)
        page.chmod(0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the landing page and the area hubs."
    )
    parser.add_argument("page", help="installed index.html, rewritten in place")
    parser.add_argument("--app-css-version", required=True)
    parser.add_argument(
        "--hub-dir", required=True, help="web root the hub pages are written under"
    )
    args = parser.parse_args(argv)

    page = Path(args.page)
    try:
        caps = system_capabilities_for_profile(read_install_profile())
        rendered = render_landing(
            page.read_text(encoding="utf-8"),
            app_css_version=args.app_css_version,
            caps=caps,
            control_token=ensure_token(),
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    page.write_text(rendered, encoding="utf-8")
    write_hub_pages(
        Path(args.hub_dir), caps=caps, app_css_version=args.app_css_version
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
