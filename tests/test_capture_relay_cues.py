# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the phone-mic relay failure-cue classifier (step 7).

No-silent-failure: a measurement that cannot complete says so audibly. These
tests pin which cue each failure maps to, and the registry's set-equality guard
(tests/test_cue_registry_coverage.py) pins that both cues are actually wired.
"""
from __future__ import annotations

import urllib.error

from jasper.capture_relay import cues
from jasper.capture_relay.client import RelayError
from jasper.capture_relay.session import CaptureFailed, CaptureTimeout


def test_connectivity_errors_map_to_relay_unreachable():
    assert (
        cues.classify_failure_cue(urllib.error.URLError("no route"))
        == cues.RELAY_UNREACHABLE_CUE_SLUG
    )
    assert (
        cues.classify_failure_cue(OSError("connection refused"))
        == cues.RELAY_UNREACHABLE_CUE_SLUG
    )
    assert (
        cues.classify_failure_cue(RelayError("relay down", 503))
        == cues.RELAY_UNREACHABLE_CUE_SLUG
    )


def test_usable_failures_map_to_measurement_failed():
    # A measurement that started but cannot be used -> generic retry cue.
    assert (
        cues.classify_failure_cue(CaptureTimeout("timed out"))
        == cues.MEASUREMENT_FAILED_CUE_SLUG
    )
    assert (
        cues.classify_failure_cue(CaptureFailed("bad integrity"))
        == cues.MEASUREMENT_FAILED_CUE_SLUG
    )
    # A 4xx is the relay rejecting a request, not unreachable -> generic.
    assert (
        cues.classify_failure_cue(RelayError("unauthorized", 401))
        == cues.MEASUREMENT_FAILED_CUE_SLUG
    )


def test_slugs_are_registered_cue_slugs():
    from jasper.cues.registry import CUES

    registered = {c.slug for c in CUES}
    assert cues.RELAY_UNREACHABLE_CUE_SLUG in registered
    assert cues.MEASUREMENT_FAILED_CUE_SLUG in registered
