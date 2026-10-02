# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver gain envelope shared by measurement fitting and playback."""

# Fitting must not propose a trim that configured playback cannot admit.
DRIVER_TRIM_MIN_DB = -60.0
