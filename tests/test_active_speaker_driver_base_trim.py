# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The banked measured per-driver BASE TRIM — record, re-key, and clear.

These pin the artifact's own envelope: what the apply seam may bank, what the
reader refuses, and the remediation an operator sees when the declaration has
moved under a banked trim. The seam that WRITES it lives in
``test_active_speaker_baseline_profile.py``; the profile's preference for this
record over the guided captures lives in ``test_active_speaker_level_match.py``.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from jasper.active_speaker import driver_base_trim as dbt

TWO_WAY = ("woofer", "tweeter")
THREE_WAY = ("woofer", "mid", "tweeter")


def _bank(tmp_path: Path, **overrides) -> Path:
    state = tmp_path / "base_trim.json"
    payload = dict(
        trims_db={"woofer": 0.0, "tweeter": -20.0},
        roles=TWO_WAY,
        speaker_group_ids=["mono"],
        declaration_fingerprint="a" * 64,
        trim_source="strict_measured_candidate",
        state_path=state,
    )
    payload.update(overrides)
    dbt.write_base_trim(**payload)
    return state


# ---------- the record: round-trip, envelope, re-key -------------------------


def test_write_then_load_round_trips_every_field(tmp_path: Path):
    state = _bank(tmp_path)
    record = dbt.load_base_trim(state_path=state)
    assert record is not None
    assert record["kind"] == dbt.BASE_TRIM_KIND
    assert record["artifact_schema_version"] == dbt.SCHEMA_VERSION
    assert record["trims_db"] == {"woofer": 0.0, "tweeter": -20.0}
    assert record["roles"] == ["woofer", "tweeter"]
    assert record["declaration_fingerprint"] == "a" * 64
    assert record["speaker_group_ids"] == ["mono"]
    assert record["trim_source"] == "strict_measured_candidate"
    assert record["measured_at"].endswith("Z")


@pytest.mark.parametrize(
    "write, why",
    [
        pytest.param(lambda p: None, "absent file", id="absent"),
        pytest.param(lambda p: p.write_text("{"), "unparseable", id="malformed"),
        pytest.param(
            lambda p: p.write_text(json.dumps({"kind": "something_else"})),
            "a document that is not ours",
            id="wrong_kind",
        ),
        pytest.param(
            lambda p: p.write_text(
                json.dumps({"kind": dbt.BASE_TRIM_KIND, "artifact_schema_version": 99})
            ),
            "a schema this build does not read",
            id="wrong_schema",
        ),
    ],
)
def test_load_is_absent_tolerant_and_never_raises(tmp_path: Path, write, why):
    state = tmp_path / "base_trim.json"
    write(state)
    assert dbt.load_base_trim(state_path=state) is None, why


@pytest.mark.parametrize(
    "overrides, reason",
    [
        pytest.param(
            {"trims_db": {"woofer": 0.0, "tweeter": 3.0}},
            dbt.REFUSE_NOT_ATTENUATION, id="a_boost",
        ),
        pytest.param(
            {"trims_db": {"woofer": 0.0, "tweeter": -80.0}},
            dbt.REFUSE_NOT_ATTENUATION, id="below_the_floor",
        ),
        pytest.param(
            {"trims_db": {"woofer": 0.0, "tweeter": float("inf")}},
            dbt.REFUSE_NOT_ATTENUATION, id="not_finite",
        ),
        pytest.param(
            {"trims_db": {"woofer": 0.0}},
            dbt.REFUSE_ROLES_INCOMPLETE, id="a_role_missing",
        ),
        pytest.param(
            {"trims_db": {"woofer": 0.0, "tweeter": -20.0, "mid": -3.0}},
            dbt.REFUSE_ROLES_INCOMPLETE, id="a_role_extra",
        ),
        pytest.param(
            {"declaration_fingerprint": ""},
            dbt.REFUSE_NO_DECLARATION, id="no_declaration",
        ),
        pytest.param({"trim_source": ""}, dbt.REFUSE_NO_TRIM_SOURCE, id="no_source"),
        pytest.param(
            {"speaker_group_ids": []},
            dbt.REFUSE_NO_SPEAKER_GROUP, id="no_speaker_group",
        ),
        pytest.param(
            {"speaker_group_ids": ["mono", 7]},
            dbt.REFUSE_NO_SPEAKER_GROUP, id="an_unreadable_group",
        ),
    ],
)
def test_write_refuses_a_record_its_own_reader_would_reject(
    tmp_path: Path, overrides, reason
):
    """A record the reader would refuse is a silent no-op dressed up as
    success, so the writer refuses it first — with a typed reason, never
    prose a caller has to parse."""
    with pytest.raises(dbt.DriverBaseTrimError) as excinfo:
        _bank(tmp_path, **overrides)
    assert excinfo.value.reason == reason


