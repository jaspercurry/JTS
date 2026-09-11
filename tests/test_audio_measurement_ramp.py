# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Level sample parsing and validation."""

from __future__ import annotations

import math

import pytest

from jasper.audio_measurement.ramp import LevelSample


def test_level_sample_from_dict_rejects_non_finite():
    with pytest.raises(ValueError, match="non-finite"):
        LevelSample.from_dict({"seq": 1, "rms_dbfs": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        LevelSample.from_dict({"seq": 1, "rms_dbfs": -20.0, "peak_dbfs": float("inf")})


# --- LevelSample parsing -----------------------------------------------------------


def test_level_sample_from_dict_strict_on_rms():
    s = LevelSample.from_dict(
        {"seq": 5, "t_client_ms": 500, "rms_dbfs": -22.0, "peak_dbfs": -18.0}
    )
    assert s.seq == 5 and s.rms_dbfs == -22.0 and s.agc_frozen is True
    with pytest.raises(KeyError):
        LevelSample.from_dict({"seq": 1})  # missing rms_dbfs


def test_level_sample_defaults_peak_to_rms():
    s = LevelSample.from_dict({"rms_dbfs": -30.0})
    assert s.peak_dbfs == -30.0 and math.isclose(s.rms_dbfs, -30.0)
