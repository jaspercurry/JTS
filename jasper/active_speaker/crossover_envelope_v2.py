# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 session screen envelope (Wave 5a; two-stage since PR-T3).

The pure ``status -> envelope`` function for the v2 screen sequence defined in
``docs/crossover-measurement-productization-design.md`` §5.9/§5.10. It emits
the envelope dict the generic data-driven JS renderer consumes, so that
renderer needs no v2-specific code. Its schema version is
:data:`CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION` below, read from there rather
than restated — two docstrings in this file had already drifted to different
stale numbers.

``docs/historical/crossover-measurement-productization-design.md``
§5.9/§5.10 defines the v2 screen sequence — ``("speaker_setup",
"microphone_check", "measure", "apply", "verify")`` — and the four
failure-screen TEMPLATES the flow
renders (silent auto-retry banner / fix-and-retry / hard stop / session
restart), plus the two special screens (``volume_recovery`` and the VERIFY-fail
one-default screen). This module is the pure ``status → envelope`` function for
that flow, reached directly or via
``jasper.web.correction_crossover_flow._build_envelope_logged`` (this call
plus a serve log) — the only crossover flow since W5b retired the legacy schema-6 envelope and the ``JASPER_CROSSOVER_FLOW`` selector. It emits the envelope dict shape the
generic data-driven JS renderer consumes (``schema_version`` / ``screen`` / ``steps`` / ``verdict_text`` /
``nudges`` / ``capture`` / ``next_action`` / ``alternate_actions`` / ``progress``
/ ``applied``) so the generic data-driven JS renderer needs no v2-specific code.

