# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: republishing a BANKED measured candidate so apply can reach it.

The v2 apply door is single-slot — it matches the fingerprint it is handed
against ``state["candidate"]`` and nothing else — and that slot is rewritten by
every measure session and cleared by a failed one. The candidates are durable
in the commissioning bundles the whole time, so "no candidate is published" is
a state problem with three good candidates sitting on disk. These tests pin the
door that closes that gap, and the two things it must never become:

  * **A gate bypass.** Republishing changes WHICH candidate is live. Every
    admission check the apply path runs reads live SSOT, so no state write can
    satisfy one — the test here pins the *reachability* half (apply's own
    state-vs-state gates all pass) and the drift guard below pins that apply
    has not grown a state read the republish leaves unset.
  * **A way to launder a corrupted artifact.** Integrity is the candidate
    model's own recompute-and-compare; a single edited byte must refuse.
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2 import coordinator
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_republish as republish

from tests.test_active_speaker_measured_crossover_candidate import _candidate

BUNDLE = "bundle0000aa"
RELAY = "relay-session-1"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    # No design draft => `declared_crossover_geometry` says nothing, so the
    # candidate needs no /sound write. That is the ordinary shape (a candidate
    # measured at the crossover Sound already declares); the alternative-Fc
    # shape gets its own test below.
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft", lambda *a, **k: {}
    )
    yield
    v2host.set_state_path_for_tests(None)


