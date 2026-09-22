# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jasper.audio_measurement.interference_nulls import (
    CLASSIFICATION_POSITION_DEPENDENT,
    CLASSIFICATION_POSITION_INVARIANT,
)


# --------------------------------------------------------------------------- #
# Carve-out disclosure
#
# Identified interference nulls are excluded from spec evaluation AND from
# correction, the band's tolerance applies to the SURVIVING envelope, and the
# report discloses "EQ cannot fill these" with the numbers.
# ``evaluate_flat_spec`` does the excluding but must not say WHY — it is a pure
# evaluator holding no product policy — so the "why" is assembled here. This
# module is the one owner of the carve-out copy strings, so a chart callout and
# the envelope's expert disclosure cannot disagree about one carved range.
# --------------------------------------------------------------------------- #

# Which honesty instrument carved a range.
CARVE_OUT_SOURCE_IDENTIFIED_NULL = "identified_null"
CARVE_OUT_SOURCE_POSITION_SCREEN = "position_screen"


def _format_carve_out_hz(hz: float) -> str:
    """One frequency as household copy — kHz at and above 1 kHz, Hz below."""
    return f"{hz / 1000.0:.1f} kHz" if hz >= 1000.0 else f"{hz:.0f} Hz"


def _join_carve_out_phrases(parts: Sequence[str]) -> str:
    """``["a", "b", "c"]`` -> ``"a, b and c"``. No serial comma, matching the
    house copy elsewhere in this flow."""
    parts = tuple(parts)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _null_classification_copy(classification: str) -> str:
    """The classification's own household sentence, or ``""`` for one this copy
    does not cover.

    The ``position_invariant`` wording is load-bearing: a single session cannot
    separate "travels with the speaker" from "a path in the room that did not
    change while measuring", so the copy names both and names the experiment
    that would tell them apart.

    No hardware noun appears in either branch. The classification is evidence
    about how a null behaved across a mic cloud, not about what produced it.
    """
    if classification == CLASSIFICATION_POSITION_INVARIANT:
        return (
            " It sat at the same frequencies at every microphone position — "
            "consistent with something that travels with the speaker, or with "
            "a path that did not change while measuring; moving the speaker "
            "and measuring again would tell those apart."
        )
    if classification == CLASSIFICATION_POSITION_DEPENDENT:
        return (
            " It appeared at some microphone positions and not others, so "
            "whatever causes it does not travel with the speaker."
        )
    return ""


