# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared measurement-session primitives.

Small, hardware-conscious helpers used by browser-mic calibration flows. Keep
flow-specific analysis in the owning subsystem; this package owns reusable
session evidence and guard rails.
"""