@pytest.fixture
def bank(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(
        "jasper.active_speaker.bundles.sessions_dir", lambda: root
    )
    return root


def _publish(root: Path, candidate, *, bundle=BUNDLE, relay=RELAY) -> Path:
    path = (
        root / bundle / "evidence" / "v1" / "artifacts"
        / "crossover_v2" / relay / "candidate.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate.to_dict()), encoding="utf-8")
    return path


# --- the round trip: republish, then apply can reach it ---------------------


def test_republish_then_apply_reaches_the_banked_candidate(bank):
    """The whole point: a banked candidate becomes applyable by fingerprint.

    Asserted at apply's OWN gates rather than by re-reading the state we just
    wrote — the state is only correct if the apply path accepts it:

      * the fingerprint gate (``state["candidate"]["fingerprint"]``),
      * the artifact reopen, which rebuilds a filesystem path out of
        ``session_id`` + ``evidence.bundle_session_id`` and so proves the
        MINTING lineage was published, not a fresh id,
      * ``_update_current_review`` — the compare-and-set that admits
        ``observe_apply_success``. Without it the graph would go live while the
        state never recorded the apply, leaving Undo unreachable.
    """
    candidate = _candidate()
    _publish(bank, candidate)

    result = republish.handle_v2_republish({"fingerprint": candidate.fingerprint})
    assert result["status"] == "republished"
    assert result["republished"]["candidate_fingerprint"] == candidate.fingerprint

    state = v2host.load_v2_state()
    assert state["candidate"]["fingerprint"] == candidate.fingerprint
    assert state["session_id"] == RELAY
    assert state["evidence"]["bundle_session_id"] == BUNDLE

    reopened = v2host._reopen_candidate_artifact(state, state["evidence"])
    assert reopened["fingerprint"] == candidate.fingerprint

    assert v2host._update_current_review(RELAY, candidate.fingerprint, None, {})


def test_a_republished_candidate_does_not_wear_a_current_headroom_era(bank):
    """A candidate read OFF DISK has no recorded era, and may predate #2758.

    Its per-branch charges were stamped under whatever grid its minting build
    evaluated on, and the widened grid charges MORE for some of those same
    filters — so a current-era label here would under-disclose the cost of a
    correction the household is about to be offered, with a stamp saying the
    number is current. Nothing on ``MeasuredCrossoverCandidate`` records an era
    (that is what "recorded, never inferred" means), so ``unknown`` is the only
    honest answer, and the renderer already has a sentence for it.

    Asserted against the MINTING stamp too: if both said the same thing the
    field would be decoration.
    """
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
        HEADROOM_COST_BASIS_UNKNOWN,
    )

    candidate = _candidate()
    _publish(bank, candidate)

    republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    summary = v2host.load_v2_state()["candidate"]
    assert summary["headroom_cost_basis"] == HEADROOM_COST_BASIS_UNKNOWN
    assert v2host._candidate_summary(candidate)["headroom_cost_basis"] == (
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN
    ), "the minting path still stamps its own era, or this pin means nothing"


def test_republish_names_the_apply_endpoint_and_discloses_verify_is_not_restored(bank):
    """VERIFY priors belong to the round that ran the fit and cannot be rebuilt.

    They are CLEARED rather than inherited: another round's predicted sum and
    entry baseline would grade this candidate against a different one's
    prediction. The response says so, because a silently degraded VERIFY is
    exactly the false comparison the priors exist to prevent.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    v2host.save_v2_state({"verify_priors": {"predicted_sum": {"stale": True}}})

    result = republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert result["verify_priors_restored"] is False
    assert result["next_action"] == {
        "endpoint": "/correction/crossover/v2/apply",
        "expected_candidate_fingerprint": candidate.fingerprint,
    }
    assert v2host.load_v2_state()["verify_priors"] is None


def test_republish_preserves_the_way_back_of_the_playing_graph(bank):
    """A republish moves a pointer; it changes no graph.

    So the host-owned record of what IS playing — and the way back from it —
    must survive, exactly as it survives ``reset_v2_journey_state``. Dropping
    it would leave a corrected speaker with no way back.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    v2host.save_v2_state({
        "applied": True,
        "previous_candidate_fingerprint": "fp-before",
        "attempts_loop": {"history": [{"attempt": 1}]},
    })

    republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    state = v2host.load_v2_state()
    assert state["previous_candidate_fingerprint"] == "fp-before"
    assert state["attempts_loop"] == {"history": [{"attempt": 1}]}
    # ...while `applied` returns to false: this candidate is not applied, and
    # `_update_current_review`'s non-`allow_applied` calls refuse a state that
    # still claims it is. That pairing (applied false, anchor kept) is what a
    # fresh measuring session persists over a previously-applied graph.
    assert state["applied"] is False


def test_republish_clears_another_candidates_sound_accept_breadcrumb(bank):
    """A stale accepted-Sound pair would make apply skip its own Sound save.

    The pair is the retry breadcrumb of one candidate's accept. Left standing
    across a republish it tells the next apply "Sound already declares this
    candidate's crossover" when Sound declares the OTHER one's.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    v2host.save_v2_state({
        "accepted_sound_revision": 12,
        "accepted_sound_declaration_change": {"applied_hz": 2500.0},
        "sound_design_revision": 12,
    })

    republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    state = v2host.load_v2_state()
    assert state["accepted_sound_revision"] is None
    assert state["accepted_sound_declaration_change"] is None
    assert state["sound_design_revision"] is None


# --- integrity: fail closed -------------------------------------------------


def test_one_corrupted_byte_refuses_rather_than_republishing(bank):
    """The mutation check on the integrity verification.

    A single edited character in the stored candidate changes what its own
    fingerprint recomputes to, so ``from_mapping`` refuses ``candidate_tampered``
    and the artifact never becomes applyable. Both halves are asserted: the
    republish refuses, AND the state's candidate slot is left untouched — a
    refusal that had already overwritten the slot would be its own incident.
    """
    candidate = _candidate()
    path = _publish(bank, candidate)
    v2host.save_v2_state({"candidate": {"fingerprint": "incumbent"}})

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["program_id"] = raw["program_id"][:-1] + "X"  # one byte, same length
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(v2host.CrossoverV2Refused):
        republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert v2host.load_v2_state()["candidate"] == {"fingerprint": "incumbent"}


def test_a_corrupted_artifact_is_reported_as_unverifiable_not_merely_absent(bank):
    """"Not found" would send an operator hunting for a file that is right there."""
    candidate = _candidate()
    path = _publish(bank, candidate)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert "could not be verified" in str(excinfo.value)


def test_unknown_fingerprint_refuses(bank):
    _publish(bank, _candidate())

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        republish.handle_v2_republish({"fingerprint": "a" * 64})

    assert "no banked candidate matches" in str(excinfo.value)


def test_empty_bundle_store_refuses(bank):
    with pytest.raises(v2host.CrossoverV2Refused):
        republish.handle_v2_republish({"fingerprint": "a" * 64})


def test_missing_fingerprint_refuses(bank):
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        republish.handle_v2_republish({})

    assert "fingerprint is required" in str(excinfo.value)


def test_two_minting_lineages_for_one_fingerprint_refuse_as_ambiguous(bank):
    """Identical cores hash identically — but their lineage is what gets published.

    Publishing one of them would credit a round chosen by directory sort order,
    and lineage is the path the apply door rebuilds to find the artifact again.
    """
    candidate = _candidate()
    _publish(bank, candidate, bundle="bundle0000aa", relay="relay-1")
    _publish(bank, candidate, bundle="bundle0000bb", relay="relay-2")

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert "claim this candidate fingerprint" in str(excinfo.value)


def test_the_same_artifact_seen_once_is_not_ambiguous(bank):
    """The ambiguity rule keys on LINEAGE, not on how many files were scanned."""
    candidate = _candidate()
    _publish(bank, candidate)
    _publish(bank, _candidate(trims={"woofer": -1.0, "tweeter": -4.0}),
             bundle="bundle0000bb", relay="relay-2")

    result = republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert result["republished"]["session_id"] == RELAY


# --- the fact a bundle cannot reconstruct, named rather than guessed --------


def test_a_candidate_needing_a_sound_write_refuses_and_names_the_missing_fact(
    bank, monkeypatch
):
    """``sound_design_revision`` is a property of the minting SESSION, not the artifact.

    Apply needs it as a compare-and-set expectation on the one path that
    rewrites the ``/sound`` declaration. Nothing in the bundle carries it, so
    this refuses up front and says which fact is missing — rather than
    inventing a revision number, or letting the operator discover it as an
    opaque refusal several steps later.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    region = candidate.source_preset.crossover_regions[0]
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft",
        lambda *a, **k: {
            "manual_settings": {
                "crossover_candidates": [{
                    "between_roles": ["woofer", "tweeter"],
                    # A DIFFERENT corner from the one the candidate carries, so
                    # applying it would have to rewrite the declaration.
                    "frequency_hz": float(region.fc_hz) * 2.0,
                    "slope_db_per_octave": 24.0,
                    "filter_type": "LinkwitzRiley",
                }]
            }
        },
    )

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert "sound_design_revision" in str(excinfo.value)
    assert excinfo.value.code == "sound_design_revision_unavailable"
    # Both corners named: the operator's next question is "which one is wrong".
    assert f"{float(region.fc_hz) * 2.0:.0f} Hz" in str(excinfo.value)
    assert f"{float(region.fc_hz):.0f} Hz" in str(excinfo.value)
    # …and it refused BEFORE writing anything.
    assert v2host.load_v2_state() is None


