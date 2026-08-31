# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for assertions over deploy/shairport-sync.conf.template."""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SHAIRPORT_TEMPLATE = REPO / "deploy" / "shairport-sync.conf.template"


def template_value(text: str, setting: str) -> str:
    """The one live numeric assignment for ``setting``, as written."""
    matches = re.findall(
        rf"^\s*{re.escape(setting)}\s*=\s*([0-9]+(?:\.[0-9]+)?);\s*$",
        text,
        re.MULTILINE,
    )
    assert len(matches) == 1, f"expected one live {setting} assignment"
    return matches[0]


def template_string_value(text: str, setting: str) -> str:
    """The one live quoted assignment for ``setting``, without quotes."""
    matches = re.findall(
        rf'^\s*{re.escape(setting)}\s*=\s*"([^"]*)";\s*$',
        text,
        re.MULTILINE,
    )
    assert len(matches) == 1, f"expected one live {setting} assignment"
    return matches[0]
