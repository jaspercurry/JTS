# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What ONE driver branch's emitted chain does to the program (#1808, #1809).

Pure computation: where the driver radiates (:func:`radiating_band_hz`, the fit's band bound) and how much the branch puts above unity (:func:`branch_chain_peak_db`/:func:`branch_headroom_db`, what the emitter must absorb ahead of the split). One module because the CHARGE (``camilla_yaml``) and the PROOF (``runtime_contract``) must agree bit for bit. Everything models the DIGITAL filter the graph runs except :func:`radiating_band_hz`, a policy threshold. ``camilla_yaml``/``runtime_contract`` import this LAZILY (neither pulls numpy today, both load on a 1 GB Pi).
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

# How far down its own crossover a driver is still considered RADIATING, dB (#1809). An ATTENUATION threshold, not Fc: at Fc an LR4 branch is already 6 dB down. 3 dB (half-power) puts an LR4 woofer's edge at 0.801*Fc, tweeter's at 1.248*Fc.
CROSSOVER_EDGE_ATTENUATION_DB: float = 3.0

# Safety margin added to the realized branch peak when charging headroom, dB (#1808). Covers the cascade's between-sample peak plus the emitter's 4-decimal YAML rounding; worst measured residue 0.1913 dB between filter centres, up to 1.7385 dB from 18 kHz to Nyquist where the residue EXCEEDS the margin and the -1.0 dB per-driver soft-clip limiters are the backstop instead (#2850; ADR-0207's widened boost caps widened that ultrasonic exposure rather than closing it). Classifier boost ceiling 14254.4 Hz keeps the honest loop out of that band, pinned by ``test_the_classifier_cannot_vouch_into_the_under_read_band``; a Lowshelf cornered below ~1.9 Hz is tracked separately as unbudgeted (#2846). 1.0 dB equals ``camilla_yaml.BASELINE_LIMITER_CLIP_LIMIT_DB``.
HEADROOM_MARGIN_DB: float = 1.0

# Below this the evaluated peak is treated as "never exceeds unity", dB. The digital biquads evaluate a cascade's analytic zero to a residue of order 1e-4 dB; 0.01 dB is two orders above that.
_PEAK_EPS_DB: float = 0.01

# Domain every chain peak is taken over: essentially DC to Nyquist, appended to every evaluation grid. A shelf's extreme is at an EDGE, not its corner (a 20 Hz/20 kHz grid reads a +12 dB Lowshelf at 30 Hz as 9.69 dB); sampling both edges captures the asymptote exactly.
_GRID_EDGE_LO_HZ: float = 1.0
_GRID_EDGE_HI_HZ: float = 0.4999 * RESPONSE_SAMPLE_RATE_HZ

# Top of the representable band, where a filter may still SIT (not where background samples stop -- see ``_evaluation_grid``).
_NYQUIST_HZ: float = 0.5 * RESPONSE_SAMPLE_RATE_HZ

# BACKGROUND resolution, points per octave. NOT what makes a narrow filter's own peak visible -- ``_evaluation_grid`` unions each filter's exact frequency in for that.
_CHAIN_GRID_POINTS_PER_OCTAVE: int = 48

# Grid every chain peak is evaluated on: 1/48 octave, EDGE TO EDGE. NOT the fit's own 150 Hz-floored ``DEFAULT_ENVELOPE_GRID_HZ``: this grid is read by the runtime contract against an untrusted graph and must see a boost placed at 60 Hz. Full domain, not the audio band, is a correctness requirement (#2758): a shipped-band example peaks 6.8728 dB at 21500.6 Hz, which a 20 Hz-20 kHz background read as 0.8596. Roughly 6 ms per branch for a full 8-filter chain; the cut-only short-circuit below means an ordinary graph pays none of it.
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
    """``grid_hz`` (or :data:`CHAIN_GRID_HZ`) unioned with every filter's own centre frequency and the two domain edges. Tamper hardening, not optional: a peak on a fixed log grid is blind to anything narrower than its spacing (a +12 dB Q-2000 Peaking filter between two bins reads -0.0 dB). A centre goes in at its OWN frequency up to NYQUIST, not merely :data:`_GRID_EDGE_HI_HZ`; one at or above Nyquist is left to the background grid, reading the mirrored extremum to within 0.103 dB. Each adjacent centre pair's geometric midpoint goes in too -- centres alone under-read a between-centres peak by up to 0.58 dB.
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
    """One Linkwitz-Riley section a branch runs through. ``order`` is the LR order the graph emits (:data:`jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`)."""

    fc_hz: float
    order: int
    highpass: bool


def sections_by_role(regions: Iterable[Any]) -> dict[str, tuple[CrossoverSection, ...]]:
    """Map each driver role to the Linkwitz-Riley sections its branch runs through, from a preset's crossover regions. The single derivation for both the session and the emitter, so the two cannot drift apart. A role with no region gets no sections (runs FULL RANGE in the emitted graph). ``regions`` are duck-typed on ``lower_driver``/``upper_driver``/``fc_hz``/``order``, mirroring ``camilla_yaml._emit_baseline_driver_definitions``.
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
    """The magnitude, in dB, that ``sections`` apply across ``freqs_hz``. LR order N is two cascaded Butterworth passes of N/2, built as the DIGITAL sections CamillaDSP realizes. The analog closed form under-reads a branch's true output by up to 1.02 dB (LR4) / 1.58 dB (LR2) at a 10 kHz crossover. Never positive: a Butterworth pass is monotonic and never exceeds unity, so a cut-only chain is known to sit at or below it without evaluating.
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
    """The pole Qs of a Butterworth filter of ``order``, one per biquad section. An odd order's leftover real pole is :func:`_first_order_response`'s, not in this list."""
    return [
        1.0 / (2.0 * math.sin(math.pi * (2 * k + 1) / (2 * order)))
        for k in range(order // 2)
    ]


def _first_order_response(
    freqs_hz: np.ndarray, *, fc_hz: float, highpass: bool,
) -> np.ndarray:
    """One first-order digital section's complex response, the same ``tan`` prewarp ``_biquad_coeffs`` uses. Reached only for an odd Butterworth order."""
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
    """The span where this branch is within ``edge_attenuation_db`` of full output -- the band a linearization fit is allowed to claim (#1809). Solved in closed form from the ANALOG LR prototype, not the digital filter :func:`crossover_response_db` builds: deliberately places the edge slightly INSIDE the real filter's 3 dB point (measured at LR4: -2.98 dB at 2 kHz, -2.43 at 10 kHz), so the fit may add level over slightly less of the spectrum, never more. ``(0.0, inf)`` for no sections. A mid squeezed between two crossovers closer than their own edges returns an EMPTY (``lo > hi``) band. A non-positive ``edge_attenuation_db`` raises.
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


# ka at which a circular piston is taken to be BEAMING outright, named by
# #1675's owner ruling, disclosure only (ADR-0011). ka=2 is roughly -6 dB at
# 45 deg off-axis (checked in
# docs/research/2026-07-23-driver-linearization/03-fact-check.md claim L).
BEAMING_KA = 2.0


def beaming_onset_hz(radiating_diameter_mm: float, *, ka: float = BEAMING_KA) -> float:
    """Frequency at which a piston of this diameter reaches ``ka``. ``f = ka*c / (2*pi*a)``; JTS3 woofer's 114 mm diameter gives 957.7 Hz at ka=1. GEOMETRY, not DSP-fixable (#1675); pinned to match the browser hint's ka=1 value (``kaBeamingOnsetHz`` in deploy/assets/sound-profile/js/main.js). Non-positive input raises.
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
#: :func:`recommended_distance`; chosen so #3501's anchor cases land right
#: (5.5 in/2.5 kHz -> ~12 in, 12 in/500 Hz -> ~25 in, 2.5 in/2.5 kHz -> ~5 in).
K_MARGIN = 2.0

#: Placement slop the operator is held to, metres (+/- 0.5 in); priced by
#: :func:`placement_tolerance_db`.
PLACEMENT_TOLERANCE_M = 0.0127

#: Aim slop that costs nothing measurable in a close capture's validity band
#: (woofer is omnidirectional there).
AIM_TOLERANCE_DEG = 5.0


def far_field_ceiling_hz(
    diameter_m: float,
    distance_m: float,
    *,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> float:
    """Highest frequency at which ``distance_m`` is still the driver's far field. Rayleigh distance ``2*a**2/lambda`` GROWS with frequency, so solving for ``f`` gives a CEILING: near-field at HIGH frequencies, never low ones."""
    radius = 0.5 * float(diameter_m)
    if radius <= 0.0:
        raise ValueError(f"diameter must be positive, got {diameter_m}")
    return float(sound_speed_m_s) * float(distance_m) / (2.0 * radius**2)


def placement_tolerance_db(
    distance_m: float, *, tolerance_m: float = PLACEMENT_TOLERANCE_M
) -> float:
    """MAGNITUDE of the 1/r correction's uncertainty under ``+/- tolerance_m`` of mic placement, dB. An uncertainty, never a signed gain to apply."""
    return 20.0 * math.log10((float(distance_m) + float(tolerance_m)) / float(distance_m))


def recommended_distance(
    diameter_m: float,
    fc_hz: float,
    *,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> dict[str, Any]:
    """Where to put the mic for a close reference of this driver (#3501). ``r = 2*a**2/lambda_top + K_MARGIN*diameter`` at ``f_top = fc/2``; both terms returned separately (margin dominates, far-field term is the correction)."""
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
    """The COMPLEX response a cascade of emitted biquads applies at ``freqs_hz``. ``filters`` are plain ``{biquad_type, freq, q, gain}`` records, the shape the emitter, runtime contract and ``LinearizationFilter.to_dict`` all speak. Every entry goes through the one shared biquad evaluator."""
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
    """The evaluated peak of ``crossover ⊗ linearization ⊗ trim``, dB (#1808). The real gain this branch applies to the program at its loudest frequency; positive means the pre-split headroom has to absorb it. Replaces the per-branch SUM of positive filter gains, which on the 2026-07-28 JTS3 profile charged 22.458 dB against a realized peak of +4.00 dB. ``trim_db`` is the branch's own attenuation (always <= 0), added exactly. Cut-only short-circuit: with no positive filter gain the answer is ``min(0, trim_db)`` without evaluating anything (numpy-free).
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
    """:func:`branch_chain_peak_db`'s answer AND the frequency it occurs at. WHERE a chain peaks says whether it is the requested boost or an unpredicted cascade extremum (#2758's peak sits at 21.5 kHz). ``nan`` on the cut-only short-circuit, never 0.0 (which would read as DC)."""
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
    """Program-domain attenuation a branch peaking at ``peak_db`` needs. ``0.0`` for any chain that never exceeds unity (owner ruling 2026-07-28, #1808); otherwise the peak plus :data:`HEADROOM_MARGIN_DB`."""
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
    """One branch's headroom charge: :func:`headroom_charge_db` of its :func:`branch_chain_peak_db`. The number the household is told, the emitter attenuates by, and the runtime contract proves."""
    return headroom_charge_db(
        branch_chain_peak_db(
            filters, sections=sections, trim_db=trim_db, grid_hz=grid_hz,
        )
    )
