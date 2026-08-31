# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""NBD and SM (Olive 2004 / US 8,311,232 B2) — ticket 6.13, ADR-0202.

Every synthetic fixture below is spaced so the module's own
:data:`~jasper.audio_measurement.olive_metrics.OLIVE_SMOOTHING_FRACTION`
(1/20-octave) pass is a verified per-sample IDENTITY: consecutive
frequencies differ by at least 10%, while ``smooth_fractional_octave``'s
1/20-octave window reaches only ``2**(1/40) - 1`` (~1.75%) either side of a
sample. That isolation is what makes the ripple case's NBD value hand-
computable rather than merely re-running the smoother under another name.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement.olive_metrics import (
    NBD_BAND_OCTAVES,
    OLIVE_SMOOTHING_FRACTION,
    nbd,
    sm,
)


def test_nbd_is_zero_and_sm_is_one_on_a_flat_curve():
    """A perfectly flat curve has no band-deviation and a perfect line fit."""

    grid = np.geomspace(200.0, 8000.0, 40)
    flat_db = np.full_like(grid, -20.0)

    nbd_result = nbd(grid, flat_db, (200.0, 8000.0))
    sm_result = sm(grid, flat_db, (200.0, 8000.0))

    assert nbd_result.nbd_db == pytest.approx(0.0, abs=1e-9)
    assert sm_result.sm_r2 == pytest.approx(1.0, abs=1e-9)
    assert nbd_result.band_hz == (200.0, 8000.0)
    assert nbd_result.smoothing_fraction == OLIVE_SMOOTHING_FRACTION
    assert nbd_result.band_octaves == NBD_BAND_OCTAVES
    assert nbd_result.n_samples == 40
    assert sm_result.n_samples == 40


def test_nbd_matches_a_hand_computed_value_on_a_synthetic_ripple():
    """Two 1/2-octave bands (1000-1414.21.. and 1414.21..-2000 Hz), three
    isolated samples each. Fine-smoothing is identity by construction (see
    module docstring), so each band's own mean and mean-absolute-deviation
    are plain arithmetic on the values written into this test:

      band 1 [1000, 1100, 1210] Hz -> db [0, 2, -2], mean 0, MAD (0+2+2)/3
      band 2 [1500, 1650, 1815] Hz -> db [0, 4, -4], mean 0, MAD (0+4+4)/3

    NBD = mean(4/3, 8/3) = 2.0 exactly.
    """

    freqs = np.array([1000.0, 1100.0, 1210.0, 1500.0, 1650.0, 1815.0])
    magnitude_db = np.array([0.0, 2.0, -2.0, 0.0, 4.0, -4.0])

    result = nbd(freqs, magnitude_db, (1000.0, 2000.0))

    assert result.nbd_db == pytest.approx(2.0)
    assert result.n_bands == 2
    assert result.n_samples == 6
    assert result.band_hz == (1000.0, 2000.0)


def test_nbd_and_sm_are_band_clamped():
    """A huge out-of-band outlier must not move either metric or its
    sample count — only ``freqs_hz`` inside ``band_hz`` may be scored.

    ``grid[0] == 50.0`` sits an order of magnitude below the 200 Hz band
    edge, and every grid step is ~9%, so (per the module docstring) no
    sample's fine-smoothing window reaches across the 50 Hz / 200 Hz gap:
    the outlier cannot bleed into a scored sample regardless of its value.
    """

    grid = np.geomspace(50.0, 8000.0, 60)
    flat_db = np.full_like(grid, -20.0)
    flat_db[0] = 500.0  # the outlier, at grid[0] == 50.0 Hz, below the band
    band_hz = (200.0, 8000.0)
    expected_n = int(np.sum(grid >= band_hz[0]))

    nbd_result = nbd(grid, flat_db, band_hz)
    sm_result = sm(grid, flat_db, band_hz)

    assert nbd_result.nbd_db == pytest.approx(0.0, abs=1e-9)
    assert sm_result.sm_r2 == pytest.approx(1.0, abs=1e-9)
    assert nbd_result.n_samples == expected_n
    assert sm_result.n_samples == expected_n


@pytest.mark.parametrize("func", [nbd, sm])
def test_a_band_selecting_no_sample_raises(func):
    freqs = np.array([1000.0, 2000.0, 3000.0])
    magnitude_db = np.zeros_like(freqs)
    with pytest.raises(ValueError):
        func(freqs, magnitude_db, (10.0, 20.0))
