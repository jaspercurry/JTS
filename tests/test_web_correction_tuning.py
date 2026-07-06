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
import os
import threading
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
def _reset_paid_call_gate(tmp_path, monkeypatch):
    """The per-process paid-call min-interval gate is shared state; reset
    it around every test so happy-path tests within one pytest run don't
    trip each other.

    Also pins the spend-settings inputs hermetic: the gate reads the
    wizard-owned voice_provider.env (via JASPER_VOICE_PROVIDER_FILE) and the
    usage DB — point both at absent tmp paths so a test run on a real Pi never
    reads the box's live ledgers/cap and 429s a happy-path test."""
    monkeypatch.setenv("JASPER_USAGE_DB", str(tmp_path / "usage.db"))
    monkeypatch.setenv(
        "JASPER_VOICE_PROVIDER_FILE", str(tmp_path / "voice_provider.env"),
    )
    monkeypatch.delenv("JASPER_DAILY_SPEND_CAP_USD", raising=False)
    monkeypatch.delenv("JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER", raising=False)
    correction_setup._tuning_last_paid_call[0] = 0.0
    correction_setup._tuning_unpriced_warned.clear()
    yield
    correction_setup._tuning_last_paid_call[0] = 0.0
    correction_setup._tuning_unpriced_warned.clear()


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


# --- P6 tuning spend ledger: gate before + record after ----------------


@pytest.fixture()
def _tuning_ledger_env(tmp_path, monkeypatch):
    """Point the tuning ledger at a temp dir, default the tuning model, and
    enable the key gate. Yields the usage-db path (its sibling is the tuning
    ledger). No writer reset needed: the record path opens a fresh store per
    record (nothing process-global to leak between tests)."""
    usage_db = tmp_path / "usage.db"
    monkeypatch.setenv("JASPER_USAGE_DB", str(usage_db))
    monkeypatch.setenv("JASPER_TUNING_LLM_MODEL", "gpt-5.4")
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: True,
    )
    monkeypatch.setattr(correction_setup, "_get_or_create_session", _fake_session)
    return usage_db


def _advisor_returning(usage: dict):
    def _fake(session, **kwargs):
        return {"kind": "jts_correction_interpret", "explanation": "ok", "usage": usage}
    return _fake


def test_interpret_records_row_with_provider_model_and_cost(
    monkeypatch, _tuning_ledger_env
):
    """A successful interpret lands one priced row in the TUNING ledger with
    provider=openai + the resolved model, and cost > 0 (the synthesized
    text-modality details avoid the $0 audio-rate trap)."""
    usage_db = _tuning_ledger_env
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 1000, "output_tokens": 1000}),
    )
    out = correction_setup._handle_interpret(_FakeHandler())
    assert out["explanation"] == "ok"

    from jasper.usage import UsageStore, tuning_usage_db_path

    tuning_db = tuning_usage_db_path(str(usage_db))
    ro = UsageStore(tuning_db, read_only=True)
    assert ro.spend_last_24h_usd() == pytest.approx(0.0175)  # 1000/1000 @ gpt-5.4 text
    rows = list(ro._conn.execute("SELECT provider, cost_usd FROM sessions"))
    assert len(rows) == 1
    assert rows[0][0] == "openai"
    assert rows[0][1] == pytest.approx(0.0175)


def test_record_never_opens_main_usage_db_read_write(
    monkeypatch, _tuning_ledger_env
):
    """correction-web must NEVER open the jasper-voice-owned usage.db
    read-write (the 2026-06-19 wedge). Assert every UsageStore this code path
    constructs targets the tuning sibling, and the main DB is never created."""
    usage_db = _tuning_ledger_env
    from jasper import usage as usage_mod

    opened: list[tuple] = []
    real_init = usage_mod.UsageStore.__init__

    def spy_init(self, db_path, *a, **kw):
        opened.append((db_path, kw.get("read_only", False)))
        return real_init(self, db_path, *a, **kw)

    monkeypatch.setattr(usage_mod.UsageStore, "__init__", spy_init)
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 500, "output_tokens": 500}),
    )
    correction_setup._handle_interpret(_FakeHandler())

    from jasper.usage import tuning_usage_db_path

    tuning_db = tuning_usage_db_path(str(usage_db))
    # The real invariant: NO read-WRITE open of the main usage.db, ever. (A
    # read-ONLY open via household_usage_reader is legitimate when the file
    # exists — the cap gate does exactly that — so don't over-pin "never
    # constructed at all".)
    assert all(
        ro is True for path, ro in opened if path == str(usage_db)
    ), opened
    # The one write path targeted the tuning sibling.
    assert any(path == tuning_db and ro is False for path, ro in opened), opened
    # And the main DB file was never created on disk by this code path.
    assert not usage_db.exists()


