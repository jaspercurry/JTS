# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared shape for "a bonded DUMB member this box can actually serve"."""

from __future__ import annotations

from typing import Any

from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_PERIOD_FRAMES


def bonded_grouping_env(cfg: Any, **kw: Any) -> dict[str, str]:
    """`outputd_grouping_env` for a box whose period carries the return ring.

    The production signature is fail-closed (an unresolved period never arms),
    which every call site that means "a servable box" would otherwise restate.
    The DAC-profile matrix test drives the real per-profile periods instead.
    """
    from jasper.multiroom.reconcile import outputd_grouping_env

    kw.setdefault("outputd_period_frames", DAC_CONTENT_RING_PERIOD_FRAMES)
    return outputd_grouping_env(cfg, **kw)
