# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shape pins for the room-correction *screen envelope* (revision plan §3.2).

The envelope is the dumb-frontend contract: one server-computed JSON
object per step. These tests pin:

  - every :class:`SessionState` maps to one of the eight logical screens;
  - the envelope's top-level shape (schema_version, the full key set);
  - a mid-flow envelope (sweep) with server-smoothed curves;
  - a verified envelope carries the P3a fill_segments + one-number headline;
  - an idle envelope has the entry next_action and no headline;
  - nudges appear (never as a block) for uncalibrated-mic and low-confidence
    fixtures, and next_action is correct per screen.

It is a *pure* shape/behaviour pin — no CamillaDSP, no HTTP. The endpoint
wiring (`/envelope`) is a thin `build_envelope_logged` call over
`_get_or_create_session()`, exercised by the web wiring test at the end.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from jasper.correction import envelope
from jasper.correction.session import CurveJSON, SessionState

ENVELOPE_KEYS = {
    "schema_version",
    "screen",
    "state",
    "sections",
    "curves",
    "fill_segments",
    "headline",
    "verdict",
    "verdict_text",
    "nudges",
    "next_action",
    "progress",
    "tuning_llm",
}

LOGICAL_SCREENS = {
    "idle",
    "mic",
    "level",
    "sweep",
    "review",
    "apply",
    "verify",
    "result",
}


class _FakeAutolevel:
    def __init__(self, status: str = "idle") -> None:
        self._status = status

    def snapshot(self) -> dict[str, object]:
        return {"status": self._status}


class _FakeSession:
    """Minimal duck-typed stand-in for MeasurementSession.

    The envelope builder reads a fixed, documented set of attributes; a
    fake keeps the pins fast and free of the full session machinery
    (matching the duck-typed `Any` seam status.py already uses).
    """

    def __init__(self, state: SessionState = SessionState.IDLE) -> None:
        self.session_id = "sess001"
        self.state = state
        self.error: str | None = None
        self.total_positions = 3
        self.current_position = 0
        self.autolevel = _FakeAutolevel("idle")
        self.measured_curve: CurveJSON | None = None
        self.target_curve: CurveJSON | None = None
        self.predicted_curve: CurveJSON | None = None
        self.verify_curve: CurveJSON | None = None
        self.verify_before_after: dict[str, object] | None = None
        self.acceptance: dict[str, object] | None = None
        self.auto_revert_outcome: dict[str, object] | None = None
        self.confidence_report: dict[str, object] | None = None
        self.design_report: dict[str, object] | None = None


def _relay_session(
    state: SessionState,
    *,
    level_state: str = "locked",
    restored: bool = False,
    lock_kind: str | None = None,
    window_shortfall_db: float | None = None,
) -> _FakeSession:
    sess = _FakeSession(state)
    sess.capture_transport = "relay"
    ramp = {"state": level_state, "restored": restored}
    if lock_kind is not None:
        ramp["lock_kind"] = lock_kind
    if window_shortfall_db is not None:
        ramp["window_shortfall_db"] = window_shortfall_db
    sess.level_match_snapshot = lambda: {
        "running": False,
        "locks": {},
        "last": {
            "ramp": ramp,
        },
    }
    return sess


def _log_grid(n: int = 480) -> np.ndarray:
    return np.geomspace(20.0, 20000.0, n)


def _jagged_curve() -> CurveJSON:
    f = _log_grid()
    # A deliberately jagged trace so smoothing is observable.
    mag = 4.0 * np.sin(f / 37.0) + 1.5 * np.sin(f / 3.0)
    return CurveJSON(f.tolist(), mag.tolist())


def _flat_curve() -> CurveJSON:
    f = _log_grid()
    return CurveJSON(f.tolist(), np.zeros(f.size).tolist())


def _verify_before_after() -> dict[str, object]:
    return {
        "band_hz": [50.0, 350.0],
        "before": {"rms_db": 4.1, "max_db": 6.0, "n_points": 120},
        "after": {"rms_db": 1.2, "max_db": 2.0, "n_points": 120},
        "delta": {"rms_db": 2.9, "max_db": 4.0},
        "fill_segments": [
            {"tone": "improved", "i_lo": 40, "i_hi": 120,
             "f_lo_hz": 50.0, "f_hi_hz": 130.0},
            {"tone": "regressed", "i_lo": 121, "i_hi": 140,
             "f_lo_hz": 131.0, "f_hi_hz": 180.0},
        ],
    }


# ---------- state -> screen total coverage ---------------------------------


def test_every_session_state_maps_to_a_logical_screen():
    """No backend state may leave the wizard with an undefined screen."""
    for state in SessionState:
        screen = envelope.screen_for_state(state.value)
        assert screen in LOGICAL_SCREENS, (state, screen)


def test_state_screen_map_covers_exactly_the_state_enum():
    """The static map has one entry per state — a new state without a
    screen mapping is a bug this pin catches (it would otherwise silently
    fall back to 'idle')."""
    mapped = set(envelope._STATE_SCREEN)
    enum_values = {s.value for s in SessionState}
    assert mapped == enum_values


