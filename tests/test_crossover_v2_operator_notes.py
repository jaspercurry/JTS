# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The operator-notes quarantine: one artifact, one reader, no decisions.

Ticket 1.6 of ``docs/tuning-master-plan.md`` (invariant 8) and issue #2871.
The two halves are tested together because they are one contract: the artifact
decides WHERE prose lives, the packet block decides WHO reads it, and either
alone can look correct while the pair is broken.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker import design_draft as design_draft_module
from jasper.active_speaker.crossover_v2 import operator_notes as module
from jasper.active_speaker.crossover_v2.evidence_packet import (
    OPERATOR_NOTES_BLOCK,
    PACKET_SCHEMA_VERSION,
    build_crossover_evidence_packet,
)
from jasper.active_speaker.crossover_v2.operator_notes import (
    CARRIERS,
    OPERATOR_NOTES_KIND,
    OPERATOR_NOTES_PROVENANCE,
    OPERATOR_NOTES_SCHEMA_VERSION,
    build_operator_notes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

WAVEGUIDE = "190 mm R-OSSE waveguide, 110 degrees nominal coverage."
PER_DRIVER = "DE250 sits on the new waveguide, not the old horn."
LEGACY = "typed into a build that no longer offers this box"


# --------------------------------------------------------------------------- #
# fixtures — a draft, and the bundle the packet needs around it
# --------------------------------------------------------------------------- #


def _draft(
    *,
    build_notes: Any = WAVEGUIDE,
    driver_notes: Any = PER_DRIVER,
    legacy_notes: Any = LEGACY,
    driver_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A design draft carrying prose in all three of its carriers.

    Each carrier is separately suppressible with ``None`` so a test can assert
    on one without the other two answering for it.
    """
    driver: dict[str, Any] = {
        "target_id": "mono:tweeter",
        "role": "tweeter",
        "model": "DE250",
        # Non-prose declaration fields, present so the artifact has something
        # it must NOT copy.
        "measurement_band_hz": [1500.0, 18000.0],
        "required_protection_filters": [{"kind": "highpass", "cutoff_hz": 1200}],
    }
    if driver_notes is not None:
        driver["notes"] = driver_notes
    driver.update(driver_extra or {})
    draft: dict[str, Any] = {
        "operator_inputs": {"tweeter": "DE250"},
        "manual_settings": {"drivers": [
            driver,
            {"target_id": "mono:woofer", "role": "woofer", "model": "E150"},
        ]},
        "driver_research_request": {"targets": [{
            "target_id": "mono:tweeter",
            "operator_declared_context": {
                # A safety band beside the prose, ungated at this point in the
                # tree. The artifact must leave it alone.
                "measurement_band_hz": [999.0, 999.0],
            },
        }]},
    }
    if build_notes is not None:
        draft["operator_inputs"]["notes"] = build_notes
    if legacy_notes is not None:
        request_target = draft["driver_research_request"]["targets"][0]
        request_target["operator_declared_context"]["operator_notes"] = legacy_notes
    return draft


def _bundle(
    tmp_path: Path,
    draft: dict[str, Any] | None,
    state: dict[str, Any] | None = None,
) -> tuple[Path, Path | None, Path | None]:
    """A commissioning bundle, and the draft path beside it.

    Minimal on purpose: this block reads one file, and a fixture that also
    staged a receipt would let a test pass for a reason it did not name.
    """
    session = tmp_path / "session"
    round_dir = session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY"
    round_dir.mkdir(parents=True)
    (session / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": "c2a1812b849e",
        "fingerprints": {"build_sha": "200d54578"},
    }))
    draft_path = None
    if draft is not None:
        draft_path = tmp_path / "active_speaker_design_draft.json"
        draft_path.write_text(json.dumps(draft))
    state_path = None
    if state is not None:
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(state))
    return session, draft_path, state_path


def _packet(
    tmp_path: Path,
    draft: dict[str, Any] | None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session, draft_path, state_path = _bundle(tmp_path, draft, state)
    return build_crossover_evidence_packet(
        session, driver_draft_path=draft_path, state_path=state_path
    )


# --------------------------------------------------------------------------- #
# the artifact — prose in, prose out, nothing else
# --------------------------------------------------------------------------- #


def test_every_carrier_reaches_the_artifact_verbatim():
    """#2871 acceptance 1. Verbatim, per role, and labelled operator-declared.

    Mutation-checked: dropping ``build_notes`` from the artifact fails this and
    the packet-flow test below; dropping ``target_id`` from ``_DRIVER_ROW_FIELDS``
    fails only this one, which is the point — a note that names no driver is
    the failure mode the per-role assertion exists for.
    """
    artifact = build_operator_notes(_draft())

    assert artifact["available"] is True
    assert artifact["kind"] == OPERATOR_NOTES_KIND
    assert artifact["artifact_schema_version"] == OPERATOR_NOTES_SCHEMA_VERSION
    assert artifact["provenance"] == OPERATOR_NOTES_PROVENANCE
    assert artifact["build_notes"] == WAVEGUIDE
    assert artifact["drivers"] == [
        {"target_id": "mono:tweeter", "role": "tweeter", "notes": PER_DRIVER}
    ]
    assert artifact["declared_context"] == [
        {"target_id": "mono:tweeter", "operator_notes": LEGACY}
    ]


def test_a_driver_with_no_prose_produces_no_row_rather_than_an_empty_one():
    """The woofer in the fixture has no note. It must not appear at all."""
    rows = build_operator_notes(_draft())["drivers"]
    assert [row["role"] for row in rows] == ["tweeter"]


def test_a_legacy_context_row_with_no_target_omits_the_key(tmp_path):
    """Absent is absent for identity too, not ``target_id: null``.

    The legacy carrier is read off a request that predates the current schema,
    so its rows are the ones most likely to be missing a field — and a null
    that only APPEARS on the older carrier would read as "this note belongs to
    no driver" rather than "this record never said".
    """
    draft = _draft()
    del draft["driver_research_request"]["targets"][0]["target_id"]
    block = build_operator_notes(draft)["declared_context"]
    assert block == [{"operator_notes": LEGACY}]


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_prose_is_absent_not_empty(blank):
    """#2871 acceptance 2. No empty-string noise, in any carrier.

    Whitespace-only counts as nothing: the wizard posts a trimmed value, but a
    draft written by anything else can hold spaces, and a reader must not have
    to tell a blank string from a missing one.
    """
    artifact = build_operator_notes(
        _draft(build_notes=blank, driver_notes=blank, legacy_notes=blank)
    )
    assert artifact["available"] is False
    assert "build_notes" not in artifact
    assert "drivers" not in artifact
    assert "declared_context" not in artifact


def test_a_draft_with_no_prose_at_all_is_its_envelope_and_says_so():
    """The jts3-today case: the artifact is shape, not content."""
    artifact = build_operator_notes({})
    assert artifact["available"] is False
    assert set(artifact) == {
        "artifact_schema_version", "kind", "generated_by", "available",
        "provenance", "treat_as", "rule", "carriers", "excluded_prose",
        "redacted_fields",
    }


@pytest.mark.parametrize("needle", [
    pytest.param("1500", id="a-declared-measurement-band"),
    pytest.param("999", id="an-ungated-band-beside-the-legacy-prose"),
    pytest.param("highpass", id="a-declared-protection-filter"),
    pytest.param("E150", id="a-model-the-packet-publishes-elsewhere"),
])
def test_the_artifact_carries_prose_and_nothing_else(needle):
    """A prose artifact that also carried a band would be read as a declaration.

    The ``operator_declared_context`` band is the sharp case: it is ungated at
    that point in the tree, and a second copy of it inside a document labelled
    "operator-declared" is exactly how an unchecked number gets read as a
    checked one.
    """
    assert needle not in json.dumps(build_operator_notes(_draft()))


def test_an_unclaimed_prose_key_upstream_is_named_rather_than_dropped():
    """The allowlist tripwire: a fourth carrier cannot arrive in silence.

    Mutation-checked twice, each failing only this test: removing the
    ``_scan_prose_keys`` call, and widening ``_DRIVER_ROW_FIELDS`` to swallow
    the new key — the second matters because "just add it to the allowlist" is
    the fix a reader reaches for, and it must not silently satisfy the
    tripwire.

    The name is qualified by its owning record. The draft holds two
    ``drivers[]`` lists and a bare key would not say which one grew a field —
    the same ambiguity that produced this file's S1 defect.
    """
    artifact = build_operator_notes(
        _draft(driver_extra={"install_notes": "wired in reverse on purpose"})
    )
    assert artifact["redacted_fields"] == [
        "manual_settings.drivers[].install_notes"
    ]
    assert "wired in reverse" not in json.dumps(artifact)


def test_the_scan_looks_at_the_research_record_it_carries_nothing_from():
    """S1c. ``nothing is unclaimed`` must be a fact, not a blind spot.

    ``driver_research.drivers[]`` is live prose in the draft that this artifact
    deliberately does not gather. Before the scan reached it, a new prose field
    on that record could appear and no tripwire would fire — the companion
    at-rest test would have kept passing for the wrong reason.

    Mutation-checked twice, each failing only this test. Deleting the
    ``driver_research`` branch of the union in ``build_operator_notes`` is the
    blind spot this exists to hold open. Emptying ``_RESEARCH_DRIVER_CLAIMED``
    is the opposite error and matters as much: the scan then reports a key
    :data:`EXCLUDED_PROSE` deliberately names, turning a stated decision into a
    standing false alarm that teaches a reader to ignore the field.
    """
    draft = _draft()
    draft["driver_research"] = {"drivers": [
        {"role": "tweeter", "notes": "assistant summary", "fit_notes": "N-W"},
    ]}
    artifact = build_operator_notes(draft)

    # ``notes`` there is named in EXCLUDED_PROSE, so it is a decision, not a
    # miss — and a decision must not be re-reported as unclaimed.
    assert artifact["redacted_fields"] == [
        "driver_research.drivers[].fit_notes"
    ]
    # Neither string is gathered: the record is scanned, never carried.
    assert "assistant summary" not in json.dumps(artifact)
    assert "N-W" not in json.dumps(artifact)


def test_the_excluded_research_notes_row_names_its_owning_record():
    """S1b. The row exists, and its path says which ``drivers[]`` it means."""
    assert "driver_research.drivers[].notes" in module.EXCLUDED_PROSE
    assert all(
        path.split(".")[0] in {"driver_research", "driver_safety_profile"}
        for path in module.EXCLUDED_PROSE
    ), module.EXCLUDED_PROSE


def test_nothing_is_unclaimed_today():
    """The companion to the tripwire: it fires on a change, not at rest."""
    assert build_operator_notes(_draft())["redacted_fields"] == []


def test_each_carrier_reports_the_cap_its_source_actually_applies():
    """#2871 requirement 3, the "no second cap" half, made auditable.

    ``max_chars`` in the emitted table is a REPORT of the source's own cap. If
    the source moves and the table does not, the document tells a reader a
    length that is not enforced anywhere — so the numbers are read off the
    owning modules here rather than restated.

    Two of the three source caps are bare literals at a call site rather than
    named constants, so those halves match on source text. That makes them
    reformat-sensitive as well as value-sensitive; each carries its own message
    saying which it is, because a bare ``in`` assertion on a 200 KB module
    prints the whole file and tells a reader nothing.
    """
    driver_safety = (
        REPO_ROOT / "jasper/active_speaker/driver_safety.py"
    ).read_text()

    assert CARRIERS["drivers[].notes"]["max_chars"] == (
        design_draft_module.MAX_DRIVER_NOTE_CHARS
    ), "the per-driver cap moved; update CARRIERS to match design_draft"

    assert '"operator_inputs.notes",\n            max_chars=1000,' in driver_safety, (
        "driver_safety no longer caps operator_inputs.notes at 1000 chars at "
        "that call site — either the cap moved (update CARRIERS['build_notes']"
        "['max_chars']) or the call was reformatted (update this anchor)"
    )
    assert CARRIERS["build_notes"]["max_chars"] == 1000

    assert (
        "operator_declared_context.operator_notes\",\n                max_chars=2048,"
        in driver_safety
    ), (
        "driver_safety no longer caps operator_declared_context.operator_notes "
        "at 2048 chars at that call site — either the cap moved (update "
        "CARRIERS) or the call was reformatted (update this anchor)"
    )
    assert CARRIERS["declared_context[].operator_notes"]["max_chars"] == 2048


def test_the_new_kind_satisfies_the_namespacing_rule_that_landed_under_it():
    """2.8's ruling (#2873) landed while this branch was open, so it applies.

    A **new** artifact kind must name its owner as ``jts_<owner>_<name>``. The
    21 kinds written before the rule are grandfathered; this one is not, so it
    is checked against the real validator rather than against a restatement of
    the shape.
    """
    from jasper.audio_measurement.bundles import validate_artifact_kind

    assert validate_artifact_kind(OPERATOR_NOTES_KIND) == OPERATOR_NOTES_KIND
    assert OPERATOR_NOTES_KIND.startswith("jts_crossover_v2_")


def test_the_one_carrier_with_an_ambiguous_author_says_so():
    """"Declared" is not "typed", and only one carrier has to make that split.

    ``manual_settings.drivers[].notes`` predates the single Build-notes field
    and can hold text the research assistant wrote and the operator pasted —
    ``driver_safety``'s own comment at the request builder says so, and nothing
    on the record separates the two after the fact. Publishing ``authored_by:
    operator`` for that row would be a provenance the draft cannot support.
    """
    assert CARRIERS["build_notes"]["authored_by"] == "operator"
    assert CARRIERS["declared_context[].operator_notes"]["authored_by"] == "operator"
    assert CARRIERS["drivers[].notes"]["authored_by"] == (
        "operator_or_research_assistant_indistinguishable"
    )
    assert "may contain either operator prose or an imported" in (
        REPO_ROOT / "jasper/active_speaker/driver_safety.py"
    ).read_text()


def test_every_carrier_declares_the_same_four_facts():
    """One row shape, so a reader never has to ask why a row is shorter."""
    for name, row in CARRIERS.items():
        assert set(row) == {"source", "max_chars", "authored_by", "note"}, name


def test_the_artifact_never_truncates_what_the_source_let_through():
    """A second cap would mean two answers to one question, shorter one wins."""
    long_note = "w" * design_draft_module.MAX_DRIVER_NOTE_CHARS
    artifact = build_operator_notes(_draft(driver_notes=long_note))
    assert artifact["drivers"][0]["notes"] == long_note


# --------------------------------------------------------------------------- #
# the packet block — the reader, fenced
# --------------------------------------------------------------------------- #


def test_the_packet_points_at_its_own_quarantine(tmp_path):
    """``privacy`` names the block, and the name RESOLVES to the artifact.

    Following the pointer is stronger than sharing a string constant with the
    dict key: a shared constant survives the block being renamed out from
    under it, and this does not.

    Mutation-checked: renaming the packet's key to ``operator_context`` while
    leaving :data:`OPERATOR_NOTES_BLOCK` alone fails this test first, with a
    ``KeyError`` naming the block that went missing rather than a diff.
    """
    packet = _packet(tmp_path, _draft())
    named = packet["privacy"]["operator_prose_quarantined_to"]
    assert named == OPERATOR_NOTES_BLOCK
    assert packet[named]["kind"] == packet["privacy"]["operator_prose_kind"]
    assert packet[named]["kind"] == OPERATOR_NOTES_KIND


def test_the_draft_to_packet_flow_carries_every_carrier(tmp_path):
    """#2871 acceptance 4. The flow, pinned end to end so it cannot orphan.

    Deliberately asserted on the WIRE end. The unit test above proves the
    builder gathers the prose; this proves the packet still has it after
    assembly, and the two together are what a silent orphan would have to
    defeat twice.
    """
    block = _packet(tmp_path, _draft())[OPERATOR_NOTES_BLOCK]
    assert block["build_notes"] == WAVEGUIDE
    assert block["drivers"][0]["notes"] == PER_DRIVER
    assert block["declared_context"][0]["operator_notes"] == LEGACY


def test_the_two_absences_are_not_merged(tmp_path):
    """No draft passed, versus a draft nobody typed into: different fixes."""
    no_draft = _packet(tmp_path / "a", None)[OPERATOR_NOTES_BLOCK]
    assert no_draft["available"] is False
    assert no_draft["status"] == "not_evaluated"
    assert no_draft["reason"] == "no driver design draft was supplied"

    silent = _packet(
        tmp_path / "b", _draft(build_notes=None, driver_notes=None, legacy_notes=None)
    )[OPERATOR_NOTES_BLOCK]
    assert silent["available"] is False
    assert silent["reason"] == "field_null"


def test_household_prose_stays_excluded_while_operator_prose_is_carried(tmp_path):
    """The two are different populations, and only one of them travels.

    The state file is load-bearing and was missing when this test was first
    written: with no state supplied, ``withheld_state_fields`` is empty because
    there was nothing to withhold, so the assertion passed whatever
    ``_STATE_WITHHELD`` said. Emptying that tuple passed all 27 tests. Supplying
    household copy is what makes the boundary real.

    Mutation-checked after the fix: ``_STATE_WITHHELD = ()`` now fails here.
    """
    packet = _packet(tmp_path, _draft(), state={
        "household_findings": [{"household_copy": "my flat, second bedroom"}],
    })
    assert packet["privacy"]["household_prose_excluded"] is True
    assert packet["privacy"]["withheld_state_fields"] == ["household_findings"]
    # The household's words are gone from the whole document…
    assert "my flat, second bedroom" not in json.dumps(packet)
    # …and the operator's travelled, in that same document.
    assert packet[OPERATOR_NOTES_BLOCK]["build_notes"] == WAVEGUIDE


def test_the_added_block_does_not_move_the_packet_schema_version(tmp_path):
    """An added block leaves every v1 field saying what it said."""
    packet = _packet(tmp_path, _draft())
    assert packet["artifact_schema_version"] == PACKET_SCHEMA_VERSION == 1


# --------------------------------------------------------------------------- #
# information, never instruction — the part that must be provable
# --------------------------------------------------------------------------- #


def test_prose_changes_nothing_in_the_packet_but_the_prose(tmp_path):
    """#2871 acceptance 3, proved by behaviour rather than by grep.

    If ANY code path derived a bound, a threshold or a branch from these
    strings, some other field of the document would move when they change.
    None does. ``packet_fingerprint`` is the one field that must move and is
    excluded by name: it is a content hash of the whole document, so it
    identifies the briefing rather than reading it — a prescription written
    against a packet the operator has since re-typed is genuinely stale.

    Mutation-checked both ways, and both fail this test and nothing else:
    copying one prose string into the ``session`` block, and branching a
    verdict on it (``if "waveguide" in build_notes: crossover_region refuses``).
    The weaker mutation is the one worth naming — a copy is what a well-meaning
    change would actually look like, and it is caught too.
    """
    quiet = _packet(tmp_path / "a", _draft(build_notes=None, driver_notes=None,
                                           legacy_notes=None))
    loud = _packet(tmp_path / "b", _draft())

    ignore = {OPERATOR_NOTES_BLOCK, "packet_fingerprint"}
    assert {k: v for k, v in quiet.items() if k not in ignore} == {
        k: v for k, v in loud.items() if k not in ignore
    }
    assert quiet["packet_fingerprint"] != loud["packet_fingerprint"]


def test_the_prose_gatherer_has_exactly_one_production_caller():
    """The quarantine's import-graph half: one reader, and it is the packet.

    Walks every shipped module's import statements rather than grepping text,
    so a module that imports the gatherer under an alias is still caught.

    Two edges this pair does not close, named rather than left for a reader to
    find. A dynamic ``importlib.import_module("...operator_notes")`` is not an
    ``Import``/``ImportFrom`` node, so this walk misses it; and a
    ``from ...evidence_packet import OPERATOR_NOTES_BLOCK`` followed by
    ``packet[OPERATOR_NOTES_BLOCK]`` reaches the block without ever spelling
    the literal the companion greps for. Both are theoretical — neither
    appears in the tree — and both are defeated anyway by
    ``test_prose_changes_nothing_in_the_packet_but_the_prose``, which does not
    care HOW a consumer reached the strings, only that the document moves when
    they do. That behavioural test is the real guard; these two are the cheap
    ones that name the offender.
    """
    importers = set()
    for path in sorted((REPO_ROOT / "jasper").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("operator_notes"):
                    importers.add(path.relative_to(REPO_ROOT).as_posix())
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("operator_notes"):
                        importers.add(path.relative_to(REPO_ROOT).as_posix())
    assert importers == {
        "jasper/active_speaker/crossover_v2/evidence_packet.py",
    }


def test_no_shipped_module_reads_the_packets_operator_notes_block():
    """The other half: nothing subscripts the block out of an assembled packet.

    A grep rather than an import walk, because a consumer would reach the
    strings through the packet dict, not through this module — the two tests
    together cover both routes into the prose.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "jasper").rglob("*.py")):
        # The assembly pair itself: the gatherer and the block that embeds it.
        if path.name in {"evidence_packet.py", "operator_notes.py"}:
            continue
        text = path.read_text()
        if any(n in text for n in ('"operator_notes"', "'operator_notes'")):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    # driver_safety and design_draft name the DRAFT's legacy key while
    # validating and demoting it; neither reads the packet block, and neither
    # branches on the text. Named explicitly so a new offender stands out.
    assert offenders == [
        "jasper/active_speaker/design_draft.py",
        "jasper/active_speaker/driver_safety.py",
    ]
