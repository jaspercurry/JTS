"""Pair-balance wizard flow (`/balance/*`) — the one-speaker-at-a-time
equal-loudness walkthrough (#23 P2, redesigned after live use).

This module is the HANDLER layer only; it rides inside the
room-correction service's process (jasper/web/correction_setup.py owns
the route allowlists, CSRF/host guards, and the background asyncio
loop, injected as ``run_async`` / ``schedule`` so this module never
imports correction_setup back). Sharing that process is deliberate:
balance and correction both open ``measurement_window``, and
in-process state is the only place their mutual exclusion can live —
correction's ``_reserve_start_slot`` consults :func:`active_phase`,
and :func:`handle_start` is dispatched behind correction's idle check.

The walkthrough:

1. ``POST /balance/start`` — gates (bonded leader, exactly one
   reachable same-bond peer, channels {left,right}), then opens ONE
   measurement window held for the whole session (renderers stay
   quiet BETWEEN speakers — music doesn't blare back mid-walkthrough).
   A session watchdog (``SESSION_MAX_S``) guarantees the window is
   released even if the phone tab dies.
2. ``POST /balance/ramp {channel}`` — plays that channel's noise ramp
   (near-silent → -12 dBFS ceiling, see jasper/multiroom/balance.py)
   through the normal bonded chain. Returns immediately; a watcher
   marks the channel ``not_heard`` if the WAV ends with no lock — the
   actionable per-speaker error ("couldn't hear the left speaker").
   Re-POSTing the same channel retries it.
3. ``POST /balance/lock {channel}`` — the phone's meter crossed its
   target. The DRIVE level is derived server-side from
   ``monotonic() - t0`` via the pure ramp-emission function; locks
   that arrive before the ramp meaningfully started (noise transient)
   get ``keep_listening`` and playback continues. Both channels
   locked → trims recommended (drive delta composed with current
   member trims) and the window is released.
4. ``POST /balance/apply`` — one absolute ``/grouping/set`` per
   member, peer first (a failed cross-LAN hop changes nothing
   locally; partial failure reported per-member; idempotent retry).
5. ``POST /balance/stop`` — the big red button: kill playback,
   release the window, reset. ``/balance/reset`` is the same from
   terminal phases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from http import HTTPStatus
from typing import Any, Callable

from jasper.log_event import log_event

logger = logging.getLogger("jasper.web.balance")

# INACTIVITY ceiling on a held measurement window. The window-holder
# coroutine releases this long after the LAST session activity (a ramp
# start, a lock, or a ramp ending unheard) — never a fixed wall from
# window-open. Two consequences, both intended: an ACTIVE walkthrough
# keeps bumping the deadline so it is never yanked mid-use however slow
# or retry-heavy the household is; an ABANDONED session (phone tab
# backgrounded at a retry prompt, pagehide stop never landed) releases
# the renderers — which `measurement_window` both stops AND pauses the
# wake loop for — within one idle window. Must comfortably exceed one
# ramp (~24.5 s) plus human decide-to-retry time: a ramp start bumps
# the deadline, the next bump is the lock or the unheard transition
# ~24.5 s later, then the user reads and decides.
IDLE_TIMEOUT_S = 90.0

# How long /start waits for the measurement window to actually open
# (renderer stops + voice pause) before reporting failure.
WINDOW_OPEN_TIMEOUT_S = 20.0

PLAYBACK_DEVICE = "correction_substream"

# Phases that must block a new correction session (and a new /start).
# "analyzed"/"applied" do NOT hold the window.
_ACTIVE_PHASES = frozenset({"measuring"})

_lock = threading.Lock()
_state: dict[str, Any] = {
    "phase": "idle",
    "error": "",
    "members": None,
    "locks": {},            # channel -> {drive_dbfs, offset_s} | {not_heard}
    "ramp": None,           # {channel, t0, proc, token} while playing
    "ramp_token": 0,
    "release_window": None,  # thread-safe callable set by the holder
    "idle_deadline": 0.0,    # monotonic; bumped by each session activity
    "recommendation": None,
    "applied": None,
    "wav_paths": {},         # channel -> cached ramp WAV path
}


def _bump_activity_locked() -> None:
    """Push the inactivity deadline out one IDLE_TIMEOUT_S from now.
    Called under ``_lock`` on every session activity (ramp start, lock,
    ramp-ended-unheard) so an active walkthrough never times out and an
    abandoned one releases within one idle window. See IDLE_TIMEOUT_S."""
    _state["idle_deadline"] = time.monotonic() + IDLE_TIMEOUT_S


def _reset_locked(error: str = "") -> None:
    ramp = _state.get("ramp")
    if ramp and ramp.get("proc") is not None:
        try:
            ramp["proc"].terminate()
        except ProcessLookupError:
            pass
    release = _state.get("release_window")
    _state.update({
        "phase": "idle", "error": error, "members": None,
        "locks": {}, "ramp": None, "release_window": None,
        "recommendation": None, "applied": None,
    })
    if release is not None:
        release()


def active_phase() -> str | None:
    """The phase if a balance session currently owns the measurement
    surface, else None. Correction's /start reservation consults this
    (and /balance/start is dispatched behind correction's own idle
    check)."""
    with _lock:
        return _state["phase"] if _state["phase"] in _ACTIVE_PHASES else None


def _read_json(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        return {}
    if length <= 0 or length > 65536:
        return {}
    try:
        parsed = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ramp_wav_path(channel: str) -> str:
    """Path to this channel's ramp WAV, rendered once per process.

    Fixed name under the temp dir, not a unique tempfile: each ramp WAV
    is ~4.7 MB (24.5 s stereo 48 kHz S16) and the correction service is
    socket-activated, so unique-per-process files would accumulate
    multi-MB orphans in tmpfs across restarts on the 1 GB Pi. A stable
    name bounds it to two files total, overwritten (the content is
    deterministic) the first time each channel is needed in a process —
    so a deploy that changes the ramp is picked up on the next start.
    Predictable path is acceptable here: single-user appliance, sticky
    temp dir, non-secret band-limited noise."""
    from jasper.multiroom.balance import write_ramp_wav
    with _lock:
        cached = _state["wav_paths"].get(channel)
    if cached:
        return cached
    path = os.path.join(
        tempfile.gettempdir(), f"jasper-balance-{channel}.wav")
    write_ramp_wav(path, channel)
    with _lock:
        _state["wav_paths"][channel] = path
    return path


def _public_members(members: dict) -> dict:
    return {
        ch: {"label": m["label"], "is_self": m["is_self"],
             "trim_db": round(m["trim_db"], 1)}
        for ch, m in members.items()
    }


def _public_locks(locks: dict) -> dict:
    out = {}
    for ch, info in locks.items():
        if info.get("not_heard"):
            out[ch] = {"not_heard": True}
        else:
            out[ch] = {"drive_dbfs": round(info["drive_dbfs"], 1)}
    return out


def handle_status() -> dict:
    """GET /balance/status — phase + everything the page renders."""
    from jasper.multiroom.balance import ramp_duration_s
    from ..multiroom.state import read_grouping_state
    own = read_grouping_state()
    with _lock:
        out = {
            "phase": _state["phase"],
            "error": _state["error"],
            "bonded": bool(own.get("enabled")) and not own.get("error"),
            "role": str(own.get("role") or ""),
            "ramping": (_state["ramp"] or {}).get("channel", ""),
            "locks": _public_locks(_state["locks"]),
            "ramp_duration_s": round(ramp_duration_s(), 1),
            "recommendation": _state["recommendation"],
            "applied": _state["applied"],
        }
        if _state["members"]:
            out["members"] = _public_members(_state["members"])
        return out


async def _session_window(entered: threading.Event) -> None:
    """Hold ONE measurement window for the whole walkthrough, releasing
    it on explicit release OR IDLE_TIMEOUT_S of inactivity. Installs a
    thread-safe release callable into state. The deadline is re-checked
    after each bounded wait, so a bump (ramp/lock/unheard) extends the
    hold without the holder having to be signalled — an active session
    is never yanked mid-use; an abandoned one releases within one idle
    window (renderers + the wake loop come back)."""
    from jasper.correction.coordinator import measurement_window
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    with _lock:
        _state["release_window"] = (
            lambda: loop.call_soon_threadsafe(release.set))
        _bump_activity_locked()
    try:
        async with measurement_window():
            entered.set()
            while True:
                with _lock:
                    remaining = _state["idle_deadline"] - time.monotonic()
                if remaining <= 0:
                    with _lock:
                        if _state["phase"] == "measuring":
                            _state["release_window"] = None  # we ARE exiting
                            _reset_locked("balance timed out after "
                                          "inactivity — renderers restored")
                    log_event(logger, "balance.idle_timeout", level=logging.WARNING)
                    break
                try:
                    await asyncio.wait_for(release.wait(), remaining)
                    break  # explicit release (lock-complete / stop)
                except asyncio.TimeoutError:
                    continue  # deadline may have been bumped — re-check
    except Exception as e:  # noqa: BLE001 — window entry/exit failure
        log_event(logger, "balance.window_failed", level=logging.ERROR, exc_info=True)
        with _lock:
            _state["release_window"] = None
            _reset_locked(f"measurement window failed: {e}")
    finally:
        entered.set()  # never leave /start blocked


def handle_start(
    hostname: str, schedule: Callable,
) -> tuple[dict, int]:
    """POST /balance/start — gate, then open the session window.
    ``schedule`` fires a coroutine on the correction service's
    background loop without blocking."""
    from .pair_flow import members_by_channel, resolve_pair

    own, peer, err = resolve_pair()
    if err:
        return {"ok": False, "error": err}, HTTPStatus.CONFLICT
    members = members_by_channel(own, peer, hostname)
    if members is None:
        return {"ok": False, "error": (
            "pair channels are not one left + one right — repair via "
            "the swap control on jts.local/rooms"
        )}, HTTPStatus.CONFLICT

    with _lock:
        if _state["phase"] in _ACTIVE_PHASES:
            return ({"ok": False, "error": "a balance session is "
                     "already running"}, HTTPStatus.CONFLICT)
        _reset_locked()
        _state["phase"] = "measuring"
        _state["members"] = members

    entered = threading.Event()
    schedule(_session_window(entered))
    if not entered.wait(WINDOW_OPEN_TIMEOUT_S):
        with _lock:
            _reset_locked("measurement window did not open")
        return ({"ok": False, "error": "could not pause the speaker "
                 "for measurement"}, HTTPStatus.INTERNAL_SERVER_ERROR)
    with _lock:
        if _state["phase"] != "measuring":  # holder failed on entry
            err = _state["error"] or "could not pause the speaker"
            return {"ok": False, "error": err}, \
                HTTPStatus.INTERNAL_SERVER_ERROR
        members_out = _public_members(_state["members"])
    log_event(logger, "balance.session_started")
    return {"ok": True, "members": members_out}, HTTPStatus.OK


async def _start_playback(wav_path: str):
    """Spawn aplay on the bonded music lane. Local (not
    correction.playback.play_sweep) because the walkthrough needs a
    HANDLE it can terminate on lock/stop — play_sweep only offers
    blocking play-to-completion."""
    return await asyncio.create_subprocess_exec(
        "aplay", "-D", PLAYBACK_DEVICE, "-q", str(wav_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _watch_ramp(proc, channel: str, token: int) -> None:
    """When a ramp's WAV ends naturally with no lock, the speaker was
    never heard at any test level — record the per-speaker error."""
    await proc.wait()
    with _lock:
        ramp = _state["ramp"]
        if ramp and ramp.get("token") == token:
            _state["ramp"] = None
            _state["locks"][channel] = {"not_heard": True}
            _bump_activity_locked()  # the user now reads + decides to retry
            log_event(logger, "balance.ramp_unheard", channel=channel)


def handle_ramp(
    handler, run_async: Callable, schedule: Callable,
) -> tuple[dict, int]:
    """POST /balance/ramp {channel} — start (or retry) one speaker's
    ramp. Returns as soon as playback has spawned; the lock/stop/
    watcher paths own its lifetime."""
    from jasper.multiroom.balance import CHANNELS, ramp_duration_s

    channel = str(_read_json(handler).get("channel") or "").strip()
    if channel not in CHANNELS:
        return ({"ok": False, "error": "channel must be left|right"},
                HTTPStatus.BAD_REQUEST)
    with _lock:
        if _state["phase"] != "measuring":
            return ({"ok": False, "error":
                     f"no session (phase {_state['phase']})"},
                    HTTPStatus.CONFLICT)
        if _state["ramp"] is not None:
            return ({"ok": False, "error": "a ramp is already playing"},
                    HTTPStatus.CONFLICT)
        _state["locks"].pop(channel, None)  # retry clears the old answer

    wav_path = _ramp_wav_path(channel)
    try:
        proc = run_async(_start_playback(wav_path), timeout=10.0)
    except Exception as e:  # noqa: BLE001
        log_event(logger, "balance.ramp_spawn_failed", level=logging.ERROR, exc_info=True)
        return ({"ok": False, "error": f"playback failed: {e}"},
                HTTPStatus.INTERNAL_SERVER_ERROR)
    t0 = time.monotonic()
    with _lock:
        _state["ramp_token"] += 1
        token = _state["ramp_token"]
        _state["ramp"] = {"channel": channel, "t0": t0,
                          "proc": proc, "token": token}
        _bump_activity_locked()  # ramp underway — the next bump is lock/unheard
    schedule(_watch_ramp(proc, channel, token))
    log_event(logger, "balance.ramp_started", channel=channel)
    return {"ok": True, "channel": channel,
            "duration_s": round(ramp_duration_s(), 1)}, HTTPStatus.OK


def handle_lock(handler) -> tuple[dict, int]:
    """POST /balance/lock {channel} — the phone's meter crossed its
    target. Drive level is derived from the server-side clock against
    the pure ramp-emission function; spawn/buffer/LAN latencies are
    identical for both speakers and cancel in the delta."""
    from jasper.multiroom.balance import (
        MIN_LOCK_OFFSET_S,
        drive_delta_db,
        ramp_emission_dbfs,
        recommend_trims,
    )

    channel = str(_read_json(handler).get("channel") or "").strip()
    now = time.monotonic()
    with _lock:
        ramp = _state["ramp"]
        if (_state["phase"] != "measuring" or ramp is None
                or ramp["channel"] != channel):
            return ({"ok": False, "error": "no matching ramp playing"},
                    HTTPStatus.CONFLICT)
        offset = now - ramp["t0"]
        drive = ramp_emission_dbfs(offset)
        if drive is None or offset < MIN_LOCK_OFFSET_S:
            # Noise transient before the speaker was meaningfully
            # audible — keep playing, phone keeps listening.
            return {"ok": False, "keep_listening": True,
                    "offset_s": round(offset, 2)}, HTTPStatus.OK
        try:
            ramp["proc"].terminate()
        except ProcessLookupError:
            pass
        _state["ramp"] = None
        _state["locks"][channel] = {
            "offset_s": offset, "drive_dbfs": drive}
        _bump_activity_locked()  # first-channel lock; window held for the 2nd
        locks = _state["locks"]
        members = _state["members"]
        both = all(
            ch in locks and "drive_dbfs" in locks[ch]
            for ch in ("left", "right")
        )
        if both:
            delta = drive_delta_db(
                locks["left"]["drive_dbfs"],
                locks["right"]["drive_dbfs"],
            )
            rec = recommend_trims(
                delta,
                current_left_trim_db=members["left"]["trim_db"],
                current_right_trim_db=members["right"]["trim_db"],
            )
            _state["recommendation"] = dict(
                rec.to_dict(), delta_db=round(delta, 2))
            _state["phase"] = "analyzed"
            release = _state.get("release_window")
            _state["release_window"] = None
        payload = {
            "ok": True, "channel": channel,
            "drive_dbfs": round(drive, 1),
            "phase": _state["phase"],
            "locks": _public_locks(locks),
            "recommendation": _state["recommendation"],
            "members": _public_members(members),
        }
    if both and release is not None:
        release()  # renderers come back while the user reads the result
    log_event(
        logger,
        "balance.locked",
        channel=channel,
        offset_s=f"{offset:.2f}",
        drive_dbfs=f"{drive:.1f}",
    )
    return payload, HTTPStatus.OK


def handle_stop() -> tuple[dict, int]:
    """POST /balance/stop — the big red button. Also /balance/reset."""
    with _lock:
        _reset_locked()
    log_event(logger, "balance.stopped")
    return {"ok": True}, HTTPStatus.OK


def handle_apply(handler) -> tuple[dict, int]:
    """POST /balance/apply — one absolute-trim write per member via the
    same /grouping/set wire the bond flow uses. Peer first: if the
    cross-LAN hop fails nothing has changed locally.

    ``handler`` carries the browser-supplied ``X-JTS-Token`` so we forward
    it to each member's /grouping/set, exactly as the /rooms bond fan-out
    does. /grouping/set is one of jasper-control's MANDATORY token-gated
    mutations (WS1 Phase 2 auto-arms the gate), so a tokenless write — in
    particular the loopback write to THIS speaker — is rejected 403. The
    page already carries the token (``meta[name=jts-control-token]`` via
    ``canonical_page``); relaying it keeps Apply working on a gate-armed
    speaker."""
    from .rooms_setup import (
        _post_grouping_to_member,
        _request_control_token,
        _self_addresses,
    )

    token = _request_control_token(handler)
    with _lock:
        if _state["phase"] != "analyzed":
            return ({"ok": False,
                     "error": f"nothing to apply (phase {_state['phase']})"},
                    HTTPStatus.CONFLICT)
        members = _state["members"]
        rec = _state["recommendation"]

    known = _self_addresses()
    writes: dict[str, dict] = {}
    order = sorted(
        ("left", "right"), key=lambda ch: members[ch]["is_self"])
    all_ok = True
    for ch in order:
        m = members[ch]
        g = m["grouping"]
        body = {
            "enabled": True,
            "role": str(g.get("role") or ""),
            "channel": str(g.get("channel") or ""),
            "bond_id": str(g.get("bond_id") or ""),
            "leader_addr": str(g.get("leader_addr") or ""),
            "trim_db": rec[f"{ch}_trim_db"],
        }
        ok, detail = _post_grouping_to_member(
            m["addr"], body, known, token=token)
        writes[ch] = {"label": m["label"], "ok": ok,
                      "trim_db": rec[f"{ch}_trim_db"], "detail": detail}
        all_ok = all_ok and ok
        log_event(
            logger,
            "balance.apply",
            ch=ch,
            addr=m["addr"] or "(self)",
            trim=f"{rec[f'{ch}_trim_db']:.1f}",
            ok=ok,
        )
        if not ok:
            break  # don't half-balance further; report what happened

    with _lock:
        # A concurrent /stop (or a fresh /start) during the blocking
        # POSTs above may have moved us out of "analyzed"; honour that
        # rather than resurrecting a dead session to "applied".
        if _state["phase"] == "analyzed":
            if all_ok:
                _state["phase"] = "applied"
                _state["applied"] = writes
            else:
                _state["error"] = "apply failed — see member detail"
    status = HTTPStatus.OK if all_ok else HTTPStatus.BAD_GATEWAY
    return {"ok": all_ok, "writes": writes}, status


_PAGE_CSS = """
.bal-card { max-width: 560px; }
.bal-steps { margin: 0 0 1rem; padding-left: 1.2rem; }
.bal-steps li { margin: 0.25rem 0; }
.bal-status { min-height: 1.4em; margin: 0.8rem 0; font-weight: 600; }
.bal-status[data-tone="bad"] { color: var(--status-danger); }
.bal-status[data-tone="ok"] { color: var(--status-ok); }
.bal-meter { position: relative; height: 14px; border-radius: 7px;
  background: color-mix(in oklab, currentColor 10%, transparent);
  overflow: hidden; margin: 0.6rem 0 0.2rem; }