**Owner ruling (2026-07-20), SUPERSEDED.** It removed the human mid-flow Apply
gate — the former ``review_apply`` screen, a human tap over the measured
candidate — and had the session apply a trusted candidate itself. Two-stage T3
(commit ``61ba33ff1``, #1806 / #1906) replaced that: the apply is the
household's explicit POST now. Read
``jasper.active_speaker.crossover_v2_flow``'s module docstring for what replaced
it rather than this paragraph. This module's
``"applying"`` screen is the brief machine-paced in-flight state; the ``"done"``
screen is the RESULT screen — plain-language outcome first, numbers in a
collapsed expert disclosure, the way back to the previous tuning beside it.

The v2-specific state the backend threads onto the status lives under
``status["crossover_v2"]`` (phase / failure / verify / candidate /
apply_blocked / needs_recovery / applied); this module never re-derives it —
:class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session` owns
those decisions, the reason codes are
:mod:`jasper.active_speaker.crossover_v2.refusal_copy`'s, and this module maps
a reason code to its template copy through the shared
:data:`~jasper.active_speaker.crossover_v2.refusal_copy.REASON_REGISTRY`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from ..json_fields import finite_float as _finite
from ..log_event import log_event
from .angle_capture import AngleCaptureRequest, walk_price
from .angle_capture_spool import peek_staged_angle_request
from .attempts_loop import (
    PROVENANCE_MODEL_GRADED,
    PROVENANCE_REALIZED,
    REASON_ATTEMPT_NOT_COMPARABLE,
    REASON_AWAITING_FIRST_ATTEMPT,
    REASON_BASELINE_ESTABLISHED,
    REASON_BELOW_CLAIM_FLOOR,
    REASON_BUDGET_EXHAUSTED,
    REASON_DIRECTION_UNKNOWN_ABOVE_FLOOR,
    REASON_FLOOR_METRIC_MISMATCH,
    REASON_GRADED_BINS_SHRANK,
    REASON_IMPROVEMENT_ABOVE_FLOOR,
    REASON_IN_SPEC,
    REASON_NO_DEVIATION_AVAILABLE,
    REASON_NO_MATERIAL_IMPROVEMENT_PREDICTED,
    REASON_PREDECESSOR_NOT_COMPARABLE,
    REASON_PROVENANCE_MISMATCH,
    REASON_REGRESSION_FROM_PREDECESSOR,
    REASON_SITTING_MISMATCH,
    REASON_SITTING_UNRECORDED,
)
from .crossover_v2.journey import (
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_ENTRY_BASELINE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_CLOSING,
    PHASE_REVIEW,
    PHASE_VERIFY,
)
from .crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    ReasonSpec,
    TEMPLATE_HARD_STOP,
    TEMPLATE_SESSION_RESTART,
    TEMPLATE_SILENT_AUTO_RETRY,
    TEMPLATE_VERIFY_FAIL,
    reason_message,
    verify_inconclusive_cause,
)
from .crossover_v2_flow import (
    ATTEMPT_REASON_NO_FLOOR,
    CLAIM_NO_PER_BRANCH_CAPTURE,
    CLOUD_CLOSE_RUNNING,
    CrossoverV2FlowError,
    TIER_EXPRESS,
    TIER_REMOTE,
    TIER_FULL,
    resolve_plan_shape,
    tier_display_info,
)
from .crossover_v2.contracts import (
    ADOPTION_ROW_KEEP,
    ADOPTION_ROW_KEEP_FOR_ITERATION,
    ADOPTION_ROW_KEEP_ITERATING,
    ADOPTION_ROW_KEEP_MISSED_EXHAUSTED,
)
from .crossover_v2.refusal_copy import REASON_VOLUME_UNRESOLVED
# The round-outcome vocabulary this screen renders. Imported rather than
# re-typed: the four codes are picked by the web host's ``_post_apply_grade``,
# which this module may not import (#2662).
from .crossover_v2.verification import (
    RESULT_INCONCLUSIVE,
    RESULT_KEEP_PREVIOUS,
    RESULT_VERIFIED_BEST_EVALUATED,
    RESULT_VERIFIED_TARGET,
)
from .delta_probe import (
    REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND,
    VERDICT_FRAME_MISMATCH,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_SAFETY_ONLY,
)

logger = logging.getLogger(__name__)

# Bumped whenever the envelope's contract changes. The crossover wizard's own
# module is data-driven and does not gate on the version; it exists for the
# EXTERNAL DRIVER chaining rounds, which needs a stable way to know a contract
# is present rather than probing for a key that is ``null`` on every screen
# before VERIFY completes.
#
# 15 -> 16 is the first bump that is NOT additive: the in-flight block is
# ``capture`` and an action's render-while-live flag is ``show_during_capture``.
# Both rename a key 15 carried, so a driver pinned to 15 must be updated rather
# than left to read a missing key as "no session is holding".
CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION = 16

# The v2 step tuple (§5.9): these five are the whole journey.
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

# Which step is active for a given session phase. The position groups do NOT
# add wizard steps: a household walking the pre-apply cloud is still
# "measuring" and the post-apply cloud is still "verifying" — the cloud changed
# how many captures each step takes, not what the household is doing. The
# phone's own per-entry screens carry the within-step progress.
_PHASE_STEP = {
    PHASE_CHECK: "microphone_check",
    PHASE_MEASURE: "measure",
    PHASE_CLOUD_MEASURE: "measure",
    # Still measuring. This map is exhaustive — an unmapped phase raises rather
    # than falling back — and the REJECTION screens reuse whatever step it gives.
    PHASE_LATERAL: "measure",
    # #2291's entry baseline is the LAST thing stage 1 measures, and it is still
    # measuring: nothing has been applied and the apply decision has not been put
    # to the household yet.
    PHASE_ENTRY_BASELINE: "measure",
    # The review interlude sits on the APPLY step: measuring is finished and
    # what the household is now doing IS the apply decision. It shares the step
    # with PHASE_APPLYING deliberately. The measuring session's tail stays on
    # MEASURE: the household is still finishing a measurement.
    PHASE_CLOSING: "measure",
    PHASE_REVIEW: "apply",
    PHASE_APPLYING: "apply",
    PHASE_VERIFY: "verify",
    PHASE_CLOUD_VERIFY: "verify",
    PHASE_DONE: "verify",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# Presentation order for the per-role trim rows — woofer before tweeter reads
# low-to-high like the crossover itself; any other role falls to the end
# alphabetically.
_ROLE_ORDER = {"woofer": 0, "tweeter": 1}

# The top-octave centers the RESULT screen discloses per driver. The fit engine
# computes more (down to 250 Hz), but the household-facing disclosure only needs
# the top of the ladder, where an uncorrected driver's natural rolloff is
# otherwise invisible.
_TOP_OCTAVES_HZ = ("8000", "12000", "16000")


def _linearization_octave_rows(
    octaves: Any,
    reasons: Any,
    driver_classes: Any = None,
) -> list[dict[str, Any]]:
    """Per-role top-octave rows (>= 8k/12k/16k) — the OBSERVE-layer honesty
    ladder's disclosure numbers, already computed by the fit engine.

    Each value is achieved-minus-target dB. **A number here is not always a
    deficit** (#2638): the OBSERVE layer runs to the grid's own top, so past a
    driver's radiating band the same subtraction returns a large POSITIVE number
    that is the stopband's arithmetic, not performance (+23.0 dB at 16k on the
    2026-08-16 JTS3 candidate, whose largest filter gain was +2.5 dB). Each band
    carries the fit engine's own verdict code through as ``reason`` so the
    renderer can say which kind of number it holds; the vocabulary is
    ``ReasonCode``'s and this function neither reads nor interprets it.
    ``reason`` and ``driver_class`` are OMITTED, not empty-stringed, when a
    candidate predates them: present-and-known and absent-because-unrecorded
    stay structurally different.

    **These are FIT DIAGNOSTICS, not the measurement.** Per-driver, on the fit's
    own envelope grid, from the single design-axis MEASURE capture. The
    spec-facing summary on the same screen is :func:`_flatness_details_lines`,
    which reads the spatial cloud.
    """
    reason_rows = _mapping(reasons)
    class_rows = _mapping(driver_classes)
    rows: list[dict[str, Any]] = []
    for role, per_role in sorted(
        _mapping(octaves).items(),
        key=lambda kv: (_ROLE_ORDER.get(str(kv[0]), 99), str(kv[0])),
    ):
        if not isinstance(per_role, Mapping):
            continue
        per_role_reasons = _mapping(reason_rows.get(role))
        bands: list[dict[str, Any]] = []
        for hz in _TOP_OCTAVES_HZ:
            db = _finite(per_role.get(hz))
            if db is None:
                continue
            band: dict[str, Any] = {"hz": int(hz), "delta_db": db}
            code = per_role_reasons.get(hz)
            if isinstance(code, str) and code:
                band["reason"] = code
            bands.append(band)
        if bands:
            row: dict[str, Any] = {"role": str(role), "bands": bands}
            driver_class = class_rows.get(role)
            if isinstance(driver_class, str) and driver_class:
                row["driver_class"] = driver_class
            rows.append(row)
    return rows


def _headroom_cost_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """``{"db": float|None, "basis": str}`` — the correction's disclosed
    max-level cost, inseparable from the era that stamped it (#1808).

    ``basis`` is whichever stamped era the candidate carries, passed through
    rather than collapsed: the two peak eras disagree in the direction #2758
    opened, so telling them apart is the whole reason the field exists. Anything
    else is ``unknown``, including an absence, which is the only evidence of a
    pre-#1808 era there is.

    ``db`` is ``None`` — not ``0.0`` — when the stored value is missing or
    unusable. Zero is a real, common answer here (every cut-only correction
    charges nothing), so defaulting to it would state a measurement the payload
    does not have.
    """
    from .linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
        HEADROOM_COST_BASIS_UNKNOWN,
    )

    basis = candidate.get("headroom_cost_basis")
    known = (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    )
    return {
        "db": _finite(candidate.get("headroom_cost_db")),
        "basis": str(basis) if basis in known else HEADROOM_COST_BASIS_UNKNOWN,
    }


def _candidate_review_payload(
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Map the persisted ``_candidate_summary`` into the plain-language shape
    the "applying"/"done" screens render (§5.2: trims, delay, polarity, ripple,
    plus confidence/fingerprint provenance).

    The single conversion point from what ``_candidate_summary`` builds into
    rows the page can display; the renderer consumes exactly this shape. Reused
    on the RESULT screen, where the same numbers live behind the collapsed
    expert disclosure.
    """
    if not candidate:
        return None
    trims_db = _mapping(candidate.get("trims_db"))
    # A trim the round CARRIED rather than solved, per role. On the row itself
    # because unlike the crossover and the polarity below there is one per
    # driver; a pinned number is never worded as something this round measured.
    pinned = _mapping(candidate.get("trims_pinned"))
    trims: list[dict[str, Any]] = []
    for role, value in sorted(
        trims_db.items(), key=lambda kv: (_ROLE_ORDER.get(str(kv[0]), 99), str(kv[0]))
    ):
        db = _finite(value)
        if db is not None:
            trims.append({
                "role": str(role),
                "attenuation_db": db,
                "pinned": str(role) in pinned,
            })

    alignment = _mapping(candidate.get("alignment"))
    delay_us = _finite(alignment.get("delay_us"))
    delay_role = alignment.get("delay_role")
    delay: dict[str, Any] | None = None
    if delay_us is not None and isinstance(delay_role, str) and delay_role.strip():
        delay = {"role": delay_role, "delay_ms": delay_us / 1000.0}
    polarity = alignment.get("polarity")
    polarity_str = polarity if isinstance(polarity, str) and polarity.strip() else None

    crossover = _mapping(candidate.get("crossover"))
    payload: dict[str, Any] = {
        "trims": trims,
        # WHERE the reviewed graph crosses, and whether an operator PINNED it
        # there. Carried as values rather than pre-rendered prose: a pinned
        # number is worded "(pinned for this round)" and never as something the
        # round measured.
        "crossover": (
            {
                "fc_hz": _finite(crossover.get("fc_hz")),
                "order": crossover.get("order"),
                "slope_db_per_octave": _finite(crossover.get("slope_db_per_octave")),
            }
            if _finite(crossover.get("fc_hz")) is not None else None
        ),
        "crossover_pinned": bool(candidate.get("crossover_pinned")),
        "delay": delay,
        "polarity": polarity_str,
        # WHERE the polarity above came from (#2607 S3). One commitment — the
        # declared design, taken when the capture cannot support a polarity
        # decision — must not be worded as a measured result.
        "alignment_objective": str(candidate.get("alignment_objective") or ""),
        # The SECOND way a polarity can fail to be a measured result: the round
        # pinned it. Its own bit because it is a different claim — "as designed,
        # this measurement could not check it" versus "the operator held it
        # here" — and that list is also read for withdrawn-anchor objectives.
        "polarity_pinned": bool(candidate.get("polarity_pinned")),
        "confidence": _finite(candidate.get("alignment_confidence")),
        "ripple_db": _finite(candidate.get("predicted_ripple_db")),
        "fingerprint": str(candidate.get("fingerprint") or ""),
        "program_id": str(candidate.get("program_id") or ""),
        # WHY Layer-1a driver linearization did or did not run — the JS renderer
        # maps this enum to plain language.
        "linearization_outcome": str(candidate.get("linearization_outcome") or ""),
        # Per-role top-octave deficits.
        "linearization_octaves": _linearization_octave_rows(
            candidate.get("linearization_octaves"),
            candidate.get("linearization_octave_reasons"),
            candidate.get("linearization_driver_class"),
        ),
        # "This correction costs N dB of maximum level" (PR-L5), persisted by
        # ``correction_crossover_v2._candidate_summary``; this line carries it
        # the last hop, since the screens render from this payload.
        #
        # **A compound, not a bare float.** The charge's derivation changed under
        # #1808 and the stamp is not re-derived on load, so the same correction
        # discloses ~22.5 dB under the old rule where it now costs ~5. Pairing
        # the number with its basis makes that mistake unavailable: there is no
        # bare scalar on this payload to render.
        "headroom_cost": _headroom_cost_payload(candidate),
    }
    # A candidate with nothing displayable (no trims, no alignment) stays
    # hidden rather than rendering an empty card.
    if not trims and delay is None and polarity_str is None:
        return None
    return payload


def _verify_graded_band_lines(status: Mapping[str, Any]) -> list[str]:
    """"checked X-Y Hz" — the span VERIFY's tracking comparison graded.

    **One owner, rendered on every outcome** (#1868): this line used to be
    produced from the ``evidence`` block, which the host persists only on a
    NON-pass outcome, so the done screen's "Verified." badge was the one place
    that never said what was checked. The band is materially narrower than the
    crossover region a reader assumes — its lower edge is clamped up to the
    tweeter's sweep floor and again to the capture's validity floor.

    Empty when no tracking comparison was reached.
    """
    graded = _mapping(_v2(status).get("verify")).get("graded_band_hz")
    lo = _finite(graded[0]) if isinstance(graded, (list, tuple)) and graded else None
    hi = (
        _finite(graded[1])
        if isinstance(graded, (list, tuple)) and len(graded) == 2
        else None
    )
    if lo is None or hi is None:
        return []
    return [f"checked {lo:.0f}–{hi:.0f} Hz"]


def _verify_claims_lines(status: Mapping[str, Any]) -> list[str]:
    """What the crossover-region check found, and what was never checked.

    **One owner, rendered on every outcome** (R18, #1868): this says which of
    §7's claims were made at all, and two of the four never are, so a
    "Verified." badge over an unstated claim set reads as four proofs where
    there are two. The crossover-region line prints on a PASS too — the number
    IS the disclosure. A not-evaluated region claim prints nothing.
    """
    claims = _mapping(_mapping(_v2(status).get("verify")).get("claims"))
    lines: list[str] = []
    absolute = _mapping(claims.get("absolute"))
    worst_db = _finite(absolute.get("worst_db"))
    worst_hz = _finite(absolute.get("worst_hz"))
    tolerance_db = _finite(absolute.get("tolerance_db"))
    # The claim's OWN band, printed WITH the number (R18): this line lands under
    # ``checked …`` — the TRACKING band — and the dip can sit outside that one.
    # Two claims, two bands, said.
    band = absolute.get("band_hz")
    pair = band if isinstance(band, (list, tuple)) and len(band) == 2 else (None, None)
    lo, hi = _finite(pair[0]), _finite(pair[1])
    if (worst_db is not None and worst_hz is not None and tolerance_db is not None
            and lo is not None and hi is not None):
        lines.append(
            f"crossover blend {worst_db:+.2f} dB at {worst_hz:.0f} Hz "
            f"over {lo:.0f}–{hi:.0f} Hz (limit {tolerance_db:.1f} dB)"
        )
    # What is being DONE about the number directly above, placed where a reader
    # asks it: a household seeing the same blend number round after round, with
    # nothing saying the loop has a lever aimed at it, concludes nothing is
    # happening. Read off the durable receipt, never re-derived.
    blend = _mapping(_v2(status).get("round_receipt")).get("blend")
    cuts = _mapping(blend).get("filters")
    depths: list[float] = []
    if isinstance(cuts, (list, tuple)):
        for entry in cuts:
            gain = _finite(_mapping(entry).get("gain"))
            if gain is not None:
                depths.append(gain)
    if depths:
        lines.append(
            f"the next round trims this region "
            f"({len(depths)} cut{'s' if len(depths) > 1 else ''}, "
            f"deepest {min(depths):+.1f} dB)"
        )
    if _mapping(claims.get("woofer_branch")).get("reason") == CLAIM_NO_PER_BRANCH_CAPTURE:
        # Household terms: the reason code is about a capture plan, the
        # sentence is about what nobody knows yet.
        lines.append("each driver on its own was not checked")
    return lines


def _verify_frame_lines(
    status: Mapping[str, Any], *, raw_already_shown: bool,
) -> list[str]:
    """"frame offset X dB, tilt Y dB/oct" — what the comparison spanned.

    **One owner, rendered on every outcome.** The band says how WIDE the
    tracking claim is; this says how much of it was the instrument rather than
    the speaker. VERIFY differences an on-axis two-branch MODEL against an
    in-room gated MEASUREMENT, and on the 2026-07-29 corpus a single −0.79
    dB/octave tilt accounted for 84 % of the flow's apparent prediction error.

    **A tilt-removed grade is never rendered alone** — it is the friendlier
    number by construction. ``raw_already_shown`` says whether the caller has
    already put the raw pair on screen: the verify_fail screen has, the done
    screen has not (the host persists that block only on a non-pass), so on a
    pass this function prints the raw pair itself from the same record.

    Empty when no frame was fitted. Lines are terse because the wizard joins
    ``expert_details`` with "; " into one collapsed paragraph.
    """
    frame = _mapping(_mapping(_v2(status).get("verify")).get("frame"))
    offset_db = _finite(frame.get("offset_db"))
    tilt = _finite(frame.get("tilt_db_per_octave"))
    if offset_db is None or tilt is None:
        return []
    lines = [f"frame offset {offset_db:+.2f} dB, tilt {tilt:+.2f} dB/oct"]

    def _pair(max_key: str, rms_key: str) -> str:
        max_db = _finite(frame.get(max_key))
        rms_db = _finite(frame.get(rms_key))
        parts = []
        if max_db is not None:
            parts.append(f"level error {max_db:.2f} dB")
        if rms_db is not None:
            parts.append(f"tracking average error {rms_db:.2f} dB")
        return ", ".join(parts)

    raw = "" if raw_already_shown else _pair("max_db_raw", "rms_db_raw")
    tilt_removed = _pair("max_db_tilt_removed", "rms_db_tilt_removed")
    if raw:
        lines.append("raw: " + raw)
    if tilt_removed and (raw_already_shown or raw):
        lines.append("tilt-removed: " + tilt_removed)
    return lines


def _verify_gate(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """VERIFY's persisted gate record (``{"disclosure", "reflection_measured"}``).

    Empty when the state carries none — a legacy file, or a capture that could
    not be gated. Both are "the record does not say", rendered as silence.
    """
    return _mapping(_mapping(_v2(status).get("verify")).get("gate"))


def _verify_gate_lines(status: Mapping[str, Any]) -> list[str]:
    """What the gate did, in the one sentence that owns saying it (#1966).

    **One owner, rendered on every outcome.** Until this the screens said
    "checked 2000-4000 Hz" over a window whose validity floor and provenance
    were invisible.

    **Rendered verbatim, never re-phrased.** The string is
    :func:`~jasper.audio_measurement.gate_disclosure.describe_gate`'s, composed
    at verdict time and persisted; re-wording is precisely how "a reflection was
    measured at X ms" and "no reflection found; window capped at the ceiling"
    started printing identically.

    **Whose gate — the caller owns that question.** The record always describes
    ONE capture, but not necessarily the one the surrounding screen is about,
    because it deliberately survives an early-return retry. One of
    ``describe_gate``'s four sentences is DEICTIC ("this capture could not be
    gated") and takes its referent from the screen, so a caller renders this
    only where the referent is unambiguous — see :func:`_verify_expert_details`.

    Empty when no gate was recorded.
    """
    disclosure = _verify_gate(status).get("disclosure")
    if not isinstance(disclosure, str) or not disclosure:
        return []
    return [disclosure]


def _verify_gate_reflection_measured(status: Mapping[str, Any]) -> bool | None:
    """Whether VERIFY's gate actually found a reflection — or ``None``, unknown.

    The one fact the inconclusive copy branches on (#1974). ``None`` is a third
    state, not a falsy second one: a state file written before this shipped does
    not say, and the copy owner answers an unknown cause by naming no cause.
    """
    measured = _verify_gate(status).get("reflection_measured")
    return measured if isinstance(measured, bool) else None


def _verify_code(status: Mapping[str, Any]) -> str | None:
    """WHICH VERDICT produced the persisted verify outcome, or ``None``.

    The other half of the pair :func:`_verify_gate_reflection_measured` feeds,
    read through the same accessor so the two can never be taken from different
    blocks (#1974). ``None`` is unknown, not "no verdict".
    """
    code = _mapping(_v2(status).get("verify")).get("code")
    return code if isinstance(code, str) and code else None


def _verify_level_reference_lines(status: Mapping[str, Any]) -> list[str]:
    """"level reference reset for this session…" — the #1927 disclosure.

    **One owner, rendered on every outcome**: a PASS is precisely when an
    unstated reset would let a household read cross-day identity into a claim
    that only ever covered this sitting. Since #1927 every verify session
    establishes its own G3 pilot-transfer reference rather than inheriting the
    previous one; what the household gives up is cross-session comparability.

    Dated, and inform-not-berate (#1942): the date is what keeps this history
    rather than a verdict about the room, and nothing here accuses the
    microphone. Phrased as one terse parenthetical because of where it lands —
    the wizard joins ``expert_details`` into a single collapsed paragraph.

    Empty when there was nothing to disclose.
    """
    reset = _mapping(_mapping(_v2(status).get("verify")).get("level_reference"))
    step_db = _finite(reset.get("step_db"))
    if step_db is None:
        return []
    # ``_record_when_phrase`` already answers "earlier" for a stamp it cannot
    # place on a calendar, so every dated surface phrases a date the same way
    # rather than growing a second formatter.
    when = _record_when_phrase({"at": reset.get("prior_at")})
    return [
        "level reference reset for this session "
        f"(the previous one, {when}, was {step_db:.2f} dB away)"
    ]


def _verify_expert_details(
    status: Mapping[str, Any], *, headline_code: str,
) -> list[str]:
    """The verify_fail screen's collapsed expert numbers (#1605): the gated
    level error against its limit, the average error, and the band checked.
    Empty when the session persisted neither tracking evidence nor a graded band
    (an early-return verify verdict reaches neither).

    ``headline_code`` is the reason code THIS screen's verdict copy names. It
    gates the gate line alone — see the call below.

    The band line is appended INDEPENDENTLY of the evidence block rather than
    inside its guard: the two have different presence conditions, and a screen
    that has the band should print it even when the numbers beside it are gone.

    These lines and :func:`_flatness_details_lines`'s are two DIFFERENT
    constructions landing in the same collapsed disclosure: this comparator
    answers "did apply do what the model predicted" on the single design-axis
    capture, while the flatness lines answer "is the speaker flat" on the
    spatial cloud. The ``tracking`` prefix here is the one-word disambiguator.
    """
    evidence = _mapping(_mapping(_v2(status).get("verify")).get("evidence"))
    lines: list[str] = []
    if evidence:
        max_db = _finite(evidence.get("max_db"))
        tolerance_db = _finite(evidence.get("tolerance_db"))
        if max_db is not None and tolerance_db is not None:
            lines.append(f"level error {max_db:.2f} dB (limit {tolerance_db:.1f} dB)")
        elif max_db is not None:
            lines.append(f"level error {max_db:.2f} dB")
        rms_db = _finite(evidence.get("rms_db"))
        if rms_db is not None:
            lines.append(f"tracking average error {rms_db:.2f} dB")
    # Whether the evidence block above put the RAW pair on this screen. It owns
    # those numbers here, so the frame block below defers to it.
    raw_already_shown = bool(lines)
    lines.extend(_verify_graded_band_lines(status))
    # WHICH §7 claims were proved (R18, #1868) — same independent-presence terms
    # as the band above: the band bounds the tracking claim's width, this says
    # tracking is not the only claim.
    lines.extend(_verify_claims_lines(status))
    # The frame those numbers were measured ACROSS, on the same terms as the
    # band: independent of the evidence guard, because a screen that has a frame
    # should say so even when the raw numbers beside it are missing.
    lines.extend(_verify_frame_lines(status, raw_already_shown=raw_already_shown))
    # And WHAT THE COMPARISON COULD SEE (#1966): the band says how wide the
    # claim is, the frame how much of it was the instrument, the gate how much of
    # the sound the measurement had to work from.
    #
    # NOT the same independent-presence rule as those two. The band and frame are
    # cleared by every later attempt, so they can only describe the capture
    # behind THIS headline; the gate record deliberately survives an early-return
    # retry, so here it can outlive the verdict being displayed. One of
    # ``describe_gate``'s sentences is DEICTIC and would then point at the wrong
    # capture, so the line renders only when the headline's verdict is the one
    # that wrote the record. The done screen has no such ambiguity.
    if headline_code and headline_code == _verify_code(status):
        lines.extend(_verify_gate_lines(status))
    return lines


def _band_edges(value: Any) -> tuple[float, float] | None:
    """``(lo_hz, hi_hz)`` from a persisted two-element band pair, or ``None``.

    One spelling for the several band pairs a ``flatness`` block carries, so a
    block written by an older build degrades the same way everywhere: the line
    that needed the pair is not rendered, rather than half-rendered.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lo, hi = _finite(value[0]), _finite(value[1])
    return None if lo is None or hi is None else (lo, hi)


# Below this the printed step ("0.00 dB") no longer supports a direction, so the
# "X sits above Y" clause is dropped rather than asserting an ordering the
# rendered precision cannot show.
_TILT_DIRECTION_FLOOR_DB = 0.005


def _attribution_lines(
    flatness: Mapping[str, Any], band_lo: float | None, band_hi: float | None,
) -> list[str]:
    """The two lines that stop the worst-band pointer from being read as "here
    is the peak to EQ" (#1857).

    The pointer is a distance from a pooled reference, so a band INSIDE that
    pool that is uniformly off drags the shared zero toward itself and inflates
    every other band's number: the corpus session that filed the issue read
    ``+4.84 dB @ 1339.6 Hz`` against a woofer flat to +/-0.1 dB, because a ~5 dB
    dark tweeter had pulled the frame ~3 dB down. The low-mid frame (ADR-0194)
    took the two upper bands out of the pool; a defect inside 250 Hz-2 kHz still
    drags its own frame, which is what these two lines are for.

    Line one splits the pointer's number into where the whole band SITS versus
    what the curve does INSIDE it. Line two is the band-to-band step, the one
    reading here that no reference-frame choice can move — and the reading to
    trust when the two disagree (ADR-0194).

    **Disclosure only — decides nothing.** Every figure is copied from the same
    report the pointer reads; no verdict moves and the reference frame is
    untouched.
    """
    lines: list[str] = []
    level_db = _finite(flatness.get("max_band_level_deviation_db"))
    ripple_db = _finite(flatness.get("max_band_ripple_db"))
    if level_db is not None and ripple_db is not None:
        where = (
            f"the whole {band_lo:.0f}–{band_hi:.0f} Hz band"
            if band_lo is not None and band_hi is not None
            else "the whole band"
        )
        lines.append(
            f"of that, {level_db:+.2f} dB is where {where} sits; its own worst "
            f"excursion from that level is {ripple_db:+.2f} dB"
        )
    tilt = flatness.get("tilt")
    tilt = tilt if isinstance(tilt, Mapping) else {}
    step_db = _finite(tilt.get("step_db"))
    high = _band_edges(tilt.get("high_band_hz"))
    low = _band_edges(tilt.get("low_band_hz"))
    if tilt.get("evaluable") is True and step_db is not None:
        direction = (
            f": {high[0]:.0f}–{high[1]:.0f} Hz sits above "
            f"{low[0]:.0f}–{low[1]:.0f} Hz"
            if high is not None and low is not None
            and step_db >= _TILT_DIRECTION_FLOOR_DB
            else ""
        )
        lines.append(
            f"band levels differ by {step_db:.2f} dB, a reading no reference "
            f"choice moves{direction}"
        )
    return lines


def _flatness_lines_from_block(flatness: Mapping[str, Any]) -> list[str]:
    """The numeric flatness lines shared by both branches of the expert
    disclosure — max/avg deviation plus the excluded-bin count.

    Extracted so the post-apply CURRENT-STATE claim
    (:func:`_flatness_details_lines`) and the BEFORE-TUNING claim
    (:func:`_pre_apply_flatness_lines`) compute identical arithmetic from
    whichever compact ``flatness`` block they were handed.

    **The line NAMES its reference frame** (#1857). The zero is a power mean
    pooled over ``reference_band_hz``, and on a speaker with one dark driver
    that frame sits materially below a driver-anchored one — enough to flip the
    worst-band pointer's sign and move it to a different driver. A block without
    the key keeps the previous unqualified wording rather than naming a frame
    this code would be guessing at. :func:`_attribution_lines` renders how much
    of the number is the frame, immediately under the pointer.
    """
    lines: list[str] = []
    max_db = _finite(flatness.get("max_db"))
    max_hz = _finite(flatness.get("max_hz"))
    tolerance_db = _finite(flatness.get("tolerance_db"))
    band = _band_edges(flatness.get("max_band_hz"))
    band_lo, band_hi = band if band is not None else (None, None)
    ref = _band_edges(flatness.get("reference_band_hz"))
    ref_lo, ref_hi = ref if ref is not None else (None, None)
    if max_db is not None:
        where = f" at {max_hz:.0f} Hz" if max_hz is not None else ""
        against = (
            f" (spec {band_lo:.0f}–{band_hi:.0f} Hz, tolerance ±{tolerance_db:.1f} dB)"
            if band_lo is not None and band_hi is not None and tolerance_db is not None
            else ""
        )
        frame = (
            f"the {ref_lo:.0f}–{ref_hi:.0f} Hz reference mean"
            if ref_lo is not None and ref_hi is not None
            else "the spec reference"
        )
        lines.append(f"flatness {max_db:+.2f} dB from {frame}{where}{against}")
        lines.extend(_attribution_lines(flatness, band_lo, band_hi))
    rms_db = _finite(flatness.get("rms_db"))
    if rms_db is not None:
        lines.append(f"flatness average error {rms_db:.2f} dB across the spec bands")
    graded = flatness.get("n_bins")
    excluded = flatness.get("n_excluded")
    if isinstance(graded, int) and isinstance(excluded, int) and excluded > 0:
        # Bins, not "regions": an interval count would over-report here, since
        # it spans the whole axis including frequencies no spec band grades.
        lines.append(
            f"{excluded} of {graded + excluded} spec-band bins excluded from "
            "grading (interference, or below the measurement's validity floor)"
        )
    return lines


def _per_band_flatness_lines(spec_bands: Any) -> list[str]:
    """Every graded band's OWN worst deviation, from the SAME reference the
    pointer line above names (#1857).

    ``_flatness_lines_from_block`` names ONE band — whichever read furthest from
    the reference — but that reference is pooled over the low-mid band, so a
    band INSIDE it that is uniformly off drags the shared zero and can make an
    unrelated band's ordinary ripple read as the LARGER deviation. That is the
    failure #1857 reports: a shipped verdict read "+4.84 dB @ 1339.6 Hz" (the
    woofer band) while the tweeter sat uniformly ~5 dB dark. Showing every
    band's own number beside the pointer's means a reader is never limited to
    the band the gauge happened to pick.

    **Disclosure only — decides nothing.** Every figure is copied verbatim from
    ``spec_bands``; nothing is recomputed and the reference frame is untouched.
    Unevaluable bands are silently skipped, and ``[]`` is returned when nothing
    survives.
    """
    if not isinstance(spec_bands, list):
        return []
    parts: list[str] = []
    for band in spec_bands:
        if not isinstance(band, Mapping):
            continue
        lo = _finite(band.get("f_lo_hz"))
        hi = _finite(band.get("f_hi_hz"))
        deviation_db = _finite(band.get("max_deviation_db"))
        tolerance_db = _finite(band.get("tolerance_db"))
        passed = band.get("passed")
        if (
            lo is None or hi is None or deviation_db is None
            or tolerance_db is None or not isinstance(passed, bool)
        ):
            continue
        margin_db = abs(deviation_db) - tolerance_db
        compare = f"{margin_db:.1f} dB outside" if not passed else "within"
        parts.append(
            f"{lo:.0f}–{hi:.0f} Hz {deviation_db:+.2f} dB "
            f"({compare} the ±{tolerance_db:.1f} dB target)"
        )
    if not parts:
        return []
    return ["every band from the same reference: " + ", ".join(parts)]


def _flatness_details_lines(status: Mapping[str, Any]) -> list[str]:
    """The spec-facing flatness disclosure — "how flat is the speaker" —
    distinctly labeled from :func:`_verify_expert_details`'s integration-verify
    lines, which answer "did the crossover integrate as predicted" and gate.

    Reads the cloud group's spec gauge — ``spec_flatness_gauge`` of the same
    ``evaluate_flat_spec`` report ``/state``, the doctor check and the bundle
    artifact read — copied through ``_compact_cloud_status``, so the number here
    and the number in the report are the same bytes.

    **The choice is WHICH CLOUD EXISTS, not which tier** (#1965): post-apply
    cloud if there is one, otherwise the pre-apply cloud. A tier test was right
    about Express and wrong about STAGE 1, where Full rendered NOTHING on the
    apply-decision screen while Express rendered the same measured cloud. The
    pre-apply cloud is the UNCORRECTED baseline, so its branch reads it under an
    explicit BEFORE-TUNING frame and never as "how flat your speaker is now".

    Empty when neither group has closed. The fallback vocabulary for a
    post-apply group that closed but produced no usable gauge lives in
    :func:`_flatness_unavailable_line`.

    The carve-out lines close the sentence (PR-6b, owner decision 1): the
    excluded-bin count says how much of the spectrum left grading,
    :func:`_carve_out_expert_lines` says which ranges and why, with τ/r — on
    every tier, since carve-outs are a post-apply-persistent fact.
    """
    block = _cloud_verify_block(status)
    if not block:
        return _pre_apply_flatness_lines(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return _flatness_unavailable_line(block)
    if not flatness.get("evaluable"):
        # The gauge ran and could not measure — read ``SpecFlatness.passed`` with
        # ``evaluable``. Never render this as a pass or a fail. The carve-out
        # lines ride along because in this state they ARE the explanation.
        return [
            "flatness could not be measured — every spec band was excluded "
            "or out of range"
        ] + _carve_out_expert_lines(block)
    lines = _flatness_lines_from_block(flatness)
    lines.extend(_per_band_flatness_lines(block.get("spec_bands")))
    lines.extend(_carve_out_expert_lines(block))
    return lines


def _pre_apply_flatness_lines(status: Mapping[str, Any]) -> list[str]:
    """The BEFORE-TUNING flatness/carve-out disclosure — the branch
    :func:`_flatness_details_lines` takes whenever no post-apply cloud exists.

    Reads the CLOUD-MEASURE compact block and frames its numbers explicitly as
    the BEFORE-TUNING state, never as "how flat your speaker is now" (that claim
    needs a post-apply cloud). Carve-out lines render VERBATIM, unprefixed,
    because they are a distinct post-apply-persistent fact required on every
    tier rather than a claim about the CURRENT state.

    Two readers (#1965): Express takes this branch permanently, and Full takes
    it on the STAGE-1 screens.

    **The scope clause is a claim about the post-apply check, so it renders only
    where one has PASSED.** "The applied correction targets these; the result was
    confirmed at the mark only" says a correction is applied AND that the only
    confirmation was the single anchor sweep, and a passing post-apply tracking
    verify is exactly the state where both are true.
    """
    block = _cloud_measure_block(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return []
    if not flatness.get("evaluable"):
        # Same capitalized lead as the evaluable arm below, which read as a
        # fragment beside its sibling while lowercase.
        return [
            "Measured before tuning: flatness could not be measured — every "
            "spec band was excluded or out of range"
        ] + _carve_out_expert_lines(block)
    numeric = "; ".join(
        _flatness_lines_from_block(flatness)
        + _per_band_flatness_lines(block.get("spec_bands"))
    )
    line = f"Measured before tuning: {numeric}"
    if _mapping(_v2(status).get("verify")).get("outcome") == "pass":
        line += (
            ". The applied correction targets these; the result was confirmed "
            "at the mark only"
        )
    lines = [line]
    lines.extend(_carve_out_expert_lines(block))
    return lines


def _carve_out_expert_lines(block: Mapping[str, Any]) -> list[str]:
    """The carve-out τ/r lines (PR-6b, owner decision 1).

    The expert layer: the line above says HOW MANY spec-band bins left grading;
    these say WHICH ranges and WHY, with the delay and reflection ratio that
    identified them. One line per band that carved anything, in band order;
    nothing at all when nothing was carved.

    **The strings are copied, not composed here.** ``carve_outs_by_band`` in
    ``crossover_v2_flow`` owns the carve-out copy, so this and the chart callouts
    render the same words. This function only prefixes the band.

    Takes a compact cloud-phase BLOCK directly rather than ``status`` — the
    caller picks which cloud, since carve-outs are disclosed from whichever
    cloud the session actually produced.
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


# --- the review interlude (two-stage commission D3 / D6) ---------------------

# Mirrors ``jasper.web.correction_crossover_v2.STAGE2_PREFLIGHT_KEY``, spelled
# through a module constant on both sides since the writer lives in the web
# package and this reader does not.
_STAGE2_PREFLIGHT_KEY = "stage2_preflight"


def _stage2_preflight(status: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    """``(can_open_stage_2, refusal_message, refusal_action)`` for the review
    screen's refusal DISCLOSURE (a render-time preflight; the apply
    transaction's ``_assert_stage_2_can_open`` is the boundary that refuses).

    **Absence is not a clean reading.** An unset key means the predicate never
    ran, and "we never checked" must disclose exactly like "we checked and it
    refused". Only an explicit ``ok: True`` renders quiet. The message is passed
    through verbatim — every refusal is already household-facing and already
    names what to finish first.
    """
    preflight = _mapping(_v2(status).get(_STAGE2_PREFLIGHT_KEY))
    if preflight.get("ok") is True:
        return True, "", None
    message = str(preflight.get("message") or "").strip()
    action = preflight.get("next_action")
    return (
        False,
        message or (
            "JTS cannot start the confirming measurement on this speaker yet, "
            "so applying this now would leave it unchecked."
        ),
        dict(action) if isinstance(action, Mapping) and action else None,
    )


def _worst_failing_band(bands: Any) -> tuple[float, float, float] | None:
    """``(f_lo_hz, f_hi_hz, overshoot_db)`` for the band that misses the target
    by the most, or ``None`` when no band carries a gradeable miss.

    The overshoot is how far past the band's own tolerance the deviation
    reaches, not the raw deviation, which says nothing about whether it was
    allowed. Reads only bands the report marks ``passed is False``: treating an
    ungraded band as failing would manufacture a verdict out of a missing
    measurement.
    """
    if not isinstance(bands, list):
        return None
    worst: tuple[float, float, float] | None = None
    for band in bands:
        if not isinstance(band, Mapping) or band.get("passed") is not False:
            continue
        lo = _finite(band.get("f_lo_hz"))
        hi = _finite(band.get("f_hi_hz"))
        deviation = _finite(band.get("max_deviation_db"))
        tolerance = _finite(band.get("tolerance_db"))
        if lo is None or hi is None or deviation is None or tolerance is None:
            continue
        overshoot = abs(deviation) - abs(tolerance)
        if overshoot <= 0:
            continue
        if worst is None or overshoot > worst[2]:
            worst = (lo, hi, overshoot)
    return worst


def _review_verdict(prediction: Mapping[str, Any] | None, has_candidate: bool) -> str:
    """The review screen's primary copy — D3's items 3 and 4, in the
    household's language.

    **The prediction is a MODEL, and every sentence here says so.** The measured
    curve gets "measured" and the predicted one never does — it is what JTS
    "expects" or "works out", checked against the target rather than against the
    room.

    All four of ``_prediction_status``'s states render distinctly, because a
    consumer that collapses them tells a household something untrue:

    1. **Curve + report** — a graded pass says so; a graded miss names the band
       and the margin and is still offered, because improved-but-failing is
       presented, never applied silently (D3.4).
    2. **Curve, no report** — say the verdict is unknown; never infer one.
    3. **Neither** — nothing to review at all.
    4. **Report, no curve** — the refusal lane, where ``overall_passed`` is a
       REAL ``False`` rather than the ``None`` that means unknown; what is
       missing is the drawing and the candidate, so ``has_candidate`` decides
       whether a decision is on offer.
    """
    opening = (
        "JTS measured your speaker and worked out a correction for it. "
        "Nothing has been applied yet — this is the proposal."
    )
    if not has_candidate:
        # State 4's own shape, and any state with nothing applyable behind it:
        # there is no decision to present, so do not stage one.
        detail = ""
        if prediction is not None and prediction.get("overall_passed") is False:
            failing = _worst_failing_band(prediction.get("spec_bands"))
            detail = (
                " The correction it worked out would still have missed the "
                f"target by {failing[2]:.1f} dB between {failing[0]:.0f} and "
                f"{failing[1]:.0f} Hz."
                if failing
                else " The correction it worked out would still have missed "
                "the target."
            )
        return (
            "JTS measured your speaker but has no correction to propose from "
            f"this measurement.{detail} Measure again to try afresh."
        )
    if prediction is None:
        return (
            f"{opening} JTS could not work out what the result would be, so "
            "there is nothing to judge this proposal by. Measure again, or "
            "leave things as they are."
        )
    passed = prediction.get("overall_passed")
    if passed is None:
        return (
            f"{opening} JTS could not check the result it expects against the "
            "target, so there is nothing to judge this proposal by. Measure "
            "again, or leave things as they are."
        )
    if passed is False:
        failing = _worst_failing_band(prediction.get("spec_bands"))
        miss = (
            f"misses the target by {failing[2]:.1f} dB between "
            f"{failing[0]:.0f} and {failing[1]:.0f} Hz"
            if failing
            else "still misses the target"
        )
        return (
            f"{opening} Even so, the result JTS expects {miss}. That is worked "
            "out from the measurement, not measured — applying it is your "
            "call, and JTS will measure the speaker again afterwards to find "
            "out what really happened."
        )
    return (
        f"{opening} The result JTS expects meets the target in every band it "
        "checks. That is worked out from the measurement, not measured — "
        "apply it and JTS will measure the speaker again to confirm."
    )


def _closing_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    """The measuring session's TAIL — measured, not yet proposed (D1, B2).

    Every stage-1 phase is accepted and no candidate exists yet, which is true
    at two very different moments:

    * ``awaiting_confirm`` — the pre-apply cloud is walked and the group-close
      confirm is open. The household has something to do; WHERE depends on the
      transport.
    * ``running`` — they confirmed; the combine and the fit are in flight.
      Nobody has anything to do, and this is the one screen that sets ``busy``.

    **Not the review screen**: accepting the final cloud position resolves every
    stage-1 phase, so ``review`` used to render mid-hold, telling a household
    that nothing had been produced over a measurement that was still running.

    **No SCREEN-LEVEL actions**: every action this screen could offer of its own
    is destructive of work in progress. Stop rides the capture block.

    **The confirm itself belongs to the household, here** (#2881): the screen
    mints Save / Record-again against the endpoints the runner already listens
    on (``/v2/complete``, ``/v2/retake``). Both carry ``show_during_capture``
    because the session they belong to is by definition still in flight.

    **…but NOT while a capture is held.** A screen-level primary suppresses the
    walkthrough that renders the hold, so the retake's own prompt would render
    nowhere and the hold would run out its 600 s budget. The pair comes back on
    its own the moment the gate releases.
    """
    from .arm_walk import SESSION_ENDED_STATUSES

    v2 = _v2(status)
    running = str(v2.get("cloud_close") or "") == CLOUD_CLOSE_RUNNING
    capture = _mapping(status.get("capture"))
    # The screen is derived from durable ``cloud_close``, not from the slot, so
    # it also renders after the walk ended with its group un-confirmed (Stop, a
    # failure, a reload past a restart). The two moves below POST into signals
    # the slot drops the moment it leaves an in-flight status, so minting them
    # for a dead session would be minting a 409.
    live = bool(
        str(capture.get("status") or "")
        and str(capture.get("status")) not in SESSION_ENDED_STATUSES
    )
    held = bool(capture.get("position_pending"))
    ready = live and not running and not held
    if running:
        verdict = (
            "JTS is working out your correction from the measurements — this "
            "takes a few seconds."
        )
    elif ready:
        verdict = (
            "All spots measured. Save this measurement, or record the last "
            "spot again."
        )
    elif live:
        # Held. The only way to reach this screen with a hold open is a retake
        # the household just asked for, so point at the step that is waiting.
        verdict = "Re-recording one spot — follow the step below."
    else:
        verdict = (
            "All spots measured, but this measurement session has ended "
            "before it was saved. Measure again to keep a round."
        )
    return _envelope(
        screen="closing",
        active_step="measure",
        verdict=verdict,
        next_action={
            "id": "crossover_v2_complete",
            "label": "Save this measurement",
            "endpoint": "/correction/crossover/v2/complete",
            "body": {},
            "show_during_capture": True,
        } if ready else None,
        alternate_actions=[{
            "id": "crossover_v2_retake",
            "label": "Record the last spot again",
            "endpoint": "/correction/crossover/v2/retake",
            "body": {},
            "show_during_capture": True,
        }] if ready else [],
        busy=running,
        status=status,
        # The pre-apply cloud's own flatness/carve-out disclosure is on record by
        # now, and it is the same measured evidence the review screen leads with:
        # readable while the fit runs, with nothing to decide about it.
        expert_details=_flatness_details_lines(status),
    )


def _review_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    """The REVIEW screen: the household's apply decision (work order D3).

    **The review interlude IS the apply decision point.** Auto-applying a fit
    that failed its own spec by +6.04 dB, three seconds before VERIFY, left a
    box applied-and-ungraded with no decision point anywhere in the sequence.

    Renders D3's five things in order: what we measured (the pre-apply cloud
    curve and its per-band verdict), what we propose (``candidate_review``),
    what we predict (``prediction``, drawn in the same deviation frame so the
    two are comparable by eye), the honest verdict, and the decision.

    **No default, no timer, no auto-advance** (D3.5): the interlude is untimed
    by construction and the candidate lives in durable state with no TTL.

    **"Keep current sound" does not delete the candidate**: it ends the journey
    and the proposal stays reviewable until a newer measurement replaces it.
    Deleting on decline would make an accidental tap cost ten captures to undo.

    **No way back anywhere on this screen** (D6): the way back restores what an
    apply replaced, and stage 1 replaced nothing.
    """
    v2 = _v2(status)
    candidate = _mapping(v2.get("candidate"))
    review = _candidate_review_payload(candidate or None)
    prediction = v2.get("prediction")
    prediction = prediction if isinstance(prediction, Mapping) else None
    fingerprint = str(candidate.get("fingerprint") or "")
    # **One label, because nothing here re-answers "will Apply move the declared
    # crossover?".** Naming a corner in the button was a SECOND reading of a
    # question ``handle_v2_apply`` already answers from the candidate's own
    # preset, and the selector that fed it is retired
    # (``docs/tuning-master-plan.md`` ticket 2.4). Giving the button a corner
    # again needs the review-time producer PR-4 owns; do not restore it here.
    apply_label = "Apply and verify"
    # A proposal with no fingerprint cannot be applied even in principle: the
    # apply endpoint's first gate is ``expected_candidate_fingerprint``.
    has_candidate = bool(review and fingerprint)

    can_open_stage_2, preflight_message, preflight_action = _stage2_preflight(status)
    # D4: an ungradeable prediction renders as "we could not predict this" and
    # DISABLES Apply. A GRADED MISS is the opposite case and stays enabled —
    # presenting improved-but-failing is the point of this screen (D3.4).
    # ``None`` is not ``False`` here.
    gradeable = bool(prediction and prediction.get("overall_passed") is not None)
    # Stage-2 openability does NOT disable Apply: the apply transaction re-runs
    # the same predicate and refuses a truly un-openable stage 2. The preflight
    # stays a render-time DISCLOSURE, early and loud, because suppressing a
    # refusal knowable now is the cost #1828 moved this predicate earlier to
    # avoid.
    apply_enabled = has_candidate and gradeable

    # ONE condition owns both the refusal sentence and its resolution button
    # below, so the button never renders unexplained. With no candidate there is
    # no Apply control at all, so a note about applying is noise.
    show_preflight_refusal = has_candidate and not can_open_stage_2
    nudges: list[dict[str, str]] = []
    if show_preflight_refusal:
        # The refusal renders AS ITSELF, in the words the predicate used — never
        # as a generic "cannot apply".
        nudges.append({
            "code": "crossover_v2_stage2_preflight_refused",
            "severity": "warn",
            "text": (
                f"{preflight_message} Applying now will be refused until "
                "that is sorted."
            ),
        })
    if prediction is not None and prediction.get("overall_passed") is False:
        nudges.append({
            "code": "crossover_v2_prediction_out_of_spec",
            "severity": "warn",
            "text": "The predicted result does not meet the target.",
        })
    # G1's reservation (#2087) belongs on THIS screen: this is where the
    # household is asked to decide, and the #1949 defect was disclosing nothing
    # at exactly that moment. Last and quietest of the three — it qualifies the
    # evidence behind a proposal that is otherwise fine.
    nudges.extend(_ripple_reservation_nudges(status))
    nudges.extend(_calibration_reservation_nudges(status))
    apply_issue = _mapping(v2.get("apply_blocked"))
    # Gated on the fact the sentence is about: Sound holds a saved revision and
    # the DSP apply behind it is not confirmed.
    sound_saved = type(v2.get("accepted_sound_revision")) is int
    if sound_saved and apply_issue:
        nudges.append({"code": str(apply_issue.get("id") or "apply_blocked"),
                       "severity": "warn", "text": str(apply_issue.get(
                           "message") or "DSP apply is not confirmed.")})

    alternate_actions: list[dict[str, Any]] = [
        {
            "id": "review_remeasure",
            "label": "Measure again",
            "endpoint": "/correction/crossover/v2/session",
            "body": {},
            # W6.12's escape hatch, for the same reason it exists on
            # verify_fail: a session still winding down from stage 1
            # (`stopping`) would otherwise blanket-hide every alternate, and a
            # household landing on the decision screen with no way out is
            # precisely the dead end this flow is meant to remove.
            "show_during_capture": True,
        },
        {
            "id": "review_decline",
            "label": "Keep current sound",
            # #2641. Href-only looked like the honest shape — declining changes
            # nothing on the speaker — but the click reloads back onto the SAME
            # decision screen, so a household that has decided is asked again
            # indefinitely, and the round record cannot tell "declined" from
            # "never looked". So the decline is a real action: it still changes
            # nothing and still does not delete the candidate, and what it
            # records is the DECISION.
            "endpoint": "/correction/crossover/v2/decline",
            # The same guard ``review_apply`` carries: a decline recorded against
            # a candidate that has since been replaced would close a review the
            # household never saw.
            "body": {"expected_candidate_fingerprint": fingerprint},
            # A PRESENTATION HINT rather than the action itself: where a
            # household ends up after declining, for a client that cannot POST.
            # The client prefers ``endpoint`` when both are present.
            #
            # The Active speaker ENTRY screen, not the generic /correction/ hub:
            # the hub is the Room-correction wizard, whose first act is a
            # browser-mic HTTPS interstitial — a different subsystem's permission
            # flow (#1985). Declining changes nothing, so the honest destination
            # is where the journey started.
            "href": "/correction/crossover/",
            "show_during_capture": True,
        },
    ]
    if preflight_action and show_preflight_refusal:
        # The refusal's own resolution control, from the SAME registry entry the
        # hard-stop screen reads — one click to the fix rather than prose. Gated
        # identically to the sentence above.
        alternate_actions.insert(0, {**preflight_action, "show_during_capture": True})

    return _envelope(
        screen="review",
        active_step="apply",
        verdict=_review_verdict(prediction, has_candidate),
        nudges=nudges,
        next_action={
            "id": "review_apply",
            "label": apply_label,
            "endpoint": "/correction/crossover/v2/apply",
            "body": {"expected_candidate_fingerprint": fingerprint},
            "enabled": apply_enabled,
        # The ``show_during_capture`` primary is what keeps Apply visible
        # while the just-closed capture winds down.
            "show_during_capture": True,
        } if has_candidate else None,
        alternate_actions=alternate_actions,
        status=status,
        candidate_review=review,
        # D3.1: the pre-apply cloud IS the measured evidence on this screen, so
        # its disclosure belongs here — the same lines the done screen folds
        # away, on the screen where they inform a decision.
        expert_details=(
            _flatness_details_lines(status)
            + _ripple_reservation_lines(status)
        ),
        prediction=prediction,
        # CC1: the frame gate banks its disagreement and PROCEEDS, so the
        # proposal below it was reached over evidence two instruments read
        # differently — and the household was told nothing about that at the
        # exact moment they were asked to decide (#1949).
        findings=_finding_notes(status),
    )


def _cloud_verify_block(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """The compact CLOUD-VERIFY entry of the ``cloud`` block, or empty.

    ``PHASE_CLOUD_VERIFY`` is spelled through the shared phase constant, not
    a literal, so this and the session cannot drift apart on the key name.
    """
    return _mapping(_mapping(_v2(status).get("cloud")).get(PHASE_CLOUD_VERIFY))


def _cloud_measure_block(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """The compact CLOUD-MEASURE entry of the ``cloud`` block, or empty.

    Express's only cloud group, and every tier's only cloud group until the
    post-apply walk closes (#1965) — see :func:`_pre_apply_flatness_lines`.
    """
    return _mapping(_mapping(_v2(status).get("cloud")).get(PHASE_CLOUD_MEASURE))


#: The remote tier's ONE disclosure, as a done-screen badge. Info, never warn:
#: nothing went wrong and there is nothing to fix — the walk sampled one axis,
#: and a household reading "Your speaker is tuned" is owed the shape of the
#: evidence behind it.
_REMOTE_VERTICAL_NUDGE = {
    "code": "crossover_v2_remote_horizontal_only",
    "severity": "info",
    "text": (
        "Checked across the speaker's horizontal axis only. Run a Full "
        "measurement to include the up-and-down spot as well."
    ),
}


def _with_remote_disclosure(
    nudges: list[dict[str, str]], tier: str,
) -> list[dict[str, str]]:
    """Append the remote tier's vertical-coverage disclosure, once."""
    if tier != TIER_REMOTE:
        return nudges
    return [*nudges, dict(_REMOTE_VERTICAL_NUDGE)]


def _done_nudges(
    verify: Mapping[str, Any], *, spec_passed: bool | None,
    result_outcome: str = "", tier: str = "",
) -> list[dict[str, str]]:
    """The done screen's badges — one claim per instrument, none overclaiming.

    Three instruments vote: the TRACKING comparator says whether the speaker
    matched its own prediction, the post-apply cloud's spec verdict says whether
    it is flat, and the delta probe can return ``level_mismatch`` — NOT a
    rollback, but it means the probe never got to answer the shape question,
    which has to reach a household when everything else says "Verified."

    Ordering is deliberate: the strongest claim about the SPEAKER wins the badge
    slot, and the probe caveats are appended beside whichever badge won rather
    than replacing it. The terminal result code takes only the SLOT — returning
    early for it silenced the caveats on every graded session (#2738).
    ``result_outcome`` arrives already CAPPED, as ``spec_passed`` arrives
    already reduced.

    A non-pass outcome gets no badge at all — that screen is the verify_fail one
    — unless a result code qualified it (#2605). The caveats stay behind the
    pass either way: each is about a mark-VERIFY comparison a non-pass screen
    never completed.
    """
    result_badges = {
        RESULT_VERIFIED_TARGET: ("ok", "Target verified."),
        RESULT_VERIFIED_BEST_EVALUATED: (
            "warn", "Best evaluated; target still missed.",
        ),
        RESULT_KEEP_PREVIOUS: ("warn", "Keep the previous sound."),
        RESULT_INCONCLUSIVE: ("warn", "Result inconclusive."),
    }
    result_badge = result_badges.get(result_outcome)
    verified = verify.get("outcome") == "pass"
    if result_badge is None and not verified:
        # The verify_fail screen, which carries its own copy and delivered no
        # result to qualify.
        return []
    badge: dict[str, str]
    if result_badge is not None:
        badge = {"code": f"crossover_v2_{result_outcome}",
                 "severity": result_badge[0], "text": result_badge[1]}
    elif spec_passed is False:
        badge = {
            "code": "crossover_v2_out_of_spec",
            "severity": "warn",
            "text": "Verified against the prediction, but not flat to target.",
        }
    else:
        badge = {
            "code": "crossover_v2_verified",
            "severity": "ok",
            "text": "Verified.",
        }
    nudges: list[dict[str, str]] = _with_remote_disclosure([badge], tier)
    if not verified:
        return nudges
    probe = _mapping(verify.get("delta_probe"))
    if probe.get("verdict") == VERDICT_LEVEL_MISMATCH:
        nudges.append({
            "code": "crossover_v2_level_mismatch",
            "severity": "warn",
            # No hardware noun and no instruction to act: a statement about what
            # the check could and could not confirm.
            #
            # REASON-AWARE since #2537, because #2533 split the verdict in two:
            # a level the quiet bins measured across the whole graded band, and
            # one they measured in a sliver of it. The whole-band sentence is
            # false for the second.
            "text": _level_mismatch_text(probe),
        })
    if probe.get("verdict") == VERDICT_FRAME_MISMATCH:
        nudges.append({
            "code": "crossover_v2_frame_mismatch",
            "severity": "warn",
            # The tilt-carrying sibling of the caveat above, in the same register
            # and under the same rules (#2521).
            "text": (
                "The overall loudness and balance differed from what this check "
                "expected, so it could not confirm the correction's shape."
            ),
        })
    if probe.get("verdict") == VERDICT_SAFETY_ONLY:
        nudges.append({
            "code": "crossover_v2_safety_only",
            "severity": "warn",
            # The third caveat, in the two above's register (#2614). Not a
            # finding about the speaker at all — the checks did not run, which is
            # why the badge beside this still says Verified.
            #
            # It names BOTH halves since series-2 D1: the two directional
            # findings are differenced against the pre-apply capture, and a state
            # axis has no pre-apply reference, so what the pre-D1 copy called a
            # loudness comparison was a comparison against the model.
            #
            # **No cause clause, deliberately.** FOUR paths reach this verdict
            # and a named cause would be true for one and false for three. The
            # journal already names the specific reason.
            "text": (
                "This check could not confirm the correction's shape or its "
                "loudness this round."
            ),
        })
    return nudges


def _level_mismatch_text(probe: Mapping[str, Any]) -> str:
    """The level-mismatch caveat, in the register the finding actually supports.

    Two sentences for one verdict (#2537), because #2533 gave it two reasons:
    the quiet bins measured the level across the whole graded band, or they did
    not and the band they DID cover travels on the map as ``quiet.core_band_hz``.

    Falls back to the whole-band sentence for any other reason string, which is
    the conservative direction: a caveat that says slightly more than the
    evidence supports still tells the household the shape went unconfirmed,
    where inventing a band from an absent field would state a fact.
    """

    whole_band = (
        "The overall loudness changed by more than this check expected, "
        "so it could not confirm the correction's shape."
    )
    if probe.get("reason") != REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND:
        return whole_band
    band = _mapping(probe.get("quiet")).get("core_band_hz")
    # Checked inline so the narrowing is visible to a reader and to a type
    # checker: ``band`` comes off a persisted JSON payload.
    if not (
        isinstance(band, (list, tuple))
        and len(band) == 2
        and all(
            isinstance(edge, (int, float)) and not isinstance(edge, bool)
            for edge in band
        )
    ):
        return whole_band
    lo, hi = (_frequency_label(float(edge)) for edge in band)
    return (
        f"The loudness between {lo} and {hi} changed by more than this check "
        "expected, so it could not confirm the correction's shape."
    )


def _frequency_label(hz: float) -> str:
    """A frequency as a household reads it — ``480 Hz``, ``12.4 kHz``.

    Local to this caveat rather than promoted: the wizards render frequencies in
    several house styles already.
    """

    if hz >= 1000.0:
        return f"{hz / 1000.0:.1f} kHz".replace(".0 kHz", " kHz")
    return f"{hz:.0f} Hz"


#: The household copy for a round that KEPT an imperfect result (#2537), as one
#: nudge rather than a screen: ``keep_for_iteration`` leaves the speaker in the
#: same state ``keep`` does, so it must not look like a failure — but the round
#: measured something it did not fix, and saying nothing is how "we could not
#: tell" becomes "verified" by silence. Register-matched to the caveats above:
#: no hardware noun, no instruction to act.
KEEP_FOR_ITERATION_TEXT = (
    "This is the best sound measured so far, and it is what the speaker is "
    "playing. Some of what was measured is still off target — measuring again "
    "is how that gets closer."
)

#: Row 7's sentence: the same news, with no round left to spend (#2656). Named
#: for its ROW rather than the sentence above it, because the outcome differs.
#: Recomposed from sentences this screen already owns; what it cannot borrow is
#: "measuring again is how that gets closer" — a remedy this screen no longer
#: offers, and a promise with no button is what #2655 minted the button to stop.
KEEP_MISSED_EXHAUSTED_TEXT = (
    "This is the best sound measured so far, and it is what the speaker is "
    "playing. Some of what was measured is still off target, and that was the "
    "last round of this tuning."
)

#: The household copy for a round that PASSED and is iterating anyway (#2602) —
#: the ruling's *in-tolerance is not done*. The copy reports the good news and
#: says plainly that another round is coming, without turning either into the
#: other: "inside the target" is the pass, "can still get flatter" is the reason
#: to keep going, and neither is dressed as a fault.
KEEP_ITERATING_TEXT = (
    "Everything measured is inside the target, and it can still get flatter. "
    "This is the best sound measured so far and it is what the speaker is "
    "playing — measuring again is how the rest of the way gets found."
)

#: The same row, when the round could not grade how flat the result is. An
#: ungradable objective keeps the series open, so this row is reachable with NO
#: measured flatness behind it, where :data:`KEEP_ITERATING_TEXT`'s "everything
#: measured is inside the target" would be a claim nothing supports.
KEEP_ITERATING_UNGRADED_TEXT = (
    "This is the best sound measured so far, and it is what the speaker is "
    "playing. There was not enough of a full result to tell how much flatter "
    "it could get — measuring again is how that gets answered."
)

#: The household copy for a round that PASSED and ENDED the series (#2602),
#: keyed by the headroom axis's own reason: "there is nothing left to chase",
#: "it stopped moving" and "that was the third round" are three genuinely
#: different endings and a household told last round to measure again is owed
#: the specific one.
#:
#: The reasons are :mod:`~.crossover_v2.verification`'s, resolved lazily by
#: :func:`_series_complete_text` rather than imported at module scope: that
#: module reaches numpy through ``flat_spec`` and this one renders a household
#: polling surface.
SERIES_COMPLETE_DEFAULT_TEXT = (
    "Everything measured is inside the target, and the tuning is finished."
)


def _series_complete_text(reason: str) -> str:
    """The ending sentence for a passing round that closed the series (#2602).

    An unrecognised reason falls back to :data:`SERIES_COMPLETE_DEFAULT_TEXT`
    rather than to silence: a household is owed "the tuning is finished" even
    when this build does not know which ending it was. The fallback states only
    what the ROW already proves and never guesses at a cause.
    """

    from .crossover_v2.verification import (
        HEADROOM_CAP_REACHED,
        HEADROOM_NO_OBJECTIVES,
        HEADROOM_PLATEAUED,
        HEADROOM_WITHIN_PLATEAU,
    )

    return {
        HEADROOM_WITHIN_PLATEAU: (
            "Everything measured is inside the target, and it is as flat and "
            "as level as measuring can show. The tuning is finished."
        ),
        HEADROOM_PLATEAUED: (
            "Everything measured is inside the target. The last round barely "
            "moved it, so more rounds are unlikely to help — the tuning is "
            "finished."
        ),
        # Deliberately does NOT say "the third round": the cap is
        # ``ROUND_SERIES_CAP``'s to state, and spelling the number into copy
        # would be a second source of truth a change would turn into a lie.
        HEADROOM_CAP_REACHED: (
            "Everything measured is inside the target. That was the last "
            "round of this tuning, so it is finished here."
        ),
        # Reachable only from a receipt banked BEFORE the bites ruling. Kept
        # because the done screen reads persisted receipts, and a household
        # re-opening an older one is owed the sentence that round decided.
        HEADROOM_NO_OBJECTIVES: (
            "Everything measured is inside the target. There was not enough of "
            "a full result to tell whether more rounds would help, so the "
            "tuning stops here."
        ),
    }.get(reason, SERIES_COMPLETE_DEFAULT_TEXT)


def _round_summary(status: Mapping[str, Any]) -> dict[str, str] | None:
    """The last graded round's adoption row, outcome, and reason — or ``None``.

    Read straight off the durable ``round_receipt``; this computes and
    re-derives nothing, which is what keeps the screen and the receipt from
    disagreeing. ``None`` for a session that has graded no round — an absence,
    not a row named ``""``.
    """

    receipt = _mapping(_mapping(status.get("crossover_v2")).get("round_receipt"))
    row = str(receipt.get("row") or "")
    adoption = str(receipt.get("adoption") or "")
    if not (row or adoption):
        return None
    return {
        "row": row,
        "adoption": adoption,
        "reason": str(receipt.get("reason") or ""),
    }


def _round_is_iterating(v2: Mapping[str, Any]) -> bool:
    """Did the last graded round say another bite is coming, and is one left?

    Keyed on the ROW, like :func:`_round_adoption_nudges`: two rows share the
    ``keep_for_iteration`` outcome, so reading the outcome answers correctly by
    accident and stops the moment a third row joins them. Reads the same
    ``round_receipt`` the copy does, so the screen can never promise another
    round in prose and withhold the button.

    The budget check is defense in depth since #2656 — a capped MISSED series
    now ends on its own row. What the ordinal check still covers is a receipt
    banked BEFORE that change, which carries row 2 at the cap.

    Read as ``banked_ordinal >= ROUND_SERIES_CAP`` — the SAME comparison
    ``evaluate_iteration_headroom`` makes, against the same constant. An
    unreadable or absent ordinal offers the bite, which is the direction the
    series' own reader already fails in.
    """

    from .crossover_v2.round_evidence import ROUND_SERIES_CAP

    receipt = _mapping(v2.get("round_receipt"))
    row = str(receipt.get("row") or "")
    if row not in {ADOPTION_ROW_KEEP_FOR_ITERATION, ADOPTION_ROW_KEEP_ITERATING}:
        return False
    ordinal = receipt.get("round_ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        return True
    return ordinal < ROUND_SERIES_CAP


def _round_adoption_nudges(v2: Mapping[str, Any]) -> list[dict[str, str]]:
    """What the last graded round decided, and whether another one is coming.

    Appended at the CALL SITE rather than inside :func:`_done_nudges`, which
    answers "which badge did the VERIFY OUTCOME earn" — a different instrument,
    owed on this screen whichever badge won.

    **Keyed on the ROW, not the outcome** (#2602). Four rows reach this screen
    sharing two outcomes: row 2 kept a result that MISSED, row 6 kept one that
    PASSED and is chasing flatter, and rows 1 and 7 both end the series. Reading
    the outcome alone would tell a household with an in-tolerance speaker that
    some of what was measured is off target, and a household whose targets are
    still open that the tuning is finished.

    Reads off ``round_receipt`` and computes nothing. Silent for a round that
    restored or escalated: those never reach the done screen.
    """

    receipt = _mapping(v2.get("round_receipt"))
    row = str(receipt.get("row") or "")
    reason = str(receipt.get("reason") or "")
    if row == ADOPTION_ROW_KEEP:
        return [{
            "code": "crossover_v2_series_complete",
            "severity": "ok",
            "text": _series_complete_text(reason),
        }]
    if row == ADOPTION_ROW_KEEP_ITERATING:
        from .crossover_v2.verification import HEADROOM_NO_OBJECTIVES

        return [{
            "code": "crossover_v2_keep_iterating",
            "severity": "info",
            # One row, two pieces of news, told apart by the deciding axis's
            # own reason — the same rule the row/outcome split above follows.
            "text": (
                KEEP_ITERATING_UNGRADED_TEXT
                if reason == HEADROOM_NO_OBJECTIVES
                else KEEP_ITERATING_TEXT
            ),
        }]
    if row in {ADOPTION_ROW_KEEP_FOR_ITERATION, ADOPTION_ROW_KEEP_MISSED_EXHAUSTED}:
        return [{
            "code": "crossover_v2_keep_for_iteration",
            "severity": "warn",
            # One code, two endings, told apart by the ROW. One code because the
            # news a household acts on is identical: the speaker is playing the
            # best measured sound and some of it is still off target.
            "text": (
                KEEP_MISSED_EXHAUSTED_TEXT
                if row == ADOPTION_ROW_KEEP_MISSED_EXHAUSTED
                else KEEP_FOR_ITERATION_TEXT
            ),
        }]
    # An unrecognised or absent row, INCLUDING a pre-#2602 receipt that carries
    # an outcome but no row. Falling back to the outcome would resurrect the
    # ambiguity the row exists to remove.
    return []


# --- G1's ripple reservation (#2087) ----------------------------------------
#
# Three functions, one owner. The flow decides, the host persists, and
# everything a household or an operator READS about the reservation is composed
# here, so the sentence and the numbers cannot drift apart.


#: The one sentence, in the owner's register for the #2087 ruling.
#:
#: **It names no cause, and that is the load-bearing part.** A high predicted
#: ripple is consistent with the room, the microphone, the recording chain and
#: the speaker itself, and this session separated none of them. The refusal it
#: replaces did the opposite and told a household with a correctly placed mic to
#: move it (#2085).
#:
#: "blended less evenly" is the gloss: the instrument's word is *ripple*, and a
#: household sentence that needs a glossary is not a disclosure. It names no
#: part of the speaker either — the honest word would be a hardware noun the
#: register forbids.
#:
#: It claims the tuning is rougher EVIDENCE, never a worse RESULT.
RIPPLE_RESERVATION_COPY = (
    "We helped as much as this measurement allows: where this speaker's high "
    "and low ranges overlap, they blended less evenly than a clean measurement "
    "shows, so this tuning rests on rougher evidence than usual."
)


def _ripple_reservation(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """G1's banked reservation, or an empty mapping (#2087).

    The validating reader for a key ``crossover_v2_status_block`` copies through
    unchecked. Empty is the honest answer for every unusable shape, because both
    surfaces below need the pair and half a reservation is not one.
    """
    return _mapping(_mapping(_v2(status).get("measure")).get("ripple_reservation"))


def _ripple_reservation_nudges(status: Mapping[str, Any]) -> list[dict[str, str]]:
    """The household's one sentence, or nothing at all (#2087).

    ``info``, not ``warn``: the session succeeded and this is something to know
    rather than a problem to solve. Gated on the VALUE parsing, not merely on
    the record existing, so a malformed reservation renders silence rather than
    a sentence whose expert line beneath it would be blank.
    """
    if _finite(_ripple_reservation(status).get("predicted_ripple_db")) is None:
        return []
    return [{
        "code": "crossover_v2_ripple_reservation",
        "severity": "info",
        "text": RIPPLE_RESERVATION_COPY,
    }]


def _ripple_reservation_lines(status: Mapping[str, Any]) -> list[str]:
    """The reservation's numbers, for the collapsed expert disclosure (#2087).

    The measured value and the threshold it crossed, both from the ONE persisted
    record, so the line cannot claim a threshold the capture was not judged
    against — the reason the flow banks the threshold with the value.
    """
    reservation = _ripple_reservation(status)
    ripple = _finite(reservation.get("predicted_ripple_db"))
    threshold = _finite(reservation.get("threshold_db"))
    if ripple is None or threshold is None:
        return []
    return [
        f"predicted ripple {ripple:.2f} dB, above the {threshold:.1f} dB "
        "disclosure threshold"
    ]


#: Nothing on the tuning path named the mic-registration step, so a household
#: never learned an uncalibrated MEASURE was accepted. One plain sentence in
#: :data:`RIPPLE_RESERVATION_COPY`'s "not a lecture" register, pointing at the
#: concrete surface.
MIC_CALIBRATION_RESERVATION_COPY = (
    "This measurement used no calibrated microphone, so the result may be "
    "less accurate than usual. Register one under Microphone on /correction/."
)


def _calibration_reservation(status: Mapping[str, Any]) -> bool:
    """Whether the banked MEASURE ran with no resolved mic calibration. The
    validating reader for a key ``crossover_v2_status_block`` copies through
    unchecked, mirroring :func:`_ripple_reservation`.
    """
    return bool(_mapping(_v2(status).get("measure")).get("calibration_reservation"))


def _calibration_reservation_nudges(status: Mapping[str, Any]) -> list[dict[str, str]]:
    """The household's one sentence naming the mic-registration step, or nothing.

    ``warn``, unlike the ripple reservation's ``info``: this one is directly
    actionable before the NEXT measurement, so it gets the severity the doctor's
    sibling check for the identical fact uses.
    """
    if not _calibration_reservation(status):
        return []
    return [{
        "code": "crossover_v2_mic_calibration_reservation",
        "severity": "warn",
        "text": MIC_CALIBRATION_RESERVATION_COPY,
    }]


def _attempt_db(value: Any) -> str | None:
    number = _finite(value)
    if number is None:
        return None
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _attempt_provenance(decision: Mapping[str, Any]) -> str | None:
    provenance = decision.get("provenance")
    if provenance == PROVENANCE_REALIZED:
        return f"{PROVENANCE_REALIZED} vs {PROVENANCE_REALIZED}"
    if provenance == PROVENANCE_MODEL_GRADED:
        return f"{PROVENANCE_MODEL_GRADED} vs {PROVENANCE_MODEL_GRADED}"
    return None


def _attempt_first_sentence(decision: Mapping[str, Any]) -> str:
    provenance = decision.get("provenance")
    if provenance not in {PROVENANCE_REALIZED, PROVENANCE_MODEL_GRADED}:
        return "Recorded the first tracking result without an improvement claim."
    return (
        f"Recorded the first {provenance} tracking result; another attempt is "
        "needed before improvement can be judged."
    )


def _attempt_improved_sentence(decision: Mapping[str, Any]) -> str:
    amount = _attempt_db(decision.get("improvement_db"))
    provenance = _attempt_provenance(decision)
    if amount is None or provenance is None:
        return "The latest attempt was recorded without an improvement claim."
    return (
        "The latest applied result tracked its prediction "
        f"{amount} dB more closely ({provenance})."
    )


def _attempt_floor_sentence(decision: Mapping[str, Any]) -> str:
    magnitude = _attempt_db(decision.get("magnitude_db"))
    floor = _mapping(decision.get("floor"))
    floor_db = _attempt_db(floor.get("claim_floor_db"))
    if magnitude is None or floor_db is None:
        return "Stopped because the instrument could not support another claim."
    return (
        "Stopped: the change in prediction tracking from the previous attempt "
        f"({magnitude} dB) is below what this instrument can distinguish "
        f"(floor {floor_db} dB)."
    )


def _attempt_evidence_sentence(decision: Mapping[str, Any]) -> str:
    return "Stopped because the latest attempt could not be compared reliably."


def _attempt_sitting_sentence(decision: Mapping[str, Any]) -> str:
    """The #2081 refusal, in household terms: the microphone moved.

    Says what happened and that nothing is wrong, because it is the ordinary
    outcome of tuning twice. Two vocabulary rules bind the sentence: it is free
    of ENGINE words ("floor", "scope", "sitting"), which ride in the decision's
    notes instead; and the actor is **the microphone, never "the phone"**
    (#1941 R4, guarded by ``tests/test_measurement_vocabulary.py``) — the
    instrument a household holds is whatever browser reaches the wizard.
    """
    return (
        "The previous result was measured with the microphone in a different "
        "position, so this attempt is recorded without comparing the two."
    )


def _attempt_regression_sentence(decision: Mapping[str, Any]) -> str:
    improvement = _finite(decision.get("improvement_db"))
    provenance = _attempt_provenance(decision)
    if improvement is None or provenance is None:
        return "The latest attempt did not support an improvement claim."
    amount = _attempt_db(abs(improvement))
    return (
        "The latest applied result tracked its prediction "
        f"{amount} dB less closely ({provenance})."
    )


def _attempt_budget_sentence(decision: Mapping[str, Any]) -> str:
    attempts = decision.get("attempts_used")
    count = (
        int(attempts)
        if isinstance(attempts, int) and not isinstance(attempts, bool) else None
    )
    return (
        f"Stopped after {count} attempts because the attempt budget was reached."
        if count is not None
        else "Stopped because the attempt budget was reached."
    )


def _attempt_converged_sentence(decision: Mapping[str, Any]) -> str:
    return "Stopped because no material improvement remains."


def _attempt_in_spec_sentence(decision: Mapping[str, Any]) -> str:
    return "Stopped because the latest result is already within the target."


# The household sentence has one writer. It dispatches on the kernel's reason
# vocabulary and formats the kernel/store numbers; it never recomputes a
# decision or substitutes a literal floor.
_ATTEMPT_SENTENCE_BY_REASON = {
    REASON_AWAITING_FIRST_ATTEMPT: _attempt_first_sentence,
    REASON_BASELINE_ESTABLISHED: _attempt_first_sentence,
    REASON_IMPROVEMENT_ABOVE_FLOOR: _attempt_improved_sentence,
    REASON_BELOW_CLAIM_FLOOR: _attempt_floor_sentence,
    REASON_ATTEMPT_NOT_COMPARABLE: _attempt_evidence_sentence,
    REASON_PREDECESSOR_NOT_COMPARABLE: _attempt_evidence_sentence,
    REASON_FLOOR_METRIC_MISMATCH: _attempt_evidence_sentence,
    REASON_PROVENANCE_MISMATCH: _attempt_evidence_sentence,
    # #2081's two refusals answer differently on purpose: a MISMATCH is a fact
    # about the household's own two measurements, while an UNRECORDED sitting
    # cannot say where its older attempt was measured and must not be dressed up
    # as knowledge about a phone that may never have moved.
    REASON_SITTING_MISMATCH: _attempt_sitting_sentence,
    REASON_SITTING_UNRECORDED: _attempt_evidence_sentence,
    REASON_NO_DEVIATION_AVAILABLE: _attempt_evidence_sentence,
    REASON_DIRECTION_UNKNOWN_ABOVE_FLOOR: _attempt_evidence_sentence,
    REASON_GRADED_BINS_SHRANK: _attempt_evidence_sentence,
    REASON_REGRESSION_FROM_PREDECESSOR: _attempt_regression_sentence,
    REASON_BUDGET_EXHAUSTED: _attempt_budget_sentence,
    REASON_NO_MATERIAL_IMPROVEMENT_PREDICTED: _attempt_converged_sentence,
    REASON_IN_SPEC: _attempt_in_spec_sentence,
}


def attempt_loop_verdict_sentence(status: Mapping[str, Any]) -> str:
    """One household sentence from the session/kernel's last S3 output."""
    attempts = _mapping(_v2(status).get("attempts_loop"))
    decision = _mapping(attempts.get("last_decision"))
    reason = decision.get("reason")
    if not isinstance(reason, str):
        return ""
    if reason == ATTEMPT_REASON_NO_FLOOR:
        return (
            "No improvement claim was made because this speaker has no "
            "adopted measurement floor."
        )
    renderer = _ATTEMPT_SENTENCE_BY_REASON.get(reason)
    return renderer(decision) if renderer is not None else ""


def _flatness_unavailable_line(entry: Mapping[str, Any]) -> list[str]:
    """The honest gauge-absent rendering for a CLOUD-VERIFY block that CLOSED
    but carries no usable flatness — two distinguishable states, told apart.

    * **The pipeline DID run and carries no gauge** — a durable state written by
      an older build. ``overall_passed`` is the tell: it is ``None`` for an
      unavailable pipeline and the spec verdict otherwise. "Could not be
      analysed" would be false about a session that analysed fine.
    * **The pipeline never became available** — a combine or DSP-step failure.

    Neither quotes a number: the construction did not run, so there is no
    spec-frame figure to give. **A MISSING entry never reaches here** (#1965) —
    :func:`_flatness_details_lines` routes that state to
    :func:`_pre_apply_flatness_lines` first.
    """
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

_TIER_LABELS = {
    TIER_FULL: "Full measurement",
    TIER_EXPRESS: "Quick tune",
    # Present so every tier this build knows has a name, NOT so a chooser can
    # render one: ``_tier_choice_actions`` offers exactly Full and Express, since
    # the remote tier means owning a positioner the chooser cannot see.
    TIER_REMOTE: "Remote automated",
}
_TIER_CLAIMS = {
    # "at several spots around the mark" rather than "across the room", which
    # overclaimed past what the post-apply cloud samples. The post-apply claim
    # describes a session the household STARTS SEPARATELY, after deciding to
    # apply, so it reads as the PURPOSE of a second walk.
    TIER_FULL: "re-check the result at several spots around the mark",
    TIER_EXPRESS: "confirm the result at the mark",
    # Full's claim minus the axis a positioner cannot reach — the same
    # honesty rule express's line follows.
    TIER_REMOTE: (
        "re-check the result at several spots across the speaker's "
        "horizontal axis"
    ),
}


def _recommended_tier(status: Mapping[str, Any]) -> str:
    """Full recommended UNTIL a Full-tier commission has completed on this
    topology — the choice is always the household's; history decides only which
    option carries the Recommended badge, never a silent default.

    Keyed on TWO signals, both required: ``_applied_chip``'s ``"automatic"``
    state (topology-scoped by ``crossover_snapshot_state``, so reaching it
    already means this exact topology), AND the durable state's ``tier`` being
    ``TIER_FULL`` — because ``_snapshot_owner`` also reads a LEGACY per-driver
    flow's measured result as ``"automatic"``, and that flow predates tiers.
    Requiring both is what keeps an express-only household seeing Full
    recommended: a Quick-tune-only topology has never had the wider,
    comb-decorrelating walk.
    """
    if _applied_chip(status)["state"] != "automatic":
        return TIER_FULL
    return TIER_EXPRESS if str(_v2(status).get("tier") or "") == TIER_FULL else TIER_FULL


def _staged_walk_request() -> AngleCaptureRequest | None:
    """The staged walk, or ``None`` when none is staged.

    A PEEK: the session open is still the only take, so a household that reads
    this price and never presses Start leaves the walk staged. A slot that
    cannot be read says NOTHING here — the session open refuses that loudly in
    the producing module's own words, and a corrupt document costs this screen
    an offer and nothing else.

    PRICING is the caller's: one document, but its ceiling belongs to the
    session that takes it, so each tier card prices it against its own shape.
    """
    try:
        return peek_staged_angle_request()
    except CrossoverV2FlowError:
        return None


def _tier_action(
    tier: str,
    info: Mapping[str, Mapping[str, int]],
    *,
    recommended: bool,
    staged_walk: AngleCaptureRequest | None = None,
) -> dict[str, Any]:
    detail = info[tier]
    action: dict[str, Any] = {
        "id": f"start_v2_session_{tier}",
        "label": _TIER_LABELS[tier],
        # One-line claims difference (§1.3/§3), derived from the plan shape —
        # never a hand-written prettier figure (§1.1). STAGE-AWARE: a household
        # choosing a tier is choosing both stages with its own decision in
        # between, and a chooser that hid the interlude would sell a 15-capture
        # Full as one uninterrupted sitting.
        "description": (
            f"About {detail['estimated_minutes']} min — "
            f"{detail['stage1_captures']} measurements now. You decide whether "
            f"to apply, then {detail['stage2_captures']} more to "
            f"{_TIER_CLAIMS[tier]}."
        ),
        "recommended": recommended,
        "endpoint": "/correction/crossover/v2/session",
        "body": {"tier": tier},
    }
    if staged_walk is not None:
        # The price is stated BEFORE Start because the walk is taken by the
        # session open whichever tier is pressed. Priced against THIS tier's
        # shape through ``resolve_plan_shape``, the same resolution the capture
        # plans use — a second map here would be the desync ``V2PlanShape``
        # exists to close.
        price = walk_price(staged_walk, plan_shape=resolve_plan_shape(tier))
        action["staged_walk"] = {
            "program": staged_walk.program,
            "mic_moves": price["mic_moves"],
            "captures": price["captures"],
            # The WHOLE session's ceiling, not the walk's own share: the line
            # above quotes stage minutes for a session these stops lengthen.
            "ceiling_min": price["ceiling_min"],
        }
        action["description"] += (
            f" Plus a staged walk ({staged_walk.program or 'free-form'}): "
            f"{price['mic_moves']} more spots, "
            f"{price['captures']} more measurements; up to "
            f"{price['ceiling_min']} min for the whole session."
        )
    return action


def _tier_choice_actions(
    status: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The microphone_check screen's tier chooser: both tiers first-class, the
    recommended one primary — never a silent default (§3).
    """
    info = tier_display_info()
    recommended = _recommended_tier(status)
    other = TIER_FULL if recommended == TIER_EXPRESS else TIER_EXPRESS
    # Read ONCE, for both actions — one document, one peek per poll. Each action
    # prices it against its own tier.
    staged_walk = _staged_walk_request()
    return (
        _tier_action(recommended, info, recommended=True, staged_walk=staged_walk),
        [_tier_action(other, info, recommended=False, staged_walk=staged_walk)],
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
    advertise_capture: bool = True,
    prediction: Mapping[str, Any] | None = None,
    busy: bool = False,
    findings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
        "flow": "v2",
        "screen": screen,
        "active": True,
        "steps": _step_payload(active_step, _done_before(active_step)),
        "verdict_text": verdict,
        "nudges": nudges or [],
        # Optional collapsed expert-disclosure lines (#1605) — the frontend folds
        # them behind a <details>. Empty on every screen that has none.
        "expert_details": list(expert_details or []),
        # A terminal / restart screen must stop advertising the dead phone link
        # and its QR (W6.10 fold-in) — the session it pointed at is gone.
        "capture": (_mapping(status.get("capture")) or None) if advertise_capture else None,
        "next_action": next_action,
        "alternate_actions": alternate_actions or [],
        # Whether this screen is MACHINE-paced — the speaker is working and the
        # household has nothing to do but wait. Declared for the renderer; no
        # renderer reads it yet. ``False`` everywhere except the ``closing``
        # screen's fit-in-flight moment.
        "busy": bool(busy),
        "progress": _progress(active_step),
        "applied": _applied_chip(status),
        # WHICH adoption row the last graded round fired (#2537). Three of the
        # seven rows restore and four keep the graph, sharing two outcomes
        # between them, so the outcome alone cannot say which rule applied — the
        # row is the stable thing to branch on, on this screen as well as for a
        # driver chaining rounds. ``None`` until a round has been graded.
        #
        # Machine data on a household surface, deliberately: the alternative is a
        # series driver fetching an evidence-bundle artifact to learn what the
        # screen it is already polling was built from.
        "round": _round_summary(status),
        "candidate_review": dict(candidate_review) if candidate_review else None,
        # The compact per-group honesty verdict (spec pass/fail per band,
        # excluded-interval count, geometry verdict + guidance copy) — the SAME
        # projection ``crossover_v2_status_block`` serves at ``/state``. ``None``
        # before any cloud group has closed. The plain-language verdict, not the
        # chart feed.
        "cloud": _v2(status).get("cloud"),
        # The before/after chart's own feed — the decimated combined curve per
        # phase, carried through unchanged. Kept off the ``cloud`` key so the
        # doctor (which reads only ``cloud``) never has to parse curve-shaped
        # data mixed into it; it rides the SAME envelope response. ``None``
        # before any cloud group has closed.
        "cloud_chart": _v2(status).get("cloud_chart"),
        # Which commission instrument produced (or is producing) this session —
        # ``None`` when the durable state does not say. The chart module reads it
        # to tell "the post-apply cloud hasn't measured yet" from "there is no
        # post-apply cloud coming", the same unknown-vs-default rule
        # ``crossover_v2_status_block``'s own ``tier`` key uses.
        "tier": (
            str(_v2(status).get("tier"))
            if isinstance(_v2(status).get("tier"), str) and _v2(status).get("tier")
            else None
        ),
        # The PREDICTED response and its stored spec verdict — the chart's third
        # curve, drawn in the same deviation frame as the measured one.
        #
        # **Sent by the REVIEW screen only, and that is what keeps the renderer
        # data-driven**: making the third curve conditional on the SCREEN in the
        # client would be the ``env.screen`` switch PR-T2 forbids. Conditional on
        # the DATA instead leaves the chart module one honest rule — draw the
        # predicted curve when the envelope carries one.
        "prediction": dict(prediction) if prediction else None,
        # The findings this speaker's measurement banked, as household-readable
        # lines. ``[]`` on every screen that is not the apply decision or the
        # result — the same data-driven policy the ``prediction`` key states.
        "findings": list(findings or []),
    }


def _entry_envelope(
    status: Mapping[str, Any],
    *,
    next_action: dict[str, Any],
    alternate_actions: list[dict[str, Any]],
    nudges: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The journey's entry screen — the ONE place its copy lives.

    Two callers reach it: the ordinary ``PHASE_CHECK`` start, and the
    aged-failure resume (#1942), which must land a returning household on
    exactly this screen rather than a dead session's terminal one. Sharing the
    function is what makes that true by construction.
    """
    return _envelope(
        screen="microphone_check", active_step="microphone_check",
        # The journey-opening promise. Before the spatial cloud this said "keep
        # it in that one spot for the whole measurement", which PR-3b made false
        # on the FIRST screen the household reads. The mark is still where the
        # session starts and returns to; the moving is named up front.
        #
        # §3: the tier choice is the household's, explicitly, every session —
        # both actions are first-class and which one is primary is only history's
        # Recommended badge.
        verdict=(
            "Place the microphone about 1 m in front of the speaker, at "
            "tweeter height and pointing at it — about where you'd sit to "
            "listen (see the picture). That spot is your mark. JTS runs a "
            "quick microphone check first, then measures from the mark and "
            "from a few nearby spots it will guide you to — that "
            "is what lets it tell the speaker apart from the room. Choose "
            "how thorough a measurement to run below."
        ),
        next_action=next_action,
        alternate_actions=alternate_actions,
        nudges=nudges,
        status=status,
    )


# The banked-candidate way back: republish the candidate that was live before
# the last apply, so the household can bring the previous tuning back through
# the ORDINARY path (republish -> review -> apply). One tap restores the SLOT,
# not the graph, and every admission gate still runs on that apply.
#
# A list so call sites can splice it: empty when the pre-apply stash names no
# banked candidate, because republish can only reach candidates the bank holds.
# The fingerprint is a pointer, never a promise — the bank verifies on POST. A
# factory rather than a shared constant: the action carries a mutable ``body``,
# so a module-level dict would share one across every envelope this process
# ever serves.
def _way_back_action(status: Mapping[str, Any]) -> list[dict[str, Any]]:
    fingerprint = _v2(status).get("previous_candidate_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return []
    return [{
        "id": "republish_previous",
        "label": "Go back to the previous tuning",
        "endpoint": "/correction/crossover/v2/republish",
        "body": {"fingerprint": fingerprint},
        "show_during_capture": True,
    }]


# --- session recency (issues #1942, #1947) -----------------------------------

#: How long a dated record keeps rendering as the LIVE screen.
#:
#: A clock rather than a structural signal because every structural fact in the
#: durable state is frozen by the last persist and reads identically a second
#: later and a week later, and the one that does change — capture liveness —
#: changes the WRONG WAY: a terminal failure purges its own capture within 3 s, so
#: keying on it would age out a screen the household is actively reading.
#:
#: 30 minutes: these screens are read with a microphone in hand and acted on in
#: seconds to minutes, while the defect they guard against is measured in hours
#: to days. Erring long is the conservative direction — every realistic
#: in-session case keeps the shipped screen byte for byte.
#:
#: Not borrowed from ``DEFAULT_TTL_S``, which is the lifetime of a session
#: already over by the time this is asked.
SESSION_FRESH_WINDOW_S = 30 * 60.0

# The one-line history note's reason clause, keyed on the reason's TEMPLATE
# rather than its code: a resume needs the SHAPE of what happened, and the
# registry's own ``message`` is a full instruction written for the live screen,
# which on a resume would be a wall of stale advice.
_FAILURE_HISTORY_REASONS = {
    TEMPLATE_VERIFY_FAIL: "it wasn't confirmed",
    TEMPLATE_SESSION_RESTART: "it stopped before finishing",
    TEMPLATE_HARD_STOP: "it couldn't continue",
}
# Covers fix_and_retry, silent_auto_retry, the volume template, and any code
# this build does not know. Deliberately the weakest true statement: all of them
# ended a measurement that did not finish.
_FAILURE_HISTORY_REASON_DEFAULT = "it didn't finish"

def _record_is_fresh(record: Mapping[str, Any]) -> bool:
    """Is this persisted record the moment the household is in RIGHT NOW?

    Named for the RECORD rather than any one writer: the terminal failure
    (#1942), a banked finding, and the session's own last activity (#1947) all
    want the identical answer from one clock.

    A record with no ``at`` answers False — the fail-honest direction. The state
    file's schema version is deliberately NOT bumped for these keys (a bump makes
    ``load_v2_state`` reject every deployed Pi's file, discarding
    ``previous_candidate_fingerprint`` and the way back with it), so legacy
    records arrive undated, and undated means "we cannot say this is current".

    A clock that has stepped BACKWARD yields a negative age and reads as fresh,
    which is the safe direction.
    """
    at = _finite(record.get("at"))
    if at is None:
        return False
    return time.time() - at <= SESSION_FRESH_WINDOW_S


def _session_is_live(status: Mapping[str, Any]) -> bool:
    """Is the durable state's session the one the household is in RIGHT NOW?

    A session can end — walked away from, browser closed, Pi rebooted — without
    ever persisting a terminal failure (#1947), and those states replayed the
    dead session's screen, its live imperative and its numbers on the next plain
    GET. The clock is the state file's own ``updated_at``, whose every write
    site is a session transition.

    A NON-TERMINAL capture block short-circuits the clock, and this is the one
    place that signal is sound: there is no failure and no purge, so a slot the
    wizard still holds is proof the session is not over — and it has to win,
    because a commission's own wall-clock ceiling is 3600 s, double this window,
    so a legitimately in-flight session CAN idle past it. Unknown statuses read
    as in flight: waiting costs a poll, ending early strands a round.
    """
    from .arm_walk import SESSION_ENDED_STATUSES

    capture_status = str(_mapping(status.get("capture")).get("status") or "")
    if capture_status and capture_status not in SESSION_ENDED_STATUSES:
        return True
    return _record_is_fresh({"at": _v2(status).get("updated_at")})


def _record_when_phrase(record: Mapping[str, Any]) -> str:
    """"yesterday" / "earlier today" / "on July 29" / "on July 29, 2025".

    Follows ``jasper.tools._format_relative_date``'s shape: a bare "on July 30"
    when today IS July 30 reads as a date the household has to decode.

    Answers ``"earlier"`` — no date claim at all — for a record this build
    cannot place on a calendar: an undated pre-#1942 record, or a stamp so far
    out of range that rendering it would raise (glibc ``localtime`` rejects
    roughly <= -1e16, and an uncaught ``OSError`` here is a 500 on the wizard's
    main GET) or print a nonsense year.

    Shared with the banked-finding line so a finding and a failure never
    describe the same afternoon differently.
    """
    at = _finite(record.get("at"))
    # A negative stamp is not a clock reading this project ever produced; it
    # is a corrupt/garbage field, and the year it would render is nonsense.
    if at is None or at < 0:
        return "earlier"
    try:
        stamp = time.localtime(at)
        now = time.localtime()
        if (stamp.tm_year, stamp.tm_yday) == (now.tm_year, now.tm_yday):
            return "earlier today"
        yesterday = time.localtime(time.time() - 24 * 60 * 60)
        if (stamp.tm_year, stamp.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
            return "yesterday"
        fmt = "%B %-d" if stamp.tm_year == now.tm_year else "%B %-d, %Y"
        return f"on {time.strftime(fmt, stamp)}"
    except (OSError, OverflowError, ValueError):
        # Platform-dependent range limits. Never let a corrupt byte on disk
        # turn the wizard's own entry screen into a 500.
        return "earlier"


def _failure_history_note(code: str, failure: Mapping[str, Any]) -> str:
    """The aged failure's ONE quiet line: what happened, and when.

    Dated because an undated outcome presented on a resume is the defect itself
    (#1942: last session's numbers read as this session's verdict).
    """
    when = _record_when_phrase(failure)
    spec = REASON_REGISTRY.get(code)
    reason = (
        _FAILURE_HISTORY_REASONS.get(spec.template, _FAILURE_HISTORY_REASON_DEFAULT)
        if spec is not None
        else _FAILURE_HISTORY_REASON_DEFAULT
    )
    return f"Your last measurement ended {when} — {reason}."


def _session_history_note(status: Mapping[str, Any]) -> str:
    """The aged NO-failure session's ONE quiet line: what happened, and when.

    A session that simply stopped (#1947) left no reason code, so this is
    deliberately the weakest true statement: it ended, when it ended, and
    whether the speaker was left tuned — the one fact a returning household most
    needs.
    """
    v2 = _v2(status)
    when = _record_when_phrase({"at": v2.get("updated_at")})
    if bool(v2.get("applied")):
        return (
            f"Your last measurement ended {when}, with the tuning it found "
            "already applied to your speaker."
        )
    return f"Your last measurement ended {when} before it finished."


def _banked_progress_note(status: Mapping[str, Any]) -> str:
    """What a stopped session KEPT, when the screen would otherwise imply a
    blind start-over (#2100).

    The failed Full stage 2 that filed #2100 kept an applied tuning and a
    passing check at the mark, and the household surface said only that the
    measurement ended — so the honest recovery (re-walk every spot) read as
    losing everything.

    ``""`` whenever the state cannot support the claim: nothing applied, or an
    express commission, which has no cross-position post-apply walk to be
    missing. The line never implies the spatial group can be resumed.
    """
    v2 = _v2(status)
    if not bool(v2.get("applied")):
        return ""
    if str(v2.get("tier") or "") == TIER_EXPRESS or _cloud_verify_block(status):
        return ""
    kept = "Your speaker keeps the tuning that was applied"
    if str(_mapping(v2.get("verify")).get("outcome") or "") == "pass":
        kept += ", and the check at the mark passed"
    return (
        f"{kept}. Only the wider check across several spots did not finish, "
        "and re-running it starts again from the first spot."
    )


# --- banked findings (WO-1's read half; panel lens C, CC1) --------------------


def _finding_notes(status: Mapping[str, Any]) -> list[dict[str, str]]:
    """What this speaker's measurement LEARNED, one line each.

    The read end of the wire ``_bank_household_findings`` fills. Until it
    existed, the flow banked a finding with validated household copy into a
    durable store and **nothing ever read one back** — so after #1949 the frame
    gate began proceeding on disputed evidence with the household's disclosure
    at none.

    **``household_copy`` and nothing else reaches this line.** The mechanism id,
    the evidence scalars, the confidence tier and the probe lists are internal
    taxonomy by ``jasper.attribution.findings``' two-vocabularies rule. Two
    independent things keep them off a household screen: the producer projects
    only the sentence and a clock, and this reader names the two keys it reads.

    **One line per finding, never a paragraph.** The sentence is minted and
    validated at the producer, so this composes nothing and only decides whether
    it needs a date in front of it — **dated when it is not the moment the
    household is in**, on the same clock and window as every other dated line
    here.

    Empty renders as nothing at all: a clean speaker has nothing to say.
    """
    rows = _v2(status).get("findings")
    if not isinstance(rows, list):
        return []
    notes: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = row.get("household_copy")
        if not isinstance(text, str) or not text.strip():
            continue
        if not _record_is_fresh(row):
            text = f"From your measurement {_record_when_phrase(row)}: {text}"
        notes.append({"text": text})
    return notes


def _aged_session_envelope(
    status: Mapping[str, Any], *, code: str, text: str,
) -> dict[str, Any]:
    """A session that is over: the ENTRY screen plus ONE quiet dated line.

    R11 of #1941 (#1942, generalised by #1947). A returning household must not
    be greeted by a screen from a session that is over — least of all one
    carrying the previous session's ``verify.evidence`` numbers as a live
    verdict, or a live imperative to go stand in front of the speaker. Every
    aged path renders the ordinary entry screen and reports the prior outcome as
    one ``info`` nudge.

    * **The entry screen's DATA contract, not just its copy.** ``_envelope``
      copies ``cloud`` / ``cloud_chart`` / ``tier`` through on EVERY screen and
      ``crossover/main.js`` calls ``renderCloud`` with no screen switch, so a
      resume rendered the dead session's before/after chart card and a caption
      promising a second pass that ended yesterday. Those three keys are nulled
      below; ``None`` is what the ``tier`` key's own contract already calls
      unknown.
    * **The way back survives** when there is one to offer —
      :func:`_way_back_action`, present exactly when the pre-apply stash names a
      candidate the bank can republish.
    * **Uniform across templates and phases**, because "act on this now" is
      equally untrue of all of them. If the blocking condition still holds, the
      next session refuses again and the household reads a FRESH verdict.
    """
    next_action, alternate_actions = _tier_choice_actions(status)
    env = _entry_envelope(
        status,
        next_action=next_action,
        alternate_actions=[*alternate_actions, *_way_back_action(status)],
        nudges=[{
            "code": code,
            # ``info``, never ``warn``: this is history, not a problem the
            # household has to solve. The wizard's nudge renderer already styles
            # the two differently.
            "severity": "info",
            "text": text,
        }],
    )
    # The dead session's measurement payloads. Nulled AFTER the build rather than
    # by sanitising ``status`` first, because ``tier`` has a second legitimate
    # reader: ``_recommended_tier`` uses it for the Recommended badge, which is
    # durable history about the speaker. What must not survive is the CHART's
    # copy of it. Pinned by the full-envelope guard test.
    for dead_session_key in ("cloud", "cloud_chart", "tier"):
        env[dead_session_key] = None
    return env


def _verify_fail_envelope(
    code: str, message: str, status: Mapping[str, Any],
) -> dict[str, Any]:
    """The VERIFY-fail screen (§5.2): one default "Try again" + the way back.

    Shared by ``REASON_VERIFY_OUT_OF_TOLERANCE`` / ``REASON_VERIFY_INCONCLUSIVE``
    and the VERIFY-phase override in :func:`_failure_envelope` for any OTHER
    code surfacing once the candidate is applied — the household is entitled to
    a route out the moment something is live on the speaker.

    **A code no retry can clear does not get a "Try again"** (#1873). For every
    other code the primary is honest; for ``verify_deterministic_mismatch``,
    whose verdict IS that a second attempt agreed with the first, it is a dead
    lever presented as the obvious next step. Such a code promotes **Re-measure**
    — the lever that CAN change the outcome — to the primary.

    Keyed on the code's OWN registry row, its template AND its budget, never on
    the code itself, so a future non-retriable row inherits this without an edit
    here. Both halves are load-bearing: a budget-0 code reaching this screen
    through the ``applied`` override says nothing about the VERIFY check (its
    zero budget means no extra CAPTURE can help, and "Try again" here is a fresh
    ``/v2/verify`` session, which for a dead capture is precisely the fix). Only a
    row whose own template is ``verify_fail`` is a verdict ABOUT the check.

    ``republish_previous`` and ``verify_remeasure`` carry ``show_during_capture``
    (W6.12, the same seam W6.10 added for the review screen's Apply): the
    JS action-row renderer's in-flight gate otherwise blanket-clears EVERY
    alternate action while the capture is still transitioning (``stopping``,
    a real window right after a failed capture, before the walk has fully
    wound down), so a household landing on this screen saw no buttons at all
    and had to guess "hit Stop" to make them reappear. ``verify_retry`` (the
    primary "Try again") deliberately keeps NO such flag: it starts a
    brand-new session, and doing that while the prior one is still tearing
    down is exactly the race the gate exists to prevent — the way back and
    Re-measure are the "get me out of this" affordances that must stay
    reachable regardless — "regardless" of the CAPTURE gate, that is, not a
    claim both are always offered (the way back needs a prior banked
    candidate; #1873 omits a promoted Re-measure from the alternates).
    """
    remeasure = {
        "id": "verify_remeasure",
        "label": "Re-measure",
        "endpoint": "/correction/crossover/v2/session",
        "body": {},
        "expert": True,
        "show_during_capture": True,
    }
    own_spec = REASON_REGISTRY.get(code)
    retriable = not (
        own_spec is not None
        and own_spec.template == TEMPLATE_VERIFY_FAIL
        and own_spec.retry_budget == 0
    )
    return _envelope(
        screen="verify_fail", active_step="verify",
        verdict=message,
        nudges=[{"code": code, "severity": "warn", "text": message}],
        next_action={
            "id": "verify_retry",
            "label": "Try again",
            "endpoint": "/correction/crossover/v2/verify",
            "body": {},
        } if retriable else {
            # Promoted, not duplicated: it leaves the alternate list below so the
            # household is never offered the same action twice. ``expert`` is
            # dropped with the promotion.
            #
            # ``show_during_capture`` is KEPT, and on a primary it does a second
            # thing: the wizard reads it as ``suppressConnectAffordance`` and
            # stands the capture block's own controls down. That is wanted here —
            # this verdict ENDS the capture session — and without the flag the
            # capture-in-flight
            # gate would hide this primary in that window, leaving the household
            # with the way back alone, or with NOTHING on a first-ever apply.
            **{k: v for k, v in remeasure.items() if k != "expert"},
            "label": "Re-measure this speaker",
        },
        alternate_actions=[
            *_way_back_action(status),
            *([remeasure] if retriable else []),
        ],
        status=status,
        # The flatness lines are a SIBLING claim to the integration-verify
        # numbers above, distinctly labeled; both travel in the same collapsed
        # disclosure since this screen has only the one mechanism.
        expert_details=(
            # ``code`` is this screen's own headline verdict — handed down so the
            # gate line can tell whether the record belongs to the capture being
            # described.
            _verify_expert_details(status, headline_code=code)
            + _verify_level_reference_lines(status)
            + _flatness_details_lines(status)
            # G1's numbers (#2087). EXPERT ONLY on this screen — no household
            # sentence, matching the banked-findings precedent: the household
            # copy here is the failure's own, and a second caveat would compete
            # with the one action they are being asked to take. The numbers still
            # belong, and arguably most of all here.
            + _ripple_reservation_lines(status)
        ),
    )


def _failure_pilot_heard(status: Mapping[str, Any]) -> bool | None:
    """Whether the failed capture's pilot pair was heard — or ``None``, unknown.

    ``locate_failed``'s copy branches on this (#2085), read through one accessor
    for :func:`_verify_gate_reflection_measured`'s reason. ``None`` is a third
    state, not a falsy second one: a failure that ran no capture, and every
    state file written before this shipped, simply do not say.
    """
    heard = _mapping(_v2(status).get("failure")).get("pilot_heard")
    return heard if isinstance(heard, bool) else None


def _failure_rollback_anchor_available(status: Mapping[str, Any]) -> bool | None:
    """Which ``correction_rollback_failed`` arm the record describes (#2291).

    ``True`` a restore was attempted against a real way back and did not
    complete, so going back is a live remedy. ``False`` there was never one.
    ``None`` is the third state, read for :func:`_failure_pilot_heard`'s reason.

    **The recorded ``True`` is ANDed with the way back being offerable NOW**:
    the record describes the round, but the sentence's remedy clause describes a
    button on THIS screen, and a sentence naming a control the screen cannot
    mint is the advertised-then-refused drift the door's preflight removes.
    """
    available = _mapping(_v2(status).get("failure")).get("rollback_anchor_available")
    if available is True and not _way_back_action(status):
        return False
    return available if isinstance(available, bool) else None


def _reason_message(
    code: str, spec: ReasonSpec, status: Mapping[str, Any],
) -> str:
    """This screen's sentence: the registry's copy, or its live rendering.

    SELECTION, never composition (#1974). Two codes have honest copy that
    depends on a fact only the record holds — whether the gate that came up
    short found a reflection (``verify_inconclusive``), and whether the capture
    that could not be located had been heard (``locate_failed``, #2085) — so the
    registry holds their fact-unknown renderings and this pulls the persisted
    facts out of ``status``. Every other code renders its registry copy
    unchanged, and no sentence is built here.

    Applies to EVERY template, not just verify_fail: ``locate_failed`` is
    ``fix_and_retry``, so a selector wired only into that branch would leave the
    screen most households see rendering the literal.

    WHICH sentence a fact produces belongs to
    :func:`~jasper.active_speaker.crossover_v2.refusal_copy.reason_message`,
    which the capture verdict and the budget refusal also call. Both facts are
    extracted unconditionally: branching here on which code needs which would
    put a second copy of that knowledge next to the one that owns it.
    """
    return reason_message(
        code, spec,
        pilot_heard=_failure_pilot_heard(status),
        reflection_measured=_verify_gate_reflection_measured(status),
        rollback_anchor_available=_failure_rollback_anchor_available(status),
    )


def _failure_envelope(
    code: str, status: Mapping[str, Any], active_step: str, *, applied: bool,
) -> dict[str, Any]:
    """Render one of the four §5.10 templates from a reason code.

    **Applied override:** once the crossover is DURABLY applied, ANY failure
    code renders through the ``verify_fail`` template regardless of
    REASON_REGISTRY's own owning template — the other templates hide the route
    out the household is entitled to the moment something is live on the
    speaker. REASON_REGISTRY stays the single copy source; only the template
    choice is overridden, except the copy addendum below.

    **THIS MUST KEY ON THE STATE FACT, NEVER ON ``active_step``/phase.** A
    terminal-failure persist landing WHILE the auto-apply transaction is still
    mid-flight resets ``accepted_phases``, which makes ``_phase_from_state``
    resolve back to ``PHASE_CHECK`` even once the other thread's apply durably
    flips ``applied=True``. The caller passes the state fact directly.

    **Copy addendum:** a ``TEMPLATE_SESSION_RESTART`` code owns copy that assumes
    NOTHING was ever applied ("start over…"), written for the pre-apply phases.
    Reaching this override with ``applied=True`` means the transaction genuinely
    landed, quite possibly racing the very failure being rendered, so this
    appends an honest acknowledgment rather than leaving that claim uncontested.
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
    # ONE resolution point for this screen's sentence, whichever template renders
    # it below (#2085). Resolved before the template branch so no branch can
    # quietly go back to ``spec.message``.
    message = _reason_message(code, spec, status)
    if applied and spec.template != TEMPLATE_VERIFY_FAIL:
        if spec.template == TEMPLATE_SESSION_RESTART:
            # The registry copy assumes nothing was applied ("start over…"); this
            # addendum keeps the household from believing nothing changed.
            message = f"{message} The crossover was already applied."
        return _verify_fail_envelope(code, message, status)
    template = spec.template
    if template == TEMPLATE_SILENT_AUTO_RETRY:
        # No decision screen: stay on the phase screen with a banner; the phone
        # auto-retries (§5.10 template 1). ``message`` rather than ``spec.banner``
        # — for a silent-auto-retry code the selector returns exactly the banner,
        # so this stays correct if an evidence-keyed code ever lands here.
        return _envelope(
            screen=active_step, active_step=active_step,
            verdict=message,
            nudges=[{"code": code, "severity": "info", "text": message}],
            next_action=None,
            status=status,
        )
    if template == TEMPLATE_HARD_STOP:
        # hard_stop keeps the capture block (Finding D contract): the failure
        # copy and the stopped/failed status stay visible together.
        # A hard-stop reason that knows the exact place explaining it (#1820)
        # declares that destination on its own ReasonSpec and wins over this
        # screen's generic one, so the verdict text and the action can never
        # disagree about what the household should do next.
        return _envelope(
            screen="hard_stop", active_step=active_step,
            verdict=message,
            nudges=[{"code": code, "severity": "warn", "text": message}],
            next_action=dict(spec.next_action) if spec.next_action else {
                "id": "speaker_setup", "label": "Back to speaker setup", "href": "/sound/setup/",
            },
            status=status,
        )
    if template == TEMPLATE_SESSION_RESTART:
        return _envelope(
            screen="session_restart", active_step="microphone_check",
            verdict=message,
            nudges=[{"code": code, "severity": "warn", "text": message}],
            next_action={
                "id": "restart_session",
                "label": "Start over",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
            },
            status=status,
            # The session this screen replaced is dead — do not re-advertise its
            # phone link / QR (W6.10 fold-in). Start over mints a fresh one.
            advertise_capture=False,
        )
    if template == TEMPLATE_VERIFY_FAIL:
        # One default — "Try again" (internally re-verify once, then re-measure)
        # — plus the way back; the explicit trio lives behind the expert
        # disclosure (§5.2).
        return _verify_fail_envelope(code, message, status)
    # TEMPLATE_FIX_AND_RETRY (the default decision screen).
    nudges = [{"code": code, "severity": "warn", "text": message}]
    if active_step == "apply":
        # Layer the SPECIFIC blocked-apply issue on top of the generic
        # REASON_APPLY_FAILED headline, so the household gets both an honest
        # generic outcome and the concrete reason the apply seam gave.
        apply_blocked = _mapping(_v2(status).get("apply_blocked"))
        if apply_blocked:
            nudges.append({
                "code": str(apply_blocked.get("id") or "apply_blocked"),
                "severity": "warn",
                "text": str(apply_blocked.get("message") or message),
            })
    return _envelope(
        screen="fix_and_retry", active_step=active_step,
        verdict=message,
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
    """The v2 session envelope for the served status.

    Stamped with :data:`CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION` rather than a
    number written down here.
    """
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
            "capture": _mapping(status.get("capture")) or None,
            "next_action": {
                "id": "room", "label": "Correct the room", "href": "/correction/room/",
            },
            "alternate_actions": [],
            "progress": {"position": 0, "total": len(_STEP_IDS)},
            "applied": _applied_chip(status),
            "round": None,
            "candidate_review": None,
            "cloud": None,
        }

    v2 = _v2(status)
    phase = str(v2.get("phase") or PHASE_CHECK)
    active_step = _PHASE_STEP[phase]

    # Volume recovery keys on needs_recovery, NOT unresolved_volume_safety alone:
    # a crash-hydrated active plan surfaces no unresolved payload but still needs
    # draining.
    if bool(v2.get("needs_recovery")):
        spec = REASON_REGISTRY[REASON_VOLUME_UNRESOLVED]
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
            next_action={"id": "speaker_setup", "label": "Finish speaker setup", "href": "/sound/setup/"},
            status=status,
        )

    failure = _mapping(v2.get("failure"))
    failure_code = str(failure.get("code") or "")
    if failure_code:
    # Pass the RAW state fact — never derive "was this applied" from
    # phase/active_step (see ``_failure_envelope``).
        applied = bool(v2.get("applied"))
        # Only a failure that is still the household's current moment gets the
        # terminal screen; an older one is history on the entry screen (#1942).
        # Keyed on the failure's OWN stamp rather than the session clock, so a
        # later unrelated persist cannot revive it.
        if _record_is_fresh(failure):
            env = _failure_envelope(
                failure_code, status, active_step, applied=applied,
            )
            note = _banked_progress_note(status)
            if note:
                env["nudges"].append({
                    "code": "crossover_v2_banked_progress",
                    "severity": "info",
                    "text": note,
                })
        else:
            env = _aged_session_envelope(
                status,
                code=failure_code,
                text=_failure_history_note(failure_code, failure),
            )
        log_event(
            logger, "correction.crossover_v2_envelope_serve",
            screen=env["screen"], phase=phase, failure=failure_code,
        )
        return env

    # #1947: the same defect on every route that persists NO failure record — a
    # session walked away from, a closed browser, a reboot — whose screens are
    # frozen durable state with a live imperative and yesterday's numbers on
    # them.
    #
    # Two phases are exempt. PHASE_DONE is a receipt about what the speaker is
    # playing NOW, which stays true a week later and asks nothing of anybody.
    # PHASE_REVIEW is untimed BY CONSTRUCTION (D3.5) and the entry screen has no
    # route back to it, so aging it out would strand a whole commission's
    # captures; #1947's case B stays open and wants a dated review screen rather
    # than this branch.
    #
    # ``session_id`` gates it because a box that has never measured has no
    # session to be dead, and dating a first-ever start screen would be the
    # opposite dishonesty. Like the aged failure above, this does NOT fall
    # through into the phase chain: post-apply the stale state resolves to
    # PHASE_VERIFY, and a fall-through would invite the household to "confirm
    # the result" of a session that ended yesterday.
    if (
        phase not in (PHASE_DONE, PHASE_REVIEW)
        and str(v2.get("session_id") or "")
        and not _session_is_live(status)
    ):
        env = _aged_session_envelope(
            status,
            code="crossover_v2_session_ended",
            text=_session_history_note(status),
        )
        log_event(
            logger, "correction.crossover_v2_envelope_serve",
            screen=env["screen"], phase=phase, failure="",
        )
        return env

    if phase == PHASE_CHECK:
        next_action, alternate_actions = _tier_choice_actions(status)
        env = _entry_envelope(
            status, next_action=next_action, alternate_actions=alternate_actions,
        )
    elif phase == PHASE_MEASURE:
        env = _envelope(
            screen="measure", active_step="measure",
            verdict=(
                "Keep the microphone still — JTS is measuring both drivers. Follow "
                "the measurement page; it continues automatically."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_CLOUD_MEASURE:
        # Same wizard screen as MEASURE: the household is still measuring, and
        # the phone is where the per-position instructions live. What changes is
        # the verdict copy, which must stop telling someone to keep the phone
        # still when the point of this phase is that they move it.
        env = _envelope(
            screen="measure", active_step="measure",
            verdict=(
                "JTS is measuring from a few different spots — follow the "
                "step below. Moving the microphone between spots is what lets "
                "JTS tell the speaker apart from the room."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_LATERAL:
        # R16's walk (§4.4). Same screen/step as the two above; bespoke copy
        # because MEASURE's keep-still verdict is wrong while the household MOVES
        # the microphone, and it must state the return to the mark.
        env = _envelope(
            screen="measure", active_step="measure",
            verdict=(
                "JTS is measuring from a few spots either side of the mark, "
                "and then back on it — follow the step below. Moving the "
                "microphone is what shows how the speaker's drivers hand over "
                "to each other away from the middle."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_ENTRY_BASELINE:
        # #2291's "before" capture. Same screen and step as the three above, with
        # copy saying the two things the neighbours' copy would get wrong: they
        # are on the mark and holding still, and this recording is what the tuning
        # gets compared against afterwards. "on the mark", not "BACK on the
        # mark": this capture follows MEASURE, where the microphone never left.
        env = _envelope(
            screen="measure", active_step="measure",
            verdict=(
                "One last measurement, on the mark and held still — this "
                "is how your speaker sounds now, so JTS can tell you whether "
                "the tuning actually improved it."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_APPLYING:
        # An apply is pending. RETAINED but unreached since PR-T3 (D10):
        # ``_phase_from_state`` can only return PHASE_APPLYING from the walk's
        # VERIFY-unaccepted-and-not-applied case, and no shipped session records
        # a VERIFY it has not accepted. The household-visible wait while the fit
        # runs is the ``closing`` screen; a blocked or errored apply surfaces
        # through the generic ``failure`` branch above.
        env = _envelope(
            screen="applying", active_step="apply",
            verdict="Applying the measured crossover to your speaker…",
            next_action=None,
            status=status,
        )
    elif phase == PHASE_VERIFY:
        verdict = (
            "The crossover is applied. Put the microphone back where it "
            "started and follow the measurement page to confirm the result"
        )
        # Express (M=1) has no post-apply cloud — this anchor is the WHOLE
        # post-apply check, not the first of several (§1.3). Full says nothing
        # extra here: its cloud walk follows.
        verdict += (
            " — this quick tune's only check, at the mark."
            if str(v2.get("tier") or "") == TIER_EXPRESS
            else "."
        )
        env = _envelope(
            screen="verify", active_step="verify",
            verdict=verdict,
            # STAGE 2's entry point (D2, PR-T3). The measuring session ended at
            # the review screen, so the post-apply check is a NEW session somebody
            # has to start — deliberately, because the session TTL begins
            # ticking at open while the household is still walking back to the
            # microphone.
            #
            # No ``show_during_capture``: while stage 2's own capture IS in flight
            # this screen renders the same copy with the action suppressed by the
            # shared capture gate — one button to start it, none to start it twice.
            next_action={
                "id": "verify_start",
                "label": "Check the result",
                "endpoint": "/correction/crossover/v2/verify",
                "body": {"stage": "post_apply"},
            },
            status=status,
            # The pre-apply cloud has already closed by the time this screen
            # renders (it walks BEFORE VERIFY), so its before-tuning disclosure is
            # available here too, on BOTH tiers since #1965.
            expert_details=_flatness_details_lines(status),
        )
    elif phase == PHASE_CLOUD_VERIFY:
        env = _envelope(
            screen="verify", active_step="verify",
            verdict=(
                "Checking the result from the same few spots — follow the "
                "prompts on the measurement page."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_CLOSING:
        env = _closing_envelope(status)
    elif phase == PHASE_REVIEW:
        env = _review_envelope(status)
    elif phase == PHASE_DONE:
        # The RESULT screen: plain-language outcome first — no numbers, no jargon
        # — with the measured numbers folded into the SAME collapsed "Technical
        # details" disclosure the former review screen used. The way back rides
        # the alternate list when a prior banked candidate exists.
        verify = _mapping(v2.get("verify"))
        candidate = _mapping(v2.get("candidate"))
        is_express = str(v2.get("tier") or "") == TIER_EXPRESS
        # Express disclosure (§1.3): the household is told exactly what was
        # verified ("confirmed at the mark") and named the upgrade path — never a
        # claim wider than what express measured. "the verified-everywhere
        # result" overclaimed past what a Full measurement re-checks: a handful of
        # prompted spots around the mark, never every point in the room.
        done_verdict = (
            "Your speaker is tuned and confirmed at the mark. Run a Full "
            "measurement for the result checked at several spots around "
            "the mark."
            if is_express
            else "Your speaker is tuned."
        )
        # The spec verdict gets a VOTE, on the primary copy.
        #
        # Both the headline above and the "Verified." badge read the TRACKING
        # comparator, which asks whether the speaker matched its own prediction,
        # not whether it is flat. The one instrument that compares the result to
        # FLAT is the post-apply cloud's spec verdict, and until now it reached
        # only a line inside the collapsed disclosure — so on 2026-07-27 a
        # household read "Your speaker is tuned" over a profile whose own gauge
        # had failed all three bands.
        #
        # Only an explicit False speaks. ``None`` (never measured, or a group that
        # could not be graded) leaves the copy alone.
        #
        # R19 (#2160): read the PRODUCER's spatial grade, not the cloud entry's
        # ``overall_passed``, which is ``False`` for BOTH a graded miss and a
        # spectrum no band survived to grade. ``_post_apply_grade`` is the one
        # owner of that distinction. Literal grade words because
        # ``jasper.active_speaker`` never imports ``jasper.web``; pinned against
        # the producer's constants by this module's contract tests.
        grade = _mapping(v2.get("post_apply_grade"))
        spatial = str(grade.get("spatial") or "")
        spec_passed = (
            True if spatial == "passed" else False if spatial == "failed" else None
        )
        if spec_passed is False:
            done_verdict = (
                "Your speaker is tuned, but the result still measures further "
                "from flat than the target in at least one band."
            )
        elif spatial == "unmeasurable":
            # The group closed and the gauge could not grade it. Distinct from
            # both a miss and a check that never ran: something WAS measured and
            # carried no readable answer, which points at the room.
            done_verdict = (
                "Your speaker is tuned, but the check that measures how flat "
                "it is could not read enough of the sound to say either way."
            )
            # Applied implies graded, and when it does not the household is TOLD
            # rather than restored behind their back. A failing grade already has
            # its own screen; this is the case where no check finished at all.
        elif not grade.get("graded", True):
            # Three answers, three sentences. "Never finished" is false for an
            # INCONCLUSIVE check — that one ran to completion and could not
            # decide, which points at a quieter room rather than a retry.
            grade_state = str(grade.get("state") or "")
            if grade_state == "inconclusive":
                # WHY it could not tell, from the verdict that produced the
                # outcome (#1974). One writer, shared with the verify_fail
                # screen: the pilot level-shift path reaches here too and
                # involves no reflection and no window. An empty clause is a
                # state file that does not record the cause, which leaves the
                # outcome stated and the cause unnamed.
                cause = verify_inconclusive_cause(
                    _verify_code(status),
                    _verify_gate_reflection_measured(status),
                )
                because = f" — {cause}" if cause else ""
                done_verdict = (
                    "Your speaker is tuned, but the check that confirms it "
                    f"could not tell either way{because}. Re-verify to try "
                    "again."
                )
            elif grade_state == "failed":
                # The check ran, completed, and did not pass — reachable here only
                # for a state file carrying no terminal result code to override
                # this copy, and behind a closed post-apply group since #2464.
                done_verdict = (
                    "Your speaker is tuned, but the check that confirms it did "
                    "not match its prediction, so this result is unconfirmed. "
                    "Re-verify to try again."
                )
            else:
                done_verdict = (
                    "Your speaker is tuned, but the check that confirms it "
                    "never finished, so this result is unverified. Re-verify "
                    "to confirm it."
                )
        elif grade.get("complete") is False:
            # #2098: the local check PASSED and it is a real result — just not the
            # result this tier promised. A Full session that verified at the mark
            # and never closed its post-apply group read here as an unqualified
            # "Your speaker is tuned": the widest claim on the narrowest evidence.
            # Express never reaches this branch — the mark IS its whole promise.
            done_verdict = (
                "Your speaker is tuned and confirmed at the mark, but the "
                "wider check across several spots has not produced a result "
                "— that part is unproven. Measure again to finish it."
            )
        result_outcome = str(grade.get("outcome") or "")
        # **A failed spatial grade caps this claim whatever the result code says**
        # (#2738, the twin of #2464's cap one surface over). ``_verify_claims``
        # always writes an ``integration`` entry, so the override below fired on
        # every post-R18 session whose VERIFY produced a tracking analysis — the
        # 2026-08-07 jts3 shape, a group that closed FAILED at −4.63 dB, rendered
        # as "Target verified." with the number reachable only inside the
        # collapsed disclosure.
        #
        # Capped HERE, where the grade is already reduced to ``spec_passed``, so
        # the copy below and ``_done_nudges`` read one capped fact.
        # ``verified_target`` is the one code that claims the target was reached;
        # the other three are NOT capped because each already refuses that claim.
        if spec_passed is False and result_outcome == RESULT_VERIFIED_TARGET:
            result_outcome = ""
        if result_outcome in {
            RESULT_VERIFIED_TARGET,
            RESULT_VERIFIED_BEST_EVALUATED,
            RESULT_KEEP_PREVIOUS,
            RESULT_INCONCLUSIVE,
        }:
            result_copy = {
                RESULT_VERIFIED_TARGET: (
                    "The measured result reached the target and matched its "
                    "prediction."
                ),
                RESULT_KEEP_PREVIOUS: (
                    "This result should not replace the previous sound. This "
                    "report changed nothing automatically — go back to the "
                    "previous tuning if this audition is still applied."
                ),
                RESULT_INCONCLUSIVE: (
                    "There is not enough complete evidence to grade this result. Valid saved "
                    "measurements are kept; this report changed nothing automatically."
                ),
            }.get(result_outcome)
            if result_copy:
                done_verdict = result_copy
            elif result_outcome == RESULT_VERIFIED_BEST_EVALUATED:
                miss = _finite(grade.get("absolute_miss_db"))
                hz = _finite(grade.get("absolute_worst_hz"))
                miss_text = (
                    f" by {miss:.2f} dB" + (f" near {hz / 1000:.2f} kHz" if hz else "")
                    if miss is not None else ""
                )
                # **Says what was measured, and names no comparison it did not
                # make.** The margin behind this grade is THIS candidate's
                # linearized forecast against THIS SAME candidate's un-linearized
                # one — one analysis, two versions of it. It is NOT a field of
                # alternatives (the corner selector is retired, ticket 2.4) and
                # NOT the previously-applied graph, which is ``commanded.py``'s
                # separate instrument.
                #
                # So the sentence claims only the prediction match and the miss:
                # an improvement clause would have to name a linearized-versus-
                # un-linearized forecast delta, which is not a thing a household
                # can act on. ``RESULT_VERIFIED_BEST_EVALUATED`` keeps its name —
                # it is banked in log events and receipts.
                done_verdict = (
                    f"This matched its prediction, but it still misses the "
                    f"target{miss_text}."
                )
        attempt_sentence = attempt_loop_verdict_sentence(status)
        if attempt_sentence:
            done_verdict = f"{done_verdict} {attempt_sentence}"
        alternate_actions = [
            {
                "id": "room",
                "label": "Continue to Room correction",
                "href": "/correction/room/",
            },
        ]
        # The two iterating rows PROMISE another round in their own copy and,
        # until now, offered no way to take it: a screen that names an action and
        # does not carry it is #2641's inert Keep button one screen over.
        #
        # The SAME re-measure the review screen mints — one endpoint, one empty
        # body, the tier inherited from the lapsed session. First in the list
        # because on a round that says another bite is coming, taking it is the
        # recommended next step.
        if _round_is_iterating(v2):
            alternate_actions.insert(0, {
                "id": "round_remeasure",
                "label": "Try again with what we learned",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
            })
        if is_express:
            alternate_actions.append({
                "id": "run_full_measurement",
                "label": "Run a Full measurement",
                "endpoint": "/correction/crossover/v2/session",
                "body": {"tier": TIER_FULL},
            })
        # Last, after the forward-looking exits: the way back is a safety net,
        # not the recommended next step, and the promotion below inherits this
        # list's ordering — the head must stay a forward action.
        alternate_actions.extend(_way_back_action(status))
        # The HEAD of ``alternate_actions`` is promoted to the primary rather than
        # a named action: the branches above already order that list by
        # recommendedness, so promoting the head inherits that decision. Never
        # empty — the room action seeds it; an edit that makes room conditional
        # owes this line a fallback.
        next_action, *alternate_actions = alternate_actions
        env = _envelope(
            screen="done", active_step="verify",
            verdict=done_verdict,
            next_action=next_action,
            alternate_actions=alternate_actions,
            # The badge may not claim more than the evidence: "Verified." still
            # means the tracking comparator passed, but a speaker whose spec
            # verdict FAILED gets the honest badge instead. G1's reservation
            # (#2087) is appended at the CALL SITE rather than inside
            # ``_done_nudges``, which returns nothing on a non-pass — the
            # reservation is about the MEASURE capture this tuning was built from
            # and is owed whichever badge won.
            nudges=(
                _done_nudges(
                    verify, spec_passed=spec_passed,
                    result_outcome=result_outcome,
                    tier=str(v2.get("tier") or ""),
                )
                + _round_adoption_nudges(v2)
                + _ripple_reservation_nudges(status)
                + _calibration_reservation_nudges(status)
            ),
            status=status,
            candidate_review=_candidate_review_payload(candidate or None),
            # Four report-only disclosures behind a PASS, each bounding the word
            # the badge uses: the band the tracking comparator graded (#1868),
            # the FRAME the comparison spanned (an instrument tilt is the
            # difference between "the model was right" and "the model and the
            # room disagreed smoothly and nobody said so"), what the gate DID,
            # and which §7 claims were proved — measured-vs-model alone cannot
            # fail on a null the model reproduces.
            #
            # ``raw_already_shown=False``: this screen has no evidence block, so
            # the frame block must print the RAW pair itself, or the one screen a
            # household reads as "it worked" would show the tilt-removed grade
            # alone.
            expert_details=(
                _verify_graded_band_lines(status)
                + _verify_claims_lines(status)
                + _verify_frame_lines(status, raw_already_shown=False)
                + _verify_gate_lines(status)
                + _verify_level_reference_lines(status)
                + _flatness_details_lines(status)
                # R9's rule, one instrument over (#2087): a screen that reports
                # a result owes the numbers behind any caveat printed above it.
                + _ripple_reservation_lines(status)
            ),
            # CC1: the result screen is the OTHER place a banked finding is owed.
            # It rides the durable projection rather than this session's bundle
            # deliberately — stage 2 is a different session in a different bundle,
            # so a finding the MEASURING session banked would otherwise vanish.
            findings=_finding_notes(status),
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
