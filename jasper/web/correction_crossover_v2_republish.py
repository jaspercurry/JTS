# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Select a banked candidate for the existing full apply path. Applies no graph."""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from jasper.log_event import log_event

logger = logging.getLogger(__name__)


def _refused(code: str, message: str, *, fingerprint: str = "") -> Exception:
    """Journal the refusal, then hand back the exception for the caller to raise.

    This door exists for incident recovery — an operator reaches it precisely
    when something has already gone wrong — so a refusal that reaches only the
    HTTP response is invisible in the journal, which is where they look next.
    The success paths already log; every refusal does now too, carrying the
    machine ``code`` so the four outcomes are greppable apart.

    Returns rather than raises so each call site keeps its own ``raise``
    (and its ``from exc``), which is what makes the control flow readable.
    """
    log_event(
        logger,
        "correction.crossover_v2_republish_refused",
        level=logging.WARNING,
        code=code,
        candidate_fingerprint=fingerprint,
    )
    from jasper.web import correction_crossover_v2 as _host

    return _host.CrossoverV2Refused(message, code=code)


def _admit_banked_candidate(fingerprint: str) -> tuple[str, str, Any, Any]:
    """The republish door's read-only admission: ``(code, detail, banked, change)``.

    ``code == ""`` admits; otherwise it is the refusal code the door would
    answer, with ``detail`` the door-independent half of its sentence.
    Read-only by construction — it loads the bank and the live declaration and
    writes nothing — so a caller may ask without moving the published slot.

    ONE owner for the door's admission rules, with two callers:
    :func:`handle_v2_republish` (which raises the refusal and then republishes)
    and :func:`republish_preflight` (the answer half — the status block and the
    round's ``rollback_available`` ask it so no surface advertises a way back
    this door would refuse; #2291's answer/action non-drift rule).
    """
    from jasper.active_speaker.candidate_bank import (
        CandidateBankRefusal,
        find_banked_candidate,
    )
    from jasper.active_speaker.candidate_trials import require_candidate_trial
    from jasper.active_speaker.crossover_declaration import (
        declaration_change_for_candidate,
    )
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.output_topology import load_output_topology
    from jasper.web import correction_crossover_v2 as _host

    if not str(fingerprint or "").strip():
        return "fingerprint_required", "fingerprint is required", None, None
    try:
        banked = find_banked_candidate(str(fingerprint).strip())
        require_candidate_trial(banked.candidate)
    except CandidateBankRefusal as exc:
        return (
            exc.code,
            f"that candidate cannot be republished: {exc.detail}",
            None,
            None,
        )
    # The one companion fact the bundle cannot supply (see
    # :func:`handle_v2_republish`'s docstring). Read through the SAME helper
    # the apply path branches on, so the two agree by construction rather
    # than by a restated rule.
    change = declaration_change_for_candidate(
        source_preset=banked.candidate.source_preset,
        design_draft=load_design_draft(topology=load_output_topology()),
    )
    if change is not None:
        # BOTH numbers, because the operator's next question is always "which
        # one is wrong" — a refusal naming only the candidate's corner makes
        # them go look up the declaration by hand.
        return (
            "sound_design_revision_unavailable",
            f"that candidate crosses at "
            f"{_host._crossover_label(change.selected, change.changes_slope)} but "
            f"Sound declares "
            f"{_host._crossover_label(change.configured, change.changes_slope)}; "
            "applying it would rewrite the declaration, and the Sound revision "
            "its measurement was taken under (sound_design_revision) is not "
            "recorded in the bundle. Measure this crossover again to apply it.",
            banked,
            change,
        )
    return "", "", banked, change


def republish_preflight(fingerprint: str) -> str | None:
    """The refusal code this door would answer for ``fingerprint``, or ``None``.

    The ANSWER half of the way back, asked before anything advertises the
    ACTION: the status block's ``previous_candidate_fingerprint`` (the
    wizard's way-back button) and the round's ``rollback_available`` both gate
    on it, so neither can promise a republish the door then refuses — the
    same drift #2291 closed for the old restore verb. A pointer is still
    never a promise: the door re-runs the same admission on POST, because the
    bank can change between the answer and the action.
    """
    code, _detail, _banked, _change = _admit_banked_candidate(fingerprint)
    return code or None


