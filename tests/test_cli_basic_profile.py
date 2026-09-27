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


from jasper.cli import basic_profile as cli
from jasper.cli._refusal import STATUS_BY_CODE

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

    def posts(self) -> list:
        return [request for request in self.requests if request.data is not None]


def _opener(**pages: str) -> _FakeOpener:
    return _FakeOpener(
        {
            cli.REVIEW_PATH: pages.get("review", json.dumps(_CANDIDATE)),
        }
    )


def _run(argv: list[str], opener: _FakeOpener) -> int:
    return cli.main([*argv, "--hostname", "jts3.local"], opener=opener)


def _stdout_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_review_reports_the_fingerprint_and_that_nothing_is_carried(capsys):
    opener = _opener()

    assert _run(["review"], opener) == cli.EXIT_OK

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
    assert [request.get_header("Host") for request in opener.requests] == ["jts3.local"]


def test_review_prints_the_same_facts_for_a_human(capsys):
    """The human rendering is stderr's; stdout carries the answer alone."""
    assert _run(["review"], _opener()) == cli.EXIT_OK

    streams = capsys.readouterr()
    assert _FINGERPRINT in streams.err
    assert "tweeter" in streams.err and "woofer" in streams.err
    assert json.loads(streams.out)["candidate_fingerprint"] == _FINGERPRINT


def test_a_door_that_does_not_answer_is_not_a_traceback(capsys):
    opener = _FakeOpener({cli.REVIEW_PATH: "<html>the wizard is starting"})

    assert _run(["review"], opener) == cli.EXIT_UNREADABLE
    streams = capsys.readouterr()
    payload = json.loads(streams.out)
    assert payload["status"] == STATUS_BY_CODE[cli.EXIT_UNREADABLE]
    assert payload["reason"] == cli.ANSWER_LOST
    assert payload["detail"]["path"] == cli.REVIEW_PATH
