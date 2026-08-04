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
the envelope composer (``crossover_v2_flow._fit_linearization``), the
disclosure (:attr:`~jasper.active_speaker.linearization_fit.LinearizationFit.
headroom_cost_db`), the CHARGE
(``camilla_yaml.linearization_headroom_db``, which sets
``active_baseline_headroom``), and the PROOF
(``runtime_contract._consume_linearization_chain``, which re-derives it from
the emitted YAML). A charge and a proof that disagree by a hair refuse a
correct graph on hardware; two implementations of one number is exactly the
drift this file exists to remove. :func:`sections_by_role` is here for the
same reason and not a smaller one: the conductor and the emitter each derived
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

from jasper.sound.profile import (
    RESPONSE_SAMPLE_RATE_HZ, FilterSpec, _filter_response_complex, _freq_trig,
)
from jasper.audio_measurement.transfer_composition import (
    LinearTransferSection,
    linkwitz_riley_response_complex,
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
# the band edges), and the crossover term IS the digital filter the graph runs
# rather than an analytic stand-in for it (``crossover_response_db``).
#
# What remains:
#   * the CASCADE's peak between two adjacent centres, where near-coincident
#     filters reach more together than either does alone — measured at
#     <= 0.07 dB with the adjacent-pair midpoints in the grid;
#   * the emitter's own 4-decimal YAML rounding of freq/q/gain.
# Both are an order or more inside 1.0 dB.
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

# The grid every chain peak is evaluated on: 1/48 octave, 20 Hz - 20 kHz.
#
# Deliberately NOT the fit's own ``DEFAULT_ENVELOPE_GRID_HZ`` (150 Hz floor):
# this grid is read by the runtime contract against a graph it does not trust,
# and a grid that starts at 150 Hz cannot see a boost placed at 60 Hz.
#
# It is the BACKGROUND sampling — the crossover's smooth shape and whatever a
# cascade does between its filters' centres. It is NOT what makes a narrow
# filter's own peak visible: ``_evaluation_grid`` unions each filter's exact
# frequency into it for that, because no fixed resolution can bound an
# arbitrary Q. Roughly 4 ms per branch on a laptop for a full
# 8-filter chain behind a crossover, on a path that runs once per config emit
# and once per branch at graph re-proof; the cut-only short-circuit below
# means the ordinary graph pays none of it.
CHAIN_GRID_HZ: np.ndarray = np.geomspace(20.0, 20_000.0, 480)
CHAIN_GRID_HZ.flags.writeable = False

# The two points appended to every evaluation grid, beyond the audio band.
#
# A shelf's extreme is at an EDGE, not at its corner: a Lowshelf reaches its
# full gain below the corner and a Highshelf above it. A grid that stops at
# 20 Hz / 20 kHz reads a +12 dB Lowshelf cornered at 30 Hz as 9.69 dB — an
# under-read of 2.3 dB, which is past the margin. Sampling essentially DC and
# essentially Nyquist (the digital response's own domain edges) captures both
# asymptotes exactly. The frequencies are outside the audible band on purpose:
# what is being bounded is what the FILTER can do to the signal, and a shelf
# that lifts 12 dB at 25 Hz has lifted 12 dB whether or not the household can
# hear it.
_GRID_EDGE_LO_HZ: float = 1.0
_GRID_EDGE_HI_HZ: float = 0.4999 * RESPONSE_SAMPLE_RATE_HZ


def _evaluation_grid(
    filters: Sequence[Mapping[str, Any]], grid_hz: np.ndarray | None,
) -> np.ndarray:
    """``grid_hz`` (or :data:`CHAIN_GRID_HZ`) unioned with every filter's own
    centre frequency and the two band edges.

    **Tamper hardening, and it is not optional.** A peak evaluated on a fixed
    log grid is blind to anything narrower than its own spacing: a +12 dB
    Peaking filter at Q 2000, placed at the midpoint between two 1/48-octave
    bins, reads -0.0 dB and would prove SAFE against a graph that charged
    nothing for it. No honest fit can emit that — the fit engine's own
    ``_PEAKING_Q_MAX`` is 8, where the between-bin error is at most 0.21 dB
    and disappears inside :data:`HEADROOM_MARGIN_DB` — but the runtime
    contract reads graphs it does not trust, and "the emitter would never
    write this" is not a proof.

    A Peaking or shelf filter's extremum is at its own ``freq`` or at a band
    edge, both of which this grid contains, so every filter's own peak is
    sampled exactly whatever its Q.

    A CASCADE's peak can sit BETWEEN two centres — two near-coincident bells
    reach more together in the middle than either does at the other's centre —
    so the geometric midpoint of each adjacent pair goes in too. Measured
    against a 400 000-point sweep over two +6 dB bells at Q 0.5 to 2000 and
    separations of 0.1 % to 50 %, the centres alone under-read the true peak by
    up to 0.58 dB; with the midpoints that falls to 0.07 dB. Sampling can only
    ever under-read a continuous maximum — what :data:`HEADROOM_MARGIN_DB`
    covers is this residue, and it is an order inside it.
    """
    base = CHAIN_GRID_HZ if grid_hz is None else np.asarray(grid_hz, dtype=np.float64)
    extra = [_GRID_EDGE_LO_HZ, _GRID_EDGE_HI_HZ]
    centres = sorted(
        freq for entry in filters
        if 0.0 < (freq := float(entry.get("freq") or 0.0)) < _GRID_EDGE_HI_HZ
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
    this: the conductor, which bounds the fit's lift band and stamps the
    disclosed ``headroom_cost_db`` with it, and the emitter, which charges
    ``active_baseline_headroom`` with it. They were separate derivations for
    one review cycle and had already drifted — on a preset with no region for
    a role the conductor invented ``(session Fc, order 4)`` while the emitter
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
    response = linkwitz_riley_response_complex(
        freqs_hz,
        tuple(
            LinearTransferSection(
                highpass=section.highpass,
                frequency_hz=section.fc_hz,
                order=section.order,
                reason="configured_crossover",
            )
            for section in sections
        ),
    )
    # The historical branch evaluator floored one Butterworth pass at -240 dB
    # and then ran that pass twice, so its exact LR floor is -480 dB.
    return 20.0 * np.log10(np.maximum(np.abs(response), 1e-24))


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
    if not any(float(f.get("gain") or 0.0) > 0.0 for f in filters):
        return min(0.0, float(trim_db))
    grid = _evaluation_grid(filters, grid_hz)
    magnitude_db = 20.0 * np.log10(
        np.maximum(np.abs(chain_response(filters, grid)), 1e-12)
    )
    magnitude_db = magnitude_db + crossover_response_db(grid, sections) + float(trim_db)
    return float(np.max(magnitude_db))


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
