# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small, dependency-free retry schedules shared across subsystems."""
from __future__ import annotations

import random


RECONNECT_INITIAL_BACKOFF_SEC = 1.0
RECONNECT_MAX_BACKOFF_SEC = 60.0
RECONNECT_BACKOFF_JITTER_FRACTION = 0.25


def reconnect_backoff_delay(attempt: int) -> float:
    """Return a capped exponential reconnect delay with ±25% jitter.

    ``attempt`` is one-indexed. The exponent is saturated because callers may
    retry forever; after the cap is reached, larger powers cannot affect the
    result and should not grow without bound.
    """
    shift = min(attempt - 1, 32)
    base = min(
        RECONNECT_INITIAL_BACKOFF_SEC * (2 ** shift),
        RECONNECT_MAX_BACKOFF_SEC,
    )
    jitter = base * RECONNECT_BACKOFF_JITTER_FRACTION
    return base + random.uniform(-jitter, jitter)