def test_record_is_fail_soft_on_unwritable_dir(monkeypatch, tmp_path, caplog):
    """An unwritable tuning-DB dir must not block the response: the handler
    returns the normal advisor payload and logs tuning_spend.record_failed."""
    # A usage_db path whose parent dir does not exist and cannot be created
    # (parent is a FILE, so mkdir raises).
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    usage_db = blocker / "usage.db"
    monkeypatch.setenv("JASPER_USAGE_DB", str(usage_db))
    monkeypatch.setenv("JASPER_TUNING_LLM_MODEL", "gpt-5.4")
    monkeypatch.setattr(
        "jasper.calibration_agent.key_provisioning.tuning_llm_available",
        lambda **_: True,
    )
    monkeypatch.setattr(correction_setup, "_get_or_create_session", _fake_session)
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 100, "output_tokens": 100}),
    )
    with caplog.at_level("WARNING"):
        out = correction_setup._handle_interpret(_FakeHandler())
    assert out["explanation"] == "ok"  # user got their answer
    assert any("tuning_spend.record_failed" in r.message for r in caplog.records)


def test_created_tuning_db_is_0644_under_restrictive_umask(
    monkeypatch, _tuning_ledger_env
):
    """The DB file must end up 0644 regardless of a 0077 UMask (the root unit's
    umask), so the jasper-group readers (voice/web/doctor) can open it ro."""
    usage_db = _tuning_ledger_env
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 100, "output_tokens": 100}),
    )
    old = os.umask(0o077)
    try:
        correction_setup._handle_interpret(_FakeHandler())
    finally:
        os.umask(old)

    from jasper.usage import tuning_usage_db_path

    tuning_db = tuning_usage_db_path(str(usage_db))
    assert oct(os.stat(tuning_db).st_mode & 0o777) == "0o644"


def test_record_from_two_threads_lands_two_priced_rows(_tuning_ledger_env):
    """Regression shaped like the review-proven Blocker 1: correction-web is a
    ThreadingHTTPServer (one thread per TCP connection), and a process-global
    sqlite store binds to its creator thread (check_same_thread=True) — the
    old global-store shape recorded 1 row of 2 while still logging a
    "recorded" event for both (the cross-thread ProgrammingError is swallowed
    by open_session's fail-soft). The record path must land ONE PRICED ROW PER
    CALL regardless of which thread runs it."""
    usage_db = _tuning_ledger_env
    out = {
        "kind": "jts_correction_interpret",
        "usage": {"input_tokens": 1000, "output_tokens": 1000},
    }

    def _record():
        # The record path is fail-soft by contract (never raises); if it ever
        # does, threading's default excepthook prints the traceback and the
        # row-count assertion below fails.
        correction_setup._record_tuning_spend(dict(out), str(usage_db))

    # Two calls on two DIFFERENT threads (sequential — the failure is
    # thread-identity, not a race).
    for _ in range(2):
        t = threading.Thread(target=_record)
        t.start()
        t.join()

    from jasper.usage import UsageStore, tuning_usage_db_path

    ro = UsageStore(tuning_usage_db_path(str(usage_db)), read_only=True)
    rows = list(ro._conn.execute("SELECT cost_usd FROM sessions"))
    assert len(rows) == 2, rows
    assert all(cost == pytest.approx(0.0175) for (cost,) in rows), rows


