# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round's own restore target, and the provenance of "previous sound".

Issue #2537. On 2026-08-15 (jts3 cycle 4) a crossover-v2 restore faithfully put
back the graph the global applied-baseline-profile record named as "the previous
sound" — a candidate applied six and a half hours earlier, which an operator had
displaced out of band at 01:00 by reconciling the running config to
``sound_current.yml``. The restore MECHANICS were proven good that night. The
TARGET was wrong, because one fact had two owners.

Three claims are pinned here, and the first is the incident itself:

1. the exact cycle-4 shape — candidate applied, running config displaced out of
   band, restore fired — must REFUSE rather than resurrect the stale candidate;
2. the divergence rule reports "it moved" only when it actually compared two
   things, never when it could not;
3. a displaced applied-profile record stops being read as authoritative for
   "which graph is on the speaker".
"""

from __future__ import annotations

import hashlib

import pytest

from jasper.active_speaker.baseline_profile import (
    APPLIED_PROFILE_DISPLACED,
    APPLIED_PROFILE_PATH_UNKNOWN,
    APPLIED_PROFILE_RUNNING_UNKNOWN,
    applied_profile_displacement,
)
from jasper.active_speaker.crossover_v2.round_anchor import (
    ROUND_ANCHOR_STATE_KEY,
    round_anchor_record,
    running_config_diverged,
)


def _yaml(tmp_path, name: str, body: str):
    """One config file on disk, with its digest — what an anchor names."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path), hashlib.sha256(body.encode("utf-8")).hexdigest()


def _apply_payload(*, prior_path: str, applied_path: str, applied_sha: str):
    """``apply_baseline_profile``'s result, reduced to what the record reads."""
    return {
        "status": "applied",
        "apply": {"result": "success", "prior_config_path": prior_path},
        "profile": {"config": {"path": applied_path, "sha256": applied_sha}},
    }


# --------------------------------------------------------------------------- #
# 1. the record: what an apply displaced, and what it put live
# --------------------------------------------------------------------------- #


def test_the_record_names_the_graph_the_apply_displaced_and_the_one_it_applied(
    tmp_path,
):
    """``prior_config_path`` is the running graph at apply moment.

    It is CamillaDSP's own answer from immediately before the load, which is the
    one reading nothing out-of-band can have moved yet — the whole reason the
    round takes its own snapshot instead of asking a global record later.
    """
    prior_path, prior_sha = _yaml(tmp_path, "previous.yml", "devices: {}\n")
    applied_path, applied_sha = _yaml(tmp_path, "candidate.yml", "devices: {a}\n")

    record = round_anchor_record(
        _apply_payload(
            prior_path=prior_path,
            applied_path=applied_path,
            applied_sha=applied_sha,
        )
    )

    assert record["displaced"] == {"config_path": prior_path, "sha256": prior_sha}
    assert record["applied"] == {"config_path": applied_path, "sha256": applied_sha}


def test_round_n_plus_ones_displaced_is_exactly_round_ns_applied(tmp_path):
    """The chain the owner's iterate-and-learn series needs.

    "Round N+1's previous is exactly round N's result" is a property a single
    global "previous sound" record structurally cannot promise, because it has
    no idea a round is happening. Here it falls out of where the two facts are
    read from.
    """
    first_path, _ = _yaml(tmp_path, "round1.yml", "devices: {r1}\n")
    second_path, second_sha = _yaml(tmp_path, "round2.yml", "devices: {r2}\n")

    round_1 = round_anchor_record(
        _apply_payload(
            prior_path=str(tmp_path / "round0.yml"),
            applied_path=first_path,
            applied_sha="",
        )
    )
    round_2 = round_anchor_record(
        _apply_payload(
            prior_path=first_path,
            applied_path=second_path,
            applied_sha=second_sha,
        )
    )

    assert round_2["displaced"]["config_path"] == round_1["applied"]["config_path"]
    assert round_2["displaced"]["sha256"] == round_1["applied"]["sha256"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"apply": None, "profile": None},
        {"apply": {"prior_config_path": None}, "profile": {"config": {}}},
    ],
    ids=["empty", "null_halves", "null_paths"],
)
def test_an_apply_that_can_name_nothing_records_empty_identities(payload):
    """Both halves are always present, including as empty.

    An absent key would be a second way of saying "unknown" alongside the empty
    string, on a record a human reads off durable state.
    """
    record = round_anchor_record(payload)
    assert set(record) == {"displaced", "applied"}
    assert record["displaced"] == {"config_path": "", "sha256": ""}
    assert record["applied"] == {"config_path": "", "sha256": ""}


