# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read side of the accessory supervisor's status file.

Kept apart from :mod:`jasper.accessories.supervisor` so jasper-control's
doctor row (``jasper.cli.doctor.resilience.check_accessory_bridges``) reads
the file without importing the bridge runtime.

It also owns the vocabulary of a mic adapter's live link facts, for the adapter
that writes them and every reader (issue #3346).
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

# jasper-input's RuntimeDirectory (deploy/systemd/jasper-input.service).
# systemd reaps it on stop, so a file that exists describes the process
# running now; no staleness stamp is needed.
STATUS_PATH = "/run/jasper-input/status.json"

# Why a published accessory mic cannot stream right now. `disconnected` is a
# remote's normal sleep. `unsubscribed` is a connected remote the adapter's
# last attempt could not subscribe to; its retry repeats that attempt.
MIC_NOT_READY_ADAPTER_DOWN = "adapter_down"
MIC_NOT_READY_LINK_UNKNOWN = "link_unknown"
MIC_NOT_READY_DISCONNECTED = "disconnected"
MIC_NOT_READY_UNSUBSCRIBED = "unsubscribed"


def snapshot(status_path: str | os.PathLike = STATUS_PATH) -> dict[str, Any]:
    """``bridges`` maps each bridge to ``restarts`` (failures since the process
    started) and ``last_error`` (the exception class name, set only while the
    bridge waits out its backoff). A bridge that supervises sub-tasks adds its
    own key — the HID bridge's ``readers`` carries one entry per attached
    device node (an unplug drops it), because that bridge stays healthy while
    every reader under it is dead; a mic adapter's ``link`` carries its
    :class:`MicLink` facts.
    Never raises: a missing or unreadable file reads as ``published: False``."""
    try:
        with open(status_path, encoding="utf-8") as f:
            return {"published": True, "bridges": json.load(f)["bridges"]}
    except (OSError, KeyError, TypeError, ValueError):
        return {"published": False, "bridges": {}}


class MicLink:
    """A mic adapter's live facts, published in its jasper-input status entry.

    Set at the end of each attempt to subscribe, so a reconnect goes from
    disconnected to ready in one step. ``connected`` is None until the adapter
    has asked BlueZ, and again after an attempt that could not reach it.
    ``subscribed`` means voice notifications are on. Readers derive readiness
    through :func:`mic_not_ready_reason` only.
    """

    def __init__(self) -> None:
        self.facts: dict[str, bool | None] = {"connected": None, "subscribed": False}
        self._publish: Callable[[], None] = lambda: None

    def register(self, publish: Callable[[], None]) -> dict[str, Any]:
        """The supervisor's detail hook: the shared mapping rides every publish."""
        self._publish = publish
        return {"link": self.facts}

    def update(self, **facts: bool | None) -> None:
        if all(self.facts.get(key) == value for key, value in facts.items()):
            return
        self.facts.update(facts)
        self._publish()


def mic_not_ready_reason(
    source_id: str, snap: Mapping[str, Any] | None = None,
) -> str | None:
    """Why ``source_id``'s adapter cannot stream, or None when it can.

    Ready is connected and subscribed. The adapter runs as the jasper-input
    bridge named after its source id, so a missing entry, or one in restart
    backoff, is an adapter that is not running.
    """
    entry = (snapshot() if snap is None else snap)["bridges"].get(source_id)
    if not isinstance(entry, dict) or entry.get("last_error") is not None:
        return MIC_NOT_READY_ADAPTER_DOWN
    link = entry.get("link")
    if not isinstance(link, dict) or link.get("connected") is None:
        return MIC_NOT_READY_LINK_UNKNOWN
    if not link["connected"]:
        return MIC_NOT_READY_DISCONNECTED
    if not link.get("subscribed"):
        return MIC_NOT_READY_UNSUBSCRIBED
    return None
