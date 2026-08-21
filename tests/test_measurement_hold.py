# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-control's measurement hold — the measurement window's third lease.

The incident this closes: on jts3, ``jasper-seat-level``'s ramp was fought by
jasper-voice's ``VolumeCoordinator`` patrol once a second because seat-level
never entered ``measurement_window()``. The window is the ONE writer of "a
measurement is live"; jasper-control was the one enforcement point with no copy
of that fact, so a host slider (or the idle reconciler, once the active source
resolves to ``usbsink``) could still walk the fader a sweep is holding.

What must hold, and what would break without it:

* the hold **self-expires** — no reaper, no file, no surviving process, so a
  ``kill -9``'d holder frees the speaker on its own;
* the renewal cadence fits inside the TTL **with room for one failed renewal**,
  or a healthy long sweep un-gates itself mid-capture;
* a second owner is **refused, by name** — this is the cross-process mutex the
  window's in-process ``_window_active`` flag structurally cannot be;
* jasper-control **declines source-observed volume writes** while held, on the
  established ``observation_applied: false`` contract, and **still honours
  authoritative ones** (a human at the speaker is not locked out);
* the window **releases on every exit** — normal, raised, and cancelled;
* an unreachable jasper-control is **fail-soft** (the hazard it guards cannot
  occur while the daemon serving it is down), while a 409 is **fail-closed**.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jasper.active_speaker import session_volume_plan as svp
from jasper.control import measurement_hold as mh
from jasper.control.server import _make_handler
from jasper.correction import coordinator
from jasper.correction.coordinator import (
    MEASUREMENT_HOLD_COMMAND_TIMEOUT_SEC,
    MEASUREMENT_LEASE_REFRESH_SEC,
    MEASUREMENT_LEASE_RETRY_SEC,
    MeasurementWindowError,
    measurement_window,
)

from tests._async_wait import wait_signalled
from tests.test_control_server import FakeCoordinator


class Clock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


# --------------------------------------------------------------------------- #
# 1. the registrar: TTL, renewal, owner scoping
# --------------------------------------------------------------------------- #


def test_a_hold_expires_without_renewal():
    """Test 3 of the plan. The whole crash-recovery story in one assertion.

    Mutation-verified: deleting the ``now < self._expires_at`` guard in
    ``MeasurementHold._expire_locked`` (so a hold never lapses) turns the third
    assertion red.
    """
    clock = Clock()
    hold = mh.MeasurementHold(clock=clock)

    hold.acquire("seat-level")
    assert hold.held() is True

    clock.advance(mh.MEASUREMENT_HOLD_TTL_SEC - 0.5)
    assert hold.held() is True, "lapsed before its TTL"

    clock.advance(1.0)
    assert hold.held() is False, "outlived its TTL with no renewal"
    assert hold.owner() is None


def test_renewal_keeps_a_long_measurement_alive():
    """A legitimate multi-minute sweep must not un-gate itself."""
    clock = Clock()
    hold = mh.MeasurementHold(clock=clock)
    hold.acquire("correction-measurement")

    for _ in range(10):
        clock.advance(MEASUREMENT_LEASE_REFRESH_SEC)
        hold.acquire("correction-measurement")
        assert hold.held() is True

    # …and it still lapses once renewal stops.
    clock.advance(mh.MEASUREMENT_HOLD_TTL_SEC + 1.0)
    assert hold.held() is False


def test_a_second_owner_is_refused_by_name():
    """Test 5 of the plan (registrar half): the cross-process mutex."""
    clock = Clock()
    hold = mh.MeasurementHold(clock=clock)
    hold.acquire("correction-measurement")

    with pytest.raises(mh.MeasurementHoldConflict) as excinfo:
        hold.acquire("seat-level")
    assert excinfo.value.owner == "correction-measurement"
    assert "correction-measurement" in str(excinfo.value)

    # The incumbent still holds it — a refused acquire must not disturb it.
    assert hold.owner() == "correction-measurement"


def test_a_lapsed_hold_lets_the_next_measurement_in():
    clock = Clock()
    hold = mh.MeasurementHold(clock=clock)
    hold.acquire("correction-measurement")
    clock.advance(mh.MEASUREMENT_HOLD_TTL_SEC + 1.0)

    hold.acquire("seat-level")
    assert hold.owner() == "seat-level"


def test_release_is_owner_scoped():
    """One measurement's teardown must never un-gate another's live capture.

    The same rule jasper-mux applies to ``TEST_RELEASE``.
    """
    hold = mh.MeasurementHold(clock=Clock())
    hold.acquire("correction-measurement")

    with pytest.raises(mh.MeasurementHoldConflict) as excinfo:
        hold.release("seat-level")
    assert excinfo.value.owner == "correction-measurement"
    assert hold.held() is True

    hold.release("correction-measurement")
    assert hold.held() is False


