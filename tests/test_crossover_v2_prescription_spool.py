# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A9 — an accepted prescription's route into a live round.

The finding these pin: on 2026-08-19 the prescriber harness produced a
validated, provenance-stamped cut and it could not be applied, because nothing
consumed :func:`read_blend_prescription`'s output. The flow's blend reader took
instructions from the prior round's receipt or the graph already playing, and
neither is reachable from ``propose``.

So the properties worth pinning are not "the gate works" — that is
``tests/test_crossover_v2_blend_prescription.py``'s four review rounds — but the
DOOR: that a staged document reaches the candidate through the same single
entry every banked instruction uses, that it is re-validated rather than
trusted, that it runs once, and that an Undo withdraws it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.active_speaker.crossover_v2.blend_prescription import (
    BLEND_CANDIDATE_FIELD,
    PRESCRIPTION_REFUSAL_REASONS,
    BlendPrescriptionRefused,
    prescription_sha256,
    read_blend_prescription,
    read_prescription_bytes,
)
from jasper.cli import crossover_prescriber as cli
from jasper.web import correction_crossover_v2 as v2host

# The stage-bridge harness — one definition of "what a real preparer needs
# stubbed". The autouse fixtures are re-exported under the redundant-alias form
# for the reason ``test_crossover_v2_round_wiring`` states: pytest activates an
# autouse fixture by its presence in this namespace, and nothing here calls one.
from tests.test_crossover_v2_stage_bridge import (
    _isolated_v2_state as _isolated_v2_state,
    _open_prepared,
    _production_host_seams as _production_host_seams,
    _seed_applied_stage_1_state,
    _stage_2,
    _status,
)

# --------------------------------------------------------------------------- #
# the night's own numbers
#
# The fingerprint, region and filter below are verbatim from
# ``captures/wired-night-2026-08-19/prescriptions/``: the cut the harness
# accepted and A9 could not apply. The document around them is rebuilt here
# rather than read from that corpus — a test may not depend on a capture
# directory — so its DIGEST is this module's own and is asserted as a
# round-trip property rather than against the file the driver banked.
# --------------------------------------------------------------------------- #

_PACKET_FINGERPRINT = (
    "da274693544f05e67234eabdb5ec4bf6238363c3c9d0b0c31b4c49489720ce9f"
)
_BAND_HZ = (824.35, 3297.4)
_ACCEPTED_FILTERS = (
    {"biquad_type": "Peaking", "freq": 1400.0, "q": 2.0, "gain": -1.2},
)

#: A blend correction that is NOT the prescription, so "the round took the
#: staged document" and "the round fell back" are two distinguishable answers.
#: A fixture whose fallback matched would pass for the wrong reason.
_APPLIED_INCUMBENT = (
    {"biquad_type": "Peaking", "freq": 1200.0, "q": 2.0, "gain": -0.5},
)


def _document(**overrides: Any) -> bytes:
    """The accepted document's bytes, verbatim unless a test changes one field.

    Serialized with the same separators and key order every call, so the digest
    is stable across the whole module and a test that wants a DIFFERENT digest
    has to change the content rather than the formatting.
    """
    body: dict[str, Any] = {
        "artifact_schema_version": 1,
        "kind": "jts_crossover_blend_prescription",
        "packet_fingerprint": _PACKET_FINGERPRINT,
        "prescriber": {
            "model": "claude-opus-5",
            "operator": "wired-night driver session 2026-08-19 (jts3)",
        },
        "filters": [dict(f) for f in _ACCEPTED_FILTERS],
        "rationale": "cut the 1283-1526 Hz rise; second slot declined",
    }
    body.update(overrides)
    return json.dumps(body, indent=2, sort_keys=True).encode("utf-8")


def _accept(document: bytes) -> Any:
    """Run the REAL gate, exactly as the staging step does."""
    return read_blend_prescription(
        read_prescription_bytes(document),
        packet_fingerprint=_PACKET_FINGERPRINT,
        band_hz=_BAND_HZ,
        positional_evidence=None,
    )


def _stage(*, for_round_ordinal: int = 9, document: bytes | None = None) -> bytes:
    payload = _document() if document is None else document
    spool.stage_prescription(
        payload,
        _accept(payload),
        for_round_ordinal=for_round_ordinal,
        # The blend class has no classification bar; its anchor is its band.
        classifications=None,
    )
    return payload


def _rewrite_envelope(**overrides: Any) -> None:
    """Hand-edit the staged file, which is the whole threat model here."""
    path = spool.prescription_spool_path()
    envelope = json.loads(path.read_text())
    envelope.update(overrides)
    path.write_text(json.dumps(envelope))


@pytest.fixture(autouse=True)
def _isolated_spool(tmp_path):
    spool.set_prescription_spool_path_for_tests(
        tmp_path / "staged_prescription.json"
    )
    yield
    spool.set_prescription_spool_path_for_tests(None)


# --------------------------------------------------------------------------- #
# 1. the hop — a staged document reaches the candidate through the REAL preparer
# --------------------------------------------------------------------------- #


def _state_carrying_a_kept_round() -> dict[str, Any]:
    """Durable state as a kept round 8 leaves it, with its own instruction.

    The banked instruction is deliberately PRESENT and different from the
    staged prescription: precedence is the thing being pinned, and a state with
    no instruction would let "the staged document won" pass on a fixture where
    nothing was competing with it.
    """
    return {
        "round_receipt": {
            "round_ordinal": 8,
            "objectives": {"tilt_db": 0.4, "ripple_db": 0.9},
            "trusted_floor_hz": 143.0,
            "blend": {
                "filters": [
                    {"biquad_type": "Peaking", "freq": 2120.34, "q": 2.0,
                     "gain": -0.72},
                ],
                "residual_db": 0.73,
            },
        },
    }


def _prepare(monkeypatch) -> Any:
    """Drive the real stage-1 preparer and return its conductor."""
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile."
        "load_applied_baseline_profile_state",
        lambda: {"blend_correction": [dict(f) for f in _APPLIED_INCUMBENT]},
    )
    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=None,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)
    return conductor


