# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A rigid piston's ka geometry (#1675) and its far-field ceiling."""

from __future__ import annotations

import math

import pytest

from jasper.audio_measurement.piston import BEAMING_KA, beaming_onset_hz, far_field_ceiling_hz

# The JTS3 declaration, so the numbers below are the ones the owner's speaker
# actually produces rather than a synthetic shape.
JTS3_DIAMETER_MM = 114.0


def test_beaming_onset_is_the_ka_closed_form_at_the_declared_diameter():
    """ka = 2*pi*f*a/c, so f = ka*c/(2*pi*a). The two numbers the owner ruling
    quotes for the 114 mm declaration, re-derived rather than restated."""
    assert beaming_onset_hz(JTS3_DIAMETER_MM, ka=1.0) == pytest.approx(957.7, abs=0.05)
    assert beaming_onset_hz(JTS3_DIAMETER_MM, ka=2.0) == pytest.approx(1915.4, abs=0.05)
    # ka is linear in f, so doubling ka doubles the frequency exactly.
    assert beaming_onset_hz(JTS3_DIAMETER_MM, ka=2.0) == pytest.approx(
        2.0 * beaming_onset_hz(JTS3_DIAMETER_MM, ka=1.0)
    )
    # …and inversely proportional to the diameter: a cone twice as wide beams
    # an octave lower. This is the whole content of the prior.
    assert beaming_onset_hz(2.0 * JTS3_DIAMETER_MM) == pytest.approx(
        0.5 * beaming_onset_hz(JTS3_DIAMETER_MM)
    )
    assert BEAMING_KA == 2.0


def test_beaming_onset_agrees_with_the_browsers_component_entry_hint():
    """The browser hint (kaBeamingOnsetHz) rounds ka=1 to a whole Hz so its
    displayed "2x" is exact. This value is unrounded and must round TO it, so
    the two surfaces cannot quote different geometry for one declaration."""
    for diameter_mm in (25.0, 114.0, 140.0, 165.0, 200.0):
        js_ka1 = round(343.0 / (2.0 * math.pi * (diameter_mm / 2000.0)))
        assert round(beaming_onset_hz(diameter_mm, ka=1.0)) == js_ka1


def test_beaming_onset_refuses_a_dimension_nobody_declared():
    """No conservative default: inventing a diameter would manufacture a
    beaming ceiling out of nothing, and #1675 derives this FROM a declaration."""
    for bad in (0.0, -114.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            beaming_onset_hz(bad)
    with pytest.raises(ValueError):
        beaming_onset_hz(JTS3_DIAMETER_MM, ka=0.0)


def test_the_far_field_criterion_is_a_ceiling_not_a_floor():
    """A close mic is near-field at HIGH frequencies, never at low ones: the
    Rayleigh distance grows with frequency, so solving it for f bounds above."""
    near = far_field_ceiling_hz(0.1397, 0.30)
    far = far_field_ceiling_hz(0.1397, 1.00)
    assert far > near
    # Twice the aperture radius is four times the Rayleigh distance.
    assert far_field_ceiling_hz(2.0 * 0.1397, 1.00) == pytest.approx(0.25 * far)
    with pytest.raises(ValueError):
        far_field_ceiling_hz(0.0, 1.00)
