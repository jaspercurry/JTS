# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The orientation verb: where a speaker stands, from the doors' own readers.

Three properties are pinned here, and they fail in different ways:

* **it is a reader** — ``status`` writes nothing, and in particular does not
  consume the staged prescription it reports;
* **it is not a second tree-walker** — every fact it prints comes from the
  builder and the named packet readers the three doors already call, so a
  monkeypatched builder moves the status output;
* **it hands the human to THIS speaker** — every printed URL derives from the
  configured hostname, because a speaker is ``jts3.local`` as readily as
  ``jts.local`` and the wrong one resolves to a different box in silence.

The bundle, design draft and classification fixtures are imported from the two
suites that already own those shapes rather than rebuilt, so a change to the
real on-disk shape reaches this suite too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.cli import crossover_prescriber as cli

from tests.test_crossover_v2_blend_prescription import _bundle, _receipt
from tests.test_crossover_v2_driver_prescription import (
    TWEETER_BAND,
    WOOFER_BAND,
    _classification,
    _draft,
    applied_profile,
)


@pytest.fixture(autouse=True)
def _isolated_spool(tmp_path: Path):
    """No test may see, or leave, a document in the real speaker's slot."""
    spool.set_prescription_spool_path_for_tests(tmp_path / "spool" / "pending.json")
    (tmp_path / "spool").mkdir()
    yield
    spool.set_prescription_spool_path_for_tests(None)


@pytest.fixture(autouse=True)
def _known_hostname(monkeypatch):
    """Every test names the speaker, so none of them reads the laptop's env."""
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")


def _speaker_dirs(
    tmp_path: Path,
    *,
    draft: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
) -> tuple[Path, Path | None]:
    """A bundle on disk, plus the design draft path when one was asked for."""
    session, _ = _bundle(tmp_path)
    if classification is not None:
        round_dir = next((session / "evidence/v1/artifacts/crossover_v2").iterdir())
        (round_dir / "feature_classification.json").write_text(
            json.dumps(classification)
        )
    draft_path = None
    if draft is not None:
        draft_path = tmp_path / "draft.json"
        draft_path.write_text(json.dumps(draft))
    return session, draft_path


