# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P6 tuning-LLM HTTP handlers on jasper-correction-web.

Fixture-driven; no paid calls. Drives the handler functions directly with
a minimal fake handler + a mocked advisor / session, proving: the no-key
409, the read-only interpret/propose passthrough, and — the safety core —
that /propose/apply RE-VALIDATES + RE-SIMULATES server-side and requires
explicit confirm before routing through the existing apply path.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.web import correction_setup


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in carrying a JSON body."""

    def __init__(self, body: bytes = b"{}"):
        self.rfile = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}


def _fake_session(state_value="ready"):
    from jasper.correction.session import SessionState

    freqs = np.geomspace(20, 350, 60)
    measured = 8.0 * np.exp(-((np.log2(freqs / 62.0)) ** 2) / (2 * 0.25 ** 2))
    Curve = lambda m: SimpleNamespace(  # noqa: E731 - tiny local ctor
        freqs_hz=freqs.tolist(), magnitude_db=m.tolist()
    )
    state = getattr(SessionState, state_value.upper())
    return SimpleNamespace(
        session_id="sess-1",
        state=state,
        strategy_choice="balanced",
        measured_curve=Curve(measured),
        target_curve=Curve(np.zeros_like(freqs)),
        position1_curve=Curve(measured),
        peqs=[],
        config_path=None,
    )


# --- availability gate ------------------------------------------------

def test_interpret_without_key_conflicts(monkeypatch):
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: False,
    )
    with pytest.raises(correction_setup.RequestConflict):
        correction_setup._handle_interpret(_FakeHandler())


def test_propose_without_key_conflicts(monkeypatch):
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: False,
    )
    with pytest.raises(correction_setup.RequestConflict):
        correction_setup._handle_propose(_FakeHandler())


# --- interpret / propose passthrough ----------------------------------

def test_interpret_delegates_to_advisor(monkeypatch):
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: True,
    )
    monkeypatch.setattr(correction_setup, "_get_or_create_session", _fake_session)
    captured = {}

    def fake_interpret(session, **kwargs):
        captured["called"] = True
        captured["kwargs"] = kwargs
        return {"kind": "jts_correction_interpret", "explanation": "ok"}

    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret", fake_interpret
    )
    out = correction_setup._handle_interpret(_FakeHandler(b'{"message":"hi"}'))
    assert out["explanation"] == "ok"
    assert captured["called"] is True
    assert captured["kwargs"]["user_message"] == "hi"


def test_interpret_rejects_non_string_message(monkeypatch):
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: True,
    )
    with pytest.raises(correction_setup.BadRequest):
        correction_setup._handle_interpret(_FakeHandler(b'{"message":123}'))


# --- /propose/apply: the safety core ----------------------------------

def test_propose_apply_requires_confirm():
    with pytest.raises(correction_setup.BadRequest):
        correction_setup._handle_propose_apply(
            _FakeHandler(b'{"correction_peqs":[{"freq_hz":62,"q":3,"gain_db":-7}]}')
        )


def test_propose_apply_requires_peqs():
    with pytest.raises(correction_setup.BadRequest):
        correction_setup._handle_propose_apply(_FakeHandler(b'{"confirm":true}'))


def test_propose_apply_conflicts_when_not_ready(monkeypatch):
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session",
        lambda: _fake_session("applied"),
    )
    body = b'{"confirm":true,"correction_peqs":[{"freq_hz":62,"q":3,"gain_db":-7}]}'
    with pytest.raises(correction_setup.RequestConflict):
        correction_setup._handle_propose_apply(_FakeHandler(body))


def test_propose_apply_rejects_out_of_bounds_without_applying(monkeypatch):
    sess = _fake_session("ready")
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    applied = {"called": False}
    monkeypatch.setattr(
        correction_setup, "_handle_apply",
        lambda h: applied.__setitem__("called", True),
    )
    # 5000 Hz is outside the correction band -> re-validation fails.
    body = b'{"confirm":true,"correction_peqs":[{"freq_hz":5000,"q":3,"gain_db":-7}]}'
    out = correction_setup._handle_propose_apply(_FakeHandler(body))
    assert out["applied"] is False
    assert "re-validation" in out["reason"]
    assert applied["called"] is False


def test_propose_apply_rejects_regressing_set_via_resimulation(monkeypatch):
    sess = _fake_session("ready")
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    applied = {"called": False}

    def fake_apply(h):
        applied["called"] = True
        return {"session_id": "x", "state": "applied", "config_path": None}

    monkeypatch.setattr(correction_setup, "_handle_apply", fake_apply)
    # A stack of wide, deep cuts around the mode over-corrects and gouges
    # the region below target — the noise-free simulation returns a
    # revert-class verdict, so the server rejects before apply. (Bounds
    # pass: all within the balanced band, cuts-only, no boost stacking.)
    body = (
        b'{"confirm":true,"correction_peqs":['
        b'{"freq_hz":62,"q":1.0,"gain_db":-10},'
        b'{"freq_hz":50,"q":1.0,"gain_db":-10},'
        b'{"freq_hz":80,"q":1.0,"gain_db":-10}]}'
    )
    out = correction_setup._handle_propose_apply(_FakeHandler(body))
    assert out["applied"] is False
    assert "simulation" in out["reason"]
    assert applied["called"] is False


def test_propose_apply_good_cut_routes_through_apply(monkeypatch):
    from jasper.correction.session import PEQJSON

    sess = _fake_session("ready")
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    applied = {"peqs": None}

    def fake_apply(handler):
        # The handler populated session.peqs before calling apply.
        applied["peqs"] = list(sess.peqs)
        return {"session_id": sess.session_id, "state": "applied", "config_path": "/x.yml"}

    monkeypatch.setattr(correction_setup, "_handle_apply", fake_apply)
    body = b'{"confirm":true,"correction_peqs":[{"freq_hz":62,"q":3,"gain_db":-7}]}'
    out = correction_setup._handle_propose_apply(_FakeHandler(body))
    assert out["applied"] is True
    assert out["state"] == "applied"
    # session.peqs was populated with the proposed filter as a PEQJSON.
    assert applied["peqs"] and isinstance(applied["peqs"][0], PEQJSON)
    assert applied["peqs"][0].freq_hz == 62.0
    assert out["simulation"]["accepted"] is True