def test_state_screen_map_spot_checks():
    assert envelope.screen_for_state("idle") == "idle"
    assert envelope.screen_for_state("needs_noise_capture") == "mic"
    assert envelope.screen_for_state("sweeping") == "sweep"
    assert envelope.screen_for_state("awaiting_capture") == "sweep"
    assert envelope.screen_for_state("ready") == "review"
    assert envelope.screen_for_state("analyzing") == "review"
    assert envelope.screen_for_state("applied") == "apply"
    assert envelope.screen_for_state("verifying") == "verify"
    assert envelope.screen_for_state("awaiting_verify_capture") == "verify"
    assert envelope.screen_for_state("verified") == "result"
    assert envelope.screen_for_state("failed") == "result"


def test_unknown_state_value_fails_closed_instead_of_offering_start():
    with pytest.raises(ValueError, match="unsupported room-correction state"):
        envelope.screen_for_state("some_future_state")


# ---------- top-level shape ------------------------------------------------


def test_schema_version_is_six():
    # v2 added the P4 `verdict` block; v3 added the P5 crossover-region
    # distinction (REVIEW verdict_text + crossover_region_dip_not_boosted nudge);
    # v4 (P6) added the `tuning_llm` affordance block.
    # v5 wires the relay-owned level-before-sweep actions; v6 makes the
    # ordered section list the sole whole-page visibility authority.
    assert envelope.ENVELOPE_SCHEMA_VERSION == 6
    env = envelope.build_envelope(_FakeSession())
    assert env["schema_version"] == 6


def test_tuning_llm_block_offered_on_review_shape_pinned(monkeypatch):
    # P6: the affordance block always carries `offered` (measurement-screen
    # gate) + `available`/`provider`; `nudge` when no OpenAI key is
    # configured. READY maps to the review screen (a measurement screen).
    # HERMETIC: availability is monkeypatched so BOTH branches are pinned
    # deterministically regardless of the test host's key state (a keyed
    # dev box must not silently skip the nudge assertions).
    from jasper.calibration_agent import key_provisioning as kp

    monkeypatch.setattr(
        kp, "availability",
        lambda **_: kp.TuningAvailability(
            available=False, model="", nudge="Add an OpenAI key at /voice …",
        ),
    )
    env = envelope.build_envelope(_FakeSession(SessionState.READY))
    block = env["tuning_llm"]
    assert block["offered"] is True
    assert block["provider"] == "openai"
    # Offered-but-unavailable: the nudge is present, no model id leaks.
    assert block["available"] is False
    assert isinstance(block["nudge"], str) and block["nudge"]
    assert "model" not in block

    monkeypatch.setattr(
        kp, "availability",
        lambda **_: kp.TuningAvailability(available=True, model="gpt-5.4"),
    )
    env2 = envelope.build_envelope(_FakeSession(SessionState.READY))
    block2 = env2["tuning_llm"]
    assert block2["offered"] is True
    assert block2["available"] is True
    assert block2["model"] == "gpt-5.4"
    assert "nudge" not in block2


def test_tuning_llm_not_offered_before_measurement():
    # Pre-measurement screens (idle/mic/sweep) never offer the affordance.
    env = envelope.build_envelope(_FakeSession(SessionState.IDLE))
    assert env["tuning_llm"]["offered"] is False


def test_envelope_top_level_shape_is_pinned():
    env = envelope.build_envelope(_FakeSession())
    assert set(env) == ENVELOPE_KEYS


def test_section_vocabulary_is_exact_and_room_owned():
    assert envelope.SECTION_VOCABULARY == {
        "current-correction",
        "run-defaults",
        "readiness-blocker",
        "capture-handoff",
        "placement",
        "capture-setup",
        "local-certificate-warning",
        "level-check",
        "position-capture",
        "measurement-review",
        "apply-status",
        "verification",
        "result-proof",
        "tuning",
        "reports",
    }


@pytest.mark.parametrize(
    ("state", "sections"),
    [
        (SessionState.IDLE, ["current-correction", "run-defaults"]),
        (
            SessionState.NEEDS_NOISE_CAPTURE,
            [
                "run-defaults",
                "capture-handoff",
                "placement",
                "local-certificate-warning",
                "capture-setup",
            ],
        ),
        (
            SessionState.AWAITING_CAPTURE,
            ["capture-handoff", "placement", "position-capture"],
        ),
        (SessionState.READY, ["measurement-review", "tuning"]),
        (SessionState.APPLIED, ["apply-status", "tuning"]),
        (
            SessionState.AWAITING_VERIFY_CAPTURE,
            ["capture-handoff", "placement", "verification", "tuning"],
        ),
        (
            SessionState.VERIFIED,
            ["current-correction", "result-proof", "tuning"],
        ),
    ],
)
def test_sections_are_server_ordered_for_each_screen(state, sections):
    env = envelope.build_envelope(_FakeSession(state))
    assert env["sections"] == sections