def _status(argv: list[str], capsys) -> tuple[int, dict[str, Any]]:
    """Run the verb in its JSON shape and hand back the code and the payload."""
    code = cli.main(["status", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _report(argv: list[str], capsys) -> tuple[int, str]:
    """Run the verb in its human shape and hand back the code and stdout."""
    code = cli.main(["status", *argv])
    return code, capsys.readouterr().out


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --------------------------------------------------------------------------- #
# 1. the four sections
# --------------------------------------------------------------------------- #


def test_a_fully_evidenced_speaker_reports_all_four_states(tmp_path, capsys):
    """Declared, banked, staged and applied, each from its own evidence."""
    session, draft = _speaker_dirs(
        tmp_path, draft=_draft(), classification=_classification()
    )
    applied = tmp_path / "applied-profile.json"
    applied.write_text(
        json.dumps(applied_profile(blend=[{"freq": 1000.0}]))
    )

    code, payload = _status(
        [
            str(session), "--drivers", str(draft),
            "--applied-profile", str(applied),
        ],
        capsys,
    )

    assert code == cli.EXIT_OK
    # declared — the design draft's own per-role bands, narrowed by the
    # protective corners each target declares.
    assert payload["declared"]["available"] is True
    assert payload["declared"]["roles"] == ["tweeter", "woofer"]
    assert payload["declared"]["passbands_hz"] == {
        "tweeter": list(TWEETER_BAND),
        "woofer": list(WOOFER_BAND),
    }
    # banked — the round, its region, and its classified features.
    assert payload["banked"]["available"] is True
    assert payload["banked"]["round_id"] == "r1"
    assert payload["banked"]["region"]["available"] is True
    assert payload["banked"]["classification"]["n_verdicts"] == 2
    # staged — nothing placed in the slot by this test.
    assert payload["staged"]["pending"] is False
    # applied — both records the packet keeps side by side.
    assert payload["applied"]["from_round_receipt"] == {
        "available": True, "n_filters": 0
    }
    assert payload["applied"]["from_applied_profile"] == {
        "available": True, "n_filters": 1
    }


def test_the_report_prints_one_line_per_state_and_the_next_actions(tmp_path, capsys):
    """The human shape, on stdout, where the SSH operator reading it will be."""
    session, _ = _speaker_dirs(tmp_path)

    code, out = _report([str(session)], capsys)

    assert code == cli.EXIT_OK
    for label in ("speaker:", "declared:", "banked:", "staged:", "applied:", "next:"):
        assert label in out
    assert out.strip(), "the report is this verb's whole product"


def test_the_json_and_the_report_say_the_same_sentence(tmp_path, capsys):
    """One wording per fact. Two would let the terminal and the pipe disagree."""
    session, _ = _speaker_dirs(tmp_path)

    _, payload = _status([str(session)], capsys)
    _, out = _report([str(session)], capsys)

    for name in ("declared", "banked", "staged", "applied"):
        assert payload[name]["summary"] in out
    for action in payload["next_actions"]:
        assert action in out


def test_an_absence_carries_the_reason_the_packet_gave_for_it(tmp_path, capsys):
    """``source_absent`` and "not supplied" are different facts, both kept."""
    session, _ = _speaker_dirs(tmp_path)

    _, payload = _status([str(session)], capsys)

    # No draft was passed at all — the packet's own words for that.
    assert payload["declared"]["available"] is False
    assert payload["declared"]["reason"] == "no driver design draft was supplied"
    # The artifact could have been banked in the bundle and was not.
    assert payload["banked"]["classification"]["reason"] == "source_absent"


def test_an_empty_incumbent_is_not_a_missing_one(tmp_path, capsys):
    """A prescription is a TOTAL, so "empty" and "unknown" must not merge.

    The synthetic round records ``incumbent: []`` — the round derived from
    nothing. Reported as zero filters rather than as an absence, because an
    author told "none" would prescribe the same document either way and only
    one of the two readings is safe.
    """
    session, _ = _speaker_dirs(tmp_path)

    _, payload = _status([str(session)], capsys)

    assert payload["applied"]["from_round_receipt"]["available"] is True
    assert payload["applied"]["from_round_receipt"]["n_filters"] == 0
    # …while the side that genuinely was not supplied says so.
    assert payload["applied"]["from_applied_profile"]["available"] is False
    assert (
        payload["applied"]["from_applied_profile"]["reason"]
        == "no applied baseline profile was supplied"
    )


def test_the_packet_discloses_the_trim_the_round_re_solved(tmp_path, capsys):
    """F-5: the automatic per-driver trim re-solves every round, unannounced.

    Blind-run-v3 measured it drifting -1.361 -> -2.105 -> -3.103 dB on a
    physically unchanged speaker; the only place the size of a re-solve
    showed up was the receipt's ``delta_db``, and only after a prescriber
    already pinned it. Here the packet — evidence read BEFORE that decision —
    carries both halves, so a next-round prescription can be written with the
    drift already in view.
    """
    session, state_path = _bundle(
        tmp_path,
        state={"candidate": {"trims_db": {"tweeter": -2.105}, "trims_pinned": {}}},
    )
    applied = tmp_path / "applied-profile.json"
    applied.write_text(
        json.dumps(applied_profile(corrections={"tweeter": {"gain_db": -1.361}}))
    )

    code = cli.main([
        "packet", str(session),
        "--state", str(state_path),
        "--applied-profile", str(applied),
    ])
    out, err = capsys.readouterr()

    assert code == cli.EXIT_OK
    trim = json.loads(out)["incumbent"]["trim"]["tweeter"]
    assert trim == {
        "applied_db": -1.361,
        "round_resolved_db": -2.105,
        "delta_db": pytest.approx(-2.105 - (-1.361)),
        "pinned_this_round": False,
    }
    # The same numbers reach the operator's terminal, not only the JSON.
    assert "trim tweeter: applied -1.36 dB, round resolved -2.10 dB" in err
    assert "-0.74 dB" in err
    assert "pinned" not in err


def _receipt_with_incumbent(session: Path, incumbent: Any) -> None:
    """Rewrite the banked receipt's incumbent to an arbitrary JSON value."""
    round_dir = next((session / "evidence/v1/artifacts/crossover_v2").iterdir())
    receipt = _receipt()
    receipt["round_measurements"]["blend"]["incumbent"] = incumbent
    (round_dir / "round_receipt.json").write_text(json.dumps(receipt))


def test_a_stray_reason_key_in_the_receipt_is_not_echoed_as_an_explanation(
    tmp_path, capsys
):
    """The incumbent block is the one value read VERBATIM out of a bundle.

    A receipt whose ``incumbent`` is some other object would otherwise have that
    object's ``reason`` key printed as though the packet builder had explained
    something. Only the builder's own absence shape is echoed.
    """
    session, _ = _speaker_dirs(tmp_path)
    _receipt_with_incumbent(session, {"unrelated": "junk", "reason": "STRAY-FIELD"})

    _, payload = _status([str(session)], capsys)
    _, out = _report([str(session)], capsys)

    assert payload["applied"]["from_round_receipt"]["reason"] == "not reported"
    assert "STRAY-FIELD" not in out


def test_a_receipt_that_mimics_the_absence_shape_is_a_recorded_residue(
    tmp_path, capsys
):
    """The half this layer CANNOT close, pinned so it stays a decision.

    A receipt carrying the builder's own ``status: not_evaluated`` shape is
    indistinguishable from one the builder wrote, because the packet does not
    record which of the two authored a given absence. Closing it belongs to the
    packet builder, not here. It is terminal output only — this verb gates
    nothing and no refusal reads this string — so it is recorded rather than
    papered over with an allowlist of the packet's own reason slugs, which
    would be a second copy of a vocabulary the packet owns and would not close
    it anyway (a crafted receipt can always claim a legal slug).
    """
    session, _ = _speaker_dirs(tmp_path)
    _receipt_with_incumbent(
        session, {"status": "not_evaluated", "reason": "FABRICATED-REASON"}
    )

    _, payload = _status([str(session)], capsys)

    assert payload["applied"]["from_round_receipt"]["reason"] == "FABRICATED-REASON"


# --------------------------------------------------------------------------- #
# 2. the same builders the doors read
# --------------------------------------------------------------------------- #


def test_the_status_reads_the_builder_the_doors_read(tmp_path, capsys, monkeypatch):
    """R11 structurally: move the builder, and the status moves with it.

    The three doors reach the evidence through this one name. A status verb
    with its own walk of the bundle would answer a slightly different question
    from the door beside it, and the day they disagreed the operator would
    believe the one that was not enforcing anything.
    """
    session, _ = _speaker_dirs(tmp_path)
    monkeypatch.setattr(
        cli,
        "build_crossover_evidence_packet",
        lambda *a, **k: {"session": {"round_id": "from-the-builder"}, "round":
                         {"available": True}},
    )

    _, payload = _status([str(session)], capsys)

    assert payload["banked"]["round_id"] == "from-the-builder"


@pytest.mark.parametrize(
    "reader, replacement, section, expected",
    [
        ("packet_region_band_hz", lambda p: (111.0, 222.0),
         lambda s: s["banked"]["region"]["band_hz"], [111.0, 222.0]),
        ("packet_driver_passbands_hz", lambda p: {"midrange": (300.0, 3000.0)},
         lambda s: s["declared"]["roles"], ["midrange"]),
        ("packet_feature_classifications", lambda p: ("one", "two", "three"),
         lambda s: s["banked"]["classification"]["n_verdicts"], 3),
    ],
)
def test_each_state_comes_from_the_named_reader_its_gate_uses(
    tmp_path, capsys, monkeypatch, reader, replacement, section, expected
):
    """The packet owns its layout; this verb asks it the gate's own questions.

    ``packet_region_band_hz`` is what bounds a blend prescription,
    ``packet_driver_passbands_hz`` is what bounds a per-driver one, and
    ``packet_feature_classifications`` is what the per-driver gate's
    classification bar counts. Reading the blocks by hand instead would be a
    second opinion about where in the document those live.

    The third case is the one with a live consumer: the typed reader
    deliberately DROPS a row it cannot type, so a hand-rolled ``len(verdicts)``
    would count rows the gate will not honour and report a round as answerable
    when it is not. A block that grows columns the typed reader ignores makes
    the two numbers diverge silently, which is exactly when a status line is
    read instead of the gate.
    """
    session, _ = _speaker_dirs(tmp_path, draft=_draft(), classification=_classification())
    monkeypatch.setattr(cli, reader, replacement)

    _, payload = _status([str(session)], capsys)

    assert section(payload) == expected


def test_the_staged_state_comes_from_the_spools_own_predicate(
    tmp_path, capsys, monkeypatch
):
    """Not a ``.is_file()`` spelled again here — the spool owns that question."""
    session, _ = _speaker_dirs(tmp_path)
    monkeypatch.setattr(cli, "staged_prescription_pending", lambda: True)

    _, payload = _status([str(session)], capsys)

    assert payload["staged"]["pending"] is True


def test_the_staged_sentence_is_the_one_stage_itself_prints(tmp_path, capsys):
    """Two wordings of "what becomes of this file" would be two answers."""
    session, _ = _speaker_dirs(tmp_path)
    spool.prescription_spool_path().write_text("{}")

    _, payload = _status([str(session)], capsys)

    assert cli.STAGED_LIFECYCLE_NOTE in payload["staged"]["summary"]
    assert any(
        cli.STAGED_LIFECYCLE_NOTE in action for action in payload["next_actions"]
    )


# --------------------------------------------------------------------------- #
# 3. hostname-derived handoff URLs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hostname", ["jts.local", "jts3.local", "kitchen.local"])
def test_every_printed_url_follows_the_configured_hostname(
    tmp_path, capsys, monkeypatch, hostname
):
    """Product invariant 7. A hard-coded name sends its reader to another box.

    Silently, because ``jts.local`` usually resolves to something — which is
    why this asserts the absence of every other speaker's name as well as the
    presence of this one's.
    """
    monkeypatch.setenv("JASPER_HOSTNAME", hostname)
    session, _ = _speaker_dirs(tmp_path)

    _, payload = _status([str(session)], capsys)
    _, out = _report([str(session)], capsys)

    assert payload["speaker"]["hostname"] == hostname
    assert payload["speaker"]["crossover_url"] == f"http://{hostname}/sound/crossover/"
    assert payload["speaker"]["declaration_url"] == f"http://{hostname}/sound/setup/"
    assert f"http://{hostname}/sound/crossover/" in out
    for other in {"jts.local", "jts3.local", "kitchen.local"} - {hostname}:
        assert f"http://{other}" not in out


def test_a_speaker_with_no_declaration_is_handed_the_page_that_makes_one(
    tmp_path, capsys, monkeypatch
):
    """Every human handoff point, not just the one that runs rounds.

    ``--drivers`` is a path away for a speaker that HAS a design draft; a
    speaker that was never commissioned has no path to point harder at, and the
    page that writes the draft is the only useful thing to say to its operator.
    """
    monkeypatch.setenv("JASPER_HOSTNAME", "jts5.local")
    session, _ = _speaker_dirs(tmp_path, classification=_classification())

    _, payload = _status([str(session)], capsys)

    assert payload["declared"]["available"] is False
    assert any(
        "http://jts5.local/sound/setup/" in action
        for action in payload["next_actions"]
    )


def test_the_handoff_url_survives_an_unset_hostname(tmp_path, capsys, monkeypatch):
    """The documented default, not a crash: orientation must always answer."""
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    session, _ = _speaker_dirs(tmp_path)

    code, payload = _status([str(session)], capsys)

    assert code == cli.EXIT_OK
    assert payload["speaker"]["crossover_url"].startswith("http://jts.local/")


def test_the_handoff_page_is_the_one_the_household_actually_opens():
    """The path printed here is the path the correction hub links to.

    Two owners of "where does a human go for active-speaker tuning" would drift
    the day the page moves, and the CLI's copy is the one nobody would notice —
    a wizard tab that 404s is seen immediately, a printed URL is followed once
    by an operator who then assumes the speaker is broken. The CLI keeps its own
    constant rather than importing the web module (a CLI that depended on
    ``jasper.web`` to print a string would be the worse trade); this test is
    what makes the two one answer.
    """
    from jasper.web.correction_hub import SECTIONS

    hrefs = {key: href for key, _label, href in SECTIONS}
    assert cli.CROSSOVER_PAGE_PATH == hrefs["crossover"]


@pytest.mark.parametrize(
    "path", [cli.CROSSOVER_PAGE_PATH, cli.SOUND_SETUP_PAGE_PATH]
)
def test_every_path_this_tool_prints_is_a_route_nginx_serves(path):
    """A URL an operator cannot open is worse than no URL at all.

    They follow it once, get a 404, and conclude the speaker is broken. The
    deployed nginx config is the authority on what is actually reachable, so
    the paths this tool hands out are checked against it rather than against a
    second list somebody would have to remember to update.
    """
    conf = (
        Path(__file__).resolve().parents[1] / "deploy" / "nginx-jasper.conf"
    ).read_text()

    assert f"location {path} {{" in conf


# --------------------------------------------------------------------------- #
# 4. it writes nothing
# --------------------------------------------------------------------------- #


def test_the_status_verb_mutates_nothing_on_disk(tmp_path, capsys):
    """A verb an operator runs to orient must be safe to run at any moment."""
    session, draft = _speaker_dirs(
        tmp_path, draft=_draft(), classification=_classification()
    )
    before = _tree(tmp_path)

    code, _ = _report([str(session), "--drivers", str(draft)], capsys)

    assert code == cli.EXIT_OK
    assert _tree(tmp_path) == before


def test_reporting_a_staged_prescription_does_not_consume_it(tmp_path, capsys):
    """The one mutation this verb could plausibly cause, and must not.

    ``take_staged_prescription`` is the only reader of the document and it
    always consumes; a status verb that reached for it would spend the next
    round's instruction to print one line about it.
    """
    pending = spool.prescription_spool_path()
    pending.write_text('{"kind": "whatever"}')
    session, _ = _speaker_dirs(tmp_path)

    _status([str(session)], capsys)
    _, payload = _status([str(session)], capsys)

    assert payload["staged"]["pending"] is True
    assert pending.read_text() == '{"kind": "whatever"}'
    assert spool.staged_prescription_pending() is True


# --------------------------------------------------------------------------- #
# 5. next actions, from artifact dependencies
# --------------------------------------------------------------------------- #


def test_an_unreadable_bundle_still_reports_and_says_which_half_failed(
    tmp_path, capsys
):
    """A partial answer beats no answer, and the exit code stays honest.

    The spool lives on the speaker rather than in the bundle, so a prescription
    waiting for the next round is a fact whichever directory was named.
    """
    spool.prescription_spool_path().write_text("{}")

    code, payload = _status([str(tmp_path / "not-a-bundle")], capsys)

    assert code == cli.EXIT_EVIDENCE_UNREADABLE
    assert payload["packet_error"]
    assert payload["staged"]["pending"] is True
    assert payload["banked"]["available"] is False
    assert payload["banked"]["reason"] == payload["packet_error"]
    assert any("run a round at" in action for action in payload["next_actions"])


def test_a_missing_declaration_names_the_flag_that_supplies_it(tmp_path, capsys):
    """Each absence names the tool that would refuse for want of it."""
    session, _ = _speaker_dirs(tmp_path, classification=_classification())

    _, payload = _status([str(session)], capsys)

    assert any("--drivers" in action for action in payload["next_actions"])


def test_a_missing_classification_names_the_instrument_that_banks_it(
    tmp_path, capsys
):
    """A declared band alone answers half the per-driver question."""
    session, draft = _speaker_dirs(tmp_path, draft=_draft())

    _, payload = _status([str(session), "--drivers", str(draft)], capsys)

    assert any(
        "jasper-classify-features" in action for action in payload["next_actions"]
    )


def test_both_prescription_classes_are_offered_when_both_have_a_bound(
    tmp_path, capsys
):
    """The region bounds a blend document; the declared bands bound a driver one."""
    session, draft = _speaker_dirs(
        tmp_path, draft=_draft(), classification=_classification()
    )

    _, payload = _status([str(session), "--drivers", str(draft)], capsys)

    actions = payload["next_actions"]
    assert any("a blend prescription can be written" in a for a in actions)
    assert any("a per-driver prescription can be written" in a for a in actions)
    assert any("`stage` to leave it for the next round" in a for a in actions)


def test_a_round_with_no_region_says_a_blend_document_has_no_bound(
    tmp_path, capsys, monkeypatch
):
    """The refusal the blend gate would raise, named before it is paid for."""
    session, _ = _speaker_dirs(tmp_path)
    monkeypatch.setattr(cli, "packet_region_band_hz", lambda packet: None)

    _, payload = _status([str(session)], capsys)

    assert any(
        "no crossover region is banked" in action
        for action in payload["next_actions"]
    )
    assert not any(
        "a blend prescription can be written" in action
        for action in payload["next_actions"]
    )


def test_the_state_file_is_asked_for_only_when_it_was_not_supplied(tmp_path, capsys):
    """``stage`` hard-refuses without it, so an operator hears about it once."""
    session, _ = _speaker_dirs(tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"pre_apply_profile": {}}))

    _, without = _status([str(session)], capsys)
    _, with_state = _status([str(session), "--state", str(state)], capsys)

    assert any("--state" in action for action in without["next_actions"])
    assert not any("--state" in action for action in with_state["next_actions"])


