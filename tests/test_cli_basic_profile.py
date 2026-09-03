# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-basic-profile``: the machine surface on the basic-profile door.

Every request is served by a fake opener -- :class:`WizardClient`'s own
transport seam -- so these pin what the CLI SENDS and what it PRINTS without a
wizard, a network, or a speaker.
"""
from __future__ import annotations

import json

import pytest

from jasper.active_speaker.baseline_profile import (
    BASELINE_PROFILE_KIND,
    SCHEMA_VERSION,
    baseline_candidate_fingerprint,
)
from jasper.active_speaker.state_paths import (
    BASELINE_PROFILE_STATE_ENV as STATE_PATH_ENV,
)
from jasper.cli import basic_profile as cli

_FINGERPRINT = "a" * 64

_CANDIDATE = {
    "status": "ready_to_apply",
    "candidate_fingerprint": _FINGERPRINT,
    "tuning_owner": "manual",
    "linearization": {},
    "blend_correction": [],
    "corrections": {
        "tweeter": {"gain_db": -4.5, "delay_ms": 0.35, "inverted": True},
        "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
    },
    "issues": [
        {
            "severity": "warning",
            "code": "driver_gain_derived_from_sensitivity",
            "message": "interim trim",
        }
    ],
}

_APPLIED_STATE = {
    "artifact_schema_version": SCHEMA_VERSION,
    "kind": BASELINE_PROFILE_KIND,
    "status": "applied",
    "applied_at": "2026-09-01T12:00:00Z",
    "source": {"fingerprint": "source-1"},
    "config": {"path": "/var/lib/camilladsp/configs/active_speaker_baseline.yml"},
    "tuning_owner": "manual",
    "linearization": {},
    "blend_correction": [],
    "corrections": _CANDIDATE["corrections"],
    "recomposition_snapshot": {"schema_version": 1, "preset": {"way_count": 2}},
}

_DOOR_REFUSAL = {
    "status": "blocked",
    "apply": None,
    "issues": [
        {
            "severity": "blocker",
            "code": cli.FINGERPRINT_MISMATCH_CODE,
            "message": "the crossover candidate changed after review",
        }
    ],
}


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Serves canned bodies by path suffix; records every request it saw."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        for path, page in self.pages.items():
            if request.full_url.endswith(path):
                return _FakeResponse(page)
        return _FakeResponse("")

    def paths(self) -> list[str]:
        return [request.full_url for request in self.requests]

    def posted_to(self, path: str) -> list:
        return [
            request
            for request in self.requests
            if request.data is not None and request.full_url.endswith(path)
        ]

    def posts(self) -> list:
        return [request for request in self.requests if request.data is not None]


def _opener(**pages: str) -> _FakeOpener:
    return _FakeOpener(
        {
            cli.CSRF_PAGE_PATH: '<meta name="jts-csrf" content="tok-abcdefgh12345678">',
            cli.SAVE_AND_APPLY_PATH: pages.get("save_and_apply", "{}"),
            cli.REVIEW_PATH: pages.get("review", json.dumps(_CANDIDATE)),
        }
    )


def _run(argv: list[str], opener: _FakeOpener) -> int:
    return cli.main([*argv, "--hostname", "jts3.local"], opener=opener)


def _stdout_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def applied_state(tmp_path, monkeypatch):
    path = tmp_path / "active_speaker_baseline_profile.json"
    path.write_text(json.dumps(_APPLIED_STATE), encoding="utf-8")
    monkeypatch.setenv(STATE_PATH_ENV, str(path))
    return path


def test_review_reports_the_fingerprint_and_that_nothing_is_carried(capsys):
    opener = _opener()

    assert _run(["review", "--json"], opener) == cli.EXIT_OK

    payload = _stdout_json(capsys)
    assert payload["candidate_fingerprint"] == _FINGERPRINT
    assert payload["linearization_roles"] == []
    assert payload["blend_correction_count"] == 0
    assert payload["tuning_owner"] == "manual"
    assert payload["structure_and_trim_only"] is True
    assert payload["trims"]["tweeter"] == {
        "gain_db": -4.5,
        "delay_ms": 0.35,
        "inverted": True,
    }
    # A pure read: the route's POST arm COMPILES, rewriting the baseline YAML
    # the CamillaDSP statefile may still select. Review must never send one.
    assert opener.posts() == []
    assert opener.paths() == ["http://127.0.0.1" + cli.REVIEW_PATH]


def test_review_prints_the_same_facts_for_a_human(capsys):
    assert _run(["review"], _opener()) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert _FINGERPRINT in out
    assert "tweeter" in out and "woofer" in out


def test_the_door_is_reached_at_its_own_daemons_paths_with_that_daemons_token():
    """The reason this verb exists: nginx fronts several wizard daemons.

    ``/sound/setup/`` is jasper-web's prefix, so the door's routes carry it and
    the double-submit token is minted from a page jasper-web itself serves --
    not from the correction wizard's page next door.
    """
    opener = _opener(save_and_apply=json.dumps({"status": "applied"}))

    _run(["apply", "--json"], opener)

    mints = [url for url in opener.paths() if url.endswith(cli.CSRF_PAGE_PATH)]
    assert mints == ["http://127.0.0.1" + cli.CSRF_PAGE_PATH]
    # The apply is the ONE write: the review is read over the same daemon's GET.
    posts = opener.posts()
    assert [request.full_url for request in posts] == [
        "http://127.0.0.1" + cli.SAVE_AND_APPLY_PATH
    ]
    for request in opener.requests:
        assert request.get_header("Host") == "jts3.local"
    assert posts[0].get_header("X-csrf-token") == "tok-abcdefgh12345678"


def test_apply_pins_the_fingerprint_it_just_reviewed_and_proves_the_result(
    capsys, applied_state
):
    disclosure = {
        "severity": "warning",
        "code": "driver_gain_derived_from_sensitivity",
        "message": "interim trim",
    }
    opener = _opener(
        save_and_apply=json.dumps({"status": "applied", "issues": [disclosure]})
    )

    assert _run(["apply", "--json"], opener) == cli.EXIT_OK

    sent = opener.posted_to(cli.SAVE_AND_APPLY_PATH)
    assert [json.loads(request.data.decode()) for request in sent] == [
        {"expected_candidate_fingerprint": _FINGERPRINT}
    ]
    payload = _stdout_json(capsys)
    assert payload["status"] == "applied"
    assert payload["candidate_fingerprint"] == _FINGERPRINT
    assert payload["issues"] == [disclosure]
    assert payload["proof"] == {
        "candidate_fingerprint": baseline_candidate_fingerprint(_APPLIED_STATE),
        "applied_at": "2026-09-01T12:00:00Z",
        "tuning_owner": "manual",
        "linearization_roles": [],
        "blend_correction_count": 0,
        "structure_and_trim_only": True,
    }


@pytest.mark.parametrize("named", ["b" * 64, ""])
def test_a_fingerprint_that_is_not_the_live_one_sends_nothing(capsys, named):
    """Refused on the speaker's own shell, before any request leaves it."""
    opener = _opener(
        review=json.dumps({**_CANDIDATE, "candidate_fingerprint": named})
        if not named
        else json.dumps(_CANDIDATE)
    )
    argv = ["apply", "--json"] + (["--expected-fingerprint", named] if named else [])

    assert _run(argv, opener) == cli.EXIT_REFUSED

    payload = _stdout_json(capsys)
    assert payload["status"] == "blocked"
    assert payload["refused_by"] == "client"
    assert [issue["code"] for issue in payload["issues"]] == [
        cli.FINGERPRINT_MISMATCH_CODE
    ]
    assert opener.posts() == []


