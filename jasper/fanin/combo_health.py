# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure policy for the USB-combo runtime-fallback watcher (defect 2026-07-10).

WHY THIS EXISTS — the P3 USB path (fan-in DIRECT-captures ``hw:UAC2Gadget`` as
the sole USB ingress; ``jasper-usbsink.service`` is only a process-free readiness
marker) is only (re)resolved on a CONFIG change: boot, deploy, or a ``/sources/``
toggle (all three run ``jasper-fanin-coupling-reconcile
--auto``). Nothing re-resolves it on a LIVE capture failure. So if fan-in's direct
capture of the gadget breaks at runtime (the gadget rebuilt underneath a live
stream — the flowing→dead zombie handle), USB audio goes silent with no fallback
and nothing surfaces it.

This module is the PURE decision half of the watcher (pattern 3: the reconciler
owns the env I/O + daemon transitions; this owns the tick accounting + marker
lifecycle + the arm/disarm decision). The impure orchestrator — read fan-in
STATUS, act on the decision — is ``jasper.fanin.coupling_reconcile.run_health_check``
(the ``--health`` verb), fired every ~3 min by ``jasper-fanin-combo-health.timer``
(mirrors ``jasper-wifi-recover``: a systemd timer, no resident daemon).

THE SIGNAL. fan-in exports a per-direct-lane ``health`` in STATUS
(``rust/jasper-fanin/src/mixer.rs`` ``direct_health``): ``"capturing"`` /
``"idle"`` / ``"broken"``. ``"broken"`` is the flowing→dead zombie signature ONLY
— an idle or unplugged host reads ``"idle"`` and can NEVER trip the fallback (the
binding constraint). Because ``"broken"`` is instantaneous (the self-heal reopen
resets the zombie streak the moment it trips, so a ~3-min poll rarely lands on
it), the DURABLE cross-tick signal the watcher acts on is the cumulative
``reopens`` / ``card_gen_reopens`` self-heal counters CLIMBING between ticks — BUT
only while the lane is simultaneously ``health=="capturing"``. That gate is
load-bearing (defect 2026-07-11): the counters climb on a purely IDLE box too. A
Mac left connected as the default output streams digital silence, and the UAC2
gadget routinely re-enumerates (host sleep/wake, USB autosuspend, a ``/sources/``
toggle) — each rebuild a NORMAL fan-in self-heal that bumps ``card_gen_reopens``
(function rebuilt, no frames flowed) or ``reopens`` (silence had flowed, then went
deaf) while ``health`` reads ``idle`` the whole time. Counting those raw climbs as
brokenness disarmed an idle jts.local TWICE in one day with zero user action. A
REAL break of an actively-playing stream re-establishes capture within
milliseconds of each self-heal reopen, so the ~3-min poll reads ``capturing`` —
gating the counter delta on ``capturing`` preserves the real detection while
rejecting the idle churn. (The prior "idle can't move those counters" claim was
wrong: a silence-streaming Mac makes frames flow, so the zombie latch DOES fire on
idle.) A physical unplug/replug moves ``opens``/``retries``, NOT
``reopens``/``card_gen_reopens``, so the signal stays physical-unplug-immune too.

THE ACTION. On brokenness SUSTAINED across ``FALLBACK_CONSECUTIVE_TICKS`` (>= 2,
~6 min), the reconciler — the single writer — DISARMS the combo exactly the way it
arms it (the same combo env writes + daemon restarts). Since the aloop solo path
was deleted (2026-07-10) there is NO capture to fall back to, so this leaves USB
audio UNAVAILABLE until recovery, and writes a fallback MARKER (timestamp +
reason) the doctor + ``/state`` surface LOUDLY.

FLAP-PROOF. While the marker exists, the periodic ``--health`` pass never re-arms
(it is disarm-only). The marker — and combo re-arm — is cleared only by an
``--auto`` pass, which runs on exactly the three specified clear-events (boot,
deploy, ``/sources/`` toggle) and clears-and-retries once per event. So combo
never oscillates on its own within a boot. (See ``run_health_check`` /
``reconcile_auto`` for where the marker is read/cleared.)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

# The fan-in input lane whose direct{} health we watch, and the STATUS source
# token that marks it as the USB DIRECT (combo) lane. Reused from the status
# helper so the label / source token never drift from the /state reader.
from .status import FANIN_INPUT_SOURCE_DIRECT as DIRECT_LANE_SOURCE
from .status import USBSINK_INPUT_LABEL

