# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver-measurement defaults: the sweep's length and level and the crossover-null threshold."""

from __future__ import annotations

from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.quality_model import DRIVER

DEFAULT_DURATION_S = 6.0
# Level tone and ESS share one source peak; acoustic level is then governed by
# the locked main volume and the applied per-role baseline gain.
DEFAULT_AMPLITUDE_DBFS = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

DEFAULT_NULL_THRESHOLD_DB = DRIVER.null_threshold_db  # deep crossover null = "present"
