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
* ``status_payload``-shape threading: the schema-8 envelope advances through
  the phase screens end-to-end from the durable state the host persists.

Route registration + CSRF ordering ride the existing exact-surface contract
test (tests/test_web_correction_setup.py::test_known_post_routes_reach_csrf_guard,
which drives every ``_POST_ROUTES`` entry — now including the three
``/crossover/v2/*`` routes — to the CSRF guard); this file adds the
flow-selector refusals the dispatch relies on.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2_flow import (
    STAGE1_INCLUDES_CLOUD_MEASURE,
    DEFAULT_CLOUD_MEASURE_POSITIONS,
    MAX_EXTRA_ATTEMPTS_PER_POSITION,
    GEOMETRY_RETRY_OFFSET_CM,
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
    POSITION_ROLES,
    REASON_APPLY_FAILED,
    REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_CORRECTION_NOT_AN_IMPROVEMENT,
    REASON_CORRECTION_MODEL_ERROR,
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    TIER_EXPRESS,
    TIER_FULL,
    CrossoverV2Conductor,
    V2FlowSeams,
    build_v2_cloud_index_phase_map,
    build_v2_session_spec,
    build_v2_verify_session_spec,
    cloud_capture_target,
    format_position_distance,
    locate_failed_diagnosis,
    resolve_plan_shape,
)
from jasper.capture_relay.client import RelayClient
from jasper.capture_relay.session import (
    CaptureAborted,
    CaptureBeginRefused,
    CaptureResult,
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

# The distance the geometry-locked retake rungs ask for, DERIVED from the
# constant the rungs are built from rather than pasted. PR-T4 replaced the
# body-part register the old "forearms" marker keyed on (#1805's 2026-07-28
# ruling); keying on the derived distance is what keeps this pin honest if the
# rung's spread is ever re-tuned.
_WIDER_SPOT_DISTANCE = format_position_distance(GEOMETRY_RETRY_OFFSET_CM)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MODEL_ERROR_PATH",
        str(tmp_path / "model_error.json"),
    )
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


def test_live_model_error_binding_reports_identity_conflict_to_conductor():
    observation = {
        "speaker_id": "speaker-a",
        "attempt_id": "candidate-a",
        "metric": "max_db_notch_excluded",
        "predicted_db": 0.0,
        "realized_db": 0.9,
        "context": {"session_id": "session-a"},
    }

    assert v2host._record_live_model_error(**observation) is True
    assert v2host._record_live_model_error(
        **{**observation, "realized_db": 0.7},
    ) is False


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


def _mint_v2_session(
    backend, spec, *, driver_cls=V2PhoneDriver, record=None, **driver_kwargs
):
    import urllib.parse as _urlparse

    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )

    def register_transport(method, url, headers, body):
        if record is not None:
            record.append((method, _urlparse.urlsplit(url).path))
        return backend(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=register_transport)
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
               analyses=None, index_phase_map=None, tier="",
               retained=None, on_play=None,
               **conductor_kwargs) -> CrossoverV2Conductor:
    """A real conductor whose fake play uploads the phone blob (the acoustic
    seam) and whose fake analyze returns canned per-phase analyses.

    Note the analyze dispatch keys on the EXCITATION PROGRAM's phase, not the
    conductor's: every cloud position plays the VERIFY-shaped mono summed sweep
    (``program.phase == "verify"``), which is exactly why the analyzer needed no
    new dispatch branch for the position groups.
    """
    analyses = analyses or {}

    def play(phase: str, program: Any) -> None:
        if phases_seen is not None:
            block = v2host.crossover_v2_status_block()
            phases_seen.append((phase, block["phase"] if block else None))
        if on_play is not None:
            # Observed at the START of a capture, so it reports the durable
            # state produced by every PRECEDING capture — which is what a
            # test about "when did the apply fire" needs to read.
            on_play(phase, phone.begun[0])
        index, attempt = phone.begun
        backend.phone_upload(
            session.session_id, session.content_key, _wav(attempt),
            index=attempt - 1,
        )

    def analyze(
        program: Any, result: Any, priors: Any, geometry: Any,
        *, phase: str | None = None,
    ) -> Any:
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
        tier=tier,
        seams=V2FlowSeams(
            play=play,
            analyze=analyze,
            publish_check=lambda plan, ambient: published.append(("check", plan)),
            publish_candidate=lambda cand: published.append(("candidate", cand)),
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
            retain_position=(
                None if retained is None
                else lambda pid, result, meta: retained.append((pid, dict(meta)))
            ),
        ),
        index_phase_map=(
            build_v2_cloud_index_phase_map() if index_phase_map is None
            else index_phase_map
        ),
        driver_spacing_m=0.15,
        # A STAGE-1 conductor declares what ``prepare_v2_session`` declares:
        # the correction it proposes is verified by stage 2, so the fit keeps
        # its boost vocabulary even though this session runs no VERIFY of its
        # own (work order D2). Overridable, so a stage-2 conductor built
        # through ``_stage2_conductor_kwargs`` stays on the phase-derived
        # default.
        **{"post_apply_verifies": True, **conductor_kwargs},
    )


# STAGE 1's capture target: CHECK(1), MEASURE(2), then N-1 prompted pre-apply
# positions. Since the two-stage split (work order D1) this is also the LAST
# index of the measuring session — VERIFY belongs to stage 2's own index space,
# where it is index 1.
STAGE1_LAST_INDEX = DEFAULT_CLOUD_MEASURE_POSITIONS + 1
STAGE2_VERIFY_INDEX = 1


def _walk_to_verify(conductor, *, persist: bool = False) -> int:
    """Drive CHECK, MEASURE and the whole pre-apply cloud, one capture per index.

    Returns the last attempt number used, so a caller can continue the global
    attempt counter the way the relay runner does. Tests that are ABOUT the
    post-apply phase use this rather than a hand-written 1/2/3 walk: the cloud
    sits between MEASURE and the end of stage 1, so a three-step walk would be
    testing an index layout no session emits.

    The walk ends with the CONFIRM — the household's explicit set-completion
    signal, which is what fits the correction (work order D1) — so a caller
    holds a built candidate at the end.
    """
    attempt = 0
    for index in range(1, STAGE1_LAST_INDEX + 1):
        attempt += 1
        conductor.consume_capture(index, attempt, CaptureResult(wav=b"fake-capture"))
        if persist:
            v2host.persist_conductor_state(conductor, failure_code=None)
    conductor.confirm_cloud_measure_group()
    if persist:
        v2host.persist_conductor_state(conductor, failure_code=None)
    return attempt


def _stage2_conductor_kwargs() -> dict:
    """What ``prepare_v2_verify`` builds for a post-apply session: applied,
    CHECK/MEASURE already accepted, and stage 2's own index->phase map."""
    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_verify_index_phase_map,
        resolve_plan_shape,
    )

    return {
        "index_phase_map": build_v2_verify_index_phase_map(
            plan_shape=resolve_plan_shape()
        ),
        "accepted_phases": (PHASE_CHECK, PHASE_MEASURE),
        "applied": True,
        "post_apply_verifies": None,
    }


def _stage2_conductor(backend, session, phone, *, published=None, **kwargs):
    """A post-apply (stage-2) conductor over the SAME fakes.

    The journey is two sessions since the split, so a test about the
    post-apply phase builds the conductor stage 2 would build rather than
    consuming a VERIFY index that stage 1's map does not contain.
    """
    return _conductor(
        backend, session, phone,
        published=[] if published is None else published,
        **{**_stage2_conductor_kwargs(), **kwargs},
    )


@contextlib.contextmanager
def _stage2_openable():
    """Satisfy the apply's stage-2 openability preflight (work order D3).

    ``handle_v2_apply`` runs ``resolve_conductor_context`` immediately before
    the transaction commits — PR-T3's half of the preflight, the one that
    catches a household applying from a stale page or a second tab. Tests whose
    subject is the apply TRANSACTION stub the predicate to a pass so they keep
    testing what they are about; the preflight's own behaviour (refusal copy,
    fail-closed on an unexpected error, ordering after the freshness gates) has
    its own tests.
    """
    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = lambda status: object()
    try:
        yield
    finally:
        v2host.resolve_conductor_context = original


def _apply(raw, run_async, camilla_factory, *, status=None):
    """``handle_v2_apply`` with the stage-2 preflight satisfied."""
    with _stage2_openable():
        return v2host.handle_v2_apply(
            raw, run_async, camilla_factory, status={} if status is None else status,
        )


def _run(runner, client, session):
    return asyncio.run(runner(client, session))


def _persisted_failure(state) -> dict:
    """The persisted failure record minus its ``at`` stamp.

    #1942 gave the record its own clock so the envelope can tell a failure the
    household is looking at now from one a previous session left behind. The
    stamp is a ``time.time()`` read, so it cannot be compared by equality —
    but its PRESENCE can, and asserting it here means every call site below
    (each of which already pins the record's exact shape) additionally proves
    its own write path stamps the record. A write that forgot to would render
    as permanently-aged history, which is the failure mode worth catching.
    """
    failure = dict(state["failure"])
    at = failure.pop("at", None)
    assert isinstance(at, float), "every persisted failure carries its own clock"
    return failure


def _build_runner(conductor, volume, **kwargs):
    kwargs.setdefault("poll_interval_s", 0.01)  # fast polling for tests
    kwargs.setdefault("timeout_s", 20.0)
    # Keep the first-begin window equal to the (small) test timeout_s rather
    # than inheriting the production 300s V2_FIRST_BEGIN_TIMEOUT_S default, so a
    # first-begin timeout test still fires fast. Tests that specifically exercise
    # the 300s widening live in test_capture_relay_plan.py.
    kwargs.setdefault("first_begin_timeout_s", kwargs["timeout_s"])
    # A caller that needs to observe/drive the stop signal directly (SF1's
    # interleaving tests) passes its own; every other test gets a fresh one,
    # unchanged from before.
    kwargs.setdefault("stop_event", threading.Event())
    kwargs.setdefault("stop_lock", threading.Lock())
    return v2host.build_v2_run_and_consume(
        conductor,
        volume=volume.hooks(),
        **kwargs,
    )


# --- happy path through the REAL plan runner -----------------------------------


def test_production_happy_path_is_local_check_measure_then_review():
    """The shipped protected-neutral path reaches review without cloud."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding=_BINDING,
        include_cloud_measure=False,
    )
    client, session, phone = _mint_v2_session(backend, spec)
    published: list = []
    phases_seen: list = []
    conductor = _conductor(
        backend, session, phone, published=published, phases_seen=phases_seen,
        index_phase_map=build_v2_cloud_index_phase_map(include_cloud_measure=False),
    )
    volume = VolumeRecorder()
    _run(_build_runner(conductor, volume), client, session)

    assert conductor.current_phase == PHASE_DONE
    assert conductor.accepted_phases == {PHASE_CHECK, PHASE_MEASURE}
    assert [kind for kind, _ in published] == ["check", "candidate"]
    assert PHASE_CLOUD_MEASURE not in conductor.session_phases
    assert phone.completions_posted == 0
    # Fix 2 (W6.4): the CHECK capture's host-event sequence includes the sweep
    # progress pair a real phone's `waitForSweepComplete`
    # (capture-page/js/main.js) polls for around its own play wait --
    # "sweep_started" (cosmetic status text) then "sweep_complete" (the ONLY
    # phase that makes `waitForSweepComplete` return, unblocking the phone to
    # stop recording and upload). Before Fix 2 the v2 runner posted neither, so
    # a real phone would sit until that function's own timeout and never
    # complete a v2 capture (W6 run 5). CHECK is index 1, the first phase
    # authorized in a fresh session.
    #
    # This runner is built WITHOUT a playback-start signal, which is the
    # documented fallback since #1824 D4: no signal ⇒ keep the legacy eager
    # `sweep_started` at arm time, so a host binding its own play seam still
    # resolves the phone's wait. The production shape (ladder posted from
    # inside the play) is pinned by
    # test_phase_ladder_replaces_the_eager_sweep_started_post.
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

    state = v2host.load_v2_state()
    assert state["accepted_phases"] == [PHASE_CHECK, PHASE_MEASURE]
    assert state["session_phases"] == [PHASE_CHECK, PHASE_MEASURE]
    assert state.get("applied") is not True
    assert state.get("verify") is None
    assert state["failure"] is None
    assert state["candidate"]["fingerprint"]

    # The schema-8 envelope advanced through the phase screens end-to-end,
    # rendered purely from the host-persisted status blocks (S1b).
    from jasper.active_speaker.crossover_envelope import build_crossover_envelope

    assert [phase for _p, phase in phases_seen] == [PHASE_CHECK, PHASE_MEASURE]
    class _NoRecovery:
        needs_recovery = False

    v2host.set_volume_plan_for_tests(_NoRecovery())

    def _envelope_for(block_phase):
        return build_crossover_envelope({
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": {"phase": block_phase, "needs_recovery": False},
        })

    screens = [_envelope_for(p)["screen"] for _s, p in phases_seen]
    assert screens == ["microphone_check", "measure"]
    assert v2host.crossover_v2_status_block()["phase"] == "review"


# --- the split: nothing applies without an explicit household POST (D1) ------
#
# Eight tests lived here and are DELETED rather than adapted, because their
# subject is gone: `_fire_auto_apply`, its background thread, its idle-exit
# hold (#1854), its volume-close-on-failure path (#1811), and the two
# stop-vs-in-flight-apply races only that thread could produce. PR-T3 removed
# auto-apply, so a session no longer starts an apply at all — there is no
# thread to hold the wizard alive for, no in-flight apply for a Stop to race,
# and no failed auto-apply to close the volume on (the session's own drain
# already does). The apply is a household POST served in-request. What replaces
# them is the invariant below, plus `handle_v2_apply`'s own tests further down.


def test_no_session_path_applies_anything(monkeypatch):
    """**The load-bearing pin of PR-T3**: no code path applies without an
    explicit household POST (work order D1).

    Drives a WHOLE stage-1 session through the REAL ``run_capture_plan``,
    scripted phone included, with ``handle_v2_apply`` monkeypatched to record
    any call at all. It must record none — while still producing the candidate
    the review screen exists to show. Before the split this same walk applied a
    correction unconditionally, inside the session, three seconds before VERIFY.
    """
    calls: list = []
    monkeypatch.setattr(
        v2host,
        "handle_v2_apply",
        lambda *a, **kw: calls.append((a, kw)) or {"status": "applied"},
    )
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    assert spec.capture_plan.capture_target == 10
    client, session, phone = _mint_v2_session(backend, spec)
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    volume = VolumeRecorder()
    _run(_build_runner(conductor, volume), client, session)

    assert calls == []
    # …and the runner's own wiring cannot fire one: the seam is gone.
    import inspect

    source = inspect.getsource(v2host.build_v2_run_and_consume)
    # Its docstring names the removal, so read the CODE: no call, no thread.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "handle_v2_apply(" not in code
    assert "def _fire_auto_apply" not in code
    assert "threading.Thread" not in code
    # …and the ONE background thread stage 1 does start — the eager-fit
    # rider's speculative group close (2026-07-30) — is read here too rather
    # than left outside this pin's reach. It lives in a module-level helper,
    # so the runner's own "no thread" assertion above stayed literally true
    # while the meaning it stands for ("nothing on the capture path can
    # apply") would have quietly stopped being checked. Its target computes a
    # candidate; making one real still needs the household's confirm, and
    # applying it still needs their separate POST.
    eager = "\n".join(
        line
        for line in inspect.getsource(
            v2host._start_speculative_group_close
        ).splitlines()
        if not line.lstrip().startswith("#")
    )
    # The call form, exactly as above: this helper's prose NAMES the apply it
    # replaced, which is the point of the prose.
    assert "handle_v2_apply(" not in eager
    assert "target=conductor.run_speculative_group_close" in eager

    # The measurement still finished, and still produced a PROPOSAL.
    assert [kind for kind, _ in published] == ["check", "candidate"]
    state = v2host.load_v2_state()
    assert state["candidate"]["fingerprint"]
    assert state.get("applied") is not True
    assert state["failure"] is None
    # A measure-only session resolves to the review interlude, never "done".
    assert v2host.crossover_v2_status_block()["phase"] == "review"
    # The phone signalled the set complete exactly once; the Pi answered with
    # capture_set_complete only after that signal.
    assert phone.completions_posted == 1
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"


def test_the_set_stays_open_until_the_household_signals_it(monkeypatch):
    """The held-set contract, at the runner boundary (work order D1).

    Stage 1's final cloud position IS its capture target, so an unheld runner
    would post ``capture_set_complete`` and return the moment that position was
    accepted — the fit would never run, and the retake window the confirm
    screen exists to keep open would shut in the same instant. This drives the
    walk with a phone that NEVER signals, and pins that the Pi neither closes
    the group nor claims the set complete.
    """
    _skip_purge_grace(monkeypatch)

    class _SilentPhone(V2PhoneDriver):
        def complete_set(self):  # the household who never taps Continue
            self.finished = True

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(
        backend, spec, driver_cls=_SilentPhone,
    )
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    with pytest.raises(CaptureTimeout):
        _run(
            _build_runner(conductor, VolumeRecorder(), timeout_s=0.4),
            client,
            session,
        )

    # Walked in full, never confirmed: no fit, no candidate, nothing durable
    # claiming a proposal exists.
    assert conductor.cloud_measure_group_awaiting_confirm() is True
    assert conductor.candidate is None
    assert [kind for kind, _ in published] == ["check"]
    assert "capture_set_complete" not in backend.phases(session.session_id)


def test_the_group_close_is_the_signal_never_a_begin(monkeypatch):
    """D1's first item, pinned where it can regress.

    ``confirm_cloud_measure_group`` used to gate on a begin whose index was
    past the cloud group, and the host called it on EVERY begin. Now it is an
    explicit entry point with no index at all, called only from the runner's
    completion-signal seam. A conductor that has walked its cloud must not
    close it on an admission of any kind.
    """
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    attempt = 0
    for index in range(1, STAGE1_LAST_INDEX + 1):
        attempt += 1
        conductor.consume_capture(index, attempt, CaptureResult(wav=b"w"))
    assert conductor.cloud_measure_group_awaiting_confirm() is True

    # A voluntary retake of the final position is an admission, not a
    # confirmation — the window this seam exists to keep open.
    conductor.authorize_begin(STAGE1_LAST_INDEX, attempt + 1)
    assert conductor.candidate is None
    assert [kind for kind, _ in published] == ["check"]

    # The explicit close is what fits, exactly once.
    assert conductor.confirm_cloud_measure_group()["candidate_fingerprint"]
    assert conductor.confirm_cloud_measure_group() is None
    assert [kind for kind, _ in published] == ["check", "candidate"]


def test_an_express_session_runs_end_to_end_through_the_real_runner():
    """The express tier's STAGE 1, walked in full — the shape nothing else
    exercises.

    Every other test here drives the Full tier, so express's own structural
    claims (flow-simplification §1.1/§1.2) were only ever asserted against a
    plan object. This runs one through the REAL ``run_capture_plan`` +
    conductor and pins what is different about it after the two-stage split:
    six captures rather than ten, its own held-set confirmation at its own
    tail, a candidate proposed and nothing applied.
    """
    shape = resolve_plan_shape(TIER_EXPRESS)
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding=_BINDING, plan_shape=shape,
    )
    assert spec.capture_plan.capture_target == 6
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_v2_session(backend, spec)
    published: list = []
    conductor = _conductor(
        backend, session, phone, published=published, tier=TIER_EXPRESS,
        index_phase_map=build_v2_cloud_index_phase_map(plan_shape=shape),
    )
    confirms: list = []
    real_confirm = conductor.confirm_cloud_measure_group

    def _watched_confirm():
        payload = real_confirm()
        if payload:
            confirms.append(payload)
        return payload

    conductor.confirm_cloud_measure_group = _watched_confirm  # type: ignore[method-assign]
    _run(_build_runner(conductor, VolumeRecorder()), client, session)

    # The session really ran all six and finished on the household's signal.
    assert conductor.current_phase == PHASE_DONE
    assert PHASE_VERIFY not in conductor.session_phases
    assert phone.completions_posted == 1
    assert len(confirms) == 1
    candidate = next(cand for kind, cand in published if kind == "candidate")
    assert confirms[0]["candidate_fingerprint"] == candidate.fingerprint
    # …and nothing was applied by any of it.
    assert v2host.load_v2_state().get("applied") is not True
    assert v2host.crossover_v2_status_block()["tier"] == TIER_EXPRESS
    assert v2host.crossover_v2_status_block()["phase"] == "review"

    # Stage 2 is where express's end screen lives now — one entry at the mark.
    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_verify_capture_plan,
    )

    stage2 = build_v2_verify_capture_plan(FC_HZ, plan_shape=shape)
    assert stage2.capture_target == 1
    assert stage2.entries[-1].screen["done_title"] == "Your speaker is tuned"


def test_abandoning_an_express_session_before_the_confirm_leaves_the_dsp_alone(
    monkeypatch,
):
    """§2.6's abandonment guarantee on the EXPRESS path too.

    Express reaches the confirm four captures sooner than Full does, so its
    "walked the whole cloud, then stopped" window is a different (and much
    likelier) moment. Stopping there must still leave the speaker untouched.
    """
    _skip_purge_grace(monkeypatch)
    shape = resolve_plan_shape(TIER_EXPRESS)
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding=_BINDING, plan_shape=shape,
    )
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_v2_session(backend, spec)
    phone.abort_after_results = shape.measure_capture_target

    def _abort_stopped(reason="stopped"):
        PhonePlanDriver.abort(phone, "stopped")

    phone.abort = _abort_stopped
    published: list = []
    conductor = _conductor(
        backend, session, phone, published=published, tier=TIER_EXPRESS,
        index_phase_map=build_v2_cloud_index_phase_map(plan_shape=shape),
    )
    with pytest.raises(CaptureAborted) as excinfo:
        _run(_build_runner(conductor, VolumeRecorder()), client, session)

    assert excinfo.value.reason == "stopped"
    assert [kind for kind, _payload in published] == ["check"]
    state = v2host.load_v2_state()
    assert state.get("applied") is not True
    assert state.get("candidate") is None


def test_abandoning_before_the_confirm_leaves_the_speaker_untouched(monkeypatch):
    """Flow-simplification §2.6's safety consequence, at the host boundary.

    Before the group-close move, the FINAL cloud position's acceptance fitted
    the correction and started the apply — so a household that walked the whole
    cloud and then stopped had already had their speaker retuned. Now the fit
    waits for their confirmation, and stopping instead of confirming must leave
    the DSP alone entirely: no candidate published, nothing durable claiming
    applied.
    """
    _skip_purge_grace(monkeypatch)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    # Stop right after the pre-apply cloud's LAST position — walked in full,
    # never confirmed.
    phone.abort_after_results = STAGE1_LAST_INDEX

    def _abort_stopped(reason="stopped"):
        PhonePlanDriver.abort(phone, "stopped")

    phone.abort = _abort_stopped
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    with pytest.raises(CaptureAborted) as excinfo:
        _run(_build_runner(conductor, VolumeRecorder()), client, session)

    assert excinfo.value.reason == "stopped"
    # No CANDIDATE was published (the CHECK gain plan is a different artifact
    # and legitimately rides the same recorder).
    assert [kind for kind, _payload in published] == ["check"]
    state = v2host.load_v2_state()
    assert state.get("applied") is not True
    assert state.get("candidate") is None
    assert _persisted_failure(state) == {"code": "user_stopped"}


# --- relay-session death: timeout + abort (S1c) ---------------------------------


def _skip_purge_grace(monkeypatch):
    """No-op the relay-death / catch-all cleanup grace sleep so a test that only
    cares about the terminal outcome does not wait the real
    TERMINAL_FAILURE_PURGE_GRACE_S. The poll loop uses time.sleep on its own
    thread, so only the async cleanup grace is affected."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def test_measure_accountability_refusal_posts_its_exact_terminal_event(monkeypatch):
    _skip_purge_grace(monkeypatch)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding=_BINDING,
        include_cloud_measure=False,
    )
    client, session, phone = _mint_v2_session(backend, spec)
    conductor = _conductor(
        backend, session, phone, published=[],
        index_phase_map=build_v2_cloud_index_phase_map(include_cloud_measure=False),
    )
    real_consume = conductor.consume_capture

    def refuse_measure(index, attempt, result, entry=None):
        if index == 2:
            raise conductor._refuse(REASON_CORRECTION_NOT_AN_IMPROVEMENT)
        return real_consume(index, attempt, result, entry)

    monkeypatch.setattr(conductor, "consume_capture", refuse_measure)
    volume = VolumeRecorder()
    with pytest.raises(CaptureBeginRefused):
        _run(_build_runner(conductor, volume), client, session)

    event = backend.host_events[session.session_id][-1]
    assert (event["phase"], event["index"], event["code"]) == (
        "capture_result", 2, REASON_CORRECTION_NOT_AN_IMPROVEMENT,
    )
    assert _persisted_failure(v2host.load_v2_state()) == {
        "code": REASON_CORRECTION_NOT_AN_IMPROVEMENT,
    }
    assert volume.events == ["open", "abandon"]


def test_the_terminal_rider_defers_to_a_refusal_the_relay_already_published(
    monkeypatch,
):
    """Panel resilience SF1 — the host-event slot is last-write-wins.

    The RELAY publishes an ``authorize_begin`` refusal as ``capture_refused``
    for the refused index, which is what the phone's authorize loop consumes.
    The rider overwrote it with a ``capture_result`` that loop ignores, so the
    phone showed "session expired" instead of the named refusal.

    Driven through the REAL relay: the slot for index 2 is settled with a
    non-retriable code before the walk, so the phone's begin hits the genuine
    ``authorize_begin`` backstop (owner ruling #2086) and ``run_capture_plan``
    posts the refusal itself. The assertion is on the LAST host event, which is
    the only shape that discriminates — the delta re-review showed an
    ``index``/``code`` conjunction cannot, because the ungated rider addresses
    the last ARMED capture (index 1) and falls back to ``relay_timeout``.
    """
    from jasper.active_speaker import crossover_v2_flow as _flow
    _skip_purge_grace(monkeypatch)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding=_BINDING,
        include_cloud_measure=False,
    )
    client, session, phone = _mint_v2_session(backend, spec)
    conductor = _conductor(
        backend, session, phone, published=[],
        index_phase_map=build_v2_cloud_index_phase_map(include_cloud_measure=False),
    )
    assert conductor.relay_published_refusal is False
    slot = conductor._slot_of_index(2)
    conductor._slot_attempts[slot] = _flow.SlotAttempts(admitted=1)
    conductor._last_reason[slot] = REASON_CORRECTION_NOT_AN_IMPROVEMENT

    with pytest.raises(CaptureBeginRefused):
        _run(_build_runner(conductor, VolumeRecorder()), client, session)

    assert conductor.relay_published_refusal is True
    last = backend.host_events[session.session_id][-1]
    assert (last["phase"], last["index"], last["code"]) == (
        "capture_refused", 2, REASON_CORRECTION_NOT_AN_IMPROVEMENT,
    ), backend.host_events[session.session_id]


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
    assert _persisted_failure(state) == {"code": "relay_timeout"}
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

    # Rendered from the record the REAL persist path just wrote, not a
    # hand-built one — which is also what proves that path stamps a failure
    # freshly enough to still be the live screen (#1942). A write that
    # dropped the stamp would render history here instead.
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {"phase": "check", "failure": state["failure"]},
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
    assert _persisted_failure(state) == {"code": "relay_timeout"}
    assert state["accepted_phases"] == []  # CHECK evidence died with the session


def test_deliberate_phone_stop_gets_its_own_honest_reason_not_relay_timeout(monkeypatch):
    """Gotcha #18 (2026-07-20): a deliberate phone Stop (abort_reason ==
    "stopped") is not a relay-transport death and must not get the "the
    measurement link timed out" copy every OTHER abort reason
    (backgrounded/vanished) gets — see
    test_phone_abort_is_session_death_abandon_and_invalidation above, whose
    default-reason ("backgrounded") abort is UNCHANGED and still classifies
    as relay_timeout."""
    _skip_purge_grace(monkeypatch)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    phone.abort_after_results = 1  # abort right after CHECK's result

    def _abort_stopped(reason="stopped"):
        PhonePlanDriver.abort(phone, "stopped")

    phone.abort = _abort_stopped
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    volume = VolumeRecorder()
    with pytest.raises(CaptureAborted) as excinfo:
        _run(_build_runner(conductor, volume), client, session)

    assert excinfo.value.reason == "stopped"
    assert volume.events == ["open", "abandon"]
    state = v2host.load_v2_state()
    assert _persisted_failure(state) == {"code": "user_stopped"}
    assert state["accepted_phases"] == []


def test_the_review_hold_machinery_is_retained_but_no_longer_on_the_path():
    """Work order D10, made executable.

    ``VERIFY_ANCHOR_HOLD_MESSAGE``, the ``awaiting_apply``
    ``CaptureBeginDeferred``, ``REVIEW_HOLD_BUDGET_S`` and the
    ``review_hold_timeout`` reason stop governing a live session once the apply
    moves into the untimed interlude. D10 keeps them — no new design may depend
    on them, and a conductor built without a prior apply still gets the honest
    hold — so this pins BOTH halves: the machinery still works, and no shipped
    session reaches it.

    **A discrepancy with the work order, recorded here rather than papered
    over.** D10 says the 1-entry recovery re-verify and the ``apply_failed``
    refusal "still reach them". Neither does: ``prepare_v2_verify`` constructs
    its conductor with ``applied=True``, so ``_apply_observed`` short-circuits
    before the deferral AND before the ``apply_failed`` check, which live in
    the same branch. The machinery is genuinely unreached in production now.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        VERIFY_ANCHOR_HOLD_MESSAGE,
        CrossoverV2Conductor,
    )
    from jasper.capture_relay.session import (
        REVIEW_HOLD_BUDGET_S,
        CaptureBeginDeferred,
    )

    assert REVIEW_HOLD_BUDGET_S > 0
    assert VERIFY_ANCHOR_HOLD_MESSAGE

    # No SHIPPED conductor reaches the hold: stage 1 has no VERIFY index at
    # all, and stage 2 is constructed applied.
    assert PHASE_VERIFY not in build_v2_cloud_index_phase_map().values()
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    stage2 = _stage2_conductor(backend, session, phone)
    stage2.authorize_begin(STAGE2_VERIFY_INDEX, 1)  # admitted, never deferred

    # …and a conductor built WITHOUT a prior apply still gets the honest hold,
    # with the household-facing sentence, exactly as before.
    unapplied = CrossoverV2Conductor(
        session_id=session.session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda phase, program: None,
            analyze=lambda program, result, priors, geometry: None,
            publish_check=lambda plan, ambient: None,
            publish_candidate=lambda cand: None,
            apply_complete=lambda: False,
            apply_failed=lambda: "",
        ),
        index_phase_map={1: PHASE_VERIFY},
    )
    with pytest.raises(CaptureBeginDeferred) as excinfo:
        unapplied.authorize_begin(1, 1)
    assert excinfo.value.code == "awaiting_apply"
    assert excinfo.value.user_message == VERIFY_ANCHOR_HOLD_MESSAGE


# --- the full cloud session, end to end through the real runner ---------------


def test_full_cloud_session_with_a_mid_cloud_retake_through_the_real_runner():
    """The whole shipped STAGE-1 choreography — 10 prompted captures plus one
    retake — driven by a scripted phone through the REAL ``run_capture_plan``,
    the REAL conductor, and the REAL capture-relay registration.

    This is the test that finally exercises PR-3a's capacity raise in anger:
    the plan's ``max_attempts`` is above the pre-capacity relay's frozen
    ceiling, so registration probes ``GET /capabilities`` (dormant until now,
    since no builder emitted a plan that large), and the 11th attempt writes
    blob index 10 — a key the old 8-index space could not have stored.
    """
    from jasper.capture_relay.spec import LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    # RE-DERIVED (work order D1): stage 1 is 10 captures, not the 16 of the
    # single session it replaced; ``cloud_capture_target()`` still names the
    # whole journey.
    assert spec.capture_plan.capture_target == 10
    assert cloud_capture_target() == 16
    assert spec.capture_plan.max_attempts > LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS

    requests: list = []
    client, session, phone = _mint_v2_session(backend, spec, record=requests)
    # The capacity gate fired at registration, ahead of POST /sessions.
    assert ("GET", "/capabilities") in requests
    assert requests.index(("GET", "/capabilities")) < requests.index(
        ("POST", "/sessions")
    )

    # A REAL geometry retake (S10): the pre-apply group's last position closes
    # the group, the injected combine reports a hard lock, and the conductor
    # rejects that position asking for a wider one. This is the production
    # path end to end — not a stand-in rejection that merely has the same
    # transport shape.
    last_cloud_measure_index = DEFAULT_CLOUD_MEASURE_POSITIONS + 1
    locks = {"n": 0}

    def one_lock(_combined, n_positions):
        locks["n"] += 1
        return {
            "locked": locks["n"] == 1, "reason": "geometry_locked",
            "thin_evidence": False, "n_positions": n_positions,
            "median_tau_us": 320.0, "clustered_fraction": 1.0, "n_confident": 6,
        }

    # S3 review finding (2026-07-26): _close_cloud_group combines a group's
    # positions exactly ONCE and derives its retry-gating verdict via
    # _geometry_verdict_from_combined — the seam to patch, not
    # cloud_geometry_verdict (a positions-only convenience wrapper no longer
    # called on this path). The real (unmocked) combine still runs; this
    # patch only replaces what verdict is derived from its result.
    monkeypatch_target = (
        "jasper.active_speaker.crossover_v2_flow._geometry_verdict_from_combined"
    )
    published: list = []
    retained: list = []
    conductor = _conductor(
        backend, session, phone, published=published, retained=retained,
    )
    volume = VolumeRecorder()
    with mock.patch(monkeypatch_target, side_effect=one_lock):
        _run(_build_runner(conductor, volume), client, session)

    assert conductor.current_phase == PHASE_DONE
    assert conductor.candidate is not None  # proposed, not applied
    assert v2host.load_v2_state().get("applied") is not True
    # 10 accepted captures + the one geometry retake = 11 attempts, so the last
    # blob rode index 10 — beyond the legacy 0..7 space entirely.
    results = [
        e for e in backend.host_events[session.session_id]
        if e.get("phase") == "capture_result"
    ]
    assert len(results) == 11
    assert sum(1 for e in results if e["accepted"]) == 10
    assert max(e["attempt"] for e in results) - 1 == 10
    rejections = [e for e in results if not e["accepted"]]
    assert [e["index"] for e in rejections] == [last_cloud_measure_index]
    # B2: the rejection carries its household copy AND the wider-spot
    # instruction onto the wire, which is what the phone now renders.
    #
    # RE-DERIVED for PR-T4's register change (#1805/#1806): the rung is a
    # numeric ABSOLUTE pose now, so the marker is the wider-spot DISTANCE
    # rather than the withdrawn "forearms". Asserting the distance is also
    # strictly stronger — it names the spread the rung actually asks for, which
    # a body-part unit never did.
    assert rejections[0]["code"] == REASON_CLOUD_GEOMETRY_LOCKED
    assert rejections[0]["reason"]
    assert _WIDER_SPOT_DISTANCE in rejections[0]["prompt"]

    # The pre-apply group closed with a geometry verdict on record; the
    # post-apply one belongs to stage 2 and this session never had it.
    assert PHASE_CLOUD_MEASURE in conductor.accepted_phases
    assert conductor.group_geometry(PHASE_CLOUD_MEASURE) is not None
    assert PHASE_CLOUD_VERIFY not in conductor.session_phases
    assert len(conductor.group_positions(PHASE_CLOUD_MEASURE)) == (
        DEFAULT_CLOUD_MEASURE_POSITIONS - 1
    )
    # The replaced take left no position behind; the accepted retake did.
    ids = conductor.group_positions(PHASE_CLOUD_MEASURE)
    assert len(ids) == len(set(ids))
    # B3: BOTH takes of the retaken position reached the retention seam — the
    # superseded one is the walk record — and they are distinguishable, because
    # a bare position id is ambiguous once a retake shares it.
    retaken_id = f"{PHASE_CLOUD_MEASURE}_{last_cloud_measure_index:02d}"
    takes = [meta for pid, meta in retained if pid == retaken_id]
    assert len(takes) == 2
    assert takes[0]["attempt"] != takes[1]["attempt"]
    # And the SURVIVING take is the retake, recorded with the wider-spot prompt
    # the operator actually followed — never the original table entry, which
    # named a spot they were told to abandon.
    surviving = {
        t["attempt"] for t in conductor.group_position_takes(PHASE_CLOUD_MEASURE)
        if t["position_id"] == retaken_id
    }
    assert surviving == {takes[1]["attempt"]}
    assert _WIDER_SPOT_DISTANCE in takes[1]["prompt"]
    assert takes[1]["wide"] is True
    assert _WIDER_SPOT_DISTANCE not in takes[0]["prompt"]
    # …and each take carries the named question its position answers, so the
    # attribution stage reads a labelled sample rather than an anonymous member
    # of an average (attribution-stage plan §5 promotion queue item 1).
    assert takes[0]["role"] in POSITION_ROLES
    assert takes[1]["role"] in POSITION_ROLES

    # §5.5 unchanged: exactly one volume open, exact restore on the done path.
    # A measure-only session that finishes IS done — every phase it ran is
    # accepted — so it takes the same clean CLOSE the single session took.
    assert volume.events[0] == "open"
    assert volume.events[-1] == "close"
    assert "abandon" not in volume.events
    # …and the set ended on the household's own signal.
    assert phone.completions_posted == 1
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"

    # The durable state discloses the cloud outcome — PR-4's own extension.
    state = v2host.load_v2_state()
    # Stage 1 discloses its OWN group; the post-apply one is stage 2's.
    assert set(state["cloud"]) == {PHASE_CLOUD_MEASURE}
    for phase, block in state["cloud"].items():
        assert block["geometry"]["locked"] in (True, False)
        # Position + attempt, so a reader can tell which take of a retaken
        # position is the one actually in the cloud.
        assert block["positions"]
        assert all(
            set(entry) == {"position_id", "index", "attempt"}
            for entry in block["positions"]
        )
        # PR-4: the honest-instrument pipeline ran (not "None" — a group that
        # closed always gets a verdict, even a degraded one) and its /state
        # projection agrees.
        pipeline = block["pipeline"]
        assert pipeline["available"] in (True, False)
        if pipeline["available"]:
            assert "spec" in pipeline and "null_registry" in pipeline
            # Issue #1763: the applied analysis band NEVER reaches the durable
            # state without the disclosure of how it was derived — a reader
            # here can tell a contract-derived band from an HF-regime-clamped
            # one without going to the journal.
            provenance = pipeline["echo_band_provenance"]
            assert set(provenance) == {
                "source", "hf_regime_clamped", "derived_lo_hz", "floor_hz",
            }
            assert provenance["source"] in {
                "declared", "undeclared_default",
                "clamp_degenerate_default", "passband_fallback",
            }
            assert isinstance(provenance["hf_regime_clamped"], bool)
            unclamped_contract = (
                not provenance["hf_regime_clamped"]
                and provenance["source"] in {"declared", "undeclared_default"}
            )
            if unclamped_contract:
                # No clamp on this path, so the applied lower edge IS the
                # contract-derived one. (The two fallback sources publish the
                # band they fell back TO, so their derived edge legitimately
                # differs — see _CloudEchoBand.)
                assert provenance["derived_lo_hz"] == pipeline["echo_band_hz"][0]
        assert conductor.group_cloud_result(phase) == pipeline
    compact = v2host.crossover_v2_status_block()["cloud"]
    assert set(compact) == {PHASE_CLOUD_MEASURE}
    for entry in compact.values():
        assert isinstance(entry["geometry_locked"], bool)
        # None (not 0) is the honest shape when the pipeline never became
        # available (SF-1 review finding, 2026-07-27) — this real walk's
        # pipeline availability isn't pinned above, so accept either the
        # honestly-typed int (available) or None (unavailable), never a
        # fabricated 0.
        assert entry["excluded_interval_count"] is None or isinstance(
            entry["excluded_interval_count"], int
        )


def test_a_position_that_spends_its_extras_is_dropped_and_the_real_runner_goes_on():
    """Owner ruling #2086, end to end through the SHIPPED runner.

    The conductor unit tests prove the settled verdict's SHAPE; only this
    proves it is a shape ``run_capture_plan`` acts on. That matters because the
    whole design rests on one protocol fact: the runner completes a
    fixed-length set at exactly ``capture_target`` ACCEPTED captures with
    ``index == accepted_count + 1``, so a position the flow gives up on has to
    look accepted on the wire or the phone re-prompts the same spot forever.

    On 2026-08-03 this shape ended the session instead: the fourth failure at
    one position raised ``CaptureBeginRefused`` before any audio played, while
    the household's screen still read "one last time".
    """
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)

    # A mid-cloud position (never the group's last, so nothing here can be
    # confused with the geometry ladder) that cannot be located, ever.
    doomed = DEFAULT_CLOUD_MEASURE_POSITIONS - 1
    assert build_v2_cloud_index_phase_map()[doomed] == PHASE_CLOUD_MEASURE
    assert doomed != STAGE1_LAST_INDEX, "not the group's last position"
    attempts_at_doomed: list[int] = []

    def verify_analysis(program):
        index, attempt = phone.begun
        if index != doomed:
            return _verify_analysis(program)
        attempts_at_doomed.append(attempt)
        return _verify_analysis(
            program, locate_confidence=0.0, pilot_snr_ok=True,
        )

    published: list = []
    conductor = _conductor(
        backend, session, phone,
        published=published,
        analyses={"verify": verify_analysis},
    )
    volume = VolumeRecorder()
    _run(_build_runner(conductor, volume), client, session)

    # FINITE: the planned capture plus exactly three extras, then the flow
    # stopped asking. Not "until the plan's attempt budget ran out".
    assert len(attempts_at_doomed) == MAX_EXTRA_ATTEMPTS_PER_POSITION + 1

    # AND THE SESSION LIVED. The walk finished, the group closed, and a
    # candidate was built from the positions that did land.
    assert conductor.current_phase == PHASE_DONE
    assert conductor.candidate is not None
    assert PHASE_CLOUD_MEASURE in conductor.accepted_phases
    assert volume.events[-1] == "close"
    assert "abandon" not in volume.events
    assert backend.phases(session.session_id)[-1] == "capture_set_complete"

    # ATTRIBUTED: the give-up rode the wire carrying the condition observed,
    # and the count the phone renders said the tries were gone.
    results = [
        e for e in backend.host_events[session.session_id]
        if e.get("phase") == "capture_result"
    ]
    settled = [e for e in results if e.get("unresolved")]
    assert [e["index"] for e in settled] == [doomed]
    assert settled[0]["unresolved"] == {
        "index": doomed,
        "code": REASON_LOCATE_FAILED,
        "diagnosis": locate_failed_diagnosis(True),
    }
    assert settled[0]["accepted"] is True
    assert settled[0]["attempts"]["left"] == 0
    assert settled[0]["attempts"]["by_household"] == 3

    # …and the dropped position is genuinely absent from the evidence, so the
    # correction was fitted from one fewer spot rather than from a fabricated
    # one.
    walked = {
        int(pid.rsplit("_", 1)[1])
        for pid in conductor.group_positions(PHASE_CLOUD_MEASURE)
    }
    assert doomed not in walked
    assert len(walked) == DEFAULT_CLOUD_MEASURE_POSITIONS - 2


def test_a_spent_final_slot_close_refusal_ends_the_real_runner_immediately():
    """Closing X replaces the position miss at the real runner boundary.

    The final post-apply cloud position spends planned+3 on locate misses, is
    left out, and the group close then hard-stops on a model error. The runner
    must publish that close-time truth as its final event and return — never
    wait for a next begin that the position ledger will refuse.
    """
    shape = resolve_plan_shape()
    spec = build_v2_verify_session_spec(
        FC_HZ, acknowledgement_binding=_BINDING, plan_shape=shape,
    )
    backend = FakePlanRelayBackend()
    client, session, phone = _mint_v2_session(backend, spec)
    doomed = spec.capture_plan.capture_target
    attempts_at_doomed: list[int] = []

    def verify_analysis(program):
        index, attempt = phone.begun
        if index != doomed:
            return _verify_analysis(program)
        attempts_at_doomed.append(attempt)
        return _verify_analysis(
            program, locate_confidence=0.0, pilot_snr_ok=True,
        )

    conductor = _stage2_conductor(
        backend,
        session,
        phone,
        analyses={"verify": verify_analysis},
    )
    conductor._delta_probe_refusal = (  # type: ignore[method-assign]
        lambda _probe: (
            REASON_CORRECTION_MODEL_ERROR
            if conductor.current_phase == PHASE_CLOUD_VERIFY
            else None
        )
    )
    _run(_build_runner(conductor, VolumeRecorder()), client, session)

    assert len(attempts_at_doomed) == MAX_EXTRA_ATTEMPTS_PER_POSITION + 1
    phases = backend.phases(session.session_id)
    assert phases[-1] == "capture_result"
    assert "capture_set_complete" not in phases
    assert "capture_set_exhausted" not in phases
    terminal = backend.host_events[session.session_id][-1]
    assert terminal["index"] == doomed
    assert terminal["accepted"] is False
    assert terminal["code"] == REASON_CORRECTION_MODEL_ERROR
    assert terminal["reason"] == REASON_REGISTRY[REASON_CORRECTION_MODEL_ERROR].message
    assert terminal["terminal"] is True
    assert terminal["terminal_outcome"] == "phase_cannot_proceed"
    assert terminal["attempts"]["left"] == 0
    assert "unresolved" not in terminal
    assert "could hear the speaker" not in terminal["reason"]


def test_position_retention_survives_a_retake_through_the_real_evidence_store(
    tmp_path,
):
    """The retention seam against the REAL, write-once store — not a lambda.

    Round-1 review blocker B3: every other test substitutes a recorder for
    ``retain_position``, so nothing exercised the store's write-once contract.
    Against the real one, two takes of a retaken position used to collide on a
    single path: the second write was refused (fail-soft, so the session
    survived) and the ONLY surviving sidecar described the REPLACED take — its
    wav, its prompt — while the curve actually in the cloud had no record at
    all. That inverts the forensic honesty the retention bump was paid for.

    What this pins: both takes persist, under distinguishable paths, each
    describing itself.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_2_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    refs: dict = {}
    retain = v2host.bind_position_retention(store, "cap_retake_session", refs)

    position_id = f"{PHASE_CLOUD_MEASURE}_10"
    base = {
        "position_id": position_id, "phase": PHASE_CLOUD_MEASURE, "index": 10,
        "wide": False, "role": "onax", "captured_at": 1.0,
        "session_id": "cap_retake_session",
        "gate_window_ms": 8.0, "validity_floor_hz": 140.0,
        "gating_applied": True, "summed_ripple_db": 1.0,
        "glitch_detected": False,
    }
    retain(
        position_id, CaptureResult(wav=b"first-take"),
        {**base, "attempt": 10,
         "prompt": "Move the microphone 10 in (25 cm) to the LEFT of the "
                   "mark, at mark height."},
    )
    retain(
        position_id, CaptureResult(wav=b"wider-retake"),
        {**base, "attempt": 11, "wide": True, "role": "offax",
         "prompt": "Same measurement, wider spot: move the microphone "
                   "30 in (75 cm) to the LEFT of the mark."},
    )

    # Two artifacts, both published — the second is NOT a refused duplicate.
    assert [entry["attempt"] for entry in refs["position_artifacts"]] == [10, 11]
    assert len({entry["artifact"] for entry in refs["position_artifacts"]}) == 2

    # The strict store namespaces every artifact under evidence/v1/artifacts/.
    sidecars = sorted(
        (Path(info["bundle_dir"]) / "evidence" / "v1" / "artifacts"
         / "crossover_v2" / "cap_retake_session" / "positions").glob("*.json")
    )
    assert [p.name for p in sidecars] == [
        f"{position_id}_a10.json", f"{position_id}_a11.json",
    ]
    first, second = (json.loads(p.read_text()) for p in sidecars)
    # Each sidecar describes ITS OWN take: its prompt, its wav.
    assert first["attempt"] == 10 and second["attempt"] == 11
    assert "wider spot" in second["prompt"]
    assert "wider spot" not in first["prompt"]
    assert second["wide"] is True
    # The role rides the sidecar — it is the durable half of the promotion.
    assert first["role"] == "onax" and second["role"] == "offax"
    assert first["wav_path"] != second["wav_path"]
    for record in (first, second):
        wav = Path(info["bundle_dir"]) / record["wav_path"]
        assert wav.is_file() and wav.stat().st_size == record["wav_bytes"]
    assert (
        Path(info["bundle_dir"]) / first["wav_path"]
    ).read_bytes() == b"first-take"
    assert (
        Path(info["bundle_dir"]) / second["wav_path"]
    ).read_bytes() == b"wider-retake"


def test_cloud_publisher_writes_one_artifact_per_group_through_the_real_store(
    tmp_path,
):
    """Flat-linearization plan PR-4's ``publish_cloud`` seam against the REAL,
    write-once evidence store — mirrors
    ``test_position_retention_survives_a_retake_through_the_real_evidence_store``
    above, proving the two-groups-in-one-session shape does not collide.

    This is the mechanism deviation ``bind_cloud_publisher`` documents: the
    work order's literal ``crossover_v2/<session>/cloud.json`` would be
    written TWICE in one real session (once per closed group), and the store
    refuses a repeated path — so each group gets its own
    ``<phase>.json`` artifact instead.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_2_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    refs: dict = {}
    publish_cloud = v2host.bind_cloud_publisher(store, "cap_cloud_session", refs)

    measure_result = {
        "available": True,
        "geometry": {"locked": True, "reason": "geometry_locked"},
        "null_registry": {"classification": "position_invariant"},
        "spec": {"overall_passed": False},
        "curve": {"freqs_hz": [100.0, 200.0], "magnitude_db": [-1.0, -2.0]},
    }
    verify_result = {
        "available": True,
        "geometry": {"locked": False, "reason": "geometry_insufficient_usable_estimates"},
        "null_registry": {"classification": "insufficient_evidence"},
        "spec": {"overall_passed": True},
        "curve": {"freqs_hz": [100.0, 200.0], "magnitude_db": [-0.5, -0.6]},
    }
    publish_cloud(PHASE_CLOUD_MEASURE, measure_result)
    publish_cloud(PHASE_CLOUD_VERIFY, verify_result)

    # Both artifacts published, both fingerprints recorded — no collision.
    assert set(refs["cloud_artifacts"]) == {PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY}
    assert (
        refs["cloud_artifacts"][PHASE_CLOUD_MEASURE]
        != refs["cloud_artifacts"][PHASE_CLOUD_VERIFY]
    )

    artifacts_dir = (
        Path(info["bundle_dir"]) / "evidence" / "v1" / "artifacts"
        / "crossover_v2" / "cap_cloud_session"
    )
    # One CLOUD artifact per closed group, distinctly named — the claim this
    # test exists for. Attribution's per-phase finding set (WO-1) rides in the
    # same directory under its own `findings_` prefix and shares the same
    # per-phase rule, so it is named here rather than allowed to widen the
    # assertion into "whatever happens to be on disk".
    assert sorted(p.name for p in artifacts_dir.glob("*.json")) == [
        f"{PHASE_CLOUD_MEASURE}.json",
        f"{PHASE_CLOUD_VERIFY}.json",
        f"findings_{PHASE_CLOUD_MEASURE}.json",
        f"findings_{PHASE_CLOUD_VERIFY}.json",
    ]
    measure_on_disk = json.loads(
        (artifacts_dir / f"{PHASE_CLOUD_MEASURE}.json").read_text()
    )
    assert measure_on_disk["kind"] == "jts_crossover_v2_cloud_evidence"
    assert measure_on_disk["relay_session_id"] == "cap_cloud_session"
    assert measure_on_disk["phase"] == PHASE_CLOUD_MEASURE
    assert measure_on_disk["geometry"]["locked"] is True
    assert measure_on_disk["null_registry"]["classification"] == "position_invariant"
    assert measure_on_disk["curve"]["freqs_hz"] == [100.0, 200.0]
    verify_on_disk = json.loads(
        (artifacts_dir / f"{PHASE_CLOUD_VERIFY}.json").read_text()
    )
    assert verify_on_disk["geometry"]["locked"] is False
    assert verify_on_disk["spec"]["overall_passed"] is True


def test_state_cloud_block_is_the_compact_projection_of_the_durable_pipeline():
    """PR-4's ``/state`` surface: per band, only ``passed``; the
    excluded-interval COUNT, not the intervals; the geometry verdict's two
    household-relevant bits. The full per-null τ/r/evidence numbers stay in
    the durable state's own ``pipeline`` sub-key (not re-derived here) and
    the bundle artifact — this is the dashboard-sized read, not a third
    owner of the same data."""
    v2host.save_v2_state({
        "session_id": "cap_state",
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked", "thin_evidence": False},
                "positions": [],
                "pipeline": {
                    "available": True,
                    "merged_excluded_bands_hz": [[8000.0, 9000.0], [11000.0, 12000.0]],
                    "spec": {
                        "overall_passed": False,
                        "reference_db": -27.27,
                        "bands": [
                            {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": True,
                             "max_deviation_db": 1.02, "tolerance_db": 1.5},
                            {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "passed": True,
                             "max_deviation_db": -1.41, "tolerance_db": 2.0},
                            {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "passed": False,
                             "max_deviation_db": -4.85, "tolerance_db": 2.5},
                        ],
                    },
                    "flatness": {
                        "max_db": -4.85, "max_hz": 11480.0,
                        "max_band_hz": [8000.0, 16000.0], "tolerance_db": 2.5,
                        "rms_db": 1.37, "n_bins": 900, "n_excluded": 42,
                        "evaluable": True, "passed": False,
                    },
                    "validity_floor_hz": 187.5,
                },
            },
            PHASE_CLOUD_VERIFY: {
                "geometry": {"locked": False, "reason": "geometry_insufficient_usable_estimates"},
                "positions": [],
                "pipeline": {"available": False, "reason": "combine_failed"},
            },
        },
    })

    block = v2host.crossover_v2_status_block()
    cloud = block["cloud"]
    assert set(cloud) == {PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY}

    measure = cloud[PHASE_CLOUD_MEASURE]
    assert measure["geometry_locked"] is True
    assert measure["thin_evidence"] is False
    # Computed straight from the geometry verdict (SF-1 review finding,
    # 2026-07-27), not read out of the pipeline's own copy — the fixture
    # above deliberately carries no ``pipeline.geometry_guidance`` key to
    # prove that.
    assert measure["geometry_guidance"] == (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )
    assert measure["overall_passed"] is False
    assert measure["excluded_interval_count"] == 2
    # Per-band ``max_deviation_db``/``tolerance_db`` ride along
    # (flat-linearization PR-5 N-3 / PR-7): `/state` is what a chart reads,
    # and per-band numbers missing from the only projection a page sees is
    # the pressure that grows a second derivation downstream.
    assert measure["spec_bands"] == [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": True,
         "max_deviation_db": 1.02, "tolerance_db": 1.5},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "passed": True,
         "max_deviation_db": -1.41, "tolerance_db": 2.0},
        {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "passed": False,
         "max_deviation_db": -4.85, "tolerance_db": 2.5},
    ]
    # PR-7: the report-level reference the tolerance corridor is centered on
    # rides the entry too, copied verbatim like everything else here.
    assert measure["reference_db"] == -27.27
    # The gauge is copied verbatim; the clamp is separable from interference
    # on this live surface (PR-5 SF-2), so a reader can tell a combed room
    # apart from one capture's collapsed gate.
    assert measure["flatness"]["max_db"] == -4.85
    assert measure["flatness"]["rms_db"] == 1.37
    assert measure["validity_floor_hz"] == 187.5

    # A group whose pipeline never became available (combine_failed) reports
    # the honest "nothing to disclose" shape, never a fabricated pass --
    # excluded_interval_count is None, not 0 (SF-1 review finding,
    # 2026-07-27): 0 would read as "the pipeline looked and found nothing",
    # a fabricated-clean claim for a pipeline that never ran.
    verify = cloud[PHASE_CLOUD_VERIFY]
    assert verify["geometry_locked"] is False
    assert verify["overall_passed"] is None
    assert verify["excluded_interval_count"] is None
    assert verify["spec_bands"] == []
    assert verify["geometry_guidance"] == ""
    # Same rule for the two PR-5 keys: unavailable means unknown, never a
    # fabricated zero or a floor of 0 Hz.
    assert verify["flatness"] is None
    # PR-7: same rule again for the chart's own reference level.
    assert verify["reference_db"] is None
    assert verify["validity_floor_hz"] is None


def test_state_cloud_block_reports_locked_guidance_even_when_pipeline_never_ran():
    """SF-1 review finding (2026-07-27): a locked group's "spread the mic
    further" guidance must survive an unrelated downstream pipeline failure,
    not disappear with it -- geometry locking is decided and RECORDED
    BEFORE the honest-instrument pipeline ever runs (see
    ``_close_cloud_group``), so the guidance is a pure function of the
    geometry verdict alone. Before the fix, an unavailable pipeline
    defaulted ``geometry_guidance`` to ``""`` regardless of the geometry
    verdict -- a locked-but-pipeline-failed group silently lost its one
    actionable piece of copy. Also pins the sibling fix: ``excluded_interval_count``
    is ``None``, never a fabricated ``0``, when the pipeline never became
    available."""
    v2host.save_v2_state({
        "session_id": "cap_state_locked_unavailable",
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {
                    "locked": True, "reason": "geometry_locked",
                    "thin_evidence": False,
                },
                "positions": [],
                "pipeline": {"available": False, "reason": "combine_failed"},
            },
        },
    })

    measure = v2host.crossover_v2_status_block()["cloud"][PHASE_CLOUD_MEASURE]
    assert measure["geometry_locked"] is True
    assert measure["excluded_interval_count"] is None
    assert measure["overall_passed"] is None
    assert measure["spec_bands"] == []
    assert measure["geometry_guidance"] == (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )


def test_state_cloud_block_is_none_before_any_group_closes():
    v2host.save_v2_state({"session_id": "cap_fresh"})
    assert v2host.crossover_v2_status_block()["cloud"] is None


def test_cloud_summary_stamps_the_producing_session_id():
    """PR-7's provenance marker: ``_cloud_summary`` stamps each closed
    phase's dict with the CONDUCTOR's own session id, so a later carry-
    forward (``persist_conductor_state``'s B1 branch, which copies this
    whole per-phase dict verbatim) can still say which session actually
    produced it — see ``_compact_cloud_status``'s ``provenance_note``."""
    fake = SimpleNamespace(
        session_id="cap_producer_session",
        session_phases=(PHASE_CLOUD_MEASURE,),
        group_geometry=lambda phase: {"locked": True, "reason": "geometry_locked"},
        group_position_takes=lambda phase: [],
        group_cloud_result=lambda phase: {
            "available": True, "spec": {"overall_passed": True},
        },
    )
    summary = v2host._cloud_summary(fake)
    assert summary[PHASE_CLOUD_MEASURE]["session_id"] == "cap_producer_session"


def test_provenance_note_reflects_whether_the_group_matches_the_active_session():
    """The household-facing half of the same marker
    (``_compact_cloud_status``'s ``provenance_note``, PR-7). Three states,
    told apart rather than collapsed: the stamped producer matches the
    caller's current session (nothing to say — the chart is fresh); it
    disagrees (a group carried forward from an earlier session — say so);
    or there is no stamp at all (a durable state written before this marker
    existed — unknown, not stale, so an upgrade cannot manufacture a false
    warning for data nobody ever mis-attributed)."""
    pipeline = {"available": True, "spec": {"overall_passed": True, "bands": []}}
    stamped_state = {
        PHASE_CLOUD_VERIFY: {
            "geometry": {"locked": False},
            "positions": [],
            "pipeline": pipeline,
            "session_id": "cap_producer_session",
        },
    }

    fresh = v2host._compact_cloud_status(
        stamped_state, current_session_id="cap_producer_session",
    )
    assert fresh[PHASE_CLOUD_VERIFY]["provenance_note"] == ""

    stale = v2host._compact_cloud_status(
        stamped_state, current_session_id="cap_rearm_session",
    )
    assert stale[PHASE_CLOUD_VERIFY]["provenance_note"] == (
        "This chart is from a previous session's measurement — "
        "re-measure to see this session's own result."
    )

    legacy_state = {
        PHASE_CLOUD_VERIFY: {
            "geometry": {"locked": False}, "positions": [], "pipeline": pipeline,
        },
    }
    legacy = v2host._compact_cloud_status(
        legacy_state, current_session_id="cap_rearm_session",
    )
    assert legacy[PHASE_CLOUD_VERIFY]["provenance_note"] == ""

    # Backward compatibility: an existing caller that never passes
    # current_session_id at all (every test seam before this PR) still gets
    # the honest "unknown" reading, not a crash or a fabricated verdict.
    no_current = v2host._compact_cloud_status(stamped_state)
    assert no_current[PHASE_CLOUD_VERIFY]["provenance_note"] == ""


def test_verify_rearm_preserves_candidate_identity_and_cloud_block(monkeypatch):
    """A new VERIFY relay keeps the applied candidate and its cloud evidence.

    B1 (blocker, 2026-07-26 review): a verify-only re-arm's conductor
    (``prepare_v2_verify``'s ``index_phase_map={1: PHASE_VERIFY}``) has no
    group phase in ITS OWN session, so ``_cloud_summary`` honestly returns
    ``None`` for it — but the OLD session-id-gated carry-forward turned that
    ``None`` into a destructive overwrite of a real prior cloud verdict.
    One tap of "Try again" (the PRIMARY next_action after a failed verify)
    used to blank `/state.crossover_v2.cloud`, the envelope's ``cloud`` key,
    AND make the doctor report "no cloud-measurement session recorded yet"
    for a session that very much ran.

    Walks: a completed cloud session (durable state seeded, mirroring what
    ``persist_conductor_state`` would have written) -> the REAL re-arm
    conductor + the REAL ``persist_conductor_state`` call (the exact
    production seam ``prepare_v2_verify``'s ``_open`` uses, mirroring
    ``test_second_apply_pre_apply_profile_survives_the_deferred_verify_rearm``'s
    own pattern for ``pre_apply_profile``) -> asserts all three surfaces
    (`/state`, the envelope, the doctor) still see the cloud verdict. The
    candidate assertion also pins #2079's crash/retry write identity: the
    fingerprint must survive this same new-session rebind so a recovery
    VERIFY cannot become a second model-error observation.
    """
    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
    from jasper.cli.doctor.correction import check_crossover_v2_cloud_pipeline

    cloud_block = {
        PHASE_CLOUD_MEASURE: {
            "geometry": {"locked": True, "reason": "geometry_locked", "thin_evidence": False},
            "positions": [{"position_id": "cloud_measure_09", "index": 9, "attempt": 9}],
            "pipeline": {
                "available": True,
                "geometry_guidance": "Spread the mic further.",
                "merged_excluded_bands_hz": [[8000.0, 9000.0]],
                "spec": {
                    "overall_passed": False,
                    "bands": [{"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "passed": False}],
                },
            },
        },
    }
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-original"},
        "applied": True,
        "cloud": cloud_block,
        "evidence": {
            "bundle_session_id": "bundle-1",
            "cloud_artifacts": {PHASE_CLOUD_MEASURE: "artifact-fingerprint-abc"},
        },
    })

    # The real production seam: prepare_v2_verify's _open mints a fresh
    # conductor bound to a NEW relay session id and immediately persists it
    # ("Keep the durable candidate/applied facts; rebind the session id.").
    conductor = CrossoverV2Conductor(
        session_id="cap_rearm_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map={1: PHASE_VERIFY},
    )
    v2host.persist_conductor_state(
        conductor, failure_code=None, evidence={"bundle_session_id": "bundle-2"},
    )

    # Surface 1: the durable state itself.
    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["candidate"] == {"fingerprint": "fp-original"}
    assert state["cloud"] == cloud_block
    assert state["evidence"]["cloud_artifacts"] == {
        PHASE_CLOUD_MEASURE: "artifact-fingerprint-abc"
    }

    # Surface 2: /state's compact projection.
    compact = v2host.crossover_v2_status_block()["cloud"]
    assert compact is not None
    assert compact[PHASE_CLOUD_MEASURE]["geometry_locked"] is True
    assert compact[PHASE_CLOUD_MEASURE]["overall_passed"] is False

    # Surface 3: the envelope.
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    status = {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2host.crossover_v2_status_block(),
    }
    envelope = build_crossover_envelope_v2(status)
    assert envelope["cloud"] is not None
    assert envelope["cloud"][PHASE_CLOUD_MEASURE]["geometry_locked"] is True

    # Surface 4 (named "all three" in the review, the doctor makes four):
    # the doctor no longer reports "no cloud-measurement session recorded".
    monkeypatch.setattr(v2host, "load_v2_state", lambda: state)
    r = check_crossover_v2_cloud_pipeline()
    assert "no cloud-measurement session" not in r.detail
    assert f"{PHASE_CLOUD_MEASURE}: spec=fail" in r.detail


def test_a_session_with_its_own_group_phase_overwrites_stale_prior_cloud():
    """N5 review finding (2026-07-27): the B1 fix's guard is "carry ``cloud``
    forward ONLY when THIS conductor's own session has no group phase" — the
    inverse must also hold, and nothing asserted it before this test (a
    regression to an unconditional carry-forward would have gone green).

    A conductor whose OWN session DOES include a group phase (a fresh,
    full — not verify-only — session that has started walking a cloud but
    has not closed any group of its OWN yet) must report ``cloud`` as
    honestly ``None`` for THIS session, never silently inheriting a stale
    verdict from whatever the previous session left behind.
    """
    v2host.save_v2_state({
        "session_id": "cap_stale_prior_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-stale"},
        "applied": True,
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked"},
                "positions": [
                    {"position_id": "cloud_measure_09", "index": 9, "attempt": 9}
                ],
                "pipeline": {"available": True, "spec": {"overall_passed": False}},
            },
        },
    })

    # A NEW full session whose own index_phase_map includes a cloud group
    # phase — mirrors the B1 test's verify-only conductor, but with
    # PHASE_CLOUD_MEASURE instead of PHASE_VERIFY, so this session's
    # session_phases DOES overlap GROUP_PHASES. It has not walked far enough
    # to close that group yet.
    conductor = CrossoverV2Conductor(
        session_id="cap_fresh_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(),
        applied=False,
        index_phase_map={1: PHASE_CLOUD_MEASURE},
    )
    v2host.persist_conductor_state(conductor, failure_code=None, evidence=None)

    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_fresh_session"
    # Honestly None -- "this session has not closed a group yet" -- never
    # the previous session's stale verdict.
    assert state["cloud"] is None
    assert v2host.crossover_v2_status_block()["cloud"] is None


def _seeded_session_with_a_banked_finding(copy: str) -> None:
    """A completed measuring session whose fit banked one household finding."""
    v2host.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "evidence": {
            "bundle_session_id": "bundle-stage-1",
            v2host.FINDING_HOUSEHOLD_REFS_KEY: [
                {"household_copy": copy, "at": time.time()},
            ],
        },
    })


def _rearm_conductor(session_id: str, *, index_phase_map: dict) -> Any:
    return CrossoverV2Conductor(
        session_id=session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map=index_phase_map,
    )


def test_stage_2_keeps_the_measuring_sessions_fc_recommendation():
    """R17, on the SAME predicate and for the same reason as the finding below.

    The Fc recommendation is produced at the lateral walk's close, which only a
    MEASURING session runs. Stage 2 is a different session whose conductor has
    no ``fc_selection`` at all, so without the carry-forward it persists
    ``None`` over the recommendation — the household reads "your crossover
    could be 1750 Hz" while deciding, and then nothing once the tuning is
    applied. That is the half where they would act on it.

    The converse is asserted too: a session that DOES measure writes its own
    value, ``None`` included, so a fresh measurement clears a superseded
    recommendation rather than replaying it.
    """
    recommendation = {
        "verdict": "recommend_alternative", "configured_hz": 2000.0,
        "recommended_hz": 1750.0, "margin_db": 1.4, "evaluated": 6,
        "planned": 6, "limits": {}, "refusals": [], "scores": [],
    }
    v2host.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "fc_selection": recommendation,
    })

    v2host.persist_conductor_state(
        _rearm_conductor("cap_rearm_session", index_phase_map={1: PHASE_VERIFY}),
        failure_code=None,
    )
    assert v2host.load_v2_state()["fc_selection"] == recommendation

    # …and a session that measures owns the answer, including "none".
    v2host.persist_conductor_state(
        _rearm_conductor(
            "cap_fresh_measure",
            index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE},
        ),
        failure_code=None,
    )
    assert v2host.load_v2_state()["fc_selection"] is None


def test_stage_2_keeps_the_measuring_sessions_banked_finding(monkeypatch):
    """CC1: the DONE screen is in a different session from the one that banked.

    ``prepare_v2_verify`` opens a NEW relay session AND a NEW evidence bundle,
    so "read this session's bundle" cannot reach the finding the MEASURING
    session banked — the household would be told the speaker is tuned with the
    disclosure silently dropped somewhere between deciding and being told.
    The projection therefore carries forward on its own predicate: a session
    that never runs MEASURE never had the chance to bank one, so its silence is
    a timestamp rather than a verdict.

    Walks the real seam (mirroring the ``cloud`` B1 test above): seeded durable
    state → the REAL re-arm conductor → the REAL ``persist_conductor_state`` →
    the state, `/state`'s projection, and the done screen.
    """
    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2

    copy = "Two measurements of how this speaker's ranges balance disagreed."
    _seeded_session_with_a_banked_finding(copy)

    v2host.persist_conductor_state(
        _rearm_conductor("cap_rearm_session", index_phase_map={1: PHASE_VERIFY}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-stage-2"},
    )

    # Surface 1: the durable state — carried across the bundle hop.
    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["evidence"]["bundle_session_id"] == "bundle-stage-2"
    assert state["evidence"][v2host.FINDING_HOUSEHOLD_REFS_KEY][0][
        "household_copy"
    ] == copy

    # Surface 2: /state's projection.
    assert [
        row["household_copy"]
        for row in v2host.crossover_v2_status_block()["findings"]
    ] == [copy]

    # Surface 3: the screen the household actually reads.
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            **v2host.crossover_v2_status_block(),
            "phase": PHASE_DONE,
            "verify": {"outcome": "pass"},
        },
    })
    assert [f["text"] for f in env["findings"]] == [copy]


def test_a_fresh_measurement_that_banks_nothing_clears_the_old_finding():
    """The converse, and the reason the predicate is MEASURE rather than an
    unconditional carry: a new measuring session writes its own projection —
    empty included — so a speaker that has since been re-measured cleanly never
    replays a previous session's finding as if this measurement had found it.
    """
    _seeded_session_with_a_banked_finding("An old finding nobody re-measured.")

    # A fresh full session: its own session_phases include MEASURE, so it owns
    # the answer to "what did this measurement learn" — and it learned nothing.
    v2host.persist_conductor_state(
        _rearm_conductor("cap_fresh_session", index_phase_map={1: PHASE_MEASURE}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-fresh"},
    )

    state = v2host.load_v2_state()
    assert v2host.FINDING_HOUSEHOLD_REFS_KEY not in (state["evidence"] or {})
    assert v2host.crossover_v2_status_block()["findings"] == []


# --- G1's ripple reservation across the stage-2 bundle hop (#2087) ------------


def _seeded_session_with_a_ripple_reservation() -> dict:
    """A completed measuring session whose accepted MEASURE banked one."""
    reservation = {"predicted_ripple_db": 15.244, "threshold_db": 15.0}
    v2host.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "measure": {"ripple_reservation": dict(reservation)},
        "evidence": {"bundle_session_id": "bundle-stage-1"},
    })
    return reservation


def test_stage_2_keeps_the_measuring_sessions_ripple_reservation(monkeypatch):
    """The reservation survives the stage-2 session hop to the DONE screen.

    Same shape and same hazard as the banked-finding test above: stage 2 runs
    under a NEW conductor that never ran MEASURE, so its own persist writes
    ``None`` over the reservation. Without the carry-forward the household
    would read the caveat on the screen where they DECIDE and then not on the
    screen that tells them the speaker is tuned — the worse half to lose,
    because that screen is the one that otherwise says only "Verified."

    Walks the real seam: seeded durable state -> the REAL re-arm conductor ->
    the REAL ``persist_conductor_state`` -> state, ``/state``, and the screen.
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        RIPPLE_RESERVATION_COPY,
        build_crossover_envelope_v2,
    )

    reservation = _seeded_session_with_a_ripple_reservation()

    v2host.persist_conductor_state(
        _rearm_conductor("cap_rearm_session", index_phase_map={1: PHASE_VERIFY}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-stage-2"},
    )

    # Surface 1: the durable state — carried across the bundle hop.
    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["measure"]["ripple_reservation"] == reservation

    # Surface 2: /state's projection.
    assert (
        v2host.crossover_v2_status_block()["measure"]["ripple_reservation"]
        == reservation
    )

    # Surface 3: the screen the household actually reads.
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            **v2host.crossover_v2_status_block(),
            "phase": PHASE_DONE,
            "verify": {"outcome": "pass"},
        },
    })
    assert RIPPLE_RESERVATION_COPY in [n["text"] for n in env["nudges"]]
    assert (
        "predicted ripple 15.24 dB, above the 15.0 dB disclosure threshold"
        in env["expert_details"]
    )


def test_a_fresh_measurement_clears_a_previous_sessions_ripple_reservation():
    """The converse, and the reason the predicate is MEASURE rather than an
    unconditional carry: a new measuring session owns the answer to "how good
    was this measurement", so a clean retake must not replay a caveat about a
    capture the household already replaced.
    """
    _seeded_session_with_a_ripple_reservation()

    v2host.persist_conductor_state(
        _rearm_conductor("cap_fresh_session", index_phase_map={1: PHASE_MEASURE}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-fresh"},
    )

    assert v2host.load_v2_state()["measure"] is None
    assert v2host.crossover_v2_status_block()["measure"] is None


# --- the projection contract, pinned AT the projection layer ------------------
#
# Gate finding SF-1 (adversarial review of #1982): `_household_findings_status`
# docstrings its whole contract — "a row without usable copy is DROPPED … an
# unusable `at` becomes None. Fabricating neither a sentence nor a date" — and
# nothing asserted it HERE. The gate proved the gap by weakening the copy check
# to `str(row.get("household_copy") or "")`, which renders a fabricated "42" on
# the household done screen end to end with the whole suite green: every
# screen-level test hands the envelope well-formed rows, so none of them can
# see a projection that coerces. These pin the layer that actually decides.


def _findings_state(rows: Any) -> None:
    """Durable state whose projection is exactly ``rows``."""
    v2host.save_v2_state({
        "session_id": "cap_projection",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": True,
        "evidence": {
            "bundle_session_id": "bundle-1",
            v2host.FINDING_HOUSEHOLD_REFS_KEY: rows,
        },
    })


@pytest.mark.parametrize("copy", [
    42,                     # the gate's own mutation subject — `str(42)` = "42"
    42.5,
    True,                   # bool is an int; `str(True)` = "True"
    None,                   # `str(None or "")` = "" — falsy, but still not text
    ["a sentence"],
    {"text": "a sentence"},
    "",
    "   ",
    "\n\t ",
])
def test_a_row_without_a_real_sentence_is_dropped_never_coerced(copy):
    """**A finding is prose or it is nothing.** Never `str()`-ed into existence.

    The failure this forbids is not cosmetic: a coerced row puts a fabricated
    sentence — "42", "True", "None" — into the one register this program
    promises is household-readable, on the screen that tells someone their
    speaker is tuned. Dropping is the honest answer to a row this build cannot
    read, exactly as `read_finding_set` returning None is the honest answer to
    a bundle that never banked one.
    """
    _findings_state([{"household_copy": copy, "at": time.time()}])
    assert v2host.crossover_v2_status_block()["findings"] == []


def test_a_good_row_survives_beside_every_unusable_one():
    """Guards the guard: the drops above are a FILTER, not this layer refusing
    to project at all. A test suite where every projection came back empty
    would pass the assertions above while shipping nothing."""
    _findings_state([
        {"household_copy": 42, "at": time.time()},
        {"household_copy": "A real one.", "at": 1_700_000_000.0},
        "not even an object",
        {"household_copy": "", "at": time.time()},
    ])
    assert v2host.crossover_v2_status_block()["findings"] == [
        {"household_copy": "A real one.", "at": 1_700_000_000.0},
    ]


@pytest.mark.parametrize("at", [
    None,
    "2026-07-29T10:00:00Z",   # an ISO string is not this file's clock
    True,                     # bool is an int, and it is not a timestamp
    [1_700_000_000.0],
    float("nan"),
    float("inf"),
    float("-inf"),
    10 ** 400,                # nit 1: `float()` RAISES OverflowError here
])
def test_an_unusable_clock_becomes_none_and_never_takes_the_row_with_it(at):
    """**The date is dropped; the sentence is not.** An unreadable ``at`` means
    "we cannot say when", which the envelope renders as "From your measurement
    earlier: …" — a real disclosure with no date CLAIM. Losing the whole finding
    over a bad byte in its timestamp would trade a missing date for a missing
    diagnosis.

    ``10 ** 400`` is the nit-1 case and it is reachable through the file, not
    theoretical: JSON integers are unbounded, `json` round-trips one happily,
    and `float()` on it RAISES `OverflowError` rather than returning `inf` — on
    the wizard's 1.5 s poll path, where an escape is a 500 on a plain page load.
    """
    _findings_state([{"household_copy": "A real one.", "at": at}])
    assert v2host.crossover_v2_status_block()["findings"] == [
        {"household_copy": "A real one.", "at": None},
    ]


def test_the_projection_reads_only_its_two_fields():
    """A durable row written by a later build — one that persists the mechanism
    beside the copy — must not leak that field onto `/state`. The reader NAMES
    what it takes rather than passing a row through, so a field added upstream
    cannot publish itself here."""
    _findings_state([{
        "household_copy": "A real one.",
        "at": 1_700_000_000.0,
        "mechanism": "M7",
        "evidence": {"disagreement_db": 3.2307},
    }])
    assert v2host.crossover_v2_status_block()["findings"] == [
        {"household_copy": "A real one.", "at": 1_700_000_000.0},
    ]


@pytest.mark.parametrize("rows", [None, {}, "findings", 7, [None, 5, "x"]])
def test_a_malformed_projection_block_reads_as_no_findings(rows):
    """A whole projection key that is not a list of objects is "nothing banked",
    never a crash on the poll path."""
    _findings_state(rows)
    assert v2host.crossover_v2_status_block()["findings"] == []


def test_a_corrupt_session_phases_list_never_reads_as_done():
    """S5: ``session_phases`` filters to the empty tuple on garbage, and a
    zero-length walk falls through to PHASE_DONE — i.e. a garbled state file
    would tell a household "Your speaker is tuned". Fail toward the fallback
    instead."""
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "session_phases": ["nonsense", "also-not-a-phase"],
        "applied": False,
    })
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_MEASURE

    # A partially-recognisable list keeps only what it can name — and that IS
    # enough to walk, so it is used rather than discarded.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": ["nonsense", PHASE_VERIFY],
        "applied": True,
    })
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_VERIFY


# --- the stage-1 PHASE_DONE collision (two-stage commission PR-T2) ------------


def test_a_measure_only_session_resolves_to_review_never_done():
    """**The work order's premise 6, and PR-T2's first pin.**

    ``_phase_from_state`` walks the recorded ``session_phases`` and returns
    PHASE_DONE once each is accepted. Its one special case — VERIFY unaccepted
    with MEASURE accepted and not applied ⇒ PHASE_APPLYING — cannot fire when
    VERIFY is not in the recorded phases at all. So a stage-1 session (CHECK,
    MEASURE, CLOUD_MEASURE, no VERIFY) fell straight through to PHASE_DONE:
    the RESULT screen, whose copy is "Your speaker is tuned", over a speaker
    that had been measured and never touched. A direct collision, not a
    theoretical one — and the acceptance criterion is explicit that "a stage-1
    session never renders 'your speaker is tuned'".
    """
    from jasper.active_speaker.crossover_v2_flow import PHASE_REVIEW

    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "applied": False,
    })
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_REVIEW


def test_an_applied_measure_only_session_resolves_to_verify_not_review_or_done():
    """RE-DERIVED from PR-T2's ``…_still_resolves_to_done`` (work order D2).

    T2 pinned that ``applied`` wins over the review branch, which is still
    true and still the point: re-offering "apply this?" over a speaker that
    already has it would be the mirror of the bug T2 fixed. What T2 could not
    yet express is where an applied measure-only session goes INSTEAD, because
    stage 2 did not exist — so it pinned ``done``, the only other terminal.

    T3 makes that answer wrong: stage 1 measured, the household applied from
    the review screen, and the post-apply check has not been opened yet. "Your
    speaker is tuned" over an unverified correction is exactly the class of
    claim this work order exists to remove. The honest resolution is
    PHASE_VERIFY, whose screen carries the action that opens stage 2.
    """
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "applied": True,
    })
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_VERIFY
    # …and the review interlude is NOT re-offered.
    assert v2host.crossover_v2_status_block()["phase"] != "review"

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2host.crossover_v2_status_block(),
    })
    assert env["screen"] == "verify"
    # The stage-2 entry point, tier-matched by the durable state's own tier.
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/verify"
    assert env["next_action"]["body"] == {"stage": "post_apply"}


def test_a_session_that_verified_still_resolves_to_done():
    """The review branch keys on a session that never intended to VERIFY, so
    every shape that DID keeps its shipped terminal — a full pre-cloud session
    and a verify-only re-arm alike. Without this the fix would silently move
    the RESULT screen for the flows that already work."""
    for phases in (
        [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        [PHASE_VERIFY],
        [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE, PHASE_VERIFY,
         PHASE_CLOUD_VERIFY],
    ):
        v2host.save_v2_state({
            "session_id": "cap_x",
            "accepted_phases": list(phases),
            "session_phases": list(phases),
            "applied": True,
        })
        assert v2host.crossover_v2_status_block()["phase"] == PHASE_DONE, phases


def test_a_corrupt_state_cannot_reach_the_review_screen_either():
    """The corrupt-state fallback the walk already documents must keep working
    — and must not become a NEW way to reach the review screen.

    A garbled ``session_phases`` filters to the empty tuple and walks
    PRE_CLOUD_CAPTURE_PHASES, which DOES contain VERIFY, so it can never
    satisfy the measure-only test on the strength of an unreadable state file.
    It resolves through the loop to its first unaccepted phase, exactly as
    before this change.
    """
    from jasper.active_speaker.crossover_v2_flow import PHASE_REVIEW

    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": ["nonsense", "also-not-a-phase"],
        "applied": False,
    })
    phase = v2host.crossover_v2_status_block()["phase"]
    assert phase != PHASE_REVIEW
    # MEASURE is accepted and VERIFY is not, so the shipped special case owns
    # this state — the honest "the apply is in flight" answer, not a terminal.
    assert phase == PHASE_APPLYING


# --- the stage-2 openability preflight (D3, render-time half) ----------------


def _preflight_status(phase, **v2):
    # A candidate by default: the preflight also gates on one (PR-T3), because
    # with nothing to apply there is nothing to preflight, and every REAL
    # review screen that renders an Apply control has one. A fixture without
    # it would be testing the cost gate, not the predicate — the tests that
    # ARE about the cost gate pass ``candidate=None`` explicitly.
    v2.setdefault("candidate", {"fingerprint": "fp-preflight"})
    return {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {"phase": phase, **v2},
    }


def test_the_preflight_runs_only_on_the_review_screen():
    """It is the review screen's own honesty layer, and it is not free: one
    call is several JSON loads, a profile fingerprint, and a preset compile,
    and ``ensure_crossover_preview_ready()`` can WRITE a regenerated preview.
    No other screen may pay that, so the phase gate is a contract, not an
    optimisation — pinned by refusing to let a non-review phase call the
    predicate at all. (``closing`` is in the list for PR-T3's own reason: it
    is a POLLED screen — the relay is still live — so the predicate running
    there would be the 1.5 s write loop the cost paragraph names.)"""
    calls = []

    def _boom(status):
        calls.append(status)
        raise AssertionError("the preflight must not run off the review screen")

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _boom
    try:
        for phase in (PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE,
                      PHASE_VERIFY, "applying", "closing", "done"):
            status = _preflight_status(phase)
            v2host.attach_stage2_preflight(status)
            assert v2host.STAGE2_PREFLIGHT_KEY not in status["crossover_v2"]
        assert calls == []
    finally:
        v2host.resolve_conductor_context = original


def test_a_refused_preflight_carries_the_predicates_own_sentence(caplog):
    """The refusal messages already name what to finish first, so they are
    passed through verbatim rather than re-phrased here — and the refusal gets
    a named log line, because a household-visible dead end nobody can grep for
    is not a disclosure (AGENTS.md's no-silent-failure rule)."""
    from jasper.active_speaker.crossover_v2_flow import PHASE_REVIEW

    def _refuse(status):
        raise v2host.CrossoverV2Refused(
            "protected speaker setup is not ready; finish it before measuring",
        )

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _refuse
    try:
        status = _preflight_status(PHASE_REVIEW)
        with caplog.at_level(logging.WARNING):
            v2host.attach_stage2_preflight(status)
    finally:
        v2host.resolve_conductor_context = original

    preflight = status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]
    assert preflight["ok"] is False
    assert preflight["message"] == (
        "protected speaker setup is not ready; finish it before measuring"
    )
    assert "event=correction.crossover_v2_stage2_preflight_refused" in caplog.text


def test_a_coded_refusal_carries_its_registrys_own_resolution_control():
    """#1820's precedent: a refusal that knows the exact control which clears
    it declares that control, from the SAME registry entry the hard-stop screen
    reads — so the review screen's message and its button can never disagree
    about what the household should do next."""
    from jasper.active_speaker.crossover_v2_flow import PHASE_REVIEW

    def _refuse(status):
        raise v2host.CrossoverV2Refused(
            "safety limits are not confirmed",
            code="program_profile_not_confirmed",
        )

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _refuse
    try:
        status = _preflight_status(PHASE_REVIEW)
        v2host.attach_stage2_preflight(status)
    finally:
        v2host.resolve_conductor_context = original

    action = status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]["next_action"]
    assert action and action["id"] == "confirm_safety_limits"


def test_an_unexpected_preflight_failure_fails_closed(caplog):
    """"We could not check" and "we checked and it is fine" must never render
    as the same screen. An unexpected exception is not permission — it disables
    Apply on its own honest sentence, because the end state being prevented
    (applied, then stage 2 refuses at open, box corrected and ungraded) is
    identical whether the predicate refused or simply could not run."""
    from jasper.active_speaker.crossover_v2_flow import PHASE_REVIEW

    def _explode(status):
        raise OSError("the topology file is unreadable")

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _explode
    try:
        status = _preflight_status(PHASE_REVIEW)
        with caplog.at_level(logging.WARNING):
            v2host.attach_stage2_preflight(status)
    finally:
        v2host.resolve_conductor_context = original

    preflight = status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]
    assert preflight["ok"] is False
    assert "could not check" in preflight["message"]
    assert "event=correction.crossover_v2_stage2_preflight_refused" in caplog.text


def test_the_held_window_is_not_the_review_screen(monkeypatch):
    """**PR-T3 gate blocker B2.** Accepting the final cloud position marks
    every stage-1 phase accepted, so the phase resolution fires the INSTANT
    that capture lands — while the runner is parked in D1's held-set window,
    the relay is live, and the household is holding a phone at the confirm
    screen. That used to resolve to ``review``, whose no-candidate copy told
    them "JTS measured your speaker but has no correction to propose — measure
    again to try afresh" and offered a destructive "Measure again" beside it,
    over a measurement that was still in progress.

    Driven through the REAL conductor and the REAL persist path, reading the
    durable state exactly as the wizard does.
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    conductor = _conductor(backend, session, phone, published=[])
    attempt = 0
    for index in range(1, STAGE1_LAST_INDEX + 1):
        attempt += 1
        conductor.consume_capture(index, attempt, CaptureResult(wav=b"w"))
        v2host.persist_conductor_state(conductor, failure_code=None)

    # HELD: walked, unconfirmed, no candidate.
    assert conductor.cloud_measure_group_awaiting_confirm() is True
    block = v2host.crossover_v2_status_block()
    assert block["phase"] == "closing"
    assert block["cloud_close"] == "awaiting_confirm"
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })
    assert env["screen"] == "closing"
    assert "confirm on the measurement page" in env["verdict_text"]
    assert env["busy"] is False
    # NOTHING destructive, and nothing to decide: the phone owns the only live
    # control (Retake / Continue), and every action this screen could offer
    # would throw away work in progress.
    assert env["next_action"] is None
    assert env["alternate_actions"] == []
    assert "no correction to propose" not in env["verdict_text"]

    # CONFIRMED, fit in flight: the host persists BEFORE running the close.
    conductor.note_group_close_started()
    v2host.persist_conductor_state(conductor, failure_code=None)
    block = v2host.crossover_v2_status_block()
    assert block["phase"] == "closing"
    assert block["cloud_close"] == "running"
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })
    assert env["screen"] == "closing"
    assert "working out your correction" in env["verdict_text"]
    assert env["busy"] is True  # the flow's one machine-paced wait
    assert env["next_action"] is None
    assert env["alternate_actions"] == []

    # CLOSED: the candidate exists, and only now is it the household's move.
    assert conductor.confirm_cloud_measure_group()["candidate_fingerprint"]
    v2host.persist_conductor_state(conductor, failure_code=None)
    block = v2host.crossover_v2_status_block()
    assert block["phase"] == "review"
    assert block["cloud_close"] == ""


def test_the_eager_fit_is_invisible_to_the_speaker_page(monkeypatch):
    """The eager-fit rider's UX ruling, pinned at the surface it is about.

    The owner asked for two things: start computing the moment the last
    position is accepted, and never show a stale-looking screen. The second one
    does NOT mean showing ``running`` while the eager fit works — during that
    window the household genuinely has something to do, and it is on their
    phone. ``running`` says "nobody has anything to do"; rendering it here
    would invite them to wait passively for a tap only they can make, which is
    the same dead air the rider exists to remove, just relocated. It would also
    have to be walked BACKWARDS on a retake, which the envelope has no
    vocabulary for.

    So the eager fit is invisible, and the rider delivers its promise the other
    way: by the time the household taps Continue and reaches a browser, the
    candidate is already built and the review screen renders immediately.
    ``running`` keeps its honest meaning — the household confirmed, and the
    close is in flight — for the now-rare case where the tap beats the fit.

    Driven through the REAL conductor, the REAL persist path, and the REAL
    envelope, reading durable state exactly as the wizard does.
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    conductor = _conductor(backend, session, phone, published=[])
    attempt = 0
    for index in range(1, STAGE1_LAST_INDEX + 1):
        attempt += 1
        conductor.consume_capture(index, attempt, CaptureResult(wav=b"w"))
        v2host.persist_conductor_state(conductor, failure_code=None)

    # The eager fit runs to completion…
    assert conductor.run_speculative_group_close() is True
    v2host.persist_conductor_state(conductor, failure_code=None)

    # …and the speaker page is byte-for-byte the held window it was before it:
    # still "confirm on the measurement page", still not busy, nothing to decide.
    block = v2host.crossover_v2_status_block()
    assert block["phase"] == "closing"
    assert block["cloud_close"] == "awaiting_confirm"
    assert block["candidate"] is None
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })
    assert env["screen"] == "closing"
    assert "confirm on the measurement page" in env["verdict_text"]
    assert "working out your correction" not in env["verdict_text"]
    assert env["busy"] is False
    assert env["next_action"] is None
    assert env["alternate_actions"] == []

    # And the household's confirm lands the review screen straight away.
    assert conductor.confirm_cloud_measure_group()["candidate_fingerprint"]
    v2host.persist_conductor_state(conductor, failure_code=None)
    assert v2host.crossover_v2_status_block()["phase"] == "review"


def test_the_runner_starts_the_eager_fit_on_the_group_close_accept(monkeypatch):
    """The host wiring, through the REAL ``run_capture_plan`` and a real phone.

    Pins WHERE the rider hooks in: the accept that leaves a walked, unconfirmed
    cloud — not the confirm, and not any other capture in a ten-position
    session. The trigger is run synchronously here so the assertion is about
    the wiring rather than about thread scheduling; production hands the same
    call to a daemon thread.
    """
    starts: list = []

    def _synchronous_start(conductor):
        starts.append(conductor.run_speculative_group_close())
        return None

    monkeypatch.setattr(v2host, "_start_speculative_group_close", _synchronous_start)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    published: list = []
    conductor = _conductor(backend, session, phone, published=published)
    builds: list = []
    real_build = conductor._build_candidate

    def _counting_build(analysis, cloud):
        builds.append(1)
        return real_build(analysis, cloud)

    conductor._build_candidate = _counting_build
    _run(_build_runner(conductor, VolumeRecorder()), client, session)

    # Fired exactly once across a whole ten-position walk, and it banked.
    assert starts == [True]
    # The fit ran ONCE — on the accept. The household's Continue committed it
    # rather than paying for a second one.
    assert len(builds) == 1
    # …and the session is otherwise exactly what T3 shipped: one proposal,
    # nothing applied.
    assert [kind for kind, _ in published] == ["check", "candidate"]
    assert v2host.load_v2_state().get("applied") is not True
    assert v2host.crossover_v2_status_block()["phase"] == "review"
    assert phone.completions_posted == 1


def test_a_session_that_ended_with_nothing_still_reaches_the_review_screen():
    """The state ``closing`` must NOT swallow the absence case.

    **What this actually covers, stated precisely:** durable state with every
    stage-1 phase accepted, no candidate, and NO ``cloud_close`` — which is
    state written before this field existed, or state whose ``cloud_close``
    was never populated. It is genuine and correctly handled: the review
    screen's absence copy plus "measure again" is the honest answer for a
    session that is not in progress. It is NOT a live-conductor path — no live
    conductor reaches all-phases-accepted with an empty ``cloud_close``,
    because accepting the group's last index stashes the combine and the
    property reads ``awaiting_confirm`` from that moment until a candidate
    exists. The pin is about the READER's fallback, not about a state the
    writer can produce."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "applied": False,
    })
    block = v2host.crossover_v2_status_block()
    assert block["phase"] == "review"
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })
    assert env["screen"] == "review"
    assert any(a["id"] == "review_remeasure" for a in env["alternate_actions"])


def test_the_preflight_does_not_run_without_a_candidate(caplog):
    """The cost gate (gate blocker B2's second half). The preflight is not
    cheap — six JSON reads, a profile fingerprint, a preset compile, and
    ``ensure_crossover_preview_ready`` which can WRITE two files and logs on
    every call — and it used to run on every 1.5 s poll of a live held window
    (~80 calls per hold, measured). With no candidate there is nothing to
    apply, so there is nothing to preflight."""
    calls: list = []
    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = lambda status: calls.append(status) or object()
    try:
        status = {
            "active": True,
            "crossover_v2": {"phase": "review", "candidate": None},
        }
        v2host.attach_stage2_preflight(status)
        assert calls == []
        assert v2host.STAGE2_PREFLIGHT_KEY not in status["crossover_v2"]
        # …and a candidate WITH a fingerprint still runs it, so the gate is a
        # condition rather than a switch that turned the feature off.
        status["crossover_v2"]["candidate"] = {"fingerprint": "abc"}
        v2host.attach_stage2_preflight(status)
        assert len(calls) == 1
        assert status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]["ok"] is True
    finally:
        v2host.resolve_conductor_context = original


def test_a_stage_1_map_has_no_verify_and_a_stage_2_map_does():
    """**The deliberate T3 tripwire, re-derived** (work order D1/D2).

    PR-T2 pinned ``test_every_shipped_index_phase_map_contains_verify``: every
    shipped ``index_phase_map`` contained a VERIFY, which was the load-bearing
    half of its claim that ``PHASE_REVIEW`` — and therefore
    ``attach_stage2_preflight`` — cost nothing, because the review branch keys
    on VERIFY's absence and nothing could produce it. T2 wrote that pin
    expecting T3 to break it, and named the break as the moment to re-read the
    preflight's cost paragraph.

    **The new invariant, stated explicitly:** a STAGE-1 (measuring) map
    contains no VERIFY entry, by design — its absence is what resolves a
    measure-only session to the review interlude instead of "your speaker is
    tuned". A STAGE-2 (post-apply) map always contains exactly one, at index 1,
    because a post-apply session that verified nothing would have nothing to
    grade. Both halves are checked across both tiers and all four corners of
    Full's validated (N, M) box, so neither rests on the default counts.

    **The cost paragraph, re-read.** ``attach_stage2_preflight`` is now
    genuinely reachable, once per envelope GET while the review interlude is
    on screen. T2 named the one shape it would not survive: a review screen
    rendering beside a permanently in-flight relay, which would turn
    ``ensure_crossover_preview_ready``'s writes into a 1.5 s loop. T3 owns
    re-checking that, and it holds — the review screen is reached only AFTER
    stage 1's session has ended (its runner returns, the relay is purged, and
    the wizard's poll stops at ``relayIsActive(env.relay)``), and the interlude
    itself starts no session. The calls are bounded to the seconds a
    just-closed relay spends winding down, exactly as T2 predicted.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        TIER_EXPRESS,
        TIER_FULL,
        build_v2_cloud_index_phase_map,
        build_v2_verify_index_phase_map,
        resolve_plan_shape,
    )

    for tier in (TIER_FULL, TIER_EXPRESS):
        stage1 = build_v2_cloud_index_phase_map(tier=tier)
        assert PHASE_VERIFY not in stage1.values(), tier
        assert PHASE_CLOUD_VERIFY not in stage1.values(), tier
        stage2 = build_v2_verify_index_phase_map(
            plan_shape=resolve_plan_shape(tier)
        )
        assert stage2[1] == PHASE_VERIFY, tier
        assert sum(1 for p in stage2.values() if p == PHASE_VERIFY) == 1, tier
    for n, m in ((6, 5), (12, 5), (6, 12), (12, 12), (9, 6)):
        shape = resolve_plan_shape(
            cloud_measure_positions=n, cloud_verify_positions=m,
        )
        stage1 = build_v2_cloud_index_phase_map(plan_shape=shape)
        assert PHASE_VERIFY not in stage1.values(), (n, m)
        assert PHASE_CLOUD_VERIFY not in stage1.values(), (n, m)
        stage2 = build_v2_verify_index_phase_map(plan_shape=shape)
        assert stage2[1] == PHASE_VERIFY, (n, m)
        assert sum(1 for p in stage2.values() if p == PHASE_VERIFY) == 1, (n, m)
    # The recovery re-verify keeps its shipped one-entry map.
    assert build_v2_verify_index_phase_map() == {1: PHASE_VERIFY}


def test_the_envelope_route_actually_runs_the_preflight():
    """The wiring, pinned at its one call site.

    The preflight fails CLOSED, so dropping this call does not break loudly —
    it silently disables Apply on every review screen forever, which looks like
    a product bug rather than a missing line. ``handle_envelope`` is the only
    path that serves this envelope to the wizard, so the call belongs there and
    a source read is enough to prove it has not been lost in a refactor (same
    shape as ``test_both_session_preparers_rearm_the_walked_away_volume_ceiling``
    above, and for the same reason).
    """
    import inspect

    from jasper.web import correction_crossover_flow

    source = inspect.getsource(correction_crossover_flow.handle_envelope)
    assert "attach_stage2_preflight(status)" in source
    # ...and BEFORE the envelope is built, or it would stamp a status nobody
    # reads.
    assert source.index("attach_stage2_preflight(status)") < source.index(
        "build_crossover_envelope_logged(status)"
    )


def test_a_resolvable_context_is_the_only_thing_that_enables_apply():
    """The positive case, end to end through the envelope: a preflight that
    resolves is what turns the Apply control on, and nothing else does."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.active_speaker.crossover_v2_flow import PHASE_REVIEW

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = lambda status: object()
    try:
        status = _preflight_status(
            PHASE_REVIEW,
            candidate={"fingerprint": "fp-1", "trims_db": {"woofer": -2.0}},
            prediction={
                "curve": {"freqs_hz": [100.0], "magnitude_db": [80.0]},
                "spec_bands": [], "overall_passed": True, "reference_db": 80.0,
            },
        )
        v2host.attach_stage2_preflight(status)
        assert status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]["ok"] is True
        env = build_crossover_envelope_v2(status)
        assert env["screen"] == "review"
        assert env["next_action"]["enabled"] is True
    finally:
        v2host.resolve_conductor_context = original


def test_both_session_preparers_rearm_the_walked_away_volume_ceiling():
    """S4: the ceiling is only correct because both preparers re-arm it from
    their OWN plan — the volume plan is process-global, so a dropped call in
    either one silently leaves the other's ceiling in force. Nothing enforced
    that; this does, by reading the call out of each preparer's source.
    """
    import inspect

    for fn in (v2host.prepare_v2_session, v2host.prepare_v2_verify):
        source = inspect.getsource(fn)
        assert "set_wall_clock_ceiling_s(" in source, fn.__name__
        assert "session_wall_clock_ceiling_s(spec.capture_plan)" in source, (
            f"{fn.__name__} must size the ceiling from the plan it emits"
        )
    # And the two plans really do want different ceilings, which is the whole
    # reason the re-arm cannot be done once at import.
    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_capture_plan,
        build_v2_verify_capture_plan,
        session_wall_clock_ceiling_s,
    )
    from jasper.active_speaker.session_volume_plan import DEFAULT_WALL_CLOCK_CEILING_S

    assert session_wall_clock_ceiling_s(
        build_v2_capture_plan(_roles(), FC_HZ)
    ) > session_wall_clock_ceiling_s(
        build_v2_verify_capture_plan(FC_HZ)
    ) == DEFAULT_WALL_CLOCK_CEILING_S


# --- the apply's stage-2 openability preflight (work order D3, PR-T3 half) ---


def _ready_to_apply(monkeypatch, tmp_path):
    """The real apply environment plus durable state holding its candidate.

    Same seeding the neighbouring apply tests use, so these exercise the REAL
    ``handle_v2_apply`` up to (and, when the preflight refuses, not past) the
    transaction.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_preflight",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })
    return candidate


def test_apply_refuses_when_stage_2_could_not_be_opened(monkeypatch, tmp_path):
    """**The pin the work order names for this rung.** A speaker that cannot
    open its post-apply check must not be corrected and left ungraded — the
    applied-and-ungraded end state this whole work order exists to eliminate.

    T2 shipped the render-time half (Apply is disabled and the refusal renders
    verbatim). This is the server-side half, and it is NOT redundant with it: a
    disabled control is not a security boundary — a stale page, a second tab,
    or a direct POST all reach this endpoint.
    """
    candidate = _ready_to_apply(monkeypatch, tmp_path)

    def _refuse(_status):
        raise v2host.CrossoverV2Refused(
            "confirm the driver safety profile before measuring"
        )

    monkeypatch.setattr(v2host, "resolve_conductor_context", _refuse)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_apply(
            {
                "expected_candidate_fingerprint": candidate.fingerprint,
                "candidate": candidate.to_dict(),
            },
            _bg_run_async,
            _FakeApplyCam,
            status={},
        )

    # The predicate's OWN sentence reaches the household, not a generic one.
    assert "confirm the driver safety profile" in str(excinfo.value)
    assert "was not run" in str(excinfo.value)
    # The DSP was never touched: nothing durable claims an apply happened, and
    # no pre-apply profile was stashed (which only a real commit produces).
    state = v2host.load_v2_state()
    assert state.get("applied") is not True
    assert state.get("pre_apply_profile") is None


def test_an_unexpected_preflight_failure_refuses_the_apply_too(
    monkeypatch, tmp_path,
):
    """Fail-closed in BOTH directions: "we could not check" and "we checked
    and it is fine" must never produce the same outcome on the one action that
    touches the speaker."""
    candidate = _ready_to_apply(monkeypatch, tmp_path)

    def _explode(_status):
        raise RuntimeError("the topology file is unreadable")

    monkeypatch.setattr(v2host, "resolve_conductor_context", _explode)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_apply(
            {
                "expected_candidate_fingerprint": candidate.fingerprint,
                "candidate": candidate.to_dict(),
            },
            _bg_run_async,
            _FakeApplyCam,
            status={},
        )
    assert "could not confirm" in str(excinfo.value)
    assert v2host.load_v2_state().get("applied") is not True


def test_the_preflight_runs_after_the_freshness_gates(monkeypatch):
    """Ordering: a STALE candidate gets its own specific refusal ("review the
    newest measurement"), not the preflight's. Getting this backwards would
    tell a household to go fix their safety profile when what they actually
    need is to re-read a newer measurement."""
    v2host.save_v2_state({
        "session_id": "cap_preflight",
        "candidate": {"fingerprint": "a-newer-fingerprint"},
        "applied": False,
    })
    monkeypatch.setattr(
        v2host, "resolve_conductor_context",
        lambda _status: (_ for _ in ()).throw(
            v2host.CrossoverV2Refused("safety profile")
        ),
    )

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_apply(
            {"expected_candidate_fingerprint": "the-reviewed-one"},
            _bg_run_async,
            _FakeApplyCam,
            status={},
        )
    assert "no longer current" in str(excinfo.value)


def test_the_apply_endpoint_cannot_skip_the_preflight():
    """``status`` is REQUIRED and keyword-only, so no caller can quietly drop
    it — and the dispatch really does supply one."""
    import inspect

    sig = inspect.signature(v2host.handle_v2_apply)
    status_param = sig.parameters["status"]
    assert status_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert status_param.default is inspect.Parameter.empty

    from jasper.web import correction_setup

    source = inspect.getsource(correction_setup._handle_crossover_v2_apply)
    assert "status=correction_crossover_backend.status_payload()" in source
    # …and the call site really is inside handle_v2_apply, before the commit.
    apply_source = inspect.getsource(v2host.handle_v2_apply)
    assert "_assert_stage_2_can_open(status)" in apply_source
    # rindex, not index: the first `apply_baseline_profile(` is the import.
    assert apply_source.index("_assert_stage_2_can_open(status)") < apply_source.rindex(
        "apply_baseline_profile("
    )


# --- stage 2's entry point (work order D2) -----------------------------------


def test_the_verify_endpoint_opens_the_tier_matched_stage_2_or_the_recovery():
    """ONE entry point, two shapes — generalized over the plan shape rather
    than forked into a second builder (work order D2).

    The tier comes from the durable state the MEASURING session wrote, so the
    household's choice at the tier chooser governs both stages.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        TIER_EXPRESS,
        TIER_FULL,
        build_v2_verify_capture_plan,
        resolve_plan_shape,
    )

    for tier, expected in ((TIER_FULL, 6), (TIER_EXPRESS, 1)):
        shape = v2host._verify_plan_shape({"stage": "post_apply"}, {"tier": tier})
        assert shape == resolve_plan_shape(tier)
        assert build_v2_verify_capture_plan(
            FC_HZ, plan_shape=shape,
        ).capture_target == expected

    # Absent / explicit "recovery" is the shipped 1-entry re-arm, which is what
    # a FAILED stage 2 offers — every pre-two-stage caller posts `{}`.
    for raw in ({}, {"stage": "recovery"}, None):
        assert v2host._verify_plan_shape(raw, {"tier": TIER_FULL}) is None
    assert build_v2_verify_capture_plan(FC_HZ).capture_target == 1


def test_an_unknown_verify_stage_is_refused_rather_than_guessed():
    """Same strictness ``normalize_tier`` applies to a tier: a caller asking
    for an instrument this build does not have fails loudly rather than
    silently measuring something else."""
    from jasper.active_speaker.crossover_v2_flow import TIER_FULL

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host._verify_plan_shape({"stage": "turbo"}, {"tier": TIER_FULL})
    assert "unknown verify stage" in str(excinfo.value)


def test_the_failed_screens_re_verify_still_asks_for_the_recovery():
    """The shipped ``verify_retry`` action posts no ``stage``, so a failed
    post-apply check still offers ONE cheap sweep rather than re-walking the
    whole post-apply cloud. Read off the envelope so a body change is visible
    here rather than on hardware."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": PHASE_VERIFY,
            "applied": True,
            # Stamped now: this is the screen a household is on, which is the
            # only state that renders the live verify_fail actions (#1942).
            "failure": {"code": "verify_out_of_tolerance", "at": time.time()},
        },
    })
    retry = env["next_action"]
    assert retry["endpoint"] == "/correction/crossover/v2/verify"
    assert "stage" not in (retry.get("body") or {})


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


def test_verify_rearm_persists_a_dated_pilot_transfer_reference():
    """Measurement-honesty gate G3's reference threads through
    persist_conductor_state's verify_priors exactly like gate_window_ms
    above — written (through the REAL production ``consume()`` closure,
    ``persist_conductor_state`` runs after every capture) once the
    conductor's own first usable VERIFY attempt sets it.

    It travels DATED (#1927), because the only thing a later re-arm may do
    with it is disclose it, and a disclosure has to say when."""
    backend = FakePlanRelayBackend()
    spec = build_v2_verify_session_spec(FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec)
    published: list = []

    freqs = np.linspace(100.0, 20000.0, 64)
    base = _conductor(
        backend, session, phone, published=published,
        analyses={
            "verify": lambda program: _verify_analysis(program, pilot_hi_dbfs=-20.0),
        },
    )
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
    volume = VolumeRecorder()
    _run(_build_runner(conductor, volume), client, session)

    assert conductor.current_phase == PHASE_DONE
    # transfer = level_hi_dbfs(-20.0) - programmed_hi_gain_db(-20.0) = 0.0.
    assert conductor.verify_pilot_transfer_reference["values"] == {"summed": 0.0}
    state = v2host.load_v2_state()
    reference = state["verify_priors"]["pilot_transfer_reference"]
    assert reference["values"] == {"summed": 0.0}
    assert reference["at"] > 0.0
    # The flat, undated key the rehydrating baseline used is gone from the
    # writer — nothing can read a previous session's numbers as a comparator.
    assert "pilot_transfer_baseline" not in state["verify_priors"]


def _rearm_conductor_for_persist(session_id: str, index_phase_map: dict, **kwargs):
    """A conductor of ``prepare_v2_verify``'s shape, seams stubbed — the same
    construction ``test_verify_rearm_does_not_blank_the_persisted_cloud_block``
    uses to exercise the REAL ``persist_conductor_state``."""
    return CrossoverV2Conductor(
        session_id=session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map=index_phase_map,
        **kwargs,
    )


def test_verify_rearm_keeps_the_prior_level_reference_across_its_own_writes():
    """#1927: the history the disclosure reads must survive the opening
    persist of a re-arm, which runs BEFORE any usable VERIFY attempt has set
    this session's own reference. Same carry-forward shape as ``tier`` and
    ``cloud`` — a re-arm runs under a brand-new relay session id, so a
    session-id guard would drop it on the first "Try again"."""
    reference = {"values": {"summed": -20.0}, "at": 1_700_000_000.0}
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify_priors": {"pilot_transfer_reference": reference},
    })
    conductor = _rearm_conductor_for_persist(
        "cap_rearm_session", {1: PHASE_VERIFY},
    )
    v2host.persist_conductor_state(conductor, failure_code=None)

    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["verify_priors"]["pilot_transfer_reference"] == reference


def test_seeding_a_rearm_from_durable_state_never_seeds_the_comparator():
    """The SEEDING path end to end, minus the relay: durable state carrying a
    previous session's reference → the value ``prepare_v2_verify`` passes as
    ``verify_pilot_transfer_prior`` → a fresh conductor. The comparator stays
    empty; only the history arrives (#1927)."""
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify_priors": {
            "pilot_transfer_reference": {
                "values": {"summed": -20.0}, "at": time.time() - 86400.0,
            },
        },
    })
    prior = v2host.pilot_transfer_prior_from_state(v2host.load_v2_state())
    assert prior["values"] == {"summed": -20.0}
    conductor = _rearm_conductor_for_persist(
        "cap_rearm_session", {1: PHASE_VERIFY},
        verify_pilot_transfer_prior=prior,
    )
    assert conductor._verify_pilot_baseline is None
    assert conductor.verify_pilot_transfer_reference is None
    # …and the preparer really does route it to that argument, never to a
    # baseline. Source-read for the same reason
    # ``test_both_session_preparers_rearm_the_walked_away_volume_ceiling``
    # uses one: driving ``_open`` needs a live relay.
    source = inspect.getsource(v2host.prepare_v2_verify)
    assert "pilot_transfer_prior_from_state(state)" in source
    assert "verify_pilot_transfer_prior=pilot_transfer_prior" in source
    assert "verify_pilot_transfer_baseline" not in source


def test_a_measuring_session_drops_the_prior_level_reference():
    """A pilot transfer is captured THROUGH the applied graph, so once a new
    candidate is measured the previous reference answers a different question.
    A measuring session drops it rather than letting the next stage-2 verify
    report a graph change as a level-reference move — the misattribution
    #1924 and #1927 both exist to stop."""
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify_priors": {
            "pilot_transfer_reference": {
                "values": {"summed": -20.0}, "at": 1_700_000_000.0,
            },
        },
    })
    conductor = _rearm_conductor_for_persist(
        "cap_measure_session", {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY},
    )
    v2host.persist_conductor_state(conductor, failure_code=None)

    state = v2host.load_v2_state()
    assert state["verify_priors"]["pilot_transfer_reference"] is None


# --- endpoint gates (recovery) ----------------------------------------


def test_prepare_refuses_when_volume_needs_recovery():
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {}, status={}, run_async=None, camilla_factory=None
        )
    assert "recover" in str(excinfo.value)


def test_prepare_refuses_an_unknown_tier_before_touching_anything(caplog):
    """Flow-simplification §3: the wizard posts the household's explicit tier.
    An id this build does not have must be refused BEFORE any relay
    registration or volume mutation, not silently measured as something else —
    so the gate runs ahead of every other one in the preparer.

    The evidence that THIS gate fired moved when #1833 stopped the raw flow
    text reaching the household: the refusal now carries the classifier's code
    and the journal carries the constraint. Both still separate it from the
    volume-recovery gate below it, which is uncoded and says "recover".
    """
    import logging

    from jasper.active_speaker.crossover_v2_flow import REASON_PROGRAM_UNPLAYABLE

    class _Ready:
        needs_recovery = False

    v2host.set_volume_plan_for_tests(_Ready())
    caplog.set_level(logging.WARNING, logger=v2host.__name__)
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {"tier": "turbo"}, status={}, run_async=None, camilla_factory=None
        )
    assert excinfo.value.code == REASON_PROGRAM_UNPLAYABLE
    assert "recover" not in str(excinfo.value)
    assert any(
        "unknown commission tier" in r.getMessage() and "turbo" in r.getMessage()
        for r in caplog.records
    )


def test_prepare_refuses_unrepresentable_confirmed_protection_before_bundle(
    monkeypatch,
):
    class _Ready:
        needs_recovery = False

    def _unrepresentable(*_args):
        raise ValueError("unsupported confirmed filter")

    from jasper.active_speaker import branch_chain

    v2host.set_volume_plan_for_tests(_Ready())
    monkeypatch.setattr(v2host, "reconcile_session_volume_for_new_session", lambda *_: None)
    monkeypatch.setattr(
        v2host, "resolve_conductor_context",
        lambda _status: SimpleNamespace(safety_profile={}, role_targets={}),
    )
    monkeypatch.setattr(branch_chain, "confirmed_protection_sections", _unrepresentable)
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda *_: pytest.fail("bundle opened before protection preflight"),
    )
    with pytest.raises(v2host.CrossoverV2Refused, match="confirmed driver protection"):
        v2host.prepare_v2_session({}, status={}, run_async=None, camilla_factory=None)


def test_the_session_preparer_threads_one_tier_into_the_spec_and_the_map():
    """§1.2's whole point: the emitted plan and the conductor's index→phase map
    come from ONE resolved shape, so a tier can never reach one and not the
    other. Read the preparer's own source rather than trusting the call site to
    stay wired — this is the desync the shape value exists to prevent.
    """
    import inspect

    source = inspect.getsource(v2host.prepare_v2_session)
    assert 'resolve_plan_shape(raw.get("tier")' in source
    assert "build_v2_session_spec(" in source and "plan_shape=plan_shape" in source
    # Stronger than the old literal: the preparer must READ the one owner of
    # this fact, so the chooser and the session cannot drift (#2098).
    assert "include_cloud_measure = STAGE1_INCLUDES_CLOUD_MEASURE" in source
    assert STAGE1_INCLUDES_CLOUD_MEASURE is False
    assert source.count("include_cloud_measure=include_cloud_measure") == 2
    assert "confirmed_protection_sections(" in source
    assert "protection_sections_by_role=protection_sections" in source
    assert "measurement_protection_sections_by_role=protection_sections" in source
    assert "tier=plan_shape.tier," in source


def test_the_tier_rides_the_durable_state_and_state_block():
    """§1.2: `/state` can tell WHICH instrument produced a result, and an
    unknown one reads as unknown rather than as "full" — the
    ``echo_band_provenance`` discipline (issue #1763)."""
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "tier": "express",
    })
    assert v2host.crossover_v2_status_block()["tier"] == "express"
    # State written before tiers existed says nothing, and nothing is invented.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
    })
    assert v2host.crossover_v2_status_block()["tier"] is None


# --- two-stage commission D4: the prediction on the wire ------------------


def _closed_cloud_conductor():
    """A real conductor walked to its cloud-measure close, so it carries a
    candidate, a full-resolution ``measure_predicted_sum``, and the spec report
    its accountability veto graded that sum with."""
    from tests.test_crossover_v2_conductor import (
        FakeSeams,
        _cloud_conductor,
        _eligible_measure_analysis,
        _walk_measure_cloud_to_close,
    )

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    conductor = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(conductor)
    return conductor


def test_the_persisted_prediction_verdict_is_the_veto_s_not_a_re_grade():
    """D4's "one grading instrument", at the persistence seam.

    The durable state carries the prediction TWICE in different resolutions —
    the curve at ``MAX_PERSISTED_SUM_POINTS`` (a drawing) and the verdict from
    the full-resolution tuple (the instrument). This pins that the stored
    verdict is the conductor's own, and that re-grading the stored curve would
    have produced something else, so the distinction is load-bearing rather
    than notional."""
    from jasper.active_speaker.crossover_v2_flow import spec_report_for_predicted_sum

    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)

    priors = v2host.load_v2_state()["verify_priors"]
    assert priors["predicted_spec"] == conductor.measure_predicted_spec_report
    assert priors["predicted_spec"] == spec_report_for_predicted_sum(
        conductor.measure_predicted_sum
    ).to_dict()

    # The curve that WAS persisted grades differently — which is exactly why
    # the report is persisted instead of being recomputed from it.
    stored_curve = priors["predicted_sum"]
    re_graded = spec_report_for_predicted_sum((
        np.asarray(stored_curve["freqs_hz"], dtype=float),
        np.asarray(stored_curve["magnitude_db"], dtype=float),
    )).to_dict()
    assert re_graded != priors["predicted_spec"]


def test_the_prediction_verdict_survives_a_verify_rearm_persist():
    """The carry-forward, pinned on the shape that has broken three times.

    A verify-only re-arm builds a FRESH conductor that never runs a fit, so
    every MEASURE-owned prior has to travel to it explicitly or the first
    "Try again" blanks it — the ``cloud`` B1 / ``pre_apply_profile`` W6.12 bug
    shape. The verdict rides the same route as ``gate_window_ms``, and this is
    what proves the route is wired at BOTH ends."""
    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)
    stored = v2host.load_v2_state()["verify_priors"]["predicted_spec"]
    assert stored is not None

    rearmed = CrossoverV2Conductor(
        session_id="cap_rearm",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=conductor._seams,
        index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        measure_predicted_sum=conductor.measure_predicted_sum,
        measure_predicted_spec_report=stored,
    )
    assert rearmed.measure_predicted_spec_report == stored
    v2host.persist_conductor_state(rearmed, failure_code=None)
    assert v2host.load_v2_state()["verify_priors"]["predicted_spec"] == stored


def test_the_prediction_reaches_the_status_block_with_its_verdict():
    """D4's projection: curve + stored verdict, beside the cloud blocks."""
    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)

    prediction = v2host.crossover_v2_status_block()["prediction"]
    stored = conductor.measure_predicted_spec_report
    assert prediction["overall_passed"] == stored["overall_passed"]
    assert prediction["reference_db"] == pytest.approx(stored["reference_db"])
    # The per-band vocabulary matches the compact cloud block's, key for key,
    # so the review screen can draw both curves in one tolerance corridor.
    assert [set(b) for b in prediction["spec_bands"]] == [
        {"f_lo_hz", "f_hi_hz", "passed", "max_deviation_db", "tolerance_db"}
    ] * len(stored["bands"])
    assert prediction["curve"]["freqs_hz"]


def test_the_predicted_curve_rides_the_existing_chart_decimation_owner():
    """D4: "the chart feed keeps one decimation owner".

    The predicted curve and the cloud curves are drawn in one frame, so they
    must be strided by the SAME function at the SAME ceiling — a second inline
    copy of the stride is how two curves in one chart end up at silently
    different densities. Pinned by handing both projections the identical raw
    curve and requiring identical output.

    **The ceiling is now a HARD one (gate finding on #1858, SF-1) — re-derived,
    not adjusted to match.** This used to read "the ceiling is a SOFT one":
    ``max(1, n // CAP)`` floor-division stride, so a length not a multiple of
    it overshot by up to one stride (1031 raw points strode by 4 and yielded
    258, not 256). That was tolerable only because every persisted length that
    ever reached this function historically overshot its OWN cap (both
    ``_decimate_sum``'s old raw stride and ``_decimate_curve_for_json``'s
    still land at/above 512-513 for a real capture). #1858's block-average fix
    to ``_decimate_sum`` undershoots its cap instead (a 32769-bin capture
    persists at 504, not 512-513) — landing the predicted curve's persisted
    length just below ``CAP * 2``, where the OLD floor-division stride
    computed ``step = 1`` (no reduction at all: 504 rendered, not ~252),
    silently doubling the prediction's density against the cloud curves in
    the same frame and breaking this function's own soft-ceiling promise.
    Fixed at this owner with ceiling division (``-(-n // CAP)``), which
    guarantees ``len(rendered) <= CAP`` unconditionally — re-derived here on
    the SAME 1031-point fixture: ``ceil(1031 / 256) = 5`` (not floor's 4), so
    1031 strode by 5 yields 207, not 258. Both curve families still ride the
    identical function, so the "one owner" pin is unmoved; only the stride
    arithmetic inside that one owner changed, verified by direct sweep (see
    ``test_realized_chart_lengths_stay_within_cap_for_both_curve_families``)
    over 1..5000 plus 2000 random larger lengths: max observed output was
    exactly 256, never more, for any input."""
    n = v2host.CHART_CURVE_MAX_JSON_POINTS * 4 + 7  # not a multiple of the cap
    freqs = [100.0 + i for i in range(n)]
    mags = [float(i % 5) for i in range(n)]
    raw = {"freqs_hz": freqs, "magnitude_db": mags}

    v2host.save_v2_state({
        "session_id": "cap_x",
        "cloud": {
            PHASE_CLOUD_MEASURE: {"pipeline": {"available": True, "curve": raw}},
        },
        "verify_priors": {"predicted_sum": raw},
    })
    block = v2host.crossover_v2_status_block()
    predicted = block["prediction"]["curve"]
    # THE pin: one owner, so identical input yields byte-identical output.
    assert predicted == block["cloud_chart"][PHASE_CLOUD_MEASURE]["curve"]
    assert len(predicted["freqs_hz"]) == len(predicted["magnitude_db"])
    # Genuinely decimated, to exactly the shared owner's (now ceiling-division)
    # stride -- re-derived: ceil(1031 / 256) = 5, not floor's 4.
    stride = -(-n // v2host.CHART_CURVE_MAX_JSON_POINTS)
    assert stride == 5
    assert len(predicted["freqs_hz"]) == len(range(0, n, stride))
    assert len(predicted["freqs_hz"]) == 207
    # The hard ceiling itself: never CAP + stride (the old soft promise),
    # always CAP outright.
    assert len(predicted["freqs_hz"]) <= v2host.CHART_CURVE_MAX_JSON_POINTS


def test_realized_chart_lengths_stay_within_cap_for_both_curve_families():
    """Gate finding on #1858 (SF-1): the constants-only drift guard
    (``test_cloud_curve_max_json_points_mirrors_the_verify_priors_
    decimation_cap`` in ``tests/test_crossover_v2_cloud_pipeline.py``) pins
    ``MAX_PERSISTED_SUM_POINTS == CLOUD_CURVE_MAX_JSON_POINTS`` (512 == 512),
    never the REALIZED wire lengths downstream of them — so it stayed green
    straight through the regression where the predicted curve rendered at
    ~2x the cloud curves' density in the same chart frame (504 points,
    undecimated, next to 257). This test drives both persist-time decimators
    (``_decimate_sum`` for the prediction, ``_decimate_curve_for_json`` for
    the cloud) through the SAME chart-time re-decimation
    (``_decimate_curve_for_chart``) at real FFT-bin grid sizes, and asserts
    what actually reaches the wire, not the constants that feed it.

    Two sizes, both realistic ``np.fft.rfftfreq`` outputs (the shape
    ``predicted_sum`` and the cloud's combined curve actually carry): the
    65536-point FFT window's 32769-bin grid (matches the size
    ``test_predicted_spec_report_is_graded_on_the_shared_analysis_grid``
    already uses as its own "real capture" fixture) and a second, smaller
    16384-point window's 8193-bin grid — so the bound is pinned as a
    property of the functions, not of one fixture that happens to clear it.
    """
    from jasper.active_speaker.crossover_v2_flow import _decimate_curve_for_json

    for n_fft in (1 << 16, 1 << 14):
        freqs = np.fft.rfftfreq(n_fft, 1.0 / 48000.0)
        mag_db = np.zeros(freqs.size)

        persisted_pred = v2host._decimate_sum((freqs, mag_db))
        rendered_pred = v2host._decimate_curve_for_chart(
            persisted_pred["freqs_hz"], persisted_pred["magnitude_db"],
        )
        persisted_cloud = _decimate_curve_for_json(freqs, mag_db)
        rendered_cloud = v2host._decimate_curve_for_chart(
            persisted_cloud["freqs_hz"], persisted_cloud["magnitude_db"],
        )

        # The hard ceiling itself, for BOTH curve families -- this is what
        # the constants-equality guard could never see.
        assert len(rendered_pred["freqs_hz"]) <= v2host.CHART_CURVE_MAX_JSON_POINTS
        assert len(rendered_cloud["freqs_hz"]) <= v2host.CHART_CURVE_MAX_JSON_POINTS

        # Same-frame density parity: the bug's own signature was an
        # UNBOUNDED mismatch (504 undecimated vs. 257, ~2x and growing with
        # input size, since floor-division gave the prediction NO reduction
        # at all). A generous 2x margin still catches that class outright
        # while tolerating the two decimators' differing raw-stride vs.
        # block-average characters (measured ~1.4-1.5x on these two grids).
        len_pred = len(rendered_pred["freqs_hz"])
        len_cloud = len(rendered_cloud["freqs_hz"])
        assert max(len_pred, len_cloud) <= 2 * min(len_pred, len_cloud), (
            n_fft, len_pred, len_cloud,
        )


def test_decimate_sum_tracks_smoothed_truth_not_the_aliased_stride():
    """Issue #1858: ``_decimate_sum`` must anti-alias before reducing point
    count, not stride-pick raw bins.

    The synthetic curve is a slow, genuine trend (what a persisted prior
    should track) plus a fast ripple whose ~10 Hz period is far shorter than
    the ~46.9 Hz output grid spacing (``24000 / MAX_PERSISTED_SUM_POINTS``)
    -- "ripple faster than the output grid" -- planted across the full
    sweep including the sub-500 Hz region the issue calls out. 500 Hz sits
    inside the old stride's fewer-than-3-samples-per-1/3-octave-band zone
    (below ~607 Hz; below ~202 Hz the stride spacing exceeds the band's own
    width outright, zero guaranteed samples), so a single stride-picked raw
    bin there was noise, not shape.

    Pinned against the regression it fixes, not just that new code runs: the
    naive floor-division stride this replaces is reproduced locally (it no
    longer exists in production after this fix) and demonstrably fails the
    same tolerance the fixed function meets.
    """
    n = 1 << 16
    fs = 48000.0
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    slow_true_db = 3.0 * np.sin(2.0 * np.pi * freqs / 400.0)
    fast_ripple_db = 2.0 * np.sin(2.0 * np.pi * freqs / 10.0)
    mag_db = slow_true_db + fast_ripple_db
    mag_db[0] = slow_true_db[0]  # avoid the f=0 edge

    decimated = v2host._decimate_sum((freqs, mag_db))
    out_freqs = np.asarray(decimated["freqs_hz"])
    out_mag = np.asarray(decimated["magnitude_db"])
    assert len(out_freqs) <= v2host.MAX_PERSISTED_SUM_POINTS
    assert len(out_freqs) < freqs.size  # genuinely decimated

    below_500 = out_freqs < 500.0
    assert below_500.sum() >= 5  # the region actually gets exercised
    truth_below_500 = 3.0 * np.sin(2.0 * np.pi * out_freqs[below_500] / 400.0)
    new_err = np.abs(out_mag[below_500] - truth_below_500)

    def _old_removed_stride_decimate(freqs, mags, cap):
        """The exact shape ``_decimate_sum`` used before #1858: a raw
        floor-division stride. No longer in production; reproduced here so
        the fix is pinned against the regression it replaces."""
        n = len(freqs)
        step = max(1, n // cap)
        return freqs[::step], mags[::step]

    old_freqs, old_mag = _old_removed_stride_decimate(
        freqs, mag_db, v2host.MAX_PERSISTED_SUM_POINTS,
    )
    old_below_500 = old_freqs < 500.0
    old_truth = 3.0 * np.sin(2.0 * np.pi * old_freqs[old_below_500] / 400.0)
    old_err = np.abs(old_mag[old_below_500] - old_truth)

    # The fix: honest tracking of the slow truth below 500 Hz, well inside
    # the ripple's own 2.0 dB amplitude.
    assert np.median(new_err) < 0.5
    # The regression it fixes: the old stride does not track the truth --
    # a stride-picked raw bin is dominated by whichever ripple phase it
    # happened to land on, comparable to the ripple's own amplitude.
    assert np.median(old_err) > 1.0


def test_an_ungraded_prediction_reaches_the_wire_as_unknown_never_a_pass():
    """``None`` is load-bearing on every field of this block.

    Three absences, three honest shapes: no priors at all ⇒ no block; a curve
    with no stored report (a state written before D4, or a prediction the
    evaluator refused) ⇒ the curve with ``overall_passed`` **None** and no
    bands — never ``False``, which would read as a measured failure, and never
    ``True``, which the compact-cloud rule already forbids fabricating."""
    v2host.save_v2_state({"session_id": "cap_x", "verify_priors": None})
    assert v2host.crossover_v2_status_block()["prediction"] is None

    v2host.save_v2_state({
        "session_id": "cap_x",
        "verify_priors": {"predicted_sum": None, "predicted_spec": None},
    })
    assert v2host.crossover_v2_status_block()["prediction"] is None

    v2host.save_v2_state({
        "session_id": "cap_x",
        "verify_priors": {
            "predicted_sum": {"freqs_hz": [100.0, 200.0], "magnitude_db": [0.0, 0.0]},
            "predicted_spec": None,
        },
    })
    prediction = v2host.crossover_v2_status_block()["prediction"]
    assert prediction["curve"]["freqs_hz"] == [100.0, 200.0]
    assert prediction["overall_passed"] is None
    assert prediction["spec_bands"] == []
    assert prediction["reference_db"] is None


def test_a_refused_correction_still_reaches_the_wire_with_its_verdict():
    """The 4th reachable ``prediction`` state: report present, curve absent.

    The verdict is stashed BEFORE the improvement gate runs, while
    ``_measure_predicted_sum`` is assigned only after that gate returns — so a
    ``correction_not_an_improvement`` refusal persists the report with
    ``predicted_sum`` still ``None``. Honest rather than leaky: the spec
    verdict genuinely evaluated that prediction, and the gate refused on a
    different question (insufficient improvement over the correction's own
    pre-fit model). Pinned because it is the state a review screen is most
    likely to render wrong — ``overall_passed`` here is a REAL ``False``, not
    the ``None`` that means unknown, and there is no curve to draw beside it.

    Fixture is the shipped refusal case: a comb far denser than the 8-filter
    budget, in both drivers so the shared level frame has nothing to fix.
    """
    from tests.test_crossover_v2_conductor import (
        _LINEARIZABLE_FREQS_HZ,
        FakeSeams,
        _cloud_conductor,
        _eligible_measure_analysis,
        _walk_measure_cloud_to_close,
    )

    comb_db = 9.0 * np.sin(
        2.0 * np.pi * np.log2(_LINEARIZABLE_FREQS_HZ / 200.0) * 5.0
    )
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(
        program, woofer_db=comb_db, tweeter_db=comb_db,
    )
    conductor = _cloud_conductor(fakes)
    with pytest.raises(CaptureBeginRefused):
        _walk_measure_cloud_to_close(conductor)

    # The speaker was left untouched — no candidate, no persisted curve.
    assert conductor.candidate is None
    assert conductor.measure_predicted_sum is None
    v2host.persist_conductor_state(
        conductor, failure_code="correction_not_an_improvement",
    )
    priors = v2host.load_v2_state()["verify_priors"]
    assert priors["predicted_sum"] is None
    assert priors["predicted_spec"] is not None

    prediction = v2host.crossover_v2_status_block()["prediction"]
    assert prediction["curve"] is None
    # A graded miss, NOT an ungradeable unknown.
    assert prediction["overall_passed"] is False
    assert prediction["spec_bands"]
    assert prediction["reference_db"] is not None


def test_a_candidate_persisted_now_records_which_headroom_era_stamped_it():
    """D3/D4's era stamp, at the only place that can honestly write it.

    A candidate this function serializes was built by THIS process, so its
    per-fit charges are the post-#1808 realized-peak rule by construction. The
    stamp is recorded here rather than inferred downstream because nothing on a
    persisted fit distinguishes the two derivations."""
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
    )

    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)

    candidate = v2host.load_v2_state()["candidate"]
    assert candidate["headroom_cost_basis"] == HEADROOM_COST_BASIS_REALIZED_PEAK
    assert isinstance(candidate["headroom_cost_db"], float)


def test_apply_endpoint_requires_current_candidate():
    with pytest.raises(v2host.CrossoverV2Refused):
        _apply(
            {"expected_candidate_fingerprint": "fp"}, None, None
        )
    # A stale fingerprint against a persisted candidate is refused by name.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-current"},
    })
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        _apply(
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
    candidate.

    ``observe_restore`` clears in PLACE, so — unlike ``reset_v2_journey_state``,
    which writes a fresh dict and drops unlisted fields for free — every new
    durable field must be added to it by hand. This pin therefore grows with
    the state: it passed unchanged while ``session_phases``/``cloud`` survived
    an Undo, which is exactly the shape of hole it exists to catch — and now
    ``evidence`` (SF-2 review finding, 2026-07-27): the B1 fix's group-phase-less
    carry-forward in ``persist_conductor_state`` reads ``prior["evidence"]``
    unconditionally, so a stale ``cloud_artifacts`` fingerprint map left behind
    by an Undo would be resurrected on the very next verify-only re-arm.

    ``measure`` is the fourth (#2087): G1's ripple reservation describes the
    capture the undone tuning was built from, and the same PR added a
    ``prior["measure"]`` carry-forward with the identical resurrection shape.
    """
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked"},
                "positions": [
                    {"position_id": "cloud_measure_03", "index": 3, "attempt": 3}
                ],
                # PR-4's own addition to this block — must be just as gone
                # after an Undo as the two PR-3b fields above it; a surviving
                # "pipeline" hands PR-4's spec verdict for an undone session
                # to any surface reading the durable state as if it were live.
                "pipeline": {"available": True, "spec": {"overall_passed": True}},
            }
        },
        "candidate": {"fingerprint": "fp-1"},
        "verify": {"outcome": "fail"},
        "applied": True,
        "apply_blocked": {"id": "x", "message": "x"},
        "pre_apply_profile": {"status": "applied"},
        "gain_plan_db": {"woofer": -3.0},
        "evidence": {
            "bundle_session_id": "bundle-undone",
            "cloud_artifacts": {PHASE_CLOUD_MEASURE: "artifact-fingerprint-undone"},
        },
        "attempts_loop": {
            "history": [{"attempt_id": "candidate-undone"}],
            "last_decision": {"decision": "stop_floor"},
            "store_count": 1,
        },
        "measure": {
            "ripple_reservation": {
                "predicted_ripple_db": 15.244, "threshold_db": 15.0,
            }
        },
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
    # The two fields PR-3b added. A surviving ``session_phases`` sends
    # ``_phase_from_state`` to the VERIFY screen — "The crossover is applied.
    # Put the microphone back where it started…" — moments after the household
    # removed it; a surviving ``cloud`` hands PR-4 a geometry verdict for an
    # undone session.
    assert state["session_phases"] == []
    assert state["cloud"] is None
    # SF-2's field: a surviving ``evidence.cloud_artifacts`` is exactly what
    # persist_conductor_state's B1 carry-forward would resurrect on the next
    # verify-only re-arm, describing artifacts from the undone session.
    assert state["evidence"] is None
    assert state["attempts_loop"] is None
    # #2087's field: a surviving ``measure`` is a reservation about the capture
    # the UNDONE tuning was built from, and persist_conductor_state's own
    # ``prior["measure"]`` carry-forward is the same resurrection shape SF-2
    # found for ``evidence``.
    assert state["measure"] is None
    assert v2host._applied_gate() is False


def test_attempt_loop_status_is_minimal_and_start_over_keeps_its_basis():
    loop = {
        "history": [
            {
                "attempt_id": "candidate-a",
                "metric": "max_db_notch_excluded",
                "provenance": "realized",
                "integrity": {"comparable": True, "reasons": []},
                "repeats_used": 1,
                "grade_db": 0.9,
            }
        ],
        "last_decision": {
            "decision": "continue",
            "reason": "baseline_established",
            "basis_attempt_ids": ["candidate-a"],
            "provenance": "realized",
            "floor": {"claim_floor_db": 0.17},
        },
    }
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "attempts_loop": loop,
    })

    from jasper.active_speaker.model_error_store import record_model_error

    for index in range(7):
        record_model_error(
            speaker_id="speaker-a",
            attempt_id=f"candidate-{index}",
            metric="max_db_notch_excluded",
            predicted_db=0.0,
            realized_db=float(index),
        )

    block = v2host.crossover_v2_status_block()
    assert block["attempts_loop"] == {
        "last_decision": loop["last_decision"],
        "store_count": 7,
    }
    assert "history" not in block["attempts_loop"]

    v2host.reset_v2_journey_state()
    assert v2host.load_v2_state()["attempts_loop"] == loop


def test_undo_after_a_re_verify_does_not_leave_the_verify_screen_standing():
    """The reachable path NEW-2 was found on: apply → re-verify (whose session
    runs ONLY ``verify``) → Undo. Before the fix the leftover
    ``session_phases == ["verify"]`` made the exit screen contradict the action
    the household had just taken."""
    v2host.save_v2_state({
        "session_id": "cap_reverify",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "session_phases": [PHASE_VERIFY],
        "candidate": {"fingerprint": "fp-1"},
        "applied": True,
        "pre_apply_profile": {"status": "applied"},
    })
    v2host.observe_restore()
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_CHECK


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
    with pytest.raises(v2host.CrossoverV2Refused, match="first measured crossover"):
        v2host.handle_v2_restore(None, None)


def test_restore_refuses_across_a_topology_change(monkeypatch):
    """Item 2 (#1605): the stashed Undo target was composed for the output
    topology live at apply time; restoring it after the topology changed would
    realize the wrong graph. handle_v2_restore refuses with a re-measure nudge
    when the current topology's config fingerprint differs from the one stashed
    in pre_apply_profile.source. (The matching-topology pass-through is proven
    end-to-end by test_apply_stashes_pre_apply_profile_and_restore_reverts_
    through_real_seams, which restores under one unchanged topology.)"""
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": True,
        "pre_apply_profile": {
            "status": "applied",
            "source": {"topology_fingerprint": "topo-A"},
            "config": {"path": "/tmp/whatever.yml"},
        },
    })
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology", lambda: object()
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.topology_config_fingerprint",
        lambda _topology: "topo-B",
    )
    with pytest.raises(
        v2host.CrossoverV2Refused, match="output configuration changed"
    ):
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


def test_status_block_reports_an_applied_but_ungraded_result():
    """PR-L4 item 4: applied implies graded, and when it does not, `/state`
    says so in its own field rather than leaving an empty `verify` block for
    every surface to read as "nothing to report" — which is how a 10 dB-dark
    profile sat on JTS3 under a green tick."""
    v2host.save_v2_state({
        "session_id": "cap_ungraded",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": True,
    })
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_UNVERIFIED
    assert grade["graded"] is False
    assert grade["verify_outcome"] is None


def test_status_block_reports_a_graded_result_from_either_instrument():
    """Both a passing VERIFY and a graded post-apply cloud are real checks, and
    the tiers differ in which one they run — express omits the post-apply group
    entirely, so keying only on the cloud would call every express session
    ungraded."""
    v2host.save_v2_state({
        "session_id": "cap_graded_verify",
        "applied": True,
        "verify": {"outcome": "pass"},
    })
    by_verify = v2host.crossover_v2_status_block()["post_apply_grade"]
    # Verified at the mark only — express's whole grade, and distinguishable
    # from a walked post-apply group WITHOUT consulting `tier`.
    assert by_verify["state"] == v2host.GRADE_MARK_VERIFIED
    assert by_verify["graded"] is True

    v2host.save_v2_state({
        "session_id": "cap_graded_cloud",
        "applied": True,
        "verify": {"outcome": "inconclusive"},
        "cloud": {
            PHASE_CLOUD_VERIFY: {
                "geometry": {"locked": False},
                "pipeline": {
                    "available": True,
                    "spec": {"overall_passed": False, "bands": []},
                    "merged_excluded_bands_hz": [],
                },
                "session_id": "cap_graded_cloud",
            },
        },
    })
    by_cloud = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert by_cloud["state"] == v2host.GRADE_GRADED
    # A grade that exists and FAILED is still a grade — "we checked and it is
    # out of spec" is a different claim from "we never checked", and item 7's
    # headline is what renders the first one.
    assert by_cloud["post_apply_spec_passed"] is False


# --- R19 honest grading: scope, the spatial gauge's own state (#2098/#2160) --


def _applied_state(*, tier=None, verify_outcome="pass", cloud_verify=None):
    """An applied session, optionally with a post-apply cloud group."""
    state = {
        "session_id": "cap_r19",
        "session_phases": [PHASE_VERIFY, PHASE_CLOUD_VERIFY],
        "applied": True,
        "verify": {"outcome": verify_outcome},
    }
    if tier is not None:
        state["tier"] = tier
    if cloud_verify is not None:
        state["cloud"] = {PHASE_CLOUD_VERIFY: cloud_verify}
    return state


_NO_GAUGE = object()  # "this era wrote no flatness key", vs. an explicit None


def _closed_cloud_group(*, passed, flatness=_NO_GAUGE):
    """A closed post-apply group in DURABLE shape, as the conductor writes it."""
    pipeline = {
        "available": True,
        "spec": {"overall_passed": passed, "bands": []},
        # Four excluded intervals — the jts3 2026-08-07 shape.
        "merged_excluded_bands_hz": [
            [1400.0, 1900.0], [3000.0, 3200.0], [5000.0, 5400.0], [9000.0, 9600.0],
        ],
    }
    if flatness is not _NO_GAUGE:
        pipeline["flatness"] = flatness
    return {
        # Never locked — the same checkpoint fact the cloud-pipeline doctor
        # line prints beside this group.
        "geometry": {"locked": False},
        "pipeline": pipeline,
        "session_id": "cap_r19",
    }


_GRADED_AND_FAILED_FLATNESS = {
    "max_db": -4.628, "max_hz": 1650.0, "max_band_hz": [1250.0, 2000.0],
    "tolerance_db": 1.5, "rms_db": 1.9, "n_bins": 700, "n_excluded": 40,
    "evaluable": True, "passed": False,
}


def test_a_closed_post_apply_group_that_failed_grades_as_failed_not_as_green():
    """#2160 — the jts3 2026-08-07 shape, reproduced.

    ``overall_passed=False`` reaches ``GRADE_GRADED`` because a
    graded-and-failed group IS graded, and every consuming surface read that
    state name as a clean result: doctor printed ``applied and graded
    (state=graded, verify=pass)`` beside a cloud line reading ``spec=fail
    worst=-4.63dB``. ``state`` cannot carry the difference; ``spatial`` does,
    and the failing gauge's own number rides with it so no consumer re-derives
    it. The ruling is grade-and-disclose: the tune stays, the failure is
    loud."""
    v2host.save_v2_state(_applied_state(
        tier=TIER_FULL,
        cloud_verify=_closed_cloud_group(
            passed=False, flatness=_GRADED_AND_FAILED_FLATNESS,
        ),
    ))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_GRADED  # unchanged vocabulary
    assert grade["spatial"] == v2host.GRADE_SPATIAL_FAILED
    # A failed grade is a COMPLETED grade — the tier delivered what it
    # promised, and what it delivered is a miss.
    assert grade["scope"] == v2host.GRADE_SCOPE_SPATIAL
    assert grade["complete"] is True
    assert grade["spatial_worst_db"] == pytest.approx(-4.628)
    assert grade["spatial_worst_hz"] == pytest.approx(1650.0)


def test_a_full_session_that_only_verified_at_the_mark_is_incomplete():
    """#2098's own field evidence: Full, ``verify.outcome=pass``, ``cloud``
    absent — a true local result rendered as the wider claim. The local pass
    is preserved; what is added is that it is not what Full promised."""
    v2host.save_v2_state(_applied_state(tier=TIER_FULL))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_MARK_VERIFIED  # the local pass stands
    assert grade["graded"] is True
    assert grade["scope"] == v2host.GRADE_SCOPE_MARK
    assert grade["spatial"] == v2host.GRADE_SPATIAL_ABSENT
    assert grade["complete"] is False


def test_an_express_session_verified_at_the_mark_is_complete_and_scoped():
    """Express structurally never walks a post-apply group, so the mark IS its
    whole promise. Judging it against Full's would warn every express session
    ever run — the mirror of the defect."""
    v2host.save_v2_state(_applied_state(tier=TIER_EXPRESS))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["scope"] == v2host.GRADE_SCOPE_MARK
    assert grade["complete"] is True


def test_a_closed_and_passing_group_is_a_complete_spatial_grade():
    v2host.save_v2_state(_applied_state(
        tier=TIER_FULL,
        cloud_verify=_closed_cloud_group(passed=True, flatness={
            **_GRADED_AND_FAILED_FLATNESS, "max_db": 0.9, "passed": True,
        }),
    ))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["spatial"] == v2host.GRADE_SPATIAL_PASSED
    assert grade["scope"] == v2host.GRADE_SCOPE_SPATIAL
    assert grade["complete"] is True
    # No number: there is no failure for it to quantify, and printing the
    # margin of a pass beside a failure verdict is how the two get confused.
    assert grade["spatial_worst_db"] is None
    assert grade["spatial_worst_hz"] is None


def test_a_group_that_could_not_be_graded_is_not_reported_as_a_failure():
    """``SpecFlatness.passed`` is ``False`` for a spectrum no band survived to
    measure, by its own "will not report a clean bill of health for a spectrum
    it could not fully measure" rule. Reading that as a miss states a
    measurement that never happened."""
    v2host.save_v2_state(_applied_state(
        tier=TIER_FULL,
        cloud_verify=_closed_cloud_group(passed=False, flatness={
            **_GRADED_AND_FAILED_FLATNESS,
            "max_db": None, "max_hz": None, "evaluable": False, "passed": False,
        }),
    ))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["spatial"] == v2host.GRADE_SPATIAL_UNMEASURABLE
    assert grade["spatial_worst_db"] is None
    # No spatial CLAIM exists, so the delivered width falls back to whatever
    # the mark proved — and on Full that is short of the promise.
    assert grade["scope"] == v2host.GRADE_SCOPE_MARK
    assert grade["complete"] is False


def test_a_failing_group_with_no_gauge_at_all_stays_a_failure():
    """Unmeasurable is claimed only on POSITIVE evidence. A durable state
    written before the gauge shipped carries a real ``overall_passed=False``
    and no ``flatness`` — downgrading that to "could not be measured" on the
    ABSENCE of an instrument is the fabricated reading, pointed the other
    way."""
    v2host.save_v2_state(_applied_state(
        tier=TIER_FULL, cloud_verify=_closed_cloud_group(passed=False),
    ))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["spatial"] == v2host.GRADE_SPATIAL_FAILED
    assert grade["spatial_worst_db"] is None  # no gauge, no number invented


def test_an_unreadable_tier_is_judged_on_delivery_not_on_a_guessed_promise():
    """A pre-tier state file, or one written by a later build. This build
    cannot know what was promised, and manufacturing an incompleteness warning
    about a promise it never read would be worse than saying what it said —
    the same degrade rule the ``state`` vocabulary already follows."""
    v2host.save_v2_state(_applied_state(tier=None))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["scope"] == v2host.GRADE_SCOPE_MARK
    assert grade["complete"] is True

    v2host.save_v2_state(_applied_state(tier="tier-from-2027"))
    later = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert later["complete"] is True


def test_a_verify_that_did_not_pass_delivers_no_scope_at_all():
    v2host.save_v2_state(_applied_state(
        tier=TIER_FULL, verify_outcome="inconclusive",
    ))
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_INCONCLUSIVE
    assert grade["scope"] == v2host.GRADE_SCOPE_NONE
    assert grade["complete"] is False


def test_status_block_never_asks_an_unapplied_session_for_a_grade():
    v2host.save_v2_state({"session_id": "cap_none", "applied": False})
    grade = v2host.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_NOT_APPLIED
    # Nothing promised, so nothing outstanding: `complete=False` here would
    # warn every speaker that has never been commissioned.
    assert grade["complete"] is True
    assert grade["scope"] == v2host.GRADE_SCOPE_NONE
    assert grade["spatial"] == v2host.GRADE_SPATIAL_ABSENT
    assert grade["graded"] is True


def test_apply_blocked_is_scoped_to_its_producing_session():
    """Item 1 (#1605): a blocked-apply nudge belongs to the session that
    produced it. persist_conductor_state carries apply_blocked forward while
    the durable session_id still matches the snapshot's (the blocked-apply
    terminal path is same-session), but a FRESH session's first snapshot drops
    the stale nudge instead of surfacing session A's blocker on session B's
    apply step. pre_apply_profile stays unconditional — only apply_blocked is
    gated (see persist_conductor_state's carry-forward comment)."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    _c1, session1, phone1 = _mint_v2_session(backend, spec, driver_cls=None)
    conductor1 = _conductor(backend, session1, phone1, published=[])
    conductor1.consume_capture(1, 1, CaptureResult(wav=b"fake-check"))
    v2host.persist_conductor_state(conductor1, failure_code=None)
    conductor1.consume_capture(2, 2, CaptureResult(wav=b"fake-measure"))
    v2host.persist_conductor_state(conductor1, failure_code=None)

    issue = {"id": "boom", "message": "the apply was blocked"}
    v2host._persist_apply_blocked(issue)
    # Same session ⇒ preserved (the real blocked-apply terminal persist path).
    v2host.persist_conductor_state(conductor1, failure_code=REASON_APPLY_FAILED)
    assert v2host.load_v2_state()["apply_blocked"] == issue

    # A brand-new session's first snapshot drops the stale nudge.
    _c2, session2, phone2 = _mint_v2_session(backend, spec, driver_cls=None)
    assert session2.session_id != session1.session_id
    conductor2 = _conductor(backend, session2, phone2, published=[])
    v2host.persist_conductor_state(conductor2, failure_code=None)
    assert v2host.load_v2_state()["apply_blocked"] is None


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
    out = analyze(
        program, result, MeasurementPriors(crossover_fc_hz=FC_HZ), geometry,
        phase="verify",
    )

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
            phase="verify",
        )
    # NOT silent: analysis ran uncalibrated, annotated as a stored fact + WARN.
    assert seen["calibration"] is None
    assert meta["calibration"]["verify"] == {"applied": False, "calibration_id": None}
    assert "crossover_v2_uncalibrated_capture" in caplog.text
    # W6.13 round-5 diagnostic: the WARN names what the phone-reported setup
    # actually held at resolve time — here nothing at all.
    assert "setup_mode=absent" in caplog.text


# --- mic_tier threading (#1668 PR-C) --------------------------------------
#
# jasper.audio_measurement.calibration.mic_tier_for_model's own docstring
# names bind_production_analyze as "the PR that actually calls
# compose_envelope in the measure/fit flow" — this is that wiring.


def test_production_analyze_threads_mic_tier_from_resolved_calibration(monkeypatch):
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None, priors=None):
        seen["priors"] = priors
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    class _Record:
        curve = object()
        calibration_id = "cal-umik2"
        model = "minidsp_umik2"

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: _Record(), meta={},
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(setup={"calibration": {"mode": "serial"}})
    incoming_priors = MeasurementPriors(crossover_fc_hz=FC_HZ)
    analyze(
        program, result, incoming_priors, MeasurementGeometry(), phase="verify",
    )

    # The ORIGINAL priors object is untouched (dataclasses.replace returns a
    # new instance) — the mutated copy is what reaches analyze_program_capture.
    assert incoming_priors.mic_tier is None
    assert seen["priors"].mic_tier == "reference"
    assert seen["priors"] is not incoming_priors
    # Every other field survives the replace unchanged.
    assert seen["priors"].crossover_fc_hz == FC_HZ


def test_production_analyze_mic_tier_defaults_to_phone_when_no_calibration_resolves(monkeypatch):
    """No calibration record at all (resolver returned None) must resolve
    to the CONSERVATIVE "phone" tier — never a guess at "reference", and
    never a crash on ``getattr(None, "model", None)``."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None, priors=None):
        seen["priors"] = priors
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={},
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    analyze(
        program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    assert seen["priors"].mic_tier == "phone"


def test_production_analyze_mic_tier_handles_a_bare_calibration_curve_record(monkeypatch):
    """A record with no ``model`` attribute at all (the "bare
    CalibrationCurve" test-double shape bind_production_analyze already
    special-cases for ``curve``) must not crash — getattr's default takes
    over and resolves to the conservative "phone" tier."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.calibration import CalibrationCurve
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None, priors=None):
        seen["priors"] = priors
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)
    bare_curve = CalibrationCurve(
        freqs_hz=[20.0, 20000.0], correction_db=[0.0, 0.0],
    )
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: bare_curve, meta={},
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    analyze(
        program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    assert seen["priors"].mic_tier == "phone"


# --- operator capture retention (durable observability, Part 2) ----------------
#
# Off by default, gated on XOVER_CAPTURE_DUMP_DIR / "ENABLED" existing.
# Productizes a hot-patch that used to live directly in bind_production_
# analyze._analyze and kept getting wiped by every deploy.


def test_capture_retention_marker_absent_writes_nothing(tmp_path, monkeypatch):
    """ABSENT marker — the default for every real household — is zero
    behavior change: analyze still runs and returns normally, and nothing
    is ever written to disk (not even the directory)."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    dump_dir = tmp_path / "xover-capture-dump"
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", dump_dir)
    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: "analysis")

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    out = analyze(
        program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    assert out == "analysis"
    assert not dump_dir.exists()


def test_capture_retention_marker_present_writes_wav_and_diagnostic_sidecar(
    tmp_path, monkeypatch,
):
    """PRESENT marker persists the raw WAV plus a JSON sidecar carrying the
    device/setup/hash metadata AND the analysis's own diagnostic summary —
    the same numbers the v2 conductor's per-capture diag log events surface,
    so a retained clip is self-describing."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        AlignmentEstimate,
        MeasurementGeometry,
        MeasurementPriors,
        ProgramAnalysis,
    )

    dump_dir = tmp_path / "xover-capture-dump"
    dump_dir.mkdir()
    (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).touch()
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", dump_dir)

    fake_analysis = ProgramAnalysis(
        phase="verify", program_id="prog-1", locations=(),
        alignment=AlignmentEstimate(
            delay_us=12.0, raw_delay_us=12.0, parallax_us=0.0,
            polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
            confidence=0.9,
        ),
    )
    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: fake_analysis)

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(
        setup={"calibration": {"mode": "none"}}, device={"label": "UMIK-2"},
    )
    out = analyze(
        program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    assert out is fake_analysis

    wavs = sorted(dump_dir.glob("*.wav"))
    jsons = sorted(dump_dir.glob("*.json"))
    assert len(wavs) == 1
    assert len(jsons) == 1
    assert wavs[0].stem == jsons[0].stem
    assert wavs[0].read_bytes() == result.wav

    sidecar = json.loads(jsons[0].read_text())
    assert sidecar["phase"] == program.phase
    assert sidecar["device_label"] == "UMIK-2"
    assert sidecar["wav_bytes"] == len(result.wav)
    assert len(sidecar["wav_sha256_12"]) == 12
    assert sidecar["setup_mode"] == "none"
    # The diagnostic summary is the analysis's OWN numbers (no accepted/code
    # — the analyze seam runs before the conductor's phase gate).
    assert sidecar["diagnostic"]["alignment_confidence"] == 0.9
    assert sidecar["diagnostic"]["delay_us"] == 12.0


def test_capture_retention_labels_from_flow_phase_not_program_identity(
    tmp_path, monkeypatch, caplog,
):
    """Issue #1855 (the byte-identity trap): every cloud position plays
    ``self._verify_program`` — see ``CrossoverV2Conductor._program_for_phase``'s
    ``SUMMED_SWEEP_PHASES`` branch — so the SAME program object/bytes is
    retained during VERIFY, CLOUD_MEASURE, and CLOUD_VERIFY alike, and
    ``program.phase`` is always "verify" for all three. Before the fix,
    retention read ``program.phase`` directly and mislabeled every cloud
    position as "verify" (32 of 45 retained sidecars, per the 2026-07-29
    WO-0 retrospective) — the issue's headline symptom being every
    ``event=correction.crossover_v2_capture_retained`` line during
    CLOUD_MEASURE carrying ``phase=verify``. The fix threads the conductor's
    own flow phase through the ``analyze`` seam's ``phase=`` keyword instead.
    Pin: the SAME byte-identical capture, retained once per flow phase, must
    yield two sidecars AND two ``crossover_v2_capture_retained`` log lines
    with two DIFFERENT, correct labels."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        AlignmentEstimate,
        MeasurementGeometry,
        MeasurementPriors,
        ProgramAnalysis,
    )

    dump_dir = tmp_path / "xover-capture-dump"
    dump_dir.mkdir()
    (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).touch()
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", dump_dir)

    fake_analysis = ProgramAnalysis(
        phase="verify", program_id="prog-1", locations=(),
        alignment=AlignmentEstimate(
            delay_us=12.0, raw_delay_us=12.0, parallax_us=0.0,
            polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
            confidence=0.9,
        ),
    )
    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: fake_analysis)

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    # ONE program — the conductor's real ``self._verify_program`` — reused
    # across both calls, exactly as the real flow reuses it for VERIFY and
    # every cloud position.
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    assert program.phase == "verify"  # the trap: identical for all three phases

    verify_result = _FakeResult(device={"label": "UMIK-2"})
    cloud_result = _FakeResult(device={"label": "UMIK-2"})
    # _mono_wav_bytes() is deterministic — these two captures are genuinely
    # byte-identical, matching the WO-0 retrospective's SHA-256 finding.
    assert verify_result.wav == cloud_result.wav

    def _retained_event_line(text: str) -> str:
        # Isolate the retention event's OWN line — the resolver returns no
        # calibration here, so a same-batch ``crossover_v2_uncalibrated_
        # capture`` WARN also fires and legitimately says ``phase=verify``
        # (it reports the PROGRAM's type, an unrelated, unchanged concern —
        # see bind_production_analyze's docstring). A bare ``in caplog.text``
        # check would false-collide with that line.
        lines = [
            line for line in text.splitlines()
            if "event=correction.crossover_v2_capture_retained" in line
        ]
        assert len(lines) == 1, f"expected exactly one retained-event line, got {lines!r}"
        return lines[0]

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        analyze(
            program, verify_result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(), phase="verify",
        )
        assert "phase=verify" in _retained_event_line(caplog.text)
        caplog.clear()

        analyze(
            program, cloud_result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(), phase="cloud_measure",
        )
        # The exact regression: this event must say cloud_measure, not the
        # "verify" #1855 reported for every cloud position.
        retained_line = _retained_event_line(caplog.text)
        assert "phase=cloud_measure" in retained_line
        assert "phase=verify" not in retained_line

    jsons = sorted(dump_dir.glob("*.json"))
    assert len(jsons) == 2
    sidecars = {json.loads(p.read_text())["phase"]: p for p in jsons}
    assert set(sidecars) == {"verify", "cloud_measure"}
    # Both sidecars agree on the WAV hash (same bytes) — only the label
    # differs, and it differs correctly.
    verify_sidecar = json.loads(sidecars["verify"].read_text())
    cloud_sidecar = json.loads(sidecars["cloud_measure"].read_text())
    assert verify_sidecar["wav_sha256_12"] == cloud_sidecar["wav_sha256_12"]


def test_capture_retention_prunes_oldest_past_the_file_count_cap(tmp_path, monkeypatch):
    import time as _time

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    dump_dir = tmp_path / "xover-capture-dump"
    dump_dir.mkdir()
    (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).touch()
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", dump_dir)
    # 2 captures' worth (wav+json each) — a huge byte cap so only the file
    # count constraint is exercised.
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_MAX_FILES", 4)
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_MAX_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: "analysis")

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    for _ in range(3):
        analyze(
            program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
        _time.sleep(0.02)  # distinct filenames/mtimes for oldest-first pruning

    entries = [
        p for p in dump_dir.iterdir()
        if p.name != v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER
    ]
    # 3 captures write 6 files (wav+json each); the cap keeps only 4 — the
    # newest 2 captures survive, the oldest is pruned. The enable marker
    # itself is excluded from both the cap and this count — it must never
    # be a prune candidate (see XOVER_CAPTURE_DUMP_ENABLED_MARKER).
    assert len(entries) == 4
    stems = {p.stem for p in entries}
    assert len(stems) == 2
    assert (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).exists()


def test_capture_retention_write_failure_does_not_break_analysis(
    tmp_path, monkeypatch, caplog,
):
    """Retention is best-effort: a failure inside the write path is caught
    and logged at WARN, and never affects the measurement's own analysis."""
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    dump_dir = tmp_path / "xover-capture-dump"
    dump_dir.mkdir()
    (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).touch()
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", dump_dir)
    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: "analysis")

    def _boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _boom)

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        out = analyze(
            program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
    assert out == "analysis"
    assert "correction.crossover_v2_capture_retain_failed" in caplog.text
    assert not list(dump_dir.glob("*.wav"))


def test_capture_retention_prunes_oldest_past_the_byte_cap(tmp_path, monkeypatch):
    """The count cap is pinned by the sibling test above; this pins the
    BYTES branch — a file-count cap generous enough that only the byte
    constraint binds. Assertions stay invariant-based (never exceed the
    byte cap; the newest capture survives; pruning happened) rather than an
    exact final file count: the WAV (large) and its JSON sidecar (tiny) are
    independent ring-buffer entries by design (see
    ``XOVER_CAPTURE_DUMP_MAX_FILES``'s module comment), so the byte cap
    alone does not guarantee they are always evicted together — an older
    capture's tiny sidecar can legitimately survive under budget after its
    much larger WAV is evicted."""
    import time as _time

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: "analysis")
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)

    # Measure one capture's actual on-disk footprint (wav + sidecar) with a
    # huge cap so nothing is pruned during the measurement itself — avoids
    # hardcoding a byte count that would drift if the sidecar shape changes.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).touch()
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", probe_dir)
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_MAX_FILES", 1000)
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_MAX_BYTES", 10 * 1024 * 1024)
    analyze(
        program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    one_capture_bytes = sum(
        p.stat().st_size for p in probe_dir.iterdir()
        if p.name != v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER
    )

    dump_dir = tmp_path / "xover-capture-dump"
    dump_dir.mkdir()
    (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).touch()
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", dump_dir)
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_MAX_FILES", 1000)
    byte_cap = int(one_capture_bytes * 1.5)  # room for ~1 capture, not 2
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_MAX_BYTES", byte_cap)

    newest_files: list[Path] = []
    for _ in range(3):
        before = {p.name for p in dump_dir.iterdir()}
        analyze(
            program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
        newest_files = [p for p in dump_dir.iterdir() if p.name not in before]
        _time.sleep(0.02)  # distinct filenames/mtimes for oldest-first pruning

    entries = [
        p for p in dump_dir.iterdir()
        if p.name != v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER
    ]
    total_bytes = sum(p.stat().st_size for p in entries)
    # The byte cap is respected...
    assert total_bytes <= byte_cap
    # ...pruning actually removed something (3 captures write 6 files)...
    assert len(entries) < 6
    # ...and the newest capture's own wav+json specifically survived, under
    # the cap the file-count cap (1000) never binds against.
    assert len(newest_files) == 2
    assert all(p.exists() for p in newest_files)
    assert (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).exists()


def test_capture_retention_file_vanishing_mid_prune_does_not_break_analysis(
    tmp_path, monkeypatch, caplog,
):
    """Review finding: the operator workflow this feature exists for is
    ls/scp/rm-ing the dump directory WHILE captures keep landing, so a file
    can legitimately disappear between prune's iterdir() and a later stat()
    on it. That must be skipped, not propagate an exception that crashes the
    measurement."""
    import errno as _errno
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    dump_dir = tmp_path / "xover-capture-dump"
    dump_dir.mkdir()
    (dump_dir / v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER).touch()
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_DIR", dump_dir)
    monkeypatch.setattr(pa_mod, "analyze_program_capture", lambda *a, **k: "analysis")
    # Exactly one capture's worth — the SECOND capture's prune pass then has
    # to evict the first generation's files (exercising the stat loop).
    monkeypatch.setattr(v2host, "XOVER_CAPTURE_DUMP_MAX_FILES", 2)

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)

    # First capture lands two files (wav + sidecar) — the ones the SECOND
    # capture's prune pass will try to stat/evict. Exclude the enable
    # marker: it is not (and must not be) a prune candidate.
    analyze(
        program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    first_gen = [
        p for p in dump_dir.iterdir()
        if p.name != v2host.XOVER_CAPTURE_DUMP_ENABLED_MARKER
    ]
    assert len(first_gen) == 2

    original_stat = Path.stat
    stat_seen: set[Path] = set()
    vanished = {"done": False}

    def _flaky_stat(self, *a, **k):
        # Simulate ONE first-generation file disappearing (an operator deleting
        # it concurrently) while prune is mid-pass. The subtlety this test MUST
        # honor: prune stats each candidate TWICE — first in the entries
        # comprehension's ``is_file()``, then in the guarded sizing loop's
        # ``p.stat()`` (post-fix) / the UNGUARDED sort+sum ``.stat()`` (pre-fix).
        # ``is_file()`` catches OSError internally, so raising on the FIRST stat
        # is swallowed and never reaches the vulnerable path — a test that does
        # that passes on broken code too (the review finding). So we let the
        # first stat SUCCEED (the file enters ``entries``) and raise on the
        # SECOND — the sort/sum stat the pre-fix code left outside any
        # try/except. Realistic errno (ENOENT) so pathlib classifies it exactly
        # like a real vanish (a bare ``FileNotFoundError(self)`` sets no
        # ``.errno`` and pathlib re-raises it). One file only; every other stat
        # (the new capture's files, the enable marker) behaves normally.
        if not vanished["done"] and self in first_gen:
            if self in stat_seen:
                vanished["done"] = True
                raise FileNotFoundError(
                    _errno.ENOENT, "No such file or directory", str(self)
                )
            stat_seen.add(self)
        return original_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", _flaky_stat)

    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        out = analyze(
            program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
    # The measurement itself is completely unaffected by the vanished file.
    assert out == "analysis"
    assert vanished["done"], "the flaky-stat path was never exercised"
    # Degrades silently — no retention-failure WARN, since retention + prune
    # both still succeeded overall (the vanished file was simply skipped).
    assert "correction.crossover_v2_capture_retain_failed" not in caplog.text


def test_uncalibrated_warn_reports_the_setup_the_phone_actually_sent(
    monkeypatch, caplog,
):
    """W6.13: the round-5 ambiguity was 'did the phone send NO setup, or a
    setup whose calibration did not resolve?' — the uncalibrated-capture WARN
    now carries the observed calibration mode + id (redacted-safe: never a
    serial or an uploaded file body) so one live journal line settles it."""
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    monkeypatch.setattr(
        pa_mod, "analyze_program_capture", lambda *a, **k: "analysis"
    )
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(
        setup={
            "calibration": {
                "mode": "stored",
                "calibration_id": "cal-stale",
                "model": "minidsp_umik2",
                "serial": "SECRET-810",
            },
        },
    )
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
    assert "crossover_v2_uncalibrated_capture" in caplog.text
    assert "setup_mode=stored" in caplog.text
    assert "setup_calibration_id=cal-stale" in caplog.text
    # Redaction: the serial never reaches the journal.
    assert "SECRET-810" not in caplog.text


def test_setup_calibration_observation_is_redacted_safe():
    """The extractor itself: absent / mode-none / stored shapes, and only
    mode + calibration_id ever come back."""
    assert v2host._setup_calibration_observation(None) == ("absent", "")
    assert v2host._setup_calibration_observation({}) == ("absent", "")
    assert v2host._setup_calibration_observation(
        {"calibration": {"mode": "none"}}
    ) == ("none", "")
    assert v2host._setup_calibration_observation(
        {"calibration": {"mode": "stored", "calibration_id": "cal-1"}}
    ) == ("stored", "cal-1")
    assert v2host._setup_calibration_observation(
        {"calibration": {"mode": "serial", "serial": "810-8494"}}
    ) == ("serial", "")


def test_production_analyze_default_resolver_is_the_shared_relay_machinery():
    """The default resolver IS correction_setup._relay_calibration_from_setup
    (the one point the room + legacy crossover flows resolve phone calibration
    choices) — a no-choice setup resolves to None."""
    assert v2host.resolve_relay_calibration(None, None) is None
    assert v2host.resolve_relay_calibration({"calibration": {"mode": "none"}}, None) is None


# --- W6.12: v2 calibration handoff — the household-mic hint reaches a v2 session --
#
# Every v2 capture logged crossover_v2_uncalibrated_capture even with a
# resolvable stored household mic (a UMIK-2 by serial) — root cause: a v2
# capture-plan session has no calibration-picker screen of its own (unlike
# level_ramp/room_sweep) and, unlike the legacy per-driver crossover flow
# (which inherits its choice from the level_ramp page visited first in the
# same tab), never had anywhere to carry the Wave-2 household-mic hint. The
# fix threads correction_setup._default_setup_calibration_for_spec() into
# build_v2_session_spec/build_v2_verify_session_spec (their existing
# **spec_kwargs already forwards to build_crossover_sweep_spec's new
# default_setup_calibration parameter) and applies it silently on the
# capture page. These tests pin each link of that handoff.


def _seed_household_mic(tmp_path, monkeypatch):
    """A resolvable stored household mic (mirrors
    test_default_setup_calibration_for_spec_present_and_absent)."""
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial="810-8494",
        root=cal_root,
    )
    write_household_mic(
        household_mic_from_calibration(record, serial="810-8494"),
        path=household_path,
    )
    return record


def test_default_setup_calibration_for_v2_reuses_the_household_mic_hint(
    tmp_path, monkeypatch,
):
    """No household mic ⇒ no hint (fail-soft); a resolvable one ⇒ the SAME
    hint correction_setup._default_setup_calibration_for_spec builds for
    level_ramp, now available to a v2 session too."""
    assert v2host.default_setup_calibration_for_v2() is None

    record = _seed_household_mic(tmp_path, monkeypatch)

    hint = v2host.default_setup_calibration_for_v2()
    assert hint is not None
    assert hint.mode == "serial"
    assert hint.calibration_id == record.calibration_id
    assert hint.resolvable is True


def test_v2_session_and_verify_specs_carry_the_default_calibration_hint(
    tmp_path, monkeypatch,
):
    """build_v2_session_spec / build_v2_verify_session_spec's existing
    **spec_kwargs forwards default_setup_calibration through to
    build_crossover_sweep_spec's new parameter, landing on the WIRE spec the
    phone actually receives."""
    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_session_spec,
        build_v2_verify_session_spec,
    )

    record = _seed_household_mic(tmp_path, monkeypatch)
    hint = v2host.default_setup_calibration_for_v2()
    assert hint is not None

    session_spec = build_v2_session_spec(
        _roles(), FC_HZ,
        acknowledgement_binding=_BINDING,
        default_setup_calibration=hint,
    )
    verify_spec = build_v2_verify_session_spec(
        FC_HZ, acknowledgement_binding=_BINDING, default_setup_calibration=hint,
    )
    for spec in (session_spec, verify_spec):
        wire = spec.to_dict()
        assert wire["default_setup"]["calibration"]["calibration_id"] == (
            record.calibration_id
        )
        assert wire["default_setup"]["calibration"]["mode"] == "serial"

    # Omitted (the pre-W6.12 default): no hint on the wire — every existing
    # caller (including the two legacy correction_setup.py handlers, which
    # never pass this) stays byte-identical.
    bare = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding=_BINDING,
    ).to_dict()
    assert "default_setup" not in bare


def test_plan_flow_stored_calibration_lands_in_the_analyze_call_and_evidence(
    tmp_path, monkeypatch, caplog,
):
    """THE handoff pin: once the capture page applies the household-mic hint
    (a v2 capture posting setup.calibration = {mode: "stored", calibration_id,
    model} — the exact shape applyDefaultCalibrationHintSilently now submits),
    bind_production_analyze's PRODUCTION resolver (resolve_relay_calibration,
    not a mock) must actually apply the calibration curve and record it in the
    persisted evidence — never silently falling back to uncalibrated."""
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    record = _seed_household_mic(tmp_path, monkeypatch)

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None, priors=None):
        seen["calibration"] = calibration
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    meta: dict[str, Any] = {}
    # resolve_calibration defaults to resolve_relay_calibration — the REAL
    # production seam — proving the fix through the exact path a live
    # v2 session rides, not a test double.
    analyze = v2host.bind_production_analyze(meta=meta)
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(
        setup={
            "calibration": {
                "mode": "stored",
                "calibration_id": record.calibration_id,
                "model": "minidsp_umik2",
            },
        },
        device={"label": "UMIK-2"},
    )
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        out = analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )

    assert out == "analysis"
    assert seen["calibration"] is not None
    assert meta["calibration"]["verify"] == {
        "applied": True, "calibration_id": record.calibration_id,
    }
    assert "crossover_v2_uncalibrated_capture" not in caplog.text


def test_plan_flow_stored_calibration_refuses_on_device_mismatch(
    tmp_path, monkeypatch, caplog,
):
    """The 2026-07-20 incident, through the full production seam: the
    household's UMIK-2 calibration is the resolvable stored default, but THIS
    capture's phone-reported device is a Dayton iMM-6C. The real
    ``resolve_relay_calibration`` seam must refuse to apply it — the
    analysis still runs (never blocked), annotated uncalibrated, with BOTH
    the existing ``crossover_v2_uncalibrated_capture`` WARN and the NEW
    distinct mismatch event."""
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    record = _seed_household_mic(tmp_path, monkeypatch)

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None, priors=None):
        seen["calibration"] = calibration
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    meta: dict[str, Any] = {}
    analyze = v2host.bind_production_analyze(meta=meta)
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(
        setup={
            "calibration": {
                "mode": "stored",
                "calibration_id": record.calibration_id,
                "model": "minidsp_umik2",
            },
        },
        device={"label": "iMM-6C", "device_id": "some-dayton-device-id"},
    )
    with caplog.at_level(_logging.WARNING):
        out = analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )

    assert out == "analysis"
    assert seen["calibration"] is None  # never mis-applied
    assert meta["calibration"]["verify"] == {"applied": False, "calibration_id": None}
    assert "crossover_v2_uncalibrated_capture" in caplog.text
    assert "calibration_device_identity_mismatch" in caplog.text

    # The household record was never re-persisted against the wrong device.
    from jasper.correction.household_mic import read_household_mic
    from jasper.web.correction_setup import _household_mic_path

    saved = read_household_mic(path=_household_mic_path())
    assert saved is not None
    assert saved.model_key == "minidsp_umik2"


# --- status block (S1b) -----------------------------------------------------------


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
    # And the "applying" projection: measure accepted, not yet applied — the
    # conductor's own auto-apply is in flight (owner ruling, 2026-07-20).
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
    })
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_APPLYING


def test_verify_fail_persists_expert_evidence_through_the_real_persist_path():
    """Item 5b (#1605): a real out_of_tolerance VERIFY persists its tracking
    numbers under state["verify"]["evidence"] via the SAME persist_conductor_
    state the relay-driven flow calls after every capture — so the verify_fail
    screen can fold them behind its expert disclosure. Pins the end-to-end key
    match between what the conductor writes and what
    crossover_envelope_v2._verify_expert_details reads (the two are tested
    separately in test_crossover_v2_conductor.py / test_crossover_envelope_v2.py;
    this closes the loop that a renamed key would silently break)."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    analyses = {"verify": lambda program: _verify_analysis(program, max_db=2.4)}
    stage1 = _conductor(backend, session, phone, published=[], analyses=analyses)
    attempt = _walk_to_verify(stage1, persist=True)
    # STAGE 2 is its own session with its own conductor (work order D1/D2), so
    # VERIFY is index 1 of a new index space rather than index 11 of stage 1's.
    conductor = _stage2_conductor(backend, session, phone, analyses=analyses)
    conductor.consume_capture(
        STAGE2_VERIFY_INDEX, attempt + 1, CaptureResult(wav=b"fake-verify")
    )
    v2host.persist_conductor_state(conductor, failure_code=conductor.last_failure_code)

    assert conductor.verify_outcome == "fail"
    verify = v2host.load_v2_state()["verify"]
    assert verify["outcome"] == "fail"
    assert verify["evidence"]["max_db"] == 2.4
    assert verify["evidence"]["tolerance_db"] == 1.5


def test_verify_block_carries_no_flatness_key_after_the_pr5_rebase():
    """The flat-linearization plan's PR-5 removed ``verify["flatness"]``.

    That key carried the retired per-VERIFY-capture construction; the spec
    verdict is a property of the CLOUD and lives in the ``cloud`` block, so a
    second copy under ``verify`` would be exactly the duplicated-frame shape
    PR-5 removes. The two assertions that were the point of the retired tests
    (a PASS still persists no ``evidence``; a FAIL still persists it, with the
    gated number) are kept verbatim — the removal must not have disturbed
    them."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    analyses = {"verify": _verify_analysis}
    stage1 = _conductor(backend, session, phone, published=[], analyses=analyses)
    attempt = _walk_to_verify(stage1, persist=True)
    conductor = _stage2_conductor(backend, session, phone, analyses=analyses)
    conductor.consume_capture(
        STAGE2_VERIFY_INDEX, attempt + 1, CaptureResult(wav=b"fake-verify")
    )
    v2host.persist_conductor_state(conductor, failure_code=conductor.last_failure_code)

    assert conductor.verify_outcome == "pass"
    verify = v2host.load_v2_state()["verify"]
    assert verify["outcome"] == "pass"
    assert "evidence" not in verify  # unchanged: a pass never persists this
    assert "flatness" not in verify


def test_verify_fail_block_still_persists_evidence_and_no_flatness():
    """The fail-branch half of the same boundary."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    analyses = {"verify": lambda program: _verify_analysis(program, max_db=2.4)}
    stage1 = _conductor(backend, session, phone, published=[], analyses=analyses)
    attempt = _walk_to_verify(stage1, persist=True)
    # STAGE 2 is its own session with its own conductor (work order D1/D2), so
    # VERIFY is index 1 of a new index space rather than index 11 of stage 1's.
    conductor = _stage2_conductor(backend, session, phone, analyses=analyses)
    conductor.consume_capture(
        STAGE2_VERIFY_INDEX, attempt + 1, CaptureResult(wav=b"fake-verify")
    )
    v2host.persist_conductor_state(conductor, failure_code=conductor.last_failure_code)

    assert conductor.verify_outcome == "fail"
    verify = v2host.load_v2_state()["verify"]
    assert verify["outcome"] == "fail"
    assert verify["evidence"]["max_db"] == 2.4
    assert "flatness" not in verify


def test_candidate_summary_surfaces_linearization_outcome_and_octaves():
    """Gauge fix (2026-07-24): items 2/3 -- _candidate_summary reads
    linearization_outcome straight off the candidate, and reduces the rich
    per-role linearization dict down to just observe_octave_summary (the
    OBSERVE-layer honesty-ladder disclosure) for the wizard payload -- never
    persisting the rest of LinearizationFit.to_dict()'s cargo (filters,
    residuals, reason_summary, ...) into this session-scoped view."""
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
    )

    candidate = MeasuredCrossoverCandidate(
        program_id="prog-abc",
        analysis={"alignment_confidence": 0.9, "predicted_ripple_db": 1.1,
                  "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4}},
        source_preset=_preset(),
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
        linearization={
            "woofer": {
                "role": "woofer", "filters": [], "fit_band_hz": [150.0, 3951.5],
                "target_level_db": -20.22, "residual_rms_db": 4.16,
                "residual_max_db": 12.21, "reason_summary": {},
                "mic_tier": "reference", "driver_class": "unknown", "n_repeats": 2,
                "observe_octave_summary": {"8000": -0.3, "12000": -1.1, "16000": -2.8},
            },
            "tweeter": {
                "role": "tweeter", "filters": [], "fit_band_hz": [2020.0, 13905.2],
                "target_level_db": -8.63, "residual_rms_db": 2.63,
                "residual_max_db": 7.13, "reason_summary": {},
                "mic_tier": "reference", "driver_class": "unknown", "n_repeats": 2,
                "observe_octave_summary": {"8000": -0.1, "12000": -3.2, "16000": -9.4},
            },
        },
        linearization_outcome="fitted",
    )

    summary = v2host._candidate_summary(candidate)

    assert summary["linearization_outcome"] == "fitted"
    assert summary["linearization_octaves"] == {
        "woofer": {"8000": -0.3, "12000": -1.1, "16000": -2.8},
        "tweeter": {"8000": -0.1, "12000": -3.2, "16000": -9.4},
    }
    # Only the octave dict is threaded — confirm the rest of the rich
    # LinearizationFit.to_dict() cargo (filters, residuals, ...) is NOT
    # duplicated into this session-scoped view.
    assert "filters" not in summary
    assert "residual_rms_db" not in summary


def test_candidate_summary_linearization_fields_default_empty():
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
    )

    candidate = MeasuredCrossoverCandidate(
        program_id="prog-abc",
        analysis={"alignment_confidence": 0.9,
                  "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4}},
        source_preset=_preset(),
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
    )

    summary = v2host._candidate_summary(candidate)

    assert summary["linearization_outcome"] == ""
    assert summary["linearization_octaves"] == {}


def test_candidate_summary_none_candidate_returns_none():
    assert v2host._candidate_summary(None) is None


def test_apply_failure_keeps_measure_accepted_through_the_real_persist_path():
    """SF2 (adversarial review, 2026-07-20): ``_persist_terminal_failure``
    used to reset ``accepted_phases`` for EVERY terminal code, including
    ``apply_failed`` — which made ``_phase_from_state`` land back on
    ``PHASE_CHECK`` instead of ``PHASE_APPLYING``, so the apply-step's
    specific blocked-issue nudge (``_failure_envelope``'s
    ``active_step == "apply"`` merge) could never actually render in
    production; the bug only looked fixed because an envelope-level test
    injected ``phase="applying"`` directly rather than driving the real
    conductor + persistence path. This test drives CHECK and MEASURE through
    the REAL ``conductor.consume_capture`` (exactly as ``build_v2_run_and_
    consume``'s ``consume()`` wrapper does), then calls the REAL
    ``_persist_terminal_failure`` — an apply failure does NOT invalidate the
    mic position (the §5.6 evidence-reset rationale is for a dead session),
    so MEASURE must stay accepted."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    conductor = _conductor(backend, session, phone, published=[])

    # Mirrors build_v2_run_and_consume's consume() wrapper: every
    # consume_capture is immediately followed by a persist, exactly as the
    # real relay-driven flow does — the durable file does not exist at all
    # until the first one runs.
    _walk_to_verify(conductor, persist=True)
    # A measure-only session's own terminal is DONE (nothing left to capture);
    # the durable state resolves to the review interlude, where the household
    # taps Apply — and where a REFUSED apply has to leave them.
    assert conductor.current_phase == PHASE_DONE
    assert v2host.crossover_v2_status_block()["phase"] == "review"

    v2host._persist_apply_blocked({
        "id": "measured_candidate_preset_mismatch",
        "message": "the measured candidate no longer matches the saved crossover",
    })
    v2host._persist_terminal_failure(conductor, REASON_APPLY_FAILED)

    state = v2host.load_v2_state()
    assert PHASE_CHECK in state["accepted_phases"]
    assert PHASE_MEASURE in state["accepted_phases"]
    block = v2host.crossover_v2_status_block()
    # RE-DERIVED for the split: a refused apply leaves the speaker UNAPPLIED,
    # so the honest phase is the review interlude the household tapped from —
    # not PHASE_APPLYING (nothing is applying; the apply is an HTTP request
    # that already returned) and, the point of this test, not PHASE_CHECK.
    assert block["phase"] == "review"  # NOT PHASE_CHECK

    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })
    assert env["screen"] == "fix_and_retry"
    codes = [n["code"] for n in env["nudges"]]
    assert REASON_APPLY_FAILED in codes
    assert "measured_candidate_preset_mismatch" in codes


def test_relay_timeout_still_resets_accepted_phases_through_the_real_persist_path():
    """The SF2 carve-out is SCOPED to apply_failed only — an ordinary
    session death (relay_timeout) still invalidates the mic position and
    resets to CHECK, exactly as before."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    conductor = _conductor(backend, session, phone, published=[])

    _walk_to_verify(conductor, persist=True)

    v2host._persist_terminal_failure(conductor, "relay_timeout")

    state = v2host.load_v2_state()
    assert state["accepted_phases"] == []
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_CHECK


def test_stop_landing_before_apply_commits_still_renders_applied_honestly():
    """SF1(b)'s load-bearing direction, and its render-side follow-up
    ("interleaving A", adversarial review, 2026-07-20): a Stop's
    _persist_terminal_failure(user_stopped) can land WHILE the auto-apply
    transaction is still mid-flight — at that instant applied reads False,
    so the §5.6 reset (correctly scoped away from apply_failed only, per
    SF2) fires for user_stopped and clears accepted_phases. When the
    auto-apply's OWN success then lands moments later via
    observe_apply_success, applied flips True but accepted_phases stays
    reset — _phase_from_state resolves that combination to PHASE_CHECK, not
    PHASE_VERIFY. Both existing interleaving tests
    (test_stop_before_apply_start_skips_the_dsp_mutation,
    test_stop_after_apply_start_preserves_applied_and_surfaces_undo) force
    the OTHER ordering (apply-first) and so never exercised this; this test
    drives the REAL persist functions in the stop-first order and pins BOTH
    that the durable state stays coherent (no clobbering either direction)
    AND that the render is honest even though phase/active_step disagree
    with the state fact — this must FAIL without the applied-keyed fix in
    crossover_envelope_v2._failure_envelope."""
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, phone = _mint_v2_session(backend, spec, driver_cls=None)
    conductor = _conductor(backend, session, phone, published=[])

    _walk_to_verify(conductor, persist=True)
    fingerprint = conductor.candidate.fingerprint

    # 1. The Stop lands FIRST, while the auto-apply transaction is still
    #    mid-flight (applied is still False at this instant).
    v2host._persist_terminal_failure(conductor, "user_stopped")
    state = v2host.load_v2_state()
    assert state["accepted_phases"] == []  # confirms the reset DID fire here

    # 2. The auto-apply's OWN transaction then lands (observe_apply_success
    #    is exactly what handle_v2_apply calls on success).
    v2host.observe_apply_success(fingerprint, pre_apply_profile={"stub": True})

    state = v2host.load_v2_state()
    # Coherent, not clobbered either direction (SF1(b), now pinned in BOTH
    # orderings): the transaction genuinely landed AND the stop is honestly
    # on record — even though accepted_phases never got a chance to recover.
    assert state["applied"] is True
    assert _persisted_failure(state) == {"code": "user_stopped"}
    assert state["accepted_phases"] == []

    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2

    block = v2host.crossover_v2_status_block()
    assert block["phase"] == PHASE_CHECK  # the corrupted derivation, on record
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })
    # The render must not trust that phase — applied=True is authoritative.
    assert env["screen"] == "verify_fail"
    assert "already applied" in env["verdict_text"].lower()
    labels = [a["label"] for a in env["alternate_actions"]]
    assert "Undo (restore previous sound)" in labels


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
        apply_failed=base_conductor._seams.apply_failed,
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
    # A DISTINCT failure persisted — not relay_timeout — and its underlying
    # refusal slug rode out with it (issue #1820: the old collapse erased which
    # of program_unplayable's several causes actually fired).
    state = v2host.load_v2_state()
    assert _persisted_failure(state) == {
        "code": "program_unplayable",
        "refusals": ["program_channel_peak_over_cap"],
    }
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


def test_v2_volume_recovery_active_tracks_needs_recovery():
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    assert v2host.v2_volume_recovery_active() is True

    class _Clean:
        needs_recovery = False

    v2host.set_volume_plan_for_tests(_Clean())
    assert v2host.v2_volume_recovery_active() is False


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


def _assert_full_cleanup(plan, cam, log, backend, session, *, code, refusals=None):
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
    # and the same failure persisted for the wizard envelope. A program-family
    # failure also carries its underlying admission-refusal slugs (issue #1820
    # forensics); everything else persists the code alone.
    state = v2host.load_v2_state()
    expected = {"code": code}
    if refusals:
        expected["refusals"] = list(refusals)
    assert _persisted_failure(state) == expected


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
        apply_failed=conductor._seams.apply_failed,
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
    _assert_full_cleanup(
        plan, cam, log, backend, session, code="program_unplayable",
        refusals=["program_channel_peak_over_cap"],
    )


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


def _probe_bind_production_play_config_dir(monkeypatch, tmp_path) -> dict[str, Any]:
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
    protection = {"woofer": (), "tweeter": ()}

    def _emit(*args, **kwargs):
        captured["emitter_protection"] = kwargs["protection_sections_by_role"]
        return "placeholder-graph-yaml"

    monkeypatch.setattr(camilla_yaml_mod, "emit_active_speaker_program_config", _emit)
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
        protection_sections_by_role=protection,
    )
    with pytest.raises(_ShortCircuit):
        play(PHASE_CHECK, object())
    captured["source_protection"] = protection
    return captured


def test_bind_production_play_default_config_dir_matches_ssot(monkeypatch, tmp_path):
    """W6 hardware run 3 finding F: bind_production_play's config_dir default
    must resolve to the SAME canonical constant every sibling DSP writer
    (commissioning apply/verify, web_commissioning, correction_setup) locks
    against — jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR — not
    the stale "/etc/camilladsp" literal this binding shipped with. An SSOT
    pin: if either side's default drifts away from the other, this fails."""
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR

    resolved = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)["config_dir"]
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

    resolved = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)["config_dir"]
    assert str(dsp_apply_lock_path(resolved)).startswith("/var/lib/camilladsp")


def test_bind_production_play_threads_exact_protection_to_emitter(monkeypatch, tmp_path):
    captured = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)
    assert captured["emitter_protection"] is captured["source_protection"]


# --- Issue #1976: the summed-sweep stimulus must also land at a stable name --


def test_cloud_measure_play_also_persists_canonical_summed_program_wav(
    monkeypatch, tmp_path
):
    """Issue #1976: a measure-stage session that walks the pre-apply cloud
    group (CLOUD_MEASURE) plays the SAME excitation object a literal VERIFY
    capture would — ``_program_for_phase`` in crossover_v2_flow.py returns
    ``self._verify_program`` for every phase in ``SUMMED_SWEEP_PHASES`` — but
    a session that never arms PHASE_VERIFY itself used to leave that reusable
    stimulus discoverable only under its cloud-phase filename.

    Confirmed against real bench data
    (``captures/bench-20260730/bundle-d76b55bc6b67``, a measure-stage-only
    session): ``cloud_measure_program.wav`` was on disk, no summed-sweep
    stimulus was recoverable under a predictable name.

    ``_play`` must now ALSO persist ``summed_program.wav`` alongside the
    phase-named file whenever the armed phase is CLOUD_MEASURE, CLOUD_VERIFY,
    or VERIFY itself. This is a NEW, dedicated filename — never
    ``verify_program.wav`` — because corpus-index tooling derives "which
    phases this bundle reached" from which ``{phase}_program.wav`` files
    exist; reusing that name would make a cloud-only bundle false-report
    having reached VERIFY (adversarial-gate SF1, PR #2028)."""
    import jasper.active_speaker.program_playback as program_playback_mod
    import jasper.audio_measurement.program as program_mod

    written: list[Path] = []

    def fake_write_program_wav(path, program):
        written.append(Path(path))
        Path(path).write_bytes(b"fake-wav-bytes")

    async def fake_verified_program_aplay(bundle_dir, artifact, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(program_mod, "write_program_wav", fake_write_program_wav)
    monkeypatch.setattr(
        program_playback_mod, "verified_program_aplay", fake_verified_program_aplay
    )
    _patch_measurement_window(monkeypatch, [])

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        relay_session_id="cap_verify_persist_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
    )
    play(PHASE_CLOUD_MEASURE, object())

    session_dir = tmp_path / "crossover_v2" / "cap_verify_persist_probe"
    assert (session_dir / "cloud_measure_program.wav").exists()
    # The phase-named file's presence must stay a reliable phase-reach signal:
    # CLOUD_MEASURE alone must NOT create a verify_program.wav that would make
    # this cloud-only session false-report having reached VERIFY.
    assert not (session_dir / "verify_program.wav").exists()
    assert (session_dir / "summed_program.wav").exists(), (
        "CLOUD_MEASURE must also persist the canonical summed_program.wav — "
        "a session that never arms a literal VERIFY capture would otherwise "
        "leave its reusable stimulus un-replayable offline (#1976)"
    )
    # Written exactly once each — the alias is not re-derived or re-rendered,
    # just a second write of the SAME program object already validated above.
    assert written == [
        session_dir / "cloud_measure_program.wav",
        session_dir / "summed_program.wav",
    ]


def test_verify_play_does_not_overwrite_existing_summed_program_wav(
    monkeypatch, tmp_path
):
    """A CLOUD_VERIFY position captured after the session's own VERIFY anchor
    must not re-render (or clobber) the summed_program.wav VERIFY already
    wrote — the alias write is a fill-if-absent, not an unconditional write,
    so repeated cloud positions in one session cost one extra WAV write, not
    N."""
    import jasper.active_speaker.program_playback as program_playback_mod
    import jasper.audio_measurement.program as program_mod

    write_calls: list[Path] = []

    def fake_write_program_wav(path, program):
        write_calls.append(Path(path))
        Path(path).write_bytes(b"fake-wav-bytes")

    async def fake_verified_program_aplay(bundle_dir, artifact, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(program_mod, "write_program_wav", fake_write_program_wav)
    monkeypatch.setattr(
        program_playback_mod, "verified_program_aplay", fake_verified_program_aplay
    )
    _patch_measurement_window(monkeypatch, [])

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        relay_session_id="cap_verify_no_clobber_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
    )
    play(PHASE_VERIFY, object())
    play(PHASE_CLOUD_VERIFY, object())

    session_dir = tmp_path / "crossover_v2" / "cap_verify_no_clobber_probe"
    assert write_calls == [
        session_dir / "verify_program.wav",
        session_dir / "summed_program.wav",
        session_dir / "cloud_verify_program.wav",
    ], (
        "summed_program.wav must be written exactly once, by VERIFY itself "
        "(the phase-named verify_program.wav write, then the summed_program.wav "
        "fill) — CLOUD_VERIFY's fill-if-absent check must skip it"
    )


def test_summed_program_wav_persist_failure_is_best_effort(monkeypatch, tmp_path):
    """A full disk or permissions fault writing the summed_program.wav
    diagnostic copy must never abort the measurement (adversarial-gate SF4,
    PR #2028) — matches the ``_maybe_retain_capture`` convention elsewhere in
    this module: catch, log at WARN, keep going. The phase-named WAV (the
    file actually played) must still have been written before the failure."""
    import jasper.active_speaker.program_playback as program_playback_mod
    import jasper.audio_measurement.program as program_mod

    calls: list[Path] = []

    def flaky_write_program_wav(path, program):
        calls.append(Path(path))
        if Path(path).name == "summed_program.wav":
            raise OSError("ENOSPC: no space left on device")
        Path(path).write_bytes(b"fake-wav-bytes")

    async def fake_verified_program_aplay(bundle_dir, artifact, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(program_mod, "write_program_wav", flaky_write_program_wav)
    monkeypatch.setattr(
        program_playback_mod, "verified_program_aplay", fake_verified_program_aplay
    )
    _patch_measurement_window(monkeypatch, [])

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        relay_session_id="cap_summed_persist_fails_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
    )
    # Must not raise — the OSError from the summed_program.wav write is
    # swallowed, not propagated into the measurement.
    play(PHASE_CLOUD_MEASURE, object())

    session_dir = tmp_path / "crossover_v2" / "cap_summed_persist_fails_probe"
    assert (session_dir / "cloud_measure_program.wav").exists(), (
        "the phase-named WAV that was actually played must persist even "
        "when the best-effort diagnostic copy fails"
    )
    assert not (session_dir / "summed_program.wav").exists()


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
        apply_failed=conductor._seams.apply_failed,
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
    assert _persisted_failure(state) == {"code": "relay_timeout"}


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
        apply_failed=conductor._seams.apply_failed,
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
    # …and it NAMES which clock ran out (work order D8, issue #1807). Without
    # this the phone renders a session-over event as renderPlanExhausted — "the
    # speaker reached its measurement attempt limit" — which is a third thing
    # entirely: no attempt limit was reached, a step budget expired. Both a
    # step timeout and a dead relay session persist as the same failure code,
    # so this field is the only place the difference survives to the household.
    assert events[-1]["budget"] == "step"


def test_a_time_budget_expiry_logs_which_clock_ran_out_and_what_survived(
    monkeypatch, caplog,
):
    """AGENTS.md's no-silent-failure rule, applied to D8's disclosure: a
    user-visible expiry gets a NAMED line in the shipped
    ``correction.crossover_v2_*`` namespace, carrying which budget expired and
    what the expiry preserved. A disclosure nobody can grep for is not a
    disclosure."""
    import logging

    from jasper.capture_relay import session as session_mod

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, _phone = _mint_v2_session(backend, spec, driver_cls=None)

    class _NoPhone:
        begun = (1, 1)

    conductor = _conductor(backend, session, _NoPhone(), published=[])
    monkeypatch.setattr(session_mod, "purge", lambda *a, **k: None)

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    runner = _build_runner(
        conductor, VolumeRecorder(), poll_interval_s=0.01, timeout_s=0.2
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(CaptureTimeout):
            _run(runner, client, session)

    lines = [
        r.getMessage() for r in caplog.records
        if "correction.crossover_v2_time_budget_expired" in r.getMessage()
    ]
    assert len(lines) == 1, caplog.text
    # WHICH clock, WHICH relay phase it died in, and what survived.
    assert "budget=step" in lines[0]
    assert "phase=" in lines[0]
    assert "accepted_phases=" in lines[0]


def test_a_transport_death_tells_the_phone_no_clock_ran_out(monkeypatch):
    """Issue #2083. A session killed by the TRANSPORT ran out of neither clock,
    and the Pi must say that in the third value rather than by omission.

    Omitting ``budget`` is not neutral: the phone renders a session-over event
    with no named clock as ``renderPlanExhausted`` — "the speaker reached its
    measurement attempt limit" — which is false here. No attempt limit was
    reached and no budget expired; the relay went away. The three outcomes want
    three different things from the household, so the wire carries three values.
    """
    from jasper.capture_relay import session as session_mod

    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)
    client, session, _phone = _mint_v2_session(backend, spec, driver_cls=None)

    class _NoPhone:
        begun = (1, 1)

    conductor = _conductor(backend, session, _NoPhone(), published=[])
    monkeypatch.setattr(session_mod, "purge", lambda *a, **k: None)

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # A relay that has stopped answering. The runner's own transient grace
    # (STATUS_POLL_TRANSIENT_GRACE_S) is what decides how long this is endured
    # — pinned in tests/test_capture_relay_plan.py — and is collapsed here so
    # the subject is only what the Pi TELLS THE PHONE once it has given up.
    monkeypatch.setattr(session_mod, "STATUS_POLL_TRANSIENT_GRACE_S", 0.0)

    def status(*args, **kwargs):
        raise TimeoutError("relay unreachable")

    monkeypatch.setattr(client, "status", status)

    runner = _build_runner(
        conductor, VolumeRecorder(), poll_interval_s=0.01, timeout_s=20.0
    )
    with pytest.raises(TimeoutError):
        _run(runner, client, session)

    events = backend.host_events[session.session_id]
    assert events[-1]["phase"] == "capture_set_exhausted"
    # Present and explicit — NOT absent. `expired_time_budget` named no clock
    # for this death, and "none" is how that is published.
    assert events[-1]["budget"] == session_mod.TIME_BUDGET_NONE == "none"


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
    against the exact preset the seam will recompile from the same files.

    W6.11: the crossover-preview file is no longer hand-built and written
    directly — that sidestepped the exact bug this wave fixed (only
    ``/sound/``'s Preview button ever generated it; v2 never did). It is
    produced by ``v2host.ensure_crossover_preview_ready()``, the real
    session-start seam, so this fixture proves the same machinery a browser
    session would drive."""
    from jasper.active_speaker import compile_preset_from_crossover_preview
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

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    preview = v2host.ensure_crossover_preview_ready()

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
        analysis={"epsilon_ppm": 29.924, "predicted_ripple_db": 29.6952,
              "alignment_confidence": 0.82,
              "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4}},
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

    payload = _apply(
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


# --- #1811: the apply boundary declares its level move, and moves no level ------


class _FakeApplyAndVolumeCam(_FakeApplyCam):
    """``_FakeApplyCam`` plus the main-volume RPCs the session plan drives, so
    ONE ``camilla_factory`` can both apply and hold the session volume — which
    is what lets these tests assert the commanded level did NOT move."""

    vol = -20.0

    async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
        type(self).vol = float(db)
        return True

    async def get_volume_db(self, best_effort: bool = False) -> float:
        return type(self).vol


# The offset the apply declares for ``_boosting_candidate(boost_db=6.0)``:
# the PEAK-rule charge (#1808) for a +6 dB bell at 900 Hz, which sits inside
# the woofer's own passband so the crossover credits back almost nothing while
# the headroom margin adds ~1 dB. Rounded to 3 dp by the persist path.
_APPLY_OFFSET_DB = -6.86


def _boosting_candidate(preset, *, boost_db: float):
    """The run-6 candidate plus a Layer-1a boost — the shape that charges
    program headroom and therefore moves the chain at apply time."""
    return replace(
        _run6_measured_candidate(preset),
        linearization={
            "woofer": {
                "filters": [
                    {
                        "biquad_type": "Peaking",
                        "freq": 900.0,
                        "q": 3.0,
                        "gain": boost_db,
                    },
                ],
                "headroom_cost_db": boost_db,
            },
        },
        linearization_outcome="fitted",
    )


def _open_session_volume_plan(*, household_db: float, measurement_db: float = -20.0):
    """An OPEN plan holding ``measurement_db``, as a live session would."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    _FakeApplyAndVolumeCam.vol = household_db
    plan = SessionVolumePlan()
    cam = _FakeApplyAndVolumeCam()
    assert (
        asyncio.run(plan.open(measurement_db, cam.set_volume_db, cam.get_volume_db))
        is SessionVolumeOpenResult.OPENED
    )
    assert _FakeApplyAndVolumeCam.vol == measurement_db
    v2host.set_volume_plan_for_tests(plan)
    return plan


def test_apply_declares_its_level_move_and_never_touches_the_volume(
    monkeypatch, tmp_path,
):
    """#1811, through the REAL apply seam.

    The applied graph absorbs its correction's boost as a pre-split common
    attenuation, so the same commanded volume drives the speaker quieter the
    instant the config swaps. That absorption is the excitation-safety property
    (``camilla_yaml``: the boosted band lands "at or under unity no matter how
    deep the correction"), so the apply must **declare** the move for the
    analysis and must **not** compensate it at the main volume — compensating
    would put the boosted band over the driver's excitation cap by the
    branch's own boost.

    Three things must hold:

    * the declared offset is the emitter's OWN delta (here −6 dB, read off the
      applied profile, not a constant);
    * the commanded session volume is completely untouched;
    * it is durable BEFORE ``observe_apply_success`` returns — that call sets
      the ``applied`` flag which releases VERIFY's deferred hold, and the
      probe seam reads the offset off the same state one capture later.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _boosting_candidate(preset, boost_db=6.0)
    plan = _open_session_volume_plan(household_db=-6.0)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyAndVolumeCam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    # The PEAK-rule charge (#1808) for a +6 dB bell at 900 Hz — inside the
    # woofer's own passband, so the crossover credits back almost nothing and
    # the headroom margin adds ~1 dB on top. Verified by running, not derived
    # here: what this test pins is that the DECLARED number is the emitter's
    # own, whatever the charge rule of the day makes it.
    assert payload["expected_post_apply_offset_db"] == _APPLY_OFFSET_DB
    # Durable, and readable through the very seam the conductor's probe uses.
    assert v2host.load_v2_state()["expected_post_apply_offset_db"] == _APPLY_OFFSET_DB
    assert v2host._applied_offset_gate() == _APPLY_OFFSET_DB
    # The speaker's commanded level did not move. This is the safety claim.
    assert _FakeApplyAndVolumeCam.vol == -20.0
    assert plan.measurement_volume_db == -20.0


def test_a_blocked_apply_declares_no_offset_and_moves_no_level(monkeypatch, tmp_path):
    """An apply the seam refused changed no graph, so there is no move to
    declare — and the probe seam must keep reporting "nothing known" (0.0)
    rather than an offset from a transaction that never landed."""
    from jasper.active_speaker.design_draft import build_design_draft

    from tests.test_active_speaker_baseline_profile import _research

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _boosting_candidate(preset, boost_db=6.0)
    plan = _open_session_volume_plan(household_db=-6.0)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    # Move the crossover design out from under the reviewed candidate, exactly
    # as the preset-mismatch test above does, so the seam blocks.
    moved_research = _research()
    moved_research["crossover_candidates"][0]["frequency_hz"] = 3000
    (tmp_path / "design_draft.json").write_text(
        json.dumps(
            build_design_draft(
                topology,
                driver_research=moved_research,
                created_at="2026-07-18T12:30:00Z",
            )
        ),
        encoding="utf-8",
    )
    v2host.ensure_crossover_preview_ready()

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyAndVolumeCam,
    )

    assert payload["status"] == "blocked"
    assert "expected_post_apply_offset_db" not in payload
    assert v2host._applied_offset_gate() == 0.0
    assert _FakeApplyAndVolumeCam.vol == -20.0
    assert plan.measurement_volume_db == -20.0


def test_the_declared_offset_survives_persist_conductor_state(monkeypatch, tmp_path):
    """The durable seam BETWEEN the writer and the reader (#1811 blocker).

    ``observe_apply_success`` writes the offset and the probe's seam reads it,
    and both halves were pinned — but nothing crossed the
    ``persist_conductor_state`` call that happens on every capture in between.
    It rebuilds the state from a fresh dict literal, so the offset was erased
    on every single call while ``applied`` survived: the CLOUD_VERIFY probe
    (the one with the spatial arm AND rollback authority) would have graded
    the apply's own headroom charge blind and could roll a healthy correction
    back, and every "Try again" re-arm — which persists under a brand-new
    session id — would have been blind too.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _boosting_candidate(preset, boost_db=6.0)
    _open_session_volume_plan(household_db=-6.0)
    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })
    _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyAndVolumeCam,
    )
    assert v2host._applied_offset_gate() == _APPLY_OFFSET_DB

    # One more capture in the SAME session, then the re-arm's brand-new one.
    for session_id in ("cap_run6", "cap_rearm"):
        v2host.persist_conductor_state(
            _StubConductor(session_id), failure_code=None,
        )
        assert v2host._applied_offset_gate() == _APPLY_OFFSET_DB, session_id


class _StubConductor:
    """The minimum ``persist_conductor_state`` reads off a conductor."""

    candidate = None
    verify_outcome = None
    verify_code = None
    verify_gate = None
    verify_evidence = None
    verify_graded_band_hz = None
    verify_frame = None
    verify_claims = None
    delta_probe = None
    measure_predicted_sum = None
    measure_gate_window_ms = None
    verify_pilot_transfer_reference = None
    verify_level_reference_reset = None
    session_phases: tuple = ()

    def __init__(self, session_id: str = "s1", *, applied: bool = True) -> None:
        self._session_id = session_id
        self._applied = applied

    def snapshot(self):
        return SimpleNamespace(
            session_id=self._session_id, accepted_phases=(), session_phases=(),
            tier="", applied=self._applied, gain_plan_db=None,
            candidate_fingerprint=None, cloud_close="",
        )


def test_every_host_owned_apply_key_survives_persist_conductor_state():
    """The drift guard for a bug class that has now shipped THREE times.

    ``persist_conductor_state`` rebuilds the durable state from a fresh dict
    literal, so any key whose value comes from ``observe_apply_success`` —
    which the conductor neither produces nor reads — is erased unless a
    carry-forward line exists for it. That has been a P0 for
    ``pre_apply_profile`` (W6.12), for ``cloud`` (PR-4 B1), and for
    ``expected_post_apply_offset_db`` (#1811).

    The host-owned set is derived MECHANICALLY rather than listed: a key is
    host-owned when the apply path gives it a value and a persist driven by
    the conductor ALONE (empty prior, nothing to carry) cannot regenerate one.
    A fourth such key fails this test the moment it is written, without anyone
    having to remember to extend a list.

    **If your new key legitimately wants session scoping** — ``apply_blocked``
    does, because a blocked apply refuses the deferred VERIFY outright and so
    never faces a new-session rebind — put it in ``persist_conductor_state``'s
    session-gated branch and add an exception here WITH that reason. Reaching
    for the exception first is the mistake this guard exists to make visible.

    The re-arm's brand-new session id is the hard case, and the one all three
    bugs hit, so that is what this crosses.
    """
    # (1) What a persist can rebuild from the conductor alone, with an empty
    # prior so nothing can be carried forward.
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(_StubConductor("s1"), failure_code=None)
    from_conductor_alone = {
        key for key, value in (v2host.load_v2_state() or {}).items()
        if value is not None
    }

    # (2) What the apply path establishes on top of it.
    v2host.save_v2_state({"session_id": "s1", "applied": False})
    v2host.observe_apply_success(
        "fp",
        pre_apply_profile={"kind": "prior", "config": {"path": "/tmp/x.yml"}},
        expected_post_apply_offset_db=-22.458,
    )
    after_apply = dict(v2host.load_v2_state() or {})
    host_owned = {
        key for key, value in after_apply.items()
        if value is not None and key not in from_conductor_alone
    }
    # The derivation must actually see this PR's key — a guard that derives an
    # empty set proves nothing.
    assert "expected_post_apply_offset_db" in host_owned
    assert "pre_apply_profile" in host_owned

    # (3) Cross the seam under the re-arm's BRAND-NEW session id.
    v2host.persist_conductor_state(_StubConductor("cap_rearm"), failure_code=None)
    after_persist = v2host.load_v2_state() or {}
    for key in sorted(host_owned):
        assert after_persist.get(key) == after_apply[key], (
            f"{key!r} is written by the apply path and erased by "
            "persist_conductor_state — add a carry-forward line for it"
        )


def test_the_probe_verdict_is_persisted_even_on_a_pass():
    """#1811 SF1: the durable record the done screen's caveat and /state read.

    Persisted on EVERY outcome, not only a failing one — a ``level_mismatch``
    only ever occurs alongside a pass (it produces no refusal), so gating this
    on failure would have persisted it exactly never.
    """
    probe = SimpleNamespace(
        verdict="level_mismatch", reason="uncommanded_level_shift",
        expected_offset_db=-22.458, residual_offset_db=-4.0,
    )
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "pass"
    conductor.delta_probe = probe
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(conductor, failure_code=None)

    persisted = (v2host.load_v2_state() or {})["verify"]["delta_probe"]
    assert persisted == {
        "verdict": "level_mismatch",
        "reason": "uncommanded_level_shift",
        "expected_offset_db": -22.458,
        "residual_offset_db": -4.0,
    }
    # A conductor with no probe writes no key rather than an empty claim.
    plain = _StubConductor("s1")
    plain.verify_outcome = "pass"
    v2host.persist_conductor_state(plain, failure_code=None)
    assert "delta_probe" not in (v2host.load_v2_state() or {})["verify"]


def test_the_graded_band_is_persisted_even_on_a_pass():
    """#1868: what VERIFY actually graded, on every outcome.

    It used to ride ``verify.evidence``, which is persisted only when the
    outcome is NOT a pass — so the state behind the "Verified." screen, the
    one place the household is told the result is good, was the one place
    that never carried the band bounding that word. Same always-persisted
    shape as ``delta_probe`` above, for the same reason.
    """
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "pass"
    conductor.verify_evidence = {"max_db": 0.9, "rms_db": 0.4, "tolerance_db": 1.5}
    conductor.verify_graded_band_hz = [2000.0, 4000.0]
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(conductor, failure_code=None)

    verify = (v2host.load_v2_state() or {})["verify"]
    assert verify["graded_band_hz"] == [2000.0, 4000.0]
    # The evidence block's pass-only suppression is UNCHANGED — a pass keeps
    # its lean shape; only the band, which bounds the claim, is added.
    assert "evidence" not in verify

    # A verify that reached no tracking comparison writes no key rather than
    # an empty claim — absent means "graded nothing", never "graded all".
    plain = _StubConductor("s1")
    plain.verify_outcome = "fail"
    v2host.persist_conductor_state(plain, failure_code=None)
    assert "graded_band_hz" not in (v2host.load_v2_state() or {})["verify"]


def test_the_section_7_claims_are_persisted_even_on_a_pass():
    """R18 (#1868): WHICH claims were proved, on every outcome.

    Same always-persisted shape and argument as the graded band above, one
    step further: that band bounds how wide the tracking claim is, this says
    which claims exist at all — and two of the four are structurally
    not-evaluated, because VERIFY plays one summed sweep.
    """
    claims = {
        "woofer_branch": {
            "status": "not_evaluated", "reason": "no_per_branch_verify_capture",
        },
        "hf_branch": {
            "status": "not_evaluated", "reason": "no_per_branch_verify_capture",
        },
        "integration": {"status": "pass", "max_db": 0.069, "tolerance_db": 1.5},
        "absolute": {
            "status": "pass", "max_db": 0.69, "tolerance_db": 2.0,
            "band_hz": [1000.0, 4000.0], "worst_db": 0.69, "worst_hz": 1050.0,
        },
    }
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "pass"
    conductor.verify_claims = claims
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(conductor, failure_code=None)
    assert (v2host.load_v2_state() or {})["verify"]["claims"] == claims

    # Graded nothing ⇒ no key: absent means "nothing was judged", not "passed".
    plain = _StubConductor("s1")
    plain.verify_outcome = "fail"
    v2host.persist_conductor_state(plain, failure_code=None)
    assert "claims" not in (v2host.load_v2_state() or {})["verify"]


def test_the_compared_frame_is_persisted_even_on_a_pass():
    """Rung P1: the frame the comparison spanned, on every outcome.

    Same always-persisted shape and same argument as the graded band above. A
    pass is precisely when an undisclosed tilt is dangerous: on the 2026-07-29
    corpus one −0.79 dB/octave frame tilt was 84 % of what the flow reported as
    prediction error, and nothing in a "Verified." state file would have said
    so.
    """
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "pass"
    conductor.verify_frame = {
        "offset_db": -0.75, "tilt_db_per_octave": -0.79,
        "rms_db_tilt_removed": 1.34, "max_db_tilt_removed": 0.62,
    }
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(conductor, failure_code=None)

    verify = (v2host.load_v2_state() or {})["verify"]
    assert verify["frame"] == conductor.verify_frame

    # A verify that fitted no frame writes no key — absent means "no frame was
    # measured", never "the frames agreed".
    plain = _StubConductor("s1")
    plain.verify_outcome = "fail"
    v2host.persist_conductor_state(plain, failure_code=None)
    assert "frame" not in (v2host.load_v2_state() or {})["verify"]


def test_the_gate_disclosure_is_persisted_even_on_a_pass():
    """Issue #1966: the sentence that says what the gate DID has to survive the
    hop to the wizard, or it renders nowhere — which is exactly where it stood
    before R9 (an off-by-default operator sidecar and a bundle artifact, both
    dropped by every projection a screen reads).

    Same always-persisted shape and same argument as the frame above: a pass is
    when nobody would otherwise ask how much of the response the comparison
    could see. The persisted value is the CONDUCTOR's record, byte-for-byte —
    the host copies it, it does not re-derive a sentence of its own.
    """
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "pass"
    conductor.verify_gate = {
        "disclosure": (
            "no reflection found; window capped at the 7.00 ms search ceiling, "
            "so nothing was gated out, valid above 357 Hz"
        ),
        "reflection_measured": False,
    }
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(conductor, failure_code=None)

    verify = (v2host.load_v2_state() or {})["verify"]
    assert verify["gate"] == conductor.verify_gate

    # No gating block, no key: absent means "this capture was never gated",
    # never "the gate found nothing".
    plain = _StubConductor("s1")
    plain.verify_outcome = "fail"
    v2host.persist_conductor_state(plain, failure_code=None)
    assert "gate" not in (v2host.load_v2_state() or {})["verify"]


def test_the_verify_code_is_persisted_beside_its_outcome():
    """Issue #1974: "inconclusive" is reached by two verdicts with no shared
    mechanism, and the done screen names the cause from the code.

    It is NOT read from ``failure.code``: that is the most recent rejection of
    any phase, and the second persist below — the ordinary shape of a session
    that fails VERIFY and then writes again with nothing failing — nulls the
    failure block while the verify outcome stands. A screen reading it there
    would have lost the cause exactly when it needed it.
    """
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "inconclusive"
    conductor.verify_code = "verify_level_shift"
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(
        conductor, failure_code="verify_level_shift",
    )
    state = v2host.load_v2_state() or {}
    assert state["verify"]["code"] == "verify_level_shift"

    v2host.persist_conductor_state(conductor, failure_code=None)
    state = v2host.load_v2_state() or {}
    assert state["failure"] is None
    assert state["verify"]["code"] == "verify_level_shift"

    # A pass carries no code — nothing rejected it.
    passing = _StubConductor("s1")
    passing.verify_outcome = "pass"
    v2host.persist_conductor_state(passing, failure_code=None)
    assert "code" not in (v2host.load_v2_state() or {})["verify"]


def test_applied_offset_gate_reports_nothing_known_rather_than_guessing():
    """``0.0`` is the honest answer for an absent or malformed value — the
    probe then leaves the whole shift visible in ``residual_offset_db``
    instead of claiming it was accounted for."""
    v2host.save_v2_state({"session_id": "s", "applied": True})
    assert v2host._applied_offset_gate() == 0.0
    for bad in ("loud", None, True, float("nan"), float("inf")):
        v2host.save_v2_state({
            "session_id": "s", "applied": True,
            "expected_post_apply_offset_db": bad,
        })
        assert v2host._applied_offset_gate() == 0.0


def test_a_pre_pr6b_candidate_payload_still_applies(monkeypatch, tmp_path):
    """Era tolerance at the LIVE surface, not just in ``from_mapping``.

    The blocker this pins: ``to_dict()`` always writes ``exclusion_evidence``,
    so a ``candidate.json`` published by a build that predates the field fails
    ``from_mapping``'s reopen comparison unless it is setdefaulted — and that
    comparison is on the apply path (``handle_v2_apply`` →
    ``_reopen_candidate_artifact`` → ``from_mapping``). The household-visible
    symptom was a ``candidate_tampered`` refusal telling them their persisted
    correction had been altered when the file was merely older than the field.

    Drives the SAME real ``apply_baseline_profile`` path as the sibling test
    above, with the key deleted from the payload — it must load, keep its
    fingerprint, and apply.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    pre_pr6b_payload = dict(candidate.to_dict())
    assert "exclusion_evidence" in pre_pr6b_payload
    del pre_pr6b_payload["exclusion_evidence"]  # the pre-PR-6b persisted shape

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": pre_pr6b_payload,
        },
        _bg_run_async,
        _FakeApplyCam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    assert v2host._applied_gate() is True


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
        _apply(
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
    underneath (a second /sound/ save, followed by a fresh v2 session start
    that re-ensures the preview) before Apply landed. The seam's own
    ``measured_candidate_preset_mismatch`` gate must refuse — never silently
    apply the wrong preset — and Finding N's wiring must name that issue and
    persist it for the review_apply nudge, instead of 200 + silent no-op."""
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
    # crossover frequency) after this candidate was measured — write the
    # moved DESIGN DRAFT directly (that part is /sound/'s job, out of this
    # wave's scope), then re-ensure the preview through the REAL seam, the
    # same way a fresh v2 session start would after that save.
    moved_research = _research()
    moved_research["crossover_candidates"][0]["frequency_hz"] = 3000
    moved_draft = build_design_draft(
        topology, driver_research=moved_research, created_at="2026-07-18T12:30:00Z",
    )
    (tmp_path / "design_draft.json").write_text(
        json.dumps(moved_draft), encoding="utf-8"
    )
    v2host.ensure_crossover_preview_ready()

    payload = _apply(
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
        analysis={"epsilon_ppm": 5.0, "predicted_ripple_db": 1.2,
              "alignment_confidence": 0.82,
              "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4}},
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
    # #1666: "prior's own file" is prior's OWN content-addressed sibling, not
    # config_path (the canonical name, which the successful apply above just
    # promoted a copy onto -- still prior's bytes at this point, but via a
    # promote, not because nothing ever touched it).
    prior_config_text = Path(
        prior_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    assert config_path.read_text(encoding="utf-8") == prior_config_text

    # Item 3 (#1605): make the candidate pruner ACTIVE at the run-8 apply below,
    # with the prior (the Undo target) the OLDEST candidate on disk. Without
    # _apply_baseline_profile_locked passing it as also_protect, the newest-K
    # mtime prune would evict it and the Undo at the end of this test would fail
    # with restore_target_missing — so the prior surviving + the restore
    # succeeding is the END-TO-END proof that the real extraction protects the
    # real Undo-target path (the isolated pruner half is pinned by
    # test_prune_keeps_the_protected_undo_target_even_when_it_is_oldest).
    import os as _os
    import time as _time

    from jasper.active_speaker import baseline_profile as _bp
    prior_config_path = Path(prior_payload["profile"]["config"]["path"])
    _now = _time.time()
    _os.utime(prior_config_path, (_now - 10_000, _now - 10_000))
    for _i in range(_bp._MAX_BASELINE_CANDIDATE_FILES + 2):
        _orphan = config_path.with_name(
            f"active_speaker_baseline_candidate_orphan{_i:03d}.yml"
        )
        _orphan.write_text(f"# orphan {_i}\n", encoding="utf-8")
        _os.utime(_orphan, (_now - 1000, _now - 1000))

    run8_candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_run8",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": run8_candidate.fingerprint},
        "applied": False,
    })
    apply_payload = _apply(
        {
            "expected_candidate_fingerprint": run8_candidate.fingerprint,
            "candidate": run8_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert apply_payload["status"] == "applied", apply_payload.get("issues")
    # The prior (Undo target) survived the run-8 prune despite being the oldest
    # candidate on disk — protected by also_protect (#1605).
    assert prior_config_path.exists()
    run8_config_path = Path(apply_payload["profile"]["config"]["path"])
    assert run8_config_path != config_path
    run8_config_text = run8_config_path.read_text(encoding="utf-8")
    # The run-8 apply must not have clobbered the prior profile's own file --
    # it landed on its own sibling, still readable unmutated.
    assert prior_config_text == Path(
        prior_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    # The canonical name is a promoted COPY of whichever candidate applied
    # most recently -- it now tracks run-8, not prior.
    assert config_path.read_text(encoding="utf-8") == run8_config_text

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
    # Restore reloads prior's own sibling and re-promotes canonical onto it.
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


def test_second_apply_pre_apply_profile_survives_the_deferred_verify_rearm(
    monkeypatch, tmp_path,
):
    """W6.12 P0 regression: the durable Undo stash must survive the deferred
    VERIFY that always auto-arms right after every apply.

    Drives handle_v2_apply TWICE in sequence, both through the production
    seam (not seeded state) — a v2-written prior profile ("run 1"), then a
    v2 apply over it ("run 2 over run 1"), matching the round-4 hardware
    differential. Round-4 found ``pre_apply_profile: null`` after EVERY
    apply on real hardware even though a standalone compose probe showed the
    host DOES attach ``applied_recomposition_profile`` — the drop was never
    in ``handle_v2_apply``/``observe_apply_success`` (both prove correct
    here); it was that ``persist_conductor_state`` built a fresh state dict
    that never carried ``pre_apply_profile`` forward, so the deferred VERIFY
    that auto-arms after every apply (``prepare_v2_verify`` mints a NEW relay
    session id and immediately calls ``persist_conductor_state`` to "rebind"
    it — see its own call site) wiped the just-stashed pointer before a
    household could ever reach the verify_fail Undo screen. This test
    reproduces that exact rebind call (a real ``CrossoverV2Conductor``, not a
    mock) between each apply and the next, and pins that the stash survives
    it."""
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_VERIFY,
        CrossoverV2Conductor,
        V2FlowSeams,
    )

    from tests.test_crossover_v2_conductor import CAPS, FC_HZ, SESSION_VOLUME_DB, _roles

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    config_path = tmp_path / "active_speaker_baseline.yml"
    state_path = tmp_path / "baseline_profile.json"

    def _simulate_deferred_verify_rearm(*, verify_session_id: str) -> None:
        """Exactly what ``prepare_v2_verify``'s ``_open`` does: mint a fresh
        conductor bound to a NEW relay session id, applied=True, and
        immediately persist it ("Keep the durable candidate/applied facts;
        rebind the session id.") — the real production seam this regression
        traces to, not a synthetic stand-in."""
        conductor = CrossoverV2Conductor(
            session_id=verify_session_id,
            source_preset=preset,
            roles_bands=_roles(),
            fc_hz=FC_HZ,
            driver_caps_dbfs=CAPS,
            session_volume_db=SESSION_VOLUME_DB,
            seams=V2FlowSeams(
                play=lambda *a, **k: None,
                analyze=lambda *a, **k: None,
                publish_check=lambda *a, **k: None,
                publish_candidate=lambda *a, **k: None,
                apply_complete=v2host._applied_gate,
                apply_failed=v2host._apply_failure_gate,
            ),
            driver_spacing_m=0.15,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
            index_phase_map={1: PHASE_VERIFY},
        )
        v2host.persist_conductor_state(conductor, failure_code=None)

    # --- run 1: a v2-written apply, no pre-existing profile to restore to ---
    run1_candidate = _prior_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_run1",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": run1_candidate.fingerprint},
        "applied": False,
    })
    run1_payload = _apply(
        {
            "expected_candidate_fingerprint": run1_candidate.fingerprint,
            "candidate": run1_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert run1_payload["status"] == "applied", run1_payload.get("issues")
    # #1666: run 1 (the speaker's first-ever apply) lands on its own
    # content-addressed sibling too, not config_path directly -- read the
    # stable reference value from run 1's own reported path. The successful
    # apply's promote step means config_path (canonical) also currently
    # holds these same bytes, as a COPY.
    run1_config_text = Path(
        run1_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    assert config_path.read_text(encoding="utf-8") == run1_config_text
    assert v2host.load_v2_state()["pre_apply_profile"] is None  # speaker's first-ever apply

    # The deferred VERIFY always auto-arms right after an apply — reproduce
    # its rebind-and-persist before the household ever reaches run 2.
    _simulate_deferred_verify_rearm(verify_session_id="verify_of_run1")
    assert v2host.load_v2_state()["applied"] is True
    assert v2host.load_v2_state()["pre_apply_profile"] is None

    # --- run 2 over run 1: also v2-written, through the SAME production seam ---
    run2_candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        **v2host.load_v2_state(),
        "session_id": "cap_run2",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": run2_candidate.fingerprint},
    })
    run2_payload = _apply(
        {
            "expected_candidate_fingerprint": run2_candidate.fingerprint,
            "candidate": run2_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert run2_payload["status"] == "applied", run2_payload.get("issues")
    # run 1's own sibling file is never clobbered by run 2's apply...
    assert Path(
        run1_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8") == run1_config_text
    # ...but canonical is a promoted COPY of whichever candidate applied most
    # recently (#1666), so it now tracks run 2, not run 1.
    run2_config_text = Path(
        run2_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    assert config_path.read_text(encoding="utf-8") == run2_config_text
    assert run2_config_text != run1_config_text

    state_after_run2_apply = v2host.load_v2_state()
    pre_apply_profile = state_after_run2_apply.get("pre_apply_profile")
    assert isinstance(pre_apply_profile, dict)
    assert (
        pre_apply_profile["candidate_fingerprint"]
        == run1_payload["profile"]["candidate_fingerprint"]
    )

    # The P0 assertion: run 2's own deferred VERIFY rebind must NOT wipe the
    # stash — before the fix this is exactly where it went null.
    _simulate_deferred_verify_rearm(verify_session_id="verify_of_run2")
    state_after_verify_rearm = v2host.load_v2_state()
    assert state_after_verify_rearm["applied"] is True
    pre_apply_profile_after_verify = state_after_verify_rearm.get("pre_apply_profile")
    assert isinstance(pre_apply_profile_after_verify, dict)
    assert (
        pre_apply_profile_after_verify["candidate_fingerprint"]
        == run1_payload["profile"]["candidate_fingerprint"]
    )

    # Undo must now succeed, through the real restore seam, reverting to run 1.
    restore_payload = v2host.handle_v2_restore(_bg_run_async, _FakeApplyCam)
    assert restore_payload["status"] == "restored", restore_payload.get("issues")
    # Restore reloads run 1's own sibling and re-promotes canonical onto it.
    assert config_path.read_text(encoding="utf-8") == run1_config_text
    active = load_applied_baseline_profile_state(state_path)
    assert active is not None
    assert (
        active["candidate_fingerprint"] == run1_payload["profile"]["candidate_fingerprint"]
    )
    state_after_restore = v2host.load_v2_state()
    assert state_after_restore["applied"] is False
    assert state_after_restore["pre_apply_profile"] is None


def test_start_over_while_applied_keeps_undo_reachable_through_real_seams(
    monkeypatch, tmp_path,
):
    """W6.10 gate should-fix, driven through the REAL restore seam: apply the
    prior crossover, apply a measured candidate over it, Start-over
    (reset_v2_journey_state — what handle_reset calls under the v2 flow), then
    Undo. The reset must serve the clean start screen WITHOUT unlinking the
    `applied`/`pre_apply_profile` pointers, so handle_v2_restore still reverts
    the active config to the prior profile afterward."""
    from jasper.active_speaker.baseline_profile import (
        apply_baseline_profile,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.crossover_preview import build_crossover_preview

    from tests.test_active_speaker_baseline_profile import _draft

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-19T09:00:00Z")
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
    apply_payload = _apply(
        {
            "expected_candidate_fingerprint": run8_candidate.fingerprint,
            "candidate": run8_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert apply_payload["status"] == "applied", apply_payload.get("issues")

    # Start-over while applied — the selective journey reset.
    v2host.reset_v2_journey_state()

    state = v2host.load_v2_state()
    assert state is not None
    assert state["applied"] is True
    assert isinstance(state["pre_apply_profile"], dict)
    assert state["accepted_phases"] == []
    assert state["candidate"] is None
    # The envelope serves the clean start screen…
    assert v2host.crossover_v2_status_block()["phase"] == PHASE_CHECK

    # …AND Undo still works, through the real restore seam.
    restore_payload = v2host.handle_v2_restore(_bg_run_async, _FakeApplyCam)
    assert restore_payload["status"] == "restored", restore_payload.get("issues")
    assert config_path.read_text(encoding="utf-8") == prior_config_text
    active = load_applied_baseline_profile_state(state_path)
    assert active is not None
    assert (
        active["candidate_fingerprint"]
        == prior_payload["profile"]["candidate_fingerprint"]
    )


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

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert payload["status"] == "applied", payload.get("issues")
    assert v2host.load_v2_state()["pre_apply_profile"] is None

    with pytest.raises(v2host.CrossoverV2Refused, match="first measured crossover"):
        v2host.handle_v2_restore(_bg_run_async, _FakeApplyCam)


# --- W6.11: the real session-start preview-ensure seam, end to end ---
#
# The P0: ``/sound/``'s Preview button was the ONLY historical writer of
# ``active_speaker_crossover_preview.json``; the v2 flow never called it. A
# candidate measured without a preview baked its ``source_preset`` against
# ``resolve_capture_preset``'s generic-bundled-preset fallback, which then
# could NEVER match a preview generated later — apply refused
# ``measured_candidate_preset_mismatch`` forever, and Start-over (which
# deletes the preview by design, see ``jasper.active_speaker.reset``)
# poisoned every subsequent apply. ``_seed_baseline_apply_environment``
# itself was part of the problem: it hand-built and wrote the preview file
# directly, sidestepping the exact fallback path that shipped broken.
#
# These tests drive the REAL fix end to end, through the real seams, with
# NO hand-seeded preview file anywhere: v2 session start
# (``v2host.ensure_crossover_preview_ready`` — the seam both
# ``resolve_conductor_context`` callers, ``prepare_v2_session`` and
# ``prepare_v2_verify``, share) generates the preview from the current
# design draft when absent, reusing ``/sound/``'s own generator
# (``jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft``
# -> ``crossover_preview.save_crossover_preview``).


def test_v2_session_start_ensures_preview_and_survives_start_over_then_reapply(
    monkeypatch, tmp_path,
):
    """The full real journey: no preview on disk -> session start ensures one
    (asserted on disk, ready) -> measure-shaped candidate baked against the
    resolved preset -> handle_v2_apply SUCCEEDS through the real
    apply_baseline_profile guard -> Start-over (the REAL handle_reset)
    deletes the preview by design -> a fresh session start re-ensures it from
    the (unchanged) design draft -> apply succeeds again. The test never
    once hand-writes active_speaker_crossover_preview.json."""
    from jasper.active_speaker import compile_preset_from_crossover_preview
    from jasper.web import correction_crossover_backend as reset_backend
    from jasper.web import correction_crossover_flow as reset_flow

    preview_path = tmp_path / "crossover_preview.json"
    assert not preview_path.exists()

    # _seed_baseline_apply_environment's own preview-generation step IS a v2
    # session start (it calls ensure_crossover_preview_ready — no direct
    # build_crossover_preview()+write since W6.11). Assert the file landed
    # ready, proving the ensure step actually ran rather than being a no-op.
    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    assert preview_path.exists()
    on_disk = json.loads(preview_path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "ready_for_protected_staging"

    candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_e2e_1",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })
    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert payload["status"] == "applied", payload.get("issues")

    # Start-over — the REAL handle_reset (real reset_measurement_journey, a
    # fresh no-op CrossoverLevelLease; only the envelope-rendering tail is
    # stubbed, mirroring test_correction_crossover_reset.py's real-clear
    # pattern). The other measurement-journey artifacts route to tmp_path too
    # so the real clear never touches /var/lib/jasper.
    for env_name in (
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE",
    ):
        monkeypatch.setenv(env_name, str(tmp_path / f"{env_name.lower()}.json"))
    fresh_lease = reset_backend.CrossoverLevelLease()
    monkeypatch.setattr(reset_backend, "level_lease", lambda: fresh_lease)
    monkeypatch.setattr(reset_flow, "handle_status", lambda *, relay=None: ({}, 200))
    monkeypatch.setattr(reset_flow, "_active_group_member", lambda: False)
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_envelope.build_crossover_envelope_logged",
        lambda status: {"screen": "start", "active": True, "steps": [], "nudges": []},
    )

    _reset_payload, reset_status = reset_flow.handle_reset()

    assert reset_status == 200
    # The preview really is gone — reset.py's documented by-design deletion.
    assert not preview_path.exists()

    # A fresh v2 session start re-ensures the preview from the unchanged
    # design draft — still no hand-seeding.
    reensured = v2host.ensure_crossover_preview_ready()
    assert reensured["status"] == "ready_for_protected_staging"
    assert preview_path.exists()

    preset_again, issues, _gates = compile_preset_from_crossover_preview(
        topology, reensured,
    )
    assert preset_again is not None, issues
    candidate_again = _run6_measured_candidate(preset_again)
    v2host.save_v2_state({
        "session_id": "cap_e2e_2",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate_again.fingerprint},
        "applied": False,
    })
    payload_again = _apply(
        {
            "expected_candidate_fingerprint": candidate_again.fingerprint,
            "candidate": candidate_again.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert payload_again["status"] == "applied", payload_again.get("issues")


def test_v2_session_start_refuses_by_name_when_draft_cannot_produce_a_ready_preview(
    monkeypatch, tmp_path,
):
    """Negative: no design draft has ever been saved, so the ensure step's
    regeneration attempt cannot reach ready_for_protected_staging. Session
    start must refuse BY NAME (CrossoverV2Refused, naming the actual
    blocker) — never a silent pass-through that only surfaces as an
    apply-time 409 later."""
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft_never_saved.json"),
    )
    preview_path = tmp_path / "crossover_preview.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE", str(preview_path)
    )

    with pytest.raises(v2host.CrossoverV2Refused, match="not ready for measurement"):
        v2host.ensure_crossover_preview_ready()

    # The regeneration attempt still ran (the same machinery /sound/ would
    # have run) and left an honest "blocked" preview on disk, never a
    # ready_for_protected_staging one.
    assert preview_path.exists()
    blocked = json.loads(preview_path.read_text(encoding="utf-8"))
    assert blocked["status"] == "blocked"


# --------------------------------------------------------------------------- #
# CHECK evidence artifact — the per-role MEASURE level solve (issue #1825)
# --------------------------------------------------------------------------- #


class _RecordingEvidenceStore:
    """Minimal stand-in for the commissioning evidence store."""

    session_id = "bundle-session"

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish_json_artifact(self, relpath, payload):
        self.published.append((relpath, payload))
        return SimpleNamespace(fingerprint="fp-check")


def test_check_evidence_artifact_carries_the_per_role_level_solve():
    """`check.json` is the session's durable record of what MEASURE was about
    to be played at. The solved gains alone are not self-explaining, so the
    artifact carries the derivation beside them — which limit chose each
    driver's level, and the ambient band it was solved against."""
    from jasper.audio_measurement.program_analysis import GainPlan, RoleGainSolve

    store = _RecordingEvidenceStore()
    publish_check, _publish_candidate, refs = v2host.bind_evidence_publishers(
        store, "relay-session"
    )
    plan = GainPlan(
        gain_db={"woofer": -19.0, "tweeter": -31.0},
        predicted_peak_dbfs=-19.0,
        snr_floor_ok=True,
        role_solves={
            "tweeter": RoleGainSolve(
                role="tweeter", gain_db=-31.0, flat_target_gain_db=-13.0,
                bound_by="room_snr", band_hz=(1500.0, 20000.0),
                ambient_dbfs=-72.0, required_snr_db=41.0,
                required_capture_dbfs=-31.0,
            ),
        },
    )
    publish_check(plan, {"bands": [{"band_id": "mid", "level_dbfs": -72.0}]})

    assert refs["check_artifact"] == "fp-check"
    (relpath, raw_payload), = store.published
    assert relpath == "crossover_v2/relay-session/check.json"
    # Round-trips as JSON — the evidence store re-opens what it writes.
    payload = json.loads(json.dumps(raw_payload))
    assert payload["gain_plan_db"] == {"woofer": -19.0, "tweeter": -31.0}
    tweeter = payload["role_solves"]["tweeter"]
    assert tweeter["bound_by"] == "room_snr"
    assert tweeter["reduction_db"] == pytest.approx(18.0)
    assert tweeter["ambient_dbfs"] == pytest.approx(-72.0)
    assert tweeter["band_hz"] == [1500.0, 20000.0]


def test_check_evidence_artifact_tolerates_a_plan_without_solves():
    """A legacy plan carries no ``role_solves``; the artifact publishes an
    empty map rather than failing — and an empty map is "no derivation
    published", never a claim that nothing moved."""
    from jasper.audio_measurement.program_analysis import GainPlan

    store = _RecordingEvidenceStore()
    publish_check, _publish_candidate, _refs = v2host.bind_evidence_publishers(
        store, "relay-session"
    )
    publish_check(
        GainPlan(
            gain_db={"woofer": -11.0}, predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
        {"bands": []},
    )
    (_relpath, payload), = store.published
    assert payload["role_solves"] == {}

# --- the pre-tone phase ladder (#1824 D3/D4) ---------------------------------


def _ladder_roles():
    return _roles()


def test_program_phase_schedule_reads_the_programs_own_segments():
    """The ladder's offsets come off the COMPOSED program, never off the
    composer's constants — so a program that stops carrying a courtesy prelude
    or an ambient window stops emitting that phase, and one that moves either
    moves the ladder with it."""
    from jasper.audio_measurement.program import (
        PILOT_AMBIENT_WINDOW_S,
        build_check_program,
        build_measure_program,
    )

    roles = _ladder_roles()
    check = v2host.program_phase_schedule(
        build_check_program(roles, courtesy_prelude=True)
    )
    # CHECK opens on its 12 s session-ambient window and splices the beeps
    # AFTER it (jasper.audio_measurement.program._insert_courtesy_prelude), so
    # the ladder is ordered by the program, not by a fixed vocabulary.
    assert [phase for _o, phase, _x in check] == [
        "ambient_started", "prelude_started", "sweep_started",
    ]
    offsets = {phase: offset for offset, phase, _x in check}
    extras = {phase: extra for _o, phase, extra in check}
    assert offsets["ambient_started"] == pytest.approx(0.0, abs=1e-6)
    assert extras["ambient_started"]["duration_s"] == pytest.approx(12.0, abs=1e-6)
    # THE S1 fact. CHECK's window is the SESSION's room-noise measurement,
    # deliberately taken BEFORE the household is asked to go quiet (the
    # composer's module docstring) — the ambient band-floor report and the gain
    # solve read it. A phone that asked for quiet here would edit the floor it
    # is measuring, so the host must not ask.
    assert extras["ambient_started"]["quiet_requested"] is False
    # The tone is announced only after the beeps AND their settle.
    assert offsets["sweep_started"] > offsets["prelude_started"]

    measure = v2host.program_phase_schedule(build_measure_program(
        {rb.role: 0.0 for rb in roles},
        roles,
        leading_pilot_gains_db=(-10.0, 0.0),
        courtesy_prelude=True,
    ))
    assert [phase for _o, phase, _x in measure] == [
        "prelude_started", "ambient_started", "sweep_started",
    ]
    m_offsets = {phase: offset for offset, phase, _x in measure}
    m_extras = {phase: extra for _o, phase, extra in measure}
    # THE defect this exists to remove: the eager post claimed the tone was
    # playing here, at t=0 — 4.6 s of beeps and silence before the first
    # sound of the measurement.
    assert m_offsets["prelude_started"] == pytest.approx(0.0, abs=1e-6)
    assert m_offsets["sweep_started"] == pytest.approx(4.6, abs=1e-3)
    # The room-listening window's own length is what the phone counts down.
    assert m_extras["ambient_started"]["duration_s"] == pytest.approx(
        PILOT_AMBIENT_WINDOW_S, abs=1e-6
    )
    # …and this window is the OPPOSITE of CHECK's: it sits after the beeps and
    # "MUST be measured with the room already quiet", so here the host DOES ask.
    assert m_extras["ambient_started"]["quiet_requested"] is True


def test_program_phase_schedule_is_empty_for_a_program_it_cannot_read():
    """No segments, no readable rate ⇒ no ladder, rather than a guess."""
    assert v2host.program_phase_schedule(None) == ()
    assert v2host.program_phase_schedule(
        SimpleNamespace(sample_rate_hz=48000, segments=())
    ) == ()
    assert v2host.program_phase_schedule(
        SimpleNamespace(sample_rate_hz=0, segments=(object(),))
    ) == ()


def _ladder_program():
    """A synthetic program whose whole ladder fits inside a test."""
    from jasper.audio_measurement.program import (
        AMBIENT_SEGMENT_ID,
        KIND_COURTESY_TONE,
        KIND_SILENCE,
        KIND_SWEEP,
    )

    def seg(segment_id, kind, start_ms, len_ms):
        return SimpleNamespace(
            segment_id=segment_id,
            kind=kind,
            start_sample=int(start_ms * 48),
            n_samples=int(len_ms * 48),
        )

    return SimpleNamespace(
        sample_rate_hz=48000,
        segments=(
            seg("beeps", KIND_COURTESY_TONE, 0, 10),
            seg(AMBIENT_SEGMENT_ID, KIND_SILENCE, 20, 10),
            seg("sweep", KIND_SWEEP, 40, 20),
        ),
    )


def test_phase_ladder_posts_each_phase_when_it_becomes_audible():
    posted: list[tuple[str, dict]] = []
    cancel = v2host.start_program_phase_ladder(
        lambda phase, **extra: posted.append((phase, extra)),
        _ladder_program(),
        skew_s=0.0,
    )
    deadline = time.monotonic() + 5.0
    while len(posted) < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    cancel()

    assert [phase for phase, _extra in posted] == [
        "prelude_started", "ambient_started", "sweep_started",
    ]
    # Only the room-listening window carries fields; the other two must not
    # invent any (the phone renders its countdown from exactly these).
    assert posted[0][1] == {}
    assert posted[1][1]["duration_s"] == pytest.approx(0.01, abs=1e-6)
    assert posted[1][1]["quiet_requested"] is True
    assert posted[2][1] == {}


def test_phase_ladder_cancel_stops_further_posts():
    """A program that ends, fails or is stopped must not leave a timer behind
    that posts a phase for a capture which is already over."""
    posted: list[str] = []
    cancel = v2host.start_program_phase_ladder(
        lambda phase, **_extra: posted.append(phase),
        _ladder_program(),
        skew_s=0.5,
    )
    cancel()
    time.sleep(0.1)
    assert posted == []


def test_phase_ladder_replaces_the_eager_sweep_started_post(monkeypatch):
    """End to end through the REAL runner: with a playback-start signal wired,
    the host posts the LADDER from inside the play instead of the eager
    `sweep_started` it used to post at arm time — the ~4.6 s claim of a tone
    that had not started (#1824 D4)."""
    monkeypatch.setattr(v2host, "PHASE_LADDER_START_SKEW_S", 0.0)
    backend = FakePlanRelayBackend()
    spec = build_v2_session_spec(_roles(), FC_HZ, acknowledgement_binding=_BINDING)

    def on_deferred(_driver):
        state = v2host.load_v2_state()
        v2host.observe_apply_success(state["candidate"]["fingerprint"])

    client, session, phone = _mint_v2_session(backend, spec, on_deferred=on_deferred)
    signal = v2host.PlaybackStartSignal()
    ladder_program = _ladder_program()

    def on_play(_phase, _begun):
        # Stands in for bind_production_play's own fire at the WAV handoff.
        signal.fire(ladder_program)
        time.sleep(0.25)

    conductor = _conductor(backend, session, phone, published=[], on_play=on_play)
    _run(
        _build_runner(conductor, VolumeRecorder(), playback_started=signal),
        client,
        session,
    )

    events = backend.host_events[session.session_id]
    phases = [e.get("phase") for e in events]
    authorized_at = phases.index("capture_authorized")
    result_at = phases.index("capture_result")
    assert phases[authorized_at:result_at + 1] == [
        "capture_authorized",
        "prelude_started",
        "ambient_started",
        "sweep_started",
        "sweep_complete",
        "capture_result",
    ]
    ambient = next(e for e in events if e.get("phase") == "ambient_started")
    # The countdown field the capture page reads. Its consumer has been
    # shipped-but-dead since the producer it was written against was deleted.
    assert ambient["duration_s"] == pytest.approx(0.01, abs=1e-6)
    assert ambient["quiet_requested"] is True
    # Every ladder post is addressed to the armed capture, like the pair it
    # replaces — a phone that matched on (index, attempt) must still match.
    assert ambient["index"] == 1
    assert ambient["attempt"] == 1


def test_phase_ladder_cancel_waits_for_a_post_already_in_flight():
    """#1824 S2: the relay's host-event slot is last-write-wins, so a ladder
    post still in flight when the caller moves on would land AFTER whatever it
    posts next — overwriting a `sweep_complete`, or a terminal
    `capture_result`, and putting the phone back to polling a refusal it can no
    longer see. `_cancel` must not return while one is running."""
    started = threading.Event()
    release = threading.Event()
    posted: list[str] = []

    def slow_post(phase: str, **_extra: Any) -> None:
        started.set()
        release.wait(5.0)
        posted.append(phase)

    cancel = v2host.start_program_phase_ladder(
        slow_post, _ladder_program(), skew_s=0.0
    )
    assert started.wait(5.0), "the ladder reached its first post"

    done = threading.Event()

    def _cancel_and_flag() -> None:
        cancel()
        done.set()

    waiter = threading.Thread(target=_cancel_and_flag, daemon=True)
    waiter.start()
    # Cancel is BLOCKED while the post runs — that is the whole contract.
    assert not done.wait(0.3), "cancel returned while a post was still in flight"
    release.set()
    assert done.wait(5.0), "cancel returned once the in-flight post finished"
    waiter.join(timeout=5.0)
    # The in-flight post completed (it was not abandoned), and the ladder
    # stopped there rather than continuing through the rest of the schedule.
    assert posted == ["prelude_started"]


def test_playback_signal_handler_failure_never_stops_the_measurement():
    """The signal fires from INSIDE the play path, so an escaping handler error
    would abort a sweep over a status line. Pins the promise the widened catch
    in `PlaybackStartSignal.fire` makes (#1824 N2)."""
    signal = v2host.PlaybackStartSignal()

    for boom in (
        AttributeError("no segments"),
        KeyError("play_wav"),
        TypeError("not a program"),
        RuntimeError("can't start new thread"),
        ValueError("bad rate"),
        OSError("resource"),
    ):
        def _raise(_program: Any, exc: BaseException = boom) -> None:
            raise exc

        signal.install(_raise)
        signal.fire(object())  # must not raise

    # …and a cleared signal is simply inert.
    signal.clear()
    signal.fire(object())
