"""Pair-balance wizard flow (`/balance/*`) — the phone-mic SPL
auto-match surface for a bonded stereo pair (#23 P2).

This module is the HANDLER layer only; it rides inside the
room-correction service's process (see jasper/web/correction_setup.py,
which owns the route allowlists, the CSRF/host guards, the background
asyncio loop, and the WAV body reader — all injected into the
functions here so this module never imports correction_setup back).
Sharing that process is deliberate: balance and correction both open
``measurement_window`` (stop renderers, pause the wake loop), and
in-process state is the only place their mutual exclusion can live —
correction's ``_reserve_start_slot`` consults :func:`active_phase`
here, and :func:`handle_play` is dispatched behind correction's own
idle check.

The flow, one continuous phone capture:

1. ``POST /balance/play`` — gates (leader, bonded, exactly one
   reachable peer, nothing else measuring), then synchronously plays
   the left/right/left burst WAV through the NORMAL bonded chain
   inside a measurement window. The browser starts recording BEFORE
   posting and stops when the response lands; the schedule's lead-in
   and the alignment step absorb the start-time slop.
2. ``POST /balance/upload-capture`` — evaluates the capture
   (jasper/multiroom/balance.py gates: alignment, clipping, gap-SNR,
   A/B/A drift) and maps the channel delta onto the two MEMBERS using
   the bond's channel assignment, composing with each member's
   current trim. A rejected capture keeps the flow in
   ``awaiting_capture`` so the user can re-try the take without
   replaying (the wizard offers replay too).
3. ``POST /balance/apply`` — ONE ``/grouping/set`` write per member
   (peer first, then self), absolute trims, batched — unlike the
   /rooms ±0.5 nudge rows there is no per-click outputd restart
   churn. Partial failure is reported per-member; the write is
   idempotent so retry is safe.

State is a module-level dict guarded by a lock (mirrors the
correction session singleton — one wizard, one household, one flow at
a time). ``awaiting_capture`` expires lazily after
``CAPTURE_DEADLINE_S`` so an abandoned phone tab can't wedge the
measurement mutex forever.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from http import HTTPStatus
from typing import Any, Callable

logger = logging.getLogger("jasper.web.balance")

# How long after playback we keep waiting for the capture upload
# before the flow lazily resets to idle (phone tab closed mid-flow).
CAPTURE_DEADLINE_S = 120.0

# Phases that must block a new correction session (and a new /play).
_ACTIVE_PHASES = frozenset({"playing", "awaiting_capture"})

_lock = threading.Lock()
_state: dict[str, Any] = {
    "phase": "idle",
    "error": "",
    "schedule": None,
    "wav_path": None,
    "members": None,
    "result": None,
    "recommendation": None,
    "applied": None,
    "deadline": 0.0,
}


def _expire_locked() -> None:
    if (_state["phase"] == "awaiting_capture"
            and time.monotonic() > _state["deadline"]):
        _reset_locked("capture window expired")


def _reset_locked(error: str = "") -> None:
    _state.update({
        "phase": "idle", "error": error, "schedule": None,
        "members": None, "result": None, "recommendation": None,
        "applied": None, "deadline": 0.0,
    })


def active_phase() -> str | None:
    """The phase if a balance run currently owns the measurement
    surface, else None. Correction's /start reservation consults this
    so a balance run can't be trampled by a sweep (and vice versa —
    /play is dispatched behind correction's own idle check)."""
    with _lock:
        _expire_locked()
        return _state["phase"] if _state["phase"] in _ACTIVE_PHASES else None


def _balance_wav_path() -> tuple[str, Any]:
    """Build (once per process) the canonical burst WAV; returns
    (path, schedule). The schedule is deterministic so the cached file
    never goes stale within a process lifetime."""
    from jasper.multiroom.balance import (
        build_balance_schedule,
        write_balance_wav,
    )
    with _lock:
        if _state["wav_path"] is None:
            schedule = build_balance_schedule()
            f = tempfile.NamedTemporaryFile(
                prefix="jasper-balance-", suffix=".wav", delete=False)
            f.close()
            write_balance_wav(f.name, schedule)
            _state["wav_path"] = (f.name, schedule)
        return _state["wav_path"]


