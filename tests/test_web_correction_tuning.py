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


@pytest.fixture(autouse=True)
def _reset_paid_call_gate():
    """The per-process paid-call min-interval gate is shared state; reset
    it around every test so happy-path tests within one pytest run don't
    trip each other."""
    correction_setup._tuning_last_paid_call[0] = 0.0
    yield
    correction_setup._tuning_last_paid_call[0] = 0.0


def _fake_session(state_value="ready"):
    from jasper.correction.session import SessionState

    freqs = np.geomspace(20, 350, 60)
    measured = 8.0 * np.exp(-((np.log2(freqs / 62.0)) ** 2) / (2 * 0.25 ** 2))
    Curve = lambda m: SimpleNamespace(
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
    # The paid call carries the hard output-token budget guard — the
    # single shared constant at the model boundary (also the live
    # harness default, so deployed and live-validated caps can't drift).
    from jasper.calibration_agent import model_client

    assert (
        captured["kwargs"]["max_output_tokens"]
        == model_client.TUNING_LLM_MAX_OUTPUT_TOKENS
    )
    assert model_client.TUNING_LLM_MAX_OUTPUT_TOKENS >= 2000, (
        "GPT-5-class reasoning tokens count against max_output_tokens; "
        "the 2026-07-06 live check saw status=incomplete below this"
    )


def test_interpret_rejects_non_string_message(monkeypatch):
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: True,
    )
    with pytest.raises(correction_setup.BadRequest):
        correction_setup._handle_interpret(_FakeHandler(b'{"message":123}'))


def test_paid_call_min_interval_gate(monkeypatch):
    """A second paid call inside the min-interval window is refused with
    an honest 409 (never a silent drop) — a stuck client retry loop must
    not burn spend. The two paid handlers share one gate."""
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: True,
    )
    monkeypatch.setattr(correction_setup, "_get_or_create_session", _fake_session)
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        lambda session, **kwargs: {"kind": "jts_correction_interpret"},
    )
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.propose",
        lambda session, **kwargs: {"kind": "jts_correction_proposal_review"},
    )
    # First paid call passes and stamps the gate...
    correction_setup._handle_interpret(_FakeHandler())
    # ...an immediate second paid call (either handler) is refused honestly.
    with pytest.raises(correction_setup.RequestConflict, match="paid call"):
        correction_setup._handle_interpret(_FakeHandler())
    with pytest.raises(correction_setup.RequestConflict, match="paid call"):
        correction_setup._handle_propose(_FakeHandler())
    # Once the window has passed, calls flow again.
    correction_setup._tuning_last_paid_call[0] = 0.0
    out = correction_setup._handle_propose(_FakeHandler())
    assert out["kind"] == "jts_correction_proposal_review"


def test_tuning_timeout_env_typo_degrades_to_default(monkeypatch):
    """A garbage JASPER_TUNING_LLM_TIMEOUT_SEC must degrade to the 90 s
    default, never crash the whole /correction/ wizard at import."""
    monkeypatch.setenv("JASPER_TUNING_LLM_TIMEOUT_SEC", "ninety")
    assert correction_setup._tuning_timeout_sec() == 90.0
    monkeypatch.setenv("JASPER_TUNING_LLM_TIMEOUT_SEC", "-5")
    assert correction_setup._tuning_timeout_sec() == 90.0
    monkeypatch.setenv("JASPER_TUNING_LLM_TIMEOUT_SEC", "120")
    assert correction_setup._tuning_timeout_sec() == 120.0


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


