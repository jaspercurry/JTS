# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bass door: which members of one fitted family a round adopts.

The family is the sealed adapter's own, published through
:class:`~jasper.bass_extension.seat_fit.SeatFit` rather than retyped, so what
the gate checks a document against is what a real ``bass-fit`` writes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2.bass_prescription import (
    BASS_PRESCRIPTION_KIND,
    BASS_PRESCRIPTION_SCHEMA_VERSION,
    FIT_UNAVAILABLE,
    MARGIN_MISMATCH,
    OWNER_MISMATCH,
    OWNER_UNRESOLVED,
    PROTECTION_MISSING,
    TARGET_NOT_IN_FAMILY,
    BassPrescriptionRefused,
    bass_prescription_response_format,
    bass_prescription_to_candidate_fields,
    read_bass_prescription,
)
from jasper.active_speaker.crossover_v2.blend_prescription import (
    BLEND_PRESCRIPTION_MALFORMED,
    BLEND_PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_PACKET_MISMATCH,
    PRESCRIPTION_PROHIBITED_FIELD,
)
from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.active_speaker.state_paths import BASELINE_PROFILE_STATE_ENV
from jasper.bass_extension.alignment import lt_boost_db
from jasper.bass_extension.seat_fit import Rung, SeatFit
from jasper.cli import crossover_prescriber as cli

from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_crossover_v2_driver_prescription import _speaker, applied_profile
from tests.test_bass_extension_candidate_field import (
    MARGIN,
    PLANT,
    SHA,
    bass_family,
    ladder_evidence,
    limiter_evidence,
)

PACKET = "packet-fingerprint-abc123"
ROUND = "round-3"
DEEPEST = bass_family()[0]["target_id"]


def bass_fit_document(**overrides: Any) -> dict[str, Any]:
    """What ``jasper-round-views bass-fit`` writes, wrapper and all."""
    fit = asdict(SeatFit(
        adapter_id="sealed_v1",
        owner_role="woofer",
        owner_target_id="main:woofer",
        margin=MARGIN.name,
        effective_plant={
            "f0_hz": PLANT.f0_hz,
            "q0": PLANT.q0,
            "fit_rms_db": PLANT.fit_rms_db,
            "source": "seat_median_fit",
            "notes": [],
        },
        fit_refusal=None,
        rungs=tuple(
            Rung(
                target=target,
                max_listening_level=96,
                lt_boost_db=(
                    0.0 if target["target_id"] == "natural"
                    else lt_boost_db(PLANT.f0_hz, target["fp_hz"])
                ),
            )
            for target in bass_family()
        ),
        curve={"freqs_hz": [20.0, 100.0], "magnitude_db": [0.0, 0.0]},
        ceiling_hz=300.0,
        ceiling_source="declared",
        n_positions=5,
    ))
    fit.update(overrides)
    return {"status": "fitted", "bass_fit": fit}


def document(**overrides: Any) -> dict[str, Any]:
    raw = {
        "artifact_schema_version": BASS_PRESCRIPTION_SCHEMA_VERSION,
        "kind": BASS_PRESCRIPTION_KIND,
        "packet_fingerprint": PACKET,
        "prescriber": {"model": "a prescriber", "operator": "the owner"},
        "margin_policy_name": MARGIN.name,
        "targets": ["natural"],
        "rationale": "start at rest",
    }
    raw.update(overrides)
    return raw


def read(raw: dict[str, Any], **overrides: Any):
    kwargs: dict[str, Any] = {
        "packet_fingerprint": PACKET,
        "bass_fit": bass_fit_document(),
        "bass_fit_sha256": SHA,
        "round_id": ROUND,
        "ladder": {},
        "limiter": None,
        "expected_owner_role": "woofer",
    }
    kwargs.update(overrides)
    return read_bass_prescription(raw, **kwargs)


def test_a_natural_only_prescription_is_admitted_and_lands_on_a_candidate():
    prescription = read(document())

    assert prescription.target_ids == ("natural",)
    assert prescription.prescription_class == "bass"
    assert prescription.filters == ()
    assert prescription.rungs[0]["max_level_db"] == 0.0
    assert prescription.bass_fit_sha256 == SHA
    assert prescription.round_id == ROUND

    fields = bass_prescription_to_candidate_fields(prescription, owner_channels=(0, 1))
    candidate = _candidate(bass_extension=fields["bass_extension"])
    assert candidate.bass_extension["owner"] == {"role": "woofer", "channels": [0, 1]}
    assert candidate.bass_extension["basis"]["round_id"] == ROUND


def test_natural_is_adopted_even_when_the_document_forgets_it():
    prescription = read(document(targets=[]))

    assert prescription.target_ids == ("natural",)