logger = logging.getLogger(__name__)

# STATUS ``direct.health`` tokens — kept in lock-step with the Rust producer
# (rust/jasper-fanin/src/mixer.rs ``direct_health_str``). Pinned by
# tests/test_fanin_combo_health.py.
DIRECT_HEALTH_CAPTURING = "capturing"
DIRECT_HEALTH_IDLE = "idle"
DIRECT_HEALTH_BROKEN = "broken"

# Consecutive broken ticks (each ~3 min) before the watcher disarms the combo.
# >= 2 so a single transient never trips the fallback; ~6 min of sustained
# brokenness is a real runtime capture failure, not a hiccup.
FALLBACK_CONSECUTIVE_TICKS = 2

# The flap-proof marker: present == "combo was disarmed by the runtime fallback;
# the periodic --health pass must not re-arm." Cleared/retried only by an --auto
# pass (boot/deploy/toggle). Lives beside the other reconciler-owned state in
# /var/lib/jasper (mode 0644 — no secret, read by /state + doctor).
FALLBACK_MARKER_PATH = "/var/lib/jasper/usb_combo_fallback.json"
# Persisted tick state: the consecutive-broken counter + the previous sample's
# counters (so the next tick can compute the reopen-churn delta). Same dir/mode.
TICK_STATE_PATH = "/var/lib/jasper/combo_health_tick.json"

@dataclass(frozen=True)
class DirectHealthSample:
    """One reading of the fan-in direct-capture lane's health.

    ``health`` is fan-in's instantaneous classification; ``reopens`` /
    ``card_gen_reopens`` are the cumulative self-heal counters whose cross-tick
    DELTA is the durable brokenness signal; ``present`` / ``frames_read`` are
    logged context. Extracted from fan-in STATUS by :func:`extract_direct_sample`.
    """

    present: bool
    health: str
    reopens: int
    card_gen_reopens: int
    frames_read: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "health": self.health,
            "reopens": self.reopens,
            "card_gen_reopens": self.card_gen_reopens,
            "frames_read": self.frames_read,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DirectHealthSample | None":
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                present=bool(raw.get("present", False)),
                health=str(raw.get("health", "")),
                reopens=int(raw.get("reopens", 0)),
                card_gen_reopens=int(raw.get("card_gen_reopens", 0)),
                frames_read=int(raw.get("frames_read", 0)),
            )
        except (TypeError, ValueError):
            return None


def extract_direct_sample(
    fanin_status: dict[str, Any] | None,
) -> DirectHealthSample | None:
    """The direct-lane health sample from fan-in STATUS, or ``None`` when the box
    is NOT running the combo (no ``source:"direct"`` usbsink lane).

    ``None`` is the "nothing to watch" signal: a box with the combo off (USB Audio
    Input disabled / no gadget) or one the fallback already disarmed. The watcher
    no-ops silently on ``None``.
    Fail-soft: a missing/malformed STATUS, absent ``inputs``, or no direct usbsink
    lane all return ``None``.
    """
    if not isinstance(fanin_status, dict):
        return None
    inputs = fanin_status.get("inputs")
    if not isinstance(inputs, list):
        return None
    for entry in inputs:
        if (
            not isinstance(entry, dict)
            or entry.get("label") != USBSINK_INPUT_LABEL
            or entry.get("source") != DIRECT_LANE_SOURCE
        ):
            continue
        direct = entry.get("direct")
        if not isinstance(direct, dict):
            return None
        frames_read = entry.get("frames_read")
        return DirectHealthSample(
            present=bool(direct.get("present", False)),
            health=str(direct.get("health", "")),
            reopens=_as_int(direct.get("reopens")),
            card_gen_reopens=_as_int(direct.get("card_gen_reopens")),
            frames_read=_as_int(frames_read),
        )
    return None