def test_releasing_nothing_is_a_no_op_success():
    """A holder whose TTL already lapsed must still finish teardown cleanly."""
    clock = Clock()
    hold = mh.MeasurementHold(clock=clock)
    hold.acquire("seat-level")
    clock.advance(mh.MEASUREMENT_HOLD_TTL_SEC + 1.0)

    state = hold.release("seat-level")  # must not raise
    assert state["active"] is False


def test_only_implemented_modes_are_accepted():
    """The surface may not advertise a mode whose behaviour does not exist.

    ``evict`` (stop jasper-voice so its UDP sockets free, for the wake-corpus
    recorder) is a later change and adds itself to MEASUREMENT_HOLD_MODES
    together with the behaviour.
    """
    hold = mh.MeasurementHold(clock=Clock())
    assert mh.MEASUREMENT_HOLD_MODES == frozenset({"gate"})

    with pytest.raises(mh.MeasurementHoldModeError):
        hold.acquire("seat-level", "evict")
    with pytest.raises(mh.MeasurementHoldModeError):
        hold.acquire("", "gate")
    assert hold.held() is False


def test_the_snapshot_reports_held_for_not_only_expiry():
    """``held_for_s`` is what a stuck hold is visible through.

    ``expires_in_s`` resets on every renewal, so a hold stuck for an hour looks
    two minutes old through it. jasper-doctor's check_measurement_hold reads
    ``held_for_s`` for exactly this reason.
    """
    clock = Clock()
    hold = mh.MeasurementHold(clock=clock)
    hold.acquire("seat-level")

    for _ in range(30):
        clock.advance(MEASUREMENT_LEASE_REFRESH_SEC)
        hold.acquire("seat-level")

    snap = hold.snapshot()
    assert snap["active"] is True
    assert snap["owner"] == "seat-level"
    assert snap["mode"] == "gate"
    assert snap["expires_in_s"] == pytest.approx(mh.MEASUREMENT_HOLD_TTL_SEC)
    assert snap["held_for_s"] == pytest.approx(30 * MEASUREMENT_LEASE_REFRESH_SEC)


def test_an_idle_snapshot_names_no_owner():
    assert mh.MeasurementHold(clock=Clock()).snapshot() == {
        "active": False,
        "owner": None,
        "mode": None,
        "expires_in_s": None,
        "held_for_s": None,
    }


# --------------------------------------------------------------------------- #
# 2. the arithmetic that keeps a healthy sweep gated
# --------------------------------------------------------------------------- #


def test_the_renewal_cadence_fits_inside_the_hold_ttl():
    """Test 4 of the plan. Mirrors the voice-lease pin in
    tests/test_voice_daemon_measurement_inflight.py.

    jasper-control drops the hold ``MEASUREMENT_HOLD_TTL_SEC`` after the last
    acquire it saw; the window re-sends one every
    ``MEASUREMENT_LEASE_REFRESH_SEC``. Invert that and a healthy long sweep
    un-gates itself mid-capture and lets the host slider back onto the fader.

    The budget is a whole FAILED renewal cycle, not just the cadence: a renewal
    that times out costs its command timeout, then backs off
    ``MEASUREMENT_LEASE_RETRY_SEC``, then costs another command timeout on the
    retry. All of that has to fit before the TTL, or one lost round trip is
    enough to lose the lease.
    """
    assert 0 < MEASUREMENT_LEASE_REFRESH_SEC < mh.MEASUREMENT_HOLD_TTL_SEC
    assert (
        MEASUREMENT_LEASE_REFRESH_SEC
        + 2 * MEASUREMENT_HOLD_COMMAND_TIMEOUT_SEC
        + MEASUREMENT_LEASE_RETRY_SEC
        < mh.MEASUREMENT_HOLD_TTL_SEC
    )


# --------------------------------------------------------------------------- #
# 3. the HTTP surface, and the volume decline
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_process_hold():
    """The registrar is process-scoped; do not leak a hold between tests."""
    mh.reset_for_tests()
    yield
    mh.reset_for_tests()


