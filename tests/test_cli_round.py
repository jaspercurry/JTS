# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-round``: the three wizard round verbs, driven from the speaker.

Every request is served by a fake opener -- :class:`WizardClient`'s own
transport seam -- so these pin what the CLI SENDS, what it PRINTS and what it
EXITS with, without a wizard, a network or a speaker.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from jasper.active_speaker import wizard_client as wc
from jasper.active_speaker.crossover_v2_flow import TIERS as _FLOW_TIERS
from jasper.cli import round as cli

_FINGERPRINT = "a" * 64
_OTHER = "b" * 64


def test_tiers_match_crossover_v2_flow() -> None:
    """wizard_client restates TIERS to stay numpy-free; keep it in sync."""
    assert sorted(wc.TIERS) == sorted(_FLOW_TIERS)


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
    """Serves canned bodies by path suffix; records every request it saw.

    ``envelopes`` is a queue consumed by the FIRST status reads, so a wait test
    can age a session across polls before the steady page takes over.
    """

    def __init__(
        self,
        pages: dict[str, str],
        envelopes: list[str] | None = None,
        raises: dict[str, Exception] | None = None,
    ):
        self.pages = pages
        self.envelopes = list(envelopes or [])
        self.raises = dict(raises or {})
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        for path, error in self.raises.items():
            if request.full_url.endswith(path):
                raise error
        if request.full_url.endswith(wc.STATUS_PATH) and self.envelopes:
            return _FakeResponse(self.envelopes.pop(0))
        for path, page in self.pages.items():
            if request.full_url.endswith(path):
                return _FakeResponse(page)
        return _FakeResponse("")

    def posted_to(self, path: str) -> list:
        return [
            request
            for request in self.requests
            if request.data is not None and request.full_url.endswith(path)
        ]

    def posts(self) -> list:
        return [r for r in self.requests if r.data is not None]


def _envelope(**block) -> str:
    return json.dumps({"crossover_v2": block})


def _opener(*, v2=None, envelopes=None, raises=None, **pages) -> _FakeOpener:
    return _FakeOpener(
        {
            wc.CSRF_PAGE_PATH: '<meta name="jts-csrf" content="tok-abcd1234">',
            wc.STATUS_PATH: _envelope(**(v2 or {})),
            wc.SESSION_PATH: pages.get("session", "{}"),
            wc.VERIFY_PATH: pages.get("verify", "{}"),
            wc.APPLY_PATH: pages.get("apply", '{"status": "applied"}'),
        },
        envelopes=envelopes,
        raises=raises,
    )


def _lost(code: int) -> Exception:
    """What a dead daemon (0) and a refusing one (403) raise at the opener."""
    if not code:
        return urllib.error.URLError("[Errno 111] Connection refused")
    return urllib.error.HTTPError(
        "http://127.0.0.1", code, "Forbidden", {}, io.BytesIO(b"nope")
    )