def test_an_unreadable_file_records_an_absent_digest_never_a_wrong_one(tmp_path):
    missing = str(tmp_path / "never-written.yml")

    record = round_anchor_record(
        _apply_payload(prior_path=missing, applied_path=missing, applied_sha="")
    )

    assert record["displaced"] == {"config_path": missing, "sha256": ""}


# --------------------------------------------------------------------------- #
# 2. the divergence rule
# --------------------------------------------------------------------------- #


def test_the_cycle_4_shape_is_a_divergence(tmp_path):
    """THE incident, as a fixture.

    A candidate applied; an operator reconciles the running config to a
    different file out of band; the round's restore fires. Every pre-#2537
    precondition passes — the anchor is stashed, its topology matches, its bytes
    are intact — because none of them asks whether the graph about to be
    REPLACED is the one this round put there.
    """
    applied_path, applied_sha = _yaml(
        tmp_path, "active_speaker_baseline_candidate_3298558b817e.yml", "run2\n"
    )
    reconciled_path, _ = _yaml(tmp_path, "sound_current.yml", "reconciled\n")
    anchor = round_anchor_record(
        _apply_payload(
            prior_path=str(tmp_path / "round1.yml"),
            applied_path=applied_path,
            applied_sha=applied_sha,
        )
    )

    assert running_config_diverged(anchor, reconciled_path) is True
    # …and the un-displaced control, so the fixture is proven to be able to
    # answer False. A test whose subject can only return True proves nothing.
    assert running_config_diverged(anchor, applied_path) is False


def test_a_same_named_file_with_different_bytes_is_a_divergence(tmp_path):
    """The sibling shape, and a live condition rather than scaffolding.

    ``build_baseline_profile_candidate`` names candidates from a SOURCE
    fingerprint that does not cover every input reaching the emitted bytes, so a
    same-fingerprint recompile can rewrite a file under its own name — which
    ``restore_applied_baseline_profile``'s ``restore_target_changed`` already
    exists for, asked there about the graph being RELOADED.
    """
    applied_path, applied_sha = _yaml(tmp_path, "candidate.yml", "original\n")
    anchor = round_anchor_record(
        _apply_payload(
            prior_path="", applied_path=applied_path, applied_sha=applied_sha
        )
    )
    assert running_config_diverged(anchor, applied_path) is False

    (tmp_path / "candidate.yml").write_text("recompiled\n", encoding="utf-8")

    assert running_config_diverged(anchor, applied_path) is True


def test_a_symlinked_spelling_of_the_same_file_is_not_a_divergence(tmp_path):
    """The statefile carries what CamillaDSP was handed; the record carries what
    the apply wrote. Two spellings of one file must not read as a move."""
    applied_path, applied_sha = _yaml(tmp_path, "candidate.yml", "same\n")
    link = tmp_path / "link.yml"
    link.symlink_to(applied_path)
    anchor = round_anchor_record(
        _apply_payload(
            prior_path="", applied_path=applied_path, applied_sha=applied_sha
        )
    )

    assert running_config_diverged(anchor, str(link)) is False


@pytest.mark.parametrize(
    ("anchor", "running", "why"),
    [
        (None, "/tmp/x.yml", "no anchor at all — a state written before #2537"),
        ({}, "/tmp/x.yml", "an anchor with no applied half"),
        ({"applied": {"config_path": ""}}, "/tmp/x.yml", "an anchor naming no path"),
        ({"applied": {"config_path": "/a.yml"}}, "", "no live reading"),
        ({"applied": {"config_path": "/a.yml"}}, None, "no live reading at all"),
        (
            {"applied": {"config_path": "/a.yml"}},
            "None",
            "CamillaClient's str()-wrapped None arriving as a literal",
        ),
        (
            {"applied": {"config_path": "/a.yml"}},
            "/definitely/not/a/real/path.yml",
            "a live reading naming no file on disk",
        ),
    ],
)
def test_what_cannot_be_compared_is_never_reported_as_moved(anchor, running, why):
    """"We could not compare" and "it moved" are different answers.

    Conflating them is the class of mistake this whole issue is about, and here
    it would refuse a household's Undo on a non-finding.
    """
    assert running_config_diverged(anchor, running) is False, why


