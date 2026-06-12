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
import tempfile
import threading
import time
from http import HTTPStatus
from typing import Any, Callable

logger = logging.getLogger("jasper.web.balance")

# Hard ceiling on one walkthrough session: the window-holder coroutine
# self-releases after this long no matter what, so an abandoned phone
# tab can never leave the renderers stopped. Two ~25 s ramps + floor
# sampling + human pace fits comfortably.
SESSION_MAX_S = 300.0

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
    "recommendation": None,
    "applied": None,
    "wav_paths": {},         # channel -> cached ramp WAV path
}


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
    """Build (once per process per channel) the canonical ramp WAV."""
    from jasper.multiroom.balance import write_ramp_wav
    with _lock:
        cached = _state["wav_paths"].get(channel)
    if cached:
        return cached
    f = tempfile.NamedTemporaryFile(
        prefix=f"jasper-balance-{channel}-", suffix=".wav", delete=False)
    f.close()
    write_ramp_wav(f.name, channel)
    with _lock:
        _state["wav_paths"][channel] = f.name
    return f.name


def _resolve_pair() -> tuple[dict | None, dict | None, str]:
    """(self_grouping, peer, error). ``peer`` is {addr, label, grouping}.

    Same one-bond-sibling discovery as /rooms swap and trim — lazy
    imports keep this module import-light and give tests one seam per
    helper."""
    from .rooms_setup import (
        _discover_speakers_cached,
        _resolve_bond_peer,
        _self_addresses,
    )
    from ..multiroom.state import read_grouping_state

    own = read_grouping_state()
    bond_id = str(own.get("bond_id") or "").strip()
    if not own.get("enabled") or not bond_id:
        return None, None, "bond a pair first (jts.local/rooms)"
    if str(own.get("role") or "") != "leader":
        return None, None, "open this page on the pair leader"

    # Shared roster-first resolution (see rooms_setup._resolve_bond_peer)
    # — the household's recorded pair sibling, never an inference that a
    # foreign bond-claimer can poison.
    known = _self_addresses()
    addr, pg, perr = _resolve_bond_peer(own, known)
    if perr:
        return None, None, f"balance {perr}"
    label = str(own.get("peer_name") or "").strip()
    if not label:
        # Legacy bond (no roster name): borrow the directory's display
        # name for the resolved address, falling back to the address.
        label = next(
            (str(r.get("name") or "").strip()
             for r in _discover_speakers_cached()
             if str(r.get("address") or "").strip() == addr
             and str(r.get("name") or "").strip()),
            addr,
        )
    return own, {"addr": addr, "label": label, "grouping": pg}, ""


def _members_by_channel(own: dict, peer: dict, hostname: str) -> dict | None:
    """Map the bond's channel assignment onto {left, right} member
    records carrying address, label, and current trim."""
    self_ch = str(own.get("channel") or "")
    peer_ch = str(peer["grouping"].get("channel") or "")
    if {self_ch, peer_ch} != {"left", "right"}:
        return None
    mine = {
        "addr": "", "is_self": True,
        "label": f"this speaker ({hostname})",
        "trim_db": float(own.get("trim_db") or 0.0),
        "grouping": own,
    }
    theirs = {
        "addr": peer["addr"], "is_self": False,
        "label": peer["label"],
        "trim_db": float(peer["grouping"].get("trim_db") or 0.0),
        "grouping": peer["grouping"],
    }
    return {self_ch: mine, peer_ch: theirs}


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
    """Hold ONE measurement window for the whole walkthrough. Installs
    a thread-safe release callable into state; self-releases after
    SESSION_MAX_S so an abandoned session can't keep renderers
    stopped."""
    from jasper.correction.coordinator import measurement_window
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    with _lock:
        _state["release_window"] = (
            lambda: loop.call_soon_threadsafe(release.set))
    try:
        async with measurement_window():
            entered.set()
            try:
                await asyncio.wait_for(release.wait(), SESSION_MAX_S)
            except asyncio.TimeoutError:
                logger.warning("event=balance.session_timeout")
                with _lock:
                    if _state["phase"] == "measuring":
                        _state["release_window"] = None  # we ARE exiting
                        _reset_locked("session timed out — renderers "
                                      "restored")
    except Exception as e:  # noqa: BLE001 — window entry/exit failure
        logger.exception("event=balance.window_failed")
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
    own, peer, err = _resolve_pair()
    if err:
        return {"ok": False, "error": err}, HTTPStatus.CONFLICT
    members = _members_by_channel(own, peer, hostname)
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
    logger.info("event=balance.session_started")
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
            logger.info("event=balance.ramp_unheard channel=%s", channel)


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
        logger.exception("event=balance.ramp_spawn_failed")
        return ({"ok": False, "error": f"playback failed: {e}"},
                HTTPStatus.INTERNAL_SERVER_ERROR)
    t0 = time.monotonic()
    with _lock:
        _state["ramp_token"] += 1
        token = _state["ramp_token"]
        _state["ramp"] = {"channel": channel, "t0": t0,
                          "proc": proc, "token": token}
    schedule(_watch_ramp(proc, channel, token))
    logger.info("event=balance.ramp_started channel=%s", channel)
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
    logger.info(
        "event=balance.locked channel=%s offset_s=%.2f drive_dbfs=%.1f",
        channel, offset, drive,
    )
    return payload, HTTPStatus.OK


def handle_stop() -> tuple[dict, int]:
    """POST /balance/stop — the big red button. Also /balance/reset."""
    with _lock:
        _reset_locked()
    logger.info("event=balance.stopped")
    return {"ok": True}, HTTPStatus.OK


def handle_apply() -> tuple[dict, int]:
    """POST /balance/apply — one absolute-trim write per member via the
    same /grouping/set wire the bond flow uses. Peer first: if the
    cross-LAN hop fails nothing has changed locally."""
    from .rooms_setup import _post_grouping_to_member, _self_addresses

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
        ok, detail = _post_grouping_to_member(m["addr"], body, known)
        writes[ch] = {"label": m["label"], "ok": ok,
                      "trim_db": rec[f"{ch}_trim_db"], "detail": detail}
        all_ok = all_ok and ok
        logger.info("event=balance.apply ch=%s addr=%s trim=%.1f ok=%s",
                    ch, m["addr"] or "(self)", rec[f"{ch}_trim_db"], ok)
        if not ok:
            break  # don't half-balance further; report what happened

    with _lock:
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
