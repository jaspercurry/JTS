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

from tests.test_crossover_v2_blend_prescription import _bundle
from tests.test_crossover_v2_driver_prescription import (
    TWEETER_BAND,
    WOOFER_BAND,
    _classification,
    _draft,
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
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"pre_apply_profile": {"blend_correction": [{"freq": 1000.0}]}})
    )

    code, payload = _status(
        [str(session), "--drivers", str(draft), "--state", str(state)], capsys
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
        == "no flow state file was supplied"
    )


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
    ],
)
def test_each_state_comes_from_the_named_reader_its_gate_uses(
    tmp_path, capsys, monkeypatch, reader, replacement, section, expected
):
    """The packet owns its layout; this verb asks it the gate's own questions.

    ``packet_region_band_hz`` is what bounds a blend prescription and
    ``packet_driver_passbands_hz`` is what bounds a per-driver one. Reading the
    blocks by hand instead would be a second opinion about where in the
    document those bounds live.
    """
    session, _ = _speaker_dirs(tmp_path, draft=_draft())
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
    assert f"http://{hostname}/sound/crossover/" in out
    for other in {"jts.local", "jts3.local", "kitchen.local"} - {hostname}:
        assert f"http://{other}" not in out


def test_the_handoff_url_survives_an_unset_hostname(tmp_path, capsys, monkeypatch):
    """The documented default, not a crash: orientation must always answer."""
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    session, _ = _speaker_dirs(tmp_path)

    code, payload = _status([str(session)], capsys)

    assert code == cli.EXIT_OK
    assert payload["speaker"]["crossover_url"].startswith("http://jts.local/")


def test_the_handoff_page_is_the_one_the_household_actually_opens(capsys):
    """The page that runs, applies and undoes a round is served at this path."""
    assert cli.CROSSOVER_PAGE_PATH == "/sound/crossover/"


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