def test_a_staged_prescription_reaches_the_next_rounds_measure_stage(monkeypatch):
    """A9's headline: the accepted cut becomes the candidate's blend correction.

    Driven through the REAL stage-1 preparer, and discriminating three ways —
    the staged filters, the banked instruction, and the applied incumbent are
    three different lists, so exactly one of them can be the answer.

    This is the hop that did not exist. Before it, the same document produced a
    round whose blend correction was whatever the deterministic solver last
    banked, with nothing on any surface saying a prescription had been ignored.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)

    conductor = _prepare(monkeypatch)

    assert conductor._blend_prescription() == _ACCEPTED_FILTERS
    # The two answers it is NOT, both reachable and both different.
    banked = conductor._series_position.previous_blend_correction
    assert tuple(banked) != _ACCEPTED_FILTERS
    assert flow.CrossoverV2Session._applied_blend_correction(
        SimpleNamespace()
    ) == _APPLIED_INCUMBENT


def test_the_round_receipt_carries_who_prescribed_it_and_which_document(
    monkeypatch,
):
    """Provenance survives the hop, or the series cannot be read back.

    The comparison the whole prescriber loop exists to make possible is
    "deterministic round versus prescribed round". That is only available if a
    banked round says which it was, by whom, and from which document — so the
    record is asserted field by field rather than for mere presence.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    payload = _stage(for_round_ordinal=9)

    conductor = _prepare(monkeypatch)
    record = conductor.blend_prescription_record

    assert record is not None
    assert record["prescription_class"] == "cut"
    assert record["prescriber"] == {
        "model": "claude-opus-5",
        "operator": "wired-night driver session 2026-08-19 (jts3)",
    }
    assert record["packet_fingerprint"] == _PACKET_FINGERPRINT
    assert record["filters"] == [dict(f) for f in _ACCEPTED_FILTERS]
    # The digest of the bytes that were STAGED, not of a re-serialization of
    # what they parsed to — the value that lets a reader find the evidence
    # packet and the conversation that produced the numbers. Asserted against a
    # fresh hash of the same bytes, so a hop that quietly re-encoded the
    # document somewhere in the middle would show up as a mismatch here. It
    # rides BESIDE the record, never inside it — the record must survive
    # `blend_prescription_from_mapping`, which refuses an unknown field.
    assert conductor.blend_prescription_sha256 == prescription_sha256(payload)
    assert "prescription_sha256" not in record


def test_the_provenance_is_written_to_durable_state_not_only_held_in_memory(
    monkeypatch,
):
    """The channel the attribution actually travels on, and the mutation for it.

    The stage that TAKES a prescription is stage 1 and the stage that banks the
    round's receipt is stage 2 — different sessions in different processes — so
    a record that lived only on the conductor would be gone before anything
    could bank it. That is why ``alignment_prescription`` rides
    ``verify_priors``, and this rides beside it.

    Reading the property alone cannot catch a missing persist: it answers off
    the conductor either way. This reads the state the preparer actually wrote.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    payload = _stage(for_round_ordinal=9)

    _prepare(monkeypatch)
    persisted = v2host.load_v2_state() or {}

    priors = persisted.get("verify_priors") or {}
    record = priors.get("blend_prescription")
    assert record is not None, "the prescription never reached durable state"
    assert record["filters"] == [dict(f) for f in _ACCEPTED_FILTERS]
    assert record["prescriber"]["model"] == "claude-opus-5"
    # The digest is banked BESIDE the record, never inside it: the record has
    # to survive `blend_prescription_from_mapping`, which refuses an unknown
    # field rather than ignoring it.
    assert priors["blend_prescription_sha256"] == prescription_sha256(payload)
    assert "prescription_sha256" not in record


def _state_a_prescribed_round_left(payload: bytes) -> dict[str, Any]:
    """An applied stage-1 state carrying a prescribed round's provenance.

    Built on the stage bridge's own ``_seed_applied_stage_1_state`` rather than
    hand-rolled, so the seed is the shipped shape and only the two A9 keys are
    this test's addition.
    """
    state = _seed_applied_stage_1_state()
    state["verify_priors"]["blend_prescription"] = _accept(payload).to_dict()
    state["verify_priors"]["blend_prescription_sha256"] = prescription_sha256(payload)
    v2host.save_v2_state(state)
    return state


def test_stage_two_does_not_erase_the_provenance_stage_one_banked(monkeypatch):
    """SF-2: the durable channel has to outlive the round, not just stage 1.

    ``verify_priors`` is REBUILT from the conductor on every persist. A stage-2
    conductor holds no prescription of its own, so without a rehydration arm
    stage 2 writes ``None`` over stage 1's record — before the round receipt is
    written — and a round that ran a prescribed correction is banked as though
    its correction had been solved.

    This is the #2698 shape exactly: the value reaches durable state and then
    nothing carries it the rest of the way. The shipped test stopped after
    stage 1, which is precisely where that defect hides, so this one reads the
    state after a stage-2 persist.
    """
    payload = _document()
    _state_a_prescribed_round_left(payload)

    _conductor, state = _stage_2(monkeypatch)

    priors = state.get("verify_priors") or {}
    record = priors.get("blend_prescription")
    assert record is not None, "stage 2 erased the prescription stage 1 banked"
    assert record["filters"] == [dict(f) for f in _ACCEPTED_FILTERS]
    assert record["packet_fingerprint"] == _PACKET_FINGERPRINT
    assert priors["blend_prescription_sha256"] == prescription_sha256(payload)


def test_the_record_stage_one_writes_is_one_stage_two_can_read(monkeypatch):
    """The round trip, end to end, with no seeded record in the middle.

    The two tests either side of this one each hold half: one proves stage 1
    WRITES a record, the other proves stage 2 REHYDRATES a record. Both passed
    while the written record was unreadable by the reader that rehydrates it —
    because the stage-2 fixture seeded its own. That is the gap this closes: the
    record that actually gets written is fed to the actual reader.

    The failure it pins is the one the gate found and the one this fix round
    caused: `blend_prescription_from_mapping` refuses an UNKNOWN FIELD, so a
    single extra key in the record — the document digest, folded in beside the
    prescription, which is exactly where it started — makes the whole thing
    read back as ``None``. Silently: the reader returns ``None`` for "no
    prescription", which is indistinguishable from an ordinary round.
    """
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        blend_prescription_from_mapping,
    )

    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)
    _prepare(monkeypatch)

    written = (
        (v2host.load_v2_state() or {}).get("verify_priors") or {}
    ).get("blend_prescription")
    assert written is not None

    rehydrated = blend_prescription_from_mapping(written)
    assert rehydrated is not None, (
        "the record stage 1 wrote is not one the stage-2 reader accepts"
    )
    assert [dict(f) for f in rehydrated.filters] == [
        dict(f) for f in _ACCEPTED_FILTERS
    ]
    assert rehydrated.packet_fingerprint == _PACKET_FINGERPRINT


def test_the_stage_two_conductor_can_name_what_the_round_was_prescribed(
    monkeypatch,
):
    """The read side: the rehydrated record lands on the grading conductor.

    The state assertion above proves the value survived the persist; this
    proves the GRADING session holds it, which is what lets the round receipt
    name it. Same split as the alignment prior's two pins.
    """
    payload = _document()
    _state_a_prescribed_round_left(payload)

    conductor, _state = _stage_2(monkeypatch)

    assert conductor.blend_prescription_record["filters"] == [
        dict(f) for f in _ACCEPTED_FILTERS
    ]
    assert conductor.blend_prescription_sha256 == prescription_sha256(payload)


def test_a_grading_session_carrying_a_prescription_cannot_re_apply_it(monkeypatch):
    """Carrying it is not applying it — the safety question the arm raises.

    Stage 2 now holds a `BlendPrescription`, and `_blend_prescription` reads it
    FIRST. That is only safe because the door it feeds — the MEASURE-stage
    candidate build — is not on stage 2's plan at all. Pinned on the plan rather
    than on an argument: a stage-2 session that ever grew a MEASURE phase would
    re-apply a correction the round already applied, and this fails first.
    """
    _state_a_prescribed_round_left(_document())

    conductor, _state = _stage_2(monkeypatch)

    assert conductor._prescribed_blend is not None
    assert flow.PHASE_MEASURE not in set(conductor.session_phases)


def test_a_deterministic_round_banks_no_prescription_in_durable_state(monkeypatch):
    """The control that makes the key's presence mean something.

    ``None`` is what every automatic round writes, so a reader can tell a
    prescribed round from a solved one by the key alone. If both wrote a record
    the attribution would be decoration.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())

    _prepare(monkeypatch)
    persisted = v2host.load_v2_state() or {}

    assert (persisted.get("verify_priors") or {}).get("blend_prescription") is None


