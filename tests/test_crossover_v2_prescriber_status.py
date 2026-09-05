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

from jasper.active_speaker.crossover_alignment import POLARITY_INVERT
from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.active_speaker.crossover_v2 import round_inputs as round_inputs_mod
from jasper.active_speaker.crossover_v2.alignment_prescription import (
    ALIGNMENT_NO_CROSSOVER_REGION,
)
from jasper.active_speaker.crossover_v2.topology_prescription import (
    TOPOLOGY_NO_CROSSOVER_REGION,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
)
from jasper.active_speaker.seat_level_reference import (
    STATE_PATH_ENV as _SEAT_LEVEL_STATE_PATH_ENV,
)
from jasper.cli import crossover_prescriber as cli

from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.test_active_speaker_session_volume_plan import _bank_reference
from tests.test_crossover_v2_blend_prescription import _bundle, _receipt
from tests.test_crossover_v2_candidate_republish import _publish
from tests.test_crossover_v2_driver_prescription import (
    TWEETER_BAND,
    WOOFER_BAND,
    _classification,
    _draft,
    applied_profile,
)


#: Every test here builds a packet from a live session bundle with no
#: --state/--drivers/--applied-profile, so none may read whatever sits at the
#: on-Pi SSOT paths of the box running pytest.
pytestmark = pytest.mark.usefixtures("no_real_pi_paths")


@pytest.fixture(autouse=True)
def _isolated_spool(tmp_path: Path):
    """No test may see, or leave, a document in the real speaker's slot."""
    spool.set_prescription_spool_path_for_tests(tmp_path / "spool" / "pending.json")
    (tmp_path / "spool").mkdir()
    yield
    spool.set_prescription_spool_path_for_tests(None)


@pytest.fixture(autouse=True)
def bank(tmp_path, monkeypatch) -> Path:
    """The candidate bank this suite reads: never the one on the box running
    pytest, whose contents would move the report and the cycle it offers."""
    root = tmp_path / "sessions"
    monkeypatch.setattr(
        "jasper.active_speaker.bundles.sessions_dir", lambda: root
    )
    return root


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
    """Run the verb and hand back the code and the document it answered with."""
    code = cli.main(["status", *argv])
    return code, json.loads(capsys.readouterr().out)


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


def test_a_live_session_dir_is_built_from_the_resolvers_defaults(
    tmp_path, capsys, monkeypatch
):
    """Where the two declared inputs live is the shared resolver's answer.

    This CLI used to carry its own copy of the on-Pi paths, which is the
    duplication ``jasper-round-views`` could not consume: pointed at a live
    session directory with no overrides, the packet must be built from exactly
    what ``round_inputs`` resolved.

    The FLOW STATE is deliberately not among them: the host rewrites it as a
    round runs, so a defaulted state would move a rebuilt packet's fingerprint
    away from the one ``packet`` emitted and ``propose``/``stage`` judge
    against.
    """
    session, _ = _speaker_dirs(tmp_path)
    seen: dict[str, Any] = {}
    build = cli.build_crossover_evidence_packet

    def _spy(session_dir: Path, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs, session_dir=session_dir)
        return build(session_dir, **kwargs)

    monkeypatch.setattr(cli, "build_crossover_evidence_packet", _spy)
    _status([str(session)], capsys)  # no --state/--drivers/--applied-profile

    assert seen == {
        "session_dir": session,
        "state_path": None,
        "driver_draft_path": round_inputs_mod.DRIVERS_DEFAULT_PATH,
        "applied_profile_path": round_inputs_mod.APPLIED_PROFILE_DEFAULT_PATH,
        "repeat_floor_path": round_inputs_mod.REPEAT_FLOOR_DEFAULT_PATH,
        "declared_geometry_path": round_inputs_mod.DECLARED_GEOMETRY_DEFAULT_PATH,
    }


def test_the_reading_order_leads_with_the_docs_and_their_size(tmp_path, capsys):
    """Tier 0's front door (ADR-0204), sized so a reader can budget the read."""
    session, _ = _speaker_dirs(tmp_path)

    _, payload = _status([str(session)], capsys)

    order = payload["reading_order"]
    assert [entry["label"] for entry in order] == [
        label for label, _filename, _gives in cli._READING_ORDER
    ]
    for entry, (_label, filename, gives) in zip(order, cli._READING_ORDER):
        path = Path(entry["path"])
        assert path.name == filename
        assert entry["gives"] == gives
        # Both numbers are the file's own, not an estimate.
        assert entry["bytes"] == path.stat().st_size
        assert entry["lines"] == path.read_bytes().count(b"\n")