def _resolve_pair() -> tuple[dict | None, dict | None, str]:
    """(self_grouping, peer, error). ``peer`` is {addr, label, grouping}.

    Same one-bond-sibling discovery as /rooms swap and trim — lazy
    imports keep this module import-light and give tests one seam per
    helper."""
    from .rooms_setup import (
        _discover_speakers_cached,
        _get_member_grouping,
        _map_peers,
        _self_addresses,
    )
    from ..multiroom.state import read_grouping_state

    own = read_grouping_state()
    bond_id = str(own.get("bond_id") or "").strip()
    if not own.get("enabled") or not bond_id:
        return None, None, "bond a pair first (jts.local/rooms)"
    if str(own.get("role") or "") != "leader":
        return None, None, "open this page on the pair leader"

    known = _self_addresses()
    rows = [r for r in _discover_speakers_cached()
            if str(r.get("address") or "").strip()
            and str(r.get("address") or "").strip() not in known]
    groupings = _map_peers(
        lambda a: _get_member_grouping(a, known),
        [str(r["address"]).strip() for r in rows],
    )
    peers = [
        {"addr": str(row["address"]).strip(),
         "label": str(row.get("name") or row.get("hostname")
                      or row["address"]),
         "grouping": pg}
        for row, pg in zip(rows, groupings)
        if pg is not None
        and str(pg.get("bond_id") or "").strip() == bond_id
    ]
    if len(peers) != 1:
        return None, None, (
            "balance needs exactly one reachable paired speaker "
            f"(found {len(peers)})"
        )
    return own, peers[0], ""


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


def handle_status() -> dict:
    """GET /balance/status — phase + everything the page renders."""
    from ..multiroom.state import read_grouping_state
    own = read_grouping_state()
    with _lock:
        _expire_locked()
        out = {
            "phase": _state["phase"],
            "error": _state["error"],
            "bonded": bool(own.get("enabled"))
            and not own.get("error"),
            "role": str(own.get("role") or ""),
            "result": _state["result"],
            "recommendation": _state["recommendation"],
            "applied": _state["applied"],
        }
        if _state["members"]:
            out["members"] = _public_members(_state["members"])
        return out


def handle_play(hostname: str, run_async: Callable) -> tuple[dict, int]:
    """POST /balance/play — gate, then play the burst sequence inside a
    measurement window. Blocks until playback finishes (~10 s) so the
    page's recording brackets it. ``run_async`` is correction_setup's
    bridge onto its background loop."""
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
        _expire_locked()
        if _state["phase"] in _ACTIVE_PHASES:
            return ({"ok": False,
                     "error": f"balance run already {_state['phase']}"},
                    HTTPStatus.CONFLICT)
        _reset_locked()
        _state["phase"] = "playing"
        _state["members"] = members

    wav_path, schedule = _balance_wav_path()

    async def _play_inside_window() -> None:
        from jasper.correction.coordinator import measurement_window
        from jasper.correction.playback import play_sweep
        async with measurement_window():
            await play_sweep(wav_path)

    try:
        run_async(_play_inside_window(),
                  timeout=schedule.total_s + 45.0)
    except Exception as e:  # noqa: BLE001 — any failure resets the flow
        logger.exception("event=balance.play_failed")
        with _lock:
            _reset_locked(f"playback failed: {e}")
        return ({"ok": False, "error": f"playback failed: {e}"},
                HTTPStatus.INTERNAL_SERVER_ERROR)

    with _lock:
        _state["phase"] = "awaiting_capture"
        _state["schedule"] = schedule
        _state["deadline"] = time.monotonic() + CAPTURE_DEADLINE_S
        members_out = _public_members(_state["members"])
    logger.info("event=balance.played total_s=%.1f", schedule.total_s)
    return {"ok": True, "schedule": schedule.to_dict(),
            "members": members_out}, HTTPStatus.OK


