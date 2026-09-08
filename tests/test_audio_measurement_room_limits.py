# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The room candidate's code-computed limits (ADR-0256, regime plan D5).

Pins the four claims a room prescription is checked against: the spread only
ever REDUCES cut depth, the correction returns to flat over the third-octave
below the ceiling, the cut floor and boost cap both carry that taper, and a
boost is admitted only on spatial evidence.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement import room_limits

#: An arbitrary ceiling: it arrives as an argument, never from a constant.
CEILING_HZ = 300.0
KNEE_HZ = CEILING_HZ / 2.0 ** room_limits.ROOM_TAPER_OCTAVES
#: Half way down the taper, in log-frequency.
MID_TAPER_HZ = KNEE_HZ * 2.0 ** (room_limits.ROOM_TAPER_OCTAVES / 2.0)


def _room_median() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A 7-position cloud on a 1/12-octave grid, 20-400 Hz.

    Four features, one per admission outcome: a 8 dB dip at 45 Hz five
    positions agree on, a 14 dB null at 90 Hz, a 8 dB dip at 160 Hz only two
    positions see, and a single-bin 6 dB notch at 250 Hz.
    """
    freqs = 20.0 * 2.0 ** (np.arange(53) / 12.0)
    median = np.zeros_like(freqs)
    for centre_hz, depth_db in ((45.0, 8.0), (90.0, 14.0), (160.0, 8.0)):
        median -= depth_db * np.exp(-(np.log2(freqs / centre_hz) ** 2) / (2 * 0.2 ** 2))
    median[int(np.argmin(np.abs(freqs - 250.0)))] -= 6.0

    deviations = np.zeros((7, freqs.size))
    # A position that does not see a dip sits 6 dB above the median there:
    # its own level is above the presence floor AND its deviation disagrees.
    deviations[5:, np.abs(np.log2(freqs / 45.0)) < 0.35] = 6.0
    deviations[2:, np.abs(np.log2(freqs / 160.0)) < 0.35] = 6.0
    return freqs, median, deviations


def test_ceiling_taper_is_whole_below_the_knee_and_gone_at_the_ceiling():
    taper = room_limits.ceiling_taper(
        np.array([20.0, KNEE_HZ, MID_TAPER_HZ, CEILING_HZ, 1000.0]), CEILING_HZ
    )
    assert taper[0] == 1.0
    assert taper[1] == 1.0
    assert taper[2] == pytest.approx(0.5)
    assert taper[3] == pytest.approx(0.0, abs=1e-12)
    assert taper[4] == 0.0

    interior = room_limits.ceiling_taper(
        np.geomspace(KNEE_HZ * 1.001, CEILING_HZ * 0.999, 32), CEILING_HZ
    )
    assert np.all(np.diff(interior) < 0.0)
    assert np.all((interior > 0.0) & (interior < 1.0))


def test_allowed_depth_is_whole_inside_the_tolerance_and_scaled_outside():
    tolerable = room_limits.TOLERABLE_STD_DB
    spread = np.array([0.0, tolerable / 2.0, tolerable, 2.0 * tolerable, 4.0 * tolerable])

    fraction = room_limits.depth_fraction(spread)
    assert list(fraction[:3]) == [1.0, 1.0, 1.0]
    assert fraction[3] == pytest.approx(0.5)

    depth = room_limits.allowed_depth_db(spread)
    assert list(depth[:3]) == [room_limits.ROOM_MAX_CUT_DB] * 3
    assert depth[3] == pytest.approx(room_limits.ROOM_MAX_CUT_DB / 2.0)
    assert depth[4] == pytest.approx(room_limits.ROOM_MAX_CUT_DB / 4.0)
    assert np.all(depth <= 0.0)


def test_cut_floor_and_boost_cap_carry_the_taper():
    freqs = np.array([50.0, KNEE_HZ, MID_TAPER_HZ, CEILING_HZ])
    spread = np.array([2.0, 2.0, 2.0 * room_limits.TOLERABLE_STD_DB, 2.0])

    floor = room_limits.cut_floor_db(spread, freqs, CEILING_HZ)
    assert floor[0] == pytest.approx(room_limits.ROOM_MAX_CUT_DB)
    assert floor[1] == pytest.approx(room_limits.ROOM_MAX_CUT_DB)
    # Half the depth the spread allows, halved again by the taper.
    assert floor[2] == pytest.approx(room_limits.ROOM_MAX_CUT_DB / 4.0)
    assert floor[3] == pytest.approx(0.0, abs=1e-12)

    cap = room_limits.boost_cap_db(freqs, CEILING_HZ)
    assert cap[0] == pytest.approx(room_limits.ROOM_MAX_FILTER_BOOST_DB)
    assert cap[2] == pytest.approx(room_limits.ROOM_MAX_FILTER_BOOST_DB / 2.0)
    assert cap[3] == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize(
    ("freq_hz", "n_positions", "reason"),
    [
        (45.0, 7, ""),
        (45.0, 2, "too_few_positions"),
        (320.0, 7, "dip_too_shallow"),
        (90.0, 7, "dip_too_deep"),
        (250.0, 7, "dip_too_narrow"),
        (160.0, 7, "insufficient_presence"),
    ],
)
def test_admit_boost_reasons(freq_hz, n_positions, reason):
    freqs, median, deviations = _room_median()

    finding = room_limits.admit_boost(
        freq_hz,
        freqs_hz=freqs,
        median_db=median,
        deviations_db=deviations[:n_positions],
        n_positions=n_positions,
    )

    assert (finding.admitted, finding.reason) == (not reason, reason)
    assert finding.evaluated_at_hz == pytest.approx(freq_hz, rel=0.02)
    assert finding.n_positions == n_positions
    assert set(finding.to_dict()) == {
        "freq_hz",
        "evaluated_at_hz",
        "n_positions",
        "n_present",
        "presence_fraction",
        "depth_db",
        "width_octaves",
        "admitted",
        "reason",
    }


def test_admit_boost_refuses_unusable_evidence_instead_of_raising():
    freqs, median, deviations = _room_median()

    finding = room_limits.admit_boost(
        45.0,
        freqs_hz=freqs,
        median_db=median,
        deviations_db=deviations[:, :-1],
        n_positions=7,
    )

    assert not finding.admitted
    assert finding.reason == "dip_too_shallow"