def test_preexisting_0600_ledger_healed_to_0644_on_next_record(
    monkeypatch, _tuning_ledger_env
):
    """Perms are SELF-HEALING per record, not first-create-only: a crash in
    the create→chmod window (or one failed chmod) leaves a 0600 file that
    non-root readers silently count as zero forever. The next record must
    repair it."""
    usage_db = _tuning_ledger_env
    from jasper.usage import UsageStore, tuning_usage_db_path

    tuning_db = tuning_usage_db_path(str(usage_db))
    store = UsageStore(tuning_db)  # pre-create the ledger…
    store._conn.close()
    os.chmod(tuning_db, 0o600)  # …in the crash-window shape

    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 100, "output_tokens": 100}),
    )
    correction_setup._handle_interpret(_FakeHandler())
    assert oct(os.stat(tuning_db).st_mode & 0o777) == "0o644"


# --- spend-cap gate: 429 with honest JSON on BOTH handlers -------------


def _fund_household_over_cap(usage_db, cost_usd: float = 5.0):
    """Write a costly row into the voice usage DB so the household aggregate
    exceeds any small cap — no paid call needed."""
    from datetime import datetime, timezone

    from jasper.usage import UsageStore

    store = UsageStore(str(usage_db))
    sid = store.open_session(provider="openai")
    store._conn.execute(
        "UPDATE sessions SET ended_at = ?, cost_usd = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), cost_usd, sid),
    )
    store._conn.close()


@pytest.mark.parametrize("handler_name", ["_handle_interpret", "_handle_propose"])
def test_cap_exceeded_refuses_both_handlers(
    monkeypatch, _tuning_ledger_env, handler_name
):
    """When household spend ≥ cap, BOTH paid handlers refuse with
    SpendCapExceeded (→ 429) and never call the advisor."""
    usage_db = _tuning_ledger_env
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "1.00")
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER", "1.0")
    _fund_household_over_cap(usage_db, cost_usd=5.0)

    called = {"advisor": False}

    def _boom(session, **kwargs):
        called["advisor"] = True
        return {"kind": "x"}

    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret", _boom
    )
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.propose", _boom
    )
    handler = getattr(correction_setup, handler_name)
    with pytest.raises(correction_setup.SpendCapExceeded, match="rollover"):
        handler(_FakeHandler())
    assert called["advisor"] is False  # refused BEFORE any paid call


def test_cap_under_limit_proceeds(monkeypatch, _tuning_ledger_env):
    """Under the cap, the paid call proceeds normally."""
    usage_db = _tuning_ledger_env
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "100.00")
    _fund_household_over_cap(usage_db, cost_usd=0.10)  # well under
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 100, "output_tokens": 100}),
    )
    out = correction_setup._handle_interpret(_FakeHandler())
    assert out["explanation"] == "ok"


def test_cap_zero_means_disabled_allows(monkeypatch, _tuning_ledger_env):
    """cap ≤ 0 disables the breaker — the paid call is allowed even with huge
    recorded spend."""
    usage_db = _tuning_ledger_env
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "0")
    _fund_household_over_cap(usage_db, cost_usd=999.0)
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 100, "output_tokens": 100}),
    )
    out = correction_setup._handle_interpret(_FakeHandler())
    assert out["explanation"] == "ok"


# --- Blocker 2: the cap comes from the wizard file, not frozen env -----
#
# The /voice spend-cap form writes the cap knobs into voice_provider.env,
# which jasper-voice sources but the correction-web unit does not — and this
# process is not restarted on a save. _spend_settings must read the wizard
# file FRESH per call, file winning over os.environ (matching jasper-voice's
# EnvironmentFile order where the wizard file is sourced last).


def _write_wizard_cap(tmp_path, **keys) -> None:
    """Write spend keys into the tmp voice_provider.env the autouse fixture
    points JASPER_VOICE_PROVIDER_FILE at."""
    lines = "".join(f"{k}={v}\n" for k, v in keys.items())
    (tmp_path / "voice_provider.env").write_text(lines)