def test_a_boosted_target_is_admitted_only_with_both_evidences_and_takes_their_level():
    prescription = read(
        document(targets=[DEEPEST, "natural"]),
        ladder={DEEPEST: ladder_evidence(-6.0)},
        limiter=limiter_evidence(),
    )

    assert prescription.target_ids == (DEEPEST, "natural")
    # CODE's number, off the evidence — never the document's.
    assert prescription.rungs[0]["max_level_db"] == -6.0
    assert prescription.rungs[0]["protection"]["limiter"] is not None
    assert prescription.rungs[-1]["max_level_db"] == 0.0

    fields = bass_prescription_to_candidate_fields(prescription, owner_channels=(2,))
    assert fields["bass_extension"]["rungs"][0]["max_level_db"] == -6.0


@pytest.mark.parametrize("evidence", (
    {"ladder": {}, "limiter": limiter_evidence()},
    {"ladder": {DEEPEST: ladder_evidence(-6.0)}, "limiter": None},
    {
        "ladder": {DEEPEST: ladder_evidence(-6.0, verdict="fail")},
        "limiter": limiter_evidence(),
    },
))
def test_a_boosted_target_without_both_passing_evidences_refuses(evidence):
    with pytest.raises(BassPrescriptionRefused) as refusal:
        read(document(targets=[DEEPEST]), **evidence)

    assert refusal.value.reason == PROTECTION_MISSING
    assert refusal.value.evidence["target_id"] == DEEPEST


@pytest.mark.parametrize("raw,gate,reason", (
    ({"targets": ["t99"]}, {}, TARGET_NOT_IN_FAMILY),
    ({"margin_policy_name": "normal"}, {}, MARGIN_MISMATCH),
    ({"margin_policy_name": "generous"}, {}, MARGIN_MISMATCH),
    ({"packet_fingerprint": "another packet"}, {}, PRESCRIPTION_PACKET_MISMATCH),
    ({"kind": "jts_room_prescription"}, {}, BLEND_PRESCRIPTION_MALFORMED),
    ({"artifact_schema_version": 99}, {}, "prescription_schema_unsupported"),
    ({"prescriber": {"model": "", "operator": ""}}, {},
     BLEND_PRESCRIPTION_PROVENANCE_MISSING),
    ({"volume_db": -10.0}, {}, PRESCRIPTION_PROHIBITED_FIELD),
    ({"unexpected": 1}, {}, BLEND_PRESCRIPTION_MALFORMED),
    ({"targets": "natural"}, {}, BLEND_PRESCRIPTION_MALFORMED),
    ({}, {"expected_owner_role": "tweeter"}, OWNER_MISMATCH),
    ({}, {"expected_owner_role": None}, OWNER_UNRESOLVED),
    ({}, {"bass_fit": None}, FIT_UNAVAILABLE),
    ({}, {"bass_fit": {"bass_fit": {"margin": MARGIN.name}}}, FIT_UNAVAILABLE),
    ({}, {"bass_fit_sha256": "not-a-digest"}, FIT_UNAVAILABLE),
    ({}, {"round_id": " "}, FIT_UNAVAILABLE),
))
def test_each_gate_refuses_under_its_own_name(raw, gate, reason):
    with pytest.raises(BassPrescriptionRefused) as refusal:
        read(document(**raw), **gate)

    assert refusal.value.reason == reason


def test_the_contract_and_the_gate_name_the_same_refusals():
    contract = bass_prescription_response_format()

    assert contract["required_top_level"]["kind"] == BASS_PRESCRIPTION_KIND
    assert PROTECTION_MISSING in contract["refusal_reasons"]
    assert contract["execution_boundary"]["model_may_execute"] is False


def test_an_ungated_prescription_cannot_reach_a_candidate_field():
    tampered = replace(read(document()), adapter_id="no_such_adapter")

    with pytest.raises(BassPrescriptionRefused) as refusal:
        bass_prescription_to_candidate_fields(tampered, owner_channels=(0,))

    assert refusal.value.reason == BLEND_PRESCRIPTION_MALFORMED


# --------------------------------------------------------------------------- #
# the CLI: one round's packet, the family beside it, one document back
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_round(tmp_path, monkeypatch):
    """A packet, the fitted family beside it, and the graph now playing."""
    packet = _speaker(tmp_path)
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    (tmp_path / "bass_fit.json").write_text(json.dumps(bass_fit_document()))
    applied = tmp_path / "applied-profile.json"
    applied.write_text(json.dumps(applied_profile(preset=_two_way_preset("mono"))))
    monkeypatch.setenv(BASELINE_PROFILE_STATE_ENV, str(applied))
    monkeypatch.setattr(
        spool, "prescription_spool_path", lambda: tmp_path / "staged.json"
    )
    return packet, packet_path