def _as_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def sample_is_broken(
    cur: DirectHealthSample, prev: DirectHealthSample | None
) -> bool:
    """True when THIS tick shows a runtime capture break of an ACTIVE stream.

    Two independent signatures, either sufficient:

    1. ``health == "broken"`` — fan-in's own instantaneous flowing→dead
       classification (rare to catch at a ~3-min poll, but free to honor). It is
       already the zombie signature — frames flowed, then the handle went deaf —
       so it needs no further gating.
    2. The self-heal reopen counters CLIMBED since the previous tick
       (``reopens`` or ``card_gen_reopens`` up) **AND the lane is actively
       CAPTURING at this tick** (``health == "capturing"``). The counter climb is
       the durable proxy for a break the instantaneous ``health`` usually misses
       (a self-heal reopen resets the zombie streak within one period, so a poll
       rarely lands on ``"broken"``); the ``capturing`` gate is what keeps the
       proxy honest.

    Why the ``capturing`` gate (defect 2026-07-11). The binding invariant, drilled
    on hardware, is that an idle or unplugged host must NEVER trip the fallback —
    ``Broken`` requires flowing→dead. But the reopen counters ALSO climb on a
    purely idle box: a Mac left connected as the default output streams digital
    silence, and the UAC2 gadget routinely re-enumerates (host sleep/wake, USB
    autosuspend, a ``/sources/`` toggle). Each rebuild is a NORMAL fan-in self-heal
    that bumps ``card_gen_reopens`` (function rebuilt, no frames flowed) or
    ``reopens`` (silence had flowed, then went deaf) — with ``health`` reading
    ``"idle"`` the whole time (the lane is not actively capturing). The pre-fix
    code counted ANY counter climb as brokenness regardless of ``health`` and
    disarmed an idle jts.local twice in one day (2026-07-11) with zero user action.
    A REAL break of an actively-playing stream re-establishes capture within
    milliseconds of each self-heal reopen, so the ~3-min poll reads ``"capturing"``
    — the gate keeps that detection while rejecting the idle churn.

    A fan-in RESTART resets the cumulative counters to 0, making ``cur < prev``
    (so the delta path reads NOT broken even while capturing) — a restart never
    false-trips the fallback; the next tick re-establishes the baseline.
    """
    if cur.health == DIRECT_HEALTH_BROKEN:
        return True
    # Durable reopen-churn signal — trusted ONLY while the lane is actively
    # capturing (see the invariant above). An idle/absent lane whose reopen
    # counters climbed is routine re-enumeration self-heal (silence-streaming Mac,
    # host sleep/wake, USB autosuspend, a /sources/ toggle), not a capture break.
    if cur.health != DIRECT_HEALTH_CAPTURING:
        return False
    if prev is not None and (
        cur.reopens > prev.reopens or cur.card_gen_reopens > prev.card_gen_reopens
    ):
        return True
    return False


@dataclass(frozen=True)
class TickState:
    """Persisted watcher state between ticks: the consecutive-broken run and the
    previous sample (for the next tick's reopen-churn delta)."""

    consecutive_broken: int
    sample: DirectHealthSample | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutive_broken": self.consecutive_broken,
            "sample": self.sample.to_dict() if self.sample is not None else None,
        }

    @classmethod
    def empty(cls) -> "TickState":
        return cls(consecutive_broken=0, sample=None)


@dataclass(frozen=True)
class HealthTickDecision:
    """The pure outcome of one watcher tick.

    ``disarm`` is True only when brokenness has been sustained across
    ``FALLBACK_CONSECUTIVE_TICKS``. ``transition`` names the log-worthy edge (empty
    string = a steady healthy tick, which the watcher logs NOTHING for —
    journal-quiet). ``next_state`` is the tick state to persist.
    """

    broken: bool
    disarm: bool
    transition: str
    next_state: TickState


def decide_health_tick(
    cur: DirectHealthSample,
    prev: TickState,
    *,
    threshold: int = FALLBACK_CONSECUTIVE_TICKS,
) -> HealthTickDecision:
    """Advance the consecutive-broken accounting one tick (pure).

    ``prev`` is the persisted state from the last tick (``TickState.empty()`` on
    first run). Returns the disarm decision + the log-worthy transition + the
    ``next_state`` to persist. A steady healthy tick yields ``transition=""`` so
    the watcher stays journal-quiet.
    """
    broken = sample_is_broken(cur, prev.sample)
    consecutive = (prev.consecutive_broken + 1) if broken else 0
    disarm = broken and consecutive >= threshold
    if disarm:
        transition = "sustained_broken"
    elif broken and prev.consecutive_broken == 0:
        transition = "first_broken"
    elif not broken and prev.consecutive_broken > 0:
        transition = "recovered"
    else:
        transition = ""
    return HealthTickDecision(
        broken=broken,
        disarm=disarm,
        transition=transition,
        next_state=TickState(consecutive_broken=consecutive, sample=cur),
    )