def test_wizard_file_cap_wins_over_process_env(
    monkeypatch, tmp_path, _tuning_ledger_env
):
    """A household lowering the cap on /voice must bind the tuning surface
    even though this unit's start-time env still carries the old value."""
    usage_db = _tuning_ledger_env
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "100.00")  # stale env
    _write_wizard_cap(
        tmp_path,
        JASPER_DAILY_SPEND_CAP_USD="0.25",  # wizard-set household cap
        JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER="1.0",
    )
    _fund_household_over_cap(usage_db, cost_usd=0.50)  # over 0.25, under 100

    _db, cap_usd, multiplier = correction_setup._spend_settings()
    assert cap_usd == pytest.approx(0.25)  # file wins
    assert multiplier == pytest.approx(1.0)

    called = {"advisor": False}

    def _boom(session, **kwargs):
        called["advisor"] = True
        return {"kind": "x"}

    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret", _boom
    )
    with pytest.raises(correction_setup.SpendCapExceeded):
        correction_setup._handle_interpret(_FakeHandler())
    assert called["advisor"] is False


def test_wizard_file_absent_falls_back_to_env_then_default(
    monkeypatch, _tuning_ledger_env
):
    """No wizard file (fresh box / operator-only setup): the env value
    applies; with neither, the shared jasper.usage defaults."""
    from jasper.usage import (
        DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
        DEFAULT_DAILY_SPEND_CAP_USD,
    )

    # The autouse fixture points JASPER_VOICE_PROVIDER_FILE at an absent tmp
    # file and clears the cap env vars → pure defaults.
    _db, cap_usd, multiplier = correction_setup._spend_settings()
    assert cap_usd == pytest.approx(DEFAULT_DAILY_SPEND_CAP_USD)
    assert multiplier == pytest.approx(DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER)

    # Env applies when the file stays absent.
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "3.50")
    _db, cap_usd, _m = correction_setup._spend_settings()
    assert cap_usd == pytest.approx(3.50)


def test_wizard_file_cap_zero_disables_and_call_proceeds(
    monkeypatch, tmp_path, _tuning_ledger_env
):
    """The documented cap=0 disable contract must hold when the ZERO comes
    from the wizard file — the old env-only read kept 429ing at the stale
    env/default cap after a household disabled the cap on /voice."""
    usage_db = _tuning_ledger_env
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "1.00")  # stale env cap
    _write_wizard_cap(tmp_path, JASPER_DAILY_SPEND_CAP_USD="0.00")
    _fund_household_over_cap(usage_db, cost_usd=999.0)  # over any real cap
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 100, "output_tokens": 100}),
    )
    out = correction_setup._handle_interpret(_FakeHandler())
    assert out["explanation"] == "ok"


# --- SF: unpriced tuning-model override warns instead of silent $0 -----


def test_unpriced_tuning_model_records_zero_and_warns_once(
    monkeypatch, _tuning_ledger_env, caplog
):
    """An operator JASPER_TUNING_LLM_MODEL override with no pricing row
    records $0 (fail-open — the call already happened) but must emit the
    pricing.unpriced WARN with surface=tuning, once per process per model —
    the same journal signal the voice surface emits for this exact trap."""
    usage_db = _tuning_ledger_env
    monkeypatch.setenv("JASPER_TUNING_LLM_MODEL", "gpt-99-imaginary")
    monkeypatch.setattr(
        "jasper.calibration_agent.correction_advisor.interpret",
        _advisor_returning({"input_tokens": 1000, "output_tokens": 1000}),
    )
    with caplog.at_level("WARNING"):
        correction_setup._handle_interpret(_FakeHandler())
        correction_setup._tuning_last_paid_call[0] = 0.0  # re-arm min-interval
        correction_setup._handle_interpret(_FakeHandler())

    from jasper.usage import UsageStore, tuning_usage_db_path

    ro = UsageStore(tuning_usage_db_path(str(usage_db)), read_only=True)
    rows = list(ro._conn.execute("SELECT cost_usd FROM sessions"))
    assert len(rows) == 2
    assert all(cost == 0.0 for (cost,) in rows)  # unpriced → $0, still recorded

    warned = [
        r for r in caplog.records
        if "pricing.unpriced" in r.message and "surface=tuning" in r.message
    ]
    assert len(warned) == 1, "warn once per process per model, not per tap"
    assert "gpt-99-imaginary" in warned[0].message