def _write(path: Path, **overrides: Any) -> str:
    path.write_text(json.dumps(document(**overrides)))
    return str(path)


def test_the_packet_declares_which_driver_owns_the_bass(cli_round):
    packet, _ = cli_round

    assert packet["drivers"]["bass_owner_role"] == "woofer"


def test_propose_answers_with_the_bass_candidate_field(cli_round, tmp_path, capsys):
    packet, packet_path = cli_round
    prescription = _write(
        tmp_path / "bass.json", packet_fingerprint=packet["packet_fingerprint"]
    )

    assert cli.main([
        "propose", "--packet", str(packet_path), "--prescription", prescription,
    ]) == cli.EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    assert answer["accepted"] is True
    assert answer["candidate_fields"] == ["bass_extension"]
    assert answer["prescription_class"] == "bass"
    receipt = json.loads(Path(answer["out"]).read_text())
    field = receipt["candidate_fields"]["bass_extension"]
    assert field["basis"]["round_id"] == packet["session"]["round_id"]
    assert field["owner"]["role"] == "woofer"
    assert [rung["target"]["target_id"] for rung in field["rungs"]] == ["natural"]


def test_propose_takes_the_family_from_the_named_file(cli_round, tmp_path, capsys):
    packet, packet_path = cli_round
    elsewhere = tmp_path / "other" / "bass_fit.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps(bass_fit_document(owner_role="tweeter")))
    prescription = _write(
        tmp_path / "bass.json", packet_fingerprint=packet["packet_fingerprint"]
    )

    assert cli.main([
        "propose", "--packet", str(packet_path), "--prescription", prescription,
        "--bass-fit", str(elsewhere),
    ]) == cli.EXIT_REFUSED

    assert json.loads(capsys.readouterr().out)["reason"] == OWNER_MISMATCH


def test_propose_refuses_a_boosted_target_with_no_protection(
    cli_round, tmp_path, capsys
):
    packet, packet_path = cli_round
    prescription = _write(
        tmp_path / "bass.json",
        packet_fingerprint=packet["packet_fingerprint"],
        targets=[DEEPEST],
    )

    assert cli.main([
        "propose", "--packet", str(packet_path), "--prescription", prescription,
    ]) == cli.EXIT_REFUSED

    answer = json.loads(capsys.readouterr().out)
    assert answer["reason"] == PROTECTION_MISSING
    assert answer["detail"]["evidence"]["target_id"] == DEEPEST


def test_the_bass_class_is_stageable(cli_round, tmp_path, capsys):
    packet, packet_path = cli_round
    prescription = _write(
        tmp_path / "bass.json", packet_fingerprint=packet["packet_fingerprint"]
    )
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"round_receipt": {"round_ordinal": 3}}))

    assert cli.main([
        "stage", "--packet", str(packet_path), "--prescription", prescription,
        "--state", str(state),
    ]) == cli.EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    assert answer["staged"] is True
    # The NEXT round's ordinal: a prescription answers one round's evidence
    # and instructs the one after it.
    assert answer["for_round_ordinal"] == 4
    # The fit is banked as the gate read it, minus the curve nothing re-gates.
    envelope = json.loads((tmp_path / "staged.json").read_text())
    assert "curve" not in envelope["bass_fit"]
    assert envelope["expected_owner_role"] == "woofer"
    taken = spool.take_staged_prescription(
        round_ordinal=4, accepts=spool.STAGEABLE_KINDS
    )
    assert taken.prescription_kind == BASS_PRESCRIPTION_KIND


def test_a_family_this_preset_cannot_bind_is_dropped_not_raised(caplog):
    """A staged family whose owner reaches no output on the graph being built
    is dropped with its reason logged: the candidate beside it is complete
    without it, and a round is not worth failing over an addition."""
    from jasper.active_speaker.crossover_v2 import planning
    from tests.test_crossover_v2_planner_wiring import _walked_to_measure

    conductor, analysis = _walked_to_measure()
    unbindable = replace(read(document()), owner_role="subwoofer")

    with caplog.at_level(logging.WARNING, logger=planning.logger.name):
        built, _state = planning.build_candidate(
            analysis, analysis.candidate,
            source_preset=conductor._preset,
            roles=(conductor._woofer.role, conductor._tweeter.role),
            plan=conductor._plan_linearization,
            exclusion_evidence=conductor._exclusion_evidence_json,
            journal=conductor._journal_linearization,
            bass_prescription=unbindable,
        )

    assert built.bass_extension == {}
    assert "event=crossover_v2.bass_prescription_dropped" in caplog.text
