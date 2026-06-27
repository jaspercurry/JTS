# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Failure-cue slugs for the phone-mic capture relay (step 7).

JTS's no-silent-failure doctrine: a measurement that cannot complete must say so
audibly, because the household is standing at the listening position with their
phone (plan §12, AGENTS.md "No silent failure paths"). These constants name the
cues registered in `jasper/cues/registry.py`; the `*_CUE_SLUG` naming is what the
cue-registry coverage guard recognizes as a play site. `classify_failure_cue`
maps a transport/capture failure to the right cue so the host (the per-flow
adapter, or `run_capture`'s injected `play_cue`) plays it on the failure path.
"""
from __future__ import annotations

# A connectivity failure reaching the relay at all → "couldn't reach the service".
RELAY_UNREACHABLE_CUE_SLUG = "measurement_relay_unreachable"
# A measurement started but cannot be used (timeout / decrypt / integrity /
# alignment / phone aborted) → "that didn't work, try again".
MEASUREMENT_FAILED_CUE_SLUG = "measurement_failed"


def classify_failure_cue(error: BaseException) -> str:
    """Map a relay/capture failure to the cue the household should hear.

    A network-reachability error (the Pi cannot reach the relay — `URLError` /
    `OSError`, or a relay 5xx/0) is `measurement_relay_unreachable`; every other
    failure (timeout, decrypt, integrity, alignment, abort, 4xx) is the generic
    `measurement_failed`.
    """
    from jasper.capture_relay.client import RelayError

    if isinstance(error, OSError):
        # URLError subclasses OSError; raw socket failures are OSError too.
        return RELAY_UNREACHABLE_CUE_SLUG
    if isinstance(error, RelayError) and (error.status == 0 or error.status >= 500):
        return RELAY_UNREACHABLE_CUE_SLUG
    return MEASUREMENT_FAILED_CUE_SLUG
