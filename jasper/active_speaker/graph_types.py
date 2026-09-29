# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

GRAPH_FLAT_FULL_RANGE = "flat_full_range"
GRAPH_ALL_MUTED_ACTIVE_STARTUP = "all_muted_active_startup"
GRAPH_GUARDED_COMMISSIONING = "guarded_commissioning"
GRAPH_APPROVED_ACTIVE_RUNTIME = "approved_active_runtime"
GRAPH_DRIVER_DOMAIN_BASELINE = "driver_domain_baseline"
GRAPH_PROGRAM_BAKE_PIPE = "program_bake_pipe"
GRAPH_PARKED_ALL_MUTED = "parked_all_muted"
GRAPH_UNKNOWN = "unknown"
GRAPH_UNSAFE = "unsafe"

# A program peak at or under this is unity, dB: it is left uncharged, and it is
# the verifier's slack on a charged peak. The emitter spells every gain,
# frequency and q to 4 decimals, so a graph charged exactly can read a hair
# above unity after the YAML round-trip; an analytic 0 dB reads about 1e-4.
PEAK_EPS_DB: float = 1e-3


@dataclass(frozen=True)
class GraphSafety:
    classification: str
    allowed: bool
    config_path: str | None = None
    camilla_classification: str = "missing"
    playback_device: str | None = None
    playback_channels: int | None = None
    issues: tuple[dict[str, str], ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "allowed": self.allowed,
            "config_path": self.config_path,
            "camilla_classification": self.camilla_classification,
            "playback_device": self.playback_device,
            "playback_channels": self.playback_channels,
            "issues": list(self.issues),
            "details": self.details,
        }