# --- observability ----------------------------------------------------------


def test_republish_emits_its_event_with_fingerprint_and_source_bundle(bank, caplog):
    candidate = _candidate()
    _publish(bank, candidate)

    with caplog.at_level("INFO"):
        republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    line = next(
        r.getMessage() for r in caplog.records
        if "correction.crossover_v2_candidate_republished" in r.getMessage()
    )
    assert f"candidate_fingerprint={candidate.fingerprint}" in line
    assert f"bundle_session_id={BUNDLE}" in line
    assert f"relay_session_id={RELAY}" in line


@pytest.mark.parametrize(
    "setup, payload, expected_code",
    [
        (lambda root: None, {}, "fingerprint_required"),
        (lambda root: None, {"fingerprint": "a" * 64}, "not_found"),
        (
            lambda root: (
                _publish(root, _candidate(), bundle="b1", relay="r1"),
                _publish(root, _candidate(), bundle="b2", relay="r2"),
            ),
            None,
            "ambiguous",
        ),
    ],
)
def test_every_refusal_reaches_the_journal(bank, caplog, setup, payload, expected_code):
    """A door reached during an incident must not refuse silently.

    The operator arrives here BECAUSE something already went wrong, so a
    refusal that lands only in the HTTP response is invisible in the place they
    look next. Each outcome carries its machine ``code`` so the refusals are
    greppable apart rather than one undifferentiated line.
    """
    setup(bank)
    body = payload if payload is not None else {
        "fingerprint": _candidate().fingerprint
    }

    with caplog.at_level("WARNING"):
        with pytest.raises(v2host.CrossoverV2Refused):
            republish.handle_v2_republish(body)

    line = next(
        r.getMessage() for r in caplog.records
        if "correction.crossover_v2_republish_refused" in r.getMessage()
    )
    assert f"code={expected_code}" in line


def test_republish_stamps_the_callers_time_not_a_clock_read(bank):
    candidate = _candidate()
    _publish(bank, candidate)

    result = republish.handle_v2_republish(
        {"fingerprint": candidate.fingerprint}, now=1234.5
    )

    assert result["republished"]["at"] == 1234.5
    assert v2host.load_v2_state()["republished"]["at"] == 1234.5


# --- the drift guard on requirement 1 ---------------------------------------


