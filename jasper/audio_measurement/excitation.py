# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Digital excitation contract shared by automatic acoustic measurements.

The automatic level tone and the ESS it calibrates must have the same source
peak.  Keeping that value here prevents a quiet/loud handoff between the level
stage and the measurement stage.  Per-driver attenuation is a separate,
explicit graph gain recorded in the active-speaker excitation ledger.

The silences an excitation keeps, and the sweeps a take plays, live here too,
with no numpy, so the measurement registry checks a plan's stimulus against
them on the web's import path.
"""

AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS = -12.0

# Deconvolution window pre-guard, s, before the scheduled sweep position;
# shared by both drivers so their IR peaks land pre-guard sample +/- delay.
DECONV_PRE_GUARD_S = 0.25

# Longest silence, s, after a one-driver take's first sound, so a
# signal-sensing amplifier (jts3's TPA3255) never drops into standby mid-take
# (ADR-0360 §4, #5684).
NEAR_FIELD_SILENCE_S = 0.5

# Sweeps each driver plays in one take, unless its preset row or its pose
# states its own (ADR-0434). Repeats past the first are bit-identical and feed
# the in-capture drift and glitch estimator (#1668).
SWEEPS_PER_TAKE = 3