def test_a_positive_trim_is_refused_and_never_banked_as_a_boost(tmp_path: Path):
    """Attenuation-only BY CONSTRUCTION, and refused rather than clamped.

    No path reaching this writer can produce a positive per-role trim — both
    measured-candidate types refuse one at construction and
    ``attenuation_from_group_deltas`` normalizes its maximum to exactly 0 dB —
    so a positive value is a fault, and clamping it would bank a trim no
    measurement produced. Downstream, the reverse-null door's branch-gap depth
    ceiling and the per-role caps argument must stay two independent legs; a
    banked positive trim would couple them.
    """
    state = _bank(tmp_path)  # a good record is already on disk
    with pytest.raises(dbt.DriverBaseTrimError) as excinfo:
        _bank(tmp_path, trims_db={"woofer": 0.0, "tweeter": 0.1})
    assert excinfo.value.reason == dbt.REFUSE_NOT_ATTENUATION
    # Refused BEFORE the write: the good record is untouched, and no positive
    # value reached disk under any key.
    record = dbt.load_base_trim(state_path=state)
    assert record is not None
    assert record["trims_db"] == {"woofer": 0.0, "tweeter": -20.0}
    assert max(record["trims_db"].values()) <= 0.0


def test_the_record_carries_the_chain_the_trim_was_co_fitted_against(
    tmp_path: Path,
):
    """#3479: a trim is degenerate with the chain it was resolved WITH.

    A flat trim plus a shelf is the same branch-gain profile as a deeper flat
    trim and no shelf, so two banked scalars resolved against different chains
    are not two estimates of one quantity. The record therefore names the
    resolving candidate, and the reader hands that name back rather than
    re-deriving a frame it cannot see.
    """
    state = _bank(tmp_path, chain_fingerprint="c" * 64)
    record = dbt.load_base_trim(state_path=state)
    assert record is not None
    assert record["chain_fingerprint"] == "c" * 64
    _trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert meta["chain_fingerprint"] == "c" * 64


@pytest.mark.parametrize(
    "banked, why",
    [
        pytest.param(None, "the profile named no resolving candidate", id="absent"),
        pytest.param("", "an empty string names no chain", id="empty"),
        pytest.param("not-a-fingerprint", "not a fingerprint at all", id="malformed"),
        pytest.param(7, "not a string at all", id="numeric"),
    ],
)
def test_a_chain_nobody_can_name_is_banked_as_no_chain(
    tmp_path: Path, banked, why
):
    """Absent, never guessed. A frame the record cannot name must read as
    "no frame", so a later comparison discloses that it has no basis rather
    than treating two scalars as comparable."""
    state = _bank(tmp_path, chain_fingerprint=banked)
    record = dbt.load_base_trim(state_path=state)
    assert record is not None
    assert record["chain_fingerprint"] is None, why
    _trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert meta["chain_fingerprint"] is None


def test_a_tampered_chain_fingerprint_reads_as_no_chain(tmp_path: Path):
    """The reader validates the banked name on the way out too: a hand-edited
    state file must not be able to dress a trim in a frame it never had."""
    state = _bank(tmp_path, chain_fingerprint="c" * 64)
    record = json.loads(state.read_text())
    record["chain_fingerprint"] = "MiXeDcAsE" * 8
    state.write_text(json.dumps(record))
    trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    # The frame is unreadable; the TRIM is still the trim the graph is playing.
    assert trims == {"woofer": 0.0, "tweeter": -20.0}
    assert meta["status"] == dbt.STATUS_APPLIED
    assert meta["chain_fingerprint"] is None


def test_banked_trims_are_returned_when_the_declaration_matches(tmp_path: Path):
    state = _bank(tmp_path)
    trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert trims == {"woofer": 0.0, "tweeter": -20.0}
    assert meta["status"] == dbt.STATUS_APPLIED
    assert meta["declaration_fingerprint"] == "a" * 64
    assert meta["speaker_group_ids"] == ["mono"]
    # Derivable from `speaker_group_ids` by construction (post-#3388); the
    # one caller (`baseline_profile._measured_level_trims`) already computes
    # its own count rather than reading this one.
    assert "groups_total" not in meta
    assert meta["trim_source"] == "strict_measured_candidate"


@pytest.mark.parametrize(
    "fingerprint",
    [
        pytest.param("b" * 64, id="a_different_one"),
        pytest.param(None, id="none_at_all"),
        pytest.param("", id="empty"),
    ],
)
def test_a_stale_declaration_refuses_loudly_with_one_remediation(
    tmp_path: Path, fingerprint
):
    """Refused, and it SAYS so. A banked trim silently dropped is
    indistinguishable from a speaker that was never measured."""
    state = _bank(tmp_path)
    trims, meta = dbt.banked_base_trims(fingerprint, TWO_WAY, state_path=state)
    assert trims == {}
    assert meta["status"] == dbt.STATUS_DECLARATION_CHANGED
    assert meta["remediation"] == dbt.REMEASURE_REMEDIATION


