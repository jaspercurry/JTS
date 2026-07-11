# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Digital excitation contract shared by automatic acoustic measurements.

The automatic level tone and the ESS it calibrates must have the same source
peak.  Keeping that value here prevents a quiet/loud handoff between the level
stage and the measurement stage.  Per-driver attenuation is a separate,
explicit graph gain recorded in the active-speaker excitation ledger.
"""

AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS = -12.0