def test_a_missing_digest_on_either_side_leaves_the_path_check_standing(tmp_path):
    """An absent digest is an absence, not a mismatch."""
    applied_path, _ = _yaml(tmp_path, "candidate.yml", "bytes\n")
    anchor = {"applied": {"config_path": applied_path, "sha256": ""}}

    assert running_config_diverged(anchor, applied_path) is False
    assert running_config_diverged(
        {"applied": {"config_path": applied_path, "sha256": "0" * 64}}, applied_path
    ) is True


# --------------------------------------------------------------------------- #
# 2b. the refusal the divergence rule feeds
# --------------------------------------------------------------------------- #


def _applied_state(tmp_path, *, applied_path: str, applied_sha: str):
    """A durable v2 state that clears the three pre-#2537 anchor checks.

    No ``topology_fingerprint`` on the stash, which is the documented "predates
    the fingerprint, cannot be compared, allowed through" path — so the ONLY
    thing left that can refuse is the divergence check, and a passing control
    below proves that is true rather than assumed.
    """
    del tmp_path
    return {
        "applied": True,
        "pre_apply_profile": {"kind": "prior", "source": {}},
        ROUND_ANCHOR_STATE_KEY: {
            "displaced": {"config_path": "", "sha256": ""},
            "applied": {"config_path": applied_path, "sha256": applied_sha},
        },
    }


def test_the_restore_refuses_when_the_running_graph_is_not_the_rounds(tmp_path):
    """The cycle-4 shape, at the precondition every restore path reads.

    Both readers of ``rollback_anchor_refusal`` see one rule; this is the one
    the household's Undo and the round's automatic restore both refuse on.
    """
    from jasper.web.correction_crossover_v2 import (
        ANCHOR_RUNNING_CONFIG_DIVERGED,
        rollback_anchor_refusal,
    )

    applied_path, applied_sha = _yaml(tmp_path, "candidate.yml", "the round's\n")
    reconciled, _ = _yaml(tmp_path, "sound_current.yml", "someone else's\n")
    state = _applied_state(
        tmp_path, applied_path=applied_path, applied_sha=applied_sha
    )

    refusal = rollback_anchor_refusal(state, running_config_path=reconciled)

    assert refusal is not None
    assert refusal.code == ANCHOR_RUNNING_CONFIG_DIVERGED
    # A household sentence, not a bug report: no path, no code, no hardware
    # noun — the same copy rule every other anchor refusal here carries.
    assert "changed by something else" in refusal.message
    assert reconciled not in refusal.message
    assert not any(
        word in refusal.message.lower()
        for word in ("tweeter", "woofer", "yml", "config_path")
    )


def test_the_restore_is_allowed_when_the_running_graph_is_still_the_rounds(
    tmp_path,
):
    """The control the test above needs to mean anything.

    Without it, a fixture that refused for one of the OTHER three reasons would
    read as a passing divergence test.
    """
    from jasper.web.correction_crossover_v2 import rollback_anchor_refusal

    applied_path, applied_sha = _yaml(tmp_path, "candidate.yml", "the round's\n")
    state = _applied_state(
        tmp_path, applied_path=applied_path, applied_sha=applied_sha
    )

    assert rollback_anchor_refusal(
        state, running_config_path=applied_path
    ) is None


def test_the_capability_probe_takes_no_live_reading_and_so_never_diverges(
    tmp_path,
):
    """The seam answers the three STATIC preconditions; the endpoint adds the
    live one.

    Deliberate: a camilla hiccup flipping ``rollback_available`` to False would
    route an otherwise-fine round to ``recovery_required``. A divergence found
    at the endpoint instead returns "not restored", which re-grades the round
    the same way — one round later and only when it matters.

    Two halves, because either alone would pass on a broken split: the seam's
    call site must pass no live reading, AND a call with none must not refuse
    even when the running graph HAS moved.
    """
    import inspect

    from jasper.web.correction_crossover_v2 import (
        _rollback_anchor_available,
        rollback_anchor_refusal,
    )

    seam = inspect.getsource(_rollback_anchor_available)
    assert "rollback_anchor_refusal(load_v2_state())" in seam
    assert "running_config_path" not in seam

    applied_path, applied_sha = _yaml(tmp_path, "candidate.yml", "the round's\n")
    _yaml(tmp_path, "sound_current.yml", "someone else's\n")
    state = _applied_state(
        tmp_path, applied_path=applied_path, applied_sha=applied_sha
    )

    assert rollback_anchor_refusal(state) is None