def _real_ready_session(tmp_path, *, with_target=True):
    """A REAL MeasurementSession forced into READY with server curves set —
    for tests that must drive the genuine _handle_apply -> session.apply
    path (no handler mocks; the P7 mock-shape lesson at one remove)."""
    from jasper.correction.session import (
        CurveJSON,
        MeasurementSession,
        SessionConfig,
        SessionState,
    )

    cfg = SessionConfig(
        sweep_dir=tmp_path / "sweeps",
        capture_dir=tmp_path / "captures",
        sessions_dir=tmp_path / "sessions",
        config_dir=tmp_path / "configs",
        base_config_path=tmp_path / "v1.yml",
        duration_s=1.0,
    )
    cfg.base_config_path.write_text("# stub base v1.yml for tests\n")
    sess = MeasurementSession(cfg)
    sess.state = SessionState.READY
    freqs = np.geomspace(20, 350, 60)
    measured = 8.0 * np.exp(-((np.log2(freqs / 62.0)) ** 2) / (2 * 0.25 ** 2))
    sess.measured_curve = CurveJSON(
        freqs_hz=freqs.tolist(), magnitude_db=measured.tolist(),
    )
    sess.position1_curve = sess.measured_curve
    if with_target:
        sess.target_curve = CurveJSON(
            freqs_hz=freqs.tolist(),
            magnitude_db=np.zeros_like(freqs).tolist(),
        )
    return sess


class _RejectingCam:
    """Fake CamillaController: reports the flat base config as loaded
    (carrier_for_loaded_config short-circuits on that exact path without
    reading the file) and REJECTS every candidate load — driving
    session.apply's swallowed DspApplyError branch (state -> FAILED,
    no exception raised)."""

    def __init__(self):
        self.load_attempts: list[str] = []

    async def get_config_file_path(self, best_effort=True):
        from jasper.sound.camilla_yaml import BASE_CONFIG_PATH

        return str(BASE_CONFIG_PATH)

    async def set_config_file_path(self, path, best_effort=False):
        self.load_attempts.append(str(path))
        return False


def test_propose_apply_reports_honest_failure_when_reload_rejected(
    tmp_path, monkeypatch,
):
    """The swallowed-CamillaDSP-reload-failure path, driven through the
    REAL _handle_apply + session.apply (no handler mocks): session.apply
    swallows the rejected-reload DspApplyError (state FAILED, returns
    normally) — the response must say applied:false, never a dishonest
    applied:true with state:"failed"."""
    sess = _real_ready_session(tmp_path)
    cam = _RejectingCam()
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(correction_setup, "_camilla", lambda: cam)

    body = b'{"confirm":true,"correction_peqs":[{"freq_hz":62,"q":3,"gain_db":-7}]}'
    out = correction_setup._handle_propose_apply(_FakeHandler(body))

    # CamillaDSP genuinely rejected a real emitted candidate config...
    assert cam.load_attempts, "the real apply path never reached CamillaDSP"
    assert sess.state.value == "failed"
    # ...and the response tells the truth about it.
    assert out["applied"] is False
    assert out["state"] == "failed"
    assert "previous sound" in out["reason"]
    # The simulation itself HAD accepted (the failure is downstream).
    assert out["simulation"]["accepted"] is True


def test_propose_apply_fails_closed_without_acceptance_basis(monkeypatch):
    """Fail-closed split: the propose PREVIEW is lenient without
    baseline/target curves (ring+headroom only), but the APPLY seam
    requires the P4 acceptance judge to have run — no judge, no apply."""
    sess = _fake_session("ready")
    sess.target_curve = None  # no target -> evaluate_acceptance cannot run
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)

    def forbidden_apply(handler):  # pragma: no cover - must never run
        raise AssertionError("apply must not be reached without the judge")

    monkeypatch.setattr(correction_setup, "_handle_apply", forbidden_apply)
    body = b'{"confirm":true,"correction_peqs":[{"freq_hz":62,"q":3,"gain_db":-7}]}'
    out = correction_setup._handle_propose_apply(_FakeHandler(body))
    assert out["applied"] is False
    assert out["code"] == "missing_acceptance_basis"
    # The sim preview itself stayed lenient: bounds+ring+headroom passed.
    assert out["simulation"]["accepted"] is True
    assert out["simulation"]["acceptance"] is None