def test_the_reading_order_prefers_the_on_box_install_when_present(
    tmp_path, capsys, monkeypatch
):
    """Repo path by default (no /opt/jasper/docs here); the installed path
    once deploy/lib/install/python-runtime.sh's install_jasper() has put one
    there -- never both, never neither. The repo fallback is anchored to the
    package, not the CWD: run from anywhere, the resolved path is the
    checkout's own docs/, absolute. A name that resolves to no file at all
    carries no size rather than a guessed one."""
    session, _ = _speaker_dirs(tmp_path)

    monkeypatch.chdir(tmp_path)
    _, before = _status([str(session)], capsys)
    # Structural: a parents[N] shift after a file move fails here directly.
    assert (cli._REPO_DOCS_DIR / "tuning-methodology.md").is_file()
    assert before["reading_order"][0]["path"] == str(
        cli._REPO_DOCS_DIR / "tuning-methodology.md"
    )

    installed = tmp_path / "installed-docs"
    installed.mkdir()
    (installed / "tuning-methodology.md").write_text("x")
    monkeypatch.setattr(cli, "_INSTALLED_DOCS_DIR", installed)
    monkeypatch.setattr(cli, "_REPO_DOCS_DIR", tmp_path / "no-checkout-here")

    _, after = _status([str(session)], capsys)
    assert after["reading_order"][0]["path"] == str(
        installed / "tuning-methodology.md"
    )
    assert after["reading_order"][0]["bytes"] == 1
    # The other two were seeded into neither directory, so each carries the
    # bare identifier and no size -- one doc's absence never fakes another's.
    assert after["reading_order"][1]["path"] == "docs/tuning-operator-runbook.md"
    assert after["reading_order"][1]["bytes"] is None
    assert after["reading_order"][1]["lines"] is None


def test_an_absence_carries_the_reason_the_packet_gave_for_it(tmp_path, capsys):
    """``source_absent`` is the packet's own word, echoed rather than reworded."""
    session, _ = _speaker_dirs(tmp_path)

    _, payload = _status([str(session)], capsys)

    # --drivers was not passed, so its true default was read and found
    # unreadable — the same "source_absent" a badly-pointed explicit flag
    # would give, not a bespoke "not supplied" wording for the omitted case.
    assert payload["declared"]["available"] is False
    assert payload["declared"]["reason"] == "source_absent"
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
    # …while the side whose default path has nothing behind it says so.
    assert payload["applied"]["from_applied_profile"]["available"] is False
    assert payload["applied"]["from_applied_profile"]["reason"] == "source_absent"


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

    artifact = tmp_path / "packet.json"
    code = cli.main([
        "packet", str(session),
        "--state", str(state_path),
        "--applied-profile", str(applied),
        "--out", str(artifact),
    ])
    out, _ = capsys.readouterr()

    assert code == cli.EXIT_OK
    trim = json.loads(artifact.read_text())["incumbent"]["trim"]["tweeter"]
    assert trim == {
        "applied_db": -1.361,
        "round_resolved_db": -2.105,
        "delta_db": pytest.approx(-2.105 - (-1.361)),
        "pinned_this_round": False,
    }
    # The same numbers reach the caller's stdout, not only the artifact.
    assert json.loads(out)["trim"]["tweeter"] == trim


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

    assert payload["applied"]["from_round_receipt"]["reason"] == "not reported"
    assert "STRAY-FIELD" not in json.dumps(payload)


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


def test_a_banked_walk_is_visible_before_any_round_receipt_is():
    """The done-signal for a measurement-only walk.

    ``banked.available`` needs a ``round_receipt.json``, which is written only
    once a graded post-apply VERIFY completes — so a session that walked poses
    and applied nothing has no receipt and would otherwise report as empty. The
    packet's ``lateral_poses`` block is filled by accepted takes instead, and
    that is what an operator (or the driver polling this verb) is waiting for.
    """
    payload = cli.status_document(
        {"lateral_poses": {"available": True, "n_takes": 8,
                           "angles_deg": [-20, 0, 20]}},
        "",
        session_dir=None,
        evidence=[],
        state=None,
    )

    assert payload["banked"]["available"] is False
    assert payload["banked"]["walk"] == {
        "available": True,
        "n_takes": 8,
        "angles_deg": [-20, 0, 20],
        "elevations_deg": [],
        "reason": None,
    }