def test_a_round_with_nothing_staged_is_byte_identical_to_today(monkeypatch):
    """The no-prescription path, which is every ordinary round.

    The control for the test above: with an empty spool the banked instruction
    still wins, so the door adds a source rather than replacing the two that
    were there. ``blend_prescription_record`` is ``None`` — the absence that
    means "this round's correction was solved", which is what makes its
    presence mean anything.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())

    conductor = _prepare(monkeypatch)

    assert conductor._blend_prescription() == (
        {"biquad_type": "Peaking", "freq": 2120.34, "q": 2.0, "gain": -0.72},
    )
    assert conductor.blend_prescription_record is None


def test_the_hop_fails_when_the_preparer_stops_handing_the_prescription_over(
    monkeypatch,
):
    """The mutation that proves the test above is load-bearing (hydration dropped).

    The #2698 defect, in this door's shape: the preparer resolves a
    prescription and the session never receives it, so the round silently falls
    back and no surface says so. Mutated by taking the ctor argument away, which
    is exactly what deleting the ``blend_prescription=`` line would do.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)

    real_hydrate = flow.CrossoverV2Session.hydrate

    def _hydrate_without_the_prescription(*args, **kwargs):
        kwargs.pop("blend_prescription", None)
        kwargs.pop("blend_prescription_sha256", None)
        return real_hydrate(*args, **kwargs)

    monkeypatch.setattr(
        flow.CrossoverV2Session, "hydrate",
        staticmethod(_hydrate_without_the_prescription),
    )
    conductor = _prepare(monkeypatch)

    assert conductor._blend_prescription() != _ACCEPTED_FILTERS
    assert conductor.blend_prescription_record is None


def test_a_refused_document_leaves_the_round_running_the_deterministic_path(
    monkeypatch,
):
    """Fail-open on the transport, fail-closed on the content.

    A document staged for another round must not cost the household its
    session: the round is prepared, the candidate carries decision 10's banked
    instruction, and nothing about the refusal reaches the speaker. Pinned
    through the real preparer because "the round proceeds" is a claim about the
    preparer, not about the spool.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=4)  # this is round 9

    conductor = _prepare(monkeypatch)

    assert conductor.blend_prescription_record is None
    assert conductor._blend_prescription() == (
        {"biquad_type": "Peaking", "freq": 2120.34, "q": 2.0, "gain": -0.72},
    )


def test_the_ordinal_the_preparer_checks_is_the_one_it_hands_the_session(
    monkeypatch,
):
    """One read, used twice — pinned by making a second read DIVERGE.

    The claim is that the preparer resolves the series position once. On a
    quiescent state file a second read returns the same answer, so a test that
    only staged for the right ordinal would pass against either shape and pin
    nothing.

    So the reader is made to answer differently each time it is called. A
    preparer that resolved twice hands the take one ordinal and the session the
    other; with the take going first it would look for round 9 and the session
    would believe it is round 10, and the prescription would be refused as
    stale. The two assertions below can only both hold if exactly ONE read
    happened.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)

    from jasper.active_speaker.crossover_v2 import coordinator

    # Patched at its OWNER, because the preparer imports it inside the function
    # — so this is the binding the call site actually resolves.
    real_reader = coordinator.series_position_from_state
    calls: list[int] = []

    def _diverging_reader(raw):
        position = real_reader(raw)
        calls.append(len(calls))
        return dataclasses.replace(position, ordinal=9 + len(calls) - 1)

    monkeypatch.setattr(
        coordinator, "series_position_from_state", _diverging_reader
    )
    conductor = _prepare(monkeypatch)

    assert calls == [0], "the preparer resolved the series position more than once"
    assert conductor._series_position.ordinal == 9
    assert conductor._blend_prescription() == _ACCEPTED_FILTERS


