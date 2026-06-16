"""Stereo-pair acoustic sync flow (`/sync/*`).

Handler layer only. The signal generation and analysis live in
``jasper.multiroom.sync_measure``; this module owns the measurement
window, pair gating, playback process, and state needed by the browser
or an operator script.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
from http import HTTPStatus
from typing import Any, Callable

from jasper.log_event import log_event

from .pair_flow import members_by_channel, resolve_pair

logger = logging.getLogger("jasper.web.sync")

SESSION_MAX_S = 240.0
WINDOW_OPEN_TIMEOUT_S = 20.0
PLAYBACK_DEVICE = "correction_substream"

_ACTIVE_PHASES = frozenset({"measuring"})

_lock = threading.Lock()
_state: dict[str, Any] = {
    "phase": "idle",
    "error": "",
    "members": None,
    "result": None,
    "recommendation": None,
    "playback": None,
    "release_window": None,
    "wav_path": "",
}


def _reset_locked(error: str = "") -> None:
    playback = _state.get("playback")
    if playback and playback.get("proc") is not None:
        try:
            playback["proc"].terminate()
        except ProcessLookupError:
            pass
    release = _state.get("release_window")
    _state.update({
        "phase": "idle",
        "error": error,
        "members": None,
        "result": None,
        "recommendation": None,
        "playback": None,
        "release_window": None,
    })
    if release is not None:
        release()


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


async def _session_window(entered: threading.Event) -> None:
    from jasper.correction.coordinator import measurement_window

    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    with _lock:
        _state["release_window"] = (
            lambda: loop.call_soon_threadsafe(release.set)
        )
    try:
        async with measurement_window():
            entered.set()
            try:
                await asyncio.wait_for(release.wait(), SESSION_MAX_S)
            except asyncio.TimeoutError:
                log_event(logger, "sync.session_timeout", level=logging.WARNING)
                with _lock:
                    if _state["phase"] == "measuring":
                        _state["release_window"] = None
                        _reset_locked("session timed out")
    except Exception as e:  # noqa: BLE001
        log_event(logger, "sync.window_failed", level=logging.ERROR, exc_info=True)
        with _lock:
            _state["release_window"] = None
            _reset_locked(f"measurement window failed: {e}")
    finally:
        entered.set()


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
    return await asyncio.create_subprocess_exec(
        "aplay", "-D", PLAYBACK_DEVICE, "-q", str(wav_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _watch_playback(proc) -> None:
    await proc.wait()
    with _lock:
        if _state.get("playback", {}).get("proc") is proc:
            _state["playback"] = None
            log_event(logger, "sync.marker_finished")


def handle_start(hostname: str, schedule: Callable) -> tuple[dict, int]:
    own, peer, err = resolve_pair()
    if err:
        return {"ok": False, "error": err}, HTTPStatus.CONFLICT
    members = members_by_channel(own, peer, hostname)
    if members is None:
        return {
            "ok": False,
            "error": "pair channels are not one left + one right",
        }, HTTPStatus.CONFLICT

    with _lock:
        if _state["phase"] in _ACTIVE_PHASES:
            return {"ok": False, "error": "a sync session is already running"}, \
                HTTPStatus.CONFLICT
        _reset_locked()
        _state["phase"] = "measuring"
        _state["members"] = members

    entered = threading.Event()
    schedule(_session_window(entered))
    if not entered.wait(WINDOW_OPEN_TIMEOUT_S):
        with _lock:
            _reset_locked("measurement window did not open")
        return {"ok": False, "error": "could not pause the speaker"}, \
            HTTPStatus.INTERNAL_SERVER_ERROR
    with _lock:
        if _state["phase"] != "measuring":
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
    try:
        proc = run_async(_start_playback(_marker_wav_path()), timeout=10.0)
    except Exception as e:  # noqa: BLE001
        log_event(logger, "sync.play_spawn_failed", level=logging.ERROR, exc_info=True)
        return {"ok": False, "error": f"playback failed: {e}"}, \
            HTTPStatus.INTERNAL_SERVER_ERROR
    with _lock:
        _state["playback"] = {"proc": proc}
    schedule(_watch_playback(proc))
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
        _post_grouping_to_member,
        _request_control_token,
        _self_addresses,
    )

    token = _request_control_token(handler)

    with _lock:
        if _state["phase"] != "analyzed" or not _state["recommendation"]:
            return {"ok": False, "error": "nothing to apply"}, HTTPStatus.CONFLICT
        members = _state["members"]
        rec = dict(_state["recommendation"])
    if not members:
        return {"ok": False, "error": "session has no members"}, \
            HTTPStatus.CONFLICT

    # Self is the leader by /sync/start gate; write its existing grouping
    # fields plus the leader-owned rendered-channel delays.
    self_member = next((m for m in members.values() if m["is_self"]), None)
    if self_member is None:
        return {"ok": False, "error": "could not identify leader member"}, \
            HTTPStatus.CONFLICT
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
    ok, detail = _post_grouping_to_member(
        "", body, _self_addresses(), token=token)
    if ok:
        with _lock:
            _state["phase"] = "applied"
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