def test_relay_handoff_omits_local_only_sections():
    env = envelope.build_envelope(
        _relay_session(SessionState.NEEDS_NOISE_CAPTURE, level_state="locked")
    )
    assert env["sections"] == [
        "run-defaults",
        "capture-handoff",
        "placement",
    ]
    assert "capture-setup" not in env["sections"]
    assert "local-certificate-warning" not in env["sections"]


def test_local_mic_setup_is_the_envelope_owned_primary_action():
    sess = _FakeSession(SessionState.NEEDS_NOISE_CAPTURE)
    env = envelope.build_envelope(sess)
    assert env["next_action"] == {
        "label": "Allow microphone",
        "endpoint": "/local-capture/setup",
    }
    sess.local_capture_setup_bound = True
    bound = envelope.build_envelope(sess)
    assert bound["screen"] == "level"
    assert bound["sections"] == [
        "capture-handoff",
        "placement",
        "level-check",
    ]
    assert bound["next_action"] == {
        "label": "Check measurement level",
        "endpoint": "/autolevel/start",
    }


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("ramping", None),
        (
            "locked",
            {"label": "Measure this position", "endpoint": "/upload-noise"},
        ),
        (
            "maxed_out",
            {"label": "Retry level check", "endpoint": "/autolevel/start"},
        ),
        (
            "cancelled",
            {"label": "Retry level check", "endpoint": "/autolevel/start"},
        ),
        (
            "error",
            {"label": "Retry level check", "endpoint": "/autolevel/start"},
        ),
    ],
)
def test_local_first_position_requires_level_before_noise_upload(status, action):
    sess = _FakeSession(SessionState.NEEDS_NOISE_CAPTURE)
    sess.local_capture_setup_bound = True
    sess.autolevel = _FakeAutolevel(status)

    env = envelope.build_envelope(sess)

    assert env["screen"] == "level"
    assert env["next_action"] == action
    if status == "maxed_out":
        assert "too quiet" in env["verdict_text"].lower()


def test_local_later_position_reuses_bound_setup_without_leveling_again():
    sess = _FakeSession(SessionState.NEEDS_NOISE_CAPTURE)
    sess.local_capture_setup_bound = True
    sess.current_position = 1

    env = envelope.build_envelope(sess)

    assert env["screen"] == "sweep"
    assert env["next_action"] == {
        "label": "Measure this position",
        "endpoint": "/upload-noise",
    }


@pytest.mark.parametrize("state", [SessionState.IDLE, SessionState.VERIFIED])
def test_reports_section_is_conditional_on_static_edge_availability(state):
    without_reports = envelope.build_envelope(
        _FakeSession(state), reports_available=False,
    )
    with_reports = envelope.build_envelope(
        _FakeSession(state), reports_available=True,
    )
    assert "reports" not in without_reports["sections"]
    assert with_reports["sections"][-1] == "reports"


def test_reports_section_never_appears_on_active_screen():
    env = envelope.build_envelope(
        _FakeSession(SessionState.AWAITING_CAPTURE),
        reports_available=True,
    )
    assert "reports" not in env["sections"]


def test_web_handler_never_scans_reports_on_active_poll(monkeypatch):
    from jasper.correction import bundles
    from jasper.web import correction_setup

    sess = _FakeSession(SessionState.AWAITING_CAPTURE)
    sess.cfg = SimpleNamespace(sessions_dir="unused")
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("active envelope polled the session store")

    monkeypatch.setattr(bundles, "list_bundles", fail_if_called)
    body = correction_setup._handle_envelope(
        SimpleNamespace(path="/envelope")
    )
    assert body["screen"] == "sweep"
    assert "reports" not in body["sections"]


def test_web_handler_adds_reports_only_when_static_store_has_one(monkeypatch):
    from jasper.capture_relay import correction_adapter
    from jasper.correction import bundles
    from jasper.web import correction_setup

    sess = _FakeSession(SessionState.IDLE)
    sess.cfg = SimpleNamespace(sessions_dir="unused")
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(correction_adapter, "relay_enabled", lambda: True)
    monkeypatch.setattr(bundles, "list_bundles", lambda *_args, **_kwargs: [{}])

    body = correction_setup._handle_envelope(
        SimpleNamespace(path="/envelope?capture_transport=local")
    )
    assert body["sections"] == [
        "current-correction",
        "run-defaults",
        "reports",
    ]


def test_web_handler_report_discovery_failure_does_not_block_idle(monkeypatch):
    from jasper.correction import bundles
    from jasper.web import correction_setup

    sess = _FakeSession(SessionState.IDLE)
    sess.cfg = SimpleNamespace(sessions_dir="unavailable")
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        bundles,
        "list_bundles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
    )

    body = correction_setup._handle_envelope(SimpleNamespace(path="/envelope"))

    assert body["screen"] == "idle"
    assert body["next_action"]["endpoint"] == "/start"
    assert "reports" not in body["sections"]