def test_a_state_written_before_this_shipped_still_restores(tmp_path):
    """No ``round_anchor`` means the comparison cannot be made, and an absent
    comparison must not refuse a household's Undo."""
    from jasper.web.correction_crossover_v2 import rollback_anchor_refusal

    reconciled, _ = _yaml(tmp_path, "sound_current.yml", "anything\n")
    state = {"applied": True, "pre_apply_profile": {"kind": "prior", "source": {}}}

    assert rollback_anchor_refusal(
        state, running_config_path=reconciled
    ) is None


# --------------------------------------------------------------------------- #
# 3. the read-side provenance of the applied-profile record
# --------------------------------------------------------------------------- #


def _statefile(tmp_path, config_path: str) -> str:
    path = tmp_path / "outputd-statefile.yml"
    path.write_text(f"config_path: {config_path}\n", encoding="utf-8")
    return str(path)


def test_a_record_naming_the_running_config_is_authoritative(tmp_path):
    applied_path, _ = _yaml(tmp_path, "candidate.yml", "live\n")
    statefile = _statefile(tmp_path, applied_path)

    assert applied_profile_displacement(
        {"config": {"path": applied_path}}, statefile_path=statefile
    ) == ""


def test_a_record_naming_a_config_the_speaker_is_not_playing_is_displaced(tmp_path):
    """The cycle-4 shape one layer up.

    The record still named run 2's candidate while the speaker played
    ``sound_current.yml``. Nothing compared the two, so every consumer that read
    the record as "which graph is on the speaker" was answering with a graph the
    speaker had not played for hours.
    """
    applied_path, _ = _yaml(tmp_path, "candidate.yml", "stale\n")
    running_path, _ = _yaml(tmp_path, "sound_current.yml", "live\n")
    statefile = _statefile(tmp_path, running_path)

    assert applied_profile_displacement(
        {"config": {"path": applied_path}}, statefile_path=statefile
    ) == APPLIED_PROFILE_DISPLACED


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, APPLIED_PROFILE_PATH_UNKNOWN),
        ({}, APPLIED_PROFILE_PATH_UNKNOWN),
        ({"config": {}}, APPLIED_PROFILE_PATH_UNKNOWN),
        ({"config": {"path": ""}}, APPLIED_PROFILE_PATH_UNKNOWN),
    ],
)
def test_a_record_with_no_path_says_so_rather_than_claiming_displacement(
    record, expected, tmp_path
):
    statefile = _statefile(tmp_path, str(tmp_path / "anything.yml"))
    assert applied_profile_displacement(record, statefile_path=statefile) == expected


def test_an_unreadable_statefile_says_it_could_not_check(tmp_path):
    """"We could not check" is not "we checked and it moved".

    The distinction is what stops every box without a statefile from reporting
    its applied profile as displaced forever.
    """
    assert applied_profile_displacement(
        {"config": {"path": str(tmp_path / "candidate.yml")}},
        statefile_path=str(tmp_path / "no-such-statefile.yml"),
    ) == APPLIED_PROFILE_RUNNING_UNKNOWN


def test_the_displacement_reader_adds_no_second_writer():
    """#2537 option (b), and the argument for choosing it.

    Option (a) — teaching ``reconcile-current-dsp`` to retire the profile
    record — would have made a sound-intent renderer a writer of the
    active-speaker profile system, trading away the separation of concerns that
    makes it safe to run at deploy time. This reader touches nothing.

    Checked over the parsed CALLS rather than the source text: prose naming a
    writer is not a call to one, and a text scan that cannot tell those apart
    fails on its own docstring (it did) and would pass on
    ``getattr(mod, "write_" + "text")``.
    """
    import ast
    import inspect

    from jasper.active_speaker import baseline_profile

    tree = ast.parse(
        inspect.getsource(baseline_profile.applied_profile_displacement)
    )
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else
        node.func.id if isinstance(node.func, ast.Name) else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    forbidden = {
        "write_text", "atomic_write_text", "persist_applied_baseline_profile",
        "promote_applied_baseline_candidate", "unlink", "open", "rename",
    }
    assert called & forbidden == set()
    # The one call that reaches outside is the statefile READ, and naming it is
    # what makes the assertion above a bound rather than a list of guesses.
    assert "read_camilla_statefile_config_path" in called


def test_the_state_key_the_host_writes_is_the_one_this_module_names():
    """One key, one owner. The host imports it rather than spelling it."""
    from pathlib import Path

    import jasper.web.correction_crossover_v2 as v2host

    source = Path(v2host.__file__).read_text(encoding="utf-8")
    assert ROUND_ANCHOR_STATE_KEY == "round_anchor"
    assert '"round_anchor"' not in source
    assert source.count("ROUND_ANCHOR_STATE_KEY") >= 6