@pytest.fixture
def control_server(monkeypatch, tmp_path):
    """A real jasper-control on a throwaway port. Yields (base_url, fake)."""
    import jasper.control.household_credential as hc
    import jasper.control.server as srv_mod
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "household_secret"))
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(_full_range_stereo(), topology_path)

    fake = FakeCoordinator(level=60)

    async def fake_with_coordinator(op, **_kwargs):
        return await op(fake)

    monkeypatch.setattr(srv_mod, "_with_coordinator", fake_with_coordinator)
    monkeypatch.setattr(srv_mod, "_read_volume_state", fake.get_volume_state)

    handler = _make_handler("127.0.0.1", 9, "/nonexistent.sock", ha_status_cache=object())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(url: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=2) as r:
        return r.status, json.loads(r.read() or b"{}")


def test_the_hold_route_acquires_renews_and_releases(control_server):
    base, _ = control_server

    status, body = _post(f"{base}/measurement/hold", {"owner": "seat-level"})
    assert status == 200
    assert body["measurement"]["active"] is True
    assert body["measurement"]["owner"] == "seat-level"
    assert body["measurement"]["mode"] == "gate"

    # Idempotent for the incumbent — this IS the renewal path.
    status, body = _post(f"{base}/measurement/hold", {"owner": "seat-level"})
    assert status == 200
    assert body["measurement"]["owner"] == "seat-level"

    status, body = _post(f"{base}/measurement/release", {"owner": "seat-level"})
    assert status == 200
    assert body["measurement"]["active"] is False


def test_a_second_acquire_is_a_409_naming_the_owner(control_server):
    """Test 5 of the plan (HTTP half)."""
    base, _ = control_server
    _post(f"{base}/measurement/hold", {"owner": "correction-measurement"})

    status, body = _post(f"{base}/measurement/hold", {"owner": "seat-level"})
    assert status == 409
    assert body["owner"] == "correction-measurement"
    assert "correction-measurement" in body["error"]
    assert body["measurement"]["owner"] == "correction-measurement"


def test_the_hold_route_rejects_a_missing_owner_and_an_unknown_mode(control_server):
    base, _ = control_server
    assert _post(f"{base}/measurement/hold", {})[0] == 400
    assert _post(f"{base}/measurement/hold", {"owner": "x", "mode": "evict"})[0] == 400
    assert _post(f"{base}/measurement/release", {})[0] == 400


def test_a_held_measurement_declines_source_observed_volume(control_server):
    """Test 2 of the plan. The jts3 fader war, at the route that caused it.

    Mutation-verified: dropping the ``measurement_hold.held()`` branch in
    ``VolumeRoutes._post_volume_set`` makes the third and fourth assertions
    fail (the observation applies and the fake coordinator records a write).
    """
    base, fake = control_server
    _post(f"{base}/measurement/hold", {"owner": "seat-level"})

    status, body = _post(
        f"{base}/volume/set", {"percent": 91, "source": "usbsink"},
    )
    assert status == 200, "the bridge's existing contract is 200 + a flag"
    assert body["observation_applied"] is False
    assert body["percent"] == 60, "the fader must be reported untouched"
    assert all(call[0] != "set" for call in fake.calls), fake.calls


def test_an_authoritative_write_still_lands_while_held(control_server):
    """A human at the speaker is not locked out. This is isolation, not a lockout."""
    base, fake = control_server
    _post(f"{base}/measurement/hold", {"owner": "seat-level"})

    status, body = _post(f"{base}/volume/set", {"percent": 42})
    assert status == 200
    assert body["percent"] == 42
    assert ("set", 42) in fake.calls
    assert "observation_applied" not in body


def test_the_observation_applies_once_the_hold_is_released(control_server):
    """The USB bridge's own retry re-applies the household slider afterwards."""
    base, fake = control_server
    _post(f"{base}/measurement/hold", {"owner": "seat-level"})
    _post(f"{base}/volume/set", {"percent": 91, "source": "usbsink"})
    _post(f"{base}/measurement/release", {"owner": "seat-level"})

    status, body = _post(
        f"{base}/volume/set", {"percent": 91, "source": "usbsink"},
    )
    assert status == 200
    assert body["observation_applied"] is True
    assert ("observe", 91) in fake.calls


def test_the_measurement_route_projects_the_hold(control_server):
    base, _ = control_server
    status, body = _get(f"{base}/measurement")
    assert status == 200
    assert body["measurement"]["active"] is False

    _post(f"{base}/measurement/hold", {"owner": "doctor-aec-probe"})
    status, body = _get(f"{base}/measurement")
    assert status == 200
    assert body["measurement"]["active"] is True
    assert body["measurement"]["owner"] == "doctor-aec-probe"


