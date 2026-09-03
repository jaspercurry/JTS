# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 session screen envelope (Wave 5a; two-stage since PR-T3).

Its schema version is :data:`CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION` below, which
is where it is read from rather than restated — two docstrings in this file had
already drifted to different stale numbers.

``docs/historical/crossover-measurement-productization-design.md``
§5.9/§5.10 defines the v2 screen sequence — ``("speaker_setup",
"microphone_check", "measure", "apply", "verify")`` — and the four
failure-screen TEMPLATES the flow
renders (silent auto-retry banner / fix-and-retry / hard stop / session
restart), plus the two special screens (``volume_recovery`` and the VERIFY-fail
one-default screen). This module is the pure ``status → envelope`` function for
that flow, reached directly or via ``crossover_envelope``'s
``build_crossover_envelope_logged`` (this call plus a serve log; its
``build_crossover_envelope`` shim was deleted as pure indirection) —
the only crossover flow since W5b retired the legacy schema-6 envelope and the ``JASPER_CROSSOVER_FLOW`` selector. It emits the envelope dict shape the
generic data-driven JS renderer consumes (``schema_version`` / ``screen`` / ``steps`` / ``verdict_text`` /
``nudges`` / ``relay`` / ``next_action`` / ``alternate_actions`` / ``progress``
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
# which this module may not import, so before #2662 both ends spelled the same
# four strings by hand and nothing held them equal.
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

# Bumped 13 → 14: the envelope gained an always-present ``round`` key — the last
# graded round's adoption ROW, outcome, and reason (#2537). Additive: no key
# removed or re-typed, and the crossover wizard's own module ignores it, so an
# unredeployed page is unaffected. The version is bumped for the same reason 13
# was: the consumer this key exists for is an EXTERNAL DRIVER chaining rounds,
# and it needs a stable way to know the contract is present rather than probing
# for a key that is ``null`` on every screen before VERIFY completes anyway.
#
# Bumped 9 → 10: the screen vocabulary gained ``review`` — the two-stage
# commission flow's apply-decision interlude (work order D3, issue #1806) — and
# the envelope gained a ``prediction`` key (the predicted curve + its stored
# spec verdict) that only that screen populates. Additive in both directions:
# no key was removed or re-typed, the crossover wizard's own module does not
# gate on this version, and every pre-existing screen's envelope is
# byte-identical apart from the new always-present ``prediction: null``.
#
# Bumped 8 → 9: ``candidate_review`` gained ``headroom_cost`` — the PR-L5
# max-level disclosure, carried as a ``{db, basis}`` compound so a cross-era
# stamp can never render as a bare number (two-stage commission work order D4,
# issue #1806). Additive: no key was removed or re-typed, and the crossover
# wizard's own module does not gate on this version, so an unredeployed page
# ignores the new key rather than refusing the envelope.
#
# Bumped 7 → 8: the screen vocabulary changed (review_apply removed, the
# "applying" in-flight screen added, the "done"/RESULT screen's shape changed
# to plain-outcome-first + expert disclosure + prominent Undo) — owner ruling,
# 2026-07-20.
# Bumped 10 → 11: a new screen id, ``closing`` — the measuring session's own
# tail, while the pre-apply cloud's close has not yet produced a candidate
# (two-stage commission work order D1, PR-T3 gate blocker B2). Those moments
# used to render as ``review`` with its no-candidate copy, which told a
# household mid-measurement that nothing had been produced and offered to
# throw it away. Additive: no key removed or re-typed, and the crossover
# wizard's own module is data-driven off ``verdict_text`` / ``next_action`` /
# ``alternate_actions``, so it renders the new screen with no JS change. The
# envelope also gains an always-present ``busy`` flag (see ``_envelope``).
#
# Bumped 11 → 12: the envelope gained ``findings`` — the household-readable
# lines of what this speaker's measurement banked (WO-1's read half; the
# first-principles panel's lens C, CC1). Populated on the review and done
# screens only, ``[]`` everywhere else, exactly like ``prediction``'s
# review-only rule one key over. Additive: no key removed or re-typed, and the
# crossover wizard's module does not gate on this version, so an unredeployed
# page ignores the key rather than refusing the envelope.
# Bumped 12 → 13: the ``relay`` block gained ``position_pending`` — what an
# externally driven (``TIER_REMOTE``) session is waiting for before it will
# admit its next capture: ``{index, attempt, degrees, role, action}``. Present
# ONLY while such a session is holding, and absent everywhere else.
#
# It cannot outlive the session holding it, and the guards that make that true
# live on the WRITE side rather than here: ``correction_setup``'s
# ``_set_relay_capture`` drops the gate reference the moment the relay slot
# leaves an in-flight status, and the gate itself clears ``_pending`` on BOTH
# exits from a hold (released, or refused as expired). Two screens additionally
# pass ``advertise_relay=False``, which blanks the whole block — that is a
# narrower belt-and-braces, not the mechanism. Additive: no key removed or
# re-typed, and the
# crossover wizard's module reads ``tap_link``/``status`` off that block and
# ignores the rest, so an unredeployed page is unaffected. The version is
# bumped because the EXTERNAL DRIVER this key exists for is a separate program
# that needs a stable way to know the contract is present.
#
# Bumped 14 → 15 for the browser-driven wired round (#2881). Two additions,
# both on the ``relay`` block's terms: ``source`` names the transport
# (``wired``/``relay``) for the whole in-flight life of a session, and
# ``position_pending`` gained ``prompt`` — the ``{progress, title, body}`` the
# capture plan already composes for that entry, so a surface with no capture
# page can say WHICH position it is waiting for in the plan's own words. The
# closing screen additionally mints its confirm/re-record actions on a wired
# session, where no phone owns them. Additive again; a driver reading
# ``degrees`` is untouched.
CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION = 15

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

# Which step is active for a given session phase. The position groups
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
    # Still measuring. Registered even while R16's flag is off: this map is
    # exhaustive now (an unmapped phase raises, it no longer falls back), and
    # the REJECTION screens use whatever step it produces as their own.
    PHASE_LATERAL: "measure",
    # #2291's entry baseline is the LAST thing stage 1 measures, and it is still
    # measuring: nothing has been applied and the apply decision has not been
    # put to the household yet. Left out of this map, the phase now raises
    # (rather than silently walking the stepper BACKWARDS to step 1 on the
    # final capture of the session, as an unmapped phase once did).
    PHASE_ENTRY_BASELINE: "measure",
    # The review interlude sits on the APPLY step: measuring is finished and
    # what the household is now doing IS the apply decision (two-stage D3).
    # It shares the step with PHASE_APPLYING deliberately — the stepper tracks
    # where the journey is, and "deciding to apply" and "applying" are the same
    # place in it.
    # The measuring session's tail stays on the MEASURE step: the household is
    # still finishing a measurement (confirming on the phone, or waiting out
    # the fit), and the apply decision has not been put to them yet.
    PHASE_CLOSING: "measure",
    PHASE_REVIEW: "apply",
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


def _linearization_octave_rows(
    octaves: Any,
    reasons: Any,
    driver_classes: Any = None,
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

    **A number here is not always a deficit, which is what #2638 cost.** The
    OBSERVE layer runs to the grid's own top, so past a driver's radiating
    band — where the crossover target dives at 24 dB/oct while the
    measurement floor stays put — the same subtraction returns a large
    POSITIVE number that is the stopband's arithmetic, not performance
    (+23.0 dB at 16k on the 2026-08-16 JTS3 candidate, whose largest filter
    gain anywhere was +2.5 dB). ``reasons`` is the fit engine's own
    band-for-band verdict for exactly that
    (``_candidate_summary``'s ``linearization_octave_reasons``, projected
    from ``LinearizationFit.reason_summary``); each band carries its code
    through as ``reason`` so the renderer can say which kind of number it is
    holding without re-deriving anything from frequencies. The vocabulary is
    :class:`~jasper.active_speaker.linearization_envelope.ReasonCode`'s and
    is owned there — this function neither reads nor interprets it.

    ``reason`` is OMITTED, not empty-stringed, for a candidate persisted
    before #2638: nothing recorded the verdict, and a row that renders
    exactly as it did then is the honest answer. Present-and-known and
    absent-because-unrecorded stay structurally different.

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

    ``driver_classes`` (audit item 4i) is ``_candidate_summary``'s
    ``linearization_driver_class`` — the role's declared
    ``LinearizationFit.driver_class``, passed through as ``row["driver_class"]``
    exactly as opaquely as ``reason`` is: this function does not compare it
    against ``"unknown"`` or anything else. The renderer needs it to tell an
    already-declared class's own prior apart from the undeclared default —
    only the second has a ``/sound/setup/`` action left to take. Omitted, not
    empty-stringed, when absent, the same present/absent discipline
    ``reason`` uses.
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
    max-level cost, inseparable from the era that stamped it (D4 / #1808).

    ``basis`` is whichever stamped era the candidate carries
    (:data:`~jasper.active_speaker.linearization_fit.HEADROOM_COST_BASIS_*`),
    passed through rather than collapsed: the two peak eras disagree in the
    direction #2758 opened — a narrower grid could read SMALLER than today's
    charge — so telling them apart is the whole reason the field exists.
    Anything else is ``unknown``, including an absence, which is the only
    evidence of a pre-#1808 era there is and is read rather than assumed
    current. A renderer must say so; "costs 22.5 dB" and "costs an amount
    measured a way we no longer use" are different sentences.

    ``db`` is ``None`` — not ``0.0`` — when the stored value is missing or
    unusable. Zero is a real, common answer here (every cut-only correction
    charges nothing), so defaulting an absent number to it would state a
    measurement the payload does not have.
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
    """Map the persisted ``_candidate_summary`` (jasper.web.correction_crossover_v2)
    into the plain-language shape the "applying"/"done" screens render (§5.2:
    trims, delay, polarity, ripple, plus confidence/fingerprint provenance).

    W6.10 blocker #2: the generic renderer's review body expected a candidate
    shape (``retained_crossover_regions``/``drivers``) the session never
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
    # A trim the round CARRIED rather than solved, per role. On the row itself
    # rather than as a flat bit beside the list, because unlike the crossover
    # and the polarity below there is one of these per driver — and the
    # renderer's rule is the same one: a pinned number is never worded as
    # something this round measured.
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
        # there. Carried as values rather than pre-rendered prose, matching
        # every other row here — and the two travel together because the
        # renderer's rule is the polarity row's rule: a pinned number is worded
        # "(pinned for this round)" and never as something the round measured.
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
        # WHERE the polarity above came from (#2607 S3). The renderer needs it
        # because one commitment — the declared design, taken when the capture
        # cannot support a polarity decision — must not be worded as a measured
        # result. Carried as the enum rather than pre-rendered prose, matching
        # how ``linearization_outcome`` reaches the same renderer.
        "alignment_objective": str(candidate.get("alignment_objective") or ""),
        # The SECOND way a polarity can fail to be a measured result: the round
        # pinned it. Its own bit rather than a fifth member of the renderer's
        # declared-design list, because it is a different claim — "as designed,
        # this measurement could not check it" versus "the operator held it
        # here" — and because that list is also read for the objectives whose
        # ANCHOR is withdrawn, which a pinned round's is not.
        "polarity_pinned": bool(candidate.get("polarity_pinned")),
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
            candidate.get("linearization_octaves"),
            candidate.get("linearization_octave_reasons"),
            candidate.get("linearization_driver_class"),
        ),
        # "This correction costs N dB of maximum level" (PR-L5), reaching a
        # screen-facing payload for the first time (two-stage commission D4).
        # The number is persisted by
        # ``correction_crossover_v2._candidate_summary``; this line is what
        # carries it the last hop, since the envelope's screens render from
        # this payload and not from that block.
        #
        # **A compound, not a bare float, and that is the whole point.** The
        # charge's derivation changed under #1808 and the stamp is not
        # re-derived on load (``docs/historical/linearization-campaign-2026-07.md``,
        # "Cross-era disclosure"), so the same correction discloses ~22.5 dB
        # under the old rule where it now costs ~5. A renderer handed a lone
        # ``headroom_cost_db`` has no way to know which it is holding and would
        # put an order-of-magnitude wrong level cost on the one screen whose
        # purpose is honesty. Pairing the number with its basis makes that
        # mistake unavailable rather than merely discouraged: there is no bare
        # scalar on this payload to render.
        "headroom_cost": _headroom_cost_payload(candidate),
    }
    # A candidate with nothing displayable (no trims, no alignment) stays
    # hidden rather than rendering an empty card.
    if not trims and delay is None and polarity_str is None:
        return None
    return payload


def _verify_graded_band_lines(status: Mapping[str, Any]) -> list[str]:
    """"checked X–Y Hz" — the span VERIFY's tracking comparison graded.

    **One owner, rendered on every outcome** (issue #1868). This line used to
    be produced inside :func:`_verify_expert_details` from the ``evidence``
    block, which the host persists only on a NON-pass outcome — so the done
    screen's "Verified." badge, the single place the household is told the
    result is good, was also the only place that never said what was
    checked. The band is materially narrower than the crossover region a
    reader assumes: its lower edge is clamped up to the tweeter's actual
    sweep floor and again to the capture's validity floor.

    Empty when no tracking comparison was reached — an early verify refusal
    graded nothing, and saying nothing is the honest rendering of that.
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

    **One owner, rendered on every outcome** (R18, #1868), like
    :func:`_verify_graded_band_lines` above and for a stronger version of its
    reason: that line says how WIDE the tracking claim is, this says which of
    §7's claims were made at all — and two of the four never are, so a
    "Verified." badge over an unstated claim set reads as four proofs where
    there are two.

    The crossover-region line prints on a PASS too: the number IS the
    disclosure. A not-evaluated region claim prints nothing — its reasons are
    measurement-internal and no household action follows.
    """
    claims = _mapping(_mapping(_v2(status).get("verify")).get("claims"))
    lines: list[str] = []
    absolute = _mapping(claims.get("absolute"))
    worst_db = _finite(absolute.get("worst_db"))
    worst_hz = _finite(absolute.get("worst_hz"))
    tolerance_db = _finite(absolute.get("tolerance_db"))
    # The claim's OWN band, printed WITH the number (R18 hearing-safety
    # review): this line lands under ``checked …`` — the TRACKING band — and
    # the dip can sit outside that one, reading as out-of-band noise on the
    # screen where the next step is chosen. Two claims, two bands, said.
    band = absolute.get("band_hz")
    pair = band if isinstance(band, (list, tuple)) and len(band) == 2 else (None, None)
    lo, hi = _finite(pair[0]), _finite(pair[1])
    if (worst_db is not None and worst_hz is not None and tolerance_db is not None
            and lo is not None and hi is not None):
        lines.append(
            f"crossover blend {worst_db:+.2f} dB at {worst_hz:.0f} Hz "
            f"over {lo:.0f}–{hi:.0f} Hz (limit {tolerance_db:.1f} dB)"
        )
    # Decision 10: what is being DONE about the number directly above. Placed
    # here rather than on a card of its own because this is exactly where a
    # reader asks it — a household that sees the same blend number reported
    # round after round, with nothing saying the loop has a lever aimed at it,
    # reasonably concludes nothing is happening. Read off the durable receipt,
    # never re-derived, so the screen and the graph cannot disagree.
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

    **One owner, rendered on every outcome**, exactly like
    :func:`_verify_graded_band_lines` above. The band says how WIDE the tracking
    claim is; this says how much of it was the instrument rather than the
    speaker. VERIFY differences an on-axis two-branch MODEL against an in-room
    gated MEASUREMENT, and on the 2026-07-29 corpus a single −0.79 dB/octave
    tilt between those frames accounted for 84 % of the flow's apparent
    prediction error (rung P1). A pass printed over an unstated tilt reads as
    "the model was right"; stating it makes the claim exactly as strong as the
    comparison.

    Lines are terse for the same reason :func:`_verify_level_reference_lines`
    is: the wizard joins ``expert_details`` with "; " into one collapsed
    paragraph beside "level error 0.42 dB (limit 1.0 dB)" and "checked
    2000–4000 Hz".

    **A tilt-removed grade is never rendered alone.** It is the smaller,
    friendlier number by construction on the corpus that motivated it, and a
    screen showing only it would be the flattering half of a comparison
    presented as the comparison. ``raw_already_shown`` says whether the caller
    has ALREADY put the raw pair on screen: the verify_fail screen has, through
    :func:`_verify_expert_details`'s evidence block, so repeating it there
    would be noise; the done screen has NOT, because the host persists that
    block only for a non-pass outcome — so on a pass this function prints the
    raw pair itself, from the same record the tilt-removed pair comes from and
    with no arithmetic of its own.

    Empty when no frame was fitted, since silence is the honest rendering of an
    unmeasured frame.
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

    Empty when the state carries none: a legacy file written before #1974, or a
    capture that could not be gated at all. Both are "the record does not say",
    which every caller here renders as silence.
    """
    return _mapping(_mapping(_v2(status).get("verify")).get("gate"))


def _verify_gate_lines(status: Mapping[str, Any]) -> list[str]:
    """What the gate did, in the one sentence that owns saying it (#1966).

    **One owner, rendered on every outcome**, like
    :func:`_verify_graded_band_lines` and :func:`_verify_frame_lines` beside
    it. This is the disclosure's first household surface: until now
    ``describe_gate``'s sentence reached only an off-by-default operator
    sidecar and a bundle artifact, so the screens said "checked 2000-4000 Hz"
    over a window whose validity floor and provenance were invisible.

    **Rendered verbatim, never re-phrased.** The string is
    :func:`~jasper.audio_measurement.gate_disclosure.describe_gate`'s, composed
    at verdict time and persisted; that function's own docstring is explicit
    that consumers do not re-word the fields, because re-wording is precisely
    how "a reflection was measured at X ms" and "no reflection found; window
    capped at the ceiling" started printing identically. So this function
    passes the sentence through and adds nothing — no prefix, no label, no
    reformatted number.

    **Whose gate — the caller owns that question, not this function.** The
    record belongs to the verify verdict the state carries, written with its
    outcome and code in one call (``crossover_v2_flow._set_verify_outcome``).
    It therefore always describes ONE capture — but not necessarily the one
    the surrounding screen is about, because the record deliberately survives
    an early-return retry while the screen's headline moves on.

    That matters because one of ``describe_gate``'s four sentences is DEICTIC
    — "this capture could not be gated" — and takes its referent from the
    screen. So a caller renders this only where the referent is unambiguous:
    the done screen always (it explains that very verdict), the verify_fail
    screen only when its headline code is the one that wrote the record. The
    check lives in :func:`_verify_expert_details`, which knows the headline;
    putting it here would need this function to know things it does not.

    Empty when no gate was recorded, since silence is the honest rendering of
    an unrecorded gate.
    """
    disclosure = _verify_gate(status).get("disclosure")
    if not isinstance(disclosure, str) or not disclosure:
        return []
    return [disclosure]


def _verify_gate_reflection_measured(status: Mapping[str, Any]) -> bool | None:
    """Whether VERIFY's gate actually found a reflection — or ``None``, unknown.

    The one fact the inconclusive copy branches on (#1974). ``None`` is a
    third state, not a falsy second one: a state file written before this
    shipped does not say, and the copy owner answers an unknown cause by
    naming no cause at all rather than by assuming the safer-sounding branch.
    """
    measured = _verify_gate(status).get("reflection_measured")
    return measured if isinstance(measured, bool) else None


def _verify_code(status: Mapping[str, Any]) -> str | None:
    """WHICH VERDICT produced the persisted verify outcome, or ``None``.

    The other half of the pair :func:`_verify_gate_reflection_measured` feeds,
    read through the same accessor so the two can never be taken from
    different blocks (#1974). ``None`` is a record written before this shipped
    — unknown, not "no verdict".
    """
    code = _mapping(_v2(status).get("verify")).get("code")
    return code if isinstance(code, str) and code else None


def _verify_level_reference_lines(status: Mapping[str, Any]) -> list[str]:
    """"level reference reset for this session…" — the #1927 disclosure.

    **One owner, rendered on every outcome**, exactly like
    :func:`_verify_graded_band_lines` beside it and for the same reason: a
    PASS is precisely when an unstated reset would let a household read
    cross-day identity into a claim that only ever covered this sitting.

    Since #1927 every verify session establishes its own G3 pilot-transfer
    reference rather than inheriting the previous one, so a day-later verify
    can no longer dead-end against a stale number. What the household gives up
    is cross-session comparability, and this line is where that is said out
    loud instead of being quietly true.

    **Dated, and inform-not-berate.** The date is what keeps this history
    rather than a verdict about the room (#1942): it names when the previous
    reference was taken, using the same phrasing the aged-failure note uses.
    Nothing here accuses the microphone — a re-placed mic and a different
    recording device both produce this step, and the line claims only what it
    measured.

    Phrased as one terse parenthetical because of WHERE it lands: the wizard
    joins ``expert_details`` with "; " inside a single collapsed "Expert
    details" paragraph (``crossover/main.js``'s ``renderNudges``), beside
    "level error 0.42 dB (limit 1.0 dB)" and "checked 2000–4000 Hz". A
    sentence would read as an interruption in that list.

    Empty when there was nothing to disclose: no previous reference, or one
    this session's own chain agreed with.
    """
    reset = _mapping(_mapping(_v2(status).get("verify")).get("level_reference"))
    step_db = _finite(reset.get("step_db"))
    if step_db is None:
        return []
    # ``_record_when_phrase`` reads one ``at`` key and already answers
    # "earlier" for a stamp it cannot place on a calendar, so every dated
    # surface phrases a date the same way rather than growing a second
    # formatter.
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
    Empty when the session persisted neither tracking evidence nor a graded
    band (an early-return verify verdict — locate/agc/gate/level-shift —
    reaches neither).

    ``headline_code`` is the reason code THIS screen's verdict copy names. It
    gates the gate line alone — see the call below for why that one line needs
    it and the others do not.

    The band line comes from :func:`_verify_graded_band_lines`, which the
    done screen renders too — see there for why it is not an evidence-block
    field any more. It is appended INDEPENDENTLY of the evidence block rather
    than inside its guard: the two now have different presence conditions
    (the evidence needs a numeric notch-excluded max, the band needs only
    that a tracking comparison ran), and a screen that has the band should
    print it even when the numbers beside it are missing.

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
    # Whether the evidence block above put the RAW pair on this screen. It is
    # the owner of those numbers here — it alone can state the gated one
    # against its limit — so the frame block below defers to it rather than
    # printing them twice.
    raw_already_shown = bool(lines)
    lines.extend(_verify_graded_band_lines(status))
    # WHICH §7 claims were proved (R18, #1868) — same independent-presence
    # terms as the band above, and next to it: the band bounds the tracking
    # claim's width, this says tracking is not the only claim nor the one a
    # handoff dip lands in.
    lines.extend(_verify_claims_lines(status))
    # The frame those numbers were measured ACROSS (rung P1), appended for the
    # same reason and on the same terms as the band above: independent of the
    # evidence guard, because it has its own presence condition and a screen
    # that has a frame should say so even when the raw numbers beside it are
    # missing.
    lines.extend(_verify_frame_lines(status, raw_already_shown=raw_already_shown))
    # And WHAT THE COMPARISON COULD SEE (#1966), last of the three: the band
    # says how wide the claim is, the frame says how much of it was the
    # instrument, and the gate says how much of the sound the measurement had
    # to work from in the first place.
    #
    # NOT the same independent-presence rule as those two, and the difference
    # is the one thing this screen must get right. The band and frame are
    # cleared by every later attempt, so they can only ever describe the
    # capture behind THIS headline. The gate record deliberately survives an
    # early-return retry (it is written with the outcome and code it belongs
    # to — ``crossover_v2_flow._set_verify_outcome``), so on this screen it can
    # outlive the verdict being displayed: ``_failure_envelope`` routes ANY
    # code through this template once the crossover is applied, including the
    # early returns that conclude nothing.
    #
    # One of ``describe_gate``'s four sentences is DEICTIC — "**this capture**
    # could not be gated" — and takes its referent from the screen rather than
    # from itself. Under a later attempt's headline, with the band and frame
    # already cleared, it renders as the screen's ONLY expert line and points
    # at the wrong capture. So the line renders here only when the headline's
    # verdict is the one that wrote the record. The done screen has no such
    # ambiguity — it explains that very verdict — and renders it always.
    if headline_code and headline_code == _verify_code(status):
        lines.extend(_verify_gate_lines(status))
    return lines


def _band_edges(value: Any) -> tuple[float, float] | None:
    """``(lo_hz, hi_hz)`` from a persisted two-element band pair, or ``None``.

    One spelling for the several band pairs a ``flatness`` block carries
    (``max_band_hz``, ``reference_band_hz``, and the tilt's own two), so a
    block written by an older build degrades the same way everywhere: the
    line that needed the pair is simply not rendered, rather than half-
    rendered around a ``None``.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lo, hi = _finite(value[0]), _finite(value[1])
    return None if lo is None or hi is None else (lo, hi)


# Below this the printed step ("0.00 dB") no longer supports a direction, so
# the "X sits above Y" clause is dropped rather than asserting an ordering
# the rendered precision cannot show.
_TILT_DIRECTION_FLOOR_DB = 0.005


def _attribution_lines(
    flatness: Mapping[str, Any], band_lo: float | None, band_hi: float | None,
) -> list[str]:
    """The two lines that stop the worst-band pointer from being read as
    "here is the peak to EQ" (issue #1857).

    The pointer is a distance from a pooled reference, so a band INSIDE that
    pool that is uniformly off drags the shared zero toward itself and
    inflates every other band's number. With the frame at its original
    250 Hz-8 kHz the corpus session that filed the issue read
    ``+4.84 dB @ 1339.6 Hz`` against a woofer that was flat to +/-0.1 dB,
    because a ~5 dB dark tweeter had already pulled the frame ~3 dB down; a
    household acting on that pointer would have EQ'd the wrong driver. The
    low-mid frame (See ADR-0194) took the two upper bands out of the pool,
    so that shape now points at the dark band — but a defect inside
    250 Hz-2 kHz still drags its own frame, which is what these two lines
    are for.

    Line one splits the pointer's own number: how much of it is *where the
    whole band sits* versus *what the curve does inside the band*
    (:attr:`~jasper.active_speaker.flat_spec.SpecFlatness.max_band_level_deviation_db`
    and ``max_band_ripple_db``, both lifted from the report). Line two is the
    band-to-band step
    (:func:`~jasper.active_speaker.flat_spec.spec_band_tilt`), the one
    reading here that **no reference-frame choice can move** — which is what
    let it ship while the anchor was still an open owner question, and what
    keeps it the reading to trust when the two disagree (See ADR-0194).

    **Disclosure only — decides nothing.** Every figure is copied from the
    same :class:`~jasper.active_speaker.flat_spec.FlatSpecReport` the pointer
    reads, no band's ``passed`` or the overall verdict moves, and the
    reference frame is untouched. A block written by an older build carries
    none of these keys and renders neither line, rather than a line built
    around a missing half.
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
    disclosure — max/avg deviation plus the excluded-bin count. Extracted (B1
    fix, adversarial review of PR #1780) so the post-apply CURRENT-STATE claim
    (:func:`_flatness_details_lines`, reading CLOUD-VERIFY) and the
    BEFORE-TUNING claim (:func:`_pre_apply_flatness_lines`, reading
    CLOUD-MEASURE) compute the identical arithmetic from whichever compact
    ``flatness`` block they were handed — one construction, not two.

    **The line NAMES its reference frame** (issue #1857). "From the spec
    reference" left the most load-bearing half of the claim unstated: the
    zero is a power mean pooled over ``reference_band_hz``, and on a speaker
    with one dark driver that frame sits materially below a driver-anchored
    one — enough to flip the worst-band pointer's sign and move it to a
    different driver. A reader who cannot see the frame cannot tell "the
    woofer is proud" from "the tweeter is dark", and those call for opposite
    corrections. A block without the key (written by an older build) keeps
    the previous unqualified wording rather than naming a frame this code
    would only be guessing at.

    **And it says how much of the number is the frame** (issue #1857, second
    half). Naming the reference band told a reader WHICH zero the pointer is
    stated against; it still left them to guess whether a "+3.36 dB at
    703 Hz" means there is a 3 dB peak at 703 Hz to EQ down. Often there is
    not: on the corpus shape that filed the issue, under that era's frame,
    the pointed-at band was flat to a tenth of a dB and simply SAT 3.26 dB
    above a zero a dark tweeter had pulled down.
    :func:`_attribution_lines` renders that split, plus the
    band-to-band step that no reference choice can move, immediately under
    the pointer — so the sentence a household acts on cannot be the pointer
    alone.
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
        # Bins, not "regions": ``SpecFlatness.n_excluded``'s own docstring
        # explains why an interval count would over-report here (it spans
        # the whole axis, including frequencies no spec band grades).
        lines.append(
            f"{excluded} of {graded + excluded} spec-band bins excluded from "
            "grading (interference, or below the measurement's validity floor)"
        )
    return lines


def _per_band_flatness_lines(spec_bands: Any) -> list[str]:
    """Every graded band's OWN worst deviation, from the SAME reference the
    pointer line above names (issue #1857).

    ``_flatness_lines_from_block`` already names ONE band -- whichever of
    the three read furthest from the reference. But that reference is a
    power mean pooled over the low-mid band
    (:data:`~jasper.active_speaker.flat_spec.REFERENCE_BAND_HZ`), so a band
    INSIDE it that is uniformly off drags the shared zero toward itself, and
    can make an unrelated band's ordinary ripple read as the LARGER deviation.
    Narrowing that frame to the low-mid band took the two upper bands out of
    it and so out of this failure mode; it did not take the low-mid band out
    of its own. That is the failure #1857 reports: under that era's frame a
    corpus session's shipped verdict
    read "+4.84 dB @ 1339.6 Hz" (the woofer band) while the tweeter sat
    uniformly ~5 dB dark across its own passband, because the tweeter's
    darkness had already pulled the reference down before the woofer's
    ordinary ripple was measured against it. Showing every band's own
    number beside the pointer's, from the reference the pointer already
    uses, means a reader is never limited to the single band the gauge
    happened to pick — the tweeter's own deviation sits right next to the
    woofer's, so nobody has to take the pointer's word alone for which
    driver is the actual problem.

    **Disclosure only — decides nothing.** Every figure here is copied
    verbatim from ``spec_bands`` (``_compact_cloud_status``'s own per-band
    projection of the SAME :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`
    the pointer line reads); nothing is recomputed, no band's ``passed`` or
    the overall verdict moves, and the reference frame itself is untouched.
    WHICH frame the spec anchors to is
    :data:`~jasper.active_speaker.flat_spec.REFERENCE_BAND_HZ`'s to state
    (See ADR-0194); this function only stops the current frame's single
    pointer from being read alone.

    Unevaluable bands (``passed`` not a bool, or either dB figure missing/
    non-finite) are silently skipped — the same "unevaluable is not a
    fabricated verdict" rule :class:`~jasper.active_speaker.flat_spec.BandResult`
    already follows. Returns ``[]`` (no line at all, not an empty-looking
    one) when nothing survives, so a caller need not special-case it.
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

    ``PHASE_CLOUD_VERIFY``, never ``PHASE_CLOUD_MEASURE``, whenever a
    post-apply cloud EXISTS: the pre-apply cloud is the UNCORRECTED baseline
    that exists in order to be out of spec (the same distinction PR-4's
    ``check_crossover_v2_cloud_pipeline`` blocker fix drew), so rendering it
    as "how flat is your speaker" would report a correct speaker as bad
    forever.

    **The choice is WHICH CLOUD EXISTS, not which tier (issue #1965).** It
    used to be a tier test — Express delegated to the pre-apply reader,
    everything else read CLOUD-VERIFY — which was right about Express and
    wrong about STAGE 1. A Full session's post-apply cloud does not exist
    until stage 2, so on the stage-1 REVIEW screen (the apply decision, and
    the household's biggest time investment) Full read an empty
    ``_cloud_verify_block`` and rendered NOTHING, while Express rendered
    "Measured before tuning: …" from the very same measured cloud. The tier
    with more measurement showed less measured evidence on the one screen
    where evidence informs a choice. Reading "post-apply cloud if there is
    one, otherwise the pre-apply cloud" is the same rule for both tiers, and
    it keeps Express's behaviour byte-identical: Express never produces a
    CLOUD-VERIFY entry — not "not yet", but PERMANENTLY, by the tier's own
    shape — so it takes the pre-apply branch on every screen exactly as it
    did before.

    ``_compact_cloud_status`` already projects the SAME flatness/spec_bands/
    carve_outs shape onto the CLOUD-MEASURE entry (the pipeline runs there
    too — see ``_close_cloud_group``), so the pre-apply branch reads that
    block under an explicit BEFORE-TUNING frame: the pre-apply cloud is still
    the uncorrected baseline, so its numbers are reported as "what was
    measured before tuning", never as "how flat your speaker is now".

    Empty when neither group has closed — that is "nothing measured yet", and
    saying nothing is the honest rendering of it. The fallback vocabulary for
    a post-apply group that closed but produced no usable gauge lives in
    :func:`_flatness_unavailable_line`.

    **The carve-out lines close the sentence** (plan PR-6b, owner decision 1).
    The excluded-bin count below says how much of the spectrum left grading;
    :func:`_carve_out_expert_lines` says which ranges and why, with τ/r. Owner
    decision 1 is explicit that the tolerance applies to the surviving
    envelope AND that the report discloses the carve-out with the numbers —
    a bin count alone satisfies only the first half — **on every tier**,
    since carve-outs are a post-apply-persistent fact ("EQ cannot fill
    these") regardless of which cloud measured them.
    """
    block = _cloud_verify_block(status)
    if not block:
        return _pre_apply_flatness_lines(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return _flatness_unavailable_line(block)
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
    lines.extend(_per_band_flatness_lines(block.get("spec_bands")))
    lines.extend(_carve_out_expert_lines(block))
    return lines


def _pre_apply_flatness_lines(status: Mapping[str, Any]) -> list[str]:
    """The BEFORE-TUNING flatness/carve-out disclosure (B1 fix, adversarial
    review of PR #1780) — the branch :func:`_flatness_details_lines` takes
    whenever no post-apply cloud exists.

    **Design direction (coordinator ruling on the review).** Reads the
    CLOUD-MEASURE compact block (:func:`_cloud_measure_block`) and frames its
    numbers explicitly as the BEFORE-TUNING state — never presented as "how
    flat your speaker is now" (that claim needs a post-apply cloud). Carve-out
    lines render VERBATIM from the same block, unprefixed by the before-tuning
    frame, because they are a distinct, post-apply-persistent fact ("EQ cannot
    fill these") that owner decision 1 requires disclosed on every tier, not a
    claim about the CURRENT state.

    **Two readers now, not one (issue #1965).** Express takes this branch on
    every screen, permanently — CLOUD-MEASURE is the only cloud it ever
    produces. Full takes it on the STAGE-1 screens, where its post-apply cloud
    does not exist yet; the same measured baseline, the same words.

    **The scope clause is a claim about the post-apply check, so it renders
    only where one has PASSED.** "The applied correction targets these; the
    result was confirmed at the mark only" says two things — a correction is
    applied, and the only confirmation of it was the single anchor sweep — and
    a passing post-apply tracking verify is exactly the state in which both
    are true. Reaching this branch already means no post-apply cloud closed,
    so "at the mark only" cannot overclaim. Before #1965 the clause was
    unconditional on Express and therefore rendered on screens where nothing
    was applied (review, closing) and where the check had FAILED
    (verify_fail); the numeric before-tuning lead was true on all of them and
    still leads them now.
    """
    block = _cloud_measure_block(status)
    flatness = _mapping(block.get("flatness"))
    if not flatness:
        return []
    if not flatness.get("evaluable"):
        # Same capitalized lead as the evaluable arm below. It was lowercase,
        # which read as a fragment beside its sibling and let the two arms
        # drift apart under tests that pin only one of them.
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
    PR #1780) rather than ``status`` — the caller picks CLOUD-VERIFY when a
    post-apply cloud exists and CLOUD-MEASURE when none does
    (:func:`_flatness_details_lines`; the tier branch that used to make that
    choice went with #1965), since carve-outs are a post-apply-persistent fact
    disclosed from whichever cloud the session actually produced, not read
    from one hardcoded phase.
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

# Mirrors ``jasper.web.correction_crossover_v2.STAGE2_PREFLIGHT_KEY``. Spelled
# through a module constant on both sides rather than a bare literal in each,
# since the writer lives in the web package and this reader does not.
_STAGE2_PREFLIGHT_KEY = "stage2_preflight"


def _stage2_preflight(status: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    """``(can_open_stage_2, refusal_message, refusal_action)`` for the review
    screen's refusal DISCLOSURE (D3's render-time preflight, demoted from gate
    to disclosure — the apply transaction's ``_assert_stage_2_can_open`` is the
    boundary that refuses a truly un-openable stage 2).

    **Absence is not a clean reading.** An unset key means the predicate never
    ran — an envelope built off a path that does not attach it — and "we never
    checked" must disclose exactly like "we checked and it refused". Only an
    explicit ``ok: True`` renders quiet.

    The message is passed through verbatim — every one of
    ``resolve_conductor_context``'s refusals is already household-facing and
    already names what to finish first, so re-phrasing them here would be a
    second copy of copy that is owned elsewhere.
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
    reaches — the number D3 asks the screen to name beside the band ("this
    fails our spec by X in band Y"), not the raw deviation, which on its own
    says nothing about whether it was allowed.

    Reads only bands the report explicitly marks ``passed is False``. A band
    with ``passed`` ``None`` was not graded, and treating an ungraded band as a
    failing one would manufacture a verdict out of a missing measurement.
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

    **The prediction is a MODEL, and every sentence here says so.** The trap
    this copy is written against is the work order's own: *"Any copy that
    presents ``improvement_db`` as evidence about the room is measured-narrow-
    stated-wide. The measured evidence on the review screen is the pre-apply
    cloud; the prediction is a model, and the screen says so."* So the measured
    curve gets "measured" and the predicted one never does — it is what JTS
    "expects" or "works out", checked against the target rather than against
    the room.

    All four of ``_prediction_status``'s enumerated states render distinctly,
    because a consumer that collapses them tells a household something untrue
    about which of them it is in:

    1. **Curve + report** — the ordinary close. A graded pass says so; a graded
       miss names the band and the margin and is still offered, because
       improved-but-failing is *presented, never applied silently* (D3.4).
    2. **Curve, no report** — a prediction nothing could grade. Say the verdict
       is unknown; never infer one from the picture.
    3. **Neither** — nothing to review at all.
    4. **Report, no curve** — the refusal lane, where ``overall_passed`` is a
       REAL ``False`` rather than the ``None`` that means unknown. The verdict
       genuinely evaluated a prediction; what is missing is the drawing, and
       (this being the improvement-gate refusal) the candidate itself, which is
       why ``has_candidate`` is what decides whether a decision is on offer.
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

    Every stage-1 phase is accepted and no candidate exists yet. That is true
    at two very different moments, and this screen says which:

    * ``awaiting_confirm`` — the pre-apply cloud is walked and the group-close
      confirm is open. The household has something to do; WHERE depends on the
      transport, which is what the two action branches below answer.
    * ``running`` — they confirmed; the combine and the fit are in flight.
      Nobody has anything to do. This is the flow's one genuinely
      machine-paced wait, so it is the one screen that sets ``busy``.

    **Why this is not the review screen.** It used to be. Accepting the final
    cloud position resolves every stage-1 phase, so ``_phase_from_state``
    flipped to ``review`` the instant that capture landed — mid-hold, with the
    relay live and the wizard polling at 1.5 s. The household read "JTS
    measured your speaker but has no correction to propose — measure again to
    try afresh" over a measurement that was still running, with a destructive
    "Measure again" beside it. The absence of a candidate was being reported as
    a verdict when it was only a timestamp.

    **No SCREEN-LEVEL actions, and that is the point**: every action this
    screen could offer of its own is destructive of work in progress. "Measure
    again" would throw away a walked cloud; "Keep current sound" would abandon
    a fit that is about to produce the very thing the household is waiting for.
    Stop rides the relay block as it does on every in-session screen.

    **The confirm itself belongs to the household, here** (#2881): the screen
    mints Save / Record-again against the endpoints the runner already listens
    on (``/v2/complete``, ``/v2/retake``). Both carry ``show_during_relay``
    because the session they belong to is by definition still in flight.

    **…but NOT while a capture is held.** Tapping "Record the last spot again"
    puts the walk back at that slot, and the position gate holds its begin —
    a hold that reaches this screen because the group is still un-confirmed,
    so ``cloud_close`` keeps saying ``awaiting_confirm`` throughout. Minting
    the pair through that window makes the two moves fight: a screen-level
    primary suppresses the walkthrough that renders the hold (the wizard shows
    one primary at a time), so the retake's own prompt would render NOWHERE
    and the hold would run out its 600 s budget with the household looking at
    a Save button. The state is not a conflict to arbitrate — while a capture
    is held the confirm genuinely is not the household's move yet, and the
    pair comes back on its own the moment the gate releases.
    """
    from .arm_walk import SESSION_ENDED_STATUSES

    v2 = _v2(status)
    running = str(v2.get("cloud_close") or "") == CLOUD_CLOSE_RUNNING
    relay = _mapping(status.get("relay"))
    # The screen is derived from durable ``cloud_close``, not from the slot, so
    # it also renders after the walk ended with its group un-confirmed (Stop, a
    # failure, a reload past a restart). The two moves below POST into signals
    # the slot drops the moment it leaves an in-flight status, so minting them
    # for a dead session would be minting a 409.
    live = bool(
        str(relay.get("status") or "")
        and str(relay.get("status")) not in SESSION_ENDED_STATUSES
    )
    held = bool(relay.get("position_pending"))
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
        # the household just asked for, so point at the step that is waiting
        # rather than repeat that everything is measured.
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
            "show_during_relay": True,
        } if ready else None,
        alternate_actions=[{
            "id": "crossover_v2_retake",
            "label": "Record the last spot again",
            "endpoint": "/correction/crossover/v2/retake",
            "body": {},
            "show_during_relay": True,
        }] if ready else [],
        busy=running,
        status=status,
        # The pre-apply cloud's own flatness/carve-out disclosure is already
        # on record by now (its group closed to get here), and it is the same
        # measured evidence the review screen will lead with. Showing it here
        # lets a household read what was measured while the fit runs, without
        # being asked to decide anything about it.
        expert_details=_flatness_details_lines(status),
    )


def _review_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    """The REVIEW screen: the household's apply decision (work order D3).

    A dedicated human review screen existed and was deliberately removed
    (2026-07-20, "no human mid-flow Apply gate"); the 2026-07-28 ruling
    restores it, because auto-applying a fit that failed its own spec by
    +6.04 dB — inside the relay session, three seconds before VERIFY, with the
    household holding a phone — left a box applied-and-ungraded with no
    decision point anywhere in the sequence. **The review interlude IS the
    apply decision point.**

    Renders D3's five things in order: what we measured (the pre-apply cloud
    curve and its per-band verdict — ``cloud``/``cloud_chart``, already on the
    wire), what we propose (``candidate_review``, now including the L5 level
    cost), what we predict (``prediction``'s curve and stored verdict, drawn in
    the same deviation frame so the two are comparable by eye), the honest
    verdict (``_review_verdict``), and the decision.

    **No default, no timer, no auto-advance** (D3.5). The interlude is untimed
    by construction — it is not a session, nothing is holding a budget open,
    and the candidate stays reachable across a browser close, a page reload,
    and a ``jasper-web`` restart because it lives in durable state with no TTL.

    **"Keep current sound" does not delete the candidate**, deliberately: it
    ends the journey and returns to the Active speaker entry screen
    (``/correction/crossover/``), and the proposal stays reviewable until a
    newer measurement replaces it. Deleting on decline would make an accidental
    tap cost ten captures to undo.

    **No way back anywhere on this screen (D6).** The way back restores what
    an apply replaced, and stage 1 replaced nothing — offering it here would
    invite a household to "restore" a speaker that was never changed.
    """
    v2 = _v2(status)
    candidate = _mapping(v2.get("candidate"))
    review = _candidate_review_payload(candidate or None)
    prediction = v2.get("prediction")
    prediction = prediction if isinstance(prediction, Mapping) else None
    fingerprint = str(candidate.get("fingerprint") or "")
    # **One label, because nothing here re-answers "will Apply move the declared
    # crossover?".** This screen used to name a corner in the button — "Use 2750
    # Hz and apply" — by reading the Fc selector's banked recommendation. That
    # was a SECOND reading of a question `handle_v2_apply` already answers from
    # the candidate's own preset
    # (`crossover_declaration.declaration_change_for_candidate`), and the
    # selector is retired (`docs/tuning-master-plan.md` ticket 2.4) along with
    # the corner hunt that fed it. The live producer of a candidate crossing
    # somewhere other than the declaration is an operator's TOPOLOGY PIN, which
    # publishes no recommendation, so the second reading had already stopped
    # tracking the apply and could only under-claim. A round banked while a
    # selector existed still carries the record; this screen no longer reads it,
    # so a stale recommendation cannot name a corner the apply is not about to
    # write. Giving the button a corner again needs the review-time producer
    # PR-4 owns; do not restore it here with a third reading.
    apply_label = "Apply and verify"
    # A proposal with no fingerprint cannot be applied even in principle: the
    # apply endpoint's first gate is `expected_candidate_fingerprint`, and it
    # refuses outright without one.
    has_candidate = bool(review and fingerprint)

    can_open_stage_2, preflight_message, preflight_action = _stage2_preflight(status)
    # D4: an ungradeable prediction "renders as 'we could not predict this' and
    # DISABLES the Apply control, rather than presenting an unevidenced
    # proposal". A GRADED MISS is the opposite case and stays enabled on
    # purpose — presenting improved-but-failing rather than applying it is the
    # entire point of this screen (D3.4). `None` is not `False` here.
    gradeable = bool(prediction and prediction.get("overall_passed") is not None)
    # Stage-2 openability does NOT disable Apply: a disabled control was never
    # the boundary, and the apply transaction re-runs the same predicate
    # (`_assert_stage_2_can_open`) and refuses a truly un-openable stage 2.
    # The preflight stays a render-time DISCLOSURE (the nudge below), early
    # and loud, because suppressing a refusal that is knowable now is the
    # exact cost #1828 moved this predicate earlier to avoid — "burned a
    # link, walked to the phone, and hit a deterministic refusal that was
    # knowable before any of it".
    apply_enabled = has_candidate and gradeable

    # ONE condition owns both the refusal sentence and the refusal's own
    # resolution button below (review N-2), so the button never renders
    # unexplained. `has_candidate` stays in the gate: with no candidate there
    # is no Apply control at all, so a note about applying is noise.
    show_preflight_refusal = has_candidate and not can_open_stage_2
    nudges: list[dict[str, str]] = []
    if show_preflight_refusal:
        # The refusal renders AS ITSELF, in the words the predicate used —
        # never as a generic "cannot apply" (D3, and the #1828 precedent that
        # moved this same predicate earlier for the same reason).
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
    # G1's reservation (#2087) belongs on THIS screen for the reason the CC1
    # findings line below it does: this is where the household is asked to
    # decide, and the #1949 defect was disclosing nothing at exactly that
    # moment. Last of the three, and quietest — the two above are about
    # whether Apply can or should happen at all; this one qualifies the
    # evidence behind a proposal that is otherwise fine.
    nudges.extend(_ripple_reservation_nudges(status))
    nudges.extend(_calibration_reservation_nudges(status))
    apply_issue = _mapping(v2.get("apply_blocked"))
    # Gated on the fact the sentence is about: Sound holds a saved revision and
    # the DSP apply behind it is not confirmed. It used to ALSO require the Fc
    # selector's banked recommendation, which was the only review-time route to
    # a saved revision at the time; with the selector retired (ticket 2.4) that
    # term could only suppress the warning on rounds it now applies to.
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
            "show_during_relay": True,
        },
        {
            "id": "review_decline",
            "label": "Keep current sound",
            # #2641. This used to be href-ONLY, and the omission was
            # deliberate: declining changes nothing on the speaker, so a
            # navigation looked like the honest shape. Measured on a live
            # round-2 review screen it is not — the click reloads the page and
            # lands back on the SAME decision screen, so a household that has
            # decided is asked again, indefinitely. And the round record
            # cannot tell "the household declined" from "the household never
            # looked", which is the fact a series needs in order to stop
            # offering bites.
            #
            # So the decline is a real action. It still changes nothing on the
            # speaker and it still does not delete the candidate (see the
            # docstring) — what it records is the DECISION.
            "endpoint": "/correction/crossover/v2/decline",
            # The same guard ``review_apply`` carries, and for the same
            # reason: a decline recorded against a candidate that has since
            # been replaced would close a review the household never saw.
            "body": {"expected_candidate_fingerprint": fingerprint},
            # Kept, now as a PRESENTATION HINT rather than the action itself:
            # where a household ends up after declining, for a client that
            # cannot POST. The client prefers ``endpoint`` whenever both are
            # present (``main.js:renderActions``).
            #
            # The Active speaker ENTRY screen, not the generic /correction/
            # hub. The hub is the Room-correction wizard, whose first act is
            # a browser-mic HTTPS-transition interstitial -- a different
            # subsystem's permission flow, landed on by a household whose
            # context is "I just finished a crossover measurement and chose
            # to keep things as they are" (#1985). Every other href exit on
            # this flow names a real destination (/sound/, /correction/room/);
            # this one named a hub. Declining changes nothing, so the
            # honest destination is where the journey started.
            "href": "/correction/crossover/",
            "show_during_relay": True,
        },
    ]
    if preflight_action and show_preflight_refusal:
        # The refusal's own resolution control, from the SAME registry entry
        # the hard-stop screen reads — one click to the fix rather than prose
        # describing where to find it (#1820's precedent). Gated identically to
        # the sentence above so the button never appears unexplained.
        alternate_actions.insert(0, {**preflight_action, "show_during_relay": True})

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
            # D3: the restored screen's prior art, not a new mechanism — the
            # `show_during_relay` primary is what keeps Apply visible (and owns
            # the phone affordance) while the just-closed relay winds down.
            "show_during_relay": True,
        } if has_candidate else None,
        alternate_actions=alternate_actions,
        status=status,
        candidate_review=review,
        # D3.1: the pre-apply cloud IS the measured evidence on this screen, so
        # its flatness/carve-out disclosure belongs here — the same lines the
        # done screen folds away, on the screen where they inform a decision
        # rather than explain a fait accompli.
        # R17: Review accepts; Sound saves; the existing DSP path applies.
        expert_details=(
            _flatness_details_lines(status)
            + _ripple_reservation_lines(status)
        ),
        prediction=prediction,
        # CC1: this is the screen the #1949 ruling took a sentence away from.
        # The frame gate now banks its disagreement and PROCEEDS, so the
        # proposal below it was reached over evidence two instruments read
        # differently — and until this line the household was told nothing
        # about that at the exact moment they were asked to decide.
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
    post-apply walk closes (B1 fix, adversarial review of PR #1780; #1965) —
    see :func:`_pre_apply_flatness_lines`. ``PHASE_CLOUD_MEASURE`` is
    spelled through the shared phase constant for the same reason
    :func:`_cloud_verify_block` does.
    """
    return _mapping(_mapping(_v2(status).get("cloud")).get(PHASE_CLOUD_MEASURE))


# ``_spec_verdict`` lived here until R19 (#2160): one compact cloud entry's
# ``overall_passed``, read locally for the done screen's headline and badge.
# It was the SECOND owner of a question the producer now answers —
# ``_post_apply_grade``'s ``spatial`` — and it could not answer it correctly,
# because ``overall_passed`` is ``False`` for a spectrum that could not be
# graded as well as for one that was graded and missed. Deleted rather than
# repointed: a reader of that key here is exactly the drift §7 forbids.


#: The remote tier's ONE disclosure, as a done-screen badge. Info, never warn:
#: nothing went wrong and there is nothing to fix — the walk simply sampled one
#: axis, and a household reading "Your speaker is tuned" is owed the shape of
#: the evidence behind it. Recommends rather than blocks, exactly as express's
#: degraded-claim line does.
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

    Two instruments already vote here: the TRACKING comparator
    (``verify.outcome``) says whether the speaker matched its own prediction,
    and the post-apply cloud's spec verdict says whether it is flat. A third
    joins them (#1811): the delta probe can return ``level_mismatch``, which is
    NOT a rollback — it is a finding about the comparison's level axis, most
    often a level move our own offset accounting did not see — but it means the
    probe never got to answer the shape question. That has to reach the
    household, because everything else on this screen says "Verified."

    Ordering is deliberate: the strongest claim about the SPEAKER wins the
    badge slot (out-of-spec over verified), and the probe caveat is appended
    beside whichever badge won rather than replacing it. They answer different
    questions, so neither may silence the other. The terminal result code, when
    the caller has one, is the widest of those speaker claims and takes the
    slot — but it takes only the SLOT. Returning early for it silenced the
    caveats on every graded session (#2738), against this paragraph's own rule.
    ``result_outcome`` arrives already CAPPED, the way ``spec_passed`` arrives
    already reduced; the caller owns that rule and states it.

    A non-pass outcome gets no badge at all, exactly as before — that screen is
    the verify_fail one and carries its own copy — unless a result code
    qualified it, the one claim that outlives a failed comparison (#2605). The
    caveats stay behind the pass either way: each is a finding about a
    mark-VERIFY comparison that a non-pass screen never completed.
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
        # result to qualify. Nothing to disclose about the shape of evidence
        # that did not produce a verdict.
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
            # No hardware noun and no instruction to act: this is a statement
            # about what the check could and could not confirm. The way back
            # to the previous tuning is already offered on this screen.
            #
            # REASON-AWARE since #2537. The verdict alone stopped being enough
            # when #2533 split it in two: a level the quiet bins measured
            # across the whole graded band, and one they only measured in a
            # sliver of it. The whole-band sentence ("the overall loudness")
            # is false for the second, and it was being shown for it — the
            # 2026-08-15 JTS3 round's level was measured entirely above
            # 12 kHz. Same register either way, and the band-scoped one names
            # where rather than claiming a smaller version of the same thing.
            "text": _level_mismatch_text(probe),
        })
    if probe.get("verdict") == VERDICT_FRAME_MISMATCH:
        nudges.append({
            "code": "crossover_v2_frame_mismatch",
            "severity": "warn",
            # The tilt-carrying sibling of the caveat above, in the same
            # register and under the same rules: no hardware noun, no
            # instruction to act, and a statement about what the check could
            # and could not confirm rather than about the speaker (#2521).
            "text": (
                "The overall loudness and balance differed from what this check "
                "expected, so it could not confirm the correction's shape."
            ),
        })
    if probe.get("verdict") == VERDICT_SAFETY_ONLY:
        nudges.append({
            "code": "crossover_v2_safety_only",
            "severity": "warn",
            # The third caveat, in the two above's register and under the same
            # rules: no hardware noun, no instruction to act, a statement about
            # what the check could and could not confirm (#2614). This one is
            # not a finding about the speaker at all — the checks simply did not
            # run. Nothing was found, which is why the badge beside this still
            # says Verified.
            #
            # **It names BOTH halves since series-2 D1.** It used to say the
            # loudness half had run; on this path it cannot. The two directional
            # findings are differenced against the pre-apply capture, and a
            # state axis has no pre-apply reference to difference against — so
            # what the pre-D1 copy called a loudness comparison was a comparison
            # against the model, which is the confusion D1 exists to end.
            #
            # **No cause clause, deliberately.** FOUR paths reach this verdict —
            # the crossover corner moved, the applied record was displaced, it
            # named no graph, or it is era-older and names no corner — and the
            # first draft's "this round changed the crossover point" was true
            # for one of them and false on the screen for the other three. The
            # journal already names the specific reason on
            # ``…previous_graph_unavailable``, so the household copy says the
            # part that is true every time and nothing else. Reason-aware copy
            # is available here (``_level_mismatch_text``'s #2537 precedent one
            # function down) and is not worth four sentences for a caveat whose
            # action is identical in every case.
            "text": (
                "This check could not confirm the correction's shape or its "
                "loudness this round."
            ),
        })
    return nudges


def _level_mismatch_text(probe: Mapping[str, Any]) -> str:
    """The level-mismatch caveat, in the register the finding actually supports.

    Two sentences for one verdict (#2537), because #2533 gave it two reasons:

    * :data:`~jasper.active_speaker.delta_probe.REASON_UNCOMMANDED_LEVEL_SHIFT`
      — the quiet bins measured the level across the whole graded band, so
      "the overall loudness" is what was measured;
    * :data:`~jasper.active_speaker.delta_probe.REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND`
      — they did not, and the band they DID cover travels on the map as
      ``quiet.core_band_hz``. Naming it is the difference between a true
      sentence and a true-sounding one.

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
    # Checked inline rather than through an intermediate flag so the narrowing
    # is visible to a reader and to a type checker: ``band`` comes off a
    # persisted JSON payload, so every one of its shapes is possible.
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

    Local to this caveat rather than promoted: the wizards render frequencies
    in several house styles already, and picking one for all of them is a
    design decision this PR is not making.
    """

    if hz >= 1000.0:
        return f"{hz / 1000.0:.1f} kHz".replace(".0 kHz", " kHz")
    return f"{hz:.0f} Hz"


#: The household copy for a round that KEPT an imperfect result (#2537), as one
#: nudge rather than a screen: ``keep_for_iteration`` leaves the speaker in the
#: same state ``keep`` does, so it must not look like a failure — but the round
#: measured something it did not fix, and saying nothing is how "we could not
#: tell" becomes "verified" by silence.
#:
#: Deliberately register-matched to the caveats above: no hardware noun, no
#: instruction to act, and a statement about what this round did and did not
#: reach. The way back to the previous tuning is already offered on this
#: screen for a household that dislikes the result.
KEEP_FOR_ITERATION_TEXT = (
    "This is the best sound measured so far, and it is what the speaker is "
    "playing. Some of what was measured is still off target — measuring again "
    "is how that gets closer."
)

#: Row 7's sentence: the same news, with no round left to spend (#2656).
#:
#: Named for its ROW rather than for the sentence above it, because the outcome
#: differs — row 7 is a ``keep``, and only the row says the round did not pass.
#:
#: Recomposed from the two sentences this screen already owns rather than
#: written fresh: the first is :data:`KEEP_FOR_ITERATION_TEXT`'s verbatim,
#: because what the speaker is playing did not change, and the last clause is
#: :func:`_series_complete_text`'s for the same ending. What it cannot borrow
#: is "measuring again is how that gets closer" — a remedy this screen no
#: longer offers, and a promise with no button is exactly what #2655 minted
#: the button to stop.
KEEP_MISSED_EXHAUSTED_TEXT = (
    "This is the best sound measured so far, and it is what the speaker is "
    "playing. Some of what was measured is still off target, and that was the "
    "last round of this tuning."
)

#: The household copy for a round that PASSED and is iterating anyway (#2602).
#:
#: The sentence the owner's ruling exists to make possible: *in-tolerance is not
#: done*. Before it, this round said "verified" and stopped, and a household
#: reading that had no way to know the speaker still had 2.37 dB of tilt left in
#: it. So the copy has to do two things at once — report the good news honestly,
#: and say plainly that another round is coming — without turning either into
#: the other. "Inside the target" is the pass; "can still get flatter" is the
#: reason to keep going; neither is dressed as a fault.
KEEP_ITERATING_TEXT = (
    "Everything measured is inside the target, and it can still get flatter. "
    "This is the best sound measured so far and it is what the speaker is "
    "playing — measuring again is how the rest of the way gets found."
)

#: The same row, when the round could not grade how flat the result is.
#:
#: An ungradable objective keeps the series open rather than ending it (see
#: :func:`~.crossover_v2.verification.evaluate_iteration_headroom`), so this
#: row is reachable with NO measured flatness behind it — an Express tier
#: walks no post-apply cloud, and a report whose bands all fell below the
#: trusted floor grades nothing either. :data:`KEEP_ITERATING_TEXT` opens with
#: "Everything measured is inside the target", which on that round would be a
#: claim nothing supports, so the row keys on the reason as well.
KEEP_ITERATING_UNGRADED_TEXT = (
    "This is the best sound measured so far, and it is what the speaker is "
    "playing. There was not enough of a full result to tell how much flatter "
    "it could get — measuring again is how that gets answered."
)

#: The household copy for a round that PASSED and ENDED the series (#2602).
#:
#: Keyed by the headroom axis's own reason, because "there is nothing left to
#: chase", "it stopped moving", and "that was the third round" are three
#: genuinely different endings and a household that was told last round to
#: measure again is owed the specific one. Before #2602 this screen said nothing
#: at all here — the series simply stopped — which is the silence the ruling
#: names.
#:
#: The reasons are :mod:`~.crossover_v2.verification`'s, resolved lazily by
#: :func:`_series_complete_text` rather than imported at module scope: that
#: module reaches numpy through ``flat_spec``, and this one renders a
#: household polling surface. Same rule, and the same reason, as
#: :func:`~.crossover_v2.refusal_copy.round_restore_reason`'s own lazy import.
SERIES_COMPLETE_DEFAULT_TEXT = (
    "Everything measured is inside the target, and the tuning is finished."
)


def _series_complete_text(reason: str) -> str:
    """The ending sentence for a passing round that closed the series (#2602).

    An unrecognised reason falls back to :data:`SERIES_COMPLETE_DEFAULT_TEXT`
    rather than to silence: a household reading a done screen is owed "the
    tuning is finished" even when this build does not know which of the
    endings it was. The fallback states only what the ROW already proves, and
    never guesses at a cause.
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
        # Deliberately does NOT say "the third round". The cap is
        # ``ROUND_SERIES_CAP``'s to state, and spelling the number into copy
        # would be a second source of truth that a change to the constant
        # would silently turn into a lie on a household's screen.
        HEADROOM_CAP_REACHED: (
            "Everything measured is inside the target. That was the last "
            "round of this tuning, so it is finished here."
        ),
        # Reachable only from a receipt banked BEFORE the bites ruling, when
        # ungradable objectives still resolved to EXHAUSTED and so still
        # landed this row. Kept rather than deleted because the done screen
        # reads persisted receipts, and a household re-opening an older one is
        # owed the sentence that round actually decided. A round graded by
        # this build reaches ``KEEP_ITERATING_UNGRADED_TEXT`` instead.
        HEADROOM_NO_OBJECTIVES: (
            "Everything measured is inside the target. There was not enough of "
            "a full result to tell whether more rounds would help, so the "
            "tuning stops here."
        ),
    }.get(reason, SERIES_COMPLETE_DEFAULT_TEXT)


def _round_summary(status: Mapping[str, Any]) -> dict[str, str] | None:
    """The last graded round's adoption row, outcome, and reason — or ``None``.

    Read straight off the durable ``round_receipt`` the coordinator stamps; this
    computes nothing and re-derives nothing, which is what keeps the screen and
    the receipt from disagreeing about what a round decided.

    ``None`` for a session that has graded no round, which is every screen
    before VERIFY completes — an absence, not a row named ``""``.
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

    Keyed on the ROW for :func:`_round_adoption_nudges`' reason — two rows
    share the ``keep_for_iteration`` outcome and this needs both of them, so
    reading the outcome answers correctly for both of them by accident, and
    stops the moment a third row joins them. Reads the same ``round_receipt`` the copy does, so the screen
    can never promise another round in prose and withhold the button, or the
    reverse.

    **The budget is checked here too, and since #2656 that is defense in depth
    rather than the only bound.** A capped MISSED series now ends in the table
    itself, on its own row
    (:data:`~.crossover_v2.contracts.ADOPTION_ROW_KEEP_MISSED_EXHAUSTED`),
    which this function refuses on the row check above like any other
    non-iterating row. What the ordinal check still covers is a receipt banked
    BEFORE that change: those carry row 2 at the cap, and reading the row alone
    would offer them a fourth bite.

    Read as ``banked_ordinal >= ROUND_SERIES_CAP`` — the SAME comparison
    :func:`~.crossover_v2.verification.evaluate_iteration_headroom` makes,
    against the same constant, on the ordinal that round banked. Not a second
    definition of the budget: one constant, one comparison, two readers. An
    unreadable or absent ordinal offers the bite, which is the direction the
    series' own reader already fails in (a lost history resolves to the first
    round, never to "the cap was reached").
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

    Appended at the CALL SITE rather than inside :func:`_done_nudges`, for that
    composer's own stated reason and G1's precedent: ``_done_nudges`` answers
    "which badge did the VERIFY OUTCOME earn", and this answers what the ROUND
    decided — a different instrument, owed on this screen whichever badge won.

    **Keyed on the ROW, not the outcome, since #2602.** Four rows reach this
    screen and they share two outcomes between them: row 2 kept a result that
    MISSED, row 6 kept one that PASSED and is chasing flatter anyway, and rows
    1 and 7 both end the series with only the row saying whether it passed.
    Reading the outcome alone would tell a household with an in-tolerance
    speaker that "some of what was measured is still off target", which is
    false — and a household whose targets are still open that the tuning is
    finished and inside them, which is worse. The row is the stable identifier
    that survives exactly this kind of split — see
    :class:`~.crossover_v2.contracts.AdoptionDecision`.

    Reads off ``round_receipt``, which the coordinator stamps beside the
    receipt's identity, and computes nothing. Silent for a round that restored
    or escalated: those never reach the done screen at all.
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
            # One code, two endings, told apart by the ROW — the same split
            # the branch above makes on a reason. One code because the news a
            # household acts on is identical: the speaker is playing the best
            # measured sound and some of it is still off target. What differs
            # is whether another round follows, which the sentence says.
            "text": (
                KEEP_MISSED_EXHAUSTED_TEXT
                if row == ADOPTION_ROW_KEEP_MISSED_EXHAUSTED
                else KEEP_FOR_ITERATION_TEXT
            ),
        }]
    # An unrecognised or absent row, INCLUDING a pre-#2602 receipt that carries
    # an outcome but no row. Falling back to the outcome here would resurrect
    # exactly the ambiguity the row exists to remove, so the screen says
    # nothing rather than guessing which of two opposite sentences applies.
    return []


# --- G1's ripple reservation (owner ruling 2026-08-03, issue #2087) ----------
#
# Three functions, one owner. The flow decides (``_note_ripple_reservation``),
# the host persists, and everything a household or an operator READS about the
# reservation is composed here — so the sentence and the numbers cannot drift
# apart the way two writers of one fact always eventually do.


#: The one sentence. Written in the owner's own register for the #2087 ruling:
#: "We helped you as much as we could, but we have some reservations, and
#: here's what they are." v1 is deliberately one plain sentence, not a lecture.
#:
#: **It names no cause, and that is the load-bearing part.** A high predicted
#: ripple is consistent with the room, the microphone, the recording chain, and
#: the speaker itself, and this session separated none of them — so the copy
#: reports what the measurement saw and stops. The refusal it replaces did the
#: opposite: it reused ``low_alignment_confidence``'s "place the microphone
#: about 1 m in front of the speaker" and told a household with a correctly
#: placed mic to move it (issue #2085).
#:
#: **"blended less evenly" is the gloss, and it is deliberate.** The word the
#: instrument uses is *ripple*, which the owner himself had to ask the meaning
#: of; a household sentence that needs a glossary is not a disclosure. It names
#: no part of the speaker either — "high and low ranges" is the same
#: phenomenon-level vocabulary ``jasper.attribution.promotion``'s household copy
#: is held to, and the honest word for the parts involved would be a
#: hardware noun that register forbids.
#:
#: **It claims the tuning is rougher EVIDENCE, never a worse RESULT.** The
#: measurement says how coherently two branches summed, not how the speaker
#: will sound, and the accountability gates that grade the correction itself
#: all still ran. Copy promising a worse outcome would be a claim no
#: instrument in this session made.
RIPPLE_RESERVATION_COPY = (
    "We helped as much as this measurement allows: where this speaker's high "
    "and low ranges overlap, they blended less evenly than a clean measurement "
    "shows, so this tuning rests on rougher evidence than usual."
)


def _ripple_reservation(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """G1's banked reservation, or an empty mapping (#2087).

    The validating reader for a key that ``crossover_v2_status_block`` copies
    through unchecked. Empty is the honest answer for every unusable shape —
    absent (a clean capture, or a state file older than the ruling), the wrong
    type, or a record missing either number — because the two surfaces below
    both need the pair, and half a reservation is not one.
    """
    return _mapping(_mapping(_v2(status).get("measure")).get("ripple_reservation"))


def _ripple_reservation_nudges(status: Mapping[str, Any]) -> list[dict[str, str]]:
    """The household's one sentence, or nothing at all (#2087).

    ``info``, not ``warn``: the session succeeded and this is something to
    know rather than a problem to solve — the same register the banked-findings
    line uses, and the renderer gives them the same quiet styling.

    Gated on the VALUE parsing, not merely on the record existing, so a
    malformed reservation renders silence rather than a sentence whose expert
    line beneath it would be blank. The sentence itself quotes no number; the
    numbers are the expert line's job.
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

    What the sentence above deliberately leaves out: the measured value and
    the threshold it crossed. Both come from the ONE persisted record, so the
    line cannot claim a threshold the capture was not judged against — the
    reason the flow banks the threshold with the value instead of letting a
    renderer re-read the constant.
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


#: Audit gauntlet 5a: nothing on the tuning path named the mic-registration
#: step, so a household never learned an uncalibrated MEASURE was accepted.
#: One plain sentence, matching :data:`RIPPLE_RESERVATION_COPY`'s own "not a
#: lecture" register, pointing at the concrete surface
#: (jasper.cli.doctor.correction.check_correction_latest_bundle's sibling
#: doctor line names the same two steps — Microphone, Fetch calibration — in
#: its own register).
MIC_CALIBRATION_RESERVATION_COPY = (
    "This measurement used no calibrated microphone, so the result may be "
    "less accurate than usual. Register one under Microphone on /correction/."
)


def _calibration_reservation(status: Mapping[str, Any]) -> bool:
    """Whether the banked MEASURE ran with no resolved mic calibration
    (audit gauntlet 5a). The validating reader for a key
    ``crossover_v2_status_block`` copies through unchecked, mirroring
    :func:`_ripple_reservation`.
    """
    return bool(_mapping(_v2(status).get("measure")).get("calibration_reservation"))


def _calibration_reservation_nudges(status: Mapping[str, Any]) -> list[dict[str, str]]:
    """The household's one sentence naming the mic-registration step, or
    nothing at all.

    ``warn``, unlike the ripple reservation's ``info``: this one is directly
    actionable before the NEXT measurement (register a calibrated mic under
    Microphone on /correction/), so it gets the severity the doctor's sibling
    check for the identical fact
    (``jasper.cli.doctor.correction.check_correction_latest_bundle``) uses,
    not the "nothing to do about it" register a ripple reading gets.
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
    outcome of tuning twice — the microphone was set down between the two
    measurements, and the loop will not call the difference an improvement when
    part of it could be the new microphone position.

    Two vocabulary rules bind this sentence, and the second one caught a first
    draft of it. It is free of ENGINE words ("floor", "scope", "sitting") per
    the household-copy rule — those and the numbers ride in the decision's
    notes for a support read. And the actor is **the microphone, never "the
    phone"** (#1941 R4, guarded by ``tests/test_measurement_vocabulary.py``):
    the instrument a household holds is whatever browser reaches the relay, and
    the 2026-07-30 bench held a UMIK-2 while the wizard talked about a phone.
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
    # #2081's two refusals answer differently on purpose. A MISMATCH is a fact
    # about the household's own two measurements and has a sentence that says
    # so; an UNRECORDED sitting is a speaker that upgraded mid-journey and
    # cannot say where its older attempt was measured, which is the generic
    # "could not be compared reliably" and must not be dressed up as knowledge
    # about a phone that may never have moved.
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
    """The honest gauge-absent rendering (plan PR-5) for a CLOUD-VERIFY block
    that CLOSED but carries no usable flatness — two distinguishable states,
    told apart rather than collapsed into one message.

    * **The entry's pipeline DID run, but carries no gauge** —
      a durable state written by a build between PR-4 and PR-5, read after
      an upgrade without a new session. The pipeline was fine; only the
      gauge is missing. ``overall_passed`` is the tell: ``_compact_cloud_status``
      leaves it ``None`` for an unavailable pipeline and copies the spec
      verdict otherwise. Saying "could not be analysed" here would be a
      false statement about a session that analysed fine.
    * **The entry's pipeline never became available** — a
      combine or DSP-step failure. Say so, because the alternative is a
      screen that silently looks like the session with no spec claim in it.

    Neither quotes a number: the construction did not run (or its result was
    not recorded), so there is no spec-frame figure to give, fabricated or
    otherwise.

    **A MISSING entry never reaches here** (issue #1965). "No post-apply cloud
    at all" is not a gauge failure — it is stage 1, or Express, where the
    pre-apply cloud is the measured evidence — so
    :func:`_flatness_details_lines` routes that state to
    :func:`_pre_apply_flatness_lines` before this function is called, and
    hands this one the block it already read rather than re-deriving it.
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
    # Present so every tier this build knows has a name, NOT so a chooser
    # can render one: ``_tier_choice_actions`` offers exactly Full and
    # Express, because consenting to the remote tier means owning a
    # positioner the chooser cannot see. It is reached by API only.
    TIER_REMOTE: "Remote automated",
}
_TIER_CLAIMS = {
    # B2 fix (adversarial review of PR #1780): "across the room" overclaimed
    # past what the post-apply cloud actually samples — a handful of prompted
    # spots around the mark, never the room at large. "at several spots
    # around the mark" is the honest description of the same cloud.
    #
    # Two-stage work order D7: the post-apply claim now describes a session the
    # household STARTS SEPARATELY, after deciding to apply, so it reads as the
    # PURPOSE of a second walk rather than as something this first session goes
    # on to do. The claim's content is unchanged; only what it is grammatically
    # attached to moved, which is exactly what the split changed.
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


def _staged_walk_request() -> AngleCaptureRequest | None:
    """The staged walk, or ``None`` when none is staged.

    A PEEK: the session open is still the only take, so a household that reads
    this price and never presses Start leaves the walk staged.

    A slot that cannot be read, or a document that no longer validates, says
    NOTHING here — the session open refuses that loudly in the producing
    module's own words, and a chooser that repeated the refusal would be a
    second vocabulary for one fact. Every refusal the spool raises is a
    ``CrossoverV2FlowError``, a hand-edited field it cannot coerce included,
    so a corrupt document costs this screen an offer and nothing else.

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
        # never a hand-written prettier figure (§1.1).
        #
        # STAGE-AWARE since the two-stage split (work order D7). The old line
        # quoted ONE number of measurements against ONE duration, which stopped
        # being a session the moment stage 1 and stage 2 became separate ones:
        # a household choosing a tier is choosing both, with its own decision in
        # between, and a chooser that hid the interlude would sell a 15-capture
        # Full as one uninterrupted sitting. Every number here is still derived
        # from the plans themselves (``tier_display_info``), never written down.
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
        # session open (``_take_staged_angle_walk``) whichever tier is pressed:
        # the household is agreeing to these extra mic moves either way, so the
        # chooser is the last screen that can say so.
        # Priced against THIS tier's shape through ``resolve_plan_shape``, the
        # same tier → shape resolution the capture plans themselves use — a
        # second map here would be the desync ``V2PlanShape`` exists to close.
        price = walk_price(staged_walk, plan_shape=resolve_plan_shape(tier))
        action["staged_walk"] = {
            "program": staged_walk.program,
            "mic_moves": price["mic_moves"],
            "captures": price["captures"],
            # The WHOLE session's ceiling, not the walk's own share of it: the
            # line above quotes stage minutes for a session these stops
            # lengthen, so a card that stopped at the extra spots would
            # under-state what the household is agreeing to.
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
    """The microphone_check screen's tier chooser: both tiers first-class,
    the recommended one primary — never a silent default (§3).

    ``tier_display_info()`` is called ONCE here (N1 fix, adversarial review
    of PR #1780) rather than once per action — it is memoized, so this is a
    minor cleanup, not a correctness fix.
    """
    info = tier_display_info()
    recommended = _recommended_tier(status)
    other = TIER_FULL if recommended == TIER_EXPRESS else TIER_EXPRESS
    # Read ONCE, for both actions — one document, one peek per poll. Each
    # action prices it against its own tier: the walk belongs to the session,
    # and the session is whichever tier the household presses.
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
    advertise_relay: bool = True,
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
        # Optional collapsed expert-disclosure lines (the verify_fail screen's
        # tracking numbers, #1605) — the frontend folds them behind a
        # <details>. Empty on every screen that has none.
        "expert_details": list(expert_details or []),
        # A terminal / restart screen must stop advertising the dead phone link
        # and its QR (W6.10 fold-in) — the session it pointed at is gone.
        "relay": (_mapping(status.get("relay")) or None) if advertise_relay else None,
        "next_action": next_action,
        "alternate_actions": alternate_actions or [],
        # Whether this screen is MACHINE-paced — the speaker is working and the
        # household has nothing to do but wait. Declared for the renderer (a
        # spinner belongs on exactly these screens and nowhere else); no
        # renderer reads it yet, so it is inert until one does. ``False``
        # everywhere except the ``closing`` screen's fit-in-flight moment,
        # which is the only state in this flow where the wait is the speaker's
        # rather than the household's.
        "busy": bool(busy),
        "progress": _progress(active_step),
        "applied": _applied_chip(status),
        # WHICH adoption row the last graded round fired, for a driver chaining
        # rounds (#2537). Three of the seven rows restore and four keep the
        # graph, sharing two outcomes between them, so the outcome alone cannot
        # say which rule applied, and the reason travels from whichever axis
        # decided — the row is the stable thing to branch on. Load-bearing on
        # this very screen since #2602, not only for a driver: rows that share
        # an outcome carry opposite news, so the copy keys on the row.
        # ``None`` until a round has been graded.
        #
        # Machine data on a household surface, deliberately: the alternative is
        # a series driver fetching an evidence-bundle artifact to learn what the
        # screen it is already polling was built from. The household-facing
        # rendering of the same fact is the ``keep_for_iteration`` caveat.
        "round": _round_summary(status),
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
        # ``jasper.web.correction_crossover_v2_status._chart_cloud_status`` and
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
        # Two-stage commission D3: the PREDICTED response and its stored spec
        # verdict — the chart's third curve, drawn in the same deviation frame
        # as the measured one so a household can compare them by eye.
        #
        # **Sent by the REVIEW screen only, and that is what keeps the renderer
        # data-driven.** PR-T2's brief is explicit that the JS gains no
        # `env.screen` switch; making the third curve conditional on the SCREEN
        # in the client would be exactly that switch. Making it conditional on
        # the DATA instead puts the policy where the rest of this envelope's
        # policy already lives — here — and leaves the chart module with one
        # honest rule: draw the predicted curve when the envelope carries one.
        # It also leaves every shipped screen's chart byte-identical, since
        # none of them carries this key.
        "prediction": dict(prediction) if prediction else None,
        # WO-1's read half (panel lens C, CC1): the findings this speaker's
        # measurement banked, as household-readable lines. ``[]`` on every
        # screen that is not the apply decision or the result — the same
        # data-driven policy the ``prediction`` key above states: the SCREEN
        # decides what it carries, here, and the renderer keeps one honest
        # rule (draw the lines the envelope carries).
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
    exactly this screen rather than on a dead session's terminal one. Sharing
    the function is what makes "exactly this screen" true by construction —
    the resume differs from a clean start only by the actions handed in and
    one quiet history nudge, and cannot drift into a second entry copy.
    """
    return _envelope(
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
# the ORDINARY path (republish -> review -> apply; the operator runbook's
# republish-then-apply, wizard-shaped). One tap here restores the SLOT, not the
# graph — the wizard's next poll renders the review screen with Apply, and
# every admission gate still runs on that apply.
#
# A list so call sites can splice it (`*_way_back_action(status)`): empty when
# the pre-apply stash names no banked candidate — a first-ever apply, or a
# prior profile that was not a measured-candidate apply — because republish can
# only reach candidates the bank holds. The fingerprint is a pointer, never a
# promise: the bank verifies on POST, and a pruned or corrupted artifact gets
# the endpoint's own typed refusal. A factory rather than a shared constant:
# the action carries a mutable ``body`` and every caller hands its result to
# an envelope a caller may edit, so a module-level dict would share one
# ``body`` across every envelope this process ever serves.
def _way_back_action(status: Mapping[str, Any]) -> list[dict[str, Any]]:
    fingerprint = _v2(status).get("previous_candidate_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return []
    return [{
        "id": "republish_previous",
        "label": "Go back to the previous tuning",
        "endpoint": "/correction/crossover/v2/republish",
        "body": {"fingerprint": fingerprint},
        "show_during_relay": True,
    }]


# --- session recency (issues #1942, #1947) -----------------------------------

#: How long a dated record keeps rendering as the LIVE screen.
#:
#: **Why a clock at all, when a structural signal is usually better.** The
#: question is "is the household still in the moment this record was written?",
#: and it is asked on a plain GET. Every structural fact in the durable state —
#: ``session_id``, ``accepted_phases``, ``applied``, ``verify`` — is frozen by
#: the last persist and then reads IDENTICALLY one second later and one week
#: later, so none of them can discriminate. The one structural fact that does
#: change, relay liveness, changes the WRONG WAY: a terminal failure purges its
#: own relay within ``TERMINAL_FAILURE_PURGE_GRACE_S`` (3 s), so keying on it
#: would age out a screen the household is actively reading.
#:
#: **Why 30 minutes.** These screens are read with a microphone in hand and
#: acted on in seconds to minutes; the defect they guard against is measured in
#: hours to days. Thirty minutes sits an order of magnitude clear of both.
#: Erring long is the conservative direction on purpose: every realistic
#: in-session case keeps the shipped screen byte-for-byte, and only an
#: unambiguously-returning household takes the new path.
#:
#: Not borrowed from the relay's ``DEFAULT_TTL_S``: that is TIME_BUDGET_LINK,
#: the lifetime of a link which is already dead by the time this is asked, and
#: ``jasper.web._systemd`` already documents that a cloud session legitimately
#: outlasts it. Reusing it would import a caveat rather than a meaning.
SESSION_FRESH_WINDOW_S = 30 * 60.0

# The one-line history note's reason clause, keyed on the reason's TEMPLATE
# rather than its code: a resume needs the SHAPE of what happened, and the
# registry's own ``message`` is a full instruction written for the live screen
# ("Start over from this page to measure again — the quick microphone check
# runs first"), which on a resume would be both a wall of text and stale
# advice. Keying on the template also means #1942 adds no field to
# REASON_REGISTRY — that registry's copy is explicitly out of scope.
_FAILURE_HISTORY_REASONS = {
    TEMPLATE_VERIFY_FAIL: "it wasn't confirmed",
    TEMPLATE_SESSION_RESTART: "it stopped before finishing",
    TEMPLATE_HARD_STOP: "it couldn't continue",
}
# Covers fix_and_retry, silent_auto_retry, the volume template, and any code
# this build does not know. Deliberately the weakest true statement rather
# than a guess: all of them ended a measurement that did not finish.
_FAILURE_HISTORY_REASON_DEFAULT = "it didn't finish"

def _record_is_fresh(record: Mapping[str, Any]) -> bool:
    """Is this persisted record the moment the household is in RIGHT NOW?

    Named for the RECORD rather than any one writer: three kinds of dated
    record ask this question — the terminal failure (#1942), a banked finding
    (WO-1's read half) and the session's own last activity (#1947, through
    :func:`_session_is_live`) — and all three want the identical answer from
    one clock, so none can drift into its own idea of "still current".

    A record with no ``at`` answers False. That is the migration story and it
    is the fail-honest direction: the state file's schema version is
    deliberately NOT bumped for these keys (a bump makes ``load_v2_state``
    reject every deployed Pi's file, which would discard
    ``previous_candidate_fingerprint`` and the way back with it), so legacy
    records simply arrive undated. Undated means "we cannot say this is
    current", and the flow must never assert currency it cannot support.

    A clock that has stepped BACKWARD since the write yields a negative age
    and reads as fresh, which is the safe direction: the worst case is the
    screen that ships today.
    """
    at = _finite(record.get("at"))
    if at is None:
        return False
    return time.time() - at <= SESSION_FRESH_WINDOW_S


def _session_is_live(status: Mapping[str, Any]) -> bool:
    """Is the durable state's session the one the household is in RIGHT NOW?

    The generalisation #1942 deliberately scoped to one branch (#1947): a
    session can end — walked away from, browser closed, Pi rebooted — without
    ever persisting a terminal failure, and those states replayed the dead
    session's screen, its live imperative and its numbers on the next plain
    GET. The clock is the state file's own ``updated_at``, which
    ``crossover_v2_status_block`` projects and whose every write site is a
    session transition; see that projection for why no second stamp is added.

    A NON-TERMINAL relay block short-circuits the clock, and this is the one
    place that signal is sound. #1942 rejected relay liveness because a
    terminal failure purges its own relay within
    ``TERMINAL_FAILURE_PURGE_GRACE_S`` — true of the FAILURE record, which
    still keys on its own stamp alone. Here there is no failure and no purge,
    so a slot the wizard still holds is proof the session is not over, and it
    has to win: the closing screen's ``awaiting_confirm`` waits on a human, and
    a commission's own wall-clock ceiling
    (``session_volume_plan.MAX_WALL_CLOCK_CEILING_S``) is 3600 s — double this
    window — so a legitimately in-flight session CAN idle past it. Unknown
    statuses read as in flight, which is ``SESSION_ENDED_STATUSES``'s own rule
    and the safe direction: waiting costs a poll, ending early strands a round.
    """
    from .arm_walk import SESSION_ENDED_STATUSES

    relay_status = str(_mapping(status.get("relay")).get("status") or "")
    if relay_status and relay_status not in SESSION_ENDED_STATUSES:
        return True
    return _record_is_fresh({"at": _v2(status).get("updated_at")})


def _record_when_phrase(record: Mapping[str, Any]) -> str:
    """"yesterday" / "earlier today" / "on July 29" / "on July 29, 2025".

    Follows ``jasper.tools._format_relative_date``'s shape (the household-date
    precedent already in the tree): a bare "on July 30" when today IS July 30
    reads as a date the household has to decode into "oh, this morning", and
    with a 30-minute freshness window a same-day aged failure is ordinary.

    Answers ``"earlier"`` — no date claim at all — for a record this build
    cannot place on a calendar. That covers the undated pre-#1942 record AND
    a stamp so far out of range that rendering it would either raise (glibc
    ``localtime`` rejects roughly ≲ -1e16, and an uncaught ``OSError`` here is
    a 500 on the wizard's main GET) or print a nonsense year. Both resolve the
    same way, because "we cannot say when" is the honest answer to both.

    Shared with the banked-finding line for the same reason
    :func:`_record_is_fresh` is: one date vocabulary in this module, so a
    finding and a failure never describe the same afternoon differently.
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

    Dated because an undated outcome presented on a resume is exactly the
    defect (#1942: last session's numbers read as this session's verdict).
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

    A session that simply stopped — walked away from, browser closed, Pi
    rebooted (#1947) — left no reason code, so this is deliberately the weakest
    true statement: it ended, when it ended, and whether the speaker was left
    tuned. The tuned/not-tuned split is the one fact a returning household most
    needs, and the only claim this screen still makes about the dead session.
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
    losing everything. Naming what survived is what makes the re-walk
    explicable rather than unexplained.

    ``""`` whenever the state cannot support the claim: nothing applied, or an
    express commission, which has no cross-position post-apply walk to be
    missing (§1.3 degraded-claims table). The line never implies the spatial
    group can be resumed — current doctrine invalidates every capture phase on
    a new relay session, and it says so.
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

    The read end of the wire ``jasper.web.correction_crossover_v2
    ._bank_household_findings`` fills. Until it existed, the flow banked a
    finding with validated household copy into a durable store and **nothing
    ever read one back**: after #1949 the frame gate stopped hard-stopping and
    started banking-and-proceeding, which took the household's disclosure from
    one sentence to none at the exact moment the flow began proceeding on
    disputed evidence.

    **``household_copy`` and nothing else reaches this line.** The mechanism id,
    the evidence scalars, the confidence tier and the probe lists are internal
    taxonomy by ``jasper.attribution.findings``' own two-vocabularies rule. Two
    independent things keep them off a household screen: the producer projects
    only the sentence and a clock, and this reader names the two keys it reads
    rather than passing a row through — so a future writer that adds a field to
    the projection does not silently publish it here.

    **One line per finding, never a paragraph.** The sentence is minted and
    validated at the producer (``promotion.LEVEL_FRAME_HOUSEHOLD_COPY`` or its
    realized-level sibling ``REALIZED_LEVEL_HOUSEHOLD_COPY``, both checked
    hardware-noun-free and slug-free at construction), so this composes nothing
    and rewrites nothing — it only decides whether the sentence needs a date in
    front of it.

    **Dated when it is not the moment the household is in** (#1944's lesson,
    one instrument over). A finding banked minutes ago on the review screen is
    the session the household is standing in, and prefixing it with a date
    would be noise. The same finding read on a Monday — the review interlude is
    untimed by construction, and the done screen persists — is HISTORY, and
    presenting last week's diagnosis undated is the defect #1942 fixed for
    failure records. Same clock, same vocabulary, same 30-minute window.

    Empty renders as nothing at all: no heading, no "no findings" line. A clean
    speaker has nothing to say and says it — the same rule
    :func:`_carve_out_expert_lines` follows for a band that carved nothing.
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

    Requirement R11 of #1941 (#1942, generalised by #1947). A returning
    household must not be greeted by a screen from a session that is over —
    least of all one carrying the previous session's ``verify.evidence``
    numbers as if they were a live verdict, or a live imperative to go stand in
    front of the speaker. So every aged path renders the ordinary entry screen,
    unchanged, and reports the prior outcome as one ``info`` nudge.

    Three properties this shape buys, all of them the point:

    * **The entry screen's DATA contract, not just its copy.** There are TWO
      numbers surfaces on this flow. ``expert_details`` is empty on the entry
      screen and so needs nothing done to it — but ``_envelope`` copies
      ``cloud`` / ``cloud_chart`` / ``tier`` through from ``status`` on EVERY
      screen, the persisted ``cloud`` sits beside ``failure`` in the same state
      file, and ``crossover/main.js`` calls ``renderCloud`` with no screen
      switch. So a resume rendered the dead session's before/after chart card —
      its curve, its spec-band numbers, and a caption promising "the
      after-correction curve appears once the second measurement pass
      finishes", a live-progress claim about a session that ended yesterday.
      Those three keys are nulled below. ``None`` is not a special aged-only
      value: it is what the ``tier`` key's own contract already calls unknown,
      and this screen genuinely HAS no session.
    * **The way back survives, when there is one to offer.** A resume with a
      prior banked candidate keeps the same route out the live screens carry —
      :func:`_way_back_action`, present exactly when the pre-apply stash names
      a candidate the bank can republish.
    * **Uniform across templates and phases.** A stale ``hard_stop`` gets the
      same treatment as a stale ``verify_fail`` or a stale VERIFY prompt,
      because "act on this now" is equally untrue of all of them. If the
      blocking condition still holds, the next session refuses again and the
      household reads a FRESH verdict — strictly better than acting on a
      day-old one that may already be fixed.
    """
    next_action, alternate_actions = _tier_choice_actions(status)
    env = _entry_envelope(
        status,
        next_action=next_action,
        alternate_actions=[*alternate_actions, *_way_back_action(status)],
        nudges=[{
            "code": code,
            # ``info``, never ``warn``: this is history, not a problem the
            # household has to solve. The wizard's nudge renderer already
            # styles the two differently (crossover/main.js renderNudges), so
            # the quiet presentation needs no page change.
            "severity": "info",
            "text": text,
        }],
    )
    # The dead session's measurement payloads. Nulled AFTER the build rather
    # than by sanitising ``status`` first, because the same ``tier`` value has
    # a second, legitimate reader: ``_recommended_tier`` uses it to decide
    # which tier carries the Recommended badge, which is durable history about
    # the speaker (has a Full commission ever completed here?) and stays
    # exactly as true on a resume as on a clean start. What must not survive
    # is the CHART's copy of it. Pinned by the full-envelope guard test.
    for dead_session_key in ("cloud", "cloud_chart", "tier"):
        env[dead_session_key] = None
    return env


def _verify_fail_envelope(
    code: str, message: str, status: Mapping[str, Any],
) -> dict[str, Any]:
    """The VERIFY-fail screen (§5.2): one default "Try again" + the way back.

    Shared by ``REASON_VERIFY_OUT_OF_TOLERANCE`` / ``REASON_VERIFY_INCONCLUSIVE``
    (whose own REASON_REGISTRY template is already ``verify_fail``) AND the
    VERIFY-phase override in :func:`_failure_envelope` (W6.7 ruling 3) for any
    OTHER code surfacing once the candidate is applied — the household is
    entitled to a route out the moment something is live on the speaker, and
    the route is :func:`_way_back_action` (present when a prior banked
    candidate exists), regardless of which check failed.

    **A code no retry can clear does not get a "Try again" (#1873).** For every
    other code on this screen the primary is honest: "Try again" opens a fresh
    ``/v2/verify`` session and re-runs the check, and a transient condition can
    genuinely clear. For a code whose own registry row has already ruled the
    retry out — ``verify_deterministic_mismatch``, whose verdict IS that a
    second attempt agreed with the first — it is a dead lever presented as the
    obvious next step, and the household's field report on this screen is
    exactly that: they took it, repeatedly, until the relay session expired.
    So such a code promotes **Re-measure** —
    the lever that CAN change the outcome, by fitting a new crossover rather
    than re-checking the applied one — to the primary, and the way back stays
    beside it. The screen's action set is otherwise unchanged; what is removed
    is the one button whose own copy has just said it will not help.

    Keyed on the code's OWN registry row — its template AND its budget — never
    on the code itself, so a future non-retriable verify-fail row inherits this
    without an edit here. Both halves are load-bearing, and the template half is
    what keeps this scoped to the observed defect. A budget-0 code that reaches
    this screen through the ``applied`` override (``relay_timeout``,
    ``user_stopped`` — ``session_restart`` rows) says nothing about the VERIFY
    check: its zero budget means "no extra CAPTURE of the thing that failed can
    help", and "Try again" here is a fresh ``/v2/verify`` session, which for a
    dead relay is precisely the fix. Only a row whose own template is
    ``verify_fail`` is a verdict ABOUT the check, and only then does budget 0
    mean re-running it is pointless. A code with no registry row at all makes no
    claim either way and keeps the retry.

    ``republish_previous`` and ``verify_remeasure`` carry ``show_during_relay``
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
    reachable regardless — "regardless" of the RELAY gate, that is, not a
    claim both are always offered (the way back needs a prior banked
    candidate; #1873 omits a promoted Re-measure from the alternates).
    """
    remeasure = {
        "id": "verify_remeasure",
        "label": "Re-measure",
        "endpoint": "/correction/crossover/v2/session",
        "body": {},
        "expert": True,
        "show_during_relay": True,
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
            # Promoted, not duplicated: it leaves the alternate list below so
            # the household is never offered the same action twice. ``expert``
            # is dropped with the promotion — a primary the screen is steering
            # towards is not a disclosure-gated one.
            #
            # ``show_during_relay`` is KEPT, and it does a second thing on a
            # primary that it does not do on an alternate: the wizard reads it
            # as ``suppressConnectAffordance`` and hides the phone connect
            # link/QR (``crossover/main.js``, the review screen's Apply is what
            # it was built for). That is the wanted behaviour here, stated so it
            # is a choice rather than a side effect — this verdict ENDS the
            # capture session, so during the seconds the relay spends winding
            # down the screen should not still be advertising a link to it. It
            # is the same rule ``TEMPLATE_SESSION_RESTART`` follows with
            # ``advertise_relay=False``. Without the flag the relay-in-flight
            # gate would hide this primary in that window and leave the
            # household with the way back alone — or with NOTHING at all on a
            # first-ever apply, where there is no prior candidate to fall back
            # to. Either way: the affordance being promoted, gone at exactly
            # the moment it is needed.
            **{k: v for k, v in remeasure.items() if k != "expert"},
            "label": "Re-measure this speaker",
        },
        alternate_actions=[
            *_way_back_action(status),
            *([remeasure] if retriable else []),
        ],
        status=status,
        # Gauge fix (2026-07-24): the flatness lines are a SIBLING claim to
        # the integration-verify numbers above, distinctly labeled — both
        # travel in the same collapsed disclosure since this screen only
        # has the one "Expert details" mechanism.
        expert_details=(
            # ``code`` is this screen's own headline verdict — handed down so
            # the gate line can tell whether the record it would print belongs
            # to the capture being described. See ``_verify_expert_details``.
            _verify_expert_details(status, headline_code=code)
            + _verify_level_reference_lines(status)
            + _flatness_details_lines(status)
            # G1's numbers (#2087). EXPERT ONLY on this screen — no household
            # sentence, matching the banked-findings precedent (this envelope
            # passes no ``findings`` either): the household copy here is the
            # failure's own, and a second caveat beside it would compete with
            # the one action they are being asked to take.
            #
            # The numbers still belong, and arguably most of all here: "the
            # measurement this tuning was built from was rough" is exactly the
            # context for a verify that did not settle, and this screen's
            # reader has already opened the disclosure to look at numbers.
            + _ripple_reservation_lines(status)
        ),
    )


def _failure_pilot_heard(status: Mapping[str, Any]) -> bool | None:
    """Whether the failed capture's pilot pair was heard — or ``None``, unknown.

    ``locate_failed``'s copy branches on this (#2085), read through one
    accessor for the same reason
    :func:`_verify_gate_reflection_measured` is. ``None`` is a third state,
    not a falsy second one: a failure that ran no capture, and every state
    file written before this shipped, simply do not say — and the copy owner
    answers an unestablished fact by making no claim about it rather than by
    assuming the safer-sounding branch.
    """
    heard = _mapping(_v2(status).get("failure")).get("pilot_heard")
    return heard if isinstance(heard, bool) else None


def _failure_rollback_anchor_available(status: Mapping[str, Any]) -> bool | None:
    """Which ``correction_rollback_failed`` arm the record describes (#2291).

    ``True`` a restore was attempted against a real way back and did not
    complete, so going back is a live remedy. ``False`` there was never one —
    nothing to point the copy at. ``None`` is the third state, read for
    :func:`_failure_pilot_heard`'s reason: a code the question does not apply
    to, and every state file written before this shipped, simply do not say.

    **The recorded ``True`` is ANDed with the way back being offerable NOW**
    (``previous_candidate_fingerprint`` on the status — already gated on the
    republish door's own admission). The record describes the round, but the
    sentence's remedy clause ("Go back to the previous tuning") describes a
    button on THIS screen, and a sentence naming a control the screen cannot
    mint is the advertised-then-refused drift the door's preflight exists to
    remove. The False-arm copy states no cause, so rendering it here stays
    honest for every way the button can be gone.
    """
    available = _mapping(_v2(status).get("failure")).get("rollback_anchor_available")
    if available is True and not _way_back_action(status):
        return False
    return available if isinstance(available, bool) else None


def _reason_message(
    code: str, spec: ReasonSpec, status: Mapping[str, Any],
) -> str:
    """This screen's sentence: the registry's copy, or its live rendering.

    SELECTION, never composition (issue #1974). Two codes have honest copy
    that depends on a fact only the record holds — whether the gate that came
    up short actually found a reflection (``verify_inconclusive``), and
    whether the capture that could not be located had nonetheless been heard
    (``locate_failed``, #2085) — so the registry cannot hold a single literal
    for either and stay true. It holds their fact-unknown renderings; this
    pulls the persisted facts out of ``status`` and asks the flow module's
    single writer for the rendering that matches. Every other code renders its
    registry copy unchanged, and no sentence is built here.

    Applies to EVERY template, not just verify_fail (which is all it covered
    when it was ``_verify_fail_message``). ``locate_failed`` is
    ``fix_and_retry``, so a selector wired only into the verify_fail branch
    would have left the screen most households actually see rendering the
    literal — the same one-fix-two-surfaces gap this whole change closes.

    This reads the record; it does not decide. WHICH sentence a fact produces
    belongs to
    :func:`~jasper.active_speaker.crossover_v2.refusal_copy.reason_message`,
    which the relay verdict and the budget refusal also call — so all three
    surfaces stay one voice. Both facts are extracted unconditionally: they
    are dictionary reads, and branching here on which code needs which fact
    would put a second copy of that knowledge next to the one that owns it.
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

    Applied override (W6.7 ruling 3; generalized 2026-07-20 adversarial
    review — SF1 follow-up): once the crossover is DURABLY applied
    (``applied``, passed in as the raw ``status["crossover_v2"]["applied"]``
    state fact), ANY failure code renders through the ``verify_fail``
    template regardless of REASON_REGISTRY's own owning template.
    fix_and_retry / hard_stop / session_restart / silent_auto_retry all hide
    the route out the household is entitled to the moment something is live
    on the speaker (the run-7 hardware bug: an ``agc_behavioral_fail``
    during VERIFY rendered ``fix_and_retry`` and displaced the VERIFY-fail
    screen's way-back action). REASON_REGISTRY stays the single copy source —
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
    # ONE resolution point for this screen's sentence, whichever template
    # renders it below (#2085). Resolved before the template branch so no
    # branch can quietly go back to ``spec.message``.
    message = _reason_message(code, spec, status)
    if applied and spec.template != TEMPLATE_VERIFY_FAIL:
        if spec.template == TEMPLATE_SESSION_RESTART:
            # The registry copy assumes nothing was applied ("start over…");
            # this addendum keeps the household from believing nothing
            # changed when it did.
            message = f"{message} The crossover was already applied."
        return _verify_fail_envelope(code, message, status)
    template = spec.template
    if template == TEMPLATE_SILENT_AUTO_RETRY:
        # No decision screen: stay on the phase screen with a banner; the phone
        # auto-retries (§5.10 template 1).
        # ``message`` rather than ``spec.banner``: for a silent-auto-retry code
        # the selector returns exactly the banner (``message`` is empty by
        # construction), so this is identical today and stays correct if an
        # evidence-keyed code ever lands on this template.
        return _envelope(
            screen=active_step, active_step=active_step,
            verdict=message,
            nudges=[{"code": code, "severity": "info", "text": message}],
            next_action=None,
            status=status,
        )
    if template == TEMPLATE_HARD_STOP:
        # hard_stop keeps the relay block (Finding D contract): the failure
        # copy + the phone's stopped/failed status stay visible together. The
        # renderer only shows the QR for an IN-FLIGHT relay, so a purged
        # session never re-advertises a live link here.
        # Issue #1820: a hard-stop reason that knows the exact place which
        # explains it (``program_profile_not_confirmed`` → the safety-limits
        # review callout) declares that destination on its own ReasonSpec, and
        # it wins over this screen's generic "Back to speaker setup" one.
        # The registry stays the single copy source for BOTH halves — the
        # sentence and the button it points at — so the verdict text and the
        # action can never disagree about what the household should do next.
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
            advertise_relay=False,
        )
    if template == TEMPLATE_VERIFY_FAIL:
        # One default — "Try again" (internally re-verify once, then re-measure)
        # — plus the way back; the explicit trio lives behind the expert
        # disclosure (§5.2).
        return _verify_fail_envelope(code, message, status)
    # TEMPLATE_FIX_AND_RETRY (the default decision screen).
    nudges = [{"code": code, "severity": "warn", "text": message}]
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
            "relay": _mapping(status.get("relay")) or None,
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

    # Volume recovery keys on needs_recovery, NOT unresolved_volume_safety alone
    # (the W2 gate ruling — a crash-hydrated active plan surfaces no unresolved
    # payload but still needs draining).
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
        # phase/active_step (see _failure_envelope's docstring for why that
        # derivation can itself be wrong).
        applied = bool(v2.get("applied"))
        # #1942: a persisted failure used to render its terminal screen here
        # unconditionally, on every build, forever — so a plain page load the
        # next day was answered with a dead session's screen AND that
        # session's verify numbers, presented as the live verdict. Only a
        # failure that is still the household's current moment gets the
        # terminal screen; an older one is history on the entry screen. See
        # ``SESSION_FRESH_WINDOW_S`` for why the discriminator is the
        # record's own age and not a session binding. Keyed on the failure's
        # OWN stamp rather than the session clock: this screen is about the
        # moment that failure happened, and a later unrelated persist must not
        # revive it.
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

    # #1947: the same defect on every route that persists NO failure record —
    # a session walked away from, a closed browser, a reboot. Its screens are
    # frozen durable state with a live imperative on them ("put the microphone
    # back… follow the measurement page") and yesterday's curve and spec
    # numbers beside it, replayed undated on the next plain GET.
    #
    # Two phases are exempt, and neither is an oversight. PHASE_DONE is a
    # receipt about what the speaker is playing NOW, which stays exactly as
    # true a week later as it was at the close, and it asks nothing of anybody.
    # PHASE_REVIEW is untimed BY CONSTRUCTION (D3.5, see _review_envelope): the
    # candidate lives in durable state with no TTL precisely so it survives a
    # browser close and a jasper-web restart, and the entry screen has no route
    # back to it — _way_back_action resolves the candidate the last APPLY
    # displaced, never a pending one — so aging it out would silently strand a
    # whole commission's captures. #1947's case B (review replays the dead
    # session's numbers undated) therefore stays open, and wants a dated review
    # screen rather than this branch.
    #
    # ``session_id`` gates it because a box that has never measured has no
    # session to be dead: its state file does not exist, so the clock reads
    # absent for the same reason a stale one does, and dating a first-ever
    # start screen would be the opposite dishonesty to the one being fixed.
    #
    # Like the aged failure above, this does NOT fall through into the phase
    # chain: post-apply the stale state resolves to PHASE_VERIFY, and a
    # fall-through would invite the household to "confirm the result" of a
    # session that ended yesterday. Landing them on the entry screen is the
    # deliberate destination, not a side effect of where they were.
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
        # the phone (not this page) is where the per-position instructions
        # live. What changes is the verdict copy, which has to stop telling
        # someone to keep the phone still when the whole point of this phase is
        # that they move it.
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
        # because MEASURE's keep-still verdict is wrong while the household
        # MOVES the microphone, the cloud's answers a different question
        # (speaker versus room), and it must state the return to the mark or
        # the last prompt reads as a mistake.
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
        # #2291's "before" capture. Same screen and step as the three above —
        # the household is still measuring — with copy that says the two things
        # the neighbours' copy would get wrong: they are on the mark and holding
        # still, and this recording is what the tuning gets compared against
        # afterwards. The copy says "on the mark", not "BACK on the mark",
        # because it has two possible predecessors and only one of them moved
        # anybody: since the 2026-08-18 lateral pause this capture follows
        # MEASURE, where the microphone never left the mark, and "back" would
        # ask a household to undo a move it was never asked to make.
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
        # VERIFY-unaccepted-and-not-applied special case, and no shipped
        # session records a VERIFY it has not accepted — stage 1 has no VERIFY
        # index and stage 2 opens already-applied. The household-visible wait
        # while the fit runs is the ``closing`` screen; the apply itself is an
        # HTTP request that has already returned by the time anything renders.
        # A blocked/errored apply surfaces through the generic ``failure``
        # branch above (REASON_APPLY_FAILED), never here.
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
            # STAGE 2's entry point (two-stage commission D2, PR-T3). The
            # measuring session ended at the review screen and the household
            # applied from there, so the post-apply check is a NEW session
            # somebody has to start — and it must be started deliberately,
            # because the relay TTL begins ticking at open and the household
            # is still walking back to fetch the phone. ``stage:
            # "post_apply"`` selects the tier's own verify shape rather than
            # the 1-entry recovery re-arm; the tier itself comes from the
            # durable state the measuring session wrote.
            #
            # No ``show_during_relay``: while stage 2's own relay IS in flight
            # this screen renders the same copy with the action suppressed by
            # the shared relay gate, exactly as every other in-session screen
            # does — one button to start it, none to start it twice.
            next_action={
                "id": "verify_start",
                "label": "Check the result",
                "endpoint": "/correction/crossover/v2/verify",
                "body": {"stage": "post_apply"},
            },
            status=status,
            # B1 fix (adversarial review of PR #1780): the pre-apply cloud has
            # already closed by the time this screen renders (it walks BEFORE
            # VERIFY), so its before-tuning flatness/carve-out disclosure is
            # available here too, not just on the done screen — on BOTH tiers
            # since #1965 (Full's post-apply cloud has not started yet, which
            # is why it now reads the pre-apply one rather than nothing).
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
        # The RESULT screen (owner ruling, 2026-07-20): plain-language outcome
        # first — no numbers, no jargon — with the measured numbers folded
        # into the SAME collapsed "Technical details" disclosure the former
        # review screen used (_candidate_review_payload). The way back to the
        # previous tuning rides the alternate list when a prior banked
        # candidate exists.
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
            "Your speaker is tuned and confirmed at the mark. Run a Full "
            "measurement for the result checked at several spots around "
            "the mark."
            if is_express
            else "Your speaker is tuned."
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
        #
        # R19 (#2160): read the PRODUCER's spatial grade, not the cloud entry's
        # `overall_passed` — this screen used to re-derive the verdict through
        # a local `_spec_verdict`, and that key is `False` for BOTH a graded
        # miss and a spectrum no band survived to grade (`SpecFlatness.passed`
        # says so itself: "read it with evaluable"). So a household whose
        # measurement could not read the room was told its speaker "measures
        # further from flat than the target" — a number-shaped claim about a
        # measurement that never happened. `_post_apply_grade` is the one owner
        # of that distinction now (plan §7: consumers do not infer it).
        # Literal grade words, matching `grade_state` below and for the same
        # reason: `jasper.active_speaker` never imports `jasper.web`. Pinned
        # against the producer's constants by
        # tests/test_crossover_envelope_v2.py's spatial-grade contract tests.
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
            # both a miss and a check that never ran: something WAS measured
            # and it carried no readable answer, which points at the room, not
            # at a retry of a step that died.
            done_verdict = (
                "Your speaker is tuned, but the check that measures how flat "
                "it is could not read enough of the sound to say either way."
            )
        # PR-L4 item 4: applied implies graded, and when it does not, the
        # household is TOLD rather than restored behind their back. See
        # `_post_apply_grade` for why surfacing beats auto-restore here (a
        # missing grade says nothing about the correction, and express omits
        # the post-apply group by design). A failing grade already has its own
        # screen; this is the case where no check finished at all.
        elif not grade.get("graded", True):
            # Three answers, three sentences. "Never finished" is false for an
            # INCONCLUSIVE check — that one ran to completion and could not
            # decide, which is a different thing to tell someone and points at
            # a different fix (a quieter room, not a retry of a step that died)
            # — and it is false for a FAILED one in the other direction.
            grade_state = str(grade.get("state") or "")
            if grade_state == "inconclusive":
                # WHY it could not tell, from the verdict that produced the
                # outcome (issue #1974). This sentence used to assert "the room
                # reflection cut the window short" on BOTH paths that reach
                # here — including the pilot level-shift path, which involves
                # no reflection and no window at all — and even on the gate
                # path it named a mechanism nothing had checked. The clause now
                # has one writer, shared with the verify_fail screen, so the
                # two surfaces cannot drift apart again the way they did.
                #
                # An empty clause is a state file that does not record the
                # cause (written before this shipped, or a capture that could
                # not be gated). It leaves the outcome stated and the cause
                # unnamed, which is what the record supports.
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
                # The check ran, completed, and did not pass — reachable here
                # only for a state file carrying no terminal result code to
                # override this copy, and reachable BEHIND a closed post-apply
                # group since #2464 capped the grade.
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
            # #2098: the local check PASSED and it is a real result — it is
            # just not the result this tier promised. A Full session that
            # verified at the mark and never closed its post-apply group read
            # here as an unqualified "Your speaker is tuned", which is the
            # widest of the three claims on the strength of the narrowest
            # evidence. Express never reaches this branch: the mark IS its
            # whole promise, so its scope is complete (and its own copy above
            # already names both the scope and the upgrade path).
            done_verdict = (
                "Your speaker is tuned and confirmed at the mark, but the "
                "wider check across several spots has not produced a result "
                "— that part is unproven. Measure again to finish it."
            )
        result_outcome = str(grade.get("outcome") or "")
        # **A failed spatial grade caps this claim whatever the result code
        # says** (#2738, ruled 2026-08-19) — the twin of #2464's cap, one
        # surface over. ``_verify_claims`` always writes an ``integration``
        # entry, so ``result_evidence`` is true on every post-R18 session
        # whose VERIFY produced a tracking analysis — one that refuses earlier
        # (a gate refusal, the pilot-transfer level-shift arm) never reaches
        # that record — and the override below fired on all of them: the
        # 2026-08-07 jts3 shape, a group that closed FAILED at −4.63 dB, is
        # what today's code renders as "Target verified." over copy saying the
        # result reached the target, with the number reachable only inside the
        # collapsed disclosure.
        #
        # Capped HERE, where the grade is already reduced to ``spec_passed``,
        # so the copy below and ``_done_nudges`` read one capped fact instead
        # of each re-deriving the rule. ``verified_target`` is the one code
        # that claims the target was reached; the other three are NOT capped
        # and keep the copy #2605 shipped, because each already refuses that
        # claim — and ``keep_previous``'s "should not replace the previous
        # sound" is the more urgent of the two sentences, so swapping it for
        # "Your speaker is tuned, but…" would endorse a result its own grade
        # declined.
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
                # make.** Two wrong referents have now been through this
                # sentence, so the honest one is worth stating exactly: the
                # margin behind this grade is THIS candidate's linearized
                # forecast against THIS SAME candidate's un-linearized forecast
                # (``accountability`` binds ``before = grade_prediction(
                # raw_predicted_sum)``, and ``crossover_v2_flow`` hands it
                # ``analysis.predicted_sum`` — one analysis, two versions of it).
                # It is NOT a field of alternatives (the corner selector that
                # ranked one is retired, ticket 2.4) and it is NOT the
                # previously-applied graph — that is ``commanded.py``'s separate
                # instrument, which refuses outright on a corner mismatch.
                #
                # So the sentence claims only the prediction match and the miss.
                # An improvement clause would have to name a linearized-versus-
                # un-linearized forecast delta, which is not a thing a household
                # can act on; that it cannot be said meaningfully here is the
                # reason it is not said at all. ``RESULT_VERIFIED_BEST_EVALUATED``
                # keeps its name — it is banked in log events and receipts — so
                # only the household-facing sentence changes.
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
        # The two iterating rows PROMISE another round in their own copy
        # ("measuring again is how that gets closer") and, until now, offered
        # no way to take it: this screen's only other exit was Room
        # correction. A screen that names an action and does not carry it is
        # the same defect as #2641's inert Keep button, one screen over.
        #
        # It is the SAME re-measure the review screen mints — one endpoint,
        # one empty body, the tier inherited from the lapsed session — rather
        # than a second way to open a round. First in the list because on a
        # round that says another bite is coming, taking it is the recommended
        # next step; "Continue to Room correction" is the household declining
        # by moving on, which this screen has always offered.
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
        # The HEAD of ``alternate_actions`` is promoted to the primary rather
        # than a named action: the branches above already order that list by
        # recommendedness (``round_remeasure`` first on an iterating round,
        # room correction otherwise), so promoting the head inherits that
        # decision instead of making a competing one. Never empty — the room
        # action seeds it and the branches only insert/append; an edit that
        # makes room conditional owes this line a fallback.
        next_action, *alternate_actions = alternate_actions
        env = _envelope(
            screen="done", active_step="verify",
            verdict=done_verdict,
            next_action=next_action,
            alternate_actions=alternate_actions,
            # PR-L4 item 7: the badge may not claim more than the evidence.
            # "Verified." still means the tracking comparator passed, but a
            # speaker whose spec verdict FAILED gets the honest badge instead —
            # one claim per instrument, neither pretending to be the other.
            # G1's reservation (#2087) is appended at the CALL SITE rather than
            # inside ``_done_nudges``, because that composer answers "which
            # badge did the verify outcome earn" and returns nothing at all on
            # a non-pass. The reservation is about the MEASURE capture this
            # tuning was built from and is owed on this screen regardless of
            # which badge won — the household is being told the speaker is
            # tuned, which is exactly when an unstated caveat becomes an
            # overclaim.
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
            # Gauge fix (2026-07-24): the flatness numbers previously never
            # reached this screen at all — a household could watch "Your
            # speaker is tuned" (a PASS) with no visibility into how far
            # from flat the summed response actually was. Report-only, so
            # it rides the same collapsed disclosure as the integration
            # numbers would on a fail — this screen has none of those (a
            # PASS never showed verify_evidence, unchanged by this fix).
            #
            # Issue #1868 adds ONE of those numbers back, and only one: the
            # band the tracking comparator graded. The screen's badge says
            # "Verified."; the band is what bounds that word. The rest of the
            # evidence block stays off a passing screen as before — how far
            # inside tolerance a pass landed is not a claim the household
            # needs adjudicated, but how WIDE the claim is, is.
            # Rung P1 adds the second: the FRAME the comparison spanned. Same
            # argument as the band — the badge says "Verified.", and an
            # instrument tilt is the difference between "the model was right"
            # and "the model and the room disagreed smoothly and nobody said
            # so". Still report-only; the raw numbers stayed the gate.
            #
            # ``raw_already_shown=False``: this screen has no evidence block
            # (the host persists it only on a non-pass), so the frame block
            # must print the RAW pair itself. Without that, the one screen a
            # household reads as "it worked" would show the tilt-removed grade
            # alone — the smaller number, unaccompanied, which is precisely the
            # over-claim rung P1 exists to stop.
            # R9 adds the third, on the same terms: what the gate DID. A screen
            # that reports a window without reporting whether anything was
            # gated out reads as "reflections removed" to every household that
            # has ever seen one — and on the 2026-07-30 corpus that reading was
            # wrong on every capture (#1966).
            # R18 adds the fourth (#1868), and on THIS screen it matters most:
            # until now the only claim behind "Verified." was measured-vs-model,
            # which cannot fail on a null the model reproduces.
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
            # CC1: the result screen is the OTHER place a banked finding is
            # owed. It rides the durable projection rather than this session's
            # bundle deliberately — stage 2 is a different session in a
            # different bundle, so a finding the MEASURING session banked would
            # otherwise vanish exactly when the household is told the speaker
            # is tuned.
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