# ---- tick-state persistence (fail-soft I/O) --------------------------------


def read_tick_state(path: str = TICK_STATE_PATH) -> TickState:
    """Read the persisted tick state; fail-soft to empty on any read/parse error
    (a missing or corrupt state file just restarts the accounting — the >=2-tick
    requirement re-establishes durability from scratch)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return TickState.empty()
    if not isinstance(raw, dict):
        return TickState.empty()
    try:
        consecutive = int(raw.get("consecutive_broken", 0))
    except (TypeError, ValueError):
        consecutive = 0
    return TickState(
        consecutive_broken=max(0, consecutive),
        sample=DirectHealthSample.from_dict(raw.get("sample")),
    )


def write_tick_state(state: TickState, path: str = TICK_STATE_PATH) -> None:
    """Persist the tick state atomically. Best-effort — a write failure logs at
    DEBUG and is swallowed (the watcher is advisory; a lost tick just restarts the
    accounting next run)."""
    from jasper.atomic_io import atomic_write_text

    try:
        atomic_write_text(path, json.dumps(state.to_dict()), mode=0o644)
    except OSError as e:
        logger.debug("combo-health tick state write failed: %s", e)


# ---- fallback marker lifecycle ---------------------------------------------


@dataclass(frozen=True)
class FallbackMarker:
    """The flap-proof fallback marker: why + when the combo was disarmed."""

    reason: str
    at_epoch: float

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "at_epoch": self.at_epoch}


def read_fallback_marker(path: str = FALLBACK_MARKER_PATH) -> FallbackMarker | None:
    """The fallback marker, or ``None`` when absent/unreadable. A corrupt marker
    reads as absent (fail toward re-arming — the ordinary auto default — rather
    than freezing a box off the combo on one bad byte)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        at_epoch = float(raw.get("at_epoch", 0.0))
    except (TypeError, ValueError):
        at_epoch = 0.0
    return FallbackMarker(reason=str(raw.get("reason", "")), at_epoch=at_epoch)


def fallback_active(path: str = FALLBACK_MARKER_PATH) -> bool:
    """True iff a fallback marker is present (combo disarmed, re-arm blocked until
    the next --auto clear-event)."""
    return read_fallback_marker(path) is not None


def write_fallback_marker(
    reason: str,
    path: str = FALLBACK_MARKER_PATH,
    *,
    now: float | None = None,
) -> bool:
    """Write the fallback marker atomically. Returns True on success. Best-effort:
    a write failure logs at WARNING and returns False (the disarm still proceeds;
    USB audio is left unavailable — the marker is the flap-guard, and its absence
    means the next --auto simply re-attempts, which is the safe direction)."""
    from jasper.atomic_io import atomic_write_text

    marker = FallbackMarker(
        reason=reason, at_epoch=time.time() if now is None else now
    )
    try:
        atomic_write_text(path, json.dumps(marker.to_dict()), mode=0o644)
    except OSError as e:
        logger.warning("combo-health fallback marker write failed: %s", e)
        return False
    return True


def clear_fallback_marker(path: str = FALLBACK_MARKER_PATH) -> bool:
    """Remove the fallback marker if present. Returns True iff a marker existed and
    was removed. Best-effort on unlink errors (returns False)."""
    import os

    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("combo-health fallback marker clear failed: %s", e)
        return False


__all__ = [
    "DIRECT_HEALTH_BROKEN",
    "DIRECT_HEALTH_CAPTURING",
    "DIRECT_HEALTH_IDLE",
    "FALLBACK_CONSECUTIVE_TICKS",
    "FALLBACK_MARKER_PATH",
    "TICK_STATE_PATH",
    "DirectHealthSample",
    "FallbackMarker",
    "HealthTickDecision",
    "TickState",
    "clear_fallback_marker",
    "decide_health_tick",
    "extract_direct_sample",
    "fallback_active",
    "read_fallback_marker",
    "read_tick_state",
    "sample_is_broken",
    "write_fallback_marker",
    "write_tick_state",
]