def test_a_raised_walk_publishes_its_elevations():
    """A walk off mark height carries its raises through; a flat one carries
    the one elevation it took."""
    raised = cli.status_document(
        {"lateral_poses": {"available": True, "n_takes": 2,
                           "angles_deg": [0], "elevations_deg": [0, 10]}},
        "",
        session_dir=None,
        evidence=[],
        state=None,
    )["banked"]["walk"]
    flat = cli.status_document(
        {"lateral_poses": {"available": True, "n_takes": 2,
                           "angles_deg": [0], "elevations_deg": [0]}},
        "",
        session_dir=None,
        evidence=[],
        state=None,
    )["banked"]["walk"]

    assert (raised["angles_deg"], raised["elevations_deg"], raised["n_takes"]) == (
        [0], [0, 10], 2,
    )
    assert (flat["angles_deg"], flat["elevations_deg"], flat["n_takes"]) == (
        [0], [0], 2,
    )


def test_the_bank_lists_what_a_walk_may_stage_and_offers_the_cycle(
    tmp_path, capsys, bank
):
    """The candidates an LLM may stage, and the one command that cycles them.

    Every banked candidate is listed with what tells it apart -- its corner and
    the alignment it was minted with -- and with the verdict the staging door
    will give it, which is the SEAM's own
    (``angle_capture.candidate_measure_axes``) rather than a second opinion
    spelled here. The cycle is offered over the measurable ones only, so a
    printed command cannot be one the door would refuse; below two there is
    nothing to compare.
    """
    plain = _candidate()
    flipped = _candidate(alignment=MeasuredCrossoverAlignment(
        delay_us=250.0, delay_role="tweeter", polarity=POLARITY_INVERT,
    ))
    unplayable = _candidate(linearization={"woofer": {"filters": []}})
    for index, candidate in enumerate((plain, flipped, unplayable)):
        _publish(bank, candidate, capture=f"capture-{index}")
    session, _ = _speaker_dirs(tmp_path)

    _, payload = _status([str(session)], capsys)

    listed = {
        record["fingerprint"]: record
        for record in payload["banked"]["candidates"]
    }
    assert set(listed) == {
        one.fingerprint for one in (plain, flipped, unplayable)
    }
    assert listed[flipped.fingerprint]["corner"] == {
        "between_roles": ["woofer", "tweeter"],
        "fc_hz": 1600.0,
        "filter_type": "Linkwitz-Riley",
        "slope_db_per_octave": 24.0,
    }
    assert listed[flipped.fingerprint]["alignment"] == {
        "polarity": POLARITY_INVERT,
        "delay_us": 250.0,
        "delay_role": "tweeter",
    }
    assert listed[plain.fingerprint]["measurable"] is True
    # A candidate carrying linearization EQ is listed with why, not hidden.
    assert listed[unplayable.fingerprint]["measurable"] is False
    assert listed[unplayable.fingerprint]["reason"]
    assert (
        "jasper-angle-capture stage --program tournament --candidates "
        f"{plain.fingerprint},{flipped.fingerprint}"
    ) in payload["next"]


def test_a_session_that_walked_nothing_says_so_rather_than_going_quiet():
    payload = cli.status_document(
        None, "no bundle here", session_dir=None, evidence=[], state=None
    )

    assert payload["banked"]["walk"]["available"] is False
    assert payload["banked"]["walk"]["reason"] == payload["packet_error"]


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