def test_staging_twice_is_last_wins_and_says_so(caplog):
    """The slot holds ONE instruction, and the second document is the answer.

    An operator who re-prescribes after re-reading the evidence means the
    second document; refusing would make them delete a file by hand to correct
    themselves. What must not happen is silence — the overwrite is logged, so a
    round that applied the second of two prescriptions can be explained.
    """
    first = _document(rationale="the first answer")
    second = _document(rationale="the second answer")
    assert prescription_sha256(first) != prescription_sha256(second)

    _stage(for_round_ordinal=9, document=first)
    with caplog.at_level(logging.INFO, logger="jasper.active_speaker.crossover_v2"
                                              ".prescription_spool"):
        _stage(for_round_ordinal=9, document=second)

    staged = spool.take_staged_prescription(round_ordinal=9)
    assert staged.prescription.rationale == "the second answer"
    assert staged.prescription_sha256 == prescription_sha256(second)
    # logfmt renders booleans lowercase — the rendered line is the contract a
    # reader greps, so it is what gets asserted.
    assert "replaced=true" in caplog.text


def test_staging_the_first_time_does_not_claim_to_have_replaced_anything(caplog):
    """The control: ``replaced`` is a fact, not a constant."""
    with caplog.at_level(logging.INFO, logger="jasper.active_speaker.crossover_v2"
                                              ".prescription_spool"):
        _stage(for_round_ordinal=9)

    assert "replaced=false" in caplog.text


# --------------------------------------------------------------------------- #
# 2. re-validation — the document is never trusted from disk
# --------------------------------------------------------------------------- #


def test_a_hand_edited_document_is_caught_by_the_digest():
    """The cheap half: an edit that did not also re-stamp the digest."""
    _stage(for_round_ordinal=9)
    envelope = json.loads(spool.prescription_spool_path().read_text())
    _rewrite_envelope(document=envelope["document"].replace("-1.2", "-12.0"))

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason == spool.SPOOL_MALFORMED


@pytest.mark.parametrize(
    "replacement, expected_reason",
    [
        # Past the per-filter cut ceiling.
        (('"gain": -1.2', '"gain": -12.0'), "filter_cut_too_deep"),
        # Outside the region the staging step banked.
        (('"freq": 1400.0', '"freq": 14000.0'), "filter_outside_region"),
        # Past the Q ceiling.
        (('"q": 2.0', '"q": 12.0'), "filter_q_out_of_range"),
        # A boost, which has no seam — and which cannot even reach the route
        # here, because the packet is gone and its positional bar refuses first.
        (('"gain": -1.2', '"gain": 1.2'), "insufficient_positional_evidence"),
        # Reaching past "numbers into a fixed shape".
        (
            ('"rationale": "cut', '"volume_db": 3, "rationale": "cut'),
            "prescription_prohibited_field",
        ),
    ],
)
def test_a_document_edited_past_a_bound_is_refused_even_with_a_fresh_digest(
    replacement, expected_reason,
):
    """The half that matters: the BOUNDS are re-applied, not just the digest.

    A digest is provenance, not protection — anyone who can edit the document
    can re-stamp it. What actually stops a hand-edited prescription reaching the
    speaker is that the take re-runs
    :func:`~...blend_prescription.read_blend_prescription`, so every bound that
    refused at staging refuses again here. Each case below re-stamps the digest
    first, so the digest check cannot be what caught it.

    The refusal slugs are the GATE's, not this module's: the take reports a bad
    correction in the vocabulary a prescriber already knows how to answer.
    """
    _stage(for_round_ordinal=9)
    envelope = json.loads(spool.prescription_spool_path().read_text())
    edited = envelope["document"].replace(*replacement)
    assert edited != envelope["document"], "the edit did not apply"
    _rewrite_envelope(
        document=edited, prescription_sha256=prescription_sha256(edited.encode()),
    )

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason == expected_reason
    assert caught.value.reason in PRESCRIPTION_REFUSAL_REASONS


def test_a_document_staged_for_another_round_is_refused_by_name():
    """Staleness, on the one fact this module does not invent.

    A prescription answers round N's evidence, so it is an instruction for
    round N+1 and no other. The ordinal comes from the round receipt the flow
    banked — neither the staging step nor the take makes it up — which is why
    this still refuses a document a failed consume left behind.
    """
    _stage(for_round_ordinal=9)

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=10)

    assert caught.value.reason == spool.PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND
    assert caught.value.evidence["staged_for_round"] == 9
    assert caught.value.evidence["this_round"] == 10


def test_the_ordinal_is_checked_before_the_document_is_unwrapped():
    """A document for another round is not this round's numbers to judge.

    Reporting a bound failure on evidence this round was never going to use
    would send an operator to fix numbers that were right for the round they
    answered. Pinned with a document that is BOTH stale and out of bounds: the
    lifecycle slug must win.
    """
    _stage(for_round_ordinal=9)
    envelope = json.loads(spool.prescription_spool_path().read_text())
    edited = envelope["document"].replace('"gain": -1.2', '"gain": -12.0')
    _rewrite_envelope(
        document=edited, prescription_sha256=prescription_sha256(edited.encode()),
    )

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=10)

    assert caught.value.reason == spool.PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "jts_crossover_blend_prescription"}, id="wrong-kind"),
        pytest.param({"artifact_schema_version": 2}, id="unknown-schema"),
        pytest.param({"for_round_ordinal": "9"}, id="ordinal-not-an-int"),
        pytest.param({"for_round_ordinal": True}, id="ordinal-is-a-bool"),
        pytest.param({"document": {"kind": "…"}}, id="document-not-text"),
        pytest.param({"band_hz": [3297.4, 824.35]}, id="band-inverted"),
    ],
)
def test_a_malformed_envelope_is_refused_rather_than_half_read(overrides):
    """The envelope's own shape gate.

    ``band-inverted`` is the one that does not resolve to
    :data:`~...prescription_spool.SPOOL_MALFORMED`: an unreadable band is handed
    to the gate as ``None`` so its own ``region_unavailable`` sentence answers,
    rather than this module growing a second spelling of the same refusal.
    """
    _stage(for_round_ordinal=9)
    _rewrite_envelope(**overrides)

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason in (
        spool.SPOOL_REFUSAL_REASONS | PRESCRIPTION_REFUSAL_REASONS
    )


