# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The site map: one row per page the user can tap through.

`NAV` renders the landing page's settings groups and, as pages adopt
`entry()`, feeds them their title and back link (docs/web-ia.md §1-§2).
Stdlib only: it runs under the system interpreter at install time.
`requires` lists a row's gates outermost first; a group whose rows share the
outermost one carries it on the `<section>` and the rest gate the row.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple


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
    NavRow("Sound", "EQ", "/eq/", "/", ("content_dsp",),
           "sound", "", "Profiles · Simple EQ · PEQ"),
    NavRow("Sound", "Sound setup", "/sound/setup/", "/", ("content_dsp",),
           "sliders", "", "Volume · Outputs · Commissioning"),
    NavRow("Sound", "Active speaker", "/sound/crossover/", "/", ("content_dsp",),
           "wave", "", "Crossover measurement"),
    NavRow("Sound", "Room correction", "/sound/room/", "/", ("content_dsp",),
           "wave", "", "Microphone measurement"),
    NavRow("Sound", "Bass", "/sound/bass/", "/", ("content_dsp",),
           "sound", "", "Bass-management status"),
    NavRow("Assistant", "Voice", "/voice/", "/", ("voice_brain",),
           "voice", "status-voice", "Provider"),
    NavRow("Assistant", "Voice assistant", "/wake/", "/",
           ("voice_brain", "wake_detection"), "wake", "", "Wake word · microphone"),
    NavRow("Assistant", "Chat history", "/chat/", "/", ("voice_brain",),
           "chat", "", "Recent voice turns"),
    NavRow("Assistant", "Tools", "/tools/", "/", ("voice_brain",),
           "tools", "", "Voice tools on/off"),
    NavRow("Integrations", "Weather", "/weather/", "/", ("voice_brain",),
           "weather", "", "Location and units"),
    NavRow("Integrations", "Transit", "/transit/", "/", ("voice_brain",),
           "transit", "", "Routes and stops"),
    NavRow("Integrations", "Google", "/google/", "/", ("voice_brain",),
           "calendar", "", "Calendar · Gmail"),
    NavRow("Integrations", "Home Assistant", "/ha/", "/", ("voice_brain",),
           "home", "status-ha", "Not connected"),
    NavRow("Network", "Wi-Fi", "/wifi/", "/", ("network_settings",),
           "wifi", "", "Network profiles"),
    NavRow("Network", "Speakers", "/rooms/", "/", ("network_settings",),
           "peers", "", "Speakers and wake response"),
    NavRow("System", "Status", "/system/", "/", (),
           "system", "status-system", "Metrics"),
    NavRow("System", "Speaker name", "/speaker/", "/", ("speaker_settings",),
           "tag", "status-speaker-name", "JTS"),
    NavRow("System", "Software", "/system/", "/", (),
           "software", "status-software", "Build"),
    NavRow("System", "Developer tools", "/wake-corpus/", "/", ("developer_tools",),
           "dev", "", "Wake corpus", True),
)

# Two ids nothing reads any more, kept so this render stays byte-identical to
# the markup it replaces. B.2 deletes them with the grouping edit
# (docs/UX-AUDIT-2026-09-03.md §7).
_DEAD_IDS = {"Sound": ' id="sound-section"', "/sound/room/": ' id="correction-card"'}


def entry(path: str) -> NavRow:
    """The row for `path`; the first when two rows share one (`/system/`)."""
    for row in NAV:
        if row.path == path:
            return row
    raise KeyError(path)


def _gate_attr(gates: tuple[str, ...]) -> str:
    if len(gates) > 1:
        raise ValueError(f"one data-requires per element, got {gates}")
    return f' data-requires="{gates[0]}" hidden' if gates else ""


def _row_html(row: NavRow, gates: tuple[str, ...]) -> str:
    status_id = f' id="{row.status_id}"' if row.status_id else ""
    return f"""\
          <a class="setting-row{' operator' if row.operator else ''}"\
{_DEAD_IDS.get(row.path, '')} href="{row.path}"{_gate_attr(gates)}>
            <span class="row-icon"><svg aria-hidden="true"><use href="#icon-{row.icon}"></use></svg></span>
            <span class="setting-copy">
              <span class="setting-title">{row.label}</span>
              <span class="setting-status"{status_id}>{row.status_text}</span>
            </span>
            <svg class="chevron" aria-hidden="true"><use href="#icon-chevron"></use></svg>
          </a>"""


def _section_html(group: str, rows: Sequence[NavRow]) -> str:
    first = rows[0].requires[:1]
    shared = first if all(row.requires[:1] == first for row in rows) else ()
    slug = group.lower()
    body = "\n".join(_row_html(row, row.requires[len(shared):]) for row in rows)
    return f"""\
      <section class="settings-section"{_DEAD_IDS.get(group, '')} \
aria-labelledby="{slug}-heading"{_gate_attr(shared)}>
        <h2 class="eyebrow group-title" id="{slug}-heading">{group}</h2>
        <div class="settings-list">
{body}
        </div>
      </section>"""


def landing_groups_html(rows: Sequence[NavRow] = NAV) -> str:
    """The `<nav class="groups">` inner markup, one section per group."""
    groups: dict[str, list[NavRow]] = {}
    for row in rows:
        groups.setdefault(row.group, []).append(row)
    return "\n\n".join(_section_html(g, r) for g, r in groups.items())