def handle_v2_republish(
    raw: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """Publish a candidate with coherent review identity and cleared verify priors.

    Authored parts need an intact capture of the exact child fingerprint. A
    changed Sound declaration also needs the original revision, which the bank
    cannot reconstruct. Apply still reads live hardware and graph protections.
    Host-owned restore pointers survive this session-state replacement.
    """
    from jasper.web import correction_crossover_v2 as _host
    from jasper.active_speaker.crossover_v2.coordinator import (
        ROUND_ORDINAL_EPOCH_STATE_KEY,
        round_ordinal_epoch_from_state,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE

    fingerprint = str(raw.get("fingerprint") or "").strip()
    code, detail, banked, _change = _admit_banked_candidate(fingerprint)
    if code:
        raise _refused(code, detail, fingerprint=fingerprint)

    # Lazy for ``_candidate_summary``'s own stated reason: the fit module pulls
    # numpy and this one is on the socket-activated wizard's cold-start path.
    from jasper.active_speaker.linearization_fit import HEADROOM_COST_BASIS_UNKNOWN

    summary = _host._candidate_summary(
        banked.candidate,
        # A republish did not MEASURE this corner — an operator named it by
        # fingerprint. ``_candidate_summary``'s own rule is that the household
        # copy must never word a pinned corner as a measured result, and the
        # minting session's pin bit is not on the artifact, so the direction
        # that cannot overclaim is the one to take.
        topology_pinned=True,
        # The SAME direction, for the same reason, about the headroom era. This
        # candidate was read off disk and may predate #2758's grid widening,
        # under which the identical filters charge MORE than the number it
        # carries — so a current-era label here would under-disclose the cost of
        # a correction the household is about to be offered. The artifact
        # records no era, and ``unknown`` is what that absence honestly means;
        # the renderer already has a sentence for it.
        headroom_cost_basis=HEADROOM_COST_BASIS_UNKNOWN,
    )
    with _host._state_lock:
        prior = _host.load_v2_state() or {}
        # The ordinal sequence RESTARTS here, and until now it did so with no
        # trace. The whole-dict replacement below drops ``round_receipt``, and
        # ``coordinator.series_position_from_state`` reads that receipt for the
        # previous ordinal — so the next round is round 1 again, indistinguishable
        # from the first round of a box that has never measured. This is the
        # disclosure, written at the line that causes it: the epoch counts the
        # resets, and ``reset_round_ordinal_from`` names the ordinal the dropped
        # receipt had reached, which is the number that stops existing.
        #
        # Disclosure, never a block: nothing gates on either value. The
        # republish is an incident-recovery door and a reset that refused would
        # be worse than a reset that admits itself.
        epoch = round_ordinal_epoch_from_state(prior) + 1
        prior_receipt = prior.get("round_receipt")
        reset_from = (
            prior_receipt.get("round_ordinal")
            if isinstance(prior_receipt, Mapping)
            else None
        )
        republished = {
            "candidate_fingerprint": banked.fingerprint,
            "bundle_session_id": banked.bundle_session_id,
            "session_id": banked.capture_session_id,
            "at": time.time() if now is None else float(now),
            "round_ordinal_epoch": epoch,
            # ``None`` when the state carried no readable receipt — a box that
            # had banked no round, or a record this reader cannot vouch for.
            # Both mean "there was no count to lose", which is a different fact
            # from losing one and is worth keeping apart.
            "reset_round_ordinal_from": (
                reset_from
                if isinstance(reset_from, int) and not isinstance(reset_from, bool)
                else None
            ),
        }
        # Whole-dict replacement, exactly like ``reset_v2_journey_state``: every
        # session-scoped fact of the round that MINTED this candidate — cloud
        # verdict, tier, gain plan, receipt, a stale review decline — belongs to
        # a session this is not, so dropping it is the honest default and
        # listing what survives is the reviewable shape.
        state = {
            "session_id": banked.capture_session_id,
            "accepted_phases": [PHASE_MEASURE],
            "session_phases": [PHASE_MEASURE],
            "applied": False,
            "candidate": summary,
            "evidence": {"bundle_session_id": banked.bundle_session_id},
            "gain_plan_db": None,
            "verify": None,
            "verify_priors": None,
            "failure": None,
            "apply_blocked": None,
            "accepted_sound_revision": None,
            "accepted_sound_declaration_change": None,
            "sound_design_revision": None,
            "republished": republished,
            # Top-level and not only inside ``republished`` above, because this
            # is the key that has to OUTLIVE this block: ``persist_conductor_state``
            # rebuilds the state dict on the next round and carries this one
            # forward by name, while ``republished`` — session-scoped like every
            # other fact of the session this door mints — does not survive it.
            ROUND_ORDINAL_EPOCH_STATE_KEY: epoch,
            # Preserved on exactly ``reset_v2_journey_state``'s terms, and for
            # its reason rather than a stronger-sounding one: these are the
            # HOST-OWNED apply keys — values only ``observe_apply_success``
            # produces, which no state rebuild can regenerate. Erasing that
            # class on a rebuild is a bug that has shipped three times
            # (``test_every_host_owned_apply_key_survives_persist_conductor_state``
            # is the guard), so a whole-dict replacement must carry them. What
            # preservation buys is that the way back of the graph still
            # playing survives a republish the operator never applies.
            "attempts_loop": (
                dict(prior["attempts_loop"])
                if isinstance(prior.get("attempts_loop"), Mapping)
                else None
            ),
            "previous_candidate_fingerprint": (
                prior.get("previous_candidate_fingerprint")
                if isinstance(prior.get("previous_candidate_fingerprint"), str)
                and prior.get("previous_candidate_fingerprint")
                else None
            ),
            # The pointer's pairing crosses on the same terms. It names the
            # apply that recorded the pointer — NOT this republish — so a
            # round graded after this door's own apply compares it against
            # a fresh stamp anyway; carrying it is what keeps a republish
            # the operator never applies from silently unpairing the way
            # back.
            "previous_candidate_displaced_by": (
                prior.get("previous_candidate_displaced_by")
                if isinstance(prior.get("previous_candidate_displaced_by"), str)
                and prior.get("previous_candidate_displaced_by")
                else None
            ),
        }
        # Not durable, on ``save_v2_state``'s own rule: this write creates no
        # rollback anchor and falsifies no receipt, and losing it leaves the
        # PREVIOUS state with the anchor still intact — the same reasoning
        # ``reset_v2_journey_state`` carries.
        _host.save_v2_state(state)
    log_event(
        logger,
        "correction.crossover_v2_candidate_republished",
        candidate_fingerprint=banked.fingerprint,
        bundle_session_id=banked.bundle_session_id,
        capture_session_id=banked.capture_session_id,
    )
    # The ordinal reset gets its own line, on this module's own standard (see
    # ``_refused``): an operator reaches this door when something has already
    # gone wrong, and the journal is where they look next. A sequence reset
    # that only ever reached the HTTP response is invisible there.
    log_event(
        logger,
        "correction.crossover_v2_round_ordinal_epoch_advanced",
        round_ordinal_epoch=epoch,
        reset_round_ordinal_from=republished["reset_round_ordinal_from"],
    )
    return {
        "status": "republished",
        "republished": republished,
        "candidate": summary,
        # What the operator does next, and the one thing that is NOT restored.
        "next_action": {
            "endpoint": "/sound/speaker/crossover/v2/apply",
            "expected_candidate_fingerprint": banked.fingerprint,
        },
        "verify_priors_restored": False,
    }