def test_an_oversized_spool_file_is_refused_on_its_size_not_its_content():
    """The cap is applied to the file, before it is read.

    A cap enforced after the read has already paid what it exists to avoid. The
    document's own 64 KiB cap still belongs to its owner and is re-applied when
    the take unwraps it; this one only stops a pathological FILE being loaded.
    """
    _stage(for_round_ordinal=9)
    path = spool.prescription_spool_path()
    path.write_text("x" * (spool.SPOOL_MAX_BYTES + 1))

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason == spool.SPOOL_TOO_LARGE
    assert caught.value.evidence["got_bytes"] > spool.SPOOL_MAX_BYTES


def test_an_oversized_file_is_refused_without_ever_being_loaded(monkeypatch):
    """What the STAT check uniquely does, and nothing else can.

    The post-read check below also refuses an oversized document, so a test
    that only asserted the refusal would pass with the stat check deleted — and
    the whole point of a cap applied before parsing is that the pathological
    file is never loaded. Pinned by making the read itself an error: reaching it
    at all is the failure.
    """
    _stage(for_round_ordinal=9)
    path = spool.prescription_spool_path()
    path.write_text("x" * (spool.SPOOL_MAX_BYTES + 1))
    real_read = Path.read_bytes

    def _must_not_load(self, *args, **kwargs):
        if self == path:
            raise AssertionError("the oversized file was loaded")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _must_not_load)

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason == spool.SPOOL_TOO_LARGE