def _apply_state_reads() -> set[str]:
    """Every durable-state key ``handle_v2_apply`` reads, from its own source.

    Derived mechanically rather than listed, for the reason every guard in this
    repo is: a seventh read added to ``handle_v2_apply``'s OWN BODY must fail
    this test on the day it lands, not be noticed the next time someone reads
    both functions side by side. That body loads state exactly once (``state =
    load_v2_state()``) and then reads it by key, so the key set is greppable.

    **Scoped to that body, and no further.** This parses one function; it does
    NOT see what apply's callees read. The two state facts apply consumes
    through :func:`_update_current_review` — ``accepted_phases`` and
    ``applied`` — are therefore invisible here, and are covered BEHAVIOURALLY
    instead by ``test_republish_then_apply_reaches_the_banked_candidate``,
    which drives that compare-and-set for real. A new condition added to the
    CAS fails that test, not this one. Two guards, two mechanisms, on purpose:
    do not "fix" this one by teaching it to follow calls.
    """
    source = textwrap.dedent(_function_source("handle_v2_apply"))
    # Three spellings, because a guard that only knows the CURRENT one degrades
    # silently: a read added as ``state["x"]`` would leave this reporting a
    # smaller set and the assertion below trivially true.
    return (
        set(re.findall(r'\(state or \{\}\)\.get\("([a-z_]+)"', source))
        | set(re.findall(r'\bstate\.get\("([a-z_]+)"', source))
        | set(re.findall(r'\bstate\["([a-z_]+)"\]', source))
    )


def _function_source(name: str) -> str:
    import inspect

    return inspect.getsource(getattr(v2host, name))


def test_republish_satisfies_every_state_key_the_apply_path_reads(bank):
    """Requirement: indistinguishable-in-contract from a fresh mint's publish.

    Not "the fields we remembered" — the fields apply actually reads. Each one
    must be PRESENT in the republished state (an explicit ``None`` counts: that
    is a decision, and several of these keys must be null for apply's own
    compare-and-set to admit the write). A key apply reads and republish never
    writes would inherit whatever the previous session left there.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    republish.handle_v2_republish({"fingerprint": candidate.fingerprint})
    state = v2host.load_v2_state()

    reads = _apply_state_reads()
    # Sanity: the extractor found the reads we know are there. A regex that
    # silently matched nothing would make this whole guard vacuous.
    assert {"session_id", "candidate", "evidence"} <= reads

    missing = sorted(key for key in reads if key not in state)
    assert not missing, (
        f"handle_v2_apply reads {missing} from durable state, but "
        "handle_v2_republish does not write it. Either publish the key or "
        "refuse the republish naming it as unreconstructable."
    )


def test_the_route_dispatches_into_the_handler(monkeypatch):
    """The wiring, not just the guard.

    ``test_known_post_routes_reach_csrf_guard`` proves the path is registered
    and CSRF-protected, but it stops AT the guard — a route in the allowlist
    with no dispatch branch would still pass it and then fall through to the
    wrong handler. This drives past the guard and asserts the refusal that only
    ``handle_v2_republish`` produces.
    """
    from jasper.web import correction_setup

    from tests.test_web_correction_setup import _drive

    monkeypatch.setattr(
        correction_setup, "guard_mutating_request", lambda handler: True
    )
    resp = _drive("/crossover/v2/republish", method="POST", body=b"{}")

    assert b"400" in resp.split(b"\r\n", 1)[0]
    assert b"fingerprint is required" in resp


# --- the ordinal reset, disclosed rather than silent ------------------------


def _round_receipt_state(ordinal: int) -> None:
    """Put a banked round's series memory on disk, as a graded round leaves it.

    ``round_receipt`` is the ONE key the whole disclosure turns on: it is where
    ``coordinator.series_position_from_state`` reads the previous ordinal from,
    and it is what both reset doors drop.
    """
    state = dict(v2host.load_v2_state() or {})
    state["round_receipt"] = {"round_ordinal": ordinal}
    v2host.save_v2_state(state)


def test_a_republish_restarts_the_ordinal_sequence_and_says_so(bank):
    """The mechanism, and the disclosure that makes it readable.

    A republish replaces durable state wholesale and never re-includes
    ``round_receipt``, so the very next round resolves to ordinal 1 — on a
    speaker that has already been through three. Both halves are asserted: the
    reset is REAL (the reader still says 1), and it is no longer silent (the
    epoch moved, and the ordinal that stopped existing is named).
    """
    candidate = _candidate()
    _publish(bank, candidate)
    _round_receipt_state(3)

    result = republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert result["republished"]["round_ordinal_epoch"] == 1
    assert result["republished"]["reset_round_ordinal_from"] == 3

    state = v2host.load_v2_state()
    position = coordinator.series_position_from_state(state)
    # The reset itself is unchanged — this PR discloses, it does not block.
    assert position.ordinal == 1
    # ...and the epoch is what tells this apart from a fresh box's round 1.
    assert position.ordinal_epoch == 1


def test_a_fresh_boxs_first_round_and_a_republished_ones_are_told_apart(bank):
    """The whole point, stated as the comparison an operator actually makes.

    Both series positions say ordinal 1. Before the epoch they were equal
    records; a reader could not tell a box that had never measured from one
    whose count was reset out from under it.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    _round_receipt_state(2)
    republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    after_republish = coordinator.series_position_from_state(v2host.load_v2_state())
    fresh_box = coordinator.series_position_from_state({})

    assert after_republish.ordinal == fresh_box.ordinal == 1
    assert after_republish.ordinal_epoch != fresh_box.ordinal_epoch
    assert fresh_box.ordinal_epoch == 0