def test_a_changed_role_set_refuses_rather_than_partly_applying(tmp_path: Path):
    state = _bank(tmp_path)
    trims, meta = dbt.banked_base_trims("a" * 64, THREE_WAY, state_path=state)
    assert trims == {}
    assert meta["status"] == dbt.STATUS_ROLES_CHANGED
    assert meta["remediation"] == dbt.REMEASURE_REMEDIATION


def test_absent_is_normal_and_says_so(tmp_path: Path):
    trims, meta = dbt.banked_base_trims(
        "a" * 64, TWO_WAY, state_path=tmp_path / "nothing.json"
    )
    assert (trims, meta["status"]) == ({}, dbt.STATUS_ABSENT)


@pytest.mark.parametrize(
    "trim, why",
    [
        pytest.param(4.0, "a hand-edited BOOST would make the speaker louder"),
        pytest.param(-999.0, "below the floor the solver clamps to"),
        pytest.param("loud", "not a number at all"),
        pytest.param(True, "a bool is not a level"),
    ],
)
def test_a_tampered_record_can_only_fail_closed(tmp_path: Path, trim, why):
    state = _bank(tmp_path)
    record = json.loads(state.read_text())
    record["trims_db"]["tweeter"] = trim
    state.write_text(json.dumps(record))
    trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert trims == {}, why
    assert meta["status"] == dbt.STATUS_UNUSABLE


@pytest.mark.parametrize(
    "field, value, why",
    [
        pytest.param("speaker_group_ids", 7, "a number cannot be walked for groups",
                     id="groups_numeric"),
        pytest.param("speaker_group_ids", "mono", "a string iterates into characters",
                     id="groups_string"),
        pytest.param("speaker_group_ids", ["left", None],
                     "one unreadable member poisons the whole set",
                     id="groups_partly_unreadable"),
        pytest.param("speaker_group_ids", None,
                     "no group is not 'levelled everywhere'", id="groups_none"),
        pytest.param("trim_source", None, "a trim with no named evidence",
                     id="source_missing"),
    ],
)
def test_a_record_that_cannot_name_its_footing_refuses(
    tmp_path: Path, field, value, why
):
    """``speaker_group_ids`` gates readiness
    (``crossover_contract.automatic_candidate_readiness``) and ``trim_source``
    is what a receipt discloses, so neither may be guessed."""
    state = _bank(tmp_path)
    record = json.loads(state.read_text())
    record[field] = value
    state.write_text(json.dumps(record))
    trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert trims == {}, why
    assert meta["status"] == dbt.STATUS_UNUSABLE
    assert meta["remediation"] == dbt.REMEASURE_REMEDIATION
    assert "speaker_group_ids" not in meta


def test_the_reader_names_every_group_the_record_covers(tmp_path: Path):
    """Readiness gates on the measured-group SET against the topology's
    required one, so a record that levelled only one cabinet of a stereo pair
    must not read as having levelled both."""
    state = _bank(tmp_path, speaker_group_ids=["right", "left"])
    _trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert meta["speaker_group_ids"] == ["left", "right"]


def test_clearing_drops_the_record_and_nothing_to_drop_is_success(
    tmp_path: Path, monkeypatch
):
    """The other half of single ownership: an apply with no measured level
    match must leave no trim behind for a walk to level by.

    ``True`` means "the box carries none", so a second clear still succeeds —
    an absent record is the state the clear exists to REACH. Only a record
    that survives the clear is a failure, which is what the apply seam logs.
    """
    state = _bank(tmp_path)
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(state))
    assert dbt.clear_base_trim() is True
    assert dbt.load_base_trim(state_path=state) is None
    assert dbt.clear_base_trim() is True


def test_a_record_that_survives_the_clear_is_reported_as_a_failure(
    tmp_path: Path, monkeypatch
):
    """A silently failed clear recreates the exact stale record the clear
    exists to prevent, so it must be distinguishable from nothing-to-drop."""
    state = _bank(tmp_path)
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(state))

    def _refuse(self, *args, **kwargs):
        raise PermissionError(13, "read-only")

    # A scoped context, not `monkeypatch.undo()`: the outer `monkeypatch`
    # fixture is shared with conftest's autouse isolations, and `.undo()`
    # would roll those back too for the rest of the test, not just this
    # one `Path.unlink` patch.
    with monkeypatch.context() as unlink_patch:
        unlink_patch.setattr(Path, "unlink", _refuse)
        assert dbt.clear_base_trim() is False
    assert dbt.load_base_trim(state_path=state) is not None


