# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Stereo-pair acoustic sync flow (`/sync/*`).

Handler layer only. The signal generation and analysis live in
``jasper.multiroom.sync_measure``; this module owns the measurement
window, pair gating, playback process, and state needed by the browser
or an operator script.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import tempfile
import threading
from contextlib import suppress
from http import HTTPStatus
from typing import Any, Callable

from jasper.audio_measurement.correction_lane import exec_correction_play
from jasper.correction.coordinator import HeldWindow
from jasper.log_event import log_event

from ._common import close_awaitable, terminate_async_process
from .pair_flow import members_by_channel, resolve_pair

logger = logging.getLogger("jasper.web.sync")

SESSION_MAX_S = 240.0
WINDOW_OPEN_TIMEOUT_S = 20.0
PLAYBACK_REAP_TIMEOUT_S = 2.0

_ACTIVE_PHASES = frozenset({"measuring", "applying"})

_lock = threading.Lock()
_state: dict[str, Any] = {
    "phase": "idle",
    "error": "",
    "members": None,
    "result": None,
    "recommendation": None,
    "playback": None,
    "session_token": 0,
    "release_window": None,
    "wav_path": "",
}


def _reset_locked(error: str = "") -> None:
    next_session_token = int(_state.get("session_token", 0)) + 1
    playback = _state.get("playback")
    if playback and playback.get("proc") is not None:
        terminate_async_process(playback["proc"])
    release = _state.get("release_window")
    _state.update({
        "phase": "idle",
        "error": error,
        "members": None,
        "result": None,
        "recommendation": None,
        "playback": None,
        "session_token": next_session_token,
        "release_window": None,
    })
    if release is not None:
        release()


def _owns_session_locked(session_token: int, *, phase: str) -> bool:
    return (
        int(_state.get("session_token", 0)) == session_token
        and _state.get("phase") == phase
    )


def active_phase() -> str | None:
    with _lock:
        return _state["phase"] if _state["phase"] in _ACTIVE_PHASES else None


def _public_members(members: dict | None) -> dict | None:
    if not members:
        return None
    return {
        ch: {"label": m["label"], "is_self": m["is_self"],
             "trim_db": round(m["trim_db"], 1)}
        for ch, m in members.items()
    }


def handle_status() -> dict:
    with _lock:
        return {
            "phase": _state["phase"],
            "error": _state["error"],
            "members": _public_members(_state["members"]),
            "result": _state["result"],
            "recommendation": _state["recommendation"],
            "playing": _state["playback"] is not None,
        }


async def _session_window(session_token: int, window: HeldWindow) -> None:
    def failed(e: BaseException) -> None:
        log_event(logger, "sync.window_failed", level=logging.ERROR, exc_info=True)
        with _lock:
            # A successful analysis advances the phase before it releases the
            # window. Cleanup can then fail in measurement_window.__aexit__;
            # generation, not the old measuring phase, owns that failure. A
            # replacement reset increments the token, so stale cleanup still
            # cannot damage the newer session.
            if int(_state.get("session_token", 0)) == session_token:
                _state["release_window"] = None
                _reset_locked(f"measurement window failed: {e}")

    with _lock:
        if not _owns_session_locked(session_token, phase="measuring"):
            window.entered.set()
            return
        _state["release_window"] = window.release
    with suppress(Exception):
        async with window.holding(on_failure=failed) as release:
            with _lock:
                if not _owns_session_locked(session_token, phase="measuring"):
                    return
            window.entered.set()
            try:
                await asyncio.wait_for(release.wait(), SESSION_MAX_S)
            except asyncio.TimeoutError:
                log_event(logger, "sync.session_timeout", level=logging.WARNING)
                with _lock:
                    if _owns_session_locked(session_token, phase="measuring"):
                        _state["release_window"] = None
                        _reset_locked("session timed out")


def _marker_wav_path() -> str:
    from jasper.multiroom.sync_measure import write_marker_wav

    with _lock:
        cached = _state.get("wav_path")
    if cached:
        return cached
    f = tempfile.NamedTemporaryFile(
        prefix="jasper-sync-marker-", suffix=".wav", delete=False,
    )
    f.close()
    write_marker_wav(f.name)
    with _lock:
        _state["wav_path"] = f.name
    return f.name


