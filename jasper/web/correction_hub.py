# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared chrome for the HTTPS correction measurement hub."""

from __future__ import annotations

import html

SECTIONS = (
    ("room", "Room", "/correction/room/"),
    # Label only (#1670) — "Active speaker" is the household-facing name for
    # what is still, internally, the crossover wizard: slug/href/every
    # internal identifier stay "crossover" (docs/active-speaker-tuning-
    # layers-design.md decision 1, "the surface gets a more honest name").
    ("crossover", "Active speaker", "/correction/crossover/"),
    ("bass", "Bass", "/correction/bass/"),
)


def section_tabs(active: str) -> str:
    buttons = []
    for key, label, href in SECTIONS:
        current = ' aria-current="page"' if key == active else ""
        buttons.append(
            '<a class="segmented__btn"'
            f'{current} href="{html.escape(href, quote=True)}">'
            f'{html.escape(label)}</a>'
        )
    return (
        '<nav class="segmented" aria-label="Correction measurement type">'
        + "".join(buttons)
        + "</nav>"
    )