# --------------------------------------------------------------------------- #
# 6. status_document — the value door behind the print door (W3-a)
# --------------------------------------------------------------------------- #

#: The literal contract, so this pin does not just compare ``status_document``
#: to itself. Both the CLI JSON below and the direct call below are built by
#: ``status_document`` — the CLI is a thin wrapper over it — so a set dropped
#: from ``status_document`` would vanish from BOTH sides of a same-keys
#: comparison and the pin would stay green. Only a literal expectation, held
#: nowhere near the code under test, has teeth against that.
_STATUS_DOCUMENT_KEYS = {
    "speaker",
    "packet_fingerprint",
    "packet_error",
    "declared",
    "banked",
    "staged",
    "applied",
    "next_actions",
}


def test_status_document_and_the_cli_json_carry_the_same_keys(tmp_path, capsys):
    """The value door W3-b will call agrees with the print door on shape.

    ``status_document`` takes the packet as a value rather than
    ``argparse.Namespace``, so a caller can hand it a packet it already
    built — via ``build_crossover_evidence_packet``, the same builder used
    here — without a second walk of the bundle. Structured keys only, never
    prose: a wording change in a section's ``summary`` must not be able to
    fail this.
    """
    session, draft = _speaker_dirs(
        tmp_path, draft=_draft(), classification=_classification()
    )

    _, cli_payload = _status([str(session), "--drivers", str(draft)], capsys)
    packet = cli.build_crossover_evidence_packet(
        session, state_path=None, driver_draft_path=draft
    )
    doc_payload = cli.status_document(packet, "", state_supplied=False)

    assert set(doc_payload) == _STATUS_DOCUMENT_KEYS
    assert set(doc_payload) == set(cli_payload)
    for name in ("declared", "banked", "staged", "applied"):
        assert set(doc_payload[name]) == set(cli_payload[name])
