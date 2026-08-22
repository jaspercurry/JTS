# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What ONE driver branch's emitted chain does to the program (#1808, #1809).

Pure computation: numpy plus the emitted graph's own RBJ biquad evaluator
(:func:`jasper.sound.profile._filter_response_complex`). No I/O, no policy
about *what* to correct — this module answers two questions about a branch
that already exists:

* **Where does this driver radiate?** — :func:`radiating_band_hz`, the span
  where the branch's own crossover leaves it within
  :data:`CROSSOVER_EDGE_ATTENUATION_DB` of full output. The linearization fit
  is bounded to it so a driver never spends filters fighting its own
  crossover (#1809).
* **How much does this branch put above unity?** — :func:`branch_chain_peak_db`
  / :func:`branch_headroom_db`, the evaluated peak of ``crossover ⊗
  linearization ⊗ trim``. That peak is what the emitter must absorb ahead of
  the split, and charging anything larger is invisible loudness the household
  never gets back (#1808).

**Why one module for both.** They are the same object — the emitted branch
chain — read two ways, and FOUR call sites need them to agree bit for bit:
the envelope composer (``crossover_v2.intervention.plan_linearization``), the
disclosure (:attr:`~jasper.active_speaker.linearization_fit.LinearizationFit.
headroom_cost_db`), the CHARGE
(``camilla_yaml.linearization_headroom_db``, which sets
``active_baseline_headroom``), and the PROOF
(``runtime_contract._consume_linearization_chain``, which re-derives it from
the emitted YAML). A charge and a proof that disagree by a hair refuse a
correct graph on hardware; two implementations of one number is exactly the
drift this file exists to remove. :func:`sections_by_role` is here for the
same reason and not a smaller one: the session and the emitter each derived
role -> crossover independently for one review cycle and had already
disagreed about the no-region case, in the direction that makes a disclosure
smaller than its own charge.

**Everything here models the DIGITAL filter the graph runs**, not its analog
prototype — the linearization biquads through the shared RBJ evaluator, the
crossover as the Butterworth passes CamillaDSP builds. The one place the
analog closed form survives is :func:`radiating_band_hz`, which is a fit
policy threshold rather than a signal claim; that function says so.

**Import weight.** ``camilla_yaml`` and ``runtime_contract`` import this
module LAZILY, inside the one function that needs it, because neither pulls
numpy today and both are loaded by daemons on a 1 GB Pi. The cut-only
short-circuit below (:func:`branch_chain_peak_db`'s ``all gains <= 0`` case)
means the overwhelmingly common graph never imports numpy at all.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.sound.profile import (
    RESPONSE_SAMPLE_RATE_HZ, FilterSpec, _filter_response_complex, _freq_trig,
)

# --------------------------------------------------------------------------- #
# policy constants
# --------------------------------------------------------------------------- #

# How far down its own crossover a driver is still considered to be RADIATING,
# dB — the bound the linearization fit band is clamped to (#1809).
#
# The defect this closes, measured on JTS3 2026-07-28: the woofer's fit placed
# +11.6155 dB (Q 8.0) at 2747 Hz and +5.9619 dB at 2197 Hz, both ABOVE its
# 2 kHz LR4 crossover, where the lowpass attenuates them -13.3 / -7.8 dB. The
# fit was flattening a curve measured THROUGH the crossover against a flat
# target, so the crossover's own rolloff read as a driver deficit; with PR-L5's
# boost vocabulary it could finally "fix" it, and spent 11.6 of its 12 dB
# per-filter budget fighting a filter the same graph emits three lines earlier.
# Net acoustic contribution ~ +1.1 / -1.6 dB, for a 17.58 dB headroom charge.
#
# Why an ATTENUATION threshold and not simply Fc. "Own side of Fc" (PR-L3's
# rule for trim averaging, ``program_analysis.branch_level_bands_hz``) removes
# the two stopband boosts but leaves the SAME pathology at the crossover's own
# knee: at Fc an LR4 branch is 6 dB down by design, so a fit that reaches Fc
# still reads a 6 dB deficit there and boosts it — the tweeter's +5.6163 dB at
# 2020 Hz on that same profile, half of the predicted +8.9 dB crossover-region
# hump. The physical statement "correct this driver only where the crossover
# leaves it at essentially full output" is the one that closes both.
#
# 3 dB: the standard "half power" line, and the point where a branch's
# contribution to the summed response starts being shared rather than owned.
# For LR4 it puts the woofer's edge at 0.801*Fc and the tweeter's at 1.248*Fc.
# The band between them is not left uncorrected by accident — the crossover
# handoff is owned by the layers that measure the SUM (Fc choice, alignment
# delay/polarity, and the ripple-optimal trim), and a per-driver magnitude fit
# there is precisely the model that mispredicts a phase-dominated summation.
CROSSOVER_EDGE_ATTENUATION_DB: float = 3.0

# Safety margin added to the realized branch peak when charging program-domain
# headroom, dB (#1808).
#
# The charge has to cover what the evaluation cannot see, and by construction
# that is now a short list. Sampling a continuous response can only ever
# UNDER-read its maximum, and two error sources this margin originally stood
# for have since been eliminated rather than budgeted for: every filter's own
# peak is sampled exactly at any Q (``_evaluation_grid`` unions the centres and
# the domain edges), and the crossover term IS the digital filter the graph runs
# rather than an analytic stand-in for it (``crossover_response_db``).
#
# What remains is the CASCADE's peak between two adjacent SAMPLES, plus the
# emitter's own 4-decimal YAML rounding of freq/q/gain.
#
# ONE CONVENTION for every number below: under-READ is ``truth - grid read``,
# the sampling error itself. (Under-CHARGE — ``truth - (read + this margin)``
# — is that number minus 1.0 dB, and is what a graph would actually clip by.
# Quoting the two interchangeably is how a 5.01 and a 6.01 end up in one
# paragraph describing one cascade, which is what #2758's did.)
#
# Measured under-READ, all on the grid this module ships:
#   * two adjacent CENTRES, where near-coincident filters reach more together
#     than either does alone. **This family has THREE axes and the centre
#     FREQUENCY is one of them** — quoting a Q and a separation without it
#     produces two "measurements" that disagree by 0.1 dB and are both right:
#     two +6 dB bells at Q 500, 0.1 % apart, under-read 0.0000 dB at 1 kHz
#     (the extremum lands on the unioned midpoint) and 0.0998 dB at 9 kHz
#     (RBJ digital asymmetry walks it off that midpoint). Searched over
#     f1 200 Hz - 15 kHz, Q 8 - 500, separations 0.1 - 3 %, the worst FOUND is
#     0.1913 dB at f1 1000 Hz, Q 118, 0.575 % — a search minimum, so the bound
#     stated below is deliberately above it;
#   * two adjacent BACKGROUND bins, outside the centres' own hull. Searched
#     over the WHOLE evaluated domain rather than the audio band — the axis a
#     narrower search silently fixes — this term is strongly BAND-DEPENDENT:
#     0.0860 dB worst over 500 randomized mixed-sign pairs inside the emitter's
#     rails (Q <= 8, +-12 dB) drawn from 200 Hz - 18 kHz, and 1.7385 dB worst
#     over the same population drawn from 18 kHz - Nyquist. 0.0423 dB on the
#     #2758 cascade. Bounded at all only because :data:`CHAIN_GRID_HZ` spans
#     the whole evaluated domain: a background narrower than that read that
#     cascade 6.0132 dB low.
#
# **The ceiling is 0.25 dB BELOW ~18 kHz, and ~2 dB above it.** Both are chosen
# above every search's output rather than equal to the last one — a hill-climb
# reports a minimum, never a maximum, and an earlier revision of this comment
# promoted 0.07 dB on exactly that mistake. **The same caution applies to the
# DOMAIN axis**: a search that stops at 19 kHz reports a ceiling for the band it
# looked at, which is how 0.25 was published as if it were global. Below 18 kHz
# the margin covers the residue four times over, which is the claim this
# constant rests on; above it the residue EXCEEDS the margin and the -1.0 dB
# per-driver soft-clip limiters are the backstop instead. Why that is a residual
# of a large improvement rather than a regression: before the widening that band
# had no samples at all and read ~6 dB low. Tracked as issue #2850.
#
# **#2850 was still open when the boost caps widened** (ruling R8, 2026-08-22),
# and an earlier revision of this comment said it would close first. It did not,
# so what changed is recorded here rather than quietly dropped. The widened
# per-filter ceiling admits filter magnitudes this residue was measured at: the
# worst rails-legal pair on record (`+10.01 Q3.67 @23632.6` with
# `-9.53 Q5.58 @23648.1`, under-charging 0.6116 dB) is refused by the
# pre-R8 3.0 dB prescription ceiling and admitted by the 12.0 dB one. It stays
# ultrasonic and stays backstopped by the -1.0 dB per-driver soft-clip limiters,
# and reaching it through `driver_prescription` additionally requires a driver
# declared past ~23 kHz carrying a banked boostable verdict up there — but the
# exposure is wider after R8 than before it, which is the honest statement of
# what widening the caps did to this margin.
#
# **What actually keeps the honest loop out of that band is the CLASSIFIER's
# vouching ceiling, and it is producer-side only.** A boost is admitted only
# against a banked `defect-boostable` verdict near its centre, and the highest
# frequency the classifier can ever vouch for is
# `feature_classifier.classifiable_band_hz((_, TRUSTED_CEILING_HZ))[1]`
# (16 kHz trimmed by the +-1/3-octave neighbourhood -> 12699.2 Hz) widened by
# `feature_classification.VERDICT_MATCH_TOLERANCE_OCTAVES` (1/6 octave) =
# **14254.4 Hz** — comfortably below the ~18 kHz where this margin stops
# covering the residue. So no verdict the measurement loop can PRODUCE reaches
# the exposed band. The gate itself does not enforce this: hand-inject a 19 kHz
# boostable verdict and it is admitted. `tests/test_active_speaker_branch_chain
# .py::test_the_classifier_cannot_vouch_into_the_under_read_band` pins the
# arithmetic so that raising the classifier's ceiling later trips a test
# instead of silently opening this band.
#
# One shape is OUTSIDE that ceiling and is tracked rather than budgeted for: a
# Lowshelf cornered below ~1.9 Hz, whose extreme is an asymptote BELOW
# :data:`_GRID_EDGE_LO_HZ` and so off the grid entirely (1.2937 dB at a 1.8 Hz
# corner, 6.0 dB at or under 1.0 Hz). Tamper-only today — nothing this repo
# emits corners a shelf there — and issue #2846 is where it closes.
#
# 1.0 dB and not an arbitrary number: it is exactly
# ``camilla_yaml.BASELINE_LIMITER_CLIP_LIMIT_DB``, so a correction's loudest
# bin lands ON the per-driver soft-clip limiter's own threshold rather than
# into it. The limiters stay the backstop they always were; this keeps the
# ordinary case from ever reaching them.
HEADROOM_MARGIN_DB: float = 1.0

# Below this the evaluated peak is treated as "this chain never exceeds
# unity", dB.
#
# A cascade's peak is 0.0 wherever every filter in it is at unity — far below
# a peaking filter's corner, say — and the digital biquads evaluate that
# analytic zero to a residue of order 1e-4 dB. Without a floor, a correction
# whose only boosts are buried in its own crossover stopband (peak 8e-5 dB
# above unity) would be charged the whole margin, which is the invisible
# headroom #1808 exists to stop.
#
# 0.01 dB is two orders above that residue and 0.1% in amplitude: a chain that
# genuinely peaks there cannot reach anything the -1.0 dB per-driver soft-clip
# limiters do not already catch, so treating it as unity is safe as well as
# honest.
_PEAK_EPS_DB: float = 0.01

# The domain every chain peak is taken over: essentially DC to essentially
# Nyquist, the digital response's own edges. Appended to every evaluation grid
# so a caller-supplied axis carries them too.
#
# A shelf's extreme is at an EDGE, not at its corner: a Lowshelf reaches its
# full gain below the corner and a Highshelf above it. A grid that stops at
# 20 Hz / 20 kHz reads a +12 dB Lowshelf cornered at 30 Hz as 9.69 dB — an
# under-read of 2.3 dB, which is past the margin. Sampling the two edges
# captures both asymptotes exactly. The frequencies are outside the audible
# band on purpose: what is being bounded is what the FILTER can do to the
# signal, and a shelf that lifts 12 dB at 25 Hz has lifted 12 dB whether or
# not the household can hear it.
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
# **The span is the domain above and not the audio band, and that is a
# correctness requirement rather than tidiness (#2758).** The two edge points
# capture a monotonic shelf's ASYMPTOTE; they say nothing about an extremum
# BETWEEN an edge and the band. A mixed-sign cascade puts one there: measured
# on the shipped tweeter band behind a real 1600 Hz LR4 high-pass,
# ``5x(+3.0 Q0.5)@20000 + 3x(-8.0 Q1.0)@18182`` peaks 6.8728 dB at 21500.6 Hz,
# which a 20 Hz - 20 kHz background read as 0.8596 — under-READ by 6.0132 dB
# and so under-CHARGED by 5.0132 (:data:`HEADROOM_MARGIN_DB`'s convention
# paragraph carries both), with ultrasonic clipping products aliasing back
# into the audible band. The woofer-side mirror is the same hole:
# ``5x(+3.0 Q0.5)@20 + 3x(-8.0 Q1.0)@22`` behind a 1600 Hz LR4 low-pass peaks
# 3.4196 dB at 9.22 Hz and was read as 1.4733 — under-READ by 1.9463.
# One span for gate, charge and re-proof is what keeps them one construction,
# so the hole was shared by all three.
#
# The anchor moving 20 Hz -> 1 Hz also moves every background bin's PHASE, so
# an individual reading can land either side of where it did: over 4000
# randomized in-band cascades at the emitter's rails, ~16 % read LOWER and the
# rest the same or higher. The worst drop is 0.0697 dB at one seed and
# 0.1134 dB across four, so treat it as >= 0.15 dB rather than as the single
# draw. That is sampling noise inside the residue bound above, not a direction
# claim — this change is NOT "every reading rises".
#
# Roughly 6 ms per branch on a laptop for a full 8-filter chain behind a
# crossover, on a path that runs once per config emit and once per branch at
# graph re-proof; the cut-only short-circuit below means the ordinary graph
# pays none of it.
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
    Peaking filter at Q 2000, placed at the midpoint between two 1/48-octave
    bins, reads -0.0 dB and would prove SAFE against a graph that charged
    nothing for it. No honest fit can emit that — the fit engine's own
    ``_PEAKING_Q_MAX`` is 8, where the between-bin error is at most 0.21 dB
    and disappears inside :data:`HEADROOM_MARGIN_DB` — but the runtime
    contract reads graphs it does not trust, and "the emitter would never
    write this" is not a proof.

    A Peaking or shelf filter's extremum is at its own ``freq`` or at a domain
    edge, and a centre goes in at its OWN frequency all the way to NYQUIST —
    not merely up to :data:`_GRID_EDGE_HI_HZ`, which bounds the background
    sampling and the asymptote sample, never where a filter may sit. The
    difference is a 4.8 Hz sliver and it is not cosmetic: the bilinear prewarp
    compresses frequency without bound as it approaches Nyquist, so a +12 dB
    Q-8 Peaking filter at 23999 Hz delivers its full 12 dB there while
    measuring 0.0120 dB at 23995.2 Hz. Bounded at the edge it read 0.0120 and
    proved SAFE against a graph charged nothing; unioned at its own centre it
    reads 12.0000.

    A centre at or ABOVE Nyquist is left to the background grid, on
    measurement rather than omission: the digital response mirrors about
    Nyquist, so such a filter's extremum lands at ``fs - freq`` INSIDE the
    band, where the 1/48-octave background already reads it to within 0.103 dB
    (measured at 26 kHz / 30 kHz / 47 kHz, +12 dB Q 8) — the same order as the
    between-sample residue below. A centre at exactly Nyquist is degenerate and
    evaluates flat.

    A CASCADE's peak can sit BETWEEN two centres — two near-coincident bells
    reach more together in the middle than either does at the other's centre —
    so the geometric midpoint of each adjacent pair goes in too, which is what
    takes the centres-alone under-read (up to 0.58 dB over that family) down to
    the residue :data:`HEADROOM_MARGIN_DB` covers. **That residue has ONE owner
    and it is that constant's own comment** — including which axes the family
    has and why two honest measurements of it can differ by 0.1 dB. Sampling
    can only ever under-read a continuous maximum, which is why the residue is
    a floor on the margin rather than a two-sided error.
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

    A two-way woofer has one (lowpass at Fc); a two-way tweeter has one
    (highpass at Fc); a three-way mid has two. ``order`` is the LR order the
    graph emits (:data:`jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`).
    """

    fc_hz: float
    order: int
    highpass: bool


def sections_by_role(regions: Iterable[Any]) -> dict[str, tuple[CrossoverSection, ...]]:
    """Map each driver role to the Linkwitz-Riley sections its branch runs
    through, from a preset's crossover regions.

    **The single derivation, and it has to be single.** Two consumers read
    this: the session, which bounds the fit's lift band and stamps the
    disclosed ``headroom_cost_db`` with it, and the emitter, which charges
    ``active_baseline_headroom`` with it. They were separate derivations for
    one review cycle and had already drifted — on a preset with no region for
    a role the session invented ``(session Fc, order 4)`` while the emitter
    credited no crossover at all, which would have made the disclosure SMALLER
    than the charge: the one direction the ledger promises is impossible.

    A role with no region gets no sections, and that is the honest answer
    rather than a conservative-looking guess: the emitter builds its
    Linkwitz-Riley filters from these same regions, so a role without one runs
    FULL RANGE in the emitted graph. It therefore radiates everywhere (no lift
    bound is correct) and attenuates nothing (no headroom credit is correct).
    Callers that consider the case a defect log it; none of them invent a
    crossover the graph does not contain.

    ``regions`` are duck-typed on ``lower_driver`` / ``upper_driver`` /
    ``fc_hz`` / ``order`` — this module does not import the preset schema, and
    the assignment (lower driver takes the low-pass, upper the high-pass)
    mirrors ``camilla_yaml._emit_baseline_driver_definitions`` exactly.
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

    Linkwitz-Riley of order N is two cascaded Butterworth passes of order N/2
    — that is what gives the -6 dB-at-Fc, in-phase-summing pair — and this
    builds exactly that, as the DIGITAL sections CamillaDSP realizes: the
    Butterworth biquads go through :func:`chain_response`, the same RBJ
    evaluator (and the same ``tan`` prewarp) the linearization filters
    themselves are evaluated with, plus the one first-order section an odd
    Butterworth order needs, which no biquad spells.

    **Modelled digitally, deliberately.** The textbook closed form
    ``|H_lp| = 1 / (1 + (f/fc)^N)`` is the ANALOG prototype, and it is what
    this function used for one review cycle. It diverges from the filter the
    graph actually runs as the response approaches Nyquist, and not in the
    safe direction: measured against the digital construction, the analog form
    UNDER-read a branch's true output by up to 1.02 dB (LR4) and 1.58 dB (LR2)
    for a 10 kHz crossover, inside the band where a chain peak can live. That
    is the whole of :data:`HEADROOM_MARGIN_DB` spent on a modelling choice, in
    the loud direction. Modelling the real filter costs a few lines and
    removes the question instead of budgeting for it.

    Never positive: a Butterworth pass is monotonic and never exceeds unity,
    which is the property that lets a cut-only chain be known to sit at or
    below unity without evaluating it at all.
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
    transform at :data:`jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ` with the
    same ``tan`` prewarp ``_biquad_coeffs`` uses, so a first-order pass and a
    biquad pass agree about where the corner is.

    Reached only for an odd Butterworth order, which across the supported LR
    orders means LR2 (two first-order passes).
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

    Solved in closed form from the ANALOG Linkwitz-Riley prototype: for a
    lowpass of order N the edge is ``fc * (10**(e/20) - 1) ** (1/N)`` and for a
    highpass ``fc * ((r / (1 - r)) ** (1/N))`` with ``r = 10**(-e/20)``. (The
    two are reciprocal — an LR pair's radiating bands are mirrored about Fc in
    log frequency, the same mirroring
    ``program_analysis.branch_level_bands_hz`` uses for its level bands.)

    The prototype rather than the digital filter :func:`crossover_response_db`
    builds, and the difference is deliberate rather than overlooked. It places
    the edge slightly inside the real filter's own 3 dB point, by an amount
    that GROWS with the corner frequency: measured at LR4, the digital
    response at this band's edge is -2.98 dB for a 2 kHz corner, -2.87 at
    5 kHz and -2.43 at 10 kHz (and -2.08 for the high-pass edge there).

    Always in the conservative direction — less attenuation at the edge means
    the band is NARROWER than a true 3 dB span, so the fit is permitted to add
    level over slightly less of the spectrum, never more. That is the right
    way for a policy threshold to be wrong, and this is a policy threshold:
    "close enough to full output that this driver still owns the band", where
    3 dB is a round number chosen for what it means rather than a measured
    quantity. The charge is the opposite case and is modelled digitally for
    exactly that reason — it has to match what the graph does to a signal, to
    the dB. Pinned across corners by
    ``test_radiating_band_is_the_three_db_span_and_mirrors_about_fc``.

    Returns ``(0.0, inf)`` for a branch with no sections (a one-way box's
    summed chain: it radiates everywhere it measures). Multiple sections
    intersect — a three-way mid gets the band between its two crossovers, and
    a mid squeezed between two crossovers closer together than their own edges
    honestly returns an EMPTY (``lo > hi``) band: it never reaches full output
    anywhere, so it gets no lift. The caller's mask is empty and the fit
    simply adds no gain; nothing here invents a band that does not exist.

    Raises ``ValueError`` for a non-positive ``edge_attenuation_db``: a 0 dB
    edge would put a lowpass branch's bound at DC and refuse the fit
    everything, which is a caller bug, not a conservative default.
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


# The ka value at which a circular piston is taken to be BEAMING outright, and
# the one #1675's owner ruling names. It is a documented heuristic, not a
# measured edge: ka = 1 is the classic onset of narrowing, ka = 2 is roughly
# -6 dB at 45 deg off-axis, and the loudspeaker literature's usual rule of
# thumb is to cross a cone below it (ATC / Acoustic Frontiers; a 5" cone
# crossed at or under ~1.7 kHz, arithmetic checked in
# docs/research/2026-07-23-driver-linearization/03-fact-check.md claim L).
#
# Named as a constant because it is a POLICY choice about where guidance
# starts, exactly like CROSSOVER_EDGE_ATTENUATION_DB above — the physics below
# is continuous and has no threshold in it.
BEAMING_KA = 2.0


def beaming_onset_hz(radiating_diameter_mm: float, *, ka: float = BEAMING_KA) -> float:
    """Frequency at which a piston of this diameter reaches ``ka``.

    ``ka`` is the dimensionless product of wavenumber and radius: for radius
    ``a`` and speed of sound ``c``, ``ka = 2*pi*f*a/c``, so
    ``f = ka*c / (2*pi*a)``. For the JTS3 woofer's declared 114 mm effective
    diameter that is **957.7 Hz at ka = 1** and **1915.4 Hz at ka = 2**.

    **This is GEOMETRY, and the honesty matters more than the number.** A
    directivity step at the crossover survives any on-axis EQ — the remedy is
    crossover placement, a different horn, or a different driver, never a
    filter (#1675; the layer-model doc lists it as an explicit non-goal). So a
    consumer may use this to PREFER a lower crossover or to warn, and must not
    present it as something the DSP will fix.

    Deliberately unrounded. The browser's component-entry hint
    (``kaBeamingOnsetHz`` in deploy/assets/sound-profile/js/main.js) rounds
    ka=1 to a whole Hz first so the "2x" it displays is exact; that is a
    display concern. This value rounds TO that one — pinned by test — so the
    two agree without either owning the other's job.

    Raises ``ValueError`` on a non-positive diameter or ``ka``: there is no
    conservative default for a dimension nobody declared, and inventing one
    would manufacture a beaming ceiling out of nothing.
    """
    if not math.isfinite(radiating_diameter_mm) or radiating_diameter_mm <= 0.0:
        raise ValueError(
            f"radiating diameter must be positive (got {radiating_diameter_mm})"
        )
    if not math.isfinite(ka) or ka <= 0.0:
        raise ValueError(f"ka must be positive (got {ka})")
    radius_m = float(radiating_diameter_mm) / 2000.0
    return ka * DEFAULT_SOUND_SPEED_M_S / (2.0 * math.pi * radius_m)


def chain_response(
    filters: Sequence[Mapping[str, Any]], freqs_hz: np.ndarray,
) -> np.ndarray:
    """The COMPLEX response a cascade of emitted biquads applies at ``freqs_hz``.

    ``filters`` are plain ``{biquad_type, freq, q, gain}`` records — the
    reduced shape the emitter, the runtime contract, and
    :meth:`~jasper.active_speaker.linearization_fit.LinearizationFilter.to_dict`
    all speak, so no caller has to hold the fit engine's dataclass to evaluate
    a chain.

    Every entry goes through :func:`jasper.sound.profile.
    _filter_response_complex`, the SAME evaluator ``reduce_cuts_for_lift``
    bottoms out in and the one whose magnitude is fixture-pinned against
    CamillaDSP's realization — there is exactly one biquad evaluator in this
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

    The real gain this branch applies to the program at its loudest
    frequency — positive means the branch drives the program above unity
    there, and that is exactly what the pre-split headroom has to absorb.

    Replaces the per-branch SUM of positive filter gains, which was a loose
    upper bound on this quantity and, on the 2026-07-28 JTS3 profile, a 5.6x
    one: 22.458 dB charged against a realized branch peak of +4.00 dB, leaving
    the speaker 8.3 dB below the household's listening level at maximum
    volume. Two bells at different centres never reach their combined height
    anywhere; two bells inside the crossover's own stopband never reach
    anything at all. The sum was conservative for a reason — overlapping
    boosts at ONE frequency do add — but the evaluated peak measures that case
    correctly too, because a cascade evaluated bin by bin already contains it.

    ``trim_db`` is the branch's own attenuation (the emitted per-driver Gain,
    always <= 0), added exactly: a constant gain shifts the whole chain.

    **Cut-only short-circuit.** With no positive filter gain the answer is
    ``min(0, trim_db)`` without evaluating anything — a cascade of cuts, an LR
    section, and a non-positive trim are each <= 0 dB everywhere, so their
    product cannot exceed unity. This keeps every cut-only graph (which is
    every graph before PR-L5) on a numpy-free path.
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

    One evaluation, two facts, because a refusal that names only the dB is a
    number an operator cannot act on: WHERE a chain peaks is what says whether
    it is the boost somebody asked for or a cascade extremum nobody predicted
    (#2758's own peak sits at 21.5 kHz, outside every band its filters name).
    The runtime contract's refusal and the prescription gate's evidence both
    report it; ``branch_chain_peak_db`` is this function's first element and
    stays the reader for everything that only needs the level.

    The frequency is ``nan`` on the cut-only short-circuit, which evaluates no
    grid and therefore has no frequency to name — never 0.0, which would read
    as DC. Callers that report it are on the boosted path by construction.
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

    ``0.0`` for any chain that never exceeds unity — the speaker gives up
    nothing for a correction that cannot clip, which is the owner's 2026-07-28
    ruling ("we should never end up applying all these corrections and being
    in an invisible cut of headroom") stated arithmetically. Otherwise the
    peak plus :data:`HEADROOM_MARGIN_DB`.
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
    :func:`branch_chain_peak_db`. The number a household is told the
    correction costs, the number the emitter attenuates by, and the number the
    runtime contract proves — one function, three readers."""
    return headroom_charge_db(
        branch_chain_peak_db(
            filters, sections=sections, trim_db=trim_db, grid_hz=grid_hz,
        )
    )
