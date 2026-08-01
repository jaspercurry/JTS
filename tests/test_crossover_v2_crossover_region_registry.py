# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#1967 / #1867: the crossover region is ASKED about, and the answer never
gates.

The defect these pin: the null registry's search band is floored at 4 kHz, so
on a 2 kHz crossover the region carrying the residual "isn't 'uncertain', it
was never asked" — while the cloud screen separately carves it out of grading.

The load-bearing half of the fix is the half that does NOT happen: the
extension's findings are unioned into no mask, the 4 kHz floor does not move,
and the gating registry is byte-identical. Each of those is a test here,
because "we added a read-only surface" is exactly the kind of claim that decays
into a gating one two refactors later.
"""
from __future__ import annotations

import types

import numpy as np
import pytest

import jasper.active_speaker.crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2_flow import (
    ECHO_BAND_HF_REGIME_FLOOR_HZ,
    _crossover_region_null_registry,
    committed_crossover_region_hz,
)


def _region(fc_hz: float):
    return types.SimpleNamespace(
        fc_hz=fc_hz, order=4, lower_driver="woofer", upper_driver="tweeter",
    )


# --------------------------------------------------------------------------- #
# committed_crossover_region_hz
# --------------------------------------------------------------------------- #


def test_no_committed_crossover_names_no_region():
    """A speaker with no committed region has no handoff, so there is nothing
    to ask about — and nothing is invented, the same rule
    ``sections_by_role`` follows."""
    assert committed_crossover_region_hz(()) is None


def test_the_region_is_one_octave_either_side_of_the_committed_fc():
    assert committed_crossover_region_hz([_region(2000.0)]) == (1000.0, 4000.0)


def test_a_three_way_region_spans_every_committed_crossover():
    lo, hi = committed_crossover_region_hz([_region(400.0), _region(2500.0)])
    assert lo == pytest.approx(200.0)
    assert hi == pytest.approx(5000.0)


def test_a_region_with_no_usable_fc_is_ignored():
    """A malformed region contributes nothing rather than a 0 Hz edge that
    would make the extension band start at DC."""
    assert committed_crossover_region_hz([_region(0.0)]) is None


# --------------------------------------------------------------------------- #
# the extension band itself
# --------------------------------------------------------------------------- #


def _stub_report(band_hz):
    """Every field ``_null_registry_to_dict`` reads, and nothing else — so a
    serializer change surfaces here rather than in a mock that silently
    tolerated it."""
    return types.SimpleNamespace(
        nulls=(), excluded_bands_hz=(), excluded_fraction=0.0, refusals=(),
        reason="corroborated_arrivals", classification="interference_null",
        band_hz=tuple(band_hz), tau_ladder_us=303.0, arrival_tau_us=303.0,
        arrival_r_time=0.28, arrival_r_max=0.31, n_corroborating=2,
        r_freq=0.27, agreement=0.9, ladder_arrival_gap=0.01, capped=False,
        min_depth_db=6.0, n_candidates=2,
        excluded=np.ones(8, dtype=bool),
    )


class _Identify:
    """Records the band it was asked about and returns a stub report."""

    def __init__(self):
        self.bands: list[tuple[float, float]] = []

    def __call__(self, combined, *, band_hz):
        self.bands.append((float(band_hz[0]), float(band_hz[1])))
        return _stub_report(band_hz)


def _run(*, region, echo_band=(ECHO_BAND_HF_REGIME_FLOOR_HZ, 18000.0)):
    identify = _Identify()
    block = _crossover_region_null_registry(
        object(), echo_band_hz=echo_band,
        crossover_region_hz=region, identify=identify,
    )
    return block, identify


def test_the_extension_reaches_the_crossover_region_the_floor_hid():
    """The whole point: a 2 kHz crossover's region starts at 1000 Hz, and the
    gating band starts at 4000 Hz. The extension asks about 1000 Hz."""
    block, identify = _run(region=(1000.0, 4000.0))
    assert block is not None
    assert identify.bands == [(1000.0, 18000.0)]
    assert block["band_hz"] == [1000.0, 18000.0]


def test_the_extension_keeps_the_gating_bands_upper_edge():
    """Extending rather than carving a narrow window is deliberate: the
    detector's quefrency step is ``1e6 / bandwidth``, so a narrow window would
    be a differently-resolved instrument reporting in the same units."""
    block, _identify = _run(region=(1000.0, 4000.0), echo_band=(4000.0, 16000.0))
    assert block is not None
    assert block["band_hz"][1] == 16000.0


def test_nothing_is_disclosed_when_the_gating_band_already_reached_the_region():
    """No hidden region, nothing to disclose — ``None``, not an empty block a
    reader would have to interpret."""
    block, identify = _run(region=(1000.0, 4000.0), echo_band=(500.0, 18000.0))
    assert block is None
    assert identify.bands == []


def test_nothing_is_disclosed_without_a_committed_crossover():
    block, identify = _run(region=None)
    assert block is None
    assert identify.bands == []


def test_a_detector_failure_in_the_extension_never_breaks_the_session():
    """A classify-only surface may not take a session down. The gating
    registry has already run by this point and is unaffected."""

    def boom(combined, *, band_hz):
        raise RuntimeError("detector exploded")

    assert _crossover_region_null_registry(
        object(), echo_band_hz=(4000.0, 18000.0),
        crossover_region_hz=(1000.0, 4000.0), identify=boom,
    ) is None


# --------------------------------------------------------------------------- #
# the half that must NOT happen
# --------------------------------------------------------------------------- #


def test_the_block_says_out_loud_that_it_never_gates():
    """Spelled out in the payload rather than implied by absence from a mask
    the reader cannot see from there."""
    block, _identify = _run(region=(1000.0, 4000.0))
    assert block is not None
    assert block["gating"] is False
    assert block["regime"] == "uncalibrated_below_hf_floor"
    assert block["hf_regime_floor_hz"] == ECHO_BAND_HF_REGIME_FLOOR_HZ
    assert "never a reason to exclude a band" in block["why"]


def test_the_hf_regime_floor_did_not_move():
    """#1763's measured protection is untouched. The six-band sweep behind
    this number found a FALSE NEGATIVE at a 2 kHz edge — not a narrowed gap —
    because the woofer's own passband sits inside the analysed band there. The
    unclamp adds a read; it does not relitigate the floor.
    """
    assert ECHO_BAND_HF_REGIME_FLOOR_HZ == 4000.0


def test_the_extension_is_unioned_into_no_mask():
    """**The load-bearing test.** The extension excludes every bin it was
    handed, and neither the merged honesty mask nor the spec mask may change
    because of it.

    Run through the real ``assemble_cloud_group_result`` twice on one
    ``combined`` — once with a committed crossover and once without — and
    require the gating outputs to be identical. A future edit that quietly
    unions the extension in fails here.
    """
    from tests.test_attribution_persistence import _combined

    combined = _combined()
    kw = dict(
        echo_band_hz=(ECHO_BAND_HF_REGIME_FLOOR_HZ, 18000.0),
        validity_floor_hz=None, tier="reference",
    )
    without = flow.assemble_cloud_group_result(combined, **kw)
    with_region = flow.assemble_cloud_group_result(
        combined, crossover_region_hz=(1000.0, 4000.0), **kw,
    )

    assert without["available"] and with_region["available"]
    # The extension appeared…
    assert without["null_registry_crossover_region"] is None
    assert with_region["null_registry_crossover_region"] is not None
    assert with_region["null_registry_crossover_region"]["gating"] is False
    # …and changed nothing that decides anything.
    for key in (
        "null_registry", "spec", "flatness", "carve_outs",
        "merged_excluded_bands_hz", "screen_excluded_bands_hz",
        "echo_band_hz", "geometry",
    ):
        assert with_region[key] == without[key], key
