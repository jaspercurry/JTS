# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bonded-leader AirPlay latency-fit assessment — OBSERVABILITY ONLY.

This module does not touch the offset derivation (that lives in
``deploy/bin/jasper-apply-airplay-mode``), the reconciler write path, or
any audio path. It only answers a question for ``/state`` and
``jasper-doctor``: *when this speaker is an active bonded leader receiving
AirPlay, does its hidden downstream delay fit inside the budget the
AirPlay sender negotiated?*

Why this can fail to fit (the "Stage D" gap deferred in
``docs/HANDOFF-airplay.md`` "AirPlay 2 latency is sender-authored — the
bonded-leader consequence" and ``docs/HANDOFF-multiroom.md`` open
question #2):

  - AP2 latency is **sender-authored**: the sender ships a PTP anchor and
    delays its own on-screen video to match. The receiver compensates its
    own hidden downstream delay by feeding frames early
    (``audio_backend_latency_offset_in_seconds``), but realized early-play
    is capped by the pre-roll the sender's budget provides.
  - A bonded LEADER plays its own channel through the Snapcast round-trip,
    inserting ``buffer_ms`` (default 400 ms) on top of the ~150 ms solo
    pipeline. ``jasper-grouping-reconcile`` folds that into the offset, but
    if the sender's budget is smaller than ~150 ms + ``buffer_ms`` the
    offset cannot fully fit and playout lands with bounded residual
    lip-sync lag.

The negotiated budget is knowable only from shairport's journal: shairport
logs ``Notified latency is N frames.`` **only when N != 77175**, so the
ABSENCE of that line means the default 77175 frames (~1.75 s; ~2.0 s with
shairport's fixed +11035) — the comfortable/"free" regime. Empirically
(jts.local, 2026-06-21) every observed real AP2 session used the default
budget, so the tight regime is expected to be rare; this surface is
deliberately quiet (warns only when the budget genuinely does not fit) and
cheap (the journal is read only when this speaker is actually a bonded
leader). The authoritative *reactive* signal — shairport's own "stream
latency too short to accommodate an offset" warning — is surfaced
separately through the AirPlay health sampler's event ring
(:func:`jasper.control.airplay_health.classify_journal_line`).

Pure/IO split mirrors the rest of the multiroom package
(:func:`jasper.multiroom.state.derive_grouping_runtime` pure +
``read_unit_active_states`` IO): :func:`assess_fit` is a PURE, total
function of (buffer_ms, notified_frames); :func:`read_notified_frames` is
the thin, bounded, fail-soft journal edge (injectable for tests).
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import GroupingConfig, is_active_leader

# --- shairport / AP2 contract constants ---
# All cross-checked against the live shairport-sync binary's format strings
# and scripts/airplay-latency-probe.sh / docs/HANDOFF-airplay.md.

# shairport's default notified latency; the ABSENCE of a "Notified latency"
# line in the journal means the sender used exactly this value.
AP2_DEFAULT_NOTIFIED_FRAMES = 77175
# Fixed term shairport adds inside the PTP anchor (the value the backend
# latency offset lives inside): set_ptp_anchor_info(..., frame_1 - 11035
# - added_latency, ...).
SHAIRPORT_FIXED_ADD_FRAMES = 11035
# AP2 RTP audio frame clock.
AIRPLAY_FRAME_RATE_HZ = 44100
# The solo speaker's fixed downstream delay above the AP2 anchor baseline
# (CamillaDSP + fan-in + outputd). The derived solo offset is ~-0.1493 s;
# 0.150 is the documented round-number estimate the tight-regime threshold
# uses. First-order on purpose — this surface flags the regime, it does not
# re-derive the offset. GROUND TRUTH for the real per-box value:
# derive_audio_backend_latency_offset in deploy/bin/jasper-apply-airplay-mode
# (a live sum of target_level/chunksize/fan-in/outputd frames). Re-check this
# constant if those pipeline buffer defaults change materially.
PIPELINE_FIXED_DELAY_SEC = 0.150
# shairport's own desired output backend buffer
# (audio_backend_buffer_desired_length_in_seconds in
# deploy/shairport-sync.conf.template) — it sits ALONGSIDE the offset inside
# the AP2 budget. shairport refuses (and DROPS) the whole latency offset when
# budget < |offset| + this buffer, so the bonded-leader need must clear
# need + this term, not just need. Verified against shairport-sync rtp.c
# (rtp_ap2_control_receiver: net_latency<=0 → "too short" + offset dropped).
SHAIRPORT_BACKEND_BUFFER_SEC = 0.5

# Float slack so an exactly-fits budget is not reported as tight.
_FIT_EPSILON_SEC = 1e-6

# How far back to look for the sender's most-recent notified latency. A
# bond is long-lived; a 30-minute window comfortably covers the current /
# most-recent AirPlay session without scanning the whole journal.
_JOURNAL_LOOKBACK = "-30min"
# Matches airplay_health.SUBPROCESS_TIMEOUT_SEC and the other /state probes:
# the read returns tiny server-side-filtered output (-g) and never legitimately
# needs longer, so a 2 s cap bounds the worst-case cold-window /state stall.
_JOURNAL_TIMEOUT_SEC = 2

_NOTIFIED_RE = re.compile(r"Notified latency is (\d+) frames")


@dataclass(frozen=True)
class BondedAirplayLatencyFit:
    """Result of :func:`assess_fit`. All times in seconds."""

    buffer_ms: int
    negotiated_frames: int
    budget_source: str  # "journal" (a Notified-latency line) | "default"
    budget_sec: float  # AP2 latency budget the sender authored
    need_sec: float  # ~150 ms pipeline + buffer_ms the offset must hide
    tight: bool  # budget cannot fit need + shairport's backend buffer
    # When tight, shairport DROPS the whole offset, so the entire need is
    # uncompensated → realized lip-sync lag == need_sec (not the shortfall).
    residual_lag_sec: float

    def to_dict(self) -> dict[str, object]:
        return {
            "buffer_ms": self.buffer_ms,
            "negotiated_frames": self.negotiated_frames,
            "budget_source": self.budget_source,
            "budget_sec": round(self.budget_sec, 6),
            "need_sec": round(self.need_sec, 6),
            "tight": self.tight,
            "residual_lag_sec": round(self.residual_lag_sec, 6),
        }


def assess_fit(buffer_ms: int, notified_frames: int | None) -> BondedAirplayLatencyFit:
    """Does the bonded-leader downstream delay fit the AP2 budget? PURE.

    ``notified_frames`` is the sender-notified latency in frames, or None
    when no ``Notified latency`` line was seen (which, by shairport's
    contract, means the default budget). A non-positive value is treated
    as absent — the same fail-safe direction as
    ``jasper-apply-airplay-mode``'s offset clamp: never assume a smaller
    budget than the default from a garbage reading.

    The tight condition mirrors shairport's own (verified against
    rtp.c ``rtp_ap2_control_receiver``): shairport applies the negative
    backend offset only while ``budget >= |offset| + backend_buffer``; below
    that it logs "stream latency too short to accommodate an offset" and
    DROPS the offset entirely. So this surface flags tight at
    ``budget < need + SHAIRPORT_BACKEND_BUFFER_SEC`` and, because the offset
    is dropped wholesale when that happens, reports the realized lag as the
    FULL ``need`` (the whole pipeline+buffer delay goes uncompensated), not
    the shortfall.
    """
    if notified_frames is None or notified_frames <= 0:
        frames = AP2_DEFAULT_NOTIFIED_FRAMES
        source = "default"
    else:
        frames = notified_frames
        source = "journal"

    budget_sec = (frames + SHAIRPORT_FIXED_ADD_FRAMES) / AIRPLAY_FRAME_RATE_HZ
    need_sec = PIPELINE_FIXED_DELAY_SEC + max(0, buffer_ms) / 1000.0
    tight = (need_sec + SHAIRPORT_BACKEND_BUFFER_SEC) - budget_sec > _FIT_EPSILON_SEC
    return BondedAirplayLatencyFit(
        buffer_ms=buffer_ms,
        negotiated_frames=frames,
        budget_source=source,
        budget_sec=budget_sec,
        need_sec=need_sec,
        tight=tight,
        residual_lag_sec=need_sec if tight else 0.0,
    )


def _default_journal_run(unit: str, lookback: str) -> subprocess.CompletedProcess:
    # `-g` filters server-side so the read stays cheap even over a 30-min
    # window; jasper-control has the systemd-journal supplementary group, so
    # this resolves the shairport (system unit) journal without sudo.
    return subprocess.run(
        [
            "journalctl",
            "-u",
            unit,
            "--since",
            lookback,
            "--no-pager",
            "-o",
            "cat",
            "-g",
            "Notified latency is",
        ],
        capture_output=True,
        text=True,
        timeout=_JOURNAL_TIMEOUT_SEC,
        check=False,
    )


def read_notified_frames(
    *,
    unit: str = "shairport-sync",
    lookback: str = _JOURNAL_LOOKBACK,
    runner: Callable[[str, str], subprocess.CompletedProcess] | None = None,
) -> int | None:
    """Most-recent sender-notified AP2 latency (frames) from shairport's
    journal within ``lookback``, or None when no such line exists.

    None is the documented "default budget" signal, NOT an error sentinel:
    shairport only logs the line for a non-default value, so absence
    genuinely means the default ~2.0 s budget. Thin IO, bounded (one
    filtered journalctl read, no follow/poll), and fail-soft — any error
    (no journalctl, timeout, permission) resolves to None so the caller
    assumes the default (comfortable) budget rather than a false warning.
    """
    run = runner or _default_journal_run
    try:
        proc = run(unit, lookback)
    except (OSError, subprocess.SubprocessError):
        return None
    frames: int | None = None
    for match in _NOTIFIED_RE.finditer(proc.stdout or ""):
        try:
            frames = int(match.group(1))
        except ValueError:
            continue
    return frames


# Short TTL cache over the journal read. The negotiated budget changes only
# per AirPlay session (minutes apart), but jasper-control's /state is polled
# every ~5 s — and by several clients concurrently on its ThreadingHTTPServer.
# Without this, an active bonded leader would spawn a `journalctl` scanning a
# verbose 30-min journal on every poll, during the busiest moment (bonded
# playback) on a 1 GB Pi. The cache bounds that to <=1 read per TTL across all
# callers, and caches the fail-soft None so a wedged journalctl is not hammered.
# Mirrors the _source_availability_cache pattern in control/state_aggregate.py.
# Deliberately NOT keyed on bond identity: the is_active_leader gate in
# bonded_airplay_latency_snapshot suppresses reads entirely while solo/follower,
# so the only staleness window is a sub-TTL unbond→rebond, whose worst case is a
# fail-soft, safe-direction value for <=30 s — not worth a cache-invalidation
# hook on every bond transition.
_NOTIFIED_FRAMES_TTL_SEC = 30.0
_notified_frames_cache: tuple[float, int | None] | None = None
_notified_frames_lock = threading.Lock()


def cached_notified_frames(
    *,
    now: Callable[[], float] = time.monotonic,
    reader: Callable[[], int | None] = read_notified_frames,
) -> int | None:
    """:func:`read_notified_frames` behind a process-wide TTL cache (monotonic
    clock). The lock guards the cache slot only; the reader runs OUTSIDE the
    lock so a slow journalctl cannot serialize concurrent /state requests — so
    concurrent callers may double-read on a cold window (harmless). ``now`` /
    ``reader`` are injectable for tests."""
    global _notified_frames_cache
    with _notified_frames_lock:
        cached = _notified_frames_cache
        if cached is not None and now() - cached[0] < _NOTIFIED_FRAMES_TTL_SEC:
            return cached[1]
    # Read outside the lock: the subprocess must not serialize concurrent
    # /state requests. A rare double-read during a cold window is harmless.
    value = reader()
    with _notified_frames_lock:
        _notified_frames_cache = (now(), value)
    return value


def bonded_airplay_latency_snapshot(
    *,
    config_loader: Callable[[], GroupingConfig] | None = None,
    frames_reader: Callable[[], int | None] | None = None,
) -> dict[str, object] | None:
    """Fail-soft ``/state`` snapshot of the bonded-leader AirPlay latency fit.

    Returns ``{"applicable": False}`` on solo / follower / invalid configs —
    the common case — WITHOUT reading the journal (the bonded-leader gate is
    one tiny env-file parse). Only an active bonded leader triggers the
    journal read (TTL-cached, see :func:`cached_notified_frames`) and returns
    the full fit. Returns None only if the read itself errors, matching the
    per-section nullability of ``/state``. Total: never raises.

    Gated on the SAME :func:`jasper.multiroom.config.is_active_leader` the
    reconciler uses to WRITE the bonded offset (airplay_grouping_env), so the
    surface can never claim "applicable" when the offset is not armed (or
    vice versa).
    """
    from .config import load_config

    load = config_loader or load_config
    try:
        cfg = load()
        if not is_active_leader(cfg):
            return {"applicable": False}
        read = frames_reader or cached_notified_frames
        fit = assess_fit(cfg.buffer_ms, read())
        return {"applicable": True, **fit.to_dict()}
    except Exception:  # noqa: BLE001 — observability must never break /state
        return None