def handle_upload(raw_wav: bytes) -> tuple[dict, int]:
    """POST /balance/upload-capture — evaluate the take; on success
    store the trim recommendation, on a gated rejection stay in
    ``awaiting_capture`` so the phone can re-record without replaying."""
    from jasper.correction.sweep import read_wav_mono
    from jasper.multiroom.balance import evaluate_capture, recommend_trims

    with _lock:
        _expire_locked()
        if _state["phase"] != "awaiting_capture":
            return ({"ok": False,
                     "error": f"no capture expected (phase "
                              f"{_state['phase']})"},
                    HTTPStatus.CONFLICT)
        schedule = _state["schedule"]
        members = _state["members"]

    with tempfile.NamedTemporaryFile(
            prefix="jasper-balance-cap-", suffix=".wav") as f:
        f.write(raw_wav)
        f.flush()
        samples, sr = read_wav_mono(f.name)
    result = evaluate_capture(samples, sr, schedule)

    if not result.ok:
        with _lock:
            if _state["phase"] == "awaiting_capture":
                _state["deadline"] = time.monotonic() + CAPTURE_DEADLINE_S
        logger.info("event=balance.capture_rejected reason=%s",
                    result.reason)
        return {"ok": False, "rejected": True,
                "result": result.to_dict()}, HTTPStatus.OK

    rec = recommend_trims(
        result.delta_db,
        current_left_trim_db=members["left"]["trim_db"],
        current_right_trim_db=members["right"]["trim_db"],
    )
    with _lock:
        _state["phase"] = "analyzed"
        _state["result"] = result.to_dict()
        _state["recommendation"] = rec.to_dict()
        members_out = _public_members(members)
    logger.info(
        "event=balance.analyzed delta_db=%.2f left=%.1f right=%.1f "
        "clamped=%s", result.delta_db, rec.left_trim_db,
        rec.right_trim_db, rec.clamped,
    )
    return {"ok": True, "result": result.to_dict(),
            "recommendation": rec.to_dict(),
            "members": members_out}, HTTPStatus.OK


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


def handle_reset() -> tuple[dict, int]:
    """POST /balance/reset — back to idle (abandon or measure again)."""
    with _lock:
        _reset_locked()
    return {"ok": True}, HTTPStatus.OK


_PAGE_CSS = """
.bal-card { max-width: 560px; }
.bal-steps { margin: 0 0 1rem; padding-left: 1.2rem; }
.bal-steps li { margin: 0.25rem 0; }
.bal-status { min-height: 1.4em; margin: 0.8rem 0; font-weight: 600; }
.bal-status[data-tone="bad"] { color: var(--status-danger); }
.bal-status[data-tone="ok"] { color: var(--status-ok); }
.bal-members { display: none; margin: 0.6rem 0 0.2rem; }
.bal-members .row {
  display: flex; justify-content: space-between; gap: 1rem;
  padding: 0.35rem 0; border-bottom: 1px solid
    color-mix(in oklab, currentColor 12%, transparent);
}
.bal-members .row .lvl { font-variant-numeric: tabular-nums; }
.bal-verdict { margin: 0.8rem 0; }
.bal-actions { display: flex; gap: 0.6rem; flex-wrap: wrap; }
button[disabled] { opacity: 0.55; }
"""

_PAGE_BODY = """
<main class="page">
  <p class="eyebrow">Stereo pair</p>
  <h1>Balance speakers</h1>
  <section class="info-card bal-card">
    <p>Match the loudness of the two speakers using this phone's
    microphone. Music pauses for about ten seconds while each speaker
    plays a short test sound.</p>
    <ol class="bal-steps">
      <li>Sit or stand where you normally listen.</li>
      <li>Hold the phone still at chest height, screen up.</li>
      <li>Tap Start and keep the phone steady until the result
      appears.</li>
    </ol>
    <div class="bal-status" id="status" data-tone=""></div>
    <div class="bal-members" id="members"></div>
    <div class="bal-verdict" id="verdict"></div>
    <div class="bal-actions">
      <button class="btn btn--primary" id="start">Start
      measurement</button>
      <button class="btn" id="apply" hidden>Apply</button>
      <button class="btn" id="again" hidden>Measure again</button>
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
