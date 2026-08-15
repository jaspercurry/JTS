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

Four claims are pinned here, and the first is the incident itself:

1. the exact cycle-4 shape — candidate applied, running config displaced out of
   band, restore fired — must REFUSE rather than resurrect the stale candidate;
2. the divergence rule reports "it moved" only when it actually compared two
   things, never when it could not;
3. a displaced applied-profile record stops being read as authoritative for
   "which graph is on the speaker";
4. issue #2559 — a restore aimed at a graph this round never displaced refuses
   too. The same record, the same speaker, seven hours later: at 14:47 the
   staleness was already INSIDE the stash when the apply froze it, so claim 1's
   check (which asks about the moment of restore) passed and the restore
   resurrected the candidate anyway. Claims 1 and 4 ask about two different
   moments and both are needed.
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
    restore_target_diverged,
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


def test_a_different_path_alone_is_a_divergence_when_no_digest_can_answer(tmp_path):
    """The PATH comparison, pinned where it is the only thing that can answer.

    ``test_the_cycle_4_shape_is_a_divergence`` above uses two files with
    different bytes, so the DIGEST branch reaches the same verdict and the path
    branch is never load-bearing there — deleting it entirely leaves that test
    green (adversarial gate, PR #2553; reproduced). This is the shape that
    isolates it: an anchor whose recorded digest is empty, which
    :func:`round_anchor_record` emits whenever ``config_file_sha256`` could not
    read one at apply time (see
    ``test_an_unreadable_file_records_an_absent_digest_never_a_wrong_one``), and
    which :func:`running_config_diverged` requires to be non-empty on BOTH sides
    before the digest branch may speak.

    So on this fixture the digest branch is structurally silent and only the
    path comparison can return ``True``.
    """
    _yaml(tmp_path, "candidate.yml", "the round's\n")
    reconciled, _ = _yaml(tmp_path, "sound_current.yml", "someone else's\n")
    anchor = {
        "applied": {
            "config_path": str(tmp_path / "candidate.yml"), "sha256": "",
        },
    }

    assert running_config_diverged(anchor, reconciled) is True
    # The control: the same digest-less anchor against its OWN path must still
    # answer False, or this fixture would pass for a subject wired to True.
    assert running_config_diverged(
        anchor, str(tmp_path / "candidate.yml")
    ) is False