def test_a_spool_this_user_cannot_stat_is_disclosed_rather_than_raised(
    tmp_path, capsys, monkeypatch
):
    """Run as ``pi`` rather than root, the 0640 spool raises out of the stat.

    That used to be a raw traceback, which is the one thing an orientation verb
    may not do. The section now answers in the shape its three neighbours use —
    unavailable, with the reason — and ``pending`` is ``None`` rather than
    ``False``, because a prescriber told "nothing waiting" would stage over a
    document nobody could see.

    The spool's own predicate still raises: ``stage`` needs the real error, so
    the catch is this verb's, not the module's.
    """
    session, _ = _speaker_dirs(tmp_path)

    def _denied() -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "staged_prescription_pending", _denied)

    code, payload = _status([str(session)], capsys)

    assert code == cli.EXIT_OK, "a partial answer still beats no answer"
    assert payload["staged"]["available"] is False
    assert payload["staged"]["pending"] is None
    assert payload["staged"]["reason"] == cli.SPOOL_UNREADABLE_REASON
    # The command that WOULD answer, against the same directory.
    sudo = [command for command in payload["next"] if command.startswith("sudo ")]
    assert sudo == [f"{cli.ORIENTATION_COMMAND} {session}"]
    # …and nothing claims a document is or is not waiting.
    assert cli.STAGED_LIFECYCLE_NOTE not in payload["staged"]["summary"]


def test_the_staged_sentence_is_the_one_stage_itself_prints(tmp_path, capsys):
    """Two wordings of "what becomes of this file" would be two answers."""
    session, _ = _speaker_dirs(tmp_path)
    spool.prescription_spool_path().write_text("{}")

    _, payload = _status([str(session)], capsys)

    assert cli.STAGED_LIFECYCLE_NOTE in payload["staged"]["summary"]


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

    assert payload["speaker"]["hostname"] == hostname
    assert payload["speaker"]["crossover_url"] == f"http://{hostname}/sound/crossover/"
    assert payload["speaker"]["declaration_url"] == f"http://{hostname}/sound/setup/"
    for other in {"jts.local", "jts3.local", "kitchen.local"} - {hostname}:
        assert f"http://{other}" not in json.dumps(payload)


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
    assert payload["speaker"]["declaration_url"] == "http://jts5.local/sound/setup/"


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

    code, _ = _status([str(session), "--drivers", str(draft)], capsys)

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
    """A partial answer beats no answer, and it is still an answer.

    This verb accepts nothing and refuses nothing, so what it could not read is
    a FIELD — exit 0 with the sentence in ``packet_error`` — never a code that
    would oblige it to publish a refusal record instead of the orientation the
    caller ran it for. The spool lives on the speaker rather than in the
    bundle, so a prescription waiting for the next round is a fact whichever
    directory was named.
    """
    spool.prescription_spool_path().write_text("{}")

    code, payload = _status([str(tmp_path / "not-a-bundle")], capsys)

    assert code == cli.EXIT_OK
    assert payload["packet_fingerprint"] is None
    assert payload["packet_error"]
    assert payload["staged"]["pending"] is True
    assert payload["banked"]["available"] is False
    assert payload["banked"]["reason"] == payload["packet_error"]
    assert payload["speaker"]["crossover_url"].endswith("/sound/crossover/")
    # Nothing is offered that would fail for the reason this report already
    # gave: both round-reading commands read what this verb could not.
    assert payload["next"] == ["jasper-seat-level --mic-serial '<mic serial>'"]


def test_a_virgin_speaker_orients_with_no_session_dir_at_all(capsys):
    """The runbook's step 1 (Orient) must run before any session exists:
    ``session_dir`` is optional for ``status`` alone, so a virgin speaker with
    nothing banked yet still gets a report naming the gap, not a usage error.
    """
    code, payload = _status([], capsys)

    assert code == cli.EXIT_OK
    assert payload["packet_fingerprint"] is None
    assert payload["packet_error"]
    assert payload["banked"]["available"] is False
    assert payload["banked"]["reason"] == payload["packet_error"]
    # Nothing to run against a speaker with no session: the page that runs one
    # is the handoff, and this verb never invents a command it cannot spell.
    assert payload["next"] == ["jasper-seat-level --mic-serial '<mic serial>'"]


def test_a_missing_declaration_carries_the_reason_and_the_page_that_fixes_it(
    tmp_path, capsys
):
    """Each absence names why, and the human handoff that closes it."""
    session, _ = _speaker_dirs(tmp_path, classification=_classification())

    _, payload = _status([str(session)], capsys)

    assert payload["declared"]["available"] is False
    assert payload["declared"]["reason"] == "source_absent"
    assert payload["speaker"]["declaration_url"].endswith("/sound/setup/")


