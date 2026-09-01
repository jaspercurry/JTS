# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-round-bank``: one live session into the on-box campaign home.

The source session is the real one ``tests/crossover_v2_banked_round.py``
builds through the product's own writers, so the tree this banks is the tree
the flow actually produces — and every layout assertion here goes through
``round_views``' own reader rather than re-spelling the layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.round_views import (
    _bundle_session_dir,
    load_banked_round,
)
from jasper.active_speaker.round_bank import (
    REASON_ALREADY_BANKED,
    REASON_NOT_A_BUNDLE,
    REASON_SESSION_UNFINISHED,
    RoundBankError,
    bank_round,
)
from jasper.cli import round_bank as cli

from tests.crossover_v2_banked_round import bank_measure_round


def _live_session(tmp_path: Path, *, state: str = "applied") -> tuple[Path, Path]:
    """``(live session bundle, the flow state banked beside it)``.

    Banking is a post-apply act, so the bundle is marked ``applied`` unless a
    test is about an unfinished one.
    """
    source = bank_measure_round(tmp_path / "live")
    session_dir = _bundle_session_dir(source)
    mark_state(session_dir, state)
    return session_dir, source / "state.json"


def _ssot(tmp_path: Path, *, present: bool) -> dict[str, Path]:
    """The design-draft/applied-profile SSOT paths, written or absent."""
    paths = {
        "design_draft_path": tmp_path / "ssot" / "design_draft.json",
        "applied_profile_path": tmp_path / "ssot" / "applied_profile.json",
    }
    if present:
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"kind": path.stem}))
    return paths


@pytest.mark.parametrize("present", [True, False], ids=["ssot-present", "ssot-absent"])
def test_banked_tree_is_the_one_round_views_reads(tmp_path, present):
    """The assembled tree loads back through ``load_banked_round`` either way:
    an absent SSOT document is named in ``provenance.json``, never a refusal."""
    session_dir, state_path = _live_session(tmp_path)

    banked = bank_round(
        session_dir,
        campaign_root=tmp_path / "campaigns",
        state_path=state_path,
        **_ssot(tmp_path, present=present),
    )

    # The round id is the receipt's, not the bundle's session id.
    assert banked.path == tmp_path / "campaigns" / "r1"
    assert _bundle_session_dir(banked.path).name == session_dir.name
    assert (banked.path / "bundle" / session_dir.name / "info.json").is_file()
    assert load_banked_round(banked.path).session_dir == _bundle_session_dir(
        banked.path
    )
    provenance = json.loads((banked.path / "provenance.json").read_text())
    # What the caller is handed is what landed on disk -- no re-read needed.
    assert provenance == banked.provenance
    assert provenance["source"] == "on-box"
    assert provenance["session_id"] == session_dir.name
    assert provenance["missing"] == (
        [] if present else ["design-draft.json", "applied-profile.json"]
    )
    for name in ("state.json", "design-draft.json", "applied-profile.json"):
        assert (banked.path / name).is_file() is (name not in provenance["missing"])


@pytest.mark.parametrize(
    "sha, git_absent", [("abc1234", False), (None, True)], ids=["sha", "no-sha"]
)
def test_provenance_records_the_installed_build_or_says_it_cannot(
    tmp_path, monkeypatch, sha, git_absent
):
    session_dir, state_path = _live_session(tmp_path)
    monkeypatch.setattr(
        "jasper.active_speaker.round_bank._detect_build_sha", lambda: sha
    )

    banked = bank_round(
        session_dir,
        campaign_root=tmp_path / "campaigns",
        state_path=state_path,
        **_ssot(tmp_path, present=False),
    )

    assert banked.provenance["installed_sha"] == sha
    assert banked.provenance["git_absent"] is git_absent
    # One key spelling across both banking paths (scripts/bank-crossover-round.sh).
    assert banked.provenance["banked_at_utc"].endswith("Z")


