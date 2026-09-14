# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Polling cadence for tuning clients."""


def next_poll_s(current_s: float, *, changed: bool, initial_s: float) -> float:
    """Reset on change; otherwise double up to 15 s or a slower explicit override."""
    return initial_s if changed else min(current_s * 2, max(15.0, initial_s))
