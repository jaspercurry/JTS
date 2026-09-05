# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The site map: one row per page the user can tap through.

`NAV` renders the landing page's settings groups, the `/sound/` and
`/assistant/` hub pages (`render_hub`, rows whose `parent` is the hub path)
and, as pages adopt `entry()`, feeds them their title and back link
(docs/web-ia.md §1-§2). Stdlib only, like `_common`'s page shell it calls:
this runs under the system interpreter at install time.
`requires` lists a row's gates outermost first; a group whose rows share the
outermost one carries it on the `<section>` and the rest gate the row.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from ._common import canonical_header, canonical_page, json_island


class NavRow(NamedTuple):
    group: str
    label: str
    path: str
    parent: str
    requires: tuple[str, ...]
    icon: str
    status_id: str
    status_text: str
    operator: bool = False


NAV: tuple[NavRow, ...] = (
    NavRow("Sources", "Playback sources", "/sources/", "/", ("local_sources",),
           "source", "status-playback-source", "Auto"),
    NavRow("Sources", "Spotify accounts", "/spotify/", "/", ("local_sources",),
           "music", "", "Household routing"),
    NavRow("Sources", "Bluetooth devices", "/bluetooth/", "/", ("local_sources",),
           "bluetooth", "", "Pairing"),
    NavRow("Sources", "AirPlay sync", "/airplay/", "/", ("local_sources",),
           "airplay", "", "Synced"),
    NavRow("Sound", "Sound", "/sound/", "/", ("content_dsp",),
           "sliders", "", "EQ · Speakers · Room · Bass"),
    NavRow("Sound", "EQ", "/eq/", "/sound/", ("content_dsp",),
           "sound", "", "Profiles · Simple EQ · PEQ"),
    NavRow("Sound", "Sound setup", "/sound/setup/", "/sound/", ("content_dsp",),
           "sliders", "", "Volume · Outputs · Commissioning"),
    NavRow("Sound", "Active speaker", "/sound/crossover/", "/sound/",
           ("content_dsp",), "wave", "", "Crossover measurement"),
    NavRow("Sound", "Stereo pair", "/rooms/", "/sound/",
           ("content_dsp", "pair_management"), "peers", "",
           "Group speakers · Wake response"),
    NavRow("Sound", "Room correction", "/sound/room/", "/sound/", ("content_dsp",),
           "wave", "", "Microphone measurement"),
    NavRow("Sound", "Bass", "/sound/bass/", "/sound/", ("content_dsp",),
           "sound", "", "Bass-management status"),
    NavRow("Sound", "Measurements", "/sound/measurements/", "/sound/",
           ("content_dsp",), "wave", "", "Saved sweeps"),
    NavRow("Assistant", "Assistant", "/assistant/", "/", ("voice_brain",),
           "voice", "", "Voice · Wake word · Services"),
    NavRow("Assistant", "Voice", "/voice/", "/assistant/", ("voice_brain",),
           "voice", "status-voice", "Provider"),
    NavRow("Assistant", "Wake word", "/wake/", "/assistant/",
           ("voice_brain", "wake_detection"), "wake", "", "Model · Sensitivity · Mic"),
    NavRow("Assistant", "Tools", "/tools/", "/assistant/", ("voice_brain",),
           "tools", "", "Voice tools on/off"),
    NavRow("Assistant", "Chat history", "/chat/", "/assistant/", ("voice_brain",),
           "chat", "", "Recent voice turns"),
    NavRow("Services", "Weather", "/weather/", "/assistant/", ("voice_brain",),
           "weather", "", "Location and units"),
    NavRow("Services", "Transit", "/transit/", "/assistant/", ("voice_brain",),
           "transit", "", "Routes and stops"),
    NavRow("Services", "Google", "/google/", "/assistant/", ("voice_brain",),
           "calendar", "", "Calendar · Gmail"),
    NavRow("Services", "Home Assistant", "/ha/", "/assistant/", ("voice_brain",),
           "home", "status-ha", "Not connected"),
    NavRow("System", "Status", "/system/", "/", (),
           "system", "status-software", "Build"),
    NavRow("System", "Wi-Fi", "/wifi/", "/", ("network_settings",),
           "wifi", "", "Network profiles"),
    NavRow("System", "Speaker name", "/speaker/", "/", ("speaker_settings",),
           "tag", "status-speaker-name", "JTS"),
    NavRow("System", "Wake corpus", "/wake-corpus/", "/", ("developer_tools",),
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
    """Every path rows hang under other than the landing: the hub pages."""
    return tuple(dict.fromkeys(row.parent for row in NAV if row.parent != "/"))


def _gate_attr(gates: tuple[str, ...]) -> str:
    if len(gates) > 1:
        raise ValueError(f"one data-requires per element, got {gates}")
    return f' data-requires="{gates[0]}" hidden' if gates else ""


def _row_html(row: NavRow, gates: tuple[str, ...]) -> str:
    status_id = f' id="{row.status_id}"' if row.status_id else ""
    return f"""\
          <a class="setting-row{' operator' if row.operator else ''}" \
href="{row.path}"{_gate_attr(gates)}>
            <span class="row-icon"><svg aria-hidden="true"><use href="#icon-{row.icon}"></use></svg></span>
            <span class="setting-copy">
              <span class="setting-title">{row.label}</span>
              <span class="setting-status"{status_id}>{row.status_text}</span>
            </span>
            <svg class="chevron" aria-hidden="true"><use href="#icon-chevron"></use></svg>
          </a>"""


def _section_html(group: str, rows: Sequence[NavRow], *, heading: bool) -> str:
    first = rows[0].requires[:1]
    shared = first if all(row.requires[:1] == first for row in rows) else ()
    slug = group.lower()
    body = "\n".join(_row_html(row, row.requires[len(shared):]) for row in rows)
    # A section named after the page it is on would repeat the title, so it is
    # labelled instead of headed.
    label = f'aria-labelledby="{slug}-heading"' if heading else f'aria-label="{group}"'
    title = (
        f'\n        <h2 class="eyebrow group-title" id="{slug}-heading">{group}</h2>'
        if heading else ""
    )
    return f"""\
      <section class="settings-section" {label}{_gate_attr(shared)}>{title}
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


def render_hub(path: str, *, caps: dict[str, object], app_css_version: str) -> str:
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