def test_state_carries_the_same_projection():
    """One fact, one reader — /state must not mint a second answer."""
    from jasper.control import state_aggregate

    mh.acquire("seat-level")
    src = state_aggregate.__file__
    assert '"measurement": measurement_hold.snapshot()' in open(src).read()
    assert mh.snapshot()["owner"] == "seat-level"


# --------------------------------------------------------------------------- #
# 4. the window holds it as its third lease
# --------------------------------------------------------------------------- #


def _hold_transport(monkeypatch, *, script=None):
    """Record every hold round trip; ``script`` overrides the reply per path."""
    calls: list[tuple[str, dict]] = []

    async def fake_command(path: str, body: dict) -> tuple[int, dict]:
        calls.append((path, body))
        if script is not None:
            return script(path, body)
        return 200, {"measurement": {"active": True, "owner": body.get("owner")}}

    monkeypatch.setattr(coordinator, "_measurement_hold_command", fake_command)
    return calls


async def test_the_window_takes_and_releases_the_hold(monkeypatch):
    calls = _hold_transport(monkeypatch)

    async with measurement_window(
        skip_voice_pause=True, skip_music_isolation=True, gate_owner="seat-level",
    ):
        assert calls == [
            ("/measurement/hold", {"owner": "seat-level", "mode": "gate"}),
        ]

    assert calls[-1] == ("/measurement/release", {"owner": "seat-level"})


async def test_the_hold_is_released_when_the_body_raises(monkeypatch):
    """Test 6 of the plan, first half."""
    calls = _hold_transport(monkeypatch)

    with pytest.raises(ZeroDivisionError):
        async with measurement_window(
            skip_voice_pause=True, skip_music_isolation=True, gate_owner="seat-level",
        ):
            raise ZeroDivisionError("boom")

    assert calls[-1] == ("/measurement/release", {"owner": "seat-level"})