def test_drivers_and_applied_profile_are_true_defaults_not_documentation(
    tmp_path, capsys, monkeypatch
):
    """``--help`` used to print these on-Pi paths as though they were
    argparse defaults; they were not, so omitting either flag silently
    carried no evidence. Now the default IS read.
    """
    draft_path = tmp_path / "design_draft.json"
    draft_path.write_text(json.dumps(_draft()))
    applied_path = tmp_path / "applied-profile.json"
    applied_path.write_text(json.dumps(applied_profile(blend=[{"freq": 1000.0}])))
    monkeypatch.setattr(round_inputs_mod, "DRIVERS_DEFAULT_PATH", draft_path)
    monkeypatch.setattr(round_inputs_mod, "APPLIED_PROFILE_DEFAULT_PATH", applied_path)

    session, _ = _speaker_dirs(tmp_path, classification=_classification())
    _, payload = _status([str(session)], capsys)  # neither flag passed

    assert payload["declared"]["available"] is True
    assert payload["declared"]["roles"] == ["tweeter", "woofer"]
    assert payload["applied"]["from_applied_profile"] == {
        "available": True, "n_filters": 1
    }


def test_a_missing_classification_names_the_instrument_that_banks_it(
    tmp_path, capsys
):
    """A declared band alone answers half the per-driver question."""
    session, draft = _speaker_dirs(tmp_path, draft=_draft())

    _, payload = _status([str(session), "--drivers", str(draft)], capsys)

    # The BUNDLE that verb takes, resolved through the reader the packet used,
    # not the round tree this one was pointed at.
    bundle = round_inputs_mod.round_inputs(session).session_dir
    assert f"jasper-round-views classify-features {bundle}" in payload["next"]


def test_both_prescription_classes_are_offered_when_both_have_a_bound(
    tmp_path, capsys
):
    """The region bounds a blend document; the declared bands bound a driver one."""
    session, draft = _speaker_dirs(
        tmp_path, draft=_draft(), classification=_classification()
    )

    _, payload = _status([str(session), "--drivers", str(draft)], capsys)

    assert payload["banked"]["region"]["available"] is True
    assert payload["declared"]["available"] is True
    assert payload["banked"]["classification"]["available"] is True
    # The next verb, carrying the flag this report was read with: a rebuild
    # without it resolves --drivers against the machine and answers differently.
    assert payload["next"][0] == f"{cli.PROG} packet {session} --drivers {draft}"


def test_a_round_with_no_region_says_a_blend_document_has_no_bound(
    tmp_path, capsys, monkeypatch
):
    """The refusal the blend gate would raise, named before it is paid for."""
    session, _ = _speaker_dirs(tmp_path)
    monkeypatch.setattr(cli, "packet_region_band_hz", lambda packet: None)

    _, payload = _status([str(session)], capsys)

    assert payload["banked"]["region"] == {
        "available": False, "band_hz": None, "reason": "not reported",
    }



def _full_range_draft() -> dict[str, Any]:
    """A 1-way main's design draft: one declaration, no protective corner."""
    return {
        "kind": "jts_active_speaker_design_draft",
        "driver_safety_profile": {
            "kind": "jts_active_speaker_driver_safety_profile",
            "confirmation": {"confirmed_fingerprint": "abc", "method": "operator"},
            "targets": [
                {
                    "role": "full_range",
                    "measurement_band_hz": [45.0, 18000.0],
                    "hard_excitation_band_hz": [40.0, 20000.0],
                    "required_protection_filters": [],
                },
            ],
        },
    }


def _rebank_round_as_no_crossover(session: Path) -> None:
    """Re-bank one bundle's round as the round a 1-way main banks — through the
    production solver, so it cannot state a shape no round produces."""
    from jasper.active_speaker.crossover_v2.blend_correction import (
        solve_blend_correction,
    )
    from jasper.active_speaker.crossover_v2.evidence_packet import (
        round_artifact_dir,
    )
    from jasper.audio_measurement.program_analysis import (
        ABSOLUTE_NO_CROSSOVER_TOPOLOGY,
    )

    round_dir, _reason = round_artifact_dir(session)
    assert round_dir is not None
    path = round_dir / "round_receipt.json"
    receipt = json.loads(path.read_text())
    receipt["round_measurements"]["blend"] = solve_blend_correction(
        graded=None, band_hz=None, incumbent=(),
        no_crossover_reason=ABSOLUTE_NO_CROSSOVER_TOPOLOGY,
    ).to_dict()
    path.write_text(json.dumps(receipt))


