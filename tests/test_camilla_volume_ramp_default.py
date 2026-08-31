# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The graph-swap duck's settle budget assumes CamillaDSP's default ramp.

`CamillaController._graph_mutation` ducks the main fader, waits
`MAIN_VOLUME_RAMP_SETTLE_S`, then swaps the graph. That wait only covers the
fade while `devices.volume_ramp_time` is unset, which is CamillaDSP 4.1.3's
400 ms default. A generator that started emitting a longer ramp would swap
mid-fade — silently, and in the loud direction — so the claim is pinned here
rather than left as a comment.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from jasper.camilla import MAIN_VOLUME_RAMP_SETTLE_S

# CamillaDSP 4.1.3 README, `volume_ramp_time`: "If left out or set to `null`,
# it defaults to 400 ms."
CAMILLA_DEFAULT_VOLUME_RAMP_MS = 400.0

_REPO = Path(__file__).resolve().parent.parent


def test_settle_budget_covers_camilladsp_default_ramp() -> None:
    assert MAIN_VOLUME_RAMP_SETTLE_S * 1000.0 >= CAMILLA_DEFAULT_VOLUME_RAMP_MS


def test_shipped_camilla_configs_leave_volume_ramp_time_at_the_default() -> None:
    configs = sorted((_REPO / "deploy" / "camilladsp").glob("*.yml"))
    assert configs, "expected at least one shipped CamillaDSP config"
    for config in configs:
        devices = yaml.safe_load(config.read_text(encoding="utf-8")).get(
            "devices", {}
        )
        ramp_ms = devices.get("volume_ramp_time")
        assert ramp_ms is None or float(ramp_ms) <= (
            MAIN_VOLUME_RAMP_SETTLE_S * 1000.0
        ), (
            f"{config.name} sets volume_ramp_time={ramp_ms} ms, longer than "
            "the graph-swap duck's settle — the swap would land mid-fade"
        )


def test_no_camilla_config_generator_emits_volume_ramp_time() -> None:
    """Shipped YAML is only half the surface: correction, sound, and
    active-speaker configs are generated at runtime by these emitters.

    `deploy/bin` is scanned alongside `jasper/` because the operator-side
    scripts there rewrite CamillaDSP configs too (jasper-camilla-recover,
    the crossover and pipe guards), and because AirPlay's volume hook now
    lands on the fader whose ramp this budget covers (ADR-0206).
    """
    # Match the key as an emitter would write it — a quoted dict key or a
    # line of YAML template text — so prose mentioning it does not trip.
    written = ('"volume_ramp_time"', "'volume_ramp_time'", "volume_ramp_time:")
    scanned = [
        *(_REPO / "jasper").rglob("*.py"),
        *(p for p in (_REPO / "deploy" / "bin").iterdir() if p.is_file()),
    ]
    # Tolerant decode: deploy/bin holds only scripts today, but the scan must
    # not become a hard error the day something binary lands beside them. A
    # byte that will not decode cannot spell the key anyway.
    offenders = sorted(
        str(path.relative_to(_REPO))
        for path in scanned
        if any(
            token in path.read_text(encoding="utf-8", errors="replace")
            for token in written
        )
    )
    assert offenders == [], (
        f"{offenders} now emit volume_ramp_time; re-check "
        "MAIN_VOLUME_RAMP_SETTLE_S against the value they write"
    )