async def test_the_hold_is_released_when_the_body_is_cancelled(monkeypatch):
    """Test 6 of the plan, second half.

    A Ctrl-C'd CLI or a cancelled web task must not leave the household's
    volume observations declined until the TTL lapses.
    """
    calls = _hold_transport(monkeypatch)
    entered = asyncio.Event()

    async def body() -> None:
        async with measurement_window(
            skip_voice_pause=True, skip_music_isolation=True, gate_owner="seat-level",
        ):
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(body())
    await wait_signalled(entered, "measurement window entered", producer=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls[-1] == ("/measurement/release", {"owner": "seat-level"})


async def test_a_409_refuses_the_window(monkeypatch):
    """Fail-CLOSED on conflict: another measurement genuinely owns the speaker."""
    calls = _hold_transport(
        monkeypatch,
        script=lambda path, body: (
            409, {"error": "a measurement is already in progress (owner=correction-measurement)"},
        ),
    )

    with pytest.raises(MeasurementWindowError) as excinfo:
        async with measurement_window(
            skip_voice_pause=True, skip_music_isolation=True, gate_owner="seat-level",
        ):
            pytest.fail("the window must not open under a conflicting hold")

    assert "correction-measurement" in str(excinfo.value)
    # Nothing was taken, so nothing is released — releasing here would un-gate
    # the OTHER measurement's live capture.
    assert [path for path, _ in calls] == ["/measurement/hold"]


async def test_an_unreachable_jasper_control_is_fail_soft(monkeypatch, caplog):
    """The one policy call worth stating out loud.

    The only writer this lease holds back is ``POST /volume/set`` with a
    ``source``, which jasper-control itself serves. A jasper-control that
    cannot answer is also not applying observations, so failing closed would
    refuse the measurement and buy nothing — it would just stop
    ``jasper-seat-level`` running on a box whose control daemon is down.
    """
    calls: list[str] = []

    async def unreachable(path: str, body: dict) -> tuple[int, dict]:
        calls.append(path)
        raise OSError("connection refused")

    monkeypatch.setattr(coordinator, "_measurement_hold_command", unreachable)

    ran = False
    async with measurement_window(
        skip_voice_pause=True, skip_music_isolation=True, gate_owner="seat-level",
    ):
        ran = True

    assert ran is True, "an unreachable jasper-control must not refuse the window"
    # No release attempt: we never held it.
    assert calls == ["/measurement/hold"]


async def test_a_non_409_refusal_is_fail_soft_too(monkeypatch):
    """A 403 from the control-token gate must not block a measurement either."""
    calls = _hold_transport(
        monkeypatch,
        script=lambda path, body: (403, {"error": "control_token_required"}),
    )

    ran = False
    async with measurement_window(
        skip_voice_pause=True, skip_music_isolation=True, gate_owner="seat-level",
    ):
        ran = True

    assert ran is True
    assert [path for path, _ in calls] == ["/measurement/hold"]


async def test_the_window_renews_the_hold_on_the_shared_cadence(monkeypatch):
    """A held window keeps re-sending acquire; that is what the TTL expects."""
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", 0.01)
    calls = _hold_transport(monkeypatch)
    renewed = asyncio.Event()

    real = coordinator._measurement_hold_command

    async def counting(path: str, body: dict) -> tuple[int, dict]:
        result = await real(path, body)
        if len([p for p, _ in calls if p == "/measurement/hold"]) >= 3:
            renewed.set()
        return result

    monkeypatch.setattr(coordinator, "_measurement_hold_command", counting)

    async with measurement_window(
        skip_voice_pause=True, skip_music_isolation=True, gate_owner="seat-level",
    ):
        await wait_signalled(renewed, "measurement hold renewed at least twice")

    assert calls[-1] == ("/measurement/release", {"owner": "seat-level"})


# --------------------------------------------------------------------------- #
# 5. the operator door reads ONE answer for "is a measurement live?"
# --------------------------------------------------------------------------- #


def _volume_state(path, *, status, opened_at, ceiling_s=None):
    path.write_text(json.dumps({
        "kind": svp.STATE_KIND,
        "schema_version": svp.SCHEMA_VERSION,
        "status": status,
        "reason": None if status == "active" else "restore_unconfirmed",
        "opened_at": opened_at,
        "wall_clock_ceiling_s": (
            svp.DEFAULT_WALL_CLOCK_CEILING_S if ceiling_s is None else ceiling_s
        ),
        "measurement_volume_db": -20.0,
        "original_main_volume_db": -6.0,
    }), encoding="utf-8")


def test_the_door_refuses_on_the_hold_with_no_statefile_at_all(tmp_path, monkeypatch):
    """Test 7 of the plan.

    A crossover-web session holds the window but its volume statefile lives
    elsewhere (or has not been written yet). Only jasper-control can tell a CLI
    that the speaker is busy — which is the whole reason the active arm moved.

    Mutation-verified: reverting ``_a_measurement_is_live`` to read only the
    plan turns this red (no statefile ⇒ it would answer "coast is clear").
    """
    state = tmp_path / "session_volume.json"
    assert not state.exists()
    monkeypatch.setattr(
        svp, "read_measurement_hold",
        lambda: {"active": True, "owner": "correction-measurement"},
    )

    busy = svp.live_measurement_session(state_path=state, action="leveling")
    assert busy is not None
    assert "already running" in busy


def test_a_reachable_control_saying_idle_beats_a_stale_statefile(tmp_path, monkeypatch):
    """The authority answers; the durable latch does not get a second vote."""
    import time as _time

    state = tmp_path / "session_volume.json"
    _volume_state(state, status="active", opened_at=_time.time())
    monkeypatch.setattr(svp, "read_measurement_hold", lambda: {"active": False})

    assert svp.live_measurement_session(state_path=state, action="leveling") is None


def test_an_unreachable_control_falls_back_to_the_statefile(tmp_path, monkeypatch):
    """The documented degradation: one authority, one fallback, never two votes."""
    import time as _time

    state = tmp_path / "session_volume.json"
    _volume_state(state, status="active", opened_at=_time.time())
    monkeypatch.setattr(svp, "read_measurement_hold", lambda: None)

    busy = svp.live_measurement_session(state_path=state, action="leveling")
    assert busy is not None
    assert "already running" in busy


def test_unresolved_stays_a_statefile_fact(tmp_path, monkeypatch):
    """A volume-recovery fact, not a liveness one: it must survive an idle hold."""
    import time as _time

    state = tmp_path / "session_volume.json"
    _volume_state(state, status="unresolved", opened_at=_time.time())
    monkeypatch.setattr(svp, "read_measurement_hold", lambda: {"active": False})

    busy = svp.live_measurement_session(state_path=state, action="leveling")
    assert busy is not None
    assert "unresolved" in busy


def test_read_measurement_hold_distinguishes_unreachable_from_idle(monkeypatch):
    """``None`` and ``{"active": False}`` must never be conflated.

    The door's conservative fallback only works because the two are distinct.
    """
    from jasper.control import client as control_client

    def boom(**_kwargs):
        raise control_client.ControlError("refused")

    monkeypatch.setattr(control_client, "get_measurement", boom)
    assert mh.read_measurement_hold() is None

    monkeypatch.setattr(
        control_client, "get_measurement", lambda **_k: {"active": False},
    )
    assert mh.read_measurement_hold() == {"active": False}
