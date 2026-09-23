# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Named evidence edges in Hz; room outer edges follow coverage and ceiling."""

import math
from collections.abc import Sequence
from types import MappingProxyType

from jasper.audio_measurement.room_boundary import GATED_SPEC_LOWER_EDGE_HZ

UPPER_BANDS_HZ = ((350.0, 700.0), (700.0, 1500.0), (1500.0, 5000.0))
LEVEL_BANDS_HZ = ((30.0, 60.0), (60.0, 100.0), (90.0, 350.0), (200.0, 300.0), *UPPER_BANDS_HZ)
LATE_ENERGY_BAND_HZ = (90.0, 250.0)
# jts3 cardioid null-band default, third-octave edges around the 100–350 Hz target (ADR-0316).
ARRIVAL_GAP_BAND_HZ = (90.0, 315.0)
BASS_BANDS_HZ = ((20.0, 30.0), (30.0, 40.0), (40.0, 50.0), (50.0, 63.0),
                 (63.0, 80.0), (80.0, 100.0), (100.0, 125.0), (125.0, 160.0), (160.0, 200.0))
_THIRD_OCTAVE_CENTERS_HZ = (20.0, 25.0, 31.5, 40.0, 50.0, 63.0,
                            80.0, 100.0, 125.0, 160.0, 200.0)
_THIRD_OCTAVE_EDGE_FACTOR = 2.0 ** (1.0 / 6.0)
THIRD_OCTAVE_BASS_BANDS_HZ = tuple(
    (center / _THIRD_OCTAVE_EDGE_FACTOR, center * _THIRD_OCTAVE_EDGE_FACTOR)
    for center in _THIRD_OCTAVE_CENTERS_HZ
)
OCTAVE_BAND_CENTERS_HZ = (31.5, 63.0, 125.0, 250.0, 500.0, 1000.0,
                          2000.0, 4000.0, 8000.0, 16000.0)
OCTAVE_BANDS_HZ = tuple(
    (center / math.sqrt(2.0), center * math.sqrt(2.0)) for center in OCTAVE_BAND_CENTERS_HZ
)
# Only the interior edges are fixed; the measured floor and room ceiling bound them.
ROOM_BAND_SPLITS_HZ = (60.0, 120.0)
BEST_EFFORT_ABOVE_HZ = 16000.0
SPEC_BANDS = ((GATED_SPEC_LOWER_EDGE_HZ, 2000.0, 1.5), (2000.0, 8000.0, 2.0), (8000.0, BEST_EFFORT_ABOVE_HZ, 2.5))
SPEC_BAND_EDGES_HZ = tuple((lo, hi) for lo, hi, _ in SPEC_BANDS)
# Static capture-quality edges; the room correction ceiling may vary (room-correction-regime-plan.md).
SNR_BANDS_HZ = (("sub_bass", 20.0, 80.0), ("bass", 80.0, 160.0),
                ("upper_bass", 160.0, 350.0), ("transition", 350.0, 1000.0))
CROSSOVER_SNR_BANDS_HZ = (*SNR_BANDS_HZ, ("mid", 1000.0, 4000.0), ("treble", 4000.0, 12000.0))

# Low-end plot ladder (frequency_plot.py's per-decade band-mean rows). A flat
# edge list, not lo/hi pairs, so it is not a BAND_LADDERS entry below.
LOW_BANDS_HZ = (20, 30, 40, 50, 60, 80, 120, 200, 500)

# One 0 dB reference/normalisation band per site. Each fixes its own edges for
# its own purpose; they are not interchangeable and must not be merged.
PLOT_REFERENCE_BAND_HZ = (200.0, 5000.0)
EXCESS_PHASE_NORMALISE_BAND_HZ = (400.0, 8000.0)
BASS_FIT_REFERENCE_BAND_HZ = (300.0, 1000.0)
GATE_SWEEP_REFERENCE_BAND_HZ = (2500.0, 8000.0)
SERIES_STATS_TILT_BAND_HZ = (100.0, 10000.0)
SERIES_STATS_FLATNESS_BAND_HZ = (400.0, 10000)

BAND_LADDERS = MappingProxyType({
    "rear_upper": UPPER_BANDS_HZ,
    "rear_level": LEVEL_BANDS_HZ,
    "rear_late_energy": (LATE_ENERGY_BAND_HZ,),
    "rear_arrival_gap": (ARRIVAL_GAP_BAND_HZ,),
    "bass": BASS_BANDS_HZ,
    "third_octave_bass": THIRD_OCTAVE_BASS_BANDS_HZ,
    "octave": OCTAVE_BANDS_HZ,
    "room": ROOM_BAND_SPLITS_HZ,  # Splits, not edges; outer edges follow coverage and ceiling; band_ladder_name never matches this entry.
    "speaker_spec": SPEC_BAND_EDGES_HZ,
    "snr": tuple((lo, hi) for _, lo, hi in SNR_BANDS_HZ),
    "crossover_snr": tuple((lo, hi) for _, lo, hi in CROSSOVER_SNR_BANDS_HZ),
})


def band_ladder_name(bands_hz: Sequence[Sequence[float]]) -> str | None:
    """Identify fixed edges; caller-defined bands have no registry name."""
    edges = tuple(tuple(band) for band in bands_hz)
    return next((name for name, fixed in BAND_LADDERS.items() if edges == fixed), None)