def test_the_cap_is_a_property_of_the_bytes_not_of_the_stat(monkeypatch):
    """A stat that under-reports must not let an oversized file through.

    The stat is what stops a huge file being LOADED; it is not what bounds the
    document, because the size it reports and the size that gets read are two
    measurements of a file that anything could have rewritten in between. Pinned
    by making the stat lie — the only way to reach the second check, and the
    reason it is not dead code.
    """
    import os

    _stage(for_round_ordinal=9)
    path = spool.prescription_spool_path()
    path.write_text("x" * (spool.SPOOL_MAX_BYTES + 1))
    real_stat = Path.stat

    def _understating_stat(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        if self != path:
            return info
        fields = list(info)
        fields[6] = 1  # st_size
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", _understating_stat)

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason == spool.SPOOL_TOO_LARGE
    assert caught.value.evidence["got_bytes"] > spool.SPOOL_MAX_BYTES


# --------------------------------------------------------------------------- #
# 2b. the envelope fuzz — every corrupt field refuses BY NAME
#
# Shipped as the fuzz rather than as the two cases it caught, because the
# finding was never "these two values"; it was that a REFUSAL path could raise
# an unnamed exception, and the preparer turns that into a raw programmer
# string in the wizard's 400 instead of a round that carries on. Two escapes
# came out of the reviewer's 8-field x 25-value sweep (a lone surrogate in the
# document string, an infinite band edge) and a third out of re-running it here
# (a finite-but-absurd band edge, which `isfinite` does not catch). A
# two-case test would pin the three values; this pins the property.
# --------------------------------------------------------------------------- #

#: Values chosen for the ways a JSON document can be hostile rather than merely
#: wrong: type confusion, the numeric tower's edges, the string edges an
#: encoder rejects, and structures that recurse or allocate.
_HOSTILE_VALUES = (
    None, True, False, 0, -1, 1, 2**63, -(2**63), 0.0, -0.0,
    float("inf"), float("-inf"), float("nan"), 1e308, -1e308, 1e-308,
    "", " ", "\x00", "\ud800", "\U0001f600", "x" * 4096,
    [], {}, [[[[[]]]]], {"a": {"b": {"c": {}}}}, [1e308, float("inf")],
)

#: Every top-level field the envelope carries. Derived from a staged document
#: rather than listed, so a field added to the envelope joins the fuzz without
#: anyone remembering to add it — the sweep cannot go stale against its subject.
def _envelope_fields() -> tuple[str, ...]:
    _stage(for_round_ordinal=9)
    fields = tuple(sorted(json.loads(spool.prescription_spool_path().read_text())))
    spool.withdraw_staged_prescription()
    return fields


def test_the_envelope_fuzz_covers_every_field_and_the_known_escapes():
    """The fuzz's own control: it sweeps what it claims to sweep.

    A fuzz whose field list drifted from the envelope would report a clean
    sweep of a subject it no longer covers — silence that looks like safety.
    The three values that actually escaped are named, so a later edit that
    dropped them from the corpus fails here rather than quietly narrowing it.
    """
    fields = _envelope_fields()

    assert fields == (
        "artifact_schema_version", "band_hz", "document", "for_round_ordinal",
        "kind", "packet_fingerprint", "prescription_kind", "prescription_sha256",
        "staged_at",
    )
    assert "\ud800" in _HOSTILE_VALUES
    assert float("inf") in _HOSTILE_VALUES
    assert 1e308 in _HOSTILE_VALUES
    assert len(fields) * len(_HOSTILE_VALUES) >= 8 * 25


def test_no_corrupt_envelope_field_escapes_the_refusal_vocabulary():
    """Every field x every hostile value: a NAMED refusal, or a clean take.

    The property, stated as the preparer experiences it: a corrupt spool may
    cost the round its prescription, and may never cost the household its
    session. Anything that is not a ``BlendPrescriptionRefused`` carrying a slug
    from one of the two vocabularies reaches ``prepare_v2_session`` as an
    unhandled exception — the wizard's 400 with a programmer string in it.

    A few combinations legitimately ACCEPT (a hostile value in a field the take
    does not read, such as ``staged_at``), and that is not a failure; the
    assertion is about what a refusal looks like, not about refusing.

    Two escapes this caught on the reviewer's sweep — a lone surrogate in
    ``document`` (``UnicodeEncodeError`` at the encode) and ``band_hz``'s
    infinite edge (a math domain error out of ``chain_response``) — plus one
    more found re-running it here: ``1e308``, finite and absurd, which reaches
    the same evaluator through ``math.cos`` instead.
    """
    known = spool.SPOOL_REFUSAL_REASONS | PRESCRIPTION_REFUSAL_REASONS
    escapes: list[str] = []

    for field in _envelope_fields():
        for value in _HOSTILE_VALUES:
            _stage(for_round_ordinal=9)
            try:
                _rewrite_envelope(**{field: value})
            except (TypeError, ValueError):  # unserializable probe, not a case
                spool.withdraw_staged_prescription()
                continue
            try:
                spool.take_staged_prescription(round_ordinal=9)
            except BlendPrescriptionRefused as exc:
                if exc.reason not in known:
                    escapes.append(f"{field}={value!r} -> unknown slug {exc.reason}")
            except Exception as exc:  # noqa: BLE001 - the finding IS the escape
                escapes.append(f"{field}={value!r} -> {type(exc).__name__}: {exc}")
            finally:
                spool.withdraw_staged_prescription()

    assert not escapes, "corrupt envelopes escaped the vocabulary:\n" + "\n".join(
        escapes
    )


@pytest.mark.parametrize(
    "band, label",
    [
        ([1.0, float("inf")], "infinite upper edge"),
        ([float("inf"), 1e9], "infinite lower edge"),
        ([1.0, float("nan")], "NaN edge"),
        # Finite, ordered, and still undefined: the evaluator computes
        # cos(2*pi*f/fs), which has no answer this far out.
        ([1.0, 1e308], "finite but past Nyquist"),
        ([1.0, spool._EVALUABLE_MAX_HZ + 1.0], "one hertz past Nyquist"),
    ],
)
def test_an_unevaluable_band_refuses_by_name_rather_than_raising(band, label):
    """The band guard, per escape shape, with the boundary named.

    ``region_unavailable`` rather than a spool slug on purpose:
    ``read_blend_prescription`` owns the sentence for "no region to check
    against", and a second spelling here would be a second owner of it.
    """
    _stage(for_round_ordinal=9)
    _rewrite_envelope(band_hz=band)

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason == "region_unavailable", label


def test_the_nyquist_bound_is_the_evaluators_own_number():
    """Imported, not restated — the rule the cut ceilings already follow.

    A locally-written 24000 would be a second source of truth for the response
    rate, and the failure mode of that copy is a band this reader accepts and
    the evaluator cannot compute.
    """
    from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

    assert spool._EVALUABLE_MAX_HZ == RESPONSE_SAMPLE_RATE_HZ / 2.0
    # And it is loose enough to constrain no real crossover region.
    assert spool._EVALUABLE_MAX_HZ > _BAND_HZ[1]


def test_a_lone_surrogate_document_refuses_rather_than_raising():
    """``json.loads`` accepts ``"\\ud800"``; no UTF-8 encoder will take it.

    So the encode in the take is a parse step, not a formality. Before the fix
    it raised ``UnicodeEncodeError`` straight through the preparer.
    """
    _stage(for_round_ordinal=9)
    _rewrite_envelope(document="\ud800")

    with pytest.raises(BlendPrescriptionRefused) as caught:
        spool.take_staged_prescription(round_ordinal=9)

    assert caught.value.reason == spool.SPOOL_MALFORMED


def test_the_two_refusal_vocabularies_stay_disjoint():
    """Two questions, two owners, and no slug answering for both.

    The gate's slugs say whether a correction may be applied; these say whether
    a document that already passed it is the instruction this round asked for. A
    slug appearing in both would make a prescriber's ``reason`` ambiguous about
    which question failed.
    """
    assert not (spool.SPOOL_REFUSAL_REASONS & PRESCRIPTION_REFUSAL_REASONS)


# --------------------------------------------------------------------------- #
# 3. consumption — one round, once
# --------------------------------------------------------------------------- #


def test_a_taken_prescription_is_never_offered_to_a_second_round():
    """The property staging exists to have: an instruction, not a setting."""
    _stage(for_round_ordinal=9)

    assert spool.take_staged_prescription(round_ordinal=9) is not None
    assert spool.take_staged_prescription(round_ordinal=9) is None
    assert not spool.staged_prescription_pending()


def test_a_refused_document_is_consumed_too():
    """Or a stale document refuses every round after it, on staler evidence.

    The take consumes BEFORE it validates, so a refusal cannot repeat itself.
    Asserted on the pending slot rather than on a second take's refusal,
    because those two would both be satisfied by a document that survived and
    happened to keep refusing.
    """
    _stage(for_round_ordinal=4)

    with pytest.raises(BlendPrescriptionRefused):
        spool.take_staged_prescription(round_ordinal=9)

    assert not spool.staged_prescription_pending()
    assert spool.take_staged_prescription(round_ordinal=4) is None


def test_a_consumed_document_survives_where_an_operator_can_read_it():
    """Consumed is moved, not deleted — the refusal is useless without it."""
    payload = _stage(for_round_ordinal=9)
    spool.take_staged_prescription(round_ordinal=9)

    pending = spool.prescription_spool_path()
    consumed = pending.with_suffix(spool.CONSUMED_SUFFIX + pending.suffix)
    assert consumed.is_file()
    assert json.loads(consumed.read_text())["document"] == payload.decode()


def test_the_round_that_took_one_leaves_nothing_for_the_next(monkeypatch):
    """Consumption through the REAL preparer, not just through the spool.

    The unit test above proves the function consumes. This proves the preparer
    reaches it: a second round prepared against the same state falls back to
    the banked instruction, with no prescription record.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)

    assert _prepare(monkeypatch).blend_prescription_record is not None
    v2host.save_v2_state(_state_carrying_a_kept_round())
    assert _prepare(monkeypatch).blend_prescription_record is None


# --------------------------------------------------------------------------- #
# 4. Undo — the #2699 withdrawal, applied to this slot
# --------------------------------------------------------------------------- #


def test_a_household_undo_withdraws_a_staged_prescription():
    """#2699's rule: an Undo withdraws the instruction it reverses.

    An operator stages a prescription by reading the evidence of a round the
    household then removes. Surviving, it would compose a correction onto a
    graph that is no longer there — the same reason ``observe_restore`` clears
    the receipt's banked ``blend``.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)

    v2host.observe_restore()

    assert not spool.staged_prescription_pending()
    assert spool.take_staged_prescription(round_ordinal=9) is None


def test_an_undo_withdraws_even_when_the_journey_state_is_gone():
    """The withdrawal runs ahead of the no-state early return, deliberately.

    A lost state file resolves the next round's ordinal back to 1, which is an
    ordinal a surviving document could legitimately match — so the one thing
    ``observe_restore`` clears that does not live in ``state`` must not be
    guarded by ``state`` being readable.
    """
    _stage(for_round_ordinal=9)
    assert v2host.load_v2_state() is None

    v2host.observe_restore()

    assert not spool.staged_prescription_pending()


def test_a_withdrawn_document_is_not_filed_among_the_ones_that_ran():
    """Nothing consumed it, so the consumed slot must not claim it did."""
    _stage(for_round_ordinal=9)
    pending = spool.prescription_spool_path()

    assert spool.withdraw_staged_prescription() is True

    assert not pending.with_suffix(spool.CONSUMED_SUFFIX + pending.suffix).exists()
    assert spool.withdraw_staged_prescription() is False


# --------------------------------------------------------------------------- #
# 5. one door
# --------------------------------------------------------------------------- #


def test_the_staged_prescription_enters_through_the_candidate_fields_seam():
    """The one-door property: a prescription reaches the candidate ONE way.

    ``_blend_prescription`` asks
    :func:`~...blend_prescription.blend_prescription_to_candidate_fields`, which
    re-asks the route, which is what makes "a boost can never populate
    ``blend_correction``" a property of the function rather than of the current
    call graph. Mutated by making that seam raise: a session that still produced
    filters would be reaching past it — a second entry path, and the one that
    could carry a class the seam refuses.
    """
    taken = _accept(_document())
    session = SimpleNamespace(
        _prescribed_blend=taken,
        _series_position=None,
        _applied_blend_correction=lambda: None,
    )
    assert flow.CrossoverV2Session._blend_prescription(session) == _ACCEPTED_FILTERS

    def _seam_refuses(_prescription):
        raise AssertionError("the seam was bypassed")

    import jasper.active_speaker.crossover_v2.blend_prescription as bp

    original = bp.blend_prescription_to_candidate_fields
    bp.blend_prescription_to_candidate_fields = _seam_refuses
    try:
        with pytest.raises(AssertionError, match="bypassed"):
            flow.CrossoverV2Session._blend_prescription(session)
    finally:
        bp.blend_prescription_to_candidate_fields = original


def test_the_seam_names_the_field_rather_than_the_flow_spelling_it():
    """The candidate field has one owner, and the door reads it from there."""
    fields = _accept(_document())
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        blend_prescription_to_candidate_fields,
    )

    assert sorted(blend_prescription_to_candidate_fields(fields)) == [
        BLEND_CANDIDATE_FIELD
    ]


def test_taking_is_the_only_reader_and_it_always_consumes():
    """No public way to look at a staged prescription without spending it.

    ``staged_prescription_pending`` answers what a stat answers and no more —
    it must not hand back the document, or "consumed on the round starting"
    becomes a convention instead of a property.
    """
    _stage(for_round_ordinal=9)

    assert spool.staged_prescription_pending() is True
    assert spool.staged_prescription_pending() is True  # a peek spends nothing

    readers = [
        name for name in spool.__all__
        if name.startswith(("read", "take", "load", "get"))
    ]
    assert readers == ["take_staged_prescription"]


# --------------------------------------------------------------------------- #
# 6. the CLI — the operator's end of the door
# --------------------------------------------------------------------------- #


def _write_state(tmp_path: Path, ordinal: int) -> Path:
    path = tmp_path / "flow_state.json"
    path.write_text(json.dumps({"round_receipt": {"round_ordinal": ordinal}}))
    return path


def test_the_stage_verb_stamps_the_round_the_receipt_says_is_next(
    tmp_path, monkeypatch,
):
    """``stage`` derives the ordinal, it does not take one on the command line.

    An operator-supplied ordinal would be a second owner of the series'
    arithmetic and the easiest possible way to file a prescription against the
    wrong round.
    """
    document = tmp_path / "prescription.json"
    document.write_bytes(_document())
    state = _write_state(tmp_path, ordinal=8)
    monkeypatch.setattr(
        cli, "_gate", lambda _args: (_document(), _accept(_document()), {}, None),
    )

    code = cli.main([
        "stage", str(tmp_path), "--state", str(state),
        "--prescription", str(document),
    ])

    assert code == cli.EXIT_OK
    envelope = json.loads(spool.prescription_spool_path().read_text())
    assert envelope["for_round_ordinal"] == 9
    assert envelope["prescription_sha256"] == prescription_sha256(_document())


def test_the_stage_verb_refuses_without_the_state_it_reads_the_ordinal_from(
    tmp_path, capsys,
):
    """Staging without ``--state`` would file against a series it cannot see.

    ``series_position_from_state`` resolves every unreadable shape to the first
    round — a real answer for a series starting over, a fabricated one here — so
    the command refuses rather than stamping a 1 nobody measured.

    The MESSAGE is asserted, not just the exit code, and that is what makes this
    a pin on the guard rather than on an accident: without the explicit check
    the ordinal read still fails, on a ``FileNotFoundError`` for a path spelled
    ``None``, and exits the same ``1``. An operator reading that has no idea
    which flag they missed.
    """
    document = tmp_path / "prescription.json"
    document.write_bytes(_document())

    code = cli.main(["stage", str(tmp_path), "--prescription", str(document)])

    assert code == cli.EXIT_EVIDENCE_UNREADABLE
    assert "--state is required" in capsys.readouterr().err
    assert not spool.staged_prescription_pending()


def test_the_state_flag_help_matches_whether_the_verb_needs_it():
    """``--help`` must not call a hard-required flag optional.

    ``stage`` refuses without ``--state``; ``packet`` and ``propose`` degrade
    and say so. One shared help string cannot be true of both, and the one that
    shipped was the optional sentence — a `--help` contradicting the command it
    documents, and contradicting the tool index's own row.
    """
    parser = cli.build_parser()
    helps = {
        name: next(
            action.help for action in sub._actions
            if action.dest == "state"
        )
        for name, sub in parser._subparsers._group_actions[0].choices.items()
    }

    assert "REQUIRED for this verb" in helps["stage"]
    assert "Optional" not in helps["stage"]
    for verb in ("packet", "propose"):
        assert "Optional" in helps[verb]
        assert "REQUIRED" not in helps[verb]


