# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persist + restore jasper-mux's source-selection mode across restarts.

The landing page's Source selector can pin the speaker to one renderer
lane (manual mode) instead of latest-source-wins (auto mode). That pin
lived only in `Mux._manual_source` (in-memory), so it silently reverted
to Auto on every jasper-mux restart — and mux restarts on every deploy
(the unit has `Restart=always`). A household that pinned "AirPlay only"
would find themselves back on Auto after the next code push, with no
user-visible event. This module makes the pin durable.

What's persisted: the mode (auto vs manual) and, when manual, the
selected source label (`Source.value`, e.g. "airplay"). Auto mode
persists as `{"mode": "auto"}` with no source.

File format (JSON, atomic tmp+rename via jasper.atomic_io):

    {"mode": "manual", "selected_source": "airplay"}
    {"mode": "auto"}

Fail-open to Auto. A missing, unreadable, malformed, or unknown-source
file resolves to `None` (auto / no pin) — exactly today's behaviour.
Better to fall back to latest-source-wins than to refuse to start or
get stuck pinned to a bogus source after one bad byte on disk.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_text
from .music_sources import MUSIC_SOURCES, Source

logger = logging.getLogger(__name__)


DEFAULT_PATH = "/var/lib/jasper/mux_mode.json"


def read_manual_source(path: str | Path) -> Source | None:
    """Return the persisted manual-pin source, or None for auto / no pin.

    None means latest-source-wins (auto). It's returned for every
    fail-open case: missing file, unreadable file, malformed JSON,
    `mode != "manual"`, a missing/blank source, or a source label that
    isn't a selectable music source. Callers treat None as "no pin".
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("mux mode persistence: read %s failed (%s)", p, e)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("mux mode persistence: parse %s failed (%s)", p, e)
        return None
    if not isinstance(data, dict):
        logger.warning(
            "mux mode persistence: %s is not a JSON object; treating as auto",
            p,
        )
        return None
    if data.get("mode") != "manual":
        # Auto mode (or anything that isn't explicitly manual) → no pin.
        return None
    label = data.get("selected_source")
    if not isinstance(label, str) or not label:
        logger.warning(
            "mux mode persistence: %s is manual but has no source; "
            "treating as auto",
            p,
        )
        return None
    try:
        source = Source(label)
    except ValueError:
        logger.warning(
            "mux mode persistence: %s has unknown source %r; treating as auto",
            p, label,
        )
        return None
    if source not in MUSIC_SOURCES:
        logger.warning(
            "mux mode persistence: %s source %r is not selectable; "
            "treating as auto",
            p, label,
        )
        return None
    return source


def write_mode(path: str | Path, manual_source: Source | None) -> None:
    """Best-effort atomic write of the current mode. Logs on failure but
    does not raise — losing the persistence write must not crash a source
    switch. Pass the manually-pinned source for manual mode, or None for
    auto mode."""
    payload: dict[str, Any] = {"mode": "auto"}
    if manual_source is not None:
        payload = {"mode": "manual", "selected_source": manual_source.value}
    try:
        atomic_write_text(path, json.dumps(payload) + "\n", mode=0o644)
    except OSError as e:
        logger.warning(
            "mux mode persistence: write to %s failed (%s)", path, e,
        )