def test_each_republish_advances_the_epoch(bank):
    """A counter, not a flag: two resets are a different history from one."""
    candidate = _candidate()
    _publish(bank, candidate)

    first = republish.handle_v2_republish({"fingerprint": candidate.fingerprint})
    second = republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert first["republished"]["round_ordinal_epoch"] == 1
    assert second["republished"]["round_ordinal_epoch"] == 2
    assert coordinator.round_ordinal_epoch_from_state(v2host.load_v2_state()) == 2


def test_a_republish_with_no_count_to_lose_says_that_rather_than_zero(bank):
    """"There was no ordinal" and "the ordinal was 0" are different facts.

    A box that banked no round has nothing to reset. The epoch still moves —
    the door ran — but ``reset_round_ordinal_from`` must not fabricate a number
    for a count that never existed.
    """
    candidate = _candidate()
    _publish(bank, candidate)

    result = republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert result["republished"]["reset_round_ordinal_from"] is None
    assert result["republished"]["round_ordinal_epoch"] == 1


def test_start_over_resets_the_same_sequence_and_takes_the_same_epoch(bank):
    """The second door with the identical omission (#B5).

    ``reset_v2_journey_state``'s applied branch drops ``round_receipt`` while
    the applied graph keeps playing — the same reset, on a speaker that has
    already been tuned. An epoch that counted only the republish would make
    ``0`` mean "never reset" on one path and "reset by the other door" on the
    other, which is the ambiguity the field exists to remove.
    """
    v2host.save_v2_state({
        "applied": True,
        "round_receipt": {"round_ordinal": 2},
        "previous_candidate_fingerprint": "fp-prev",
    })

    v2host.reset_v2_journey_state()

    position = coordinator.series_position_from_state(v2host.load_v2_state())
    assert position.ordinal == 1
    assert position.ordinal_epoch == 1


def test_an_unmeasured_start_over_is_a_fresh_box_not_a_reset(bank):
    """The not-applied branch is a full clear, and 0 is the honest answer.

    Nothing measured is left on the speaker, so a count restarting at 1 there
    is not a reset to disclose — it is what the box actually is. Pinned so the
    epoch cannot drift into "how many times has anything been cleared".
    """
    v2host.save_v2_state({"applied": False, "round_receipt": {"round_ordinal": 2}})

    v2host.reset_v2_journey_state()

    position = coordinator.series_position_from_state(v2host.load_v2_state())
    assert position.ordinal == 1
    assert position.ordinal_epoch == 0


