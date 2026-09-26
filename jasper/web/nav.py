# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The site map: one row per page the user can tap through.

`NAV` renders the landing page's settings groups and the `/sound/` and
`/assistant/` hub pages (`render_hub`, rows whose `parent` is the hub path).
Stdlib only, like `chrome`'s page shell it calls:
this runs under the system interpreter at install time.
`requires` names the capability a row needs ("" for none).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from .chrome import canonical_header, canonical_page, json_island


class NavRow(NamedTuple):
    group: str
    label: str
    path: str
    parent: str
    requires: str
    icon: str
    status_id: str
    status_text: str
    operator: bool = False


NAV: tuple[NavRow, ...] = (
    NavRow("Sources", "Playback sources", "/sources/", "/", "",
           "source", "status-playback-source", "Auto"),
    NavRow("Sources", "Spotify accounts", "/spotify/", "/", "",
           "music", "", "Household routing"),
    NavRow("Sources", "Bluetooth devices", "/bluetooth/", "/", "",
           "bluetooth", "", "Pairing"),
    NavRow("Sound", "Sound", "/sound/", "/", "",
           "sliders", "", "EQ · Speakers · Pair · Bass"),
    NavRow("Sound", "EQ", "/sound/eq/", "/sound/", "",
           "sound", "", "Profiles · Simple EQ · PEQ"),
    NavRow("Sound", "Speaker setup", "/sound/speaker/", "/sound/", "",
           "sound", "", "Layout · Drivers · Commissioning"),
    NavRow("Sound", "Active speaker", "/sound/speaker/crossover/", "/sound/speaker/",
           "", "wave", "", "Crossover measurement"),
    NavRow("Sound", "Output", "/sound/output/", "/sound/", "",
           "sliders", "", "Audio HAT · Volume shaping"),
    NavRow("Sound", "Stereo pair", "/sound/pair/", "/sound/", "",
           "peers", "", "Group speakers · Wake response"),
    NavRow("Sound", "Speaker timing", "/sound/pair/sync/", "/sound/pair/",
           "", "wave", "", "Timing between the two speakers"),
    NavRow("Sound", "Bass", "/sound/bass/", "/sound/", "",
           "sound", "", "Bass-management status"),
    NavRow("Sound", "Measurements", "/sound/measurements/", "/sound/",
           "", "wave", "", "Saved sweeps"),
    NavRow("Assistant", "Assistant", "/assistant/", "/", "",
           "voice", "", "Voice · Wake word · Services"),
    NavRow("Assistant", "Voice", "/assistant/voice/", "/assistant/", "",
           "voice", "status-voice", "Provider"),
    NavRow("Assistant", "Wake word", "/assistant/wake/", "/assistant/",
           "wake_detection", "wake", "", "Model · Sensitivity · Mic"),
    NavRow("Assistant", "Tools", "/assistant/tools/", "/assistant/", "",
           "tools", "", "Voice tools on/off"),
    NavRow("Assistant", "Chat history", "/assistant/chat/", "/assistant/", "",
           "chat", "", "Recent voice turns"),
    NavRow("Services", "Weather", "/assistant/weather/", "/assistant/", "",
           "weather", "", "Location and units"),
    NavRow("Services", "Transit", "/assistant/transit/", "/assistant/", "",
           "transit", "", "Routes and stops"),
    NavRow("Services", "Google", "/assistant/google/", "/assistant/", "",
           "calendar", "", "Calendar · Gmail"),
    NavRow("Services", "Home Assistant", "/assistant/ha/", "/assistant/", "",
           "home", "status-ha", "Not connected"),
    NavRow("System", "Status", "/system/", "/", "",
           "system", "status-software", "Build"),
    NavRow("System", "Wi-Fi", "/wifi/", "/", "",
           "wifi", "", "Network profiles"),
    NavRow("System", "Speaker name", "/speaker/", "/", "",
           "tag", "status-speaker-name", "JTS"),
    NavRow("System", "Wake corpus", "/wake-corpus/", "/", "wake_detection",
           "dev", "", "Recordings", True),
)


def entry(path: str) -> NavRow:
    """The row for `path`; a hub's row is the landing row that links it."""
    for row in NAV:
        if row.path == path:
            return row
    raise KeyError(path)


def children(parent: str) -> tuple[NavRow, ...]:
    """The rows one level under `parent` — a hub's rows, or the landing's."""
    return tuple(row for row in NAV if row.parent == parent)


def hub_paths() -> tuple[str, ...]:
    """The landing rows that are themselves parents: the static hub pages.

    Being a parent is not enough — a page one level down keeps its own
    daemon when it gains a child.
    """
    parents = {row.parent for row in NAV}
    return tuple(r.path for r in NAV if r.parent == "/" and r.path in parents)


def _row_html(row: NavRow) -> str:
    status_id = f' id="{row.status_id}"' if row.status_id else ""
    gate = f' data-requires="{row.requires}" hidden' if row.requires else ""
    return f"""\
          <a class="setting-row{' operator' if row.operator else ''}" \
href="{row.path}"{gate}>
            <span class="row-icon"><svg aria-hidden="true"><use href="#icon-{row.icon}"></use></svg></span>
            <span class="setting-copy">
              <span class="setting-title">{row.label}</span>
              <span class="setting-status"{status_id}>{row.status_text}</span>
            </span>
            <svg class="chevron" aria-hidden="true"><use href="#icon-chevron"></use></svg>
          </a>"""


def _section_html(group: str, rows: Sequence[NavRow], *, heading: bool) -> str:
    slug = group.lower()
    body = "\n".join(_row_html(row) for row in rows)
    # A section named after the page it is on would repeat the title, so it is
    # labelled instead of headed.
    label = f'aria-labelledby="{slug}-heading"' if heading else f'aria-label="{group}"'
    title = (
        f'\n        <h2 class="eyebrow group-title" id="{slug}-heading">{group}</h2>'
        if heading else ""
    )
    return f"""\
      <section class="settings-section" {label}>{title}
        <div class="settings-list">
{body}
        </div>
      </section>"""


def landing_groups_html(rows: Sequence[NavRow], *, page_title: str = "") -> str:
    """The `<nav class="groups">` inner markup, one section per group.

    `page_title` is the title of the page these groups render on, so the
    section named after it drops its heading rather than repeating it.
    """
    groups: dict[str, list[NavRow]] = {}
    for row in rows:
        groups.setdefault(row.group, []).append(row)
    return "\n\n".join(
        _section_html(g, r, heading=g != page_title) for g, r in groups.items()
    )


def render_hub(path: str, *, caps: dict[str, bool], app_css_version: str) -> str:
    """The static hub page for `path`: its child rows as settings groups.

    Rendered at install time (`jasper.web.landing`) and served from disk, so
    it carries no per-request state — the capability island gates the rows
    before any fetch, exactly as on the landing page.
    """
    row = entry(path)
    rows = children(path)
    if not rows:
        raise KeyError(f"no rows under {path}")
    body = f"""{canonical_header(row.label, back_href=row.parent)}
<main class="page">
    <nav class="groups" aria-label="{row.label} settings">
{landing_groups_html(rows, page_title=row.label)}
    </nav>
</main>
{json_island("landing-caps", caps)}
<script type="module" src="/assets/hub/js/main.js"></script>"""
    return canonical_page(
        row.label, body, app_css_version=app_css_version, control_token_meta=False
    ).decode()