def test_a_speaker_with_no_crossover_is_sent_to_the_one_door_it_has(
    tmp_path, capsys
):
    """Not "not yet" — "not ever", and the three shut doors say so by name.

    A 1-way main's blend, alignment and topology doors all describe a handoff
    between two branches; telling an operator no region "is banked" would send
    them back to a measurement for a band that cannot exist.
    """
    from jasper.audio_measurement.program_analysis import (
        ABSOLUTE_NO_CROSSOVER_TOPOLOGY,
    )

    session, draft = _speaker_dirs(
        tmp_path, draft=_full_range_draft(), classification=_classification()
    )
    _rebank_round_as_no_crossover(session)

    _, payload = _status([str(session), "--drivers", str(draft)], capsys)
    packet = cli.build_crossover_evidence_packet(
        session, state_path=None, driver_draft_path=draft
    )
    for door in ("alignment", "topology"):
        assert packet["request_time_prescriptions"][door]["available"] is False
    assert (
        packet["request_time_prescriptions"]["alignment"]["reason"]
        == ALIGNMENT_NO_CROSSOVER_REGION
    )
    assert (
        packet["request_time_prescriptions"]["topology"]["reason"]
        == TOPOLOGY_NO_CROSSOVER_REGION
    )
    not_evaluated = {e["field"]: e["reason"] for e in packet["not_evaluated"]}
    assert not_evaluated["crossover_region.band_hz"] == ABSOLUTE_NO_CROSSOVER_TOPOLOGY
    assert (
        not_evaluated["request_time_prescriptions.alignment"]
        == ALIGNMENT_NO_CROSSOVER_REGION
    )
    assert (
        not_evaluated["request_time_prescriptions.topology"]
        == TOPOLOGY_NO_CROSSOVER_REGION
    )

    assert payload["declared"]["roles"] == ["full_range"]
    # The SHAPE, not a measurement that has not happened yet.
    assert payload["banked"]["region"] == {
        **payload["banked"]["region"],
        "available": False,
        "reason": ABSOLUTE_NO_CROSSOVER_TOPOLOGY,
        "band_hz": None,
    }
    # …and the per-driver door, the only one this speaker has, is open.
    assert payload["declared"]["available"] is True
    assert payload["banked"]["classification"]["available"] is True


def test_the_state_file_is_asked_for_only_when_it_was_not_supplied(tmp_path, capsys):
    """``stage`` hard-refuses without it, so an operator hears about it once."""
    session, _ = _speaker_dirs(tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"applied": True}))

    _, without = _status([str(session)], capsys)
    _, with_state = _status([str(session), "--state", str(state)], capsys)

    # Runnable as printed: the flag carries the file that was named, and is
    # absent when none was — a placeholder path would refuse on the read.
    assert without["next"][0] == f"{cli.PROG} packet {session}"
    assert with_state["next"][0] == f"{cli.PROG} packet {session} --state {state}"


def test_the_banked_seat_level_reference_is_published_either_way(
    tmp_path, capsys, monkeypatch
):
    """A measurement session rides this level, so the number itself is reported.

    Absent one, every session rides a level nobody measured, and the tool that
    banks one is offered. ``_no_real_pi_paths`` already points the seat-level
    state path at a file that does not exist, so "not banked" is this suite's
    ambient default, same as ``--drivers`` above.
    """
    session, _ = _speaker_dirs(tmp_path)

    _, without = _status([str(session)], capsys)

    assert without["seat_level_reference_volume_db"] is None
    assert any("jasper-seat-level" in command for command in without["next"])

    path = tmp_path / "seat-level-reference.json"
    monkeypatch.setenv(_SEAT_LEVEL_STATE_PATH_ENV, str(path))
    _bank_reference(path, -9.0)

    _, banked = _status([str(session)], capsys)

    assert banked["seat_level_reference_volume_db"] == -9.0
    assert not any("jasper-seat-level" in command for command in banked["next"])


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
    "seat_level_reference_volume_db",
    "reading_order",
    "next",
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
    doc_payload = cli.status_document(
        packet, "", session_dir=None, evidence=[], state=None
    )

    assert set(doc_payload) == _STATUS_DOCUMENT_KEYS
    assert set(doc_payload) == set(cli_payload)
    for name in ("declared", "banked", "staged", "applied"):
        assert set(doc_payload[name]) == set(cli_payload[name])