def test_the_epoch_survives_the_next_rounds_persist(bank):
    """The disclosure has to outlive the session the reset door minted.

    ``persist_conductor_state`` rebuilds the state dict from scratch on every
    persist. A key it does not carry forward by name is gone on the first
    persist after the republish — which is exactly the round the epoch exists
    to label, so a session-scoped marker would be worse than none.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    republish.handle_v2_republish({"fingerprint": candidate.fingerprint})

    assert coordinator.round_ordinal_epoch_from_state(v2host.load_v2_state()) == 1

    # The REAL persist, through the host, under a brand-new session id — the
    # rebind that erased three host-owned keys before carry-forward lines
    # existed for them.
    from tests.test_correction_crossover_v2_endpoints import _StubConductor

    v2host.persist_conductor_state(_StubConductor("s-after"), failure_code=None)

    after = v2host.load_v2_state()
    assert after["session_id"] == "s-after"
    assert coordinator.round_ordinal_epoch_from_state(after) == 1


def test_start_over_after_a_republish_does_not_destroy_the_epoch(bank):
    """The clear_v2_state hole, pinned at the sequence a household walks.

    ``applied`` is not "is a measured graph playing": the republish door sets
    it ``False`` while the graph it published keeps playing, and concedes as
    much in its own comment. So Start-Over then takes the NOT-applied branch,
    whose justification — nothing measured is left on the speaker — is false
    here. Unlinking the state file there destroys the epoch, and the next round
    reads "round 1, epoch 0": a fresh box, on a speaker that has already been
    tuned AND already had its count reset.
    """
    candidate = _candidate()
    _publish(bank, candidate)
    _round_receipt_state(3)
    republish.handle_v2_republish({"fingerprint": candidate.fingerprint})
    assert coordinator.round_ordinal_epoch_from_state(v2host.load_v2_state()) == 1
    # The precondition the hole depends on, asserted rather than assumed.
    assert v2host.load_v2_state()["applied"] is False

    v2host.reset_v2_journey_state()

    position = coordinator.series_position_from_state(v2host.load_v2_state())
    assert position.ordinal == 1
    assert position.ordinal_epoch == 1, (
        "the reset marker must survive Start-Over; a disclosure any later "
        "path can erase is not a disclosure"
    )


def test_a_start_over_with_no_graded_round_does_not_invent_a_reset(bank):
    """The other direction: over-disclosure is the same bug, mirrored.

    ``applied`` can be ``True`` with no round ever graded — an apply whose
    VERIFY never landed leaves no ``round_receipt``. There is nothing to drop,
    so nothing was reset, and repeated Start-Over taps must not inflate the
    count into resets that never happened.
    """
    v2host.save_v2_state({
        "applied": True,
        "previous_candidate_fingerprint": "fp-prev",
    })

    v2host.reset_v2_journey_state()
    after_one = coordinator.round_ordinal_epoch_from_state(v2host.load_v2_state())
    v2host.reset_v2_journey_state()
    v2host.reset_v2_journey_state()
    after_three = coordinator.round_ordinal_epoch_from_state(v2host.load_v2_state())

    assert after_one == 0
    assert after_three == 0


def test_start_over_banks_the_ordinal_it_reset_from(bank):
    """Symmetric disclosure with the republish door: None-vs-count.

    A dropped receipt names the ordinal that stops existing; the increment and
    that number travel together, so a reader is never told the count moved
    without being told what it moved from.
    """
    v2host.save_v2_state({
        "applied": True,
        "round_receipt": {"round_ordinal": 4},
        "previous_candidate_fingerprint": "fp-prev",
    })

    v2host.reset_v2_journey_state()

    state = v2host.load_v2_state()
    assert coordinator.round_ordinal_epoch_from_state(state) == 1
    assert state.get("round_receipt") is None


# --- the wizard's way back rides this door -----------------------------------


def test_the_wizard_way_back_action_round_trips_through_this_door(
    bank, monkeypatch, tmp_path
):
    """The real seam a household's tap travels: save_v2_state ->
    crossover_v2_status_block -> build_crossover_envelope_v2 -> POST the
    minted body. The done screen's way-back action carries the fingerprint
    the pre-apply stash recorded, and that exact body must republish the
    banked candidate — one test over the whole route, so a renamed key at
    any layer fails here instead of shipping a dead button.
    """
    from types import SimpleNamespace

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.active_speaker.crossover_v2.journey import (
        PHASE_CHECK,
        PHASE_MEASURE,
        PHASE_VERIFY,
    )
    from jasper.web import correction_crossover_v2_status as v2status

    previous = _candidate()
    _publish(bank, previous)
    monkeypatch.setattr(
        v2host, "session_volume_plan",
        lambda: SimpleNamespace(needs_recovery=False),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MODEL_ERROR_PATH",
        str(tmp_path / "model_error.json"),
    )
    v2host.save_v2_state({
        "session_id": "cap_current",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify": {"outcome": "pass"},
        "previous_candidate_fingerprint": previous.fingerprint,
    })

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2status.crossover_v2_status_block(),
    })
    assert env["screen"] == "done"
    way_back = next(
        a for a in env["alternate_actions"] if a["id"] == "republish_previous"
    )
    assert way_back["body"] == {"fingerprint": previous.fingerprint}

    result = republish.handle_v2_republish(way_back["body"])

    assert result["status"] == "republished"
    assert (
        v2host.load_v2_state()["candidate"]["fingerprint"] == previous.fingerprint
    )
