# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins for the active-speaker commission-tone owner."""

from __future__ import annotations


import jasper.active_speaker.web_commissioning as web_commissioning
import jasper.web.correction_crossover_backend as correction_backend


def test_correction_routes_through_web_commissioning_owner():
    assert correction_backend.web_commissioning is web_commissioning