def test_the_state_path_honours_the_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(tmp_path / "elsewhere.json"))
    assert dbt.base_trim_state_path() == tmp_path / "elsewhere.json"
    assert dbt.base_trim_state_path("/explicit.json") == Path("/explicit.json")


# ---------- one vocabulary: no third estimator may grow here -----------------

_BAND_MATH_NAMES = {
    "mean", "average", "median", "log10", "log2", "sqrt", "trapz",
    "polyfit", "interp", "convolve", "fft", "rfft", "irfft", "welch",
}
_TRIM_ARTIFACT = "jasper/active_speaker/driver_base_trim.py"


def _band_math_calls(source: str) -> list[str]:
    """Every call in ``source`` whose name is band arithmetic."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else ""
        )
        if name in _BAND_MATH_NAMES:
            offenders.append(f"{node.lineno}: {name}()")
    return offenders


def test_the_base_trim_artifact_mints_no_band_averaging_of_its_own():
    """The one-vocabulary rule, made mechanical.

    The repo already carries three subordinate estimates of one physical
    quantity (the overlap-band point read, the branch power-band average, and
    the core-band median) and two live disclosures comparing them. A fourth
    would be the same defect again, so this file banks a level somebody else
    measured and may not do band arithmetic itself.
    """
    source = (
        Path(__file__).resolve().parents[1] / _TRIM_ARTIFACT
    ).read_text(encoding="utf-8")
    assert _band_math_calls(source) == []
    # The scanner fails both ways: it must actually SEE band math when band
    # math is there, or a clean result proves nothing about the file above.
    assert _band_math_calls("import numpy\nx = numpy.mean([1.0, 2.0])\n")


# ---------- durability, precision, and the declaration's shape ---------------


def test_the_banked_trim_is_the_one_the_graph_plays_not_a_rounded_one(
    tmp_path: Path,
):
    """``round(value, 1)`` banked a number no graph is playing.

    The v2 path's committed trim comes off numpy and is applied at full
    precision, so rounding on the way to disk recreated in miniature the very
    divergence this artifact exists to close -- the record and the graph
    disagreeing about what the speaker is doing. Rounding is a rendering
    concern and stays in the log line.
    """
    state = _bank(tmp_path, trims_db={"woofer": 0.0, "tweeter": -12.3456789})
    record = dbt.load_base_trim(state_path=state)
    assert record["trims_db"]["tweeter"] == -12.3456789
    trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert trims["tweeter"] == -12.3456789
    assert meta["status"] == dbt.STATUS_APPLIED


@pytest.mark.parametrize(
    "fingerprint",
    ["not-a-fingerprint", "A" * 64, "a" * 63, "a" * 65, "g" * 64],
    ids=["prose", "uppercase", "short", "long", "non-hex"],
)
def test_a_declaration_that_is_not_a_fingerprint_is_refused(
    tmp_path: Path, fingerprint
):
    """The reader keys on this string by EQUALITY, so a value that is not a
    fingerprint can never match anything -- banking one writes a record no
    reader can ever accept. Validated to the shape the six sibling artifacts
    already require of their own fingerprints."""
    with pytest.raises(dbt.DriverBaseTrimError) as caught:
        _bank(tmp_path, declaration_fingerprint=fingerprint)
    assert caught.value.reason == dbt.REFUSE_NO_DECLARATION
    assert not (tmp_path / "base_trim.json").exists()


def test_both_halves_of_the_record_survive_a_dirty_shutdown(
    tmp_path: Path, monkeypatch
):
    """The write and the clear are two halves of one apply, so both are
    durable. A power cut that keeps one but not the other leaves the box
    levelling by numbers its graph is not playing -- and an unlink is metadata,
    so a clear that never syncs its parent directory can be undone by the
    crash it is supposed to survive."""
    synced: list[int] = []
    real_fsync = os.fsync

    def _record(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _record)

    state = _bank(tmp_path)
    # A durable write syncs the tempfile AND the directory the rename
    # publishes into.
    assert len(synced) == 2
    synced.clear()

    assert dbt.clear_base_trim(state_path=state) is True
    # The unlink itself is metadata: one directory sync, no file to sync.
    assert len(synced) == 1
    assert dbt.load_base_trim(state_path=state) is None


def test_a_clear_with_no_directory_to_sync_is_still_a_success(tmp_path: Path):
    """Nothing to drop is SUCCESS, and the added durability must not change
    that: the box is left carrying no record either way, and reporting a
    failure here would log a banked trim surviving a clear that had nothing
    to clear."""
    assert dbt.clear_base_trim(state_path=tmp_path / "gone" / "base_trim.json") is True
