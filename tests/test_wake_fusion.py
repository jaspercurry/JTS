"""Unit tests for the wake-leg fire-decision seam (WakeFuser)."""
from __future__ import annotations

import pytest

from jasper.wake_fusion import WakeFuser


def test_empty_fuser_is_pure_or_gate():
    # Phase 1.2 default: no offsets -> the effective threshold IS the base
    # threshold for every leg/condition. This is the behavior-preservation
    # contract (the historical global-threshold OR-gate, unchanged).
    f = WakeFuser()
    for leg in ("on", "off", "dtln"):
        for cond in ("quiet", "ambient", "music"):
            assert f.effective_threshold(leg, cond, 0.5) == 0.5


def test_offset_raises_one_leg_condition_threshold():
    # Phase 1.3 shape: an offset for one (leg, condition) raises just that
    # bar; every other leg/condition stays at base.
    f = WakeFuser(offsets={("off", "music"): 0.1})
    assert f.effective_threshold("off", "music", 0.5) == pytest.approx(0.6)
    assert f.effective_threshold("off", "quiet", 0.5) == 0.5
    assert f.effective_threshold("on", "music", 0.5) == 0.5


def test_negative_offset_lowers_threshold():
    f = WakeFuser(offsets={("on", "quiet"): -0.05})
    assert f.effective_threshold("on", "quiet", 0.5) == pytest.approx(0.45)


def test_unknown_pair_falls_back_to_base():
    f = WakeFuser(offsets={("off", "music"): 0.1})
    assert f.effective_threshold("dtln", "ambient", 0.42) == 0.42
