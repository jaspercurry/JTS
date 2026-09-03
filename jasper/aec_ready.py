# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AEC-bridge ready marker — the reconciler's published verdict, shared reader.

``jasper-aec-reconcile`` is the single *writer* and
``jasper-aec-bridge.service`` gates on the marker's existence; ADR-0224 owns
the why. Status surfaces (``/state.aec.bridge_ready``, ``jasper-doctor``'s AEC
bridge row) read it here so an absent verdict is diagnosable as "no reconcile
pass has admitted the bridge" rather than as an unexplained dead unit.

The path literal is duplicated in ``deploy/bin/jasper-aec-reconcile`` and that
unit; the agreement is pinned by ``tests/test_aec_bridge_systemd.py``.
``JASPER_AEC_BRIDGE_READY_MARKER`` overrides it (tests, nonstandard layouts)
and must match the override the reconciler reads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Keep in lockstep with the reconciler's AEC_BRIDGE_READY_MARKER default and
# jasper-aec-bridge.service's ConditionPathExists path.
DEFAULT_AEC_BRIDGE_READY_MARKER = "/run/jasper-aec-reconcile/aec-bridge-ready"


def aec_bridge_ready_marker_path() -> str:
    """Resolved marker path (env override wins, for tests/odd layouts)."""
    return os.environ.get(
        "JASPER_AEC_BRIDGE_READY_MARKER", DEFAULT_AEC_BRIDGE_READY_MARKER
    )


@dataclass(frozen=True)
class AecBridgeReady:
    """The published verdict, display-ready.

    ``ready`` is the only fact PID 1 acts on; ``reason`` names the reconcile
    pass that published it, for surfaces that want to say which one.
    """

    ready: bool
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly projection for ``/state`` and other API surfaces."""
        return {
            "ready": self.ready,
            "reason": self.reason,
            "marker": aec_bridge_ready_marker_path(),
        }


def read_aec_bridge_ready() -> AecBridgeReady:
    """Read the marker. Never raises.

    Presence is the whole verdict — the same fact PID 1 acts on — so the body
    is best-effort enrichment and an unreadable one costs only the reason.
    """
    path = Path(aec_bridge_ready_marker_path())
    if not path.exists():
        return AecBridgeReady(ready=False)
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        body = ""
    for line in body.splitlines():
        if line.startswith("reason="):
            return AecBridgeReady(
                ready=True, reason=line[len("reason="):].strip()
            )
    return AecBridgeReady(ready=True)