def _carve_out_records(
    null_report: Any, screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Every carved range, tagged with the instrument that carved it.

    The two instruments are listed SEPARATELY: only the registry's rows carry
    τ/r, and overlapping ranges are two rows rather than one, because "both
    instruments flagged this" is a stronger statement than either alone.
    ``merged_excluded_bands_hz`` remains the merged view.

    A registry row's interval is the null's own ``f_lo_hz``/``f_hi_hz``,
    unclipped to any spec band, because τ and r describe the whole null.

    Ordered by lower edge then source, so rows starting at the same frequency
    come out stable rather than in input order.
    """
    records: list[dict[str, Any]] = []
    for null in null_report.nulls:
        records.append(
            {
                "f_lo_hz": float(null.f_lo_hz),
                "f_hi_hz": float(null.f_hi_hz),
                "source": CARVE_OUT_SOURCE_IDENTIFIED_NULL,
                "f_center_hz": float(null.f_center_hz),
                "n": int(null.n),
                "tau_us": float(null.tau_us),
                "r_time": float(null.r_time),
                "r_freq": float(null.r_freq),
                "depth_db": float(null.depth_db),
                "classification": str(null.classification),
                "reason": (
                    "A delayed copy of the sound cancels this range, and EQ "
                    "cannot fill a cancellation, so it is left out of "
                    "correction and out of grading."
                    + _null_classification_copy(str(null.classification))
                ),
            }
        )
    for band in screen_bands_hz:
        records.append(
            {
                "f_lo_hz": float(band[0]),
                "f_hi_hz": float(band[1]),
                "source": CARVE_OUT_SOURCE_POSITION_SCREEN,
                "reason": (
                    "The microphone positions disagreed about this range much "
                    "more than about the rest of the spectrum, so it reads as "
                    "interference rather than the speaker's own response and "
                    "is left out of correction and out of grading."
                ),
            }
        )
    records.sort(key=lambda record: (record["f_lo_hz"], record["source"]))
    return records


def _carve_out_disclosure_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The band's household-facing headline — plain language, no τ/r.

    ``""`` when nothing was carved, rather than a "no interference found"
    sentence a reader could mistake for a measurement. The delay is quoted in
    MILLISECONDS here; τ stays in microseconds in the structured record, which
    is the registry's own unit.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    screened = [r for r in records if r["source"] == CARVE_OUT_SOURCE_POSITION_SCREEN]
    sentences: list[str] = []
    if nulls:
        where = _join_carve_out_phrases(
            [_format_carve_out_hz(float(r["f_center_hz"])) for r in nulls]
        )
        # One ladder, one τ: ``IdentifiedNull.tau_us`` is the same value on
        # every rung of one report, so the first row's delay describes them all.
        delay_ms = float(nulls[0]["tau_us"]) / 1000.0
        plural = len(nulls) > 1
        sentences.append(
            f"{'Interference nulls at' if plural else 'An interference null at'} "
            f"{where} — a delayed copy of the sound arrives {delay_ms:.2f} ms "
            f"later. EQ cannot fill {'these' if plural else 'this'}, so "
            f"{'they are' if plural else 'it is'} left out of correction and "
            "out of this band's grading."
        )
    if screened:
        plural = len(screened) > 1
        # "One range" rather than "1 range": the frequency figures are the
        # numerals a reader should be counting in this sentence.
        count = f"{len(screened)}" if plural else "One"
        subject = f"{count} {'further ' if nulls else ''}"
        subject += "ranges are" if plural else "range is"
        tail = (
            "left out because the microphone positions disagreed about "
            if nulls
            else (
                "left out of correction and out of this band's grading "
                "because the microphone positions disagreed about "
            )
        )
        sentences.append(
            f"{subject} {tail}{'them' if plural else 'it'} too much to grade."
        )
    return " ".join(sentences)


def _carve_out_expert_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The expert-layer line — the same carve-outs WITH τ and r.

    Separate from :func:`_carve_out_disclosure_copy` because τ/r belong behind
    a disclosure rather than in the headline. ``r`` is reported as the pair the
    registry holds — time-domain and frequency-domain — rather than an average,
    because their AGREEMENT is what admitted the null.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    if not nulls:
        return ""
    where = _join_carve_out_phrases(
        [
            f"{_format_carve_out_hz(float(r['f_center_hz']))} (rung {int(r['n'])}, "
            f"{float(r['depth_db']):.1f} dB deep)"
            for r in nulls
        ]
    )
    first = nulls[0]
    return (
        f"carved out of grading: {where}; delay τ {float(first['tau_us']):.0f} µs, "
        f"reflection ratio r {float(first['r_time']):.3f} measured in time / "
        f"{float(first['r_freq']):.3f} implied by null depth"
    )


def carve_outs_by_band(
    spec_report: Any,
    null_report: Any,
    screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Per spec band: which ranges were carved out, why, and with what numbers.

    One entry per band of ``spec_report``, always all of them in the report's
    own order, so a consumer joins by index or ``band_hz`` and can render
    "nothing carved here" without inferring it from an absence.

    A record is included when its interval OVERLAPS the band's
    ``[graded_lo_hz, graded_hi_hz)`` span — the span actually graded, not the
    nominal row — so a null straddling an edge appears under both bands it
    carves and one outside the trusted range appears under none.

    Deliberately EXCLUDES the gate's trusted-floor clamp: that clamp moves each
    band's graded EDGE, so a sub-floor bin is not in the band to be excluded
    from. A band's ``n_excluded`` is therefore exactly what these records cover
    (#2551).
    """
    records = _carve_out_records(null_report, screen_bands_hz)
    out: list[dict[str, Any]] = []
    for band in spec_report.bands:
        f_lo, f_hi = float(band.f_lo_hz), float(band.f_hi_hz)
        # Overlap is tested against the GRADED edges, not the nominal row: a
        # null outside the trusted range carved nothing out of this band. The
        # upper edge matters as much as the lower, since the top band's follows
        # the microphone-trust ceiling. ``band_hz`` below stays the nominal
        # pair, since it is the join key against ``spec["bands"]``.
        graded_lo = f_lo if band.graded_lo_hz is None else float(band.graded_lo_hz)
        graded_hi = f_hi if band.graded_hi_hz is None else float(band.graded_hi_hz)
        in_band = [
            record
            for record in records
            if record["f_lo_hz"] < graded_hi and record["f_hi_hz"] > graded_lo
        ]
        out.append(
            {
                "band_hz": [f_lo, f_hi],
                "intervals": [dict(record) for record in in_band],
                "disclosure": _carve_out_disclosure_copy(in_band),
                "expert": _carve_out_expert_copy(in_band),
            }
        )
    return out


def _geometry_guidance_copy(geometry: Mapping[str, Any]) -> str:
    """Plain-language "spread the mic further" guidance from a geometry verdict
    dict (:func:`cloud_geometry_verdict`'s shape).

    Softened, never suppressed, when ``thin_evidence``, and the softened copy
    names the qualitative floor rather than a count: ``thin_evidence`` is a
    cliff at an exact confident-estimate count, so naming it would read as a
    gradient the instrument does not claim. ``""`` when not locked.
    """
    if not geometry.get("locked"):
        return ""
    if geometry.get("thin_evidence"):
        return (
            "The measured echo pattern looks the same at every microphone "
            "position, but only the bare minimum of positions gave a "
            "confident enough reading to tell. Spreading the microphone "
            "further apart next time would make this more certain."
        )
    return (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )
