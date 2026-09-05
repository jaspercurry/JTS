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

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.active_speaker.crossover_v2.blend_prescription import (
    BLEND_CANDIDATE_FIELD,
    BLEND_PRESCRIPTION_REFUSAL_REASONS,
    PRESCRIPTION_KIND,
    BlendPrescriptionRefused,
    prescription_sha256,
    read_blend_prescription,
    read_prescription_bytes,
)
from jasper.active_speaker.crossover_v2.candidates import CloudFitEvidence
from jasper.active_speaker.crossover_v2.driver_prescription import (
    DRIVER_PRESCRIPTION_KIND,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    DEFECT_BOOSTABLE,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    packet_feature_classifications,
)
from jasper.active_speaker.crossover_v2.plan_assembly import (
    compose_linearized_prediction,
)
from jasper.active_speaker.linearization_fit import (
    LinearizationFilter,
    LinearizationFit,
    linearization_filters_by_role,
    worst_headroom_cost_db,
)
from jasper.active_speaker.crossover_v2 import round_inputs as round_inputs_mod
from jasper.cli import crossover_prescriber as cli
from jasper.web import correction_crossover_v2 as v2host

from tests.crossover_v2_fixtures import (
    FakeSeams,
    _conductor,
    _eligible_measure_analysis,
    _run_phase,
)

# The per-driver class's own document builders, borrowed from the module that
# owns them (section 7) rather than re-derived: a second hand-built fixture for
# the same artifact is the second source of truth this repo trims. Aliased
# because this module already has a ``_document`` of its own, for the other
# class.
from tests.test_crossover_v2_driver_prescription import (
    TWEETER_FEATURE_HZ,
    WOOFER_FEATURE_HZ,
    _boost as _driver_boost,
    _boostable as _driver_boostable,
    _classification as _driver_classification,
    _cut as _driver_cut,
    _document as _driver_document,
    _gate as _driver_gate,
    _speaker as _driver_packet,
    _verdict as _driver_verdict,
)
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



# Production refuses a session with no volume owner; stand one up. The CLI
# tests below build a packet from a live session bundle with no
# --drivers/--applied-profile, so none may read this machine's own.
pytestmark = pytest.mark.usefixtures(
    "a_process_with_a_volume_owner", "no_real_pi_paths"
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
        # Past the boost ceiling. The cut arm's own ceilings are retired
        # (ADR-0207), so the sign flip is what an edit must reach for.
        (('"gain": -1.2', '"gain": 4.0'), "filter_boost_too_high"),
        # Outside the region the staging step banked.
        (('"freq": 1400.0', '"freq": 14000.0'), "filter_outside_region"),
        # A Q the emitter cannot build at all.
        (('"q": 2.0', '"q": -2.0'), "filter_malformed"),
        # A boost, which has no seam. It reaches the ROUTE now: the positional
        # bar that used to refuse first was demoted to a finding by the nanny
        # burn-down, so `boost_route_unavailable` — retained by ruling R8 — is
        # the only thing left refusing a blend boost.
        (('"gain": -1.2', '"gain": 1.2'), "boost_route_unavailable"),
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
    assert caught.value.reason in BLEND_PRESCRIPTION_REFUSAL_REASONS


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
        spool.PRESCRIPTION_SPOOL_REFUSAL_REASONS | BLEND_PRESCRIPTION_REFUSAL_REASONS
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

def _drop_staged() -> None:
    """Clear the pending slot between fuzz cases (test-side unlink)."""
    try:
        spool.prescription_spool_path().unlink()
    except FileNotFoundError:
        pass


#: Every top-level field the envelope carries. Derived from a staged document
#: rather than listed, so a field added to the envelope joins the fuzz without
#: anyone remembering to add it — the sweep cannot go stale against its subject.
def _envelope_fields() -> tuple[str, ...]:
    _stage(for_round_ordinal=9)
    fields = tuple(sorted(json.loads(spool.prescription_spool_path().read_text())))
    _drop_staged()
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
    known = spool.PRESCRIPTION_SPOOL_REFUSAL_REASONS | BLEND_PRESCRIPTION_REFUSAL_REASONS
    escapes: list[str] = []

    for field in _envelope_fields():
        for value in _HOSTILE_VALUES:
            _stage(for_round_ordinal=9)
            try:
                _rewrite_envelope(**{field: value})
            except (TypeError, ValueError):  # unserializable probe, not a case
                _drop_staged()
                continue
            try:
                spool.take_staged_prescription(round_ordinal=9)
            except BlendPrescriptionRefused as exc:
                if exc.reason not in known:
                    escapes.append(f"{field}={value!r} -> unknown slug {exc.reason}")
            except Exception as exc:  # noqa: BLE001 - the finding IS the escape
                escapes.append(f"{field}={value!r} -> {type(exc).__name__}: {exc}")
            finally:
                _drop_staged()

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
    assert not (
        spool.PRESCRIPTION_SPOOL_REFUSAL_REASONS & BLEND_PRESCRIPTION_REFUSAL_REASONS
    )


def test_the_unprefixed_spool_refusal_reasons_name_is_gone():
    """This module's own member of the two-file ``SPOOL_REFUSAL_REASONS``
    collision with :mod:`.angle_capture_spool` — renamed to
    :data:`PRESCRIPTION_SPOOL_REFUSAL_REASONS` so importing both modules
    unqualified cannot shadow one vocabulary with the other. The bare name
    must not still be an attribute of this module.
    """
    assert not hasattr(spool, "SPOOL_REFUSAL_REASONS")


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


def test_the_stage_verb_banks_a_document_judged_against_a_saved_packet_FILE(
    tmp_path, monkeypatch, capsys,
):
    """The whole flow, ending where it is supposed to: emit once, stage that.

    ``--packet`` is what removes the second packet, and with it the hand-copied
    fingerprint that staging used to need. The builder is replaced with a
    raiser, so the file is the only thing that can have answered.

    ``--state`` survives beside ``--packet`` here and only here: this verb reads
    it for the round ordinal — a fact about the SERIES, not the round's
    evidence — and hard-refuses without one.
    """
    from tests.test_crossover_v2_blend_prescription import (
        _bundle,
        _cut,
        _document as _blend_document,
    )
    from jasper.active_speaker.crossover_v2.evidence_packet import (
        build_crossover_evidence_packet,
    )

    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(
        session,
        driver_draft_path=round_inputs_mod.DRIVERS_DEFAULT_PATH,
        applied_profile_path=round_inputs_mod.APPLIED_PROFILE_DEFAULT_PATH,
    )
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    document = tmp_path / "prescription.json"
    payload = json.dumps(_blend_document([_cut(-1.5)], packet)).encode()
    document.write_bytes(payload)
    state = _write_state(tmp_path, ordinal=8)

    def _raise(*_a, **_k):  # pragma: no cover - asserted by not firing
        raise AssertionError("the packet was rebuilt instead of read from --packet")

    monkeypatch.setattr(cli, "build_crossover_evidence_packet", _raise)

    code = cli.main([
        "stage", "--packet", str(packet_path), "--state", str(state),
        "--prescription", str(document),
    ])

    assert code == cli.EXIT_OK
    staged_at = spool.prescription_spool_path()
    envelope = json.loads(staged_at.read_text())
    assert envelope["for_round_ordinal"] == 9
    assert envelope["packet_fingerprint"] == packet["packet_fingerprint"]
    assert envelope["prescription_sha256"] == prescription_sha256(payload)
    # stdout is the answer, and for this verb the answer is where the document
    # landed and which round it is now the instruction for.
    answer = json.loads(capsys.readouterr().out)
    assert answer["staged"] is True
    assert answer["out"] == str(staged_at)
    assert answer["bytes"] == staged_at.stat().st_size
    assert answer["for_round_ordinal"] == envelope["for_round_ordinal"]
    assert answer["prescription_sha256"] == envelope["prescription_sha256"]


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

    assert code == cli.EXIT_UNREADABLE
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
    for verb in ("packet", "propose", "status"):
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
        raise BlendPrescriptionRefused("filter_boost_too_high", "too deep")

    monkeypatch.setattr(cli, "_gate", _refuse)

    code = cli.main([
        "stage", str(tmp_path), "--state", str(state),
        "--prescription", str(document),
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

    assert code == cli.EXIT_WRITE_FAILED


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
    # --out because the gate is stubbed: nothing here resolved a round for the
    # accepted result to land beside.
    assert cli.main(
        ["propose", *argv, "--out", str(tmp_path / "proposal.json")]
    ) == cli.EXIT_OK
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
    """stdout is the machine channel; a log line there would corrupt the answer.

    The same split every other verb keeps — the answer document is stdout, the
    human and structured lines are stderr — so a caller piping this command's
    stdout into ``jq`` is never handed a log line.
    """
    result = _stage_in_a_real_process(tmp_path)

    assert "event=crossover_v2.prescription_staged" not in result.stdout


def test_configuring_logging_does_not_silence_the_human_summary(tmp_path):
    """The control: the human summary an operator reads still prints.

    A logging change that captured or reformatted the command's own stderr
    writes would trade one observability defect for another.
    """
    result = _stage_in_a_real_process(tmp_path)

    assert "staged cut prescription for round 9" in result.stderr
    assert "the next round takes it once and consumes it" in result.stderr


# --------------------------------------------------------------------------- #
# 7. the OTHER class — the per-driver document's own route into a round (PR-B)
#
# The blend half above pins a door that already opened. This section pins the
# second one: until PR-B the preparer took with ``accepts=BLEND_ONLY``, so a
# per-driver document was refused by name before its ordinal, its digest and
# its gate were ever looked at, and ``driver_prescription_to_candidate_fields``
# had no production caller at all. What is asserted here is the WIRING — that a
# real document, staged through the real gate, reaches a real session and lands
# on the candidate merged BY ROLE. The merge RULE is
# ``tests/test_crossover_v2_driver_prescription.py``'s; this is its consumption.
# --------------------------------------------------------------------------- #


def _stage_driver(
    tmp_path: Path, *, ordinal: int = 9, filters: Any = None,
    classification: dict[str, Any] | None = None,
    pinned_trim_db: Any = None,
) -> tuple[Any, bytes]:
    """One accepted per-driver document, banked in THIS module's spool.

    The real gate against the real-artifact fixture, then the real
    ``stage_prescription`` — #2752's shape — so the round below takes a document
    production would have accepted rather than a hand-built stand-in.
    """
    packet = _driver_packet(tmp_path / "driver-bundle", classification=classification)
    over = {} if pinned_trim_db is None else {"pinned_trim_db": pinned_trim_db}
    document = _driver_document(filters or [_driver_cut()], packet, **over)
    payload = json.dumps(document).encode()
    prescription = _driver_gate(packet, document)
    spool.stage_prescription(
        payload,
        prescription,
        for_round_ordinal=ordinal,
        classifications=packet_feature_classifications(packet),
    )
    return prescription, payload


def test_a_staged_per_driver_prescription_reaches_the_next_rounds_session(
    tmp_path, monkeypatch,
):
    """PR-B's headline: the class the round could not take, taken.

    Driven through the REAL stage-1 preparer against a document the REAL gate
    accepted. Before this the same file produced
    ``prescription_class_not_accepted`` and a round that ran the automatic fit,
    with nothing on any surface saying an instruction had been dropped.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    prescription, _payload = _stage_driver(tmp_path, ordinal=9)

    conductor = _prepare(monkeypatch)

    assert conductor._prescribed_driver == prescription
    assert conductor._prescribed_driver.roles == ("tweeter",)


def test_the_two_classes_are_separate_arms_and_a_session_holds_one(
    tmp_path, monkeypatch,
):
    """One slot, one take, two ctor arguments — and never both at once.

    The split is made at the take, on the envelope's class field. A split that
    leaked would hand a ``DriverPrescription`` to ``_blend_prescription``, whose
    next line reads ``filters`` as a flat region list — the exact shape
    confusion the fail-closed default existed to prevent.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage_driver(tmp_path, ordinal=9)

    conductor = _prepare(monkeypatch)

    assert conductor._prescribed_driver is not None
    assert conductor._prescribed_blend is None
    # …and the round's blend correction is untouched: the banked instruction,
    # not the per-driver document, and not the applied incumbent.
    assert conductor._blend_prescription() == (
        {"biquad_type": "Peaking", "freq": 2120.34, "q": 2.0, "gain": -0.72},
    )


def test_a_blend_document_still_lands_on_the_blend_arm_alone(monkeypatch):
    """The regression control for the split, from the other side.

    Widening ``accepts`` must not change one byte of what a blend round does.
    Both arms are asserted, because a split that put every document on the
    driver arm would leave the take itself looking healthy and fail only later,
    at a candidate nothing here builds.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)

    conductor = _prepare(monkeypatch)

    assert conductor._prescribed_driver is None
    assert conductor._blend_prescription() == _ACCEPTED_FILTERS


def test_the_hop_fails_when_the_preparer_stops_handing_the_driver_class_over(
    tmp_path, monkeypatch,
):
    """The mutation that proves the hop above is load-bearing.

    #2698's shape in this door: the preparer resolves a prescription, the
    session never receives it, and the round silently falls back. Mutated by
    taking the ctor argument away — exactly what deleting the
    ``driver_prescription=`` line would do.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage_driver(tmp_path, ordinal=9)

    real_hydrate = flow.CrossoverV2Session.hydrate

    def _hydrate_without_the_prescription(*args, **kwargs):
        kwargs.pop("driver_prescription", None)
        return real_hydrate(*args, **kwargs)

    monkeypatch.setattr(
        flow.CrossoverV2Session, "hydrate",
        staticmethod(_hydrate_without_the_prescription),
    )
    conductor = _prepare(monkeypatch)

    assert conductor._prescribed_driver is None


def test_the_preparer_accepts_every_class_the_slot_can_carry(
    tmp_path, monkeypatch, caplog,
):
    """The arming itself, pinned as a property rather than as an absence.

    ``PRESCRIPTION_CLASS_NOT_ACCEPTED`` is unreachable from this caller now, and
    that is the whole edit: the envelope's class is checked against
    ``STAGEABLE_KINDS`` first, and this taker accepts exactly that set. Asserted
    on the journal too, because a take that quietly reverted to the fail-closed
    default would leave the conductor's field ``None`` for two different reasons
    and only the slug tells them apart.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage_driver(tmp_path, ordinal=9)

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        conductor = _prepare(monkeypatch)

    assert spool.PRESCRIPTION_CLASS_NOT_ACCEPTED not in caplog.text
    assert conductor._prescribed_driver is not None


def test_the_take_event_names_the_class_and_the_branches_it_replaces(
    tmp_path, monkeypatch, caplog,
):
    """One event, extended — never twinned — and it carries the deciding numbers.

    A reader of the journal alone has to be able to answer "which class was
    taken, and which driver branches stopped being fitted this round". Both
    facts are on the line: ``prescription_kind`` is the DOCUMENT's class and
    ``prescription_class`` beside it stays cut-versus-boost, because one key may
    carry one fact.
    """
    _prescription, payload = _stage_driver(tmp_path, ordinal=9)
    v2host.save_v2_state(_state_carrying_a_kept_round())

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        _prepare(monkeypatch)

    assert "event=correction.crossover_v2_prescription_taken" in caplog.text
    assert f"prescription_kind={DRIVER_PRESCRIPTION_KIND}" in caplog.text
    assert "prescription_class=cut" in caplog.text
    assert "roles=tweeter" in caplog.text
    assert "filters=1" in caplog.text
    assert f"prescription_sha256={prescription_sha256(payload)}" in caplog.text


def test_the_blend_classs_take_event_carries_the_class_and_no_roles(
    monkeypatch, caplog,
):
    """The same line for the other class, so one grep answers both.

    ``roles`` is empty rather than absent: a blend correction is one region, not
    a set of branches, and a field that disappeared on one arm would make the
    line's shape depend on its content.
    """
    v2host.save_v2_state(_state_carrying_a_kept_round())
    _stage(for_round_ordinal=9)

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        _prepare(monkeypatch)

    assert "event=correction.crossover_v2_prescription_taken" in caplog.text
    assert f"prescription_kind={PRESCRIPTION_KIND}" in caplog.text
    assert 'roles=""' in caplog.text


# --- the refusals, inherited whole ------------------------------------------ #


def _refusal_slug(caplog) -> str:
    """The slug on this round's refusal line, or ``""`` if it never said one."""
    for record in caplog.records:
        message = record.getMessage()
        if "event=correction.crossover_v2_prescription_refused" in message:
            return message.split("reason=")[1].split(" ")[0]
    return ""


def test_a_per_driver_document_staged_for_another_round_is_refused_and_consumed(
    tmp_path, monkeypatch, caplog,
):
    """Fail-open on the transport, fail-closed on the content — for this class too.

    The three properties the blend class already had, inherited rather than
    re-argued: the document is consumed before it is judged, the refusal is
    journalled by name, and the session carries on with the automatic answer.
    """
    _stage_driver(tmp_path, ordinal=4)  # …and this is round 9
    v2host.save_v2_state(_state_carrying_a_kept_round())

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        conductor = _prepare(monkeypatch)

    assert _refusal_slug(caplog) == spool.PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND
    assert spool.staged_prescription_pending() is False
    assert conductor._prescribed_driver is None
    assert conductor._blend_prescription() == (
        {"biquad_type": "Peaking", "freq": 2120.34, "q": 2.0, "gain": -0.72},
    )


def test_a_tampered_per_driver_document_is_refused_on_the_digest(
    tmp_path, monkeypatch, caplog,
):
    """The document that ran must be the document that was accepted.

    Deepening the cut by hand after staging leaves the banked digest naming
    bytes that no longer exist, and the take refuses before the gate — so a
    filter nobody vouched for cannot reach a driver branch.
    """
    _stage_driver(tmp_path, ordinal=9)
    v2host.save_v2_state(_state_carrying_a_kept_round())
    envelope = json.loads(spool.prescription_spool_path().read_text())
    document = json.loads(envelope["document"])
    document["filters"][0]["gain"] = -11.0
    _rewrite_envelope(document=json.dumps(document))

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        conductor = _prepare(monkeypatch)

    assert _refusal_slug(caplog) == spool.SPOOL_MALFORMED
    assert conductor._prescribed_driver is None


def test_a_per_driver_filter_moved_off_its_verdict_is_refused_at_the_take(
    tmp_path, monkeypatch, caplog,
):
    """The CONTENT reading, re-run at the take and reaching the round's preparer.

    #2752 made the take's classification reading EQUAL to the staging gate's by
    banking the whole row set, and the 2026-08-23 ruling made that reading a
    DISCLOSURE. What this pins is that the equal reading is the one the ROUND
    now meets: a filter re-aimed at an unclassified frequency reaches the round
    carrying its own unvouched count, and the round is what measures it.

    The blend document beside it is untouched, which is the other half: this
    class's ruling changed nothing about the sibling's bar.
    """
    _stage_driver(tmp_path, ordinal=9)
    v2host.save_v2_state(_state_carrying_a_kept_round())
    envelope = json.loads(spool.prescription_spool_path().read_text())
    document = json.loads(envelope["document"])
    document["filters"][0]["freq"] = 12000.0
    payload = json.dumps(document).encode()
    _rewrite_envelope(
        document=payload.decode(),
        prescription_sha256=prescription_sha256(payload),
    )

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        conductor = _prepare(monkeypatch)

    assert _refusal_slug(caplog) == ""
    assert conductor._prescribed_driver is not None
    assert conductor._prescribed_driver.filters[0]["freq"] == 12000.0
    assert conductor._prescribed_driver.unvouched_filters == 1
    assert conductor._blend_prescription() == (
        {"biquad_type": "Peaking", "freq": 2120.34, "q": 2.0, "gain": -0.72},
    )


#: The 2026-08-19 record's own peak/dip pair, 0.143 octaves apart — closer than
#: the match tolerance, which is what makes NEAREST-decides load-bearing.
_RECORD_PEAK_HZ = 4149.0
_RECORD_DIP_HZ = 4582.0


def test_a_per_driver_filter_nudged_onto_a_nearby_dip_reads_unvouched_at_the_take(
    tmp_path, monkeypatch, caplog,
):
    """#2752's hole, closed, and now proven from the ROUND rather than the gate.

    The staging step disclosed a cut aimed at the 4149 Hz peak as VOUCHED. Moved
    0.143 octaves onto the 4582 Hz dip — inside the match tolerance, so the peak
    is still "a match" — the pre-#2752 take found the peak in its
    vouching-subset anchor, found no dip to outrank it, and reported it vouched
    anyway. Banking the WHOLE row set means the take asks the same question the
    staging gate asked and gets the same answer, which since 2026-08-23 is a
    COUNT rather than a refusal.

    The strong input, deliberately: a filter moved somewhere unclassified reads
    unvouched for a much cheaper reason, so it would look right even against the
    old subset. This one only reads unvouched because the dip is there.
    """
    _stage_driver(
        tmp_path, ordinal=9,
        filters=[_driver_cut(freq=_RECORD_PEAK_HZ, gain=-1.0)],
        classification=_driver_classification([
            _driver_verdict(_RECORD_PEAK_HZ),
            _driver_verdict(_RECORD_DIP_HZ, DEFECT_BOOSTABLE),
        ]),
    )
    envelope = json.loads(spool.prescription_spool_path().read_text())
    document = json.loads(envelope["document"])
    document["filters"][0]["freq"] = _RECORD_DIP_HZ
    payload = json.dumps(document).encode()
    _rewrite_envelope(
        document=payload.decode(),
        prescription_sha256=prescription_sha256(payload),
    )
    v2host.save_v2_state(_state_carrying_a_kept_round())

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        conductor = _prepare(monkeypatch)

    assert _refusal_slug(caplog) == ""
    assert conductor._prescribed_driver is not None
    assert conductor._prescribed_driver.unvouched_filters == 1
    assert conductor._prescribed_driver.classification_basis == ()


# --- the merge, at the site that consumes it -------------------------------- #


def _fitted(role: str, *, gain: float) -> dict[str, Any]:
    """One role's Layer-1a fit, in the shape the candidate carries it."""
    return LinearizationFit(
        role=role,
        filters=(LinearizationFilter("Peaking", 1400.0, 2.0, gain),),
        fit_band_hz=(200.0, 8000.0),
        target_level_db=0.0,
        residual_rms_db=0.5,
        residual_max_db=1.0,
        reason_summary={},
        mic_tier="reference",
        driver_class="cone",
        n_repeats=3,
    ).to_dict()


def _prescribed_round(monkeypatch, prescription: Any) -> SimpleNamespace:
    """Build ONE candidate on a session holding ``prescription``.

    The fit is substituted so both roles are fitted with KNOWN filters — the
    merge is only observable against a fit that had something to lose. What is
    NOT substituted is the merge or the recomposition: both run in
    ``crossover_v2.planning.build_candidate`` exactly as production reaches
    them.

    The substituted plan's own prediction is recomposed to match its
    substituted filters, so the fixture is self-consistent: ``planned`` is what
    an automatic round would have banked for THESE filters, and the candidate's
    state is what the prescribed round banks instead. Comparing the two is then
    a statement about the prescription rather than about the fixture.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    conductor = _conductor(fakes, driver_prescription=prescription)
    _run_phase(conductor, 1, 1)
    analysis = _eligible_measure_analysis(
        conductor.program_for_phase(flow.PHASE_MEASURE)
    )
    fit = {"woofer": _fitted("woofer", gain=-1.5),
           "tweeter": _fitted("tweeter", gain=-2.0)}
    real = conductor._plan_linearization(analysis, analysis.candidate, None)
    planned = dataclasses.replace(
        real,
        linearization=fit,
        linearized_predicted_sum=compose_linearized_prediction(
            real.summation_frame,
            filters_by_role=linearization_filters_by_role(fit),
            role_attenuations_db=real.role_attenuations_db,
        ),
    )
    monkeypatch.setattr(
        conductor, "_plan_linearization",
        lambda analysis, cand, cloud=None, *, candidate_sections=None: planned,
    )
    candidate, state = conductor._build_candidate(analysis, None)
    return SimpleNamespace(
        conductor=conductor, candidate=candidate, state=state,
        planned=planned, fit=fit,
    )


def _candidate_from_a_prescribed_round(
    monkeypatch, prescription: Any
) -> tuple[Any, dict[str, Any]]:
    """:func:`_prescribed_round`'s two most-asked fields, for the merge pins."""
    built = _prescribed_round(monkeypatch, prescription)
    return built.candidate, built.fit


def _candidate_from_a_failed_fit(
    monkeypatch, prescription: Any, *, cloud: Any = None,
) -> tuple[Any, Any]:
    """The SF2 degrade, carrying ``prescription``: the fit raised, the graph did not.

    The one arm where the fitted map and the merged one differ in KIND rather
    than in content — the fit linearized nothing, so every entry the candidate
    carries is prescribed and none of them knows which microphone measured.
    """
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    conductor = _conductor(fakes, driver_prescription=prescription)
    _run_phase(conductor, 1, 1)
    analysis = _eligible_measure_analysis(
        conductor.program_for_phase(flow.PHASE_MEASURE)
    )

    def _the_fit_engine_raises(analysis, cand, cloud=None, *, candidate_sections=None):
        raise ValueError("simulated fit engine bug")

    monkeypatch.setattr(conductor, "_plan_linearization", _the_fit_engine_raises)
    monkeypatch.setattr(
        conductor, "_exclusion_evidence_json", lambda cloud: {"filed": True}
    )
    return conductor._build_candidate(analysis, cloud)


def test_a_prescribed_role_replaces_its_own_fitted_filters(tmp_path, monkeypatch):
    """Merge-by-role, half one: the NAMED role is the document's, not the fit's.

    And it carries ``prescribed_by`` and no fit-quality field — a prescription
    has no ``fit_band_hz``, no residual and no ``reason_summary``, and emitting
    those zeroed would bank a claim nothing measured.
    """
    prescription, _payload = _stage_driver(tmp_path, ordinal=9)
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription
    assert taken == prescription  # the document a round would actually hold

    candidate, _fit = _candidate_from_a_prescribed_round(monkeypatch, taken)

    assert candidate.linearization["tweeter"]["filters"] == [
        {"biquad_type": "Peaking", "freq": TWEETER_FEATURE_HZ, "q": 5.0,
         "gain": -3.0},
    ]
    assert candidate.linearization["tweeter"]["prescribed_by"]["operator"] == "jasper"
    assert "fit_band_hz" not in candidate.linearization["tweeter"]


def test_an_unnamed_role_keeps_the_filters_the_fit_gave_it(tmp_path, monkeypatch):
    """Merge-by-role, half two: the role nobody prescribed is UNCHANGED.

    This is the half a wholesale replace would break silently — a prescriber
    correcting the tweeter would un-linearize the woofer without saying so —
    and it is asserted byte-for-byte against the fit's own dict rather than for
    mere presence, because a merge that dropped one key would still leave a
    woofer branch here.
    """
    prescription, _payload = _stage_driver(tmp_path, ordinal=9)
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription

    candidate, fit = _candidate_from_a_prescribed_round(monkeypatch, taken)

    assert candidate.linearization["woofer"] == fit["woofer"]
    assert set(candidate.linearization) == {"woofer", "tweeter"}


def test_a_round_with_no_per_driver_document_carries_the_fit_untouched(monkeypatch):
    """The control: no document, and the candidate is the fit's own map.

    Every ordinary round takes this path, so it is the one that must be
    byte-identical to the pre-PR-B shape.
    """
    candidate, fit = _candidate_from_a_prescribed_round(monkeypatch, None)

    assert candidate.linearization == fit


# --- the trim pin: a trim you name is not re-solved ------------------------- #


_PINNED_TWEETER_DB = -7.25


def _round_candidate(
    tmp_path, monkeypatch, *, pin: Any = None, failed_fit: bool = False,
    filters: Any = None, classification: dict[str, Any] | None = None,
) -> Any:
    """One taken document, built through the real ``build_candidate``.

    ``failed_fit`` takes the SF2 degrade instead of the fitted lane — the two
    assign ``role_attenuations_db`` at different points, which is the whole
    reason the pin folds above both rather than inside either. ``filters`` /
    ``classification`` reach the staged document so a caller can prescribe a
    boosting branch, whose headroom charge is what makes the pinned trim
    magnitude observable at all.
    """
    _stage_driver(
        tmp_path, ordinal=9, pinned_trim_db=pin,
        filters=filters, classification=classification,
    )
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription
    if failed_fit:
        return _candidate_from_a_failed_fit(monkeypatch, taken)[0]
    return _candidate_from_a_prescribed_round(monkeypatch, taken)[0]


@pytest.mark.parametrize("failed_fit", [False, True], ids=["fitted", "fit_failed"])
def test_a_pinned_trim_ships_and_an_unpinned_one_is_still_solved(
    tmp_path, monkeypatch, failed_fit,
):
    """The headline, on both lanes a candidate can take.

    A transplanted chain rides the trim it was shaped against; every role the
    document did not name keeps the value this round solved.
    """
    # Its own bundle per round: the packet fixture builds a real artifact tree
    # and cannot be laid down twice in one directory.
    pinned = _round_candidate(
        tmp_path / "pinned", monkeypatch,
        pin={"tweeter": _PINNED_TWEETER_DB}, failed_fit=failed_fit,
    )
    control = _round_candidate(
        tmp_path / "control", monkeypatch, failed_fit=failed_fit
    )

    assert pinned.role_attenuations_db["tweeter"] == _PINNED_TWEETER_DB
    assert control.role_attenuations_db["tweeter"] != _PINNED_TWEETER_DB
    # …and it is the number the compiler is handed, not just one on a receipt.
    assert pinned.driver_corrections()["tweeter"]["gain_db"] == _PINNED_TWEETER_DB
    # The woofer is named by neither document, so the round solved it both times
    # and the pin moved nothing it was not pointed at.
    assert (
        pinned.role_attenuations_db["woofer"]
        == control.role_attenuations_db["woofer"]
    )


def _round_state(tmp_path, monkeypatch, *, pin: Any = None) -> Any:
    """The linearization state ``build_candidate`` returned for one round."""
    _stage_driver(tmp_path, ordinal=9, pinned_trim_db=pin)
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription
    return _prescribed_round(monkeypatch, taken).state


def test_a_pinned_trim_clears_the_committed_pair_the_round_no_longer_ships(
    tmp_path, monkeypatch,
):
    """The pin replaces what ``decide_trim`` committed, so the record goes too.

    ``LinearizationState.trim_strategy`` is what the proposal quotes instead of
    guessing from ``outcome``; a pinned round ships the document's number, so a
    state still naming the solved pair would put a false commitment on the
    receipt — the 2026-08-10 defect's shape.
    """
    pinned = _round_state(
        tmp_path / "pinned", monkeypatch, pin={"tweeter": _PINNED_TWEETER_DB}
    )
    control = _round_state(tmp_path / "control", monkeypatch)

    assert control.trim_strategy is not None
    assert control.anchor_drift_db is not None
    assert pinned.trim_strategy is None
    assert pinned.anchor_drift_db is None
    assert pinned.outcome == control.outcome, (
        "the fit's verdict is unchanged; only the record of WHICH pair it "
        "committed goes with the trim the pin replaced"
    )


def test_a_pinned_trim_is_charged_headroom_at_the_value_that_ships(
    tmp_path, monkeypatch,
):
    """The charge describes the emitted chain, so it reads the PINNED trim.

    Folding the pin below the charge would disclose the household a cost
    computed against a trim the speaker never plays.
    """
    from jasper.active_speaker.branch_chain import branch_headroom_db
    from jasper.active_speaker.crossover_v2.planning import _sections_for_candidate

    candidate = _round_candidate(
        tmp_path, monkeypatch, pin={"tweeter": _PINNED_TWEETER_DB}
    )
    entry = candidate.linearization["tweeter"]

    assert entry["trim_pinned"] is True
    assert entry["headroom_cost_db"] == pytest.approx(
        branch_headroom_db(
            entry["filters"],
            sections=_sections_for_candidate(None, candidate.source_preset).get(
                "tweeter", ()
            ),
            trim_db=_PINNED_TWEETER_DB,
        ),
        abs=1e-9,
    )


def test_a_pinned_boost_branch_is_charged_headroom_at_the_pinned_trim(
    tmp_path, monkeypatch,
):
    """The fold-before-charge ordering, made observable.

    A cut-only branch charges 0.0 headroom at any non-positive trim, so it
    cannot tell a charge computed at the pinned trim from one at the trim the
    round solved. A BOOSTING branch peaks above unity, so its headroom carries
    the trim magnitude — and the pinned and control rounds, which ship different
    trims, are charged different numbers. If the pin folded BELOW the charge the
    pinned branch would be charged at the control's trim and the two would
    match; that they differ is the ordering holding.
    """
    from jasper.active_speaker.branch_chain import branch_headroom_db
    from jasper.active_speaker.crossover_v2.planning import _sections_for_candidate

    boost = [_driver_boost(gain=10.0)]
    cls = _driver_boostable()
    pinned = _round_candidate(
        tmp_path / "pinned", monkeypatch,
        pin={"tweeter": _PINNED_TWEETER_DB}, filters=boost, classification=cls,
    )
    control = _round_candidate(
        tmp_path / "control", monkeypatch, filters=boost, classification=cls,
    )
    p_entry = pinned.linearization["tweeter"]
    c_entry = control.linearization["tweeter"]
    sections = _sections_for_candidate(None, pinned.source_preset).get("tweeter", ())

    # The charge is computed at the PINNED trim, and the boost makes that a real
    # number this fixture actually spends.
    assert p_entry["headroom_cost_db"] == pytest.approx(
        branch_headroom_db(p_entry["filters"], sections=sections,
                           trim_db=_PINNED_TWEETER_DB),
        abs=1e-9,
    )
    assert p_entry["headroom_cost_db"] > 0.0
    assert c_entry["headroom_cost_db"] > 0.0
    # The mutation catch: charged at the pinned trim, not the trim the round
    # solved, so it does NOT equal the same branch's charge on the control round.
    assert control.role_attenuations_db["tweeter"] != _PINNED_TWEETER_DB
    assert p_entry["headroom_cost_db"] != pytest.approx(
        c_entry["headroom_cost_db"], abs=1e-6
    )


def test_the_receipt_discloses_a_pinned_trim_beside_what_the_round_measured(
    tmp_path, monkeypatch,
):
    """Disclose, never block — and against the value the pin actually DISPLACED.

    The round still solved the role, so the receipt carries what it would have
    shipped absent the pin and the gap beside the pinned value. That baseline is
    the trim THIS lane committed — which a no-pin control round ships for the
    same role — not the program-analysis trim, whose fitted-lane value is the
    pre-commit number and would misstate what the pin moved.
    """
    from jasper.active_speaker.crossover_v2.durable_state import _candidate_summary

    candidate = _round_candidate(
        tmp_path / "pinned", monkeypatch, pin={"tweeter": _PINNED_TWEETER_DB}
    )
    control = _round_candidate(tmp_path / "control", monkeypatch)
    displaced = control.role_attenuations_db["tweeter"]
    assert displaced != _PINNED_TWEETER_DB, "the fixture must have a gap to disclose"

    summary = _candidate_summary(candidate)
    disclosed = summary["trims_pinned"]
    assert set(disclosed) == {"tweeter"}
    assert disclosed["tweeter"]["pinned_db"] == _PINNED_TWEETER_DB
    # The exact value the round displaced, not the analysis trim.
    assert disclosed["tweeter"]["displaced_db"] == pytest.approx(displaced, abs=1e-12)
    assert disclosed["tweeter"]["delta_db"] == pytest.approx(
        _PINNED_TWEETER_DB - displaced, abs=1e-9
    )
    assert summary["trims_db"]["tweeter"] == _PINNED_TWEETER_DB


def test_a_reopened_candidate_still_discloses_its_pin(tmp_path, monkeypatch):
    """Republish makes a banked candidate live again and re-renders its summary.

    That is the path where a pin most misleads if it is lost: an old candidate
    shown again, with a level nothing measured worded as one that was.
    """
    from jasper.active_speaker.crossover_v2.durable_state import _candidate_summary
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
    )

    candidate = _round_candidate(
        tmp_path, monkeypatch, pin={"tweeter": _PINNED_TWEETER_DB}
    )

    reopened = MeasuredCrossoverCandidate.from_mapping(
        json.loads(json.dumps(candidate.to_dict()))
    )

    disclosed = _candidate_summary(reopened)["trims_pinned"]
    # Named, not merely equal to the original: two empty maps compare equal, so
    # a comparison alone would pass with the disclosure gone from both sides.
    assert set(disclosed) == {"tweeter"}
    assert disclosed == _candidate_summary(candidate)["trims_pinned"]
    assert reopened.role_attenuations_db["tweeter"] == _PINNED_TWEETER_DB


def test_an_unpinned_round_discloses_no_pin_at_all(tmp_path, monkeypatch):
    from jasper.active_speaker.crossover_v2.durable_state import _candidate_summary

    candidate = _round_candidate(tmp_path, monkeypatch)

    assert _candidate_summary(candidate)["trims_pinned"] == {}
    assert "trim_pinned" not in candidate.linearization["tweeter"]


def test_a_prescribed_boost_discloses_what_the_emitter_charges_for_it(
    tmp_path, monkeypatch,
):
    """#2759: the merge produces an entry with no charge on it, and a BOOST
    branch disclosing 0.0 under-states the maximum SPL the household gives up.

    Walked all the way to an emitted config, like its fitted twin
    (``test_crossover_v2_conductor
    .test_the_stamped_disclosure_equals_what_the_emitter_actually_charges``):
    the disclosure and the charge are asserted to be ONE number over the real
    preset, the committed trims and the emitted filters, rather than two that
    agree by inspection.
    """
    from jasper.active_speaker.camilla_yaml import (
        _branch_context, linearization_headroom_db,
    )

    _stage_driver(
        tmp_path, ordinal=9,
        filters=[_driver_boost()],
        classification=_driver_boostable(),
    )
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription
    assert taken.prescription_class == "boost", "the fixture must spend headroom"

    candidate, _fit = _candidate_from_a_prescribed_round(monkeypatch, taken)

    disclosed = worst_headroom_cost_db(candidate.linearization)
    assert disclosed > 0.0, "a prescribed boost disclosed as free is the defect"
    charged = linearization_headroom_db(
        linearization_filters_by_role(candidate.linearization),
        # The candidate's OWN corrections mapping — the one the emitter is
        # handed — rather than a gain map rebuilt here. A hand-built stand-in
        # would keep agreeing after the wiring that produces the real one
        # changed, which is the drift this pin exists to catch.
        branch_context=_branch_context(
            candidate.source_preset, candidate.driver_corrections()
        ),
    )
    assert disclosed == pytest.approx(charged, abs=1e-9)


# --- the prediction, which must model the graph that ships ------------------ #


def _max_divergence_db(left: Any, right: Any) -> float:
    """The worst dB gap between two ``(freqs, magnitude_db)`` predictions."""
    return float(np.max(np.abs(np.asarray(left[1]) - np.asarray(right[1]))))


def test_the_prediction_models_the_filters_that_will_actually_ship(
    tmp_path, monkeypatch,
):
    """SF1: a prescribed round's prediction is recomposed, not the fit's.

    ``plan_linearization``'s own invariant is that the persisted prediction is
    "a model of exactly what the emitted graph will do" — the #1668 PR-D fix,
    bought after a deterministic 1.688-1.699 dB VERIFY mismatch against a 1.5 dB
    tolerance. A prescription replaces the filters the graph carries AFTER that
    number is composed, so without recomposition the round would ship a model of
    a graph nobody emits.

    Two assertions, and they are the two halves of the claim:

    * against the fit's own prediction the recomposed one MOVES — by the
      prescribed-versus-fitted filter response, which for this fixture
      (prescribed 5000 Hz Q5 -3 dB against fitted 1400 Hz Q2 -2 dB) is a
      multi-dB gap, far past the 1.5 dB tolerance the precedent was about;
    * against a composition from the CANDIDATE'S OWN persisted map it does not
      move at all. That is the invariant stated as an equality: whatever the
      candidate carries is what the prediction models.
    """
    _prescription, _payload = _stage_driver(tmp_path, ordinal=9)
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription

    built = _prescribed_round(monkeypatch, taken)

    shipped = compose_linearized_prediction(
        built.planned.summation_frame,
        filters_by_role=linearization_filters_by_role(built.candidate.linearization),
        role_attenuations_db=built.planned.role_attenuations_db,
    )
    assert _max_divergence_db(built.state.linearized_predicted_sum, shipped) == 0.0
    assert _max_divergence_db(
        built.state.linearized_predicted_sum, built.planned.linearized_predicted_sum
    ) > 1.5


def test_a_round_with_no_document_banks_the_fits_prediction_unchanged(monkeypatch):
    """The control: no prescription, and the recomposition never runs.

    Byte-identical to the pre-SF1 path, which is what makes the movement above
    a statement about the document rather than about the recomposition existing.
    """
    built = _prescribed_round(monkeypatch, None)

    assert _max_divergence_db(
        built.state.linearized_predicted_sum, built.planned.linearized_predicted_sum
    ) == 0.0


def _walked_round(monkeypatch, prescription: Any) -> SimpleNamespace:
    """A conductor walked CHECK → MEASURE through the REAL fit and the REAL gate.

    No substitution at all — the fit engine runs, the merge runs, the
    recomposition runs, the accountability gate runs, and the proposal commits.
    ``graded`` is what the gate was handed, wrapped rather than replaced so the
    walk still behaves exactly as production's.
    """
    graded: list[Any] = []
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    conductor = _conductor(fakes, driver_prescription=prescription)
    real_gate = conductor._assert_accountable

    def _spy(predicted, raw, **kwargs):
        graded.append(predicted)
        return real_gate(predicted, raw, **kwargs)

    monkeypatch.setattr(conductor, "_assert_accountable", _spy)
    _run_phase(conductor, 1, 1)
    verdict = _run_phase(conductor, 2, 2)
    assert verdict["accepted"] is True
    return SimpleNamespace(conductor=conductor, graded=graded)


def test_the_three_consumers_of_the_prediction_see_the_prescribed_filters(
    tmp_path, monkeypatch,
):
    """…and the recomposed number reaches every surface that grades on it.

    Driven end-to-end twice over one fixture — once with the document, once
    without — because "the prescription reached this consumer" is exactly the
    difference between those two runs, and a recomposition that stopped at the
    state would leave the two identical.

    The three, by the three different names they read it under: the
    accountability pre-Apply grade takes it as an ARGUMENT, VERIFY tracking
    takes it through ``measure_predicted_sum``, and the delta probe's two axes
    carry it as the APPLIED side of ``commanded_delta`` / ``declared_transfer``.
    """
    _stage_driver(
        tmp_path, ordinal=9,
        # A cut the recomposed prediction still calls an improvement — see the
        # refusal test below for what happens when it is not, which is the point
        # of grading the shipping graph in the first place.
        filters=[_driver_cut(role="woofer", freq=WOOFER_FEATURE_HZ)],
    )
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription

    prescribed = _walked_round(monkeypatch, taken)
    automatic = _walked_round(monkeypatch, None)
    lhs, rhs = prescribed.conductor, automatic.conductor

    # The fixture really is a merge over a fit that had something to lose.
    assert "prescribed_by" in lhs.candidate.linearization["woofer"]
    assert "prescribed_by" not in rhs.candidate.linearization["woofer"]

    # (a) the accountability gate, on the value it was handed.
    assert _max_divergence_db(prescribed.graded[-1], lhs.measure_predicted_sum) == 0.0
    assert _max_divergence_db(prescribed.graded[-1], automatic.graded[-1]) > 0.0
    # (b) VERIFY tracking.
    assert _max_divergence_db(
        lhs.measure_predicted_sum, rhs.measure_predicted_sum
    ) > 0.0
    # (c) both delta-probe axes, each on its applied side.
    assert _max_divergence_db(
        lhs.measure_commanded_delta, rhs.measure_commanded_delta
    ) > 0.0
    assert _max_divergence_db(
        lhs._measure_declared_transfer, rhs._measure_declared_transfer
    ) > 0.0


def _gate_lines(caplog) -> list[str]:
    """Every pre-Apply prediction-gate line captured so far."""
    return [
        record.getMessage() for record in caplog.records
        if "event=correction.crossover_v2_prediction_gate" in record.getMessage()
    ]


def test_a_narrow_prescribed_cut_clears_the_prescribed_classs_own_bar(
    tmp_path, monkeypatch, caplog,
):
    """THE BAR RULING: the prescribed class is gated on NON-WORSENING, not 0.5 dB.

    This fixture is the one the gate measured: the document replaces a working
    tweeter fit with a single narrow high-Q cut, and the predicted pooled
    improvement is 0.152 dB. Against the FITTED bar
    (``PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`` = 0.5) that falls short — as
    would essentially every per-driver prescription, because a pooled-RMS
    figure is the wrong instrument for a narrow cut: the whole class would be
    written down as no improvement before its first hardware exercise rather
    than judged. (It was a REFUSAL when the ruling was made; the nanny
    burn-down turned both bars into ledger boundaries, and the ruling about
    which bar the class is measured against is what this test pins.)

    So a candidate carrying prescribed branches is asked only not to make the
    speaker WORSE. It arrives already carrying its own admission evidence — the
    classification verdict bar, the per-filter depth cap, the composed cap, and
    a digest proving the accepted bytes ran — and what adjudicates it is the
    measured round with its pre-registered keep/rollback.
    """
    _stage_driver(tmp_path, ordinal=9)  # the default: tweeter, 5 kHz, Q5, -3 dB
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription

    gate_logger = "jasper.active_speaker.crossover_v2_flow"
    with caplog.at_level(logging.INFO, logger=gate_logger):
        built = _walked_round(monkeypatch, taken)
        prescribed_lines = _gate_lines(caplog)
        caplog.clear()
        _walked_round(monkeypatch, None)
        automatic_lines = _gate_lines(caplog)

    assert built.conductor.candidate is not None
    assert "prescribed_by" in built.conductor.candidate.linearization["tweeter"]
    # BOTH bars asserted from ONE run pair, because the ruling is a difference:
    # a branch that collapsed either way would leave one of these two wrong, and
    # asserting only the prescribed side would not notice the fitted class
    # quietly losing its own 0.5 dB.
    assert any("required_db=0.0" in line for line in prescribed_lines)
    assert not any("required_db=0.5" in line for line in prescribed_lines)
    assert any("required_db=0.5" in line for line in automatic_lines)
    assert not any("required_db=0.0" in line for line in automatic_lines)
    assert flow.PRESCRIBED_NON_WORSENING_DB == 0.0
    assert flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB == 0.5


def test_a_prescription_predicted_to_worsen_is_banked_and_measured_anyway(
    tmp_path, monkeypatch, caplog,
):
    """The floor under the ruling, after the nanny burn-down.

    This used to REFUSE, on the reasoning that "a model cannot settle whether a
    narrow cut helps, but it CAN settle that a proposal makes the prediction
    worse, and spending the household's speaker on that is worth refusing
    before measuring." That is the exact argument
    ``docs/measurement-loop-doctrine.md`` overrules — a prediction recommends
    and the measurement decides — and on 2026-08-22 this refusal, in this shape,
    stopped jts3's first prescribed-boost round at ``improvement_db=-0.703``
    one line after the same gate disclosed its own level estimators 11.635 dB
    apart. A model that cannot trust its inputs has not settled anything.

    So the non-worsening bar survives as a LEDGER boundary and the round
    proceeds to the measurement that decides. Both halves are asserted: the
    verdict is banked under the prescribed bar (``required_db=0.0``, which is
    what says WHICH bar judged it), and the candidate the round measures exists.

    **Mutation guard.** Restoring the refusal raises ``CaptureBeginRefused``
    out of ``_walked_round`` and fails this test at the walk.
    """
    _stage_driver(
        tmp_path, ordinal=9,
        filters=[_driver_cut(role="woofer", freq=WOOFER_FEATURE_HZ, gain=-12.0, q=1.0)],
    )
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription

    with caplog.at_level(
        logging.WARNING, logger="jasper.active_speaker.crossover_v2_flow"
    ):
        built = _walked_round(monkeypatch, taken)

    assert built.conductor.candidate is not None
    gate = [
        line for line in caplog.text.splitlines()
        if "event=correction.crossover_v2_prediction_gate" in line
    ]
    assert gate, caplog.text
    assert "reason=not_an_improvement" in gate[-1]
    # The deciding number on the wire says WHICH bar judged it — the prescribed
    # class's non-worsening floor, not the fitted class's 0.5 dB.
    assert "required_db=0.0" in gate[-1]
    # The control: the same session with no document also proceeds, so the
    # ledger line above is about the DOCUMENT rather than the fixture.
    assert _walked_round(monkeypatch, None).conductor.candidate is not None


# --- #2649's ceiling, which the merge must not take away ------------------- #

#: A grid that reaches the reference tier's taper zero (20 kHz since the
#: 2026-08-29 horn-droop correction ruling, exactly this grid's own top edge;
#: was ~16.4 kHz, comfortably past it), so a ceiling exists to be found at all.
_TRUST_GRID_HZ = np.geomspace(20.0, 20000.0, 400)


def test_a_document_naming_every_role_keeps_the_mic_trust_ceiling(
    tmp_path, monkeypatch,
):
    """#2649's ceiling survives a fully-prescribed candidate. THE BLOCKER.

    ``_mic_trust_ceiling_hz`` scavenges the linearization map for a ``mic_tier``
    and stops the delta probe grading above it. A merge that carried only
    ``filters`` + ``prescribed_by`` left a document naming EVERY role with no
    tier anywhere, so the ceiling silently became ``None`` and the probe graded
    a microphone nobody trusts — the exact defect #2649 closed (~90% of the
    squared error on the 2026-08-16 round).

    Asserted as EQUALITY against the same candidate built with no document, not
    merely as "not None": the tier is a fact about the microphone that measured
    the round, so replacing every filter must not move the ceiling one bin.
    """
    prescription, _payload = _stage_driver(
        tmp_path, ordinal=9,
        filters=[_driver_cut(), _driver_cut(role="woofer", freq=WOOFER_FEATURE_HZ)],
    )
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription
    assert set(taken.roles) == {"woofer", "tweeter"}  # every role this box has

    prescribed, _fit = _candidate_from_a_prescribed_round(monkeypatch, taken)
    automatic, _fit = _candidate_from_a_prescribed_round(monkeypatch, None)

    assert set(prescribed.linearization) == {"woofer", "tweeter"}
    assert all(
        "prescribed_by" in entry for entry in prescribed.linearization.values()
    )
    session = SimpleNamespace(_candidate=prescribed, session_id="s")
    ceiling = flow.CrossoverV2Session._mic_trust_ceiling_hz(session, _TRUST_GRID_HZ)
    baseline = flow.CrossoverV2Session._mic_trust_ceiling_hz(
        SimpleNamespace(_candidate=automatic, session_id="s"), _TRUST_GRID_HZ
    )

    assert baseline is not None
    assert ceiling == baseline


def test_a_prescribed_round_with_no_fit_says_the_ceiling_is_unavailable(
    tmp_path, monkeypatch, caplog,
):
    """…and when there genuinely is no tier, it is LOUD rather than silent.

    A prescription can land on a round whose fit was ineligible or failed, and
    then nothing in the candidate knows which microphone measured. ``None`` is
    still the answer — inventing a ceiling would be worse — but silence is not:
    it is indistinguishable from "the mic is trusted everywhere", which is what
    let the untrusted-HF grade happen unnoticed in the first place. That arm was
    silent before PR-B too, on every ineligible/failed round; extending the
    existing slug covers both.
    """
    _prescription, _payload = _stage_driver(tmp_path, ordinal=9)
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription

    candidate, _state = _candidate_from_a_failed_fit(monkeypatch, taken)
    session = SimpleNamespace(_candidate=candidate, session_id="s")

    with caplog.at_level(
        logging.WARNING, logger="jasper.active_speaker.crossover_v2_flow"
    ):
        ceiling = flow.CrossoverV2Session._mic_trust_ceiling_hz(
            session, _TRUST_GRID_HZ
        )

    assert ceiling is None
    assert "event=correction.crossover_v2_mic_trust_ceiling_unavailable" in caplog.text
    assert "reason=no_entry_recorded_a_mic_tier" in caplog.text
    # …and the instruction still landed, so this is a statement about the TIER
    # rather than about an empty candidate.
    assert set(candidate.linearization) == {"tweeter"}


def test_a_prescribed_branch_does_not_earn_the_fits_exclusion_evidence(
    tmp_path, monkeypatch,
):
    """A record of what the fit CONSUMED may not ride a correction it did not fit.

    ``exclusion_evidence`` is filed when the cloud envelope fed a fit, and the
    build's own rule is that it must not ride a candidate whose corrections came
    from the trims-only fallback instead. A prescribed branch is exactly such an
    elsewhere — so the test that decides it reads the FIT's map, never the merged
    one. Driven on the SF2 degrade, the one arm where the two maps differ:
    the fit raised, so it linearized nothing, and the candidate is non-empty only
    because a document was staged.
    """
    prescription, _payload = _stage_driver(tmp_path, ordinal=9)
    taken = spool.take_staged_prescription(
        round_ordinal=9, accepts=spool.STAGEABLE_KINDS
    ).prescription

    candidate, state = _candidate_from_a_failed_fit(
        monkeypatch, taken,
        cloud=CloudFitEvidence(excluded_bands_hz=(), band_spread=(), n_positions=3),
    )

    assert state.outcome == "fit_failed"
    assert candidate.exclusion_evidence == {}
    # …and the instruction still landed, which is what makes the assertion above
    # a statement about the two maps rather than about an empty candidate.
    assert set(candidate.linearization) == {"tweeter"}
    assert prescription.roles == ("tweeter",)