def test_a_door_refusal_reaches_stdout_whole(capsys, applied_state):
    opener = _opener(save_and_apply=json.dumps(_DOOR_REFUSAL))

    assert _run(["apply"], opener) == cli.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out) == _DOOR_REFUSAL


def test_a_door_that_does_not_answer_is_not_a_traceback(capsys):
    opener = _FakeOpener({cli.REVIEW_PATH: "<html>the wizard is starting"})

    assert _run(["review"], opener) == cli.EXIT_TRANSPORT
    err = capsys.readouterr().err
    assert err.strip()
    # A lost READ changed nothing, so it must not send the reader to check.
    assert cli.LOST_ANSWER_ADVICE not in err


def test_a_lost_apply_answer_does_not_claim_the_apply_failed(capsys):
    """The route has no try/except around its own answer, so a connection lost
    after the graph loaded is indistinguishable here from one lost before."""
    opener = _opener(save_and_apply="<html>502 bad gateway")

    assert _run(["apply"], opener) == cli.EXIT_TRANSPORT
    assert cli.LOST_ANSWER_ADVICE in capsys.readouterr().err


def test_apply_still_reports_success_when_the_proof_cannot_be_read(
    capsys, tmp_path, monkeypatch
):
    """The record is group-readable; an apply that succeeded is not a failure
    because the caller could not open it afterwards."""
    monkeypatch.setenv(STATE_PATH_ENV, str(tmp_path / "absent.json"))
    opener = _opener(save_and_apply=json.dumps({"status": "applied"}))

    assert _run(["apply", "--json"], opener) == cli.EXIT_OK
    assert _stdout_json(capsys)["proof"] is None