def _run(argv, opener, monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_identity", lambda: _Identity())
    code = cli.main([*argv, "--json"], opener=opener)
    return code, json.loads(capsys.readouterr().out)


class _Identity:
    hostname = "jts3.local"


# --------------------------------------------------------------------------- #
# the vocabulary is the product's own
# --------------------------------------------------------------------------- #


def test_the_endpoints_and_stage_words_are_the_products_own():
    from jasper.active_speaker.crossover_v2 import journey
    from jasper.web.correction_crossover_v2 import (
        VERIFY_STAGE_KEY,
        VERIFY_STAGE_POST_APPLY,
    )
    from jasper.web.correction_setup import _POST_ROUTES

    for path in (wc.SESSION_PATH, wc.VERIFY_PATH, wc.APPLY_PATH):
        assert path.startswith("/correction/")
        assert path[len("/correction"):] in _POST_ROUTES
    assert (wc.STAGE_KEY, wc.STAGE_POST_APPLY) == (
        VERIFY_STAGE_KEY,
        VERIFY_STAGE_POST_APPLY,
    )
    # The COMPLEMENT, not a subset: `set(CAPTURE_PHASES) <= RUNNING_PHASES`
    # survives deleting `| {PHASE_CLOSING, PHASE_APPLYING}` entirely, which is
    # the bug it was supposed to catch. Every phase the journey defines must be
    # classified, and exactly two of them are places a round STOPS.
    all_phases = {
        value for name, value in vars(journey).items()
        if name.startswith("PHASE_") and isinstance(value, str)
    }
    assert all_phases - wc.RUNNING_PHASES == {journey.PHASE_REVIEW,
                                              journey.PHASE_DONE}
    assert set(journey.CAPTURE_PHASES) <= wc.RUNNING_PHASES


# --------------------------------------------------------------------------- #
# open
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv, path, body",
    [
        (["open", "--tier", "express"], wc.SESSION_PATH, {"tier": "express"}),
        (["open", "--tier", "full"], wc.SESSION_PATH, {"tier": "full"}),
        (
            ["open", "--stage", "post_apply"],
            wc.VERIFY_PATH,
            {wc.STAGE_KEY: wc.STAGE_POST_APPLY},
        ),
    ],
)
def test_open_posts_the_stage_and_tier_it_was_given(
    argv, path, body, monkeypatch, capsys
):
    opener = _opener(v2={"session_id": "s1", "phase": "check"})
    code, receipt = _run(argv, opener, monkeypatch, capsys)

    assert code == cli.EXIT_OK
    posted = opener.posted_to(path)
    assert [json.loads(r.data.decode()) for r in posted] == [body]
    assert receipt["status"] == "opened"
    assert receipt["session_id"] == "s1"
    # Every request claims this speaker's own name, never 127.0.0.1.
    assert {r.get_header("Host") for r in opener.requests} == {"jts3.local"}