def test_idle_envelope_has_entry_action_and_no_headline():
    env = envelope.build_envelope(_FakeSession(SessionState.IDLE))
    assert env["screen"] == "idle"
    assert env["state"] == "idle"
    assert env["sections"] == ["current-correction", "run-defaults"]
    assert env["headline"] is None
    assert env["fill_segments"] == []
    assert env["curves"] == {}
    assert env["nudges"] == []
    assert env["next_action"] == {"label": "Start measuring", "endpoint": "/start"}
    assert env["progress"] == {"position": 1, "total": 6}
    assert isinstance(env["verdict_text"], str) and env["verdict_text"]
    assert env["verdict"] is None  # no verify yet → no acceptance verdict


def test_relay_next_position_has_one_compound_forward_action():
    env = envelope.build_envelope(
        _relay_session(SessionState.NEEDS_NEXT_POSITION)
    )
    assert env["next_action"] == {
        "label": "Measure next position",
        "endpoint": "/next-position",
    }


def test_relay_verified_result_does_not_loop_back_to_level_check():
    sess = _relay_session(SessionState.VERIFIED, restored=True)
    env = envelope.build_envelope(sess)
    assert env["screen"] == "result"
    assert env["next_action"] == {
        "label": "Measure again",
        "endpoint": "/start",
    }


def test_relay_pending_confirmation_reuses_retained_level_for_second_verify():
    sess = _relay_session(SessionState.VERIFIED, restored=True)
    sess.acceptance = {"verdict": "revert_pending_confirm"}
    env = envelope.build_envelope(sess)
    assert env["screen"] == "result"
    assert env["next_action"]["endpoint"] == "/relay/verify"


# ---------- level screen (autolevel sub-state) -----------------------------


def test_level_screen_surfaces_from_autolevel_ramping_while_idle():
    sess = _FakeSession(SessionState.IDLE)
    sess.autolevel = _FakeAutolevel("ramping")
    env = envelope.build_envelope(sess)
    assert env["screen"] == "level"
    # level has no forward action — the ramp locks on its own.
    assert env["next_action"] is None
    # It collapses onto the idle spine position for progress.
    assert env["progress"]["position"] == 1


def test_locked_autolevel_does_not_force_level_screen():
    sess = _FakeSession(SessionState.IDLE)
    sess.autolevel = _FakeAutolevel("locked")
    assert envelope.build_envelope(sess)["screen"] == "idle"


def test_autolevel_ramping_does_not_override_a_non_idle_state():
    # Once sweeping, we're past level-match; a stale ramping flag must not
    # drag the screen back to "level".
    sess = _FakeSession(SessionState.SWEEPING)
    sess.autolevel = _FakeAutolevel("ramping")
    assert envelope.build_envelope(sess)["screen"] == "sweep"


def test_missing_or_broken_autolevel_is_tolerated():
    sess = _FakeSession(SessionState.IDLE)
    sess.autolevel = None  # type: ignore[assignment]
    assert envelope.build_envelope(sess)["screen"] == "idle"


# ---------- mid-flow (sweep) with server-smoothed curves -------------------


def test_sweep_envelope_curves_are_server_smoothed():
    sess = _FakeSession(SessionState.AWAITING_CAPTURE)
    raw = _jagged_curve()
    sess.measured_curve = raw
    sess.target_curve = _flat_curve()
    env = envelope.build_envelope(sess)

    assert env["screen"] == "sweep"
    assert set(env["curves"]) == {"measured", "target"}

    # measured is smoothed: same grid length, but not the raw values.
    measured = env["curves"]["measured"]
    assert measured["freqs_hz"] == raw.freqs_hz
    assert len(measured["magnitude_db"]) == len(raw.magnitude_db)
    assert not np.allclose(
        np.array(measured["magnitude_db"]), np.array(raw.magnitude_db)
    ), "measured curve should be fractional-octave smoothed for display"

    # target is a designed curve → passed through unsmoothed.
    assert np.allclose(env["curves"]["target"]["magnitude_db"], 0.0)


def test_absent_curves_are_omitted_not_null():
    sess = _FakeSession(SessionState.AWAITING_CAPTURE)
    sess.measured_curve = _jagged_curve()
    # no target/predicted/verify set
    env = envelope.build_envelope(sess)
    assert list(env["curves"]) == ["measured"]


def test_malformed_curve_is_dropped():
    sess = _FakeSession(SessionState.READY)
    # mismatched lengths -> dropped, not a crash
    sess.measured_curve = CurveJSON([20.0, 80.0, 200.0], [1.0, 2.0])
    env = envelope.build_envelope(sess)
    assert "measured" not in env["curves"]


def test_review_screen_next_action_is_apply():
    sess = _FakeSession(SessionState.READY)
    env = envelope.build_envelope(sess)
    assert env["screen"] == "review"
    assert env["next_action"] == {"label": "Apply correction", "endpoint": "/apply"}
    assert env["progress"] == {"position": 3, "total": 6}