async def _start_playback(wav_path: str):
    return await exec_correction_play(
        wav_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _watch_playback(proc) -> None:
    await proc.wait()
    with _lock:
        if (_state.get("playback") or {}).get("proc") is proc:
            _state["playback"] = None
            log_event(logger, "sync.marker_finished")


def handle_start(hostname: str, schedule: Callable) -> tuple[dict, int]:
    from .active_speaker_flow import active_phase as _active_speaker_phase

    own, peer, err = resolve_pair()
    if err:
        return {"ok": False, "error": err}, HTTPStatus.CONFLICT
    members = members_by_channel(own, peer, hostname)
    if members is None:
        return {
            "ok": False,
            "error": "pair channels are not one left + one right",
        }, HTTPStatus.CONFLICT
    # Active-speaker commissioning measures through the production graph too;
    # refuse so the two measurement flows can't run at once (see
    # active_speaker_flow — it participates cooperatively, not via the window).
    if _active_speaker_phase() is not None:
        return {
            "ok": False,
            "error": "active-speaker commissioning is in progress on this speaker",
        }, HTTPStatus.CONFLICT

    with _lock:
        if _state["phase"] in _ACTIVE_PHASES:
            return {"ok": False, "error": "a sync session is already running"}, \
                HTTPStatus.CONFLICT
        _reset_locked()
        _state["phase"] = "measuring"
        _state["members"] = members
        session_token = int(_state["session_token"])

    window = HeldWindow()
    window_coro = _session_window(session_token, window)
    try:
        window_future = schedule(window_coro)
    except RuntimeError as e:
        window_coro.close()
        log_event(logger, "sync.window_schedule_failed", level=logging.ERROR)
        with _lock:
            if _owns_session_locked(session_token, phase="measuring"):
                _reset_locked(f"measurement window did not start: {e}")
        return {
            "ok": False,
            "error": "could not start the measurement window",
        }, HTTPStatus.INTERNAL_SERVER_ERROR
    if not window.entered.wait(WINDOW_OPEN_TIMEOUT_S):
        cancel = getattr(window_future, "cancel", None)
        if callable(cancel):
            cancel()
        with _lock:
            if _owns_session_locked(session_token, phase="measuring"):
                _reset_locked("measurement window did not open")
        return {"ok": False, "error": "could not pause the speaker"}, \
            HTTPStatus.INTERNAL_SERVER_ERROR
    with _lock:
        if not _owns_session_locked(session_token, phase="measuring"):
            return {"ok": False, "error": _state["error"]}, \
                HTTPStatus.INTERNAL_SERVER_ERROR
        members_out = _public_members(_state["members"])
    log_event(logger, "sync.session_started")
    return {"ok": True, "members": members_out}, HTTPStatus.OK


def handle_play(run_async: Callable, schedule: Callable) -> tuple[dict, int]:
    with _lock:
        if _state["phase"] != "measuring":
            return {"ok": False, "error": "no active sync session"}, \
                HTTPStatus.CONFLICT
        if _state["playback"] is not None:
            return {"ok": False, "error": "sync marker already playing"}, \
                HTTPStatus.CONFLICT
        session_token = int(_state["session_token"])
    try:
        proc = run_async(_start_playback(_marker_wav_path()), timeout=10.0)
    except Exception as e:  # noqa: BLE001
        log_event(logger, "sync.play_spawn_failed", level=logging.ERROR, exc_info=True)
        return {"ok": False, "error": f"playback failed: {e}"}, \
            HTTPStatus.INTERNAL_SERVER_ERROR
    # Re-validate under the lock after the spawn: the pre-spawn check released
    # the lock, so two concurrent /sync/play calls could both pass it and both
    # spawn overlapping aplay markers into one delay measurement. The loser
    # kills its just-spawned proc instead of recording it (mirrors
    # balance_flow.handle_ramp's post-spawn re-check).
    with _lock:
        abort_error = ""
        if _state["phase"] != "measuring":
            abort_error = f"no active sync session (phase {_state['phase']})"
        elif int(_state["session_token"]) != session_token:
            abort_error = "sync session changed before playback started"
        elif _state["playback"] is not None:
            abort_error = "sync marker already playing"
        if abort_error:
            terminate_async_process(proc)
            log_event(
                logger,
                "sync.play_start_aborted",
                reason=abort_error,
                level=logging.WARNING,
            )
            return {"ok": False, "error": abort_error}, HTTPStatus.CONFLICT
        _state["playback"] = {"proc": proc}
    watcher = _watch_playback(proc)
    try:
        schedule(watcher)
    except RuntimeError as e:
        watcher.close()
        terminate_async_process(proc)
        # The one site that must know the marker is actually gone before it
        # answers: reap through the runner, and SIGKILL a child that sat
        # through the SIGTERM rather than leave it playing into the room.
        reaped = False
        wait_coro = proc.wait()
        try:
            run_async(wait_coro, timeout=PLAYBACK_REAP_TIMEOUT_S)
            reaped = True
        except (
            concurrent.futures.TimeoutError,
            concurrent.futures.CancelledError,
            RuntimeError,
            OSError,
        ):
            with suppress(RuntimeError):
                close_awaitable(wait_coro)
            with suppress(ProcessLookupError):
                proc.kill()
        with _lock:
            playback = _state.get("playback") or {}
            if (
                int(_state.get("session_token", 0)) == session_token
                and playback.get("proc") is proc
            ):
                _state["playback"] = None
        log_event(
            logger,
            "sync.play_watch_schedule_failed",
            error=str(e),
            reaped=reaped,
            level=logging.ERROR,
        )
        return {
            "ok": False,
            "error": "could not monitor marker playback",
        }, HTTPStatus.INTERNAL_SERVER_ERROR
    log_event(logger, "sync.marker_started")
    return {"ok": True}, HTTPStatus.OK


def handle_analyze(wav_bytes: bytes) -> tuple[dict, int]:
    from jasper.multiroom.sync_measure import (
        analyze_wav_bytes,
        recommend_channel_delays,
    )

    with _lock:
        if _state["phase"] != "measuring":
            return {"ok": False, "error": "no active sync session"}, \
                HTTPStatus.CONFLICT
        session_token = int(_state["session_token"])
    try:
        result = analyze_wav_bytes(wav_bytes)
    except Exception as e:  # noqa: BLE001
        log_event(logger, "sync.analyze_failed", level=logging.ERROR, exc_info=True)
        return {"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST
    recommendation = recommend_channel_delays(result.delta_ms)
    payload = {
        "ok": result.ok,
        "result": result.to_dict(),
        "recommendation": recommendation.to_dict(),
    }
    with _lock:
        if not _owns_session_locked(session_token, phase="measuring"):
            log_event(
                logger,
                "sync.analyze_aborted",
                reason="session-changed",
                level=logging.WARNING,
            )
            return {
                "ok": False,
                "error": "sync session changed while analysis was in progress",
            }, HTTPStatus.CONFLICT
        _state["result"] = payload["result"]
        _state["recommendation"] = payload["recommendation"]
        if result.ok:
            _state["phase"] = "analyzed"
            release = _state.get("release_window")
            _state["release_window"] = None
        else:
            release = None
    if release is not None:
        release()
    log_event(
        logger,
        "sync.analyzed",
        ok=result.ok,
        delta_ms=f"{result.delta_ms:.3f}",
        confidence=f"{result.confidence:.3f}",
    )
    return payload, HTTPStatus.OK


def handle_apply(handler) -> tuple[dict, int]:
    """Apply leader-owned acoustic delays through the grouping writer.

    This writes only the leader's grouping state. Fixed endpoint-path
    latency is a separate Snapcast client-latency apply path.

    ``handler`` carries the browser-supplied ``X-JTS-Token``; we forward it
    to the leader's /grouping/set just like the /rooms bond fan-out. That
    route is one of jasper-control's MANDATORY token-gated mutations (WS1
    Phase 2), so the loopback write would otherwise be rejected 403 on a
    gate-armed speaker — and since sync only writes self, a missing token
    fails the apply outright.
    """
    from .rooms_setup import (
        post_grouping_to_member,
        request_control_token,
        self_addresses,
    )

    token = request_control_token(handler)

    with _lock:
        if _state["phase"] != "analyzed" or not _state["recommendation"]:
            return {"ok": False, "error": "nothing to apply"}, HTTPStatus.CONFLICT
        members = _state["members"]
        if not members:
            return {"ok": False, "error": "session has no members"}, \
                HTTPStatus.CONFLICT

        # Self is the leader by /sync/start gate; write its existing grouping
        # fields plus the leader-owned rendered-channel delays. Build the body
        # and claim the bounded apply while holding the lock: after this point,
        # stop/start reject instead of replacing the session while its external
        # /grouping/set side effect is in flight. The network write itself stays
        # outside the lock and is capped at CONTROL_HTTP_TIMEOUT_SEC (5 s) by
        # post_grouping_to_member.
        self_member = next((m for m in members.values() if m["is_self"]), None)
        if self_member is None:
            return {"ok": False, "error": "could not identify leader member"}, \
                HTTPStatus.CONFLICT
        rec = dict(_state["recommendation"])
        g = self_member["grouping"]
        body = {
            "enabled": True,
            "role": str(g.get("role") or ""),
            "channel": str(g.get("channel") or ""),
            "bond_id": str(g.get("bond_id") or ""),
            "leader_addr": str(g.get("leader_addr") or ""),
            "left_delay_ms": rec["left_delay_ms"],
            "right_delay_ms": rec["right_delay_ms"],
        }
        session_token = int(_state["session_token"])
        _state["phase"] = "applying"

    apply_call_completed = False
    try:
        ok, detail = post_grouping_to_member(
            "", body, self_addresses(), token=token)
        apply_call_completed = True
    finally:
        if not apply_call_completed:
            with _lock:
                if _owns_session_locked(session_token, phase="applying"):
                    _state["phase"] = "analyzed"
    with _lock:
        if not _owns_session_locked(session_token, phase="applying"):
            log_event(
                logger,
                "sync.apply_aborted",
                reason="session-changed",
                level=logging.WARNING,
            )
            return {
                "ok": False,
                "error": "sync session changed while apply was in progress",
                "detail": detail,
            }, HTTPStatus.CONFLICT
        if ok:
            _state["phase"] = "applied"
        else:
            _state["phase"] = "analyzed"
    log_event(
        logger,
        "sync.apply",
        ok=ok,
        left_delay_ms=f"{rec['left_delay_ms']:.3f}",
        right_delay_ms=f"{rec['right_delay_ms']:.3f}",
    )
    status = HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY
    return {"ok": ok, "detail": detail, "applied": rec}, status


def handle_stop() -> tuple[dict, int]:
    with _lock:
        if _state["phase"] == "applying":
            return {
                "ok": False,
                "error": "sync delay apply is in progress",
            }, HTTPStatus.CONFLICT
        _reset_locked()
    log_event(logger, "sync.stopped")
    return {"ok": True}, HTTPStatus.OK


_PAGE_CSS = """
.sync-card { max-width: 620px; }
.sync-actions { display: flex; flex-wrap: wrap; gap: 0.6rem; }
.sync-status { min-height: 1.4em; margin: 0.8rem 0; font-weight: 600; }
"""

_PAGE_BODY = """
<main class="page">
  <p class="eyebrow">Stereo pair</p>
  <h1>Measure sync</h1>
  <section class="info-card sync-card">
    <p>This page measures left/right arrival timing at the listening
    position. It plays a short marker through the bonded pair, records
    the room with this browser, and recommends positive-only channel
    delay for the leader render graph.</p>
    <div class="sync-status" id="status"></div>
    <pre id="result"></pre>
    <div class="sync-actions">
      <button class="btn btn--primary" id="start">Start</button>
      <button class="btn" id="play" disabled>Play marker</button>
      <button class="btn" id="apply" disabled>Apply</button>
      <button class="btn" id="stop">Stop</button>
    </div>
  </section>
</main>
<script type="module" src="/assets/sync/js/main.js"></script>
"""


def render_page(csrf_token: str) -> bytes:
    from ._common import canonical_page

    return canonical_page(
        "Measure sync",
        _PAGE_BODY,
        csrf_token=csrf_token,
        page_css=_PAGE_CSS,
    )