def test_two_files_with_identical_bytes_in_different_places_are_a_divergence(
    tmp_path,
):
    """The path comparison's second isolating shape: matching digests.

    A reconcile that lands byte-identical content at a different path — a copy,
    a promoted canonical name — makes the digest branch answer ``False`` on its
    merits. If the path comparison were absent, the speaker would be reported as
    still playing the round's own graph while CamillaDSP is loaded from a file
    the round never applied, and the restore would proceed against the wrong
    assumption.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    applied_path, applied_sha = _yaml(tmp_path, "candidate.yml", "identical\n")
    twin_path, twin_sha = _yaml(elsewhere, "candidate.yml", "identical\n")
    assert twin_sha == applied_sha, "the fixture's whole point is equal digests"
    anchor = round_anchor_record(
        _apply_payload(
            prior_path="", applied_path=applied_path, applied_sha=applied_sha
        )
    )

    assert running_config_diverged(anchor, twin_path) is True
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
# 2c. the restore TARGET — was the stash right when it was TAKEN? (#2559)
# --------------------------------------------------------------------------- #


#: The two files the 2026-08-15 14:47 jts3 firing was between. Named because
#: every test below is the same incident asked a different way, and the stale
#: one is the record that has now resurrected three times.
_DISPLACED = "sound_current.yml"
_STALE_CANDIDATE = "active_speaker_baseline_candidate_3298558b817e.yml"


def test_the_1447_shape_restores_what_the_round_displaced_or_refuses(tmp_path):
    """THE second incident, as a fixture.

    14:45:41 — the round applies, displacing ``sound_current.yml``. The
    applied-baseline-profile record it froze its stash from had been stale since
    00:34, so the stash names run 2's candidate instead. 14:47:18 — the delta
    probe's seam fires. Nothing has moved the running graph in those ninety-seven
    seconds, so the divergence check passes correctly, and the restore reloads
    the stale candidate: a graph the speaker had not played for fourteen hours.

    The target must be the graph the round displaced, or the restore must refuse.
    Never the stale record's.
    """
    displaced_path, displaced_sha = _yaml(tmp_path, _DISPLACED, "the sound\n")
    stale_path, stale_sha = _yaml(tmp_path, _STALE_CANDIDATE, "run 2's\n")
    anchor = round_anchor_record(
        _apply_payload(
            prior_path=displaced_path,
            applied_path=str(tmp_path / "round1_candidate.yml"),
            applied_sha="",
        )
    )

    assert restore_target_diverged(anchor, stale_path, stale_sha) is True
    # The control, so the fixture is proven able to answer False: a stash that
    # names what the round actually displaced restores exactly as before.
    assert restore_target_diverged(anchor, displaced_path, displaced_sha) is False


def test_the_two_anchor_checks_ask_about_different_moments(tmp_path):
    """Why #2553's check could not have caught this, stated as a measurement.

    The divergence check asks "has the running graph moved SINCE the apply".
    Ninety-seven seconds after an apply the answer is no, and it was no on
    2026-08-15 — which is why every precondition passed. The target check asks
    "was the stash right AT the apply". Same state, same anchor, opposite
    answers, and only the second one sees this defect.
    """
    displaced_path, _ = _yaml(tmp_path, _DISPLACED, "the sound\n")
    applied_path, applied_sha = _yaml(tmp_path, "round1_candidate.yml", "round 1\n")
    stale_path, stale_sha = _yaml(tmp_path, _STALE_CANDIDATE, "run 2's\n")
    anchor = round_anchor_record(
        _apply_payload(
            prior_path=displaced_path,
            applied_path=applied_path,
            applied_sha=applied_sha,
        )
    )

    # The running graph IS still the one this round applied — no divergence.
    assert running_config_diverged(anchor, applied_path) is False
    # …and the stash is still aimed at the wrong sound.
    assert restore_target_diverged(anchor, stale_path, stale_sha) is True


def test_a_different_path_alone_is_a_wrong_target_when_no_digest_can_answer(
    tmp_path,
):
    """The PATH comparison, isolated where it is the only thing that can answer.

    The 14:47 fixture above uses two files with different bytes, so the DIGEST
    branch reaches the same verdict there and deleting the path comparison
    leaves it green — the shape an adversarial gate caught in #2553's own
    sibling test. Here the anchor's recorded digest is empty (what
    ``round_anchor_record`` emits whenever the displaced file could not be read
    at apply time), so the digest branch is structurally silent.
    """
    stale_path, stale_sha = _yaml(tmp_path, _STALE_CANDIDATE, "run 2's\n")
    anchor = {
        "displaced": {"config_path": str(tmp_path / _DISPLACED), "sha256": ""},
    }

    assert restore_target_diverged(anchor, stale_path, stale_sha) is True
    # The control: the same digest-less anchor against its OWN path must still
    # answer False, or this fixture would pass for a subject wired to True.
    assert restore_target_diverged(
        anchor, str(tmp_path / _DISPLACED), stale_sha
    ) is False


def test_a_stash_naming_the_displaced_file_with_other_bytes_is_a_wrong_target(
    tmp_path,
):
    """The digest half, isolated.

    Same filename, different bytes — the shape
    ``build_baseline_profile_candidate``'s source fingerprint permits, asked
    here about the graph being PUT BACK. The two digests are comparable because
    there is one hasher: the anchor's is ``config_file_sha256`` of the displaced
    file at apply moment, and a profile's ``config.sha256`` is that same
    function's answer for the file it applied — which is what
    ``restore_applied_baseline_profile``'s ``restore_target_changed`` already
    relies on.
    """
    displaced_path, displaced_sha = _yaml(tmp_path, _DISPLACED, "the sound\n")
    anchor = round_anchor_record(
        _apply_payload(
            prior_path=displaced_path, applied_path="", applied_sha="",
        )
    )

    assert restore_target_diverged(anchor, displaced_path, displaced_sha) is False
    assert restore_target_diverged(anchor, displaced_path, "0" * 64) is True
    # An absent digest on either side is an absence, so the path check stands
    # alone rather than the pair reporting a mismatch it never measured.
    assert restore_target_diverged(anchor, displaced_path, "") is False
    assert restore_target_diverged(
        {"displaced": {"config_path": displaced_path, "sha256": ""}},
        displaced_path, "0" * 64,
    ) is False


def test_the_same_hasher_answers_for_both_sides_of_the_digest_comparison(tmp_path):
    """The digest branch's precondition, measured rather than assumed.

    If the anchor's digest and a profile's ``config.sha256`` were computed by
    different hashers, this check would refuse EVERY restore — a far worse
    failure than the one it fixes. They are not: both are
    :func:`jasper.dsp_apply.config_file_sha256` of the same bytes, and this
    walks that all the way from the file to both sides of the comparison.
    """
    from jasper.dsp_apply import config_file_sha256

    displaced_path, _ = _yaml(tmp_path, _DISPLACED, "the sound\n")
    anchor = round_anchor_record(
        _apply_payload(prior_path=displaced_path, applied_path="", applied_sha="")
    )
    profile_recorded_sha = config_file_sha256(displaced_path)

    assert anchor["displaced"]["sha256"] == profile_recorded_sha
    assert restore_target_diverged(anchor, displaced_path, profile_recorded_sha) is False


@pytest.mark.parametrize(
    ("anchor", "target", "why"),
    [
        (None, "/tmp/x.yml", "no anchor at all — a state written before #2537"),
        ({}, "/tmp/x.yml", "an anchor with no displaced half"),
        (
            {"displaced": {"config_path": ""}},
            "/tmp/x.yml",
            "an apply CamillaDSP could not name a prior config for",
        ),
        ({"displaced": {"config_path": "/a.yml"}}, "", "a stash naming no config"),
        ({"displaced": {"config_path": "/a.yml"}}, None, "no stash path at all"),
    ],
)
def test_what_cannot_be_compared_is_never_reported_as_a_wrong_target(
    anchor, target, why
):
    """The same honesty rule as the divergence check, for the same reason.

    Every one of these would refuse a household's Undo on a non-finding, and the
    first row is every speaker that applied before the anchor shipped.
    """
    assert restore_target_diverged(anchor, target) is False, why


def _stale_stash_state(*, stash_path: str, stash_sha: str, displaced_path: str):
    """A durable v2 state whose stash is the 14:47 shape.

    No ``topology_fingerprint`` (the documented "predates the fingerprint,
    allowed through" path) and no ``applied`` half on the anchor, so the ONLY
    check left that can refuse is the target one — and the control below proves
    that rather than assuming it.
    """
    return {
        "applied": True,
        "pre_apply_profile": {
            "kind": "prior",
            "source": {},
            "config": {"path": stash_path, "sha256": stash_sha},
        },
        ROUND_ANCHOR_STATE_KEY: {
            "displaced": {"config_path": displaced_path, "sha256": ""},
            "applied": {"config_path": "", "sha256": ""},
        },
    }


def test_the_restore_refuses_when_the_stash_is_not_what_the_round_displaced(
    tmp_path,
):
    """The refusal both restore paths read, with the household's own sentence."""
    from jasper.web.correction_crossover_v2 import (
        ANCHOR_STASH_NOT_DISPLACED,
        rollback_anchor_refusal,
    )

    displaced_path, _ = _yaml(tmp_path, _DISPLACED, "the sound\n")
    stale_path, stale_sha = _yaml(tmp_path, _STALE_CANDIDATE, "run 2's\n")

    refusal = rollback_anchor_refusal(
        _stale_stash_state(
            stash_path=stale_path,
            stash_sha=stale_sha,
            displaced_path=displaced_path,
        )
    )

    assert refusal is not None
    assert refusal.code == ANCHOR_STASH_NOT_DISPLACED
    # A household sentence: no path, no code, no hardware noun — the copy rule
    # every other anchor refusal here carries.
    assert "which sound to put back" in refusal.message
    assert stale_path not in refusal.message
    assert not any(
        word in refusal.message.lower()
        for word in ("tweeter", "woofer", "yml", "config_path", "anchor")
    )


def test_the_restore_is_allowed_when_the_stash_is_what_the_round_displaced(
    tmp_path,
):
    """The control the test above needs to mean anything.

    Without it a fixture refusing for one of the other four reasons would read
    as a passing target test.
    """
    from jasper.web.correction_crossover_v2 import rollback_anchor_refusal

    displaced_path, displaced_sha = _yaml(tmp_path, _DISPLACED, "the sound\n")

    assert rollback_anchor_refusal(
        _stale_stash_state(
            stash_path=displaced_path,
            stash_sha=displaced_sha,
            displaced_path=displaced_path,
        )
    ) is None


def test_the_capability_probe_answers_the_stale_stash_question_itself(tmp_path):
    """Static, so the ROUND learns before it decides — unlike the live check.

    ``running_config_diverged`` is deliberately held back from the capability
    probe: it needs a CamillaDSP round trip, and a hiccup there would route an
    otherwise-fine round to ``recovery_required``. Stash-versus-displaced needs
    no live reading — the same apply wrote both facts — so the probe answers it,
    and a round that cannot restore stops promising a restore it would then have
    to refuse. #2291's drift argument applied to the new check, pinned at the
    seam's own call shape (no live reading passed).
    """
    from jasper.web.correction_crossover_v2 import (
        ANCHOR_STASH_NOT_DISPLACED,
        rollback_anchor_refusal,
    )

    displaced_path, _ = _yaml(tmp_path, _DISPLACED, "the sound\n")
    stale_path, stale_sha = _yaml(tmp_path, _STALE_CANDIDATE, "run 2's\n")

    refusal = rollback_anchor_refusal(
        _stale_stash_state(
            stash_path=stale_path,
            stash_sha=stale_sha,
            displaced_path=displaced_path,
        ),
        running_config_path=None,
    )

    assert refusal is not None and refusal.code == ANCHOR_STASH_NOT_DISPLACED


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