def test_apply_screen_next_action_is_verify():
    sess = _FakeSession(SessionState.APPLIED)
    env = envelope.build_envelope(sess)
    assert env["screen"] == "apply"
    assert env["next_action"] == {"label": "Verify the result", "endpoint": "/verify"}


def test_verify_screen_has_no_forward_action():
    # The browser drives the verify capture upload; there is no button.
    sess = _FakeSession(SessionState.AWAITING_VERIFY_CAPTURE)
    env = envelope.build_envelope(sess)
    assert env["screen"] == "verify"
    assert env["next_action"] is None


# ---------- verified: fill_segments + headline -----------------------------


def test_verified_envelope_has_fill_segments_and_headline():
    sess = _FakeSession(SessionState.VERIFIED)
    sess.measured_curve = _jagged_curve()
    sess.target_curve = _flat_curve()
    sess.verify_curve = CurveJSON(
        _log_grid().tolist(), (0.4 * np.sin(_log_grid() / 37.0)).tolist()
    )
    vba = _verify_before_after()
    sess.verify_before_after = vba
    env = envelope.build_envelope(sess)

    assert env["screen"] == "result"
    # fill_segments RELAYED verbatim from P3a's verify_before_after.
    assert env["fill_segments"] == vba["fill_segments"]

    headline = env["headline"]
    assert headline is not None
    assert headline["before_max_db"] == 6.0
    assert headline["after_max_db"] == 2.0
    assert headline["rms_delta_db"] == 2.9
    assert headline["max_delta_db"] == 4.0
    assert headline["band_hz"] == [50.0, 350.0]
    # One-number, homeowner phrasing.
    assert "±6 dB" in headline["text"]
    assert "±2 dB" in headline["text"]

    # verify curve is smoothed + present alongside measured/target.
    assert set(env["curves"]) == {"measured", "target", "verify"}

    # verdict text folds the headline in; the result offers "measure again".
    assert "±6 dB" in env["verdict_text"]
    assert env["next_action"] == {"label": "Measure again", "endpoint": "/start"}


def test_headline_absent_until_verify_before_after_present():
    sess = _FakeSession(SessionState.APPLIED)
    assert envelope.build_envelope(sess)["headline"] is None


def test_headline_none_when_before_after_malformed():
    sess = _FakeSession(SessionState.VERIFIED)
    sess.verify_before_after = {"band_hz": [50.0, 350.0]}  # missing before/after
    env = envelope.build_envelope(sess)
    assert env["headline"] is None
    assert env["fill_segments"] == []


def test_failed_state_result_screen_carries_error_in_verdict():
    sess = _FakeSession(SessionState.FAILED)
    sess.error = "capture too quiet"
    env = envelope.build_envelope(sess)
    assert env["screen"] == "result"
    assert "capture too quiet" in env["verdict_text"]
    assert env["headline"] is None


# ---------- nudges: never block, homeowner language ------------------------


def test_uncalibrated_mic_nudge_present_and_non_blocking():
    sess = _FakeSession(SessionState.READY)
    sess.confidence_report = {
        "level": "medium",
        "score": 70,
        "findings": [
            {"code": "uncalibrated_mic", "severity": "warn",
             "message": "no measurement-mic calibration was applied"},
        ],
    }
    env = envelope.build_envelope(sess)
    nudges = env["nudges"]
    assert len(nudges) == 1
    n = nudges[0]
    assert n["code"] == "uncalibrated_mic"
    # Never a block — the strongest nudge severity is "warn".
    assert n["severity"] in {"info", "warn"}
    assert n["severity"] == "info"
    # Homeowner copy, explicitly non-gating.
    assert "approximate" in n["text"].lower()
    assert "continue" in n["text"].lower()
    # A nudge NEVER removes the forward action.
    assert env["next_action"] == {"label": "Apply correction", "endpoint": "/apply"}


def test_room_bounded_low_level_surfaces_shortfall_without_blocking_sweep():
    sess = _relay_session(
        SessionState.NEEDS_NOISE_CAPTURE,
        restored=True,
        lock_kind="bounded_low_level",
        window_shortfall_db=11.88,
    )

    env = envelope.build_envelope(sess)

    assert env["screen"] == "mic"
    assert env["next_action"] == {
        "endpoint": "/relay/capture",
        "label": "Measure this position",
    }
    nudge = env["nudges"][0]
    assert nudge["code"] == "bounded_low_measurement_level"
    assert nudge["severity"] == "warn"
    assert "11.9 dB below" in nudge["text"]
    assert "verify each sweep" in nudge["text"]