def test_a_refused_prescription_stages_nothing_and_exits_two(tmp_path, monkeypatch):
    """The gate is the same one ``propose`` runs, so its refusal is the same.

    And the slot stays empty: an operator who saw a refusal must not find a
    document waiting for the next round anyway.
    """
    document = tmp_path / "prescription.json"
    document.write_bytes(_document())
    state = _write_state(tmp_path, ordinal=8)

    def _refuse(_args):
        raise BlendPrescriptionRefused("filter_cut_too_deep", "too deep")

    monkeypatch.setattr(cli, "_gate", _refuse)

    code = cli.main([
        "stage", str(tmp_path), "--state", str(state),
        "--prescription", str(document), "--json",
    ])

    assert code == cli.EXIT_REFUSED
    assert not spool.staged_prescription_pending()


def test_a_stage_that_cannot_write_is_its_own_exit_code(tmp_path, monkeypatch):
    """``3`` sends an operator to the filesystem, ``2`` to the prescription.

    Folded into ``1`` they would be indistinguishable to a script, which would
    then retry the wrong one.
    """
    document = tmp_path / "prescription.json"
    document.write_bytes(_document())
    state = _write_state(tmp_path, ordinal=8)
    monkeypatch.setattr(
        cli, "_gate", lambda _args: (_document(), _accept(_document()), {}, None),
    )

    def _cannot_write(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(cli, "stage_prescription", _cannot_write)

    code = cli.main([
        "stage", str(tmp_path), "--state", str(state),
        "--prescription", str(document),
    ])

    assert code == cli.EXIT_STAGE_FAILED


def test_propose_and_stage_run_the_same_gate(tmp_path, monkeypatch):
    """The property that makes ``propose`` a true dry run of ``stage``.

    Mutated rather than asserted by reading the source: both commands are driven
    with the gate replaced, and both must reach the replacement. A command with
    its own copy of the four gate calls would sail past it.
    """
    document = tmp_path / "prescription.json"
    document.write_bytes(_document())
    state = _write_state(tmp_path, ordinal=8)
    reached: list[str] = []

    def _counting_gate(_args):
        reached.append(_args.command)
        return _document(), _accept(_document()), {}, None

    monkeypatch.setattr(cli, "_gate", _counting_gate)

    argv = [str(tmp_path), "--state", str(state), "--prescription", str(document)]
    assert cli.main(["propose", *argv]) == cli.EXIT_OK
    assert cli.main(["stage", *argv]) == cli.EXIT_OK
    assert reached == ["propose", "stage"]


# --------------------------------------------------------------------------- #
# 7. A10 — the staged event has to be observable where an operator looks
#
# Found on jts3: ``stage_prescription`` emits
# ``event=crossover_v2.prescription_staged`` right after the atomic write, and
# the CLI configured no logging at all — so ``logging.lastResort`` (WARNING and
# above) dropped it, and the tool's one state transition reached neither the
# journal nor the operator's terminal.
#
# These run the entrypoint in a SUBPROCESS on purpose. pytest installs its own
# root handler for every test, so an in-process ``caplog`` assertion captures
# the record whether or not anything configured logging — it would have passed
# against the broken shape, which is the one thing this pin may not do.
# --------------------------------------------------------------------------- #

#: Runs the REAL ``cli.main`` with only the evidence gate stubbed, so the
#: logging wiring under test is the shipped one. Nothing in this script
#: configures a logger: if the entrypoint does not, the event has nowhere to go.
_STAGE_IN_A_REAL_PROCESS = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from jasper.active_speaker.crossover_v2 import prescription_spool as spool
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        read_blend_prescription,
        read_prescription_bytes,
    )
    from jasper.cli import crossover_prescriber as cli

    spool_path, doc_path, state_path, fingerprint, lo, hi = sys.argv[1:7]
    spool.set_prescription_spool_path_for_tests(Path(spool_path))
    document = Path(doc_path).read_bytes()
    prescription = read_blend_prescription(
        read_prescription_bytes(document),
        packet_fingerprint=fingerprint,
        band_hz=(float(lo), float(hi)),
        positional_evidence=None,
    )
    cli._gate = lambda _args: (document, prescription, {}, None)
    raise SystemExit(
        cli.main([
            "stage", str(Path(doc_path).parent),
            "--state", state_path, "--prescription", doc_path,
        ])
    )
    """
)


def _stage_in_a_real_process(tmp_path: Path) -> subprocess.CompletedProcess:
    document = tmp_path / "prescription.json"
    document.write_bytes(_document())
    state = _write_state(tmp_path, ordinal=8)
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [
            sys.executable, "-c", _STAGE_IN_A_REAL_PROCESS,
            str(tmp_path / "staged.json"), str(document), str(state),
            _PACKET_FINGERPRINT, str(_BAND_HZ[0]), str(_BAND_HZ[1]),
        ],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )


def test_the_staged_event_reaches_stderr_from_the_real_entrypoint(tmp_path):
    """A10: the one state transition this CLI performs is observable.

    Asserted on a real process's stderr, which is exactly what an operator sees
    over SSH and what systemd hands the journal. Without the entrypoint's
    logging configuration this line does not exist anywhere — the record is
    created and dropped, because ``logging.lastResort`` starts at WARNING.
    """
    result = _stage_in_a_real_process(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "event=crossover_v2.prescription_staged" in result.stderr
    # The fields an operator needs to tell one staging from another: which round
    # it is for, which document it was, and whether it replaced one.
    assert "for_round_ordinal=9" in result.stderr
    assert f"prescription_sha256={prescription_sha256(_document())}" in result.stderr
    assert "replaced=false" in result.stderr


def test_the_staged_event_goes_to_stderr_and_never_to_stdout(tmp_path):
    """stdout is the machine channel; a log line there would corrupt ``--json``.

    The same split every other verb keeps — the packet and the accepted result
    are stdout, the human and structured lines are stderr — so a caller piping
    this command's stdout into ``jq`` is never handed a log line.
    """
    result = _stage_in_a_real_process(tmp_path)

    assert "event=crossover_v2.prescription_staged" not in result.stdout


def test_configuring_logging_does_not_silence_the_human_summary(tmp_path):
    """The control: the non-JSON summary an operator reads still prints.

    A logging change that captured or reformatted the command's own stderr
    writes would trade one observability defect for another.
    """
    result = _stage_in_a_real_process(tmp_path)

    assert "staged cut prescription for round 9" in result.stderr
    assert "the next round takes it once and consumes it" in result.stderr
