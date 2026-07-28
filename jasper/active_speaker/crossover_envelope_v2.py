# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 conductor screen envelope (schema 8, Wave 5a; auto-apply since 2026-07-20).

``docs/crossover-measurement-productization-design.md`` §5.9/§5.10 defines the
v2 screen sequence — ``("speaker_setup", "microphone_check", "measure",
"apply", "verify")`` — and the four failure-screen TEMPLATES the flow
renders (silent auto-retry banner / fix-and-retry / hard stop / session
restart), plus the two special screens (``volume_recovery`` and the VERIFY-fail
one-default screen). This module is the pure ``status → envelope`` function for
that flow, dispatched from
:func:`jasper.active_speaker.crossover_envelope.build_crossover_envelope` —
the only crossover flow since W5b retired the legacy schema-6 envelope and the
``JASPER_CROSSOVER_FLOW`` selector. It emits the envelope dict shape the
generic data-driven JS renderer consumes (``schema_version`` / ``screen`` / ``steps`` / ``verdict_text`` /
``nudges`` / ``relay`` / ``next_action`` / ``alternate_actions`` / ``progress``
/ ``applied``) so the generic data-driven JS renderer needs no v2-specific code.

**Owner ruling (2026-07-20): no human mid-flow Apply gate.** The former
``review_apply`` screen (a human tap over the measured candidate) is gone from
the happy path — the conductor auto-applies a trusted candidate itself (see
``jasper.active_speaker.crossover_v2_flow``'s module docstring). This module's
``"applying"`` screen is the brief machine-paced in-flight state; the ``"done"``
screen is now the RESULT screen — plain-language outcome first, numbers in a
collapsed expert disclosure, Undo prominent.

The v2-specific state the backend threads onto the status lives under
``status["crossover_v2"]`` (phase / failure / verify / candidate /
apply_blocked / needs_recovery / applied); this module never re-derives it — the conductor
(:mod:`jasper.active_speaker.crossover_v2_flow`) owns those decisions and their
reason codes, and this module maps a reason code to its template copy through
the shared :data:`~jasper.active_speaker.crossover_v2_flow.REASON_REGISTRY`.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..log_event import log_event
from .crossover_v2_flow import (
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
    REASON_REGISTRY,
    TEMPLATE_HARD_STOP,
    TEMPLATE_SESSION_RESTART,
    TEMPLATE_SILENT_AUTO_RETRY,
    TEMPLATE_VERIFY_FAIL,
    TIER_EXPRESS,
    TIER_FULL,
    tier_display_info,
)

logger = logging.getLogger(__name__)

# Bumped 7 → 8: the screen vocabulary changed (review_apply removed, the
# "applying" in-flight screen added, the "done"/RESULT screen's shape changed
# to plain-outcome-first + expert disclosure + prominent Undo) — owner ruling,
# 2026-07-20.
CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION = 8

# The v2 step tuple (§5.9, amended 2026-07-20). The step machinery inside each
# step is gone; these five are the whole journey.
_STEP_IDS = (
    "speaker_setup",
    "microphone_check",
    "measure",
    "apply",
    "verify",
)
_STEP_LABELS = {
    "speaker_setup": "Protected speaker setup",
    "microphone_check": "Microphone check",
    "measure": "Measure",
    "apply": "Apply",
    "verify": "Verify",
}

# Which step is active for a given conductor phase. The position groups
# (flat-linearization PR-3b) do NOT add wizard steps: a household walking the
# pre-apply cloud is still "measuring" and the post-apply cloud is still
# "verifying" — the cloud changed how many captures each step takes, not what
# the household is doing. Adding steps would have made the journey read as
# longer without telling anyone anything new; the phone's own per-entry screens
# carry the within-step progress — "Measurement N of T" (the whole-session
# counter, server-derived; flow-simplification §2.1 retired the older,
# per-group "Spot i of n" vocabulary this comment used to name).
_PHASE_STEP = {
    PHASE_CHECK: "microphone_check",
    PHASE_MEASURE: "measure",
    PHASE_CLOUD_MEASURE: "measure",
    PHASE_APPLYING: "apply",
    PHASE_VERIFY: "verify",
    PHASE_CLOUD_VERIFY: "verify",
    PHASE_DONE: "verify",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# Presentation order for the per-role trim rows on the review screen — woofer
# before tweeter reads low-to-high like the crossover itself; any other role
# falls to the end alphabetically.
_ROLE_ORDER = {"woofer": 0, "tweeter": 1}

# Gauge fix (2026-07-24): the top-octave centers the RESULT screen discloses
# per driver — "at least the 8k/12k/16k values" (the item's own scope). The
# fit engine computes more (down to 250 Hz — see
# linearization_fit._OCTAVE_BAND_CENTERS_HZ), but the household-facing
# disclosure only needs the top of the ladder, where an uncorrected driver's
# natural rolloff is otherwise invisible on every other screen.
_TOP_OCTAVES_HZ = ("8000", "12000", "16000")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _linearization_octave_rows(
    octaves: Any,
) -> list[dict[str, Any]]:
    """Gauge fix (2026-07-24): per-role top-octave rows (>= 8k/12k/16k) —
    the OBSERVE-layer honesty ladder's disclosure numbers
    (``linearization_fit.LinearizationFit.observe_octave_summary``), already
    computed by the fit engine and threaded through
    ``jasper.web.correction_crossover_v2._candidate_summary``. Each value is
    achieved-minus-target dB: a large negative number at an octave means the
    driver's natural response is that far down there and nothing corrected
    it — "uncorrected regions show their natural deficit, never a
    pass/fail" (LinearizationFit's own docstring). Empty for a role whose
    fit never ran.

    **These are FIT DIAGNOSTICS, not the measurement (flat-linearization
    plan PR-5).** Every number here is per-driver, on the fit's own envelope
    grid, from the single design-axis MEASURE capture — a different curve,
    a different geometry, and a different question from the spec claim. The
    spec-facing summary on the same screen is
    :func:`_flatness_details_lines`, which reads the spatial cloud. PR-5
    did not re-derive these rows (there is no per-role decomposition of a
    summed cloud curve to re-derive them from, and the fit genuinely needs
    to disclose what IT achieved); it made sure the two are labeled apart,
    here and in the renderer that prints them
    (``deploy/assets/correction/js/crossover/main.js``), so no surface
    presents a per-driver fit residual as "the measurement".
    """
    rows: list[dict[str, Any]] = []
    for role, per_role in sorted(
        _mapping(octaves).items(),
        key=lambda kv: (_ROLE_ORDER.get(str(kv[0]), 99), str(kv[0])),
    ):
        if not isinstance(per_role, Mapping):
            continue
        bands: list[dict[str, Any]] = []
        for hz in _TOP_OCTAVES_HZ:
            db = _finite(per_role.get(hz))
            if db is not None:
                bands.append({"hz": int(hz), "delta_db": db})
        if bands:
            rows.append({"role": str(role), "bands": bands})
    return rows


def _candidate_review_payload(
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Map the persisted ``_candidate_summary`` (jasper.web.correction_crossover_v2)
    into the plain-language shape the "applying"/"done" screens render (§5.2:
    trims, delay, polarity, ripple, plus confidence/fingerprint provenance).

    W6.10 blocker #2: the generic renderer's review body expected a candidate
    shape (``retained_crossover_regions``/``drivers``) the conductor never
    builds, so ``#crossover-review-body`` rendered empty. This is the single
    conversion point from what ``_candidate_summary`` DOES build (trims_db /
    alignment / confidence / ripple / fingerprint) into rows the page can
    display; the renderer is fixed to consume exactly this shape. Reused on the
    RESULT (``done``) screen since the owner ruling (2026-07-20) removed the
    dedicated human review screen — the same numbers now live behind that
    screen's collapsed expert disclosure.
    """
    if not candidate:
        return None
    trims_db = _mapping(candidate.get("trims_db"))
    trims: list[dict[str, Any]] = []
    for role, value in sorted(
        trims_db.items(), key=lambda kv: (_ROLE_ORDER.get(str(kv[0]), 99), str(kv[0]))
    ):
        db = _finite(value)
        if db is not None:
            trims.append({"role": str(role), "attenuation_db": db})

    alignment = _mapping(candidate.get("alignment"))
    delay_us = _finite(alignment.get("delay_us"))
    delay_role = alignment.get("delay_role")
    delay: dict[str, Any] | None = None
    if delay_us is not None and isinstance(delay_role, str) and delay_role.strip():
        delay = {"role": delay_role, "delay_ms": delay_us / 1000.0}
    polarity = alignment.get("polarity")
    polarity_str = polarity if isinstance(polarity, str) and polarity.strip() else None

    payload: dict[str, Any] = {
        "trims": trims,
        "delay": delay,
        "polarity": polarity_str,
        "confidence": _finite(candidate.get("alignment_confidence")),
        "ripple_db": _finite(candidate.get("predicted_ripple_db")),
        "fingerprint": str(candidate.get("fingerprint") or ""),
        "program_id": str(candidate.get("program_id") or ""),
        # Gauge fix (2026-07-24): WHY Layer-1a driver linearization did or
        # didn't run — the JS renderer maps this enum to plain language
        # (mirrors how it already maps "polarity" below).
        "linearization_outcome": str(candidate.get("linearization_outcome") or ""),
        # Gauge fix (2026-07-24): per-role top-octave deficits.
        "linearization_octaves": _linearization_octave_rows(
            candidate.get("linearization_octaves")
        ),
    }
    # A candidate with nothing displayable (no trims, no alignment) stays
    # hidden rather than rendering an empty card.
    if not trims and delay is None and polarity_str is None:
        return None
    return payload


def _verify_expert_details(status: Mapping[str, Any]) -> list[str]:
    """The verify_fail screen's collapsed expert numbers (#1605): the gated
    level error against its limit, the average error, and the band checked.
    Empty when the conductor persisted no tracking evidence (an early-return
    verify verdict — locate/agc/gate/level-shift — never reaches them).

    **Flat-linearization plan PR-5/N-4 framing.** These lines and
    :func:`_flatness_details_lines`'s are two DIFFERENT constructions that
    land in the same collapsed disclosure (``expert_details`` concatenates
    both): this comparator answers "did apply do what the model predicted"
    on the single design-axis capture grid
    (:func:`~jasper.active_speaker.crossover_v2_flow._analyze_verify`'s
    measured-vs-``predicted_sum``), while the flatness lines answer "is the
    speaker flat" on the spatial cloud. Both used to print an unqualified
    "average error X dB", which read as one number when they are not — the
    ``tracking`` prefix here is the one-word disambiguator (mirroring how the
    flatness line already says "flatness average error"), not a
    re-derivation of either number.
    """
    evidence = _mapping(_mapping(_v2(status).get("verify")).get("evidence"))
    if not evidence:
        return []
    lines: list[str] = []
    max_db = _finite(evidence.get("max_db"))
    tolerance_db = _finite(evidence.get("tolerance_db"))
    if max_db is not None and tolerance_db is not None:
        lines.append(f"level error {max_db:.2f} dB (limit {tolerance_db:.1f} dB)")
    elif max_db is not None:
        lines.append(f"level error {max_db:.2f} dB")
    rms_db = _finite(evidence.get("rms_db"))
    if rms_db is not None:
        lines.append(f"tracking average error {rms_db:.2f} dB")
    lo = _finite(evidence.get("tracking_band_lo_hz"))
    hi = _finite(evidence.get("tracking_band_hi_hz"))
    if lo is not None and hi is not None:
        lines.append(f"checked {lo:.0f}–{hi:.0f} Hz")
    return lines


def _flatness_lines_from_block(flatness: Mapping[str, Any]) -> list[str]:
    """The numeric flatness lines shared by both tiers' expert disclosure —
    max/avg deviation plus the excluded-bin count. Extracted (B1 fix, adversarial
    review of PR #1780) so the Full tier's CURRENT-STATE claim
    (:func:`_flatness_details_lines`, reading CLOUD-VERIFY) and Express's
    BEFORE-TUNING claim (:func:`_express_pre_apply_flatness_lines`, reading
    CLOUD-MEASURE) compute the identical arithmetic from whichever compact
    ``flatness`` block they were handed — one construction, not two."""
    lines: list[str] = []
    max_db = _finite(flatness.get("max_db"))
    max_hz = _finite(flatness.get("max_hz"))
    tolerance_db = _finite(flatness.get("tolerance_db"))
    band = flatness.get("max_band_hz")
    band_lo = _finite(band[0]) if isinstance(band, (list, tuple)) and band else None
    band_hi = (
        _finite(band[1]) if isinstance(band, (list, tuple)) and len(band) == 2 else None
    )
    if max_db is not None:
        where = f" at {max_hz:.0f} Hz" if max_hz is not None else ""
        against = (
            f" (spec {band_lo:.0f}–{band_hi:.0f} Hz, tolerance ±{tolerance_db:.1f} dB)"
            if band_lo is not None and band_hi is not None and tolerance_db is not None
            else ""
        )
        lines.append(f"flatness {max_db:+.2f} dB from the spec reference{where}{against}")
    rms_db = _finite(flatness.get("rms_db"))
    if rms_db is not None:
        lines.append(f"flatness average error {rms_db:.2f} dB across the spec bands")
    graded = flatness.get("n_bins")
    excluded = flatness.get("n_excluded")
    if isinstance(graded, int) and isinstance(excluded, int) and excluded > 0:
        # Bins, not "regions": ``SpecFlatness.n_excluded``'s own docstring
        # explains why an interval count would over-report here (it spans
        # the whole axis, including frequencies no spec band grades).
        lines.append(
            f"{excluded} of {graded + excluded} spec-band bins excluded from "
            "grading (interference, or below the measurement's validity floor)"
        )
    return lines


def _flatness_details_lines(status: Mapping[str, Any]) -> list[str]:
    """The spec-facing flatness disclosure — "how flat is the speaker" —
    distinctly labeled from :func:`_verify_expert_details`'s
    integration-verify lines above, which answer "did the crossover integrate
    as predicted" and are what gates.

    **Re-based onto the spec-curve SSOT (flat-linearization plan PR-5).**
    Until PR-5 these lines rendered a per-VERIFY-capture number (one mic
    position, its own grid, its own band mean, no interference exclusion),
    persisted under ``verify.flatness``. They now read the CLOUD-VERIFY
    group's spec gauge — ``spec_flatness_gauge`` of the same
    ``evaluate_flat_spec`` report `/state`, the doctor check, and the bundle
    artifact read (and PR-7's chart will) — copied through
    ``_compact_cloud_status``. One construction, so the number here and the
    number in the report are the same bytes.

    ``PHASE_CLOUD_VERIFY``, never ``PHASE_CLOUD_MEASURE``, for the FULL
    tier: the pre-apply cloud is the UNCORRECTED baseline that exists in
    order to be out of spec (the same distinction PR-4's
    ``check_crossover_v2_cloud_pipeline`` blocker fix drew), so rendering it
    as "how flat is your speaker" would report a correct speaker as bad
    forever.

    **Express (B1 fix, adversarial review of PR #1780) delegates to
    :func:`_express_pre_apply_flatness_lines` instead.** Express (M=1) never
    produces a CLOUD-VERIFY entry — not "not yet", but PERMANENTLY, by the
    tier's own shape — so reading ``_cloud_verify_block`` for it would always
    return empty and silently withhold the honesty-instrument disclosure
    (spec bands, carve-outs) that owner decision 1 requires on every tier.
    ``_compact_cloud_status`` already projects the SAME flatness/spec_bands/
    carve_outs shape onto the CLOUD-MEASURE entry (the pipeline runs there
    too — see ``_close_cloud_group``), so express reads that block instead,
    under an explicit BEFORE-TUNING frame: the pre-apply cloud is still the
    uncorrected baseline, so its numbers are reported as "what was measured
    before tuning", never as "how flat your speaker is now".

    Empty when no cloud-verify group has closed (Full) or no cloud-measure
    group has closed (Express — cannot happen once MEASURE's cloud group is
    reached, but the shape is defensive regardless). That is "nothing
    measured yet", and saying nothing is the honest rendering of it; the
    fallback vocabulary for FULL's "measured but not evaluable" states lives
    in :func:`_flatness_unavailable_line`.

    **The carve-out lines close the sentence** (plan PR-6b, owner decision 1).
    The excluded-bin count below says how much of the spectrum left grading;
    :func:`_carve_out_expert_lines` says which ranges and why, with τ/r. Owner
    decision 1 is explicit that the tolerance applies to the surviving
    envelope AND that the report discloses the carve-out with the numbers —
    a bin count alone satisfies only the first half — **on every tier**,
    since carve-outs are a post-apply-persistent fact ("EQ cannot fill
    these") regardless of which cloud measured them.
    """
    if str(_v2(status).get("tier") or "") == TIER_EXPRESS:
        return _express_pre_apply_flatness_lines(status)
    block = _cloud_verify_block(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return _flatness_unavailable_line(status)
    if not flatness.get("evaluable"):
        # The gauge ran and could not measure — see
        # ``flat_spec.SpecFlatness.passed``'s own "read it with evaluable"
        # rule. Never render this as a pass or a fail. The carve-out lines
        # ride along because in this exact state they ARE the explanation:
        # if the honesty instruments took every spec band's bins, the ranges
        # and their τ/r are the answer to "excluded by what?".
        return [
            "flatness could not be measured — every spec band was excluded "
            "or out of range"
        ] + _carve_out_expert_lines(block)
    lines = _flatness_lines_from_block(flatness)
    lines.extend(_carve_out_expert_lines(block))
    return lines


def _express_pre_apply_flatness_lines(status: Mapping[str, Any]) -> list[str]:
    """Express's flatness/carve-out disclosure (B1 fix, adversarial review of
    PR #1780) — the household surface :func:`_flatness_details_lines`
    delegates to for ``TIER_EXPRESS``.

    **Design direction (coordinator ruling on the review).** Reads the
    CLOUD-MEASURE compact block (:func:`_cloud_measure_block`) — the ONLY
    cloud express ever produces — and frames its numbers explicitly as the
    BEFORE-TUNING state: "Measured before tuning: …. The applied correction
    targets these; the result was confirmed at the mark only." Never
    presented as "how flat your speaker is now" (that claim needs a
    post-apply cloud, which express does not make — see the degraded-claims
    table, flow-simplification plan §1.3). Carve-out lines render VERBATIM
    from the same block, unprefixed by the before-tuning frame, because they
    are a distinct, post-apply-persistent fact ("EQ cannot fill these") that
    owner decision 1 requires disclosed on every tier, not a claim about the
    CURRENT state.
    """
    block = _cloud_measure_block(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return []
    if not flatness.get("evaluable"):
        return [
            "measured before tuning: flatness could not be measured — every "
            "spec band was excluded or out of range"
        ] + _carve_out_expert_lines(block)
    numeric = "; ".join(_flatness_lines_from_block(flatness))
    lines = [
        f"Measured before tuning: {numeric}. The applied correction targets "
        "these; the result was confirmed at the mark only"
    ]
    lines.extend(_carve_out_expert_lines(block))
    return lines


def _carve_out_expert_lines(block: Mapping[str, Any]) -> list[str]:
    """The carve-out τ/r lines (plan PR-6b, owner decision 1).

    This is the "expert layer" the owner's decision names — the line above says
    HOW MANY spec-band bins left grading; these say WHICH ranges and WHY, with
    the delay and reflection ratio that identified them. One line per band that
    carved anything, in band order; nothing at all when nothing was carved,
    which is the honest rendering of a clean band rather than a "no
    interference found" sentence.

    **The strings are copied, not composed here.** ``carve_outs_by_band`` in
    ``crossover_v2_flow`` owns the carve-out copy (both registers — the plain
    ``disclosure`` headline and this ``expert`` line), so this expert
    disclosure and PR-7's chart callouts render the same words about the same
    range. This function only prefixes the band the line belongs to.

    Takes a compact cloud-phase BLOCK directly (B1 fix, adversarial review of
    PR #1780) rather than ``status`` — the caller picks CLOUD-VERIFY for the
    Full tier or CLOUD-MEASURE for Express (:func:`_flatness_details_lines`),
    since carve-outs are a post-apply-persistent fact disclosed from
    whichever cloud each tier actually produces, not read from one hardcoded
    phase.
    """
    lines: list[str] = []
    carve_outs = block.get("carve_outs")
    if not isinstance(carve_outs, list):
        return lines
    for band in carve_outs:
        if not isinstance(band, Mapping):
            continue
        expert = band.get("expert")
        if not isinstance(expert, str) or not expert:
            continue
        edges = band.get("band_hz")
        lo = _finite(edges[0]) if isinstance(edges, (list, tuple)) and edges else None
        hi = (
            _finite(edges[1])
            if isinstance(edges, (list, tuple)) and len(edges) == 2
            else None
        )
        where = f"{lo:.0f}–{hi:.0f} Hz " if lo is not None and hi is not None else ""
        lines.append(f"{where}{expert}")
    return lines


def _cloud_verify_block(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """The compact CLOUD-VERIFY entry of the ``cloud`` block, or empty.

    ``PHASE_CLOUD_VERIFY`` is spelled through the shared phase constant, not
    a literal, so this and the conductor cannot drift apart on the key name.
    """
    return _mapping(_mapping(_v2(status).get("cloud")).get(PHASE_CLOUD_VERIFY))


def _cloud_measure_block(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """The compact CLOUD-MEASURE entry of the ``cloud`` block, or empty.

    Express's only cloud group (B1 fix, adversarial review of PR #1780) —
    see :func:`_express_pre_apply_flatness_lines`. ``PHASE_CLOUD_MEASURE`` is
    spelled through the shared phase constant for the same reason
    :func:`_cloud_verify_block` does.
    """
    return _mapping(_mapping(_v2(status).get("cloud")).get(PHASE_CLOUD_MEASURE))


def _spec_verdict(entry: Mapping[str, Any]) -> bool | None:
    """One compact cloud entry's flat-spec verdict — ``True`` / ``False`` /
    ``None`` for "no verdict exists".

    The single reader of that key for copy purposes (PR-L4 item 7), so the
    done screen's headline, its badge, and any future surface cannot drift on
    what counts as a failing verdict. ``None`` is load-bearing and is never
    coerced: ``_compact_cloud_status`` leaves ``overall_passed`` ``None`` for a
    pipeline that never became available, and Express never produces a
    post-apply entry at all. Absence of a verdict is not a failing one.
    """
    passed = entry.get("overall_passed")
    return passed if isinstance(passed, bool) else None


def _flatness_unavailable_line(status: Mapping[str, Any]) -> list[str]:
    """The honest cloud-absent rendering (plan PR-5) for the FULL tier's
    CLOUD-VERIFY block — three distinguishable states, told apart rather
    than collapsed into one message.

    * **No cloud-verify entry at all** — the group has not closed (or this
      session never had one, e.g. a verify-only re-arm whose prior cloud was
      also absent). Nothing measured, nothing to say: empty. **On Express
      this is not a transient "not yet" — it is PERMANENT** (M=1 never
      produces a CLOUD-VERIFY entry, by the tier's own shape), which is why
      :func:`_flatness_details_lines` never reaches this function for
      Express at all: it delegates to
      :func:`_express_pre_apply_flatness_lines`, which reads CLOUD-MEASURE
      instead, before this "nothing to say" fallback would otherwise render
      Express's done/verify screens permanently silent on flatness (B1 fix,
      adversarial review of PR #1780).
    * **The entry exists and its pipeline DID run, but carries no gauge** —
      a durable state written by a build between PR-4 and PR-5, read after
      an upgrade without a new session. The pipeline was fine; only the
      gauge is missing. ``overall_passed`` is the tell: ``_compact_cloud_status``
      leaves it ``None`` for an unavailable pipeline and copies the spec
      verdict otherwise. Saying "could not be analysed" here would be a
      false statement about a session that analysed fine.
    * **The entry exists and its pipeline never became available** — a
      combine or DSP-step failure. Say so, because the alternative is a
      screen that silently looks like the session with no spec claim in it.

    None of the three quotes a number: the construction did not run (or its
    result was not recorded), so there is no spec-frame figure to give,
    fabricated or otherwise.
    """
    entry = _cloud_verify_block(status)
    if not entry:
        return []
    if entry.get("overall_passed") is not None:
        return [
            "flatness not recorded for this measurement — it predates the "
            "spec gauge; re-measure to see it"
        ]
    return [
        "flatness not available for this measurement — the spatial "
        "measurement could not be analysed"
    ]


def _v2(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(status.get("crossover_v2"))


def _step_payload(active_step: str, done_steps: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for step_id in _STEP_IDS:
        rows.append({
            "id": step_id,
            "label": _STEP_LABELS[step_id],
            "status": (
                "done" if step_id in done_steps
                else "active" if step_id == active_step
                else "pending"
            ),
        })
    return rows


def _progress(active_step: str) -> dict[str, int]:
    try:
        position = _STEP_IDS.index(active_step) + 1
    except ValueError:
        position = len(_STEP_IDS)
    return {"position": position, "total": len(_STEP_IDS)}


def _done_before(active_step: str) -> set[str]:
    """Every step strictly before the active one is done (monotonic journey)."""
    try:
        frontier = _STEP_IDS.index(active_step)
    except ValueError:
        frontier = len(_STEP_IDS)
    return set(_STEP_IDS[:frontier])


def _applied_chip(status: Mapping[str, Any]) -> dict[str, str]:
    """Durable applied-crossover chip — reuse the legacy contract shape."""
    contract = _mapping(_mapping(status.get("setup")).get("applied_crossover"))
    if contract.get("valid") is not True:
        return {"state": "none", "label": "No speaker profile applied"}
    owner = str(contract.get("owner") or "")
    if owner == "automatic":
        return {"state": "automatic", "label": "Automatic crossover applied"}
    if owner == "manual":
        return {"state": "manual", "label": "Manual crossover applied"}
    return {"state": "applied", "label": "Speaker profile applied"}


def _setup_ready(status: Mapping[str, Any]) -> bool:
    setup = _mapping(status.get("setup"))
    return setup.get("active") is True and setup.get("status") == "ready"


# --- tier chooser (flow-simplification §3) ------------------------------------

_TIER_LABELS = {TIER_FULL: "Full measurement", TIER_EXPRESS: "Quick tune"}
_TIER_CLAIMS = {
    # B2 fix (adversarial review of PR #1780): "across the room" overclaimed
    # past what the post-apply cloud actually samples — a handful of prompted
    # spots around the mark, never the room at large. "at several spots
    # around the mark" is the honest description of the same cloud.
    TIER_FULL: "re-checks the result at several spots around the mark",
    TIER_EXPRESS: "confirms the result at the mark",
}


def _recommended_tier(status: Mapping[str, Any]) -> str:
    """Full recommended UNTIL a Full-tier commission has completed on this
    topology (coordinator ruling, adversarial review of PR #1780, S4) — the
    choice is always the household's; history decides only which option
    carries the Recommended badge, never a silent default.

    Keyed on TWO signals, both required:

    1. ``_applied_chip``'s ``"automatic"`` state — an automatically-tuned
       crossover is currently valid for THIS topology.
       ``crossover_snapshot_state`` (the contract behind
       ``applied_crossover``) is topology-scoped, refusing as
       ``active_applied_profile_snapshot_topology_stale`` the moment the
       topology changes, so reaching "automatic" here already means this
       exact topology — no new persisted state needed.
    2. The durable v2 state's own ``tier`` being ``TIER_FULL`` specifically
       (N5a fix: an earlier revision of this docstring glossed
       ``"automatic"`` as "(v2-measured)", which overclaims —
       ``_snapshot_owner`` also reads a LEGACY per-driver flow's measured
       result as ``"automatic"`` via its ``level_match``/
       ``corrections_source`` fallback, and that flow predates tiers
       entirely). Requiring BOTH signals is what keeps an express-only
       household seeing Full recommended — the §1.3 HF-null mitigation
       this ruling exists to preserve: a Quick-tune-only topology has never
       actually had the wider, comb-decorrelating walk, so Full stays
       recommended until one completes.
    """
    if _applied_chip(status)["state"] != "automatic":
        return TIER_FULL
    return TIER_EXPRESS if str(_v2(status).get("tier") or "") == TIER_FULL else TIER_FULL


def _tier_action(
    tier: str, info: Mapping[str, Mapping[str, int]], *, recommended: bool,
) -> dict[str, Any]:
    detail = info[tier]
    return {
        "id": f"start_v2_session_{tier}",
        "label": _TIER_LABELS[tier],
        # One-line claims difference (§1.3/§3), derived from the plan shape —
        # never a hand-written prettier figure (§1.1).
        "description": (
            f"About {detail['estimated_minutes']} min — {detail['capture_target']} "
            f"measurements; {_TIER_CLAIMS[tier]}."
        ),
        "recommended": recommended,
        "endpoint": "/correction/crossover/v2/session",
        "body": {"tier": tier},
    }


def _tier_choice_actions(
    status: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The microphone_check screen's tier chooser: both tiers first-class,
    the recommended one primary — never a silent default (§3).

    ``tier_display_info()`` is called ONCE here (N1 fix, adversarial review
    of PR #1780) rather than once per action — it is memoized, so this is a
    minor cleanup, not a correctness fix.
    """
    info = tier_display_info()
    recommended = _recommended_tier(status)
    other = TIER_FULL if recommended == TIER_EXPRESS else TIER_EXPRESS
    return (
        _tier_action(recommended, info, recommended=True),
        [_tier_action(other, info, recommended=False)],
    )


def _envelope(
    *,
    screen: str,
    active_step: str,
    verdict: str,
    nudges: list[dict[str, str]] | None = None,
    next_action: dict[str, Any] | None = None,
    alternate_actions: list[dict[str, Any]] | None = None,
    status: Mapping[str, Any],
    candidate_review: Mapping[str, Any] | None = None,
    expert_details: list[str] | None = None,
    advertise_relay: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
        "flow": "v2",
        "screen": screen,
        "active": True,
        "steps": _step_payload(active_step, _done_before(active_step)),
        "verdict_text": verdict,
        "nudges": nudges or [],
        # Optional collapsed expert-disclosure lines (the verify_fail screen's
        # tracking numbers, #1605) — the frontend folds them behind a
        # <details>. Empty on every screen that has none.
        "expert_details": list(expert_details or []),
        # A terminal / restart screen must stop advertising the dead phone link
        # and its QR (W6.10 fold-in) — the session it pointed at is gone.
        "relay": (_mapping(status.get("relay")) or None) if advertise_relay else None,
        "next_action": next_action,
        "alternate_actions": alternate_actions or [],
        "progress": _progress(active_step),
        "applied": _applied_chip(status),
        "candidate_review": dict(candidate_review) if candidate_review else None,
        # Flat-linearization plan PR-4: the compact per-group honesty verdict
        # (spec pass/fail per band, excluded-interval count, geometry verdict
        # + its plain-language guidance copy) — the SAME projection
        # ``crossover_v2_status_block`` serves at ``/state``, so a wizard page
        # reading either surface sees the same numbers. ``None`` before any
        # cloud group has closed. This key is the plain-language verdict, not
        # the chart feed — see ``cloud_chart`` below.
        "cloud": _v2(status).get("cloud"),
        # PR-7: the before/after chart's own feed — the decimated combined
        # curve per phase, projected by
        # ``jasper.web.correction_crossover_v2._chart_cloud_status`` and
        # carried here unchanged (mirrors the "cloud" key's copy-through
        # pattern one line up). Kept off the ``cloud`` key above so the
        # doctor (which reads only ``cloud``) never has to parse curve-
        # shaped data mixed into it — this key still rides the SAME envelope
        # response as ``cloud`` (review S-1: a key split, not a smaller
        # payload; see ``_chart_cloud_status``'s own docstring for the
        # measured byte cost and its own re-decimation ceiling, the actual
        # size mitigation). ``None`` before any cloud group has closed, same
        # rule as ``cloud``.
        "cloud_chart": _v2(status).get("cloud_chart"),
        # Flow-simplification PR-U3: which commission instrument produced (or
        # is producing) this session — ``None`` when the durable state does
        # not say (pre-tier state, or no session yet). The chart module reads
        # this to tell "the post-apply cloud hasn't measured yet" (full, still
        # walking) apart from "there is no post-apply cloud coming" (express,
        # M=1) — the same unknown-vs-default rule as ``crossover_v2_status_block``'s
        # own ``tier`` key, copied through rather than re-derived.
        "tier": (
            str(_v2(status).get("tier"))
            if isinstance(_v2(status).get("tier"), str) and _v2(status).get("tier")
            else None
        ),
    }


def _verify_fail_envelope(
    code: str, message: str, status: Mapping[str, Any],
) -> dict[str, Any]:
    """The VERIFY-fail screen (§5.2): one default "Try again" + "Undo".

    Shared by ``REASON_VERIFY_OUT_OF_TOLERANCE`` / ``REASON_VERIFY_INCONCLUSIVE``
    (whose own REASON_REGISTRY template is already ``verify_fail``) AND the
    VERIFY-phase override in :func:`_failure_envelope` (W6.7 ruling 3) for any
    OTHER code surfacing once the candidate is applied — the household is
    entitled to the Undo affordance the moment something is live on the
    speaker, regardless of which check failed.

    ``verify_undo`` and ``verify_remeasure`` carry ``show_during_relay``
    (W6.12, the same seam W6.10 added for the review screen's Apply): the
    JS action-row renderer's relay-in-flight gate otherwise blanket-clears
    EVERY alternate action while the relay object is still transitioning
    (``finishing`` / ``committing`` / ``stopping`` — a real window right
    after a failed capture, before the phone side has fully wound down), so
    a household landing on this screen saw no buttons at all and had to
    guess "hit Stop" to make them reappear. ``verify_retry`` (the primary
    "Try again") deliberately keeps NO such flag: it starts a brand-new
    relay session, and doing that while the prior one is still tearing down
    is exactly the race the gate exists to prevent — Undo and Re-measure are
    the "get me out of this" affordances that must stay reachable
    regardless.
    """
    return _envelope(
        screen="verify_fail", active_step="verify",
        verdict=message,
        nudges=[{"code": code, "severity": "warn", "text": message}],
        next_action={
            "id": "verify_retry",
            "label": "Try again",
            "endpoint": "/correction/crossover/v2/verify",
            "body": {},
        },
        alternate_actions=[
            {
                "id": "verify_undo",
                "label": "Undo (restore previous sound)",
                # W6 run-8 Blocker Q fix: rides the v2-aware restore path
                # (jasper.web.correction_crossover_v2.handle_v2_restore),
                # which reloads the pre-candidate applied profile
                # ``handle_v2_apply`` stashed at apply time and clears the
                # durable v2 applied/candidate/failure state on success — the
                # legacy ``/crossover/restore`` expects a PENDING
                # commissioning-run candidate apply that a v2 apply never
                # creates, and 500s here instead.
                # OPEN CHECKLIST ITEM (W6.7 gate N2): a session reset that
                # clears the durable v2 state while the applied graph is
                # still live loses this Undo affordance (no verify-phase
                # state remains to render verify_fail from) — a future fix
                # should keep an Undo path reachable whenever an applied
                # candidate is in force, independent of a reset elsewhere.
                "endpoint": "/correction/crossover/v2/restore",
                "body": {},
                "show_during_relay": True,
            },
            {
                "id": "verify_remeasure",
                "label": "Re-measure",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
                "expert": True,
                "show_during_relay": True,
            },
        ],
        status=status,
        # Gauge fix (2026-07-24): the flatness lines are a SIBLING claim to
        # the integration-verify numbers above, distinctly labeled — both
        # travel in the same collapsed disclosure since this screen only
        # has the one "Expert details" mechanism.
        expert_details=_verify_expert_details(status) + _flatness_details_lines(status),
    )


def _failure_envelope(
    code: str, status: Mapping[str, Any], active_step: str, *, applied: bool,
) -> dict[str, Any]:
    """Render one of the four §5.10 templates from a reason code.

    Applied override (W6.7 ruling 3; generalized 2026-07-20 adversarial
    review — SF1 follow-up): once the crossover is DURABLY applied
    (``applied``, passed in as the raw ``status["crossover_v2"]["applied"]``
    state fact), ANY failure code renders through the ``verify_fail``
    template regardless of REASON_REGISTRY's own owning template.
    fix_and_retry / hard_stop / session_restart / silent_auto_retry all hide
    the Undo affordance the household is entitled to the moment something is
    live on the speaker (the run-7 hardware bug: an ``agc_behavioral_fail``
    during VERIFY rendered ``fix_and_retry`` and displaced the VERIFY-fail
    screen's Undo action). REASON_REGISTRY stays the single copy source —
    only the template choice is overridden here, EXCEPT the copy addendum
    just below.

    THIS MUST KEY ON THE STATE FACT, NEVER ON ``active_step``/phase. An
    earlier version of this override fired on ``active_step == "verify"``
    (reasoning: ``_phase_from_state`` only reports VERIFY once ``applied``
    is True, so the two were believed equivalent) — but a second
    adversarial pass found the CONVERSE direction false: a terminal-failure
    persist that lands WHILE the auto-apply transaction is still mid-flight
    (``_persist_terminal_failure`` sees ``applied`` still False at that
    instant, since the transaction hasn't committed yet) resets
    ``accepted_phases``, which makes ``_phase_from_state`` resolve back to
    ``PHASE_CHECK`` even once the OTHER thread's apply lands moments later
    and durably flips ``applied=True``. Deriving "was this applied" from
    phase would silently reproduce that exact bug in a new shape — the
    caller passes the state fact directly instead.

    Copy addendum (adversarial review, 2026-07-20): a ``TEMPLATE_SESSION_RESTART``
    code (``relay_timeout``, ``user_stopped``) owns copy that assumes NOTHING
    was ever applied ("start over…") — written for the pre-apply CHECK/MEASURE
    phases where that is true. Reaching this override with ``applied=True``
    means the auto-apply background thread's transaction genuinely landed,
    quite possibly racing the very failure being rendered (e.g. a phone Stop
    landing while the transaction was mid-flight — the auto-apply worker's
    own pre-apply check can't always win that race, since an in-flight DSP
    write can't be safely interrupted, and the durable-state coherence fix
    only guarantees the FINAL STATE is honest, not that every intermediate
    render along the way sees it — this is what makes the render side of
    that guarantee actually hold too). Never let the household believe
    nothing changed when it did: this appends an honest acknowledgment
    rather than leaving a "start over" claim uncontested.
    """
    spec = REASON_REGISTRY.get(code)
    if spec is None:  # defensive — an unknown code still names a retry, never a bare code
        if applied:
            return _verify_fail_envelope(
                code, "Something went wrong with that measurement. Try again.", status,
            )
        return _envelope(
            screen="fix_and_retry", active_step=active_step,
            verdict="Something went wrong with that measurement. Try again.",
            next_action={"id": "retry", "label": "Try again"},
            status=status,
        )
    if applied and spec.template != TEMPLATE_VERIFY_FAIL:
        message = spec.message or spec.banner
        if spec.template == TEMPLATE_SESSION_RESTART:
            message = (
                f"{message} The crossover was already applied — if it sounds "
                "worse than before, you can undo."
            )
        return _verify_fail_envelope(code, message, status)
    template = spec.template
    if template == TEMPLATE_SILENT_AUTO_RETRY:
        # No decision screen: stay on the phase screen with a banner; the phone
        # auto-retries (§5.10 template 1).
        return _envelope(
            screen=active_step, active_step=active_step,
            verdict=spec.banner,
            nudges=[{"code": code, "severity": "info", "text": spec.banner}],
            next_action=None,
            status=status,
        )
    if template == TEMPLATE_HARD_STOP:
        # hard_stop keeps the relay block (Finding D contract): the failure
        # copy + the phone's stopped/failed status stay visible together. The
        # renderer only shows the QR for an IN-FLIGHT relay, so a purged
        # session never re-advertises a live link here.
        return _envelope(
            screen="hard_stop", active_step=active_step,
            verdict=spec.message,
            nudges=[{"code": code, "severity": "warn", "text": spec.message}],
            next_action={"id": "speaker_setup", "label": "Back to speaker setup", "href": "/sound/"},
            status=status,
        )
    if template == TEMPLATE_SESSION_RESTART:
        return _envelope(
            screen="session_restart", active_step="microphone_check",
            verdict=spec.message,
            nudges=[{"code": code, "severity": "warn", "text": spec.message}],
            next_action={
                "id": "restart_session",
                "label": "Start over",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
            },
            status=status,
            # The session this screen replaced is dead — do not re-advertise its
            # phone link / QR (W6.10 fold-in). Start over mints a fresh one.
            advertise_relay=False,
        )
    if template == TEMPLATE_VERIFY_FAIL:
        # One default — "Try again" (internally re-verify once, then re-measure)
        # — plus "Undo (restore previous sound)"; the explicit trio lives behind
        # the expert disclosure (§5.2).
        return _verify_fail_envelope(code, spec.message, status)
    # TEMPLATE_FIX_AND_RETRY (the default decision screen).
    nudges = [{"code": code, "severity": "warn", "text": spec.message}]
    if active_step == "apply":
        # Layer the SPECIFIC blocked-apply issue
        # (jasper.web.correction_crossover_v2._persist_apply_blocked) on top
        # of the generic REASON_APPLY_FAILED headline — the household gets
        # both an honest generic outcome and, when available, the concrete
        # reason the auto-apply's own apply_baseline_profile seam gave.
        apply_blocked = _mapping(_v2(status).get("apply_blocked"))
        if apply_blocked:
            nudges.append({
                "code": str(apply_blocked.get("id") or "apply_blocked"),
                "severity": "warn",
                "text": str(apply_blocked.get("message") or spec.message),
            })
    return _envelope(
        screen="fix_and_retry", active_step=active_step,
        verdict=spec.message,
        nudges=nudges,
        next_action={
            "id": "retry",
            "label": "Try again",
            "endpoint": "/correction/crossover/v2/session",
            "body": {},
        },
        status=status,
    )


def build_crossover_envelope_v2(status: Mapping[str, Any]) -> dict[str, Any]:
    """The v2 conductor envelope (schema 8) for the served status."""
    if not bool(status.get("active")):
        return {
            "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
            "flow": "v2",
            "screen": "not_applicable",
            "active": False,
            "steps": [],
            "verdict_text": (
                "This speaker has no active crossover. Continue with room correction."
            ),
            "nudges": [],
            "relay": _mapping(status.get("relay")) or None,
            "next_action": {
                "id": "room", "label": "Correct the room", "href": "/correction/room/",
            },
            "alternate_actions": [],
            "progress": {"position": 0, "total": len(_STEP_IDS)},
            "applied": _applied_chip(status),
            "candidate_review": None,
            "cloud": None,
        }

    v2 = _v2(status)
    phase = str(v2.get("phase") or PHASE_CHECK)
    active_step = _PHASE_STEP.get(phase, "microphone_check")

    # Volume recovery keys on needs_recovery, NOT unresolved_volume_safety alone
    # (the W2 gate ruling — a crash-hydrated active plan surfaces no unresolved
    # payload but still needs draining).
    if bool(v2.get("needs_recovery")):
        spec = REASON_REGISTRY["volume_unresolved"]
        return _envelope(
            screen="volume_recovery", active_step="microphone_check",
            verdict=spec.message,
            nudges=[{
                "code": "crossover_v2_volume_unresolved",
                "severity": "warn",
                "text": spec.message,
            }],
            next_action={
                "id": "recover_volume",
                "label": "Recover safe listening volume",
                "endpoint": "/correction/crossover/recover-volume",
                "body": {},
            },
            status=status,
        )

    # Speaker setup must be proven before any measurement plays.
    if not _setup_ready(status):
        return _envelope(
            screen="speaker_setup", active_step="speaker_setup",
            verdict=(
                "Finish the protected speaker setup first. This proves the output "
                "map and tweeter protection before the microphone check can play."
            ),
            next_action={"id": "speaker_setup", "label": "Finish speaker setup", "href": "/sound/"},
            status=status,
        )

    failure = _mapping(v2.get("failure"))
    failure_code = str(failure.get("code") or "")
    if failure_code:
        # Pass the RAW state fact — never derive "was this applied" from
        # phase/active_step (see _failure_envelope's docstring for why that
        # derivation can itself be wrong).
        env = _failure_envelope(
            failure_code, status, active_step, applied=bool(v2.get("applied")),
        )
        log_event(
            logger, "correction.crossover_v2_envelope_serve",
            screen=env["screen"], phase=phase, failure=failure_code,
        )
        return env

    if phase == PHASE_CHECK:
        next_action, alternate_actions = _tier_choice_actions(status)
        env = _envelope(
            screen="microphone_check", active_step="microphone_check",
            # The journey-opening promise. Before the spatial cloud this said
            # "keep it in that one spot for the whole measurement" — false as
            # of PR-3b, and false on the FIRST screen the household reads,
            # which is the worst place for it. The mark is still where the
            # session starts and returns to; the moving is now named up front
            # rather than sprung on them at the third capture.
            #
            # Flow-simplification §3: the tier choice is the household's,
            # explicitly, every session — the two actions below are BOTH
            # first-class (never a silent default); which one is primary is
            # only history's Recommended badge (_tier_choice_actions).
            verdict=(
                "Place the microphone about 1 m in front of the speaker, at "
                "tweeter height and pointing at it — about where you'd sit to "
                "listen (see the picture). That spot is your mark. JTS runs a "
                "quick microphone check first, then measures from the mark and "
                "from a few nearby spots your phone will guide you to — that "
                "is what lets it tell the speaker apart from the room. Choose "
                "how thorough a measurement to run below."
            ),
            next_action=next_action,
            alternate_actions=alternate_actions,
            status=status,
        )
    elif phase == PHASE_MEASURE:
        env = _envelope(
            screen="measure", active_step="measure",
            verdict=(
                "Keep the phone still — JTS is measuring both drivers. Follow the "
                "phone; the measurement continues automatically."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_CLOUD_MEASURE:
        # Same wizard screen as MEASURE: the household is still measuring, and
        # the phone (not this page) is where the per-position instructions
        # live. What changes is the verdict copy, which has to stop telling
        # someone to keep the phone still when the whole point of this phase is
        # that they move it.
        env = _envelope(
            screen="measure", active_step="measure",
            verdict=(
                "JTS is measuring from a few different spots — follow the "
                "prompts on your phone. Moving the microphone between spots is "
                "what lets JTS tell the speaker apart from the room."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_APPLYING:
        # Owner ruling (2026-07-20): no human control page here anymore — the
        # conductor's own auto-apply is in flight (machine-paced seconds, not
        # a human wait). A blocked/errored auto-apply surfaces through the
        # generic ``failure`` branch above (REASON_APPLY_FAILED), never here;
        # by construction this branch only renders while genuinely pending.
        env = _envelope(
            screen="applying", active_step="apply",
            verdict="Applying the measured crossover to your speaker…",
            next_action=None,
            status=status,
        )
    elif phase == PHASE_VERIFY:
        verdict = (
            "The crossover is applied. Put the microphone back where it "
            "started and follow your phone to confirm the result"
        )
        # Express (M=1) has no post-apply cloud — this anchor is the WHOLE
        # post-apply check, not the first of several (§1.3 degraded-claims
        # table). Full says nothing extra here: its cloud walk follows.
        verdict += (
            " — this quick tune's only check, at the mark."
            if str(v2.get("tier") or "") == TIER_EXPRESS
            else "."
        )
        env = _envelope(
            screen="verify", active_step="verify",
            verdict=verdict,
            next_action=None,
            status=status,
            # B1 fix (adversarial review of PR #1780): Express's pre-apply
            # cloud has already closed by the time this screen renders (it
            # walks BEFORE VERIFY), so its before-tuning flatness/carve-out
            # disclosure is available here too, not just on the done screen.
            # Empty for Full at this point (its post-apply cloud has not
            # started yet) — harmless, same as before this fix.
            expert_details=_flatness_details_lines(status),
        )
    elif phase == PHASE_CLOUD_VERIFY:
        env = _envelope(
            screen="verify", active_step="verify",
            verdict=(
                "Checking the result from the same few spots — follow the "
                "prompts on your phone."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_DONE:
        # The RESULT screen (owner ruling, 2026-07-20): plain-language outcome
        # first — no numbers, no jargon — with the measured numbers folded
        # into the SAME collapsed "Technical details" disclosure the former
        # review screen used (_candidate_review_payload), and Undo given the
        # PRIMARY button so the household's safety net is the most visible
        # thing on the screen, not an afterthought behind an "expert" toggle.
        verify = _mapping(v2.get("verify"))
        candidate = _mapping(v2.get("candidate"))
        is_express = str(v2.get("tier") or "") == TIER_EXPRESS
        # Express disclosure (flow-simplification §1.3): the household is
        # told exactly what was verified ("confirmed at the mark") and named
        # the upgrade path — never a claim wider than what express measured
        # (no cross-position post-apply check exists for this tier). B2 fix
        # (adversarial review of PR #1780): "the verified-everywhere result"
        # overclaimed past what a Full measurement actually re-checks — a
        # handful of prompted spots around the mark, never every point in
        # the room.
        done_verdict = (
            "Your speaker is tuned and confirmed at the mark. If it sounds "
            "worse than before, you can undo. Run a Full measurement for "
            "the result checked at several spots around the mark."
            if is_express
            else "Your speaker is tuned. If it sounds worse than before, you can undo."
        )
        # PR-L4 item 7: the spec verdict gets a VOTE, on the primary copy.
        #
        # Both the headline above and the "Verified." badge below read the
        # TRACKING comparator (`verify.outcome`) — which asks whether the
        # speaker matched its own prediction, not whether it is flat. The one
        # instrument that compares the result to FLAT is the post-apply cloud's
        # spec verdict, and until now it reached exactly one surface: a line
        # inside the collapsed "Expert details" disclosure. On 2026-07-27 that
        # meant a household read "Your speaker is tuned" over a profile whose
        # own honest gauge had failed all three bands. The disclosure lines stay
        # (they carry the numbers); this puts the VERDICT where it cannot be
        # collapsed away.
        #
        # Only an explicit False speaks. `None` (never measured, or a group that
        # could not be graded) leaves the copy alone — express omits the
        # post-apply cloud entirely by design, and manufacturing a caveat out of
        # a missing measurement would be its own dishonesty.
        spec_passed = _spec_verdict(_cloud_verify_block(status))
        if spec_passed is False:
            done_verdict = (
                "Your speaker is tuned, but the result still measures further "
                "from flat than the target in at least one band. If it sounds "
                "worse than before, you can undo."
            )
        # PR-L4 item 4: applied implies graded, and when it does not, the
        # household is TOLD rather than restored behind their back. See
        # `_post_apply_grade` for why surfacing beats auto-restore here (a
        # missing grade says nothing about the correction, and express omits
        # the post-apply group by design). A failing grade already has its own
        # screen; this is the case where no check finished at all.
        elif not _mapping(v2.get("post_apply_grade")).get("graded", True):
            # Two different silences, two different sentences. "Never finished"
            # is false for an INCONCLUSIVE check — that one ran to completion
            # and could not decide, which is a different thing to tell someone
            # and points at a different fix (a quieter room, not a retry of a
            # step that died).
            grade_state = str(
                _mapping(v2.get("post_apply_grade")).get("state") or ""
            )
            done_verdict = (
                "Your speaker is tuned, but the check that confirms it could "
                "not tell either way — the room reflection cut the window "
                "short. Re-verify to try again, or undo to restore the "
                "previous sound."
                if grade_state == "inconclusive"
                else "Your speaker is tuned, but the check that confirms it "
                "never finished, so this result is unverified. Re-verify to "
                "confirm it, or undo to restore the previous sound."
            )
        alternate_actions = [
            {
                "id": "room",
                "label": "Continue to Room correction",
                "href": "/correction/room/",
            },
        ]
        if is_express:
            alternate_actions.append({
                "id": "run_full_measurement",
                "label": "Run a Full measurement",
                "endpoint": "/correction/crossover/v2/session",
                "body": {"tier": TIER_FULL},
            })
        env = _envelope(
            screen="done", active_step="verify",
            verdict=done_verdict,
            next_action={
                "id": "verify_undo",
                "label": "Undo (restore previous sound)",
                "endpoint": "/correction/crossover/v2/restore",
                "body": {},
            },
            alternate_actions=alternate_actions,
            # PR-L4 item 7: the badge may not claim more than the evidence.
            # "Verified." still means the tracking comparator passed, but a
            # speaker whose spec verdict FAILED gets the honest badge instead —
            # one claim per instrument, neither pretending to be the other.
            nudges=(
                [{
                    "code": "crossover_v2_out_of_spec",
                    "severity": "warn",
                    "text": "Verified against the prediction, but not flat to target.",
                }]
                if spec_passed is False and verify.get("outcome") == "pass"
                else [{
                    "code": "crossover_v2_verified",
                    "severity": "ok",
                    "text": "Verified.",
                }]
                if verify.get("outcome") == "pass" else []
            ),
            status=status,
            candidate_review=_candidate_review_payload(candidate or None),
            # Gauge fix (2026-07-24): the flatness numbers previously never
            # reached this screen at all — a household could watch "Your
            # speaker is tuned" (a PASS) with no visibility into how far
            # from flat the summed response actually was. Report-only, so
            # it rides the same collapsed disclosure as the integration
            # numbers would on a fail — this screen has none of those (a
            # PASS never showed verify_evidence, unchanged by this fix).
            expert_details=_flatness_details_lines(status),
        )
        # Terminal: mark every step done.
        env["steps"] = _step_payload("", set(_STEP_IDS))
        env["progress"] = {"position": len(_STEP_IDS), "total": len(_STEP_IDS)}
    else:
        next_action, alternate_actions = _tier_choice_actions(status)
        env = _envelope(
            screen="microphone_check", active_step="microphone_check",
            verdict="Choose how thorough a measurement to run below.",
            next_action=next_action,
            alternate_actions=alternate_actions,
            status=status,
        )

    log_event(
        logger, "correction.crossover_v2_envelope_serve",
        screen=env["screen"], phase=phase, failure="",
    )
    return env
