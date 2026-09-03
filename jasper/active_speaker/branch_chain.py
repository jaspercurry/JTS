# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What ONE driver branch's emitted chain does to the program (#1808, #1809).

Pure computation. Two questions about a branch that already exists: where
the driver radiates (:func:`radiating_band_hz`, the fit's band bound) and how
much the branch puts above unity (:func:`branch_chain_peak_db` /
:func:`branch_headroom_db`, what the emitter must absorb ahead of the split).
One module because the envelope composer, the disclosure, the CHARGE
(``camilla_yaml``) and the PROOF (``runtime_contract``) must agree bit for
bit; a charge and a proof that disagree by a hair refuse a correct graph on
hardware. Everything models the DIGITAL filter the graph runs except
:func:`radiating_band_hz`, which is a policy threshold and says so.
``camilla_yaml`` and ``runtime_contract`` import this module LAZILY: neither
pulls numpy today and both load on a 1 GB Pi.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.measurement_geometry import METERS_PER_INCH
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.sound.profile import (
    RESPONSE_SAMPLE_RATE_HZ, FilterSpec, _filter_response_complex, _freq_trig,
)

# --------------------------------------------------------------------------- #
# policy constants
# --------------------------------------------------------------------------- #

# How far down its own crossover a driver is still considered to be RADIATING,
# dB — the bound the linearization fit band is clamped to (#1809), so a driver
# never spends filters fighting its own crossover.
#
# An ATTENUATION threshold and not simply Fc: at Fc an LR4 branch is 6 dB down
# by design, so a fit reaching Fc still reads a 6 dB deficit there and boosts
# it. 3 dB is the half-power line, and puts an LR4 woofer's edge at 0.801*Fc
# and its tweeter's at 1.248*Fc. The band between them is owned by the layers
# that measure the SUM (Fc choice, alignment, ripple-optimal trim), not by a
# per-driver magnitude fit that mispredicts a phase-dominated summation.
CROSSOVER_EDGE_ATTENUATION_DB: float = 3.0

# Safety margin added to the realized branch peak when charging program-domain
# headroom, dB (#1808).
#
# Sampling a continuous response can only ever UNDER-read its maximum, and what
# remains to cover is the CASCADE's peak between two adjacent samples plus the
# emitter's 4-decimal YAML rounding of freq/q/gain.
#
# ONE CONVENTION for every number here: under-READ is ``truth - grid read``,
# the sampling error itself; under-CHARGE is that minus 1.0 dB, and is what a
# graph would actually clip by. Quoting the two interchangeably is how a 5.01
# and a 6.01 end up describing one cascade.
#
# The residue has two families, and the between-CENTRES one has THREE axes —
# Q, separation AND centre frequency — so two honest measurements of it differ
# by 0.1 dB (two +6 dB bells at Q 500, 0.1 % apart, under-read 0.0000 dB at
# 1 kHz and 0.0998 dB at 9 kHz). Searched worst found: 0.1913 dB between
# centres, and between BACKGROUND bins 0.0860 dB inside the audio band against
# 1.7385 dB from 18 kHz to Nyquist.
#
# **The bound is a CEILING, chosen above every search's output rather than
# equal to the last one — a hill-climb reports a minimum, never a maximum**,
# and an earlier revision of this comment promoted 0.07 dB on exactly that
# mistake. The same caution applies to the DOMAIN axis: a search that stops at
# 19 kHz reports a ceiling for the band it looked at. So: 0.25 dB below
# ~18 kHz, where the margin covers the residue four times over, and ~2 dB above
# it, where the residue EXCEEDS the margin and the -1.0 dB per-driver soft-clip
# limiters are the backstop instead. Tracked as issue #2850, and the widened
# boost caps (R8; see ADR-0207) made that ultrasonic exposure wider rather than
# closing it.
#
# What keeps the honest loop out of that band is producer-side only: the
# highest frequency the classifier can vouch a boost at is 14254.4 Hz, pinned
# by ``test_the_classifier_cannot_vouch_into_the_under_read_band`` so that
# raising the classifier's ceiling trips a test instead of opening this band.
# A hand-injected 19 kHz boostable verdict is still admitted. One shape is
# outside that ceiling and tracked rather than budgeted for: a Lowshelf
# cornered below ~1.9 Hz, whose asymptote is off the grid entirely (issue #2846).
#
# 1.0 dB is exactly ``camilla_yaml.BASELINE_LIMITER_CLIP_LIMIT_DB``, so a
# correction's loudest bin lands ON the soft-clip limiter's threshold rather
# than into it.
HEADROOM_MARGIN_DB: float = 1.0

# Below this the evaluated peak is treated as "this chain never exceeds
# unity", dB. The digital biquads evaluate a cascade's analytic zero to a
# residue of order 1e-4 dB, and charging the whole margin for that is the
# invisible headroom #1808 exists to stop. 0.01 dB is two orders above the
# residue and 0.1 % in amplitude.
_PEAK_EPS_DB: float = 0.01

# The domain every chain peak is taken over: essentially DC to essentially
# Nyquist, the digital response's own edges. Appended to every evaluation grid
# so a caller-supplied axis carries them too.
#
# A shelf's extreme is at an EDGE, not at its corner: a grid stopping at
# 20 Hz / 20 kHz reads a +12 dB Lowshelf cornered at 30 Hz as 9.69 dB, past
# the margin. Sampling the two edges captures both asymptotes exactly. Outside
# the audible band on purpose — what is bounded is what the FILTER can do to
# the signal.
_GRID_EDGE_LO_HZ: float = 1.0
_GRID_EDGE_HI_HZ: float = 0.4999 * RESPONSE_SAMPLE_RATE_HZ

# The top of the representable band — where a filter may still SIT, which is
# not where the background samples stop. ``_evaluation_grid`` says why the two
# are different numbers.
_NYQUIST_HZ: float = 0.5 * RESPONSE_SAMPLE_RATE_HZ

# The BACKGROUND resolution of the grid below, points per octave — the
# crossover's smooth shape and whatever a cascade does between its filters'
# centres. It is NOT what makes a narrow filter's own peak visible:
# ``_evaluation_grid`` unions each filter's exact frequency into it for that,
# because no fixed resolution can bound an arbitrary Q.
_CHAIN_GRID_POINTS_PER_OCTAVE: int = 48

# The grid every chain peak is evaluated on: 1/48 octave, EDGE TO EDGE.
#
# Deliberately NOT the fit's own ``DEFAULT_ENVELOPE_GRID_HZ`` (150 Hz floor):
# this grid is read by the runtime contract against a graph it does not trust,
# and a grid that starts at 150 Hz cannot see a boost placed at 60 Hz.
#
# The span is the whole domain and not the audio band, and that is a
# correctness requirement (#2758): the two edge points capture a monotonic
# shelf's asymptote but say nothing about an extremum BETWEEN an edge and the
# band, and a mixed-sign cascade puts one there — a shipped-band example peaks
# 6.8728 dB at 21500.6 Hz, which a 20 Hz - 20 kHz background read as 0.8596.
# One span for gate, charge and re-proof is what keeps them one construction.
#
# Roughly 6 ms per branch for a full 8-filter chain, on a path that runs once
# per config emit and once per branch at graph re-proof; the cut-only
# short-circuit below means the ordinary graph pays none of it.
CHAIN_GRID_HZ: np.ndarray = np.geomspace(
    _GRID_EDGE_LO_HZ,
    _GRID_EDGE_HI_HZ,
    round(
        _CHAIN_GRID_POINTS_PER_OCTAVE
        * math.log2(_GRID_EDGE_HI_HZ / _GRID_EDGE_LO_HZ)
    ) + 1,
)
CHAIN_GRID_HZ.flags.writeable = False


def _evaluation_grid(
    filters: Sequence[Mapping[str, Any]], grid_hz: np.ndarray | None,
) -> np.ndarray:
    """``grid_hz`` (or :data:`CHAIN_GRID_HZ`) unioned with every filter's own
    centre frequency and the two domain edges.

    **Tamper hardening, and it is not optional.** A peak evaluated on a fixed
    log grid is blind to anything narrower than its own spacing: a +12 dB
    Peaking filter at Q 2000, placed between two 1/48-octave bins, reads
    -0.0 dB and would prove SAFE against a graph that charged nothing for it.
    No honest fit can emit that, but the runtime contract reads graphs it does
    not trust, and "the emitter would never write this" is not a proof.

    A centre goes in at its OWN frequency all the way to NYQUIST, not merely up
    to :data:`_GRID_EDGE_HI_HZ`: the bilinear prewarp compresses frequency
    without bound near Nyquist, so a +12 dB Q-8 Peaking filter at 23999 Hz
    delivers its full 12 dB there while measuring 0.0120 dB at 23995.2 Hz. A
    centre at or above Nyquist is left to the background grid on measurement
    rather than omission — the digital response mirrors about Nyquist, and the
    1/48-octave background reads the mirrored extremum to within 0.103 dB.

    The geometric midpoint of each adjacent centre pair goes in too: a cascade's
    peak can sit BETWEEN two centres, and centres alone under-read that family
    by up to 0.58 dB. The remaining residue is owned by
    :data:`HEADROOM_MARGIN_DB`'s own comment.
    """
    base = CHAIN_GRID_HZ if grid_hz is None else np.asarray(grid_hz, dtype=np.float64)
    extra = [_GRID_EDGE_LO_HZ, _GRID_EDGE_HI_HZ]
    centres = sorted(
        freq for entry in filters
        if 0.0 < (freq := float(entry.get("freq") or 0.0)) < _NYQUIST_HZ
    )
    extra.extend(centres)
    extra.extend(
        math.sqrt(lower * upper) for lower, upper in zip(centres, centres[1:])
    )
    return np.unique(np.concatenate([base, np.asarray(extra, dtype=np.float64)]))


@dataclass(frozen=True)
class CrossoverSection:
    """One Linkwitz-Riley section a branch runs through.

    ``order`` is the LR order the graph emits
    (:data:`jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`).
    """

    fc_hz: float
    order: int
    highpass: bool


def sections_by_role(regions: Iterable[Any]) -> dict[str, tuple[CrossoverSection, ...]]:
    """Map each driver role to the Linkwitz-Riley sections its branch runs
    through, from a preset's crossover regions.

    The single derivation for the session (which bounds the fit's lift band)
    and the emitter (which charges ``active_baseline_headroom``): as two they
    had already drifted, in the direction that makes a disclosure smaller than
    its own charge. A role with no region gets no sections rather than an
    invented crossover — the emitter builds its filters from these same
    regions, so such a role runs FULL RANGE in the emitted graph, radiating
    everywhere and attenuating nothing.

    ``regions`` are duck-typed on ``lower_driver`` / ``upper_driver`` /
    ``fc_hz`` / ``order``; the assignment mirrors
    ``camilla_yaml._emit_baseline_driver_definitions``.
    """
    out: dict[str, list[CrossoverSection]] = {}
    for region in regions:
        fc_hz = float(getattr(region, "fc_hz", 0.0))
        order = int(getattr(region, "order", 0))
        lower = getattr(region, "lower_driver", None)
        upper = getattr(region, "upper_driver", None)
        if lower is not None:
            out.setdefault(str(lower), []).append(
                CrossoverSection(fc_hz=fc_hz, order=order, highpass=False)
            )
        if upper is not None:
            out.setdefault(str(upper), []).append(
                CrossoverSection(fc_hz=fc_hz, order=order, highpass=True)
            )
    return {role: tuple(sections) for role, sections in out.items()}


def confirmed_protection_sections(
    safety_profile: Mapping[str, Any], role_targets: Mapping[str, str],
) -> dict[str, tuple[CrossoverSection, ...]]:
    """Resolve confirmed role protection; unrepresentable shapes fail closed."""
    targets = safety_profile.get("targets")
    if not isinstance(targets, list):
        raise ValueError("confirmed safety profile has no target list")
    out: dict[str, tuple[CrossoverSection, ...]] = {}
    for role, fingerprint in sorted(role_targets.items()):
        matches = [
            target for target in targets
            if isinstance(target, Mapping)
            and target.get("role") == role
            and target.get("target_fingerprint") == fingerprint
        ]
        if len(matches) != 1:
            raise ValueError(f"confirmed protection target is not unique for {role}")
        raw_filters = matches[0].get("required_protection_filters")
        if not isinstance(raw_filters, list):
            raise ValueError(f"confirmed protection filters are missing for {role}")
        sections: list[CrossoverSection] = []
        for raw in raw_filters:
            if not isinstance(raw, Mapping) or raw.get("kind") not in {
                "highpass", "lowpass",
            }:
                raise ValueError(f"confirmed protection filter is invalid for {role}")
            cutoff = float(raw.get("cutoff_hz", 0.0))
            slope = float(raw.get("minimum_slope_db_per_octave", 0.0))
            order = next((value for value in (2, 4, 8) if value * 6.0 >= slope), 0)
            if not (math.isfinite(cutoff) and math.isfinite(slope)) or cutoff <= 0 or not order:
                raise ValueError(f"confirmed protection filter is unsupported for {role}")
            sections.append(CrossoverSection(cutoff, order, raw["kind"] == "highpass"))
        out[role] = tuple(sections)
    return out


def crossover_response_db(
    freqs_hz: np.ndarray, sections: Sequence[CrossoverSection],
) -> np.ndarray:
    """The magnitude, in dB, that ``sections`` apply across ``freqs_hz``.

    Linkwitz-Riley of order N is two cascaded Butterworth passes of order N/2,
    built here as the DIGITAL sections CamillaDSP realizes through the same RBJ
    evaluator the linearization filters use. The analog closed form
    ``|H_lp| = 1 / (1 + (f/fc)^N)`` under-reads a branch's true output by up to
    1.02 dB (LR4) and 1.58 dB (LR2) for a 10 kHz crossover — the whole of
    :data:`HEADROOM_MARGIN_DB`, in the loud direction.

    Never positive: a Butterworth pass is monotonic and never exceeds unity,
    which is what lets a cut-only chain be known to sit at or below unity
    without evaluating it.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    total_db = np.zeros(freqs.shape, dtype=np.float64)
    for section in sections:
        pass_magnitude = np.sqrt(
            np.abs(crossover_response_complex(freqs, (section,)))
        )
        total_db += 40.0 * np.log10(np.maximum(pass_magnitude, 1e-12))
    return total_db


def crossover_response_complex(
    freqs_hz: np.ndarray, sections: Sequence[CrossoverSection],
) -> np.ndarray:
    """Exact complex response of the digital Linkwitz-Riley ``sections``."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    total = np.ones(freqs.shape, dtype=np.complex128)
    for section in sections:
        fc_hz = max(float(section.fc_hz), 1e-9)
        # Butterworth order per pass; the pass runs twice, hence the doubling.
        butterworth_order = max(int(section.order), 1) // 2 or 1
        biquads = [
            {
                "biquad_type": "Highpass" if section.highpass else "Lowpass",
                "freq": fc_hz, "q": q, "gain": 0.0,
            }
            for q in _butterworth_qs(butterworth_order)
        ]
        pass_response = chain_response(biquads, freqs)
        if butterworth_order % 2:
            pass_response = pass_response * _first_order_response(
                freqs, fc_hz=fc_hz, highpass=section.highpass
            )
        total = total * pass_response * pass_response
    return total


def _butterworth_qs(order: int) -> list[float]:
    """The pole Qs of a Butterworth filter of ``order``, one per biquad
    section. An odd order's leftover real pole is the first-order section
    :func:`_first_order_response` adds, and is not in this list."""
    return [
        1.0 / (2.0 * math.sin(math.pi * (2 * k + 1) / (2 * order)))
        for k in range(order // 2)
    ]


def _first_order_response(
    freqs_hz: np.ndarray, *, fc_hz: float, highpass: bool,
) -> np.ndarray:
    """One first-order digital section's complex response — the bilinear
    transform at ``RESPONSE_SAMPLE_RATE_HZ`` with the same ``tan`` prewarp
    ``_biquad_coeffs`` uses, so a first-order pass and a biquad pass agree
    about where the corner is. Reached only for an odd Butterworth order.
    """
    k = math.tan(math.pi * fc_hz / RESPONSE_SAMPLE_RATE_HZ)
    b0 = (k / (1.0 + k)) if not highpass else (1.0 / (1.0 + k))
    b1 = b0 if not highpass else -b0
    a1 = (k - 1.0) / (k + 1.0)
    z = np.exp(
        -1j * 2.0 * np.pi
        * np.asarray(freqs_hz, dtype=np.float64) / RESPONSE_SAMPLE_RATE_HZ
    )
    return (b0 + b1 * z) / (1.0 + a1 * z)


def radiating_band_hz(
    sections: Sequence[CrossoverSection],
    *,
    edge_attenuation_db: float = CROSSOVER_EDGE_ATTENUATION_DB,
) -> tuple[float, float]:
    """The span where this branch is within ``edge_attenuation_db`` of full
    output — the band a linearization fit is allowed to claim (#1809).

    Solved in closed form from the ANALOG Linkwitz-Riley prototype, not the
    digital filter :func:`crossover_response_db` builds, and the difference is
    deliberate: it places the edge slightly INSIDE the real filter's 3 dB point
    (measured at LR4: -2.98 dB at a 2 kHz corner, -2.43 at 10 kHz), so the band
    is narrower than a true 3 dB span and the fit may add level over slightly
    less of the spectrum, never more. That is the right way for a policy
    threshold to be wrong; the charge is modelled digitally for the opposite
    reason. The two passes are reciprocal — an LR pair's radiating bands mirror
    about Fc in log frequency.

    ``(0.0, inf)`` for a branch with no sections. Multiple sections intersect,
    and a mid squeezed between two crossovers closer together than their own
    edges honestly returns an EMPTY (``lo > hi``) band: it never reaches full
    output anywhere, so it gets no lift. A non-positive ``edge_attenuation_db``
    raises — a 0 dB edge would put a lowpass branch's bound at DC.
    """
    if edge_attenuation_db <= 0.0:
        raise ValueError(
            f"edge_attenuation_db must be positive (got {edge_attenuation_db})"
        )
    lo_hz, hi_hz = 0.0, math.inf
    ratio = 10.0 ** (edge_attenuation_db / 20.0)
    for section in sections:
        order = max(int(section.order), 1)
        if section.highpass:
            r = 1.0 / ratio
            lo_hz = max(lo_hz, section.fc_hz * (r / (1.0 - r)) ** (1.0 / order))
        else:
            hi_hz = min(hi_hz, section.fc_hz * (ratio - 1.0) ** (1.0 / order))
    return lo_hz, hi_hz


# The ka value at which a circular piston is taken to be BEAMING outright — the
# one #1675's owner ruling names, and disclosure only (ADR-0011). A documented
# heuristic, not a measured edge: ka = 1 is the classic onset of narrowing and
# ka = 2 is roughly -6 dB at 45 deg off-axis (arithmetic checked in
# docs/research/2026-07-23-driver-linearization/03-fact-check.md claim L).
# A POLICY choice about where guidance starts — the physics is continuous.
BEAMING_KA = 2.0


def beaming_onset_hz(radiating_diameter_mm: float, *, ka: float = BEAMING_KA) -> float:
    """Frequency at which a piston of this diameter reaches ``ka``.

    ``ka`` is wavenumber times radius: ``f = ka*c / (2*pi*a)``. For the JTS3
    woofer's declared 114 mm effective diameter that is 957.7 Hz at ka = 1.

    This is GEOMETRY: a directivity step at the crossover survives any on-axis
    EQ, so a consumer may use this to prefer a lower crossover or to warn, and
    must not present it as something the DSP will fix (#1675). Deliberately
    unrounded, and pinned by test to round to the browser hint's own ka=1 value
    (``kaBeamingOnsetHz`` in deploy/assets/sound-profile/js/main.js). A
    non-positive diameter or ``ka`` raises rather than inventing a ceiling.
    """
    if not math.isfinite(radiating_diameter_mm) or radiating_diameter_mm <= 0.0:
        raise ValueError(
            f"radiating diameter must be positive (got {radiating_diameter_mm})"
        )
    if not math.isfinite(ka) or ka <= 0.0:
        raise ValueError(f"ka must be positive (got {ka})")
    radius_m = float(radiating_diameter_mm) / 2000.0
    return ka * DEFAULT_SOUND_SPEED_M_S / (2.0 * math.pi * radius_m)


#: Driver diameters of margin added to the piston far-field distance by
#: :func:`recommended_distance`. Chosen so #3501's three anchor cases land
#: where it says: 5.5 in / 2.5 kHz -> ~12 in, 12 in / 500 Hz -> ~25 in,
#: 2.5 in / 2.5 kHz -> ~5 in. The DOMINANT term at every one of them; the
#: far-field term is a 0.3-1.4 in correction on top.
K_MARGIN = 2.0

#: Placement slop the operator is held to, metres (+/- 0.5 in). Only the 1/r
#: correction cares, and :func:`placement_tolerance_db` prices it.
PLACEMENT_TOLERANCE_M = 0.0127

#: Aim slop that costs nothing measurable in a close capture's validity band:
#: the woofer is omnidirectional there, so +/-5 deg is free.
AIM_TOLERANCE_DEG = 5.0


def far_field_ceiling_hz(
    diameter_m: float,
    distance_m: float,
    *,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> float:
    """Highest frequency at which ``distance_m`` is still the driver's far field.

    The piston far-field (Rayleigh) distance is ``2*a**2/lambda`` for aperture
    RADIUS ``a`` and GROWS with frequency, so solving for ``f`` gives a
    CEILING: a close mic is near-field at HIGH frequencies, never at low ones.
    """
    radius = 0.5 * float(diameter_m)
    if radius <= 0.0:
        raise ValueError(f"diameter must be positive, got {diameter_m}")
    return float(sound_speed_m_s) * float(distance_m) / (2.0 * radius**2)


def placement_tolerance_db(
    distance_m: float, *, tolerance_m: float = PLACEMENT_TOLERANCE_M
) -> float:
    """MAGNITUDE of the 1/r correction's uncertainty under ``+/- tolerance_m``
    of mic placement, dB. An uncertainty, never a signed gain to apply."""
    return 20.0 * math.log10((float(distance_m) + float(tolerance_m)) / float(distance_m))


def recommended_distance(
    diameter_m: float,
    fc_hz: float,
    *,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Where to put the mic for a close reference of this driver (#3501).

    ``r = 2*a**2/lambda_top + K_MARGIN*diameter``, evaluated at the top of the
    close capture's validity band (``f_top = fc/2``). Both terms are returned
    separately: the margin dominates and the far-field term is the correction.
    """
    diameter = float(diameter_m)
    if diameter <= 0.0:
        raise ValueError(f"diameter must be positive, got {diameter_m}")
    if fc_hz <= 0.0:
        raise ValueError(f"fc must be positive, got {fc_hz}")
    f_top = 0.5 * float(fc_hz)
    lambda_top = float(sound_speed_m_s) / f_top
    radius = 0.5 * diameter
    far_field_m = 2.0 * radius**2 / lambda_top
    margin_m = K_MARGIN * diameter
    distance_m = far_field_m + margin_m
    return {
        "driver_diameter_m": diameter,
        "driver_diameter_in": diameter / METERS_PER_INCH,
        "fc_hz": float(fc_hz),
        "band_top_hz": f_top,
        "wavelength_top_m": lambda_top,
        "far_field_term_m": far_field_m,
        "margin_term_m": margin_m,
        "k_margin": K_MARGIN,
        "distance_m": distance_m,
        "distance_in": distance_m / METERS_PER_INCH,
        "direct_gain_over_1m_db": 20.0 * math.log10(1.0 / distance_m),
        "placement_tolerance_m": PLACEMENT_TOLERANCE_M,
        "placement_tolerance_db": placement_tolerance_db(distance_m),
        "aim_tolerance_deg": AIM_TOLERANCE_DEG,
        "far_field_ceiling_hz": far_field_ceiling_hz(
            diameter, distance_m, sound_speed_m_s=sound_speed_m_s
        ),
        "sound_speed_m_s": float(sound_speed_m_s),
    }


def chain_response(
    filters: Sequence[Mapping[str, Any]], freqs_hz: np.ndarray,
) -> np.ndarray:
    """The COMPLEX response a cascade of emitted biquads applies at ``freqs_hz``.

    ``filters`` are plain ``{biquad_type, freq, q, gain}`` records — the reduced
    shape the emitter, the runtime contract and ``LinearizationFilter.to_dict``
    all speak. Every entry goes through :func:`jasper.sound.profile.
    _filter_response_complex`: there is exactly one biquad evaluator in this
    codebase and this is a caller of it, not a second one.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    trig = _freq_trig(freqs)
    total = np.ones(freqs.shape, dtype=np.complex128)
    for entry in filters:
        spec = FilterSpec(
            name="chain",
            biquad_type=str(entry.get("biquad_type") or "Peaking"),
            freq=float(entry.get("freq") or 0.0),
            gain=float(entry.get("gain") or 0.0),
            q=float(entry.get("q") or 0.0),
        )
        total = total * np.array(_filter_response_complex(spec, freqs, trig))
    return total


def branch_chain_peak_db(
    filters: Sequence[Mapping[str, Any]],
    *,
    sections: Sequence[CrossoverSection] = (),
    trim_db: float = 0.0,
    grid_hz: np.ndarray | None = None,
) -> float:
    """The evaluated peak of ``crossover ⊗ linearization ⊗ trim``, dB (#1808).

    The real gain this branch applies to the program at its loudest frequency;
    positive means the pre-split headroom has to absorb it. Replaces the
    per-branch SUM of positive filter gains, which on the 2026-07-28 JTS3
    profile charged 22.458 dB against a realized peak of +4.00 dB and left the
    speaker 8.3 dB below the household's listening level at maximum volume.

    ``trim_db`` is the branch's own attenuation (the emitted per-driver Gain,
    always <= 0), added exactly. **Cut-only short-circuit:** with no positive
    filter gain the answer is ``min(0, trim_db)`` without evaluating anything,
    which keeps every cut-only graph on a numpy-free path.
    """
    return branch_chain_peak(
        filters, sections=sections, trim_db=trim_db, grid_hz=grid_hz,
    )[0]


def branch_chain_peak(
    filters: Sequence[Mapping[str, Any]],
    *,
    sections: Sequence[CrossoverSection] = (),
    trim_db: float = 0.0,
    grid_hz: np.ndarray | None = None,
) -> tuple[float, float]:
    """:func:`branch_chain_peak_db`'s answer AND the frequency it occurs at.

    WHERE a chain peaks is what says whether it is the boost somebody asked for
    or a cascade extremum nobody predicted (#2758's own peak sits at 21.5 kHz,
    outside every band its filters name). The frequency is ``nan`` on the
    cut-only short-circuit, which evaluates no grid — never 0.0, which would
    read as DC.
    """
    if not any(float(f.get("gain") or 0.0) > 0.0 for f in filters):
        return min(0.0, float(trim_db)), math.nan
    grid = _evaluation_grid(filters, grid_hz)
    magnitude_db = 20.0 * np.log10(
        np.maximum(np.abs(chain_response(filters, grid)), 1e-12)
    )
    magnitude_db = magnitude_db + crossover_response_db(grid, sections) + float(trim_db)
    index = int(np.argmax(magnitude_db))
    return float(magnitude_db[index]), float(grid[index])


def headroom_charge_db(peak_db: float) -> float:
    """Program-domain attenuation a branch peaking at ``peak_db`` needs.

    ``0.0`` for any chain that never exceeds unity: a correction that cannot
    clip costs the speaker nothing (owner ruling 2026-07-28, #1808). Otherwise
    the peak plus :data:`HEADROOM_MARGIN_DB`.
    """
    if peak_db <= _PEAK_EPS_DB:
        return 0.0
    return float(peak_db) + HEADROOM_MARGIN_DB


def branch_headroom_db(
    filters: Sequence[Mapping[str, Any]],
    *,
    sections: Sequence[CrossoverSection] = (),
    trim_db: float = 0.0,
    grid_hz: np.ndarray | None = None,
) -> float:
    """One branch's headroom charge: :func:`headroom_charge_db` of its
    :func:`branch_chain_peak_db`. The number the household is told, the number
    the emitter attenuates by, and the number the runtime contract proves.
    """
    return headroom_charge_db(
        branch_chain_peak_db(
            filters, sections=sections, trim_db=trim_db, grid_hz=grid_hz,
        )
    )