def test_open_carries_the_alignment_and_topology_documents(
    tmp_path, monkeypatch, capsys
):
    """The two session-open doors, under the keys the host reads (#2773)."""
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        ALIGNMENT_PRESCRIPTION_KEY,
    )
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        TOPOLOGY_PRESCRIPTION_KEY,
    )

    alignment = {"delay_us": 120.0, "basis_delay_us": 120.0}
    topology = {"fc_hz": 1800.0, "order": 4}
    document = tmp_path / "alignment.json"
    document.write_text(json.dumps(alignment))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(topology)))
    opener = _opener(v2={"session_id": "s1", "phase": "check"})

    code, _ = _run(
        ["open", "--tier", "express",
         "--alignment-prescription", str(document),
         "--topology-prescription", "-"],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_OK
    posted = opener.posted_to(wc.SESSION_PATH)
    assert [json.loads(r.data.decode()) for r in posted] == [
        {
            "tier": "express",
            ALIGNMENT_PRESCRIPTION_KEY: alignment,
            TOPOLOGY_PRESCRIPTION_KEY: topology,
        }
    ]


def test_a_malformed_prescription_document_is_refused_before_the_open(
    tmp_path, monkeypatch, capsys
):
    document = tmp_path / "topology.json"
    document.write_text("{not json")
    opener = _opener()

    code, receipt = _run(
        ["open", "--tier", "express", "--topology-prescription", str(document)],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_REFUSED
    assert receipt["reason"] == cli.REASON_OPEN_REFUSED
    assert str(document) in receipt["detail"]
    assert opener.posts() == []


def test_the_measuring_stage_refuses_an_absent_tier_without_posting(
    monkeypatch, capsys
):
    """An absent tier resolves server-side to `full` -- silently, per #2639."""
    opener = _opener()
    code, receipt = _run(["open"], opener, monkeypatch, capsys)

    assert code == cli.EXIT_REFUSED
    assert receipt["reason"] == cli.REASON_TIER_REQUIRED
    assert opener.posts() == []


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "live, reason",
    [
        ({"candidate": {"fingerprint": _OTHER}}, wc.REASON_FINGERPRINT_MISMATCH),
        ({"phase": "review"}, wc.REASON_NO_CANDIDATE),
        ({}, wc.REASON_NO_V2_STATE),
    ],
)
def test_apply_refuses_a_fingerprint_that_is_not_the_live_one(
    live, reason, monkeypatch, capsys
):
    opener = _opener(v2=live)
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_REFUSED
    assert receipt["reason"] == reason
    assert receipt["refused_by"] == "client"
    assert receipt["expected_candidate_fingerprint"] == _FINGERPRINT
    assert opener.posted_to(wc.APPLY_PATH) == []


def test_apply_posts_the_named_fingerprint_when_it_is_the_live_one(
    monkeypatch, capsys
):
    opener = _opener(v2={"candidate": {"fingerprint": _FINGERPRINT}})
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_OK
    assert receipt["status"] == "applied"
    posted = opener.posted_to(wc.APPLY_PATH)
    assert [json.loads(r.data.decode()) for r in posted] == [
        {"expected_candidate_fingerprint": _FINGERPRINT}
    ]


@pytest.mark.parametrize("body", ['{"status": "apply_failed"}', '{"ok": false}'])
def test_an_apply_that_answered_but_did_not_apply_is_a_refusal(
    body, monkeypatch, capsys
):
    """200 alone passes `apply_failed`; only 200 AND `applied` is right."""
    opener = _opener(v2={"candidate": {"fingerprint": _FINGERPRINT}}, apply=body)
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_REFUSED
    assert receipt["reason"] == wc.REASON_NOT_APPLIED
    assert receipt["refused_by"] == "wizard"


# --------------------------------------------------------------------------- #
# wait
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "steady, scripted, expected",
    [
        (
            {"session_id": "s1", "phase": "review",
             "candidate": {"fingerprint": _FINGERPRINT}},
            [_envelope(session_id="s1", phase="measure")],
            {"status": "terminal", "reason": "", "phase": "review",
             "candidate_fingerprint": _FINGERPRINT, "code": cli.EXIT_OK},
        ),
        (
            {"session_id": "s1", "phase": "measure",
             "failure": {"error": "capture refused"}},
            [_envelope(session_id="s1", phase="measure")],
            {"status": "failed", "reason": wc.REASON_SESSION_FAILED,
             "phase": "measure", "candidate_fingerprint": "",
             "code": cli.EXIT_REFUSED},
        ),
        (
            {"session_id": "s1", "phase": "measure"},
            [],
            {"status": "timed_out", "reason": wc.REASON_WAIT_TIMEOUT,
             "phase": "measure", "candidate_fingerprint": "",
             "code": cli.EXIT_UNREADABLE},
        ),
    ],
)
def test_wait_reports_the_state_it_stopped_on(
    steady, scripted, expected, monkeypatch, capsys
):
    opener = _opener(v2=steady, envelopes=scripted)
    code, receipt = _run(
        ["wait", "--timeout-s", "0.05", "--poll-s", "0.01"],
        opener, monkeypatch, capsys,
    )

    assert code == expected.pop("code")
    assert {key: receipt[key] for key in expected} == expected
    # A wait writes nothing at all.
    assert opener.posts() == []


@pytest.mark.parametrize("code", [403, 0])
def test_wait_stops_on_the_first_unanswered_status_read(
    code, monkeypatch, capsys
):
    """A dead wizard is not a slow round -- and must not be waited out (#3498).

    The timeout here is far longer than the test could run: reaching the exit
    at all proves the poll stopped on the code rather than on the clock.
    """
    opener = _opener(raises={wc.STATUS_PATH: _lost(code)})
    exit_code, receipt = _run(
        ["wait", "--timeout-s", "600", "--poll-s", "0.01"],
        opener, monkeypatch, capsys,
    )

    assert exit_code == cli.EXIT_UNREADABLE
    assert receipt["reason"] == wc.REASON_ANSWER_LOST
    assert len(opener.requests) == 1


def test_an_apply_whose_answer_is_lost_is_not_a_wizard_refusal(
    monkeypatch, capsys
):
    """Nothing refused: the POST left and no answer came back (#3498)."""
    opener = _opener(
        v2={"candidate": {"fingerprint": _FINGERPRINT}},
        raises={wc.APPLY_PATH: _lost(0)},
    )
    code, receipt = _run(
        ["apply", "--expected-fingerprint", _FINGERPRINT],
        opener, monkeypatch, capsys,
    )

    assert code == cli.EXIT_UNREADABLE
    assert receipt["reason"] == wc.REASON_ANSWER_LOST
    assert receipt["refused_by"] == ""
    assert receipt["http"] == 0