.bal-meter .fill { position: absolute; inset: 0 auto 0 0; width: 0;
  background: var(--status-ok); border-radius: 7px;
  transition: width 120ms linear; }
.bal-meter .target { position: absolute; top: -2px; bottom: -2px;
  width: 2px; background: var(--status-danger); }
.bal-meter-row { display: flex; justify-content: space-between;
  font-variant-numeric: tabular-nums; margin-bottom: 0.8rem; }
.bal-progress { display: none; margin: 0.6rem 0 0.2rem; }
.bal-progress .row {
  display: flex; justify-content: space-between; gap: 1rem;
  padding: 0.35rem 0; border-bottom: 1px solid
    color-mix(in oklab, currentColor 12%, transparent);
}
.bal-progress .row .lvl { font-variant-numeric: tabular-nums; }
.bal-verdict { margin: 0.8rem 0; }
.bal-actions { display: flex; gap: 0.6rem; flex-wrap: wrap; }
#stop { background: var(--status-danger); color: #fff; }
button[disabled] { opacity: 0.55; }
"""

_PAGE_BODY = """
<main class="page">
  <p class="eyebrow">Stereo pair</p>
  <h1>Balance speakers</h1>
  <section class="info-card bal-card">
    <p>One speaker at a time plays a test sound that starts almost
    silent and slowly gets louder; the moment this phone hears it
    clearly, that speaker is done. Music pauses during the
    walkthrough.</p>
    <ol class="bal-steps">
      <li>Sit or stand where you normally listen.</li>
      <li>Hold the phone still at chest height, screen up.</li>
      <li>Tap Start and keep the phone steady.</li>
    </ol>
    <div class="bal-status" id="status" data-tone=""></div>
    <div class="bal-meter" id="meter" hidden>
      <div class="fill" id="meter-fill"></div>
      <div class="target" id="meter-target"></div>
    </div>
    <div class="bal-meter-row" id="meter-row" hidden>
      <span>mic level</span><span id="meter-db">—</span>
    </div>
    <div class="bal-progress" id="progress"></div>
    <div class="bal-verdict" id="verdict"></div>
    <div class="bal-actions">
      <button class="btn btn--primary" id="start">Start
      walkthrough</button>
      <button class="btn" id="stop" hidden>Stop</button>
      <button class="btn" id="retry" hidden>Retry this
      speaker</button>
      <button class="btn" id="apply" hidden>Apply</button>
      <button class="btn" id="again" hidden>Start over</button>
    </div>
  </section>
</main>
<script type="module" src="/assets/balance/js/main.js"></script>
"""


def render_page(csrf_token: str) -> bytes:
    """GET /balance — the wizard shell; all state arrives via fetch."""
    from ._common import canonical_page
    return canonical_page(
        "Balance speakers",
        _PAGE_BODY,
        csrf_token=csrf_token,
        page_css=_PAGE_CSS,
    )