def test_a_banked_round_is_never_overwritten(tmp_path):
    session_dir, state_path = _live_session(tmp_path)
    kwargs = {
        "campaign_root": tmp_path / "campaigns",
        "state_path": state_path,
        **_ssot(tmp_path, present=False),
    }
    first = bank_round(session_dir, **kwargs)

    with pytest.raises(RoundBankError) as excinfo:
        bank_round(session_dir, **kwargs)

    assert excinfo.value.reason == REASON_ALREADY_BANKED
    assert (first.path / "provenance.json").is_file()


def test_a_directory_that_is_not_a_bundle_is_refused(tmp_path):
    not_a_bundle = tmp_path / "empty"
    not_a_bundle.mkdir()

    with pytest.raises(RoundBankError) as excinfo:
        bank_round(not_a_bundle, campaign_root=tmp_path / "campaigns")

    assert excinfo.value.reason == REASON_NOT_A_BUNDLE
    assert not (tmp_path / "campaigns").exists()


@pytest.mark.parametrize("state", ["open", "proposal_ready"])
def test_an_unfinished_session_is_refused_rather_than_claiming_its_round_id(
    tmp_path, state
):
    """A round id is banked once and never overwritten, so banking a session
    still in flight would permanently claim it for a partial round."""
    session_dir, state_path = _live_session(tmp_path, state=state)

    with pytest.raises(RoundBankError) as excinfo:
        bank_round(
            session_dir, campaign_root=tmp_path / "campaigns", state_path=state_path
        )

    assert excinfo.value.reason == REASON_SESSION_UNFINISHED
    assert not (tmp_path / "campaigns").exists()


@pytest.mark.parametrize("round_id", [".", "..", "../escape", "a/b"])
def test_a_round_id_that_is_not_a_plain_token_falls_back_to_the_session_id(
    tmp_path, round_id
):
    """The round id names a directory under the campaign root; anything that
    could walk out of it banks under the session id instead."""
    session_dir, state_path = _live_session(tmp_path)
    round_dir, _why = round_artifact_dir(session_dir)
    receipt_path = round_dir / "round_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["round_id"] = round_id
    receipt_path.write_text(json.dumps(receipt))

    banked = bank_round(
        session_dir, campaign_root=tmp_path / "campaigns", state_path=state_path
    )

    assert banked.path == tmp_path / "campaigns" / session_dir.name


def test_an_unreadable_info_json_exits_as_a_filesystem_failure(
    tmp_path, monkeypatch, capsys
):
    """An OSError that is not "no bundle here" must not read as a refusal: the
    operator gets the filesystem-failure exit, not "not a session bundle"."""
    session_dir, _state = _live_session(tmp_path)

    def _denied(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _denied)
    parser = cli.build_parser()
    args = parser.parse_args(
        [str(session_dir), "--campaign-root", str(tmp_path / "campaigns"), "--json"]
    )

    assert args.func(args) == cli.EXIT_BANK_FAILED

    assert json.loads(capsys.readouterr().out)["reason"] == "write_failed"


def test_cli_json_carries_the_banked_path_and_its_provenance(tmp_path, capsys):
    session_dir, _state = _live_session(tmp_path)
    argv = [str(session_dir), "--campaign-root", str(tmp_path / "campaigns"), "--json"]

    assert cli.main(argv) == cli.EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["banked"] is True
    assert Path(payload["round_dir"]) == tmp_path / "campaigns" / "r1"
    assert payload["provenance"]["session_id"] == session_dir.name


def test_cli_refusal_is_exit_2_with_the_reason_slug(tmp_path, capsys):
    not_a_bundle = tmp_path / "empty"
    not_a_bundle.mkdir()
    parser = cli.build_parser()
    args = parser.parse_args(
        [str(not_a_bundle), "--campaign-root", str(tmp_path / "campaigns"), "--json"]
    )

    assert args.func(args) == cli.EXIT_REFUSED

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "banked": False,
        "reason": REASON_NOT_A_BUNDLE,
        "detail": payload["detail"],
    }
