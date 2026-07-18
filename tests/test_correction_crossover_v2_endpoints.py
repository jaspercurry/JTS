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
import threading
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
    yield
    v2host.set_state_path_for_tests(None)
    v2host.set_volume_plan_for_tests(None)


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

    def analyze(program: Any, wav: bytes, priors: Any) -> Any:
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


def test_capture_timeout_maps_to_relay_timeout_and_abandons_volume():
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


def test_phone_abort_is_session_death_abandon_and_invalidation():
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
        "capture_authorized", "capture_result", "capture_set_complete",
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
