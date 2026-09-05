# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-audition``: what the door puts on each stream.

The engine's own behaviour is ``tests/test_active_speaker_audition.py``'s. What
is pinned here is only the CLI's contract: one JSON document on stdout, the
refusal shape ``jasper/cli/_refusal.py`` owns, and the exit code agreeing with
the ``status`` word that document carries.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jasper.active_speaker.audition import (
    AUDITION_LAYER_BASELINE,
    AUDITION_LAYER_FULL,
    AuditionRefused,
    END_SUPERSEDED,
    REFUSE_MEASUREMENT_ACTIVE,
)
from jasper.cli import audition as cli
from jasper.cli._refusal import STATUS_BY_CODE


@pytest.fixture(autouse=True)
def _no_speaker(monkeypatch):
    """No CamillaDSP, no cue daemon, no audition record on this machine."""
    monkeypatch.setattr(cli, "_camilla_controller", lambda: object())
    monkeypatch.setattr(cli, "read_audition_state", lambda: None)


def _stdout(capsys) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_status_answers_with_the_state_document(capsys):
    assert cli.main(["status"]) == cli.EXIT_OK
    assert _stdout(capsys) == {
        "status": "not_auditioning",
        "layer": AUDITION_LAYER_FULL,
    }


@pytest.mark.parametrize("verb", ["start", "stop"])
def test_a_refusal_publishes_the_shared_document(capsys, monkeypatch, verb):
    """The reason is the engine's own slug; the exit code and the ``status``
    word can never disagree, because one table maps the two."""

    async def _refuse(**_kw: Any) -> dict[str, Any]:
        raise AuditionRefused(REFUSE_MEASUREMENT_ACTIVE, "a round is measuring")

    monkeypatch.setattr(cli, f"{verb}_audition", _refuse)

    assert cli.main([verb]) == cli.EXIT_REFUSED
    assert _stdout(capsys) == {
        "status": STATUS_BY_CODE[cli.EXIT_REFUSED],
        "reason": REFUSE_MEASUREMENT_ACTIVE,
        "detail": "a round is measuring",
    }


def test_start_that_ends_off_the_full_layer_carries_the_state_under_detail(
    capsys, monkeypatch
):
    """Exit 1 with the applied graph still gone is a refusal, not a receipt:
    every field the old top-level document carried rides ``detail`` now, and
    the verb that puts the speaker back is named there."""
    mine = {"status": "auditioning", "layer": AUDITION_LAYER_BASELINE}
    # The record a SECOND start left behind: this process is done, and the
    # speaker is still on the reduced layer that other one swapped in.
    theirs = {"layer": AUDITION_LAYER_BASELINE, "deadline_at": 1.0}

    async def _started(**_kw: Any) -> dict[str, Any]:
        return mine

    async def _taken_over(*_a: Any, **_kw: Any) -> str:
        return END_SUPERSEDED

    monkeypatch.setattr(cli, "start_audition", _started)
    monkeypatch.setattr(cli, "hold_audition", _taken_over)
    monkeypatch.setattr(cli, "read_audition_state", lambda: theirs)

    assert cli.main(["start"]) == cli.EXIT_REFUSED
    payload = _stdout(capsys)
    assert payload["status"] == STATUS_BY_CODE[cli.EXIT_REFUSED]
    assert payload["reason"] == cli.NOT_RESTORED
    assert payload["detail"] == {
        "status": "auditioning",
        "ended": END_SUPERSEDED,
        "next": "jasper-audition stop",
        **theirs,
    }