@pytest.mark.parametrize("shortfall", [0.0, -1.0, float("nan"), float("inf")])
def test_room_bounded_low_level_ignores_invalid_shortfall(shortfall):
    sess = _relay_session(
        SessionState.NEEDS_NOISE_CAPTURE,
        restored=True,
        lock_kind="bounded_low_level",
        window_shortfall_db=shortfall,
    )

    env = envelope.build_envelope(sess)

    assert all(
        nudge["code"] != "bounded_low_measurement_level"
        for nudge in env["nudges"]
    )


def test_low_confidence_findings_map_to_nudges():
    sess = _FakeSession(SessionState.READY)
    sess.confidence_report = {
        "level": "low",
        "score": 40,
        "findings": [
            {"code": "single_position", "severity": "warn",
             "message": "only one listening position was measured"},
            {"code": "high_position_variance", "severity": "warn",
             "message": "position variance is high in the correction band"},
            {"code": "capture_snr_low", "severity": "warn",
             "message": "capture SNR is low"},
        ],
    }
    env = envelope.build_envelope(sess)
    codes = {n["code"] for n in env["nudges"]}
    assert {"single_position", "high_position_variance", "capture_snr_low"} <= codes
    # Every nudge is info|warn, never a block.
    assert all(n["severity"] in {"info", "warn"} for n in env["nudges"])
    # Forward action still live under low confidence — quality never gates.
    assert env["next_action"] is not None


def test_fail_severity_finding_is_clamped_to_warn_never_block():
    sess = _FakeSession(SessionState.READY)
    sess.confidence_report = {
        "findings": [
            {"code": "no_completed_positions", "severity": "fail",
             "message": "no completed measurement positions are available"},
        ],
    }
    env = envelope.build_envelope(sess)
    assert len(env["nudges"]) == 1
    assert env["nudges"][0]["severity"] == "warn"  # fail -> warn ceiling


def test_unknown_finding_is_surfaced_degraded_not_dropped():
    sess = _FakeSession(SessionState.READY)
    sess.confidence_report = {
        "findings": [
            {"code": "brand_new_finding", "severity": "fail",
             "message": "a newly added confidence check tripped"},
        ],
    }
    env = envelope.build_envelope(sess)
    assert len(env["nudges"]) == 1
    n = env["nudges"][0]
    assert n["code"] == "brand_new_finding"
    # Unknown -> raw message surfaced, severity clamped into nudge vocab.
    assert n["text"] == "a newly added confidence check tripped"
    assert n["severity"] == "warn"


def test_duplicate_finding_codes_collapse_to_one_nudge():
    sess = _FakeSession(SessionState.READY)
    sess.confidence_report = {
        "findings": [
            {"code": "single_position", "severity": "warn", "message": "a"},
            {"code": "single_position", "severity": "warn", "message": "b"},
        ],
    }
    env = envelope.build_envelope(sess)
    assert len([n for n in env["nudges"] if n["code"] == "single_position"]) == 1


def test_no_confidence_report_means_no_nudges():
    env = envelope.build_envelope(_FakeSession(SessionState.SWEEPING))
    assert env["nudges"] == []


def test_all_canned_nudge_copies_are_non_blocking_severity():
    # Catalogue-level invariant: no nudge copy may be a "block".
    for code, spec in envelope._NUDGE_COPY.items():
        assert spec["severity"] in {"info", "warn"}, code
        assert spec["text"].strip()


# ---------- logged variant emits an event but keeps the shape --------------


def test_build_envelope_logged_matches_pure_builder(caplog):
    sess = _FakeSession(SessionState.READY)
    sess.confidence_report = {
        "findings": [
            {"code": "uncalibrated_mic", "severity": "warn", "message": "x"},
        ],
    }
    import logging
    with caplog.at_level(logging.INFO, logger="jasper.correction.envelope"):
        logged = envelope.build_envelope_logged(sess)
    assert logged == envelope.build_envelope(sess)
    assert any(
        "event=correction_envelope.serve" in rec.getMessage()
        for rec in caplog.records
    )


# ---------- endpoint wiring ------------------------------------------------


def test_envelope_route_is_registered_and_additive():
    """`/envelope` is a recognized GET route and `/status` is untouched
    (additive — the legacy payload keeps its own handler).

    The GET dispatch lives in a nested `Handler` class inside the
    `_make_handler` factory (a closure, not module-accessible), so this
    pins against the module source file directly.
    """
    import inspect

    from jasper.web import correction_setup

    src = inspect.getsource(correction_setup)
    # `/envelope` is in the GET allowlist and has its own dispatch branch.
    assert '"/envelope"' in src
    assert 'path == "/envelope"' in src
    # The additive guarantee: /status still has its own dispatch branch.
    assert 'path == "/status"' in src

    # The handler delegates to the logged builder over the live session.
    handler_src = inspect.getsource(correction_setup._handle_envelope)
    assert "build_envelope_logged" in handler_src
    assert "_get_or_create_session" in handler_src


