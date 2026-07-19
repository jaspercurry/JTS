# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a endpoint binding: the v2 host through the REAL relay plan runner.

Integration tests drive :func:`jasper.web.correction_crossover_v2.build_v2_run_and_consume`
through the REAL :func:`jasper.capture_relay.session.run_capture_plan` against
the faithful in-memory relay backend + scripted phone driver from
``tests/test_capture_relay_plan.py`` — no network, no Worker, no page:

* the happy path across all three phases INCLUDING the deferred-VERIFY
  release on apply (§5.2's apply-complete auto-arm);
* ``CaptureTimeout`` → ``relay_timeout`` failure state + volume ABANDON
  (the §5.5 walked-away guarantee);
* phone session death (abort) → abandon + pre-apply evidence invalidation;
* the verify-only re-arm session (resume skips accepted phases AT THE RELAY:
  one entry, one capture, index 1 → VERIFY);
* ``status_payload``-shape threading: the schema-7 envelope advances through
  the phase screens end-to-end from the durable state the host persists.

Route registration + CSRF ordering ride the existing exact-surface contract
test (tests/test_web_correction_setup.py::test_known_post_routes_reach_csrf_guard,
which drives every ``_POST_ROUTES`` entry — now including the three
``/crossover/v2/*`` routes — to the CSRF guard); this file adds the
flow-selector refusals the dispatch relies on.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.crossover_flow import CROSSOVER_FLOW_ENV
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_CHECK,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
    CrossoverV2Conductor,
    V2FlowSeams,
    build_v2_session_spec,
    build_v2_verify_session_spec,
)
from jasper.capture_relay.client import RelayClient
from jasper.capture_relay.session import (
    CaptureAborted,
    CaptureTimeout,
    mint_session,
    register_session,
)
from jasper.web import correction_crossover_v2 as v2host

from tests.test_capture_relay_plan import FakePlanRelayBackend, PhonePlanDriver
from tests.test_crossover_v2_conductor import (
    CAPS,
    FC_HZ,
    SESSION_VOLUME_DB,
    _check_analysis,
    _measure_analysis,
    _preset,
    _roles,
    _verify_analysis,
)

_BINDING = "placement_abcdefghijklmnopqrstuv"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "v2")
    v2host.reset_session_measurement_pause_for_tests()
    yield
    v2host.set_state_path_for_tests(None)
    v2host.set_volume_plan_for_tests(None)
    v2host.reset_session_measurement_pause_for_tests()


def _bg_run_async(coro, *, timeout=None):
    """Mimic correction_setup._run_async for the host recovery helpers: run the
    coroutine to completion and return its result (each on a fresh loop — the
    session-volume drains are self-contained, no cross-loop context manager)."""
    return asyncio.run(coro)


class _FakeVolCam:
    """A CamillaController stand-in for the session-volume drains."""

    def __init__(self, vol: float) -> None:
        self.vol = vol

    async def set(self, db: float) -> bool:
        self.vol = float(db)
        return True

    async def get(self) -> float:
        return self.vol

    async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
        self.vol = float(db)
        return True

    async def get_volume_db(self, best_effort: bool = False) -> float:
        return self.vol


class VolumeRecorder:
    """Fake §5.5 volume lifecycle: records open/close/abandon order."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def hooks(self) -> v2host.V2VolumeHooks:
        async def _open():
            self.events.append("open")
            return "opened"

        async def _close():
            self.events.append("close")

        async def _abandon():
            self.events.append("abandon")

        return v2host.V2VolumeHooks(open=_open, close=_close, abandon=_abandon)


class V2PhoneDriver(PhonePlanDriver):
    """The scripted phone plus §5.2 deferral handling: on ``capture_deferred``
    it lets the test simulate the wizard Apply, then retries the SAME begin."""

    def __init__(self, *args, on_deferred=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.on_deferred = on_deferred
        self.deferrals_seen = 0

    def step(self) -> None:
        host = self.backend.sessions[self.session.session_id]["host_event"] or {}
        if (
            host.get("phase") == "capture_deferred"
            and (host.get("index"), host.get("attempt")) == self.begun
        ):
            self.deferrals_seen += 1
            if self.on_deferred is not None:
                self.on_deferred(self)
            index, attempt = self.begun
            self.begin(index, attempt)  # identical retry — budget unspent
            return
        super().step()


def _mint_v2_session(backend, spec, *, driver_cls=V2PhoneDriver, **driver_kwargs):
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    phone = driver_cls(backend, session, **driver_kwargs) if driver_cls else None

    def transport(method, url, headers, body):
        import urllib.parse

        if (
            phone is not None
            and method == "GET"
            and urllib.parse.urlsplit(url).path.endswith("/status")
        ):
            phone.step()
        return backend(method, url, headers, body)

    return RelayClient("https://relay.test", transport=transport), session, phone


def _wav(attempt: int) -> bytes:
    return b"RIFF" + bytes([attempt]) * 96


def _conductor(backend, session, phone, *, published, phases_seen=None,
               analyses=None) -> CrossoverV2Conductor:
    """A real conductor whose fake play uploads the phone blob (the acoustic
    seam) and whose fake analyze returns canned per-phase analyses."""
    analyses = analyses or {}

    def play(phase: str, program: Any) -> None:
        if phases_seen is not None:
            block = v2host.crossover_v2_status_block()
            phases_seen.append((phase, block["phase"] if block else None))
        index, attempt = phone.begun
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt),
            index=attempt - 1,
        )

    def analyze(program: Any, result: Any, priors: Any, geometry: Any) -> Any:
        factory = analyses.get(program.phase) or {
            "check": _check_analysis,
            "measure": _measure_analysis,
            "verify": _verify_analysis,
        }[program.phase]
        return factory(program)

    return CrossoverV2Conductor(
        session_id=session.session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=play,
            analyze=analyze,
            publish_check=lambda plan, ambient: published.append(("check", plan)),
            publish_candidate=lambda cand: published.append(("candidate", cand)),
            apply_complete=v2host._applied_gate,
        ),
        driver_spacing_m=0.15,
    )


def _run(runner, client, session):
    return asyncio.run(runner(client, session))


def _build_runner(conductor, volume, **kwargs):
    kwargs.setdefault("poll_interval_s", 0.01)  # fast polling for tests
    kwargs.setdefault("timeout_s", 20.0)
    # Keep the first-begin window equal to the (small) test timeout_s rather
    # than inheriting the production 300s V2_FIRST_BEGIN_TIMEOUT_S default, so a
    # first-begin timeout test still fires fast. Tests that specifically exercise
    # the 300s widening live in test_capture_relay_plan.py.
    kwargs.setdefault("first_begin_timeout_s", kwargs["timeout_s"])
    return v2host.build_v2_run_and_consume(
        conductor,
        volume=volume.hooks(),
        stop_event=threading.Event(),
        stop_lock=threading.Lock(),
        **kwargs,
    )


# --- happy path through the REAL plan runner -----------------------------------


def test_happy_path_three_phases_with_deferred_verify_release():
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)

    def on_deferred(_driver):
        # The wizard Apply lands while the phone is parked on "waiting for
        # apply": mark the durable state applied — the deferred VERIFY arms.
        state = v2host.load_v2_state()
        v2host.observe_apply_success(state["candidate"]["fingerprint"])

    client, session, phone = _mint_v2_session(
        backend, spec, on_deferred=on_deferred
    )
    published: list = []
    phases_seen: list = []
    conductor = _conductor(
        backend, session, phone, published=published, phases_seen=phases_seen
    )
    volume = VolumeRecorder()
    _run(_build_runner(conductor, volume), client, session)

    # All three phases accepted through ONE relay session.
    assert conductor.current_phase == PHASE_DONE
    assert conductor.verify_outcome == "pass"
    assert phone.deferrals_seen >= 1  # VERIFY was soft-held until apply
    assert [kind for kind, _ in published] == ["check", "candidate"]
    # The relay observed the deferral then the released capture.
    phases = backend.phases(session.session_id)
    assert "capture_deferred" in phases
    assert phases[-1] == "capture_set_complete"
    # Fix 2 (W6.4): the CHECK capture's host-event sequence includes the
    # sweep progress pair a real phone's `waitForSweepComplete`
    # (capture-page/js/main.js:1252-1327) polls for around its own play
    # wait -- "sweep_started" (cosmetic status text, line 1291-1293) then
    # "sweep_complete" (the ONLY phase that makes `waitForSweepComplete`
    # return, unblocking the phone to stop recording and upload -- line
    # 1294-1295, 1311). Before Fix 2 the v2 runner posted neither, so a real
    # phone would sit until that function's own timeout (line 1326) and
    # never complete a v2 capture (W6 run 5). CHECK is index 1, the first
    # phase authorized in a fresh session.
    check_events = backend.host_events[session.session_id]
    check_phases = [e.get("phase") for e in check_events]
    authorized_at = check_phases.index("capture_authorized")
    result_at = check_phases.index("capture_result")
    assert check_phases[authorized_at:result_at + 1] == [
        "capture_authorized", "sweep_started", "sweep_complete", "capture_result",
    ]
    # Exact shape `waitForSweepComplete` reads: `status.host_event.phase`.
    assert check_events[authorized_at + 1]["phase"] == "sweep_started"
    assert check_events[authorized_at + 2]["phase"] == "sweep_complete"
    # §5.5: exactly one volume open, closed (exact restore) on the done path.
    assert volume.events[0] == "open"
    assert volume.events[-1] == "close"
    assert "abandon" not in volume.events
    # The relay session was purged on completion.
    assert session.session_id not in backend.sessions

    # Durable state: done, applied, verify pass, candidate fingerprint kept.
    state = v2host.load_v2_state()
    assert set(state["accepted_phases"]) == {PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY}
    assert state["applied"] is True
    assert state["verify"] == {"outcome": "pass"}
    assert state["failure"] is None
    assert state["candidate"]["fingerprint"]

    # The schema-7 envelope advanced through the phase screens end-to-end,
    # rendered purely from the host-persisted status blocks (S1b).
    from jasper.active_speaker.crossover_envelope import build_crossover_envelope

    assert [phase for _p, phase in phases_seen] == ["check", "measure", "verify"]
    class _NoRecovery:
        needs_recovery = False

    v2host.set_volume_plan_for_tests(_NoRecovery())

    def _envelope_for(block_phase):
        return build_crossover_envelope({
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": {"phase": block_phase, "needs_recovery": False},
        })

    assert [_envelope_for(p)["screen"] for _s, p in phases_seen] == [
        "microphone_check", "measure", "verify",
    ]
    final_block = v2host.crossover_v2_status_block()
    assert final_block["phase"] == "done"
    assert _envelope_for(final_block["phase"])["screen"] == "done"


# --- relay-session death: timeout + abort (S1c) ---------------------------------


def _skip_purge_grace(monkeypatch):
    """No-op the relay-death / catch-all cleanup grace sleep so a test that only
    cares about the terminal outcome does not wait the real
    TERMINAL_FAILURE_PURGE_GRACE_S. The poll loop uses time.sleep on its own
    thread, so only the async cleanup grace is affected."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def test_capture_timeout_maps_to_relay_timeout_and_abandons_volume(monkeypatch):
    _skip_purge_grace(monkeypatch)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    # No phone driver: the session times out awaiting the first begin.
    client, session, _phone = _mint_v2_session(backend, spec, driver_cls=None)
    published: list = []

    class _NoPhone:
        begun = (1, 1)

    conductor = _conductor(backend, session, _NoPhone(), published=published)
    volume = VolumeRecorder()
    runner = _build_runner(conductor, volume, poll_interval_s=0.01, timeout_s=0.2)
    with pytest.raises(CaptureTimeout):
        _run(runner, client, session)

    assert volume.events == ["open", "abandon"]
    state = v2host.load_v2_state()
    assert state["failure"] == {"code": "relay_timeout"}
    # Pre-apply session death invalidates capture evidence (§5.6).
    assert state["accepted_phases"] == []
    # Blocker #3: the phone gets a session-level terminal before the purge 404,
    # so its deferred-retry loop stops instead of polling forever.
    assert backend.host_events[session.session_id][-1]["phase"] == (
        "capture_set_exhausted"
    )
    assert session.session_id not in backend.sessions
    # The envelope renders the session-restart template from this state.
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {"phase": "check", "failure": {"code": "relay_timeout"}},
    })
    assert env["screen"] == "session_restart"


def test_volume_open_failure_purges_the_fresh_relay_session():
    """An unconfirmed measurement volume must not leave the freshly-minted
    relay session lingering to worker TTL — it is purged before the raise."""
    from jasper.capture_relay.session import CaptureFailed

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, _phone = _mint_v2_session(backend, spec, driver_cls=None)

    class _FailingVolume:
        def hooks(self):
            async def _open():
                return "failed"

            async def _noop():
                pass

            return v2host.V2VolumeHooks(open=_open, close=_noop, abandon=_noop)

    class _NoPhone:
        begun = (1, 1)

    conductor = _conductor(backend, session, _NoPhone(), published=[])
    runner = _build_runner(conductor, _FailingVolume())
    with pytest.raises(CaptureFailed):
        _run(runner, client, session)
    assert session.session_id not in backend.sessions


def test_phone_abort_is_session_death_abandon_and_invalidation(monkeypatch):
    _skip_purge_grace(monkeypatch)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    phone.abort_after_results = 1  # abort right after CHECK's result
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    volume = VolumeRecorder()
    with pytest.raises(CaptureAborted):
        _run(_build_runner(conductor, volume), client, session)

    assert volume.events == ["open", "abandon"]
    state = v2host.load_v2_state()
    assert state["failure"] == {"code": "relay_timeout"}
    assert state["accepted_phases"] == []  # CHECK evidence died with the session


# --- verify-only re-arm (S1d: resume skips accepted phases at the relay) --------


def test_verify_rearm_session_runs_exactly_one_capture():
    backend = FakePlanRelayBackend()
    spec = build_v2_verify_session_spec(FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    published: list = []

    freqs = np.linspace(100.0, 20000.0, 64)
    base = _conductor(backend, session, phone, published=published)
    conductor = CrossoverV2Conductor(
        session_id=session.session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=base._seams,  # reuse the play/analyze/publish fakes
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        gain_plan_db={"woofer": -11.0, "tweeter": -13.0},
        index_phase_map={1: PHASE_VERIFY},
        measure_predicted_sum=(freqs, np.zeros(64)),
        measure_gate_window_ms=8.0,
    )
    assert conductor.pending_phases() == (PHASE_VERIFY,)
    volume = VolumeRecorder()
    _run(_build_runner(conductor, volume), client, session)

    assert conductor.current_phase == PHASE_DONE
    assert conductor.verify_outcome == "pass"
    # The relay hosted exactly ONE capture — accepted phases were skipped at
    # the relay layer via the 1-entry plan + index map, not re-measured.
    assert backend.phases(session.session_id) == [
        "capture_authorized", "sweep_started", "sweep_complete",
        "capture_result", "capture_set_complete",
    ]
    assert volume.events == ["open", "close"]


# --- endpoint gates (selector + recovery) ----------------------------------------


def test_prepare_refuses_under_legacy_flow(monkeypatch):
    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "legacy")
    with pytest.raises(v2host.CrossoverV2Refused):
        v2host.prepare_v2_session(
            {}, status={}, run_async=None, camilla_factory=None
        )
    with pytest.raises(v2host.CrossoverV2Refused):
        v2host.prepare_v2_verify(
            {}, status={}, run_async=None, camilla_factory=None
        )
    with pytest.raises(v2host.CrossoverV2Refused):
        v2host.handle_v2_apply({}, None, None)
    with pytest.raises(v2host.CrossoverV2Refused):
        v2host.handle_v2_restore(None, None)


def test_prepare_refuses_when_volume_needs_recovery():
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {}, status={}, run_async=None, camilla_factory=None
        )
    assert "recover" in str(excinfo.value)


def test_apply_endpoint_requires_current_candidate():
    with pytest.raises(v2host.CrossoverV2Refused):
        v2host.handle_v2_apply(
            {"expected_candidate_fingerprint": "fp"}, None, None
        )
    # A stale fingerprint against a persisted candidate is refused by name.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-current"},
    })
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_apply(
            {"expected_candidate_fingerprint": "fp-stale"}, None, None
        )
    assert "no longer current" in str(excinfo.value)


def test_observe_apply_success_arms_the_deferred_verify_gate():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
    })
    assert v2host._applied_gate() is False
    # A mismatched fingerprint must NOT arm verify.
    v2host.observe_apply_success("fp-other")
    assert v2host._applied_gate() is False
    v2host.observe_apply_success("fp-1")
    assert v2host._applied_gate() is True


def test_observe_apply_success_clears_a_stale_apply_blocked_nudge():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
        "apply_blocked": {"id": "baseline_profile_not_ready_to_apply", "message": "x"},
    })
    v2host.observe_apply_success("fp-1")
    assert v2host.load_v2_state()["apply_blocked"] is None


def test_observe_apply_success_stashes_the_pre_apply_profile():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
    })
    v2host.observe_apply_success("fp-1", pre_apply_profile={"status": "applied"})
    assert v2host.load_v2_state()["pre_apply_profile"] == {"status": "applied"}
    # The speaker's first-ever apply has nothing to stash.
    v2host.observe_apply_success("fp-1", pre_apply_profile=None)
    assert v2host.load_v2_state()["pre_apply_profile"] is None


def test_observe_restore_clears_applied_candidate_and_pre_apply_profile():
    """Mirrors observe_apply_success: the Undo path's durable-state clear
    (W6 run-8 Blocker Q) resets the flow to a clean unmeasured state rather
    than leaving a half-consistent review_apply pointing at the undone
    candidate."""
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "verify": {"outcome": "fail"},
        "applied": True,
        "apply_blocked": {"id": "x", "message": "x"},
        "pre_apply_profile": {"status": "applied"},
        "gain_plan_db": {"woofer": -3.0},
    })
    v2host.observe_restore()
    state = v2host.load_v2_state()
    assert state["applied"] is False
    assert state["candidate"] is None
    assert state["verify"] is None
    assert state["failure"] is None
    assert state["apply_blocked"] is None
    assert state["pre_apply_profile"] is None
    assert state["accepted_phases"] == []
    assert state["gain_plan_db"] is None
    assert v2host._applied_gate() is False


def test_restore_refuses_when_nothing_applied():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [],
        "applied": False,
    })
    with pytest.raises(v2host.CrossoverV2Refused, match="nothing is applied"):
        v2host.handle_v2_restore(None, None)


def test_restore_refuses_when_no_pre_apply_profile_is_stashed():
    """The speaker's first-ever apply has no predecessor to undo back to —
    a policy refusal, never a 500 (the legacy path's failure mode)."""
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": True,
        "pre_apply_profile": None,
    })
    with pytest.raises(v2host.CrossoverV2Refused, match="no previous crossover"):
        v2host.handle_v2_restore(None, None)


def test_status_block_surfaces_apply_blocked():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
        "apply_blocked": {"id": "measured_candidate_preset_mismatch", "message": "x"},
    })
    assert v2host.crossover_v2_status_block()["apply_blocked"] == {
        "id": "measured_candidate_preset_mismatch", "message": "x",
    }


def test_blocking_apply_issue_prefers_a_blocker_over_earlier_non_blocker_issues():
    payload = {
        "issues": [
            {"severity": "info", "code": "manual_crossover_preserved", "message": "kept"},
            {"severity": "blocker", "code": "the_real_reason", "message": "why"},
            {"severity": "blocker", "code": "generic_trailer", "message": "trailer"},
        ]
    }
    assert v2host._blocking_apply_issue(payload) == {
        "id": "the_real_reason", "message": "why",
    }


def test_blocking_apply_issue_none_when_no_issues():
    assert v2host._blocking_apply_issue({"issues": []}) is None
    assert v2host._blocking_apply_issue({}) is None


# --- production analyze binding (geometry + calibration) --------------------------


def _mono_wav_bytes(n: int = 4800) -> bytes:
    import io

    from scipy.io import wavfile

    buf = io.BytesIO()
    wavfile.write(buf, 48000, np.zeros(n, dtype=np.int16))
    return buf.getvalue()


class _FakeResult:
    def __init__(self, setup=None, device=None) -> None:
        self.wav = _mono_wav_bytes()
        self.setup = setup
        self.device = device


def test_production_analyze_threads_geometry_and_resolved_calibration(monkeypatch):
    """bind_production_analyze forwards the conductor's geometry AND the
    resolved calibration curve into analyze_program_capture."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None, priors=None):
        seen.update(calibration=calibration, geometry=geometry, rate=rate)
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    curve_sentinel = object()

    class _Record:
        curve = curve_sentinel
        calibration_id = "cal-123"

    resolved: list = []

    def resolver(setup, device):
        resolved.append((setup, device))
        return _Record()

    meta: dict[str, Any] = {}
    analyze = v2host.bind_production_analyze(resolve_calibration=resolver, meta=meta)
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    geometry = MeasurementGeometry(driver_spacing_m=0.15, mic_distance_m=1.0)
    result = _FakeResult(setup={"calibration": {"mode": "serial"}}, device={"label": "UMIK-2"})
    out = analyze(program, result, MeasurementPriors(crossover_fc_hz=FC_HZ), geometry)

    assert out == "analysis"
    # The resolver was invoked with the capture's setup/device.
    assert resolved == [(result.setup, result.device)]
    # The resolved curve AND the conductor geometry reached the analysis.
    assert seen["calibration"] is curve_sentinel
    assert seen["geometry"] is geometry
    assert seen["geometry"].driver_spacing_m == pytest.approx(0.15)
    assert seen["rate"] == 48000
    # The evidence annotation records the applied calibration.
    assert meta["calibration"]["verify"] == {
        "applied": True, "calibration_id": "cal-123",
    }


def test_production_analyze_annotates_uncalibrated_when_none_resolves(monkeypatch, caplog):
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None, priors=None):
        seen.update(calibration=calibration)
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)
    meta: dict[str, Any] = {}
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta=meta
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        analyze(
            program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
        )
    # NOT silent: analysis ran uncalibrated, annotated as a stored fact + WARN.
    assert seen["calibration"] is None
    assert meta["calibration"]["verify"] == {"applied": False, "calibration_id": None}
    assert "crossover_v2_uncalibrated_capture" in caplog.text


def test_production_analyze_default_resolver_is_the_shared_relay_machinery():
    """The default resolver IS correction_setup._relay_calibration_from_setup
    (the one point the room + legacy crossover flows resolve phone calibration
    choices) — a no-choice setup resolves to None."""
    assert v2host.resolve_relay_calibration(None, None) is None
    assert v2host.resolve_relay_calibration({"calibration": {"mode": "none"}}, None) is None


# --- status block (S1b) -----------------------------------------------------------


def test_status_block_none_under_legacy(monkeypatch):
    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "legacy")
    assert v2host.crossover_v2_status_block() is None


def test_status_block_reports_needs_recovery_and_phase():
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "applied": False,
    })
    block = v2host.crossover_v2_status_block()
    assert block["needs_recovery"] is True
    assert block["phase"] == PHASE_MEASURE
    # And the review_apply projection: measure accepted, not yet applied.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
    })
    assert v2host.crossover_v2_status_block()["phase"] == "review_apply"


# --- W6.1 Finding B: no silent playback failures --------------------------------


def _refusing_seams(base_conductor):
    """Replace a conductor's play seam with one that refuses admission at play
    time (the JTS3 over-cap ProgramPlaybackRefused), keeping the other seams."""
    from jasper.active_speaker.program_admission import (
        ProgramAdmission,
        ProgramAdmissionRefusal,
    )
    from jasper.active_speaker.program_playback import ProgramPlaybackRefused

    def refusing_play(phase: str, program: Any) -> None:
        adm = ProgramAdmission(
            program_id=program.program_id,
            phase=phase,
            session_volume_db=SESSION_VOLUME_DB,
            segments=(),
            channels=(),
            refusals=(ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP,),
        )
        raise ProgramPlaybackRefused(adm)

    return V2FlowSeams(
        play=refusing_play,
        analyze=base_conductor._seams.analyze,
        publish_check=base_conductor._seams.publish_check,
        publish_candidate=base_conductor._seams.publish_candidate,
        apply_complete=base_conductor._seams.apply_complete,
    )


def test_playback_refusal_persists_failure_abandons_volume_and_tells_phone():
    """A play-seam refusal (ProgramPlaybackRefused) must NOT escape silently:
    persist a distinct program_unplayable failure, abandon the volume, purge the
    relay session, AND post a terminal capture_result so the phone stops waiting
    (hardware run 2 froze at capture_authorized forever)."""
    from jasper.active_speaker.program_playback import ProgramPlaybackError

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    conductor._seams = _refusing_seams(conductor)
    volume = VolumeRecorder()
    with pytest.raises(ProgramPlaybackError):
        _run(_build_runner(conductor, volume), client, session)

    # Volume was drained (the §5.5 walked-away guarantee), not left active.
    assert volume.events == ["open", "abandon"]
    # A DISTINCT failure persisted — not relay_timeout.
    state = v2host.load_v2_state()
    assert state["failure"] == {"code": "program_unplayable"}
    # The relay session was purged (no leak to worker TTL).
    assert session.session_id not in backend.sessions
    # The phone got a terminal capture_result carrying the §5.10 hard-stop so it
    # renders the failure instead of recording into silence forever.
    events = backend.host_events[session.session_id]
    assert events[-1]["phase"] == "capture_result"
    assert events[-1]["accepted"] is False
    assert events[-1]["code"] == "program_unplayable"
    assert events[-1]["template"] == "hard_stop"
    assert events[-1]["index"] == 1 and events[-1]["attempt"] == 1


def test_volume_open_raise_purges_the_fresh_relay_session():
    """A SessionVolumePlanError from volume.open() (run 2's retry hit this when a
    prior session left the volume open) must purge the freshly-minted relay
    session before surfacing — no leak to worker TTL."""
    from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
    from jasper.capture_relay.session import CaptureFailed

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, _phone = _mint_v2_session(backend, spec, driver_cls=None)

    class _RaisingVolume:
        def hooks(self):
            async def _open():
                raise SessionVolumePlanError("a prior session volume is unresolved")

            async def _noop():
                pass

            return v2host.V2VolumeHooks(open=_open, close=_noop, abandon=_noop)

    class _NoPhone:
        begun = (1, 1)

    conductor = _conductor(backend, session, _NoPhone(), published=[])
    with pytest.raises(CaptureFailed):
        _run(_build_runner(conductor, _RaisingVolume()), client, session)
    assert session.session_id not in backend.sessions


# --- W6.1 Finding C: session-scoped measurement pause ---------------------------


class _FakeWindow:
    """A recording stand-in for coordinator.measurement_window()."""

    def __init__(self, log: list) -> None:
        self.log = log

    async def __aenter__(self):
        self.log.append("enter")
        return None

    async def __aexit__(self, *exc):
        self.log.append("exit")
        return False


def _patch_measurement_window(monkeypatch, log: list) -> None:
    from jasper.correction import coordinator

    monkeypatch.setattr(
        coordinator, "measurement_window", lambda **kw: _FakeWindow(log)
    )


def test_session_measurement_pause_is_idempotent(monkeypatch):
    """Acquire enters the window exactly once (a second acquire is a no-op, so a
    per-play cannot open a second exclusive window); release exits exactly once
    and a double-release is safe (no double-exit)."""
    log: list = []
    _patch_measurement_window(monkeypatch, log)

    async def scenario():
        assert not v2host.session_measurement_pause_held()
        await v2host.acquire_session_measurement_pause()
        assert v2host.session_measurement_pause_held()
        await v2host.acquire_session_measurement_pause()  # idempotent
        assert v2host.session_measurement_pause_held()
        await v2host.release_session_measurement_pause()
        assert not v2host.session_measurement_pause_held()
        await v2host.release_session_measurement_pause()  # idempotent

    asyncio.run(scenario())
    assert log == ["enter", "exit"]  # exactly one enter, one exit


def test_volume_hooks_hold_pause_from_open_to_every_drain(monkeypatch):
    """The pause is held from volume open through the drain, for BOTH the close
    and abandon paths; a per-play in between (which nest-SKIPS while held) does
    not release it. The failed-open path releases it so voice never strands."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    class _Ctx:
        session_volume_db = -20.0

    for drain in ("close", "abandon"):
        log: list = []
        _patch_measurement_window(monkeypatch, log)
        v2host.reset_session_measurement_pause_for_tests()
        v2host.set_volume_plan_for_tests(SessionVolumePlan())
        cam = _FakeVolCam(-15.0)

        async def scenario():
            hooks = v2host._volume_hooks(lambda: cam, _Ctx())
            opened = await hooks.open()
            assert opened is SessionVolumeOpenResult.OPENED
            assert cam.vol == -20.0
            # Held for the whole session; a per-play sees this and skips.
            assert v2host.session_measurement_pause_held()
            await getattr(hooks, drain)()
            assert not v2host.session_measurement_pause_held()
            assert cam.vol == -15.0  # restored

        asyncio.run(scenario())
        assert log == ["enter", "exit"], drain
        v2host.set_volume_plan_for_tests(None)


def test_volume_hooks_release_pause_when_open_does_not_confirm(monkeypatch):
    """If plan.open() drains itself (does not return OPENED), the pause is
    released — a failed open must never leave voice paused with no session."""
    log: list = []
    _patch_measurement_window(monkeypatch, log)
    v2host.reset_session_measurement_pause_for_tests()

    class _DrainedPlan:
        async def open(self, vol, set_v, get_v):
            return "failed"

    v2host.set_volume_plan_for_tests(_DrainedPlan())

    class _Ctx:
        session_volume_db = -20.0

    async def scenario():
        hooks = v2host._volume_hooks(lambda: _FakeVolCam(-15.0), _Ctx())
        result = await hooks.open()
        assert result == "failed"
        assert not v2host.session_measurement_pause_held()

    asyncio.run(scenario())
    assert log == ["enter", "exit"]


# --- W6.1 Finding E: recovery paths actually recover -----------------------------


def test_reconcile_drains_residual_owned_active_before_new_session():
    """E1: a residual owned-active plan (a prior failed session's leftover) is
    drained before a fresh session, so plan.open() starts clean instead of
    raising SessionVolumePlanError into the silent 200→adapter_failed loop."""
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    plan = SessionVolumePlan()
    cam = _FakeVolCam(-15.0)
    asyncio.run(plan.open(-20.0, cam.set, cam.get))
    assert plan.measurement_volume_db == -20.0
    assert not plan.needs_recovery  # owned-active this process, within ceiling
    v2host.set_volume_plan_for_tests(plan)

    v2host.reconcile_session_volume_for_new_session(_bg_run_async, lambda: cam)

    assert plan.measurement_volume_db is None  # residual drained
    assert not plan.needs_recovery
    assert cam.vol == -15.0  # restored to household


def test_enforce_ceiling_drains_a_stale_active_and_is_cheap_otherwise():
    """E3: enforce_ceiling (previously zero callers) force-drains a session that
    outlived the wall-clock ceiling, and is a no-op on a healthy session."""
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    clock = [1000.0]
    plan = SessionVolumePlan(wall_clock_ceiling_s=10.0, clock=lambda: clock[0])
    cam = _FakeVolCam(-15.0)
    asyncio.run(plan.open(-20.0, cam.set, cam.get))
    assert cam.vol == -20.0
    v2host.set_volume_plan_for_tests(plan)

    # Within the ceiling: cheap no-op, nothing drained.
    assert v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is False
    assert plan.measurement_volume_db == -20.0

    # Past the ceiling: force-drained back to the household volume.
    clock[0] = 2000.0
    assert v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is True
    assert plan.measurement_volume_db is None
    assert cam.vol == -15.0


def test_v2_volume_recovery_active_tracks_needs_recovery(monkeypatch):
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    assert v2host.v2_volume_recovery_active() is True

    class _Clean:
        needs_recovery = False

    v2host.set_volume_plan_for_tests(_Clean())
    assert v2host.v2_volume_recovery_active() is False

    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "legacy")
    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    assert v2host.v2_volume_recovery_active() is False  # legacy flow: never v2


def test_recover_session_volume_routes_to_the_plan():
    """E2 host seam: recover_session_volume drains via the v2 plan's
    recover_unresolved (not the legacy lease) and reports the outcome."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    drained: list = []

    class _Plan:
        needs_recovery = True

        async def recover_unresolved(self, set_v, get_v):
            await set_v(-15.0)
            await get_v()
            drained.append(True)
            return SessionVolumeRestoreResult.EXACT_RESTORED

    v2host.set_volume_plan_for_tests(_Plan())
    cam = _FakeVolCam(-20.0)
    succeeded, recovery = v2host.recover_session_volume(_bg_run_async, lambda: cam)
    assert succeeded is True
    assert recovery == "exact_restored"
    assert drained == [True]
    assert cam.vol == -15.0


# --- W6.1 gate: catch-all cleanup arm (B blocker rework) -------------------------
#
# The seams raise open-endedly — the reviewer PROVED by probe that
# CamillaUnavailable (a bare Exception, from a DSP wedge in the graph seams)
# escaped the previously-enumerated arms: volume left active, session leaked,
# phone frozen at capture_authorized, and (post-Finding C) the measurement
# pause leaked too. These drive the reviewer's exact probe + an analyze-seam
# raise through the REAL plan runner with REAL volume hooks, asserting the
# full cleanup contract: abandon ran, pause released, session purged, terminal
# host event landed, exception re-raised.


def _real_hooks_scaffold(monkeypatch):
    """Real _volume_hooks over a real SessionVolumePlan + fake DSP + fake
    measurement window; returns (hooks, plan, cam, window_log)."""
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    log: list = []
    _patch_measurement_window(monkeypatch, log)
    plan = SessionVolumePlan()
    v2host.set_volume_plan_for_tests(plan)
    cam = _FakeVolCam(-15.0)

    class _Ctx:
        session_volume_db = -20.0

    hooks = v2host._volume_hooks(lambda: cam, _Ctx())
    return hooks, plan, cam, log


def _assert_full_cleanup(plan, cam, log, backend, session, *, code):
    # abandon ran: measurement volume drained, household volume restored.
    assert plan.measurement_volume_db is None
    assert cam.vol == -15.0
    # the session measurement pause was released (exactly one enter/exit).
    assert not v2host.session_measurement_pause_held()
    assert log == ["enter", "exit"]
    # the relay session was purged (no leak to worker TTL).
    assert session.session_id not in backend.sessions
    # the phone got a terminal capture_result naming the failure.
    events = backend.host_events[session.session_id]
    assert events[-1]["phase"] == "capture_result"
    assert events[-1]["accepted"] is False
    assert events[-1]["code"] == code
    assert events[-1]["index"] == 1 and events[-1]["attempt"] == 1
    # and the same failure persisted for the wizard envelope.
    state = v2host.load_v2_state()
    assert state["failure"] == {"code": code}


def test_camilla_unavailable_from_play_seam_full_cleanup(monkeypatch):
    """The reviewer's exact probe: CamillaUnavailable (bare Exception) from the
    play seam — must hit the catch-all: internal_error + full cleanup + re-raise."""
    from jasper.camilla import CamillaUnavailable

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    conductor = _conductor(backend, session, phone, published=[])

    def wedged_play(phase: str, program: Any) -> None:
        raise CamillaUnavailable("websocket to CamillaDSP is down")

    conductor._seams = V2FlowSeams(
        play=wedged_play,
        analyze=conductor._seams.analyze,
        publish_check=conductor._seams.publish_check,
        publish_candidate=conductor._seams.publish_candidate,
        apply_complete=conductor._seams.apply_complete,
    )
    hooks, plan, cam, log = _real_hooks_scaffold(monkeypatch)
    runner = v2host.build_v2_run_and_consume(
        conductor, volume=hooks, stop_event=threading.Event(),
        stop_lock=threading.Lock(), poll_interval_s=0.01, timeout_s=20.0,
    )
    with pytest.raises(CamillaUnavailable):  # re-raised, not swallowed
        _run(runner, client, session)
    _assert_full_cleanup(plan, cam, log, backend, session, code="internal_error")


def test_analyze_seam_raise_full_cleanup(monkeypatch):
    """A ValueError from the analyze seam (consume path) — same catch-all
    contract: internal_error + full cleanup + re-raise."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)

    def broken_analyze(program: Any) -> Any:
        raise ValueError("analysis kernel fault")

    conductor = _conductor(
        backend, session, phone, published=[],
        analyses={"check": broken_analyze},
    )
    hooks, plan, cam, log = _real_hooks_scaffold(monkeypatch)
    runner = v2host.build_v2_run_and_consume(
        conductor, volume=hooks, stop_event=threading.Event(),
        stop_lock=threading.Lock(), poll_interval_s=0.01, timeout_s=20.0,
    )
    with pytest.raises(ValueError):  # re-raised, not swallowed
        _run(runner, client, session)
    _assert_full_cleanup(plan, cam, log, backend, session, code="internal_error")


def test_playback_refusal_keeps_its_distinct_code_through_the_catch_all(monkeypatch):
    """The program-side classes keep program_unplayable through the catch-all's
    dispatch — the distinct code is not collapsed into internal_error."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    conductor = _conductor(backend, session, phone, published=[])
    conductor._seams = _refusing_seams(conductor)
    hooks, plan, cam, log = _real_hooks_scaffold(monkeypatch)
    from jasper.active_speaker.program_playback import ProgramPlaybackError

    runner = v2host.build_v2_run_and_consume(
        conductor, volume=hooks, stop_event=threading.Event(),
        stop_lock=threading.Lock(), poll_interval_s=0.01, timeout_s=20.0,
    )
    with pytest.raises(ProgramPlaybackError):
        _run(runner, client, session)
    _assert_full_cleanup(plan, cam, log, backend, session, code="program_unplayable")


# --- W6.1 gate should-fix: gate-lease abort under the held window ----------------


def test_gate_abort_mid_play_cancels_the_play_and_names_the_error(monkeypatch):
    """Renew failure mid-play: the coordinator's abort cancels the REGISTERED
    play task (not the session task) and the cancellation surfaces as a named
    MeasurementWindowError so the cleanup arm persists it honestly."""
    from jasper.correction.coordinator import MeasurementWindowError

    log: list = []
    _patch_measurement_window(monkeypatch, log)

    async def scenario():
        await v2host.acquire_session_measurement_pause()
        target = v2host._session_abort_target
        assert target is not None
        started = asyncio.Event()

        async def play_body():
            started.set()
            await asyncio.sleep(30)

        play = asyncio.create_task(v2host._play_under_session_pause(play_body))
        await started.wait()
        # What the coordinator's refresh task does on a 40 s renew failure.
        target.abort(None)
        with pytest.raises(MeasurementWindowError) as excinfo:
            await play
        assert "isolation was lost" in str(excinfo.value)
        assert target.failed is True

    asyncio.run(scenario())


def test_gate_abort_between_plays_fails_the_next_play_by_name(monkeypatch):
    """Renew failure between plays: the latched failed flag refuses the NEXT
    play with a named error before any audio — never a silent nest-skip into an
    unconfirmed music-isolation gate."""
    from jasper.correction.coordinator import MeasurementWindowError

    log: list = []
    _patch_measurement_window(monkeypatch, log)
    body_ran: list = []

    async def scenario():
        await v2host.acquire_session_measurement_pause()
        target = v2host._session_abort_target
        target.abort(None)  # no play registered: latch only, no crash

        async def play_body():
            body_ran.append(True)

        with pytest.raises(MeasurementWindowError) as excinfo:
            await v2host._play_under_session_pause(play_body)
        assert "isolation was lost" in str(excinfo.value)

    asyncio.run(scenario())
    assert body_ran == []  # refused before any audio


# --- W6 hardware run 3, finding F: bind_production_play's config_dir SSOT -------


def _probe_bind_production_play_config_dir(monkeypatch, tmp_path) -> str:
    """Drive ``bind_production_play`` far enough to observe the ``config_dir``
    it threads into ``bind_program_playback_seams`` — short-circuiting via a
    sentinel exception BEFORE any real DSP graph emission/playback, since this
    probe cares only about the config_dir plumbing (graph emission and
    playback have their own coverage elsewhere)."""
    from jasper.active_speaker import camilla_yaml as camilla_yaml_mod
    from jasper.active_speaker import crossover_v2_flow as flow_mod
    import jasper.audio_measurement.program as program_mod

    captured: dict[str, Any] = {}

    class _ShortCircuit(Exception):
        pass

    def fake_bind_program_playback_seams(cam, **kwargs):
        captured["config_dir"] = kwargs["config_dir"]
        raise _ShortCircuit("captured config_dir — stop before the DSP plumbing")

    monkeypatch.setattr(
        flow_mod, "bind_program_playback_seams", fake_bind_program_playback_seams
    )
    _patch_measurement_window(monkeypatch, [])
    monkeypatch.setattr(
        camilla_yaml_mod,
        "emit_active_speaker_program_config",
        lambda *a, **kw: "placeholder-graph-yaml",
    )
    monkeypatch.setattr(program_mod, "write_program_wav", lambda path, program: None)

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        relay_session_id="cap_config_dir_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
    )
    with pytest.raises(_ShortCircuit):
        play(PHASE_CHECK, object())
    return captured["config_dir"]


def test_bind_production_play_default_config_dir_matches_ssot(monkeypatch, tmp_path):
    """W6 hardware run 3 finding F: bind_production_play's config_dir default
    must resolve to the SAME canonical constant every sibling DSP writer
    (commissioning apply/verify, web_commissioning, correction_setup) locks
    against — jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR — not
    the stale "/etc/camilladsp" literal this binding shipped with. An SSOT
    pin: if either side's default drifts away from the other, this fails."""
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR

    resolved = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)
    assert resolved == str(DEFAULT_CAMILLA_CONFIG_DIR)


def test_bind_production_play_default_config_dir_lock_lands_under_var_lib_camilladsp(
    monkeypatch, tmp_path
):
    """The resolved config_dir's DSP writer lock must land under
    /var/lib/camilladsp — the ONLY tree jasper-correction-web's
    ProtectSystem=full leaves writable (ReadWritePaths=/var/lib/jasper
    /var/lib/camilladsp; see deploy/jasper-correction-web.service). A lock
    under /etc/camilladsp is exactly the EROFS W6 run 3 hit 70 ms into the
    first play."""
    from jasper.dsp_apply import dsp_apply_lock_path

    resolved = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)
    assert str(dsp_apply_lock_path(resolved)).startswith("/var/lib/camilladsp")


# --- W6 hardware run 3, finding G: local seam OSError vs. relay transport death -


def test_local_seam_oserror_from_play_maps_to_internal_error(monkeypatch):
    """W6 run 3: the DSP writer lock's os.open raising EROFS (finding F) is a
    bare OSError from the LOCAL play seam — it must not be misclassified as
    relay_timeout. It has to hit the same catch-all internal_error arm as any
    other local seam failure (CamillaUnavailable, a ValueError from analyze,
    etc.), with full cleanup and a terminal host event so the phone stops
    waiting."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    conductor = _conductor(backend, session, phone, published=[])

    def erofs_play(phase: str, program: Any) -> None:
        raise OSError(30, "Read-only file system")

    conductor._seams = V2FlowSeams(
        play=erofs_play,
        analyze=conductor._seams.analyze,
        publish_check=conductor._seams.publish_check,
        publish_candidate=conductor._seams.publish_candidate,
        apply_complete=conductor._seams.apply_complete,
    )
    hooks, plan, cam, log = _real_hooks_scaffold(monkeypatch)
    runner = v2host.build_v2_run_and_consume(
        conductor, volume=hooks, stop_event=threading.Event(),
        stop_lock=threading.Lock(), poll_interval_s=0.01, timeout_s=20.0,
    )
    with pytest.raises(v2host.CrossoverV2LocalSeamError):
        _run(runner, client, session)
    _assert_full_cleanup(plan, cam, log, backend, session, code="internal_error")


def test_transport_oserror_still_classifies_as_relay_timeout(monkeypatch):
    """The flip side of finding G: a genuine relay-TRANSPORT OSError (the
    poll loop's client.status reaching an unreachable host — never wrapped by
    on_armed/consume) must still hit the relay-death arm and classify as
    relay_timeout, exactly as before. The fix narrows the misclassification;
    it must not also swallow real relay deaths into internal_error."""
    _skip_purge_grace(monkeypatch)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, _phone = _mint_v2_session(backend, spec, driver_cls=None)

    def raising_status(session_id, pull_token):
        raise OSError("Connection refused")

    monkeypatch.setattr(client, "status", raising_status)

    class _NoPhone:
        begun = (1, 1)

    conductor = _conductor(backend, session, _NoPhone(), published=[])
    volume = VolumeRecorder()
    runner = _build_runner(conductor, volume)
    with pytest.raises(OSError):
        _run(runner, client, session)

    assert volume.events == ["open", "abandon"]
    state = v2host.load_v2_state()
    assert state["failure"] == {"code": "relay_timeout"}


# --- W6 hardware run 3, finding H: terminal-failure purge races the phone -------


def test_terminal_failure_purge_waits_for_grace_but_volume_restore_is_immediate(
    monkeypatch,
):
    """W6 run 3: the driver's very next poll of the relay session's own status
    endpoint got a bare 404 ~1 s after a terminal capture_result was posted —
    the relay-death cleanup purged the session immediately, racing the
    phone's next poll. The catch-all cleanup arm must wait
    TERMINAL_FAILURE_PURGE_GRACE_S before purging (giving the just-posted
    event a window to actually reach the phone), while the household's volume
    restore stays immediate — no delay on the audible/safety-relevant side."""
    from jasper.capture_relay import session as session_mod

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    conductor = _conductor(backend, session, phone, published=[])

    def erofs_play(phase: str, program: Any) -> None:
        raise OSError(30, "Read-only file system")

    conductor._seams = V2FlowSeams(
        play=erofs_play,
        analyze=conductor._seams.analyze,
        publish_check=conductor._seams.publish_check,
        publish_candidate=conductor._seams.publish_candidate,
        apply_complete=conductor._seams.apply_complete,
    )
    hooks, plan, cam, log = _real_hooks_scaffold(monkeypatch)

    order: list[str] = []
    real_purge = session_mod.purge

    def recording_purge(client_arg, pi_session_arg):
        order.append("purge")
        return real_purge(client_arg, pi_session_arg)

    monkeypatch.setattr(session_mod, "purge", recording_purge)

    async def fake_sleep(seconds):
        order.append(f"sleep:{seconds}")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    runner = v2host.build_v2_run_and_consume(
        conductor, volume=hooks, stop_event=threading.Event(),
        stop_lock=threading.Lock(), poll_interval_s=0.01, timeout_s=20.0,
    )
    with pytest.raises(v2host.CrossoverV2LocalSeamError):
        _run(runner, client, session)

    # Volume restored immediately — no delay on the household-audible side.
    assert plan.measurement_volume_db is None
    assert cam.vol == -15.0
    # The terminal host event was posted before either.
    events = backend.host_events[session.session_id]
    assert events[-1]["phase"] == "capture_result"
    assert events[-1]["code"] == "internal_error"
    # The grace ran, then the purge — in exactly that order, exactly once each.
    assert order == [f"sleep:{v2host.TERMINAL_FAILURE_PURGE_GRACE_S}", "purge"]
    assert session.session_id not in backend.sessions


def test_watchdog_collapse_posts_session_over_then_grace_then_purge(monkeypatch):
    """W6.10 blocker #3: a watchdog collapse (CaptureTimeout) — the review-hold
    inactivity death — must reach the phone. The relay-death arm posts a
    session-level terminal (capture_set_exhausted), waits the purge grace so it
    reaches the phone's next poll, THEN purges — the same terminal-then-grace-
    then-purge the catch-all arm already uses. Before this the arm posted
    nothing and purged immediately, so the phone's deferred-retry loop saw no
    terminal at all (round 2: 'the phone saw nothing')."""
    from jasper.capture_relay import session as session_mod

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, _phone = _mint_v2_session(backend, spec, driver_cls=None)

    class _NoPhone:
        begun = (1, 1)

    conductor = _conductor(backend, session, _NoPhone(), published=[])

    order: list[str] = []
    real_purge = session_mod.purge

    def recording_purge(client_arg, pi_session_arg):
        order.append("purge")
        return real_purge(client_arg, pi_session_arg)

    monkeypatch.setattr(session_mod, "purge", recording_purge)

    async def fake_sleep(seconds):
        order.append(f"sleep:{seconds}")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    runner = _build_runner(
        conductor, VolumeRecorder(), poll_interval_s=0.01, timeout_s=0.2
    )
    with pytest.raises(CaptureTimeout):
        _run(runner, client, session)

    events = backend.host_events[session.session_id]
    assert events[-1]["phase"] == "capture_set_exhausted"
    assert order == [f"sleep:{v2host.TERMINAL_FAILURE_PURGE_GRACE_S}", "purge"]


# --- W6 run-6 Blocker M + Finding N: apply's real fingerprint-vocabulary seam ---
#
# Every prior test in this file that reaches "applied" fakes the apply gate
# directly (``observe_apply_success`` called from the phone-driver's
# ``on_deferred`` hook) rather than driving ``handle_v2_apply`` through the
# REAL ``apply_baseline_profile`` guard end to end. That gap is exactly how
# W6 hardware run 6 shipped an apply path that could never succeed: the
# guard compares against ``baseline_candidate_fingerprint`` (the composed
# baseline candidate's own identity), not the MEASURED candidate's
# fingerprint this endpoint reviews with the household — a vocabulary
# mismatch the endpoint tests never caught because they never crossed the
# seam. These tests seed the real topology/design-draft/crossover-preview
# files ``handle_v2_apply``'s real loaders read and drive the actual seam.


class _FakeApplyCam:
    """A CamillaController stand-in for handle_v2_apply's ``camilla_factory``."""

    def __init__(self) -> None:
        self.path: str | None = None

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False,
    ) -> bool:
        self.path = path
        return True

    async def get_config_file_path(self, *, best_effort: bool = False) -> str | None:
        return self.path


def _seed_baseline_apply_environment(monkeypatch, tmp_path):
    """Seed the real topology/design-draft/crossover-preview/measurements
    files ``handle_v2_apply``'s real loaders read (env-var overrides — the
    same pattern as ``tests/test_active_speaker_setup_status.py``), plus the
    baseline-profile/config and DSP-apply state paths. Returns
    ``(topology, preset)`` so a caller can build a ``MeasuredCrossoverCandidate``
    against the exact preset the seam will recompile from the same files."""
    from jasper.active_speaker import compile_preset_from_crossover_preview
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.output_topology import save_output_topology

    from tests.test_active_speaker_baseline_profile import _draft, _dual_apple_topology

    topology = _dual_apple_topology()
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(topology, topology_path)

    draft = _draft(topology)
    draft_path = tmp_path / "design_draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(draft_path))

    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preview_path = tmp_path / "crossover_preview.json"
    preview_path.write_text(json.dumps(preview), encoding="utf-8")
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE", str(preview_path)
    )

    # No driver-test measurements recorded — the run-6 shape: a household
    # applies purely from the reviewed measured candidate.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "measurements_missing.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        str(tmp_path / "baseline_profile.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )

    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    return topology, preset


def _run6_measured_candidate(preset):
    """A candidate shaped like W6 run 6's evidence (candidate_evidence.json):
    woofer delay 404.777 µs (quantizes to 0.4048 ms), tweeter -13.0327 dB and
    inverted."""
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverAlignment,
        MeasuredCrossoverCandidate,
    )

    return MeasuredCrossoverCandidate(
        program_id=(
            "9579a1bb9e2a3d1d8988670628bdbf6f348de3400e76baa63139abbed5ae0207"
        ),
        analysis={"epsilon_ppm": 29.924, "predicted_ripple_db": 29.6952},
        source_preset=preset,
        role_attenuations_db={"tweeter": -13.0327, "woofer": 0.0},
        alignment=MeasuredCrossoverAlignment(
            delay_us=404.7770086705022, delay_role="woofer", polarity="invert",
        ),
    )


def test_apply_translates_measured_fingerprint_to_baseline_fingerprint(
    monkeypatch, tmp_path,
):
    """Blocker M, positive: drives handle_v2_apply through the REAL
    apply_baseline_profile guard end to end (no faked apply gate) with a
    run-6-shaped measured candidate, and asserts the guard passes and the
    emitted config carries the measured delay + inversion."""
    from jasper.active_speaker.baseline_profile import baseline_candidate_fingerprint

    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    payload = v2host.handle_v2_apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    corrections = payload["profile"]["corrections"]
    assert corrections["woofer"]["delay_ms"] == pytest.approx(0.4048, abs=1e-4)
    assert corrections["woofer"]["inverted"] is False
    assert corrections["tweeter"]["delay_ms"] == 0.0
    assert corrections["tweeter"]["gain_db"] == pytest.approx(-13.0327, abs=1e-4)
    assert corrections["tweeter"]["inverted"] is True
    config_text = (tmp_path / "active_speaker_baseline.yml").read_text(
        encoding="utf-8"
    )
    assert "delay: 0.4048" in config_text

    # The fingerprint that actually reached the seam is the COMPOSED baseline
    # candidate's own identity, never the measured candidate's fingerprint —
    # confirming the vocabulary translation happened rather than the two
    # values accidentally colliding.
    assert payload["profile"]["candidate_fingerprint"] != candidate.fingerprint
    assert payload["profile"][
        "candidate_fingerprint"
    ] == baseline_candidate_fingerprint(payload["profile"])

    # Success arms the deferred VERIFY gate and clears any stale apply-blocked
    # nudge (Finding N).
    assert v2host._applied_gate() is True
    saved_state = v2host.load_v2_state()
    assert saved_state["apply_blocked"] is None


def test_apply_refuses_when_composition_is_no_longer_bound_to_reviewed_candidate(
    monkeypatch, tmp_path,
):
    """TOCTOU note pin: the host's own compose-then-verify precheck refuses by
    name (rather than silently applying) if the composition it just built no
    longer binds to the measured candidate the household reviewed — the
    guard the ARCHITECT ruling asked for, exercised directly rather than by
    trying to win a real race."""
    from jasper.active_speaker import baseline_profile as baseline_profile_mod

    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    real_build = baseline_profile_mod.build_baseline_profile_candidate

    def _tampered_build(*args, **kwargs):
        out = dict(real_build(*args, **kwargs))
        source = dict(out.get("source") or {})
        source["measured_candidate_fingerprint"] = "not-the-reviewed-candidate"
        out["source"] = source
        return out

    monkeypatch.setattr(
        baseline_profile_mod, "build_baseline_profile_candidate", _tampered_build,
    )

    with pytest.raises(v2host.CrossoverV2Refused, match="no longer current"):
        v2host.handle_v2_apply(
            {
                "expected_candidate_fingerprint": candidate.fingerprint,
                "candidate": candidate.to_dict(),
            },
            _bg_run_async,
            _FakeApplyCam,
        )
    assert v2host._applied_gate() is False


def test_apply_blocks_and_persists_a_nudge_when_the_reviewed_preset_goes_stale(
    monkeypatch, tmp_path,
):
    """Negative, through the REAL seam: the household reviewed a candidate
    measured against one crossover design, but the design moved on
    underneath (a second /sound/ save) before Apply landed. The seam's own
    ``measured_candidate_preset_mismatch`` gate must refuse — never silently
    apply the wrong preset — and Finding N's wiring must name that issue and
    persist it for the review_apply nudge, instead of 200 + silent no-op."""
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.active_speaker.design_draft import build_design_draft

    from tests.test_active_speaker_baseline_profile import _research

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    # The crossover design moved on (a second tab/session saved a different
    # crossover frequency) after this candidate was measured.
    moved_research = _research()
    moved_research["crossover_candidates"][0]["frequency_hz"] = 3000
    moved_draft = build_design_draft(
        topology, driver_research=moved_research, created_at="2026-07-18T12:30:00Z",
    )
    (tmp_path / "design_draft.json").write_text(
        json.dumps(moved_draft), encoding="utf-8"
    )
    moved_preview = build_crossover_preview(
        moved_draft, created_at="2026-07-18T12:31:00Z",
    )
    (tmp_path / "crossover_preview.json").write_text(
        json.dumps(moved_preview), encoding="utf-8"
    )

    payload = v2host.handle_v2_apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )

    assert payload["status"] == "blocked"
    assert payload["issue"]["id"] == "measured_candidate_preset_mismatch"
    assert v2host._applied_gate() is False

    saved_state = v2host.load_v2_state()
    assert saved_state["apply_blocked"] == payload["issue"]


# --- W6 run-8 Blocker Q: the v2-aware Undo, through the REAL apply/restore seams --
#
# The verify_fail screen's "Undo" posted to the legacy /crossover/restore,
# which expects a PENDING candidate-apply transaction from the per-driver
# commissioning-run machinery. handle_v2_apply commits straight through
# apply_baseline_profile's own atomic transaction and never creates one, so
# the legacy path 500s ("there is no pending candidate apply to restore")
# and a household stuck on a bad-sounding measured candidate has no way
# back. These tests drive handle_v2_apply then handle_v2_restore through
# the REAL seams (same fixture shape as the Blocker M tests above) — not a
# faked apply gate — so the fix is proven end to end.


def _prior_measured_candidate(preset):
    """The household's pre-existing applied crossover — deliberately a
    DIFFERENT measured candidate from the run-8 shape below, so a passing
    restore is proof of reversion rather than a no-op."""
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverAlignment,
        MeasuredCrossoverCandidate,
    )

    return MeasuredCrossoverCandidate(
        program_id="prog-prior-1",
        analysis={"epsilon_ppm": 5.0, "predicted_ripple_db": 1.2},
        source_preset=preset,
        role_attenuations_db={"tweeter": -2.0, "woofer": 0.0},
        alignment=MeasuredCrossoverAlignment(
            delay_us=250.0, delay_role="tweeter", polarity="keep",
        ),
    )


def test_apply_stashes_pre_apply_profile_and_restore_reverts_through_real_seams(
    monkeypatch, tmp_path,
):
    """Apply the household's pre-existing crossover, apply a run-8-shaped
    measured candidate over it (landing on a content-addressed sibling file
    — the prior config is never overwritten), then Undo. The active config,
    the applied-baseline identity, and the durable v2 state must all revert
    — never the legacy path's 500."""
    from jasper.active_speaker.baseline_profile import (
        apply_baseline_profile,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.crossover_preview import build_crossover_preview

    from tests.test_active_speaker_baseline_profile import _draft

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    config_path = tmp_path / "active_speaker_baseline.yml"
    state_path = tmp_path / "baseline_profile.json"

    prior_candidate = _prior_measured_candidate(preset)
    prior_cam = _FakeApplyCam()
    prior_payload = _bg_run_async(
        apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            load_config=prior_cam.set_config_file_path,
            get_current_config_path=prior_cam.get_config_file_path,
            tuning_owner="automatic",
            measured_candidate=prior_candidate,
        )
    )
    assert prior_payload["status"] == "applied", prior_payload.get("issues")
    prior_config_text = config_path.read_text(encoding="utf-8")

    run8_candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_run8",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": run8_candidate.fingerprint},
        "applied": False,
    })
    apply_payload = v2host.handle_v2_apply(
        {
            "expected_candidate_fingerprint": run8_candidate.fingerprint,
            "candidate": run8_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert apply_payload["status"] == "applied", apply_payload.get("issues")
    run8_config_path = Path(apply_payload["profile"]["config"]["path"])
    assert run8_config_path != config_path
    # The run-8 apply must not have clobbered the prior profile's own file.
    assert config_path.read_text(encoding="utf-8") == prior_config_text

    state_after_apply = v2host.load_v2_state()
    assert state_after_apply["applied"] is True
    pre_apply_profile = state_after_apply.get("pre_apply_profile")
    assert isinstance(pre_apply_profile, dict)
    assert (
        pre_apply_profile["candidate_fingerprint"]
        == prior_payload["profile"]["candidate_fingerprint"]
    )

    restore_payload = v2host.handle_v2_restore(_bg_run_async, _FakeApplyCam)

    assert restore_payload["status"] == "restored", restore_payload.get("issues")
    assert config_path.read_text(encoding="utf-8") == prior_config_text
    active = load_applied_baseline_profile_state(state_path)
    assert active is not None
    assert (
        active["candidate_fingerprint"]
        == prior_payload["profile"]["candidate_fingerprint"]
    )

    state_after_restore = v2host.load_v2_state()
    assert state_after_restore["applied"] is False
    assert state_after_restore["candidate"] is None
    assert state_after_restore["pre_apply_profile"] is None
    assert state_after_restore["accepted_phases"] == []
    # The envelope lands back on the pre-measurement screen — a clean
    # measure/review state, never a half-consistent review_apply pointing at
    # the now-undone candidate.
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_CHECK


def test_restore_refuses_when_run8_apply_was_the_speakers_first_ever(
    monkeypatch, tmp_path,
):
    """No pre-existing applied profile ⇒ nothing to Undo back to — a named
    policy refusal (never a 500), through the REAL apply seam."""
    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_run8",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    payload = v2host.handle_v2_apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert payload["status"] == "applied", payload.get("issues")
    assert v2host.load_v2_state()["pre_apply_profile"] is None

    with pytest.raises(v2host.CrossoverV2Refused, match="no previous crossover"):
        v2host.handle_v2_restore(_bg_run_async, _FakeApplyCam)