def test_envelope_endpoint_end_to_end_over_http(tmp_path, monkeypatch):
    """Drive a real MeasurementSession through the real handler + read
    guard over loopback HTTP and assert the envelope shape comes back.

    This closes the loop on the wiring: nginx → handler → build_envelope,
    without any CamillaDSP or capture hardware."""
    import json
    import threading
    import urllib.request

    from jasper.web import correction_setup
    from jasper.correction.session import (
        MeasurementSession,
        SessionConfig,
        SessionState,
    )

    sess = MeasurementSession(
        SessionConfig(
            sweep_dir=tmp_path / "sweeps",
            capture_dir=tmp_path / "captures",
            sessions_dir=tmp_path / "sessions",
            config_dir=tmp_path / "configs",
            base_config_path=tmp_path / "v1.yml",
        ),
    )
    sess.state = SessionState.READY
    sess.confidence_report = {
        "findings": [
            {"code": "uncalibrated_mic", "severity": "warn",
             "message": "no measurement-mic calibration was applied"},
        ],
    }
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: sess,
    )

    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/envelope", timeout=5,
        )
        body = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()

    assert set(body) == ENVELOPE_KEYS
    assert body["schema_version"] == 6
    assert body["screen"] == "review"
    assert body["state"] == "ready"
    assert body["next_action"] == {"label": "Apply correction", "endpoint": "/apply"}
    # The uncalibrated-mic nudge survives the full round-trip, non-blocking.
    assert any(n["code"] == "uncalibrated_mic" for n in body["nudges"])
    assert all(n["severity"] in {"info", "warn"} for n in body["nudges"])


# ---------- P4 verdict-driven copy + flow (blocker + should-fix pins) -------
#
# The deterministic verdict must truthfully reach the household through
# env.verdict_text — the ONLY field the shipped envelope client renders
# (deploy/assets/correction/js/main.js wizardVerdict). These pins cover the
# three revert copies (success / failed / in-flight), the pending-confirm
# next_action override, and the accept/surface headline folds — deleting
# _next_action_for or the _VERDICT_HEADLINE fold must fail tests, not ship.


def _acceptance(verdict: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "verdict": verdict,
        "reasons": ["r"],
        "confirmed": verdict == "revert",
        "verify_index": 2 if verdict == "revert" else 1,
        "basis": "position_1",
        "overall_before_rms_db": 4.0,
        "overall_after_rms_db": 6.0,
        "overall_rms_delta_db": -2.0,
        "regressed_band_count": 1,
        "worst_band_delta_db": -7.0,
        "worst_band_center_hz": 112.0,
        "bands": [],
    }
    base.update(extra)
    return base


def test_successful_auto_revert_is_announced_on_idle():
    """BLOCKER pin: after a successful auto-revert the session is IDLE — the
    envelope must say the correction was removed, never the silent default
    'Ready to measure your room.'"""
    sess = _FakeSession(SessionState.IDLE)
    sess.acceptance = _acceptance("revert")
    sess.auto_revert_outcome = {"result": "ok", "at": 1234.0}
    env = envelope.build_envelope(sess)
    assert env["screen"] == "idle"
    assert "Reverted" in env["verdict_text"]
    assert "removed the correction" in env["verdict_text"]
    assert env["verdict_text"] != "Ready to measure your room."


def test_fresh_idle_session_keeps_default_copy():
    """The post-revert branch must not fire for an ordinary fresh session."""
    env = envelope.build_envelope(_FakeSession(SessionState.IDLE))
    assert env["verdict_text"] == "Ready to measure your room."


def test_failed_auto_revert_says_still_applied():
    """BLOCKER pin: a failed rollback must say the correction is STILL
    APPLIED with the manual Reset pointer — never claim 'we put it back'."""
    sess = _FakeSession(SessionState.VERIFIED)
    sess.acceptance = _acceptance("revert")
    sess.auto_revert_outcome = {"result": "failed", "at": 1234.0}
    env = envelope.build_envelope(sess)
    assert env["screen"] == "result"
    assert "STILL APPLIED" in env["verdict_text"]
    assert "Reset" in env["verdict_text"]
    assert "put it back" not in env["verdict_text"]
    assert "removed the correction" not in env["verdict_text"]


def test_inflight_revert_copy_does_not_claim_completion():
    """BLOCKER pin: a revert verdict with no recorded outcome (rollback still
    running, or a pre-reset failure not yet stamped) must not claim the
    correction was removed; it names the manual Reset escape hatch."""
    sess = _FakeSession(SessionState.VERIFIED)
    sess.acceptance = _acceptance("revert")
    env = envelope.build_envelope(sess)
    assert env["screen"] == "result"
    assert "removing the correction" in env["verdict_text"]
    assert "Reset" in env["verdict_text"]
    assert "restored" not in env["verdict_text"]
    assert "put it back" not in env["verdict_text"]


def test_verdict_block_carries_auto_revert_outcome_without_mutation():
    sess = _FakeSession(SessionState.IDLE)
    sess.acceptance = _acceptance("revert")
    sess.auto_revert_outcome = {"result": "ok", "at": 1234.0}
    env = envelope.build_envelope(sess)
    assert env["verdict"]["auto_revert_outcome"] == {"result": "ok", "at": 1234.0}
    # The session's own acceptance record is relayed by copy, not mutated.
    assert "auto_revert_outcome" not in sess.acceptance


def test_pending_confirm_offers_verify_not_start():
    """SF pin: while a confirmation is pending, the single forward action is
    the confirmatory re-measure — NOT /start, which replaces the session and
    would destroy the pending verdict + concordance state."""
    sess = _FakeSession(SessionState.VERIFIED)
    sess.acceptance = _acceptance(
        "revert_pending_confirm", confirmed=False, verify_index=1,
    )
    env = envelope.build_envelope(sess)
    assert env["next_action"] == {
        "label": "Measure again to confirm",
        "endpoint": "/verify",
    }
    assert env["next_action"]["endpoint"] != "/start"


def test_pending_confirm_headline_folds_into_verdict_text():
    sess = _FakeSession(SessionState.VERIFIED)
    sess.acceptance = _acceptance(
        "revert_pending_confirm", confirmed=False, verify_index=1,
    )
    sess.verify_before_after = _verify_before_after()
    env = envelope.build_envelope(sess)
    assert env["verdict_text"].startswith("That measured worse. Measure once more")
    # The one-number headline folds in after the verdict lead.
    assert "±" in env["verdict_text"]


def test_accept_verdict_headline_and_default_action():
    sess = _FakeSession(SessionState.VERIFIED)
    sess.acceptance = _acceptance(
        "accept", confirmed=False, verify_index=1, overall_rms_delta_db=2.0,
    )
    env = envelope.build_envelope(sess)
    assert env["verdict_text"].startswith("Confirmed improved")
    assert env["next_action"] == {"label": "Measure again", "endpoint": "/start"}


def test_surface_verdict_headline():
    sess = _FakeSession(SessionState.VERIFIED)
    sess.acceptance = _acceptance(
        "surface", confirmed=False, verify_index=1, overall_rms_delta_db=0.1,
    )
    env = envelope.build_envelope(sess)
    assert "too small to be sure" in env["verdict_text"]
    assert env["next_action"] == {"label": "Measure again", "endpoint": "/start"}


# --------------------------------------------------------------------------
# Crossover-region distinction (revision plan §3.3 / P5). The envelope reads
# strategy.design_correction's `crossover_region` annotation off the session's
# design report and surfaces it on the REVIEW verdict_text + as a nudge.
# --------------------------------------------------------------------------


def _design_report_with_excluded_crossover_boost() -> dict[str, object]:
    return {
        "crossover_region": {
            "corner_hz": 80.0,
            "no_boost_band_hz": [63.5, 100.8],
            "excluded_boosts": [{"freq_hz": 82.0, "gain_db": 2.0}],
        },
    }


def test_review_verdict_text_distinguishes_crossover_from_room_mode():
    sess = _FakeSession(SessionState.READY)  # READY -> REVIEW screen
    sess.design_report = _design_report_with_excluded_crossover_boost()
    env = envelope.build_envelope(sess)
    # Base review copy PLUS the crossover-region distinction.
    assert env["verdict_text"].startswith(
        "Here's what your room is doing and the fix we'd apply."
    )
    assert "crossover, not a room mode" in env["verdict_text"]
    assert "80 Hz" in env["verdict_text"]


def test_crossover_region_nudge_surfaces_and_never_blocks():
    sess = _FakeSession(SessionState.READY)
    sess.design_report = _design_report_with_excluded_crossover_boost()
    env = envelope.build_envelope(sess)
    matching = [
        n for n in env["nudges"]
        if n["code"] == "crossover_region_dip_not_boosted"
    ]
    assert matching, "the crossover-region nudge must appear"
    # Never a block — the strongest measurement-flow nudge is warn/info.
    assert matching[0]["severity"] == "info"


def test_no_crossover_region_leaves_review_copy_and_nudges_plain():
    sess = _FakeSession(SessionState.READY)
    # No design report / no annotation (e.g. no bass management).
    env = envelope.build_envelope(sess)
    assert env["verdict_text"] == (
        "Here's what your room is doing and the fix we'd apply."
    )
    assert not any(
        n["code"] == "crossover_region_dip_not_boosted" for n in env["nudges"]
    )


def test_crossover_region_with_no_excluded_boosts_is_silent():
    sess = _FakeSession(SessionState.READY)
    # A corner is being read but nothing was excluded there -> no note/nudge.
    sess.design_report = {
        "crossover_region": {
            "corner_hz": 80.0,
            "no_boost_band_hz": [63.5, 100.8],
            "excluded_boosts": [],
        },
    }
    env = envelope.build_envelope(sess)
    assert env["verdict_text"] == (
        "Here's what your room is doing and the fix we'd apply."
    )
    assert not any(
        n["code"] == "crossover_region_dip_not_boosted" for n in env["nudges"]
    )
