# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-window coordinator.

`measurement_window()` is an async context manager that pauses
everything that would interfere with a clean room measurement, then
restores on exit. Used by room correction and strict audible diagnostics:

    async with measurement_window():
        await play_sweep(...)
        # ... iPhone uploads capture, deconv, design, write YAML ...
        await camilla.set_config_path(out_path)

What gets paused (and why):

  - Every music lane at fan-in's existing diagnostic gate. ``jasper-mux`` is
    the sole owner of that gate: ``TEST_SELECT correction`` excludes AirPlay,
    Spotify, Bluetooth, and USB while continuing to admit the measurement
    lane. This avoids a second writer for USB's policy mute.
  - voice_daemon's WakeLoop + outputd content meter via the
    `MEASURE_PAUSE` UDS command (see jasper/voice_daemon.py). The
    WakeLoop drops mic frames during the window — no wake events
    fire, no Ducker calls happen, no TTS plays. The outputd content
    meter is paused so the sweep does not become the next assistant
    loudness baseline. PAUSE also holds its reply while assistant audio
    that was ALREADY in playout when it landed drains, so a cue or timer
    that started a moment earlier cannot bleed into the first capture
    (#1898); that drain is bounded daemon-side and must stay under
    VOICE_MEASURE_PAUSE_TIMEOUT_SEC below.
  - jasper-control's source-observed volume writes, via the hold at
    ``jasper.control.measurement_hold``. A host moving its USB slider
    mid-sweep would otherwise walk the very fader the measurement holds.
    Authoritative volume writes (a human at the management UI, a remote,
    voice "louder") stay allowed — this is isolation, not a lockout.

This window is the ONE writer of "a measurement is live"; each of those
three enforcement points keeps a self-expiring copy (voice 120 s, mux 60 s,
control 120 s) that lapses on its own if this process dies.

What does NOT get paused:

  - jasper-camilla itself. The sweep MUST go through CamillaDSP so
    the measurement reflects the same DSP path music takes. Any
    correction we generate then acts on the same chain we measured.
  - jasper-mux (the renderer arbiter). It remains alive and reasserts the
    diagnostic gate while the window is open.
  - jasper-aec-bridge (if enabled). Its reference is jasper-outputd's
    UDP speaker monitor — U4/P7-1 retired the ALSA dsnoop tap — which
    sits downstream of CamillaDSP, so the sweep going through the chain
    temporarily drives the AEC reference. The bridge re-converges in
    ~200 ms after the sweep ends; disabling+re-enabling the bridge
    would take longer.

Robustness:
  - Music daemons keep running and fan-in keeps draining their private lanes;
    only mux's selected-input gate changes. A web crash therefore cannot leave
    enabled household sources manually stopped.
  - Gate, voice, and volume-hold restoration all run in ``finally``, and each
    has an independent crash-recovery lease.
  - The voice-daemon RESUME has a server-side auto-clear safety timer
    (voice_daemon.MEASUREMENT_AUTOCLEAR_SEC). A healthy long-running window
    renews that lease every MEASUREMENT_LEASE_REFRESH_SEC; a coordinator crash
    (kill -9) stops renewal and still recovers automatically. The volume hold
    renews on the same cadence against its own
    measurement_hold.MEASUREMENT_HOLD_TTL_SEC and recovers the same way.
  - A precondition check refuses to start if a voice session is
    currently active — yanking an in-flight session is worse than
    asking the user to wait or end it first.
  - The volume hold is also the CROSS-PROCESS mutex: ``_window_active`` below
    is one process's module global, so only jasper-control can tell a CLI that
    a web session already owns the speaker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from ..control.uds import _mux_socket_command
from ..log_event import log_event

logger = logging.getLogger(__name__)


DEFAULT_VOICE_SOCKET_PATH = "/run/jasper/voice.sock"
# Refresh the voice-side crash-recovery timer
# (voice_daemon.MEASUREMENT_AUTOCLEAR_SEC) while a valid measurement window
# remains open. A relay setup may wait up to eight minutes for a human;
# renewal preserves that legitimate window without weakening crash recovery.
# Must stay under the daemon's auto-clear with room for a retry; a test pins
# the pair. Since the volume hold joined the window this is ALSO its renewal
# cadence, and a second test pins it against MEASUREMENT_HOLD_TTL_SEC — the two
# TTLs happen to share a value today, but they are separate contracts owned by
# separate daemons.
MEASUREMENT_LEASE_REFRESH_SEC = 60.0
# Back-off before re-trying a failed lease acquire/renewal. One policy,
# deliberately shared by ALL THREE leases an open window holds — the voice
# pause above, the mux gate below, and jasper-control's volume hold. It is
# budgeted three times: the voice/auto-clear pin, the mux abort-ladder bound
# (see MEASUREMENT_GATE_ABORT_SEC), and the volume-hold TTL pin (see
# MEASUREMENT_HOLD_TTL_SEC) all read it, so retuning it for one lease is
# checked against the other two.
MEASUREMENT_LEASE_RETRY_SEC = 5.0
# How long we wait for the daemon's MEASURE_PAUSE reply. Named because it
# is now a contract, not a local timeout: since #1898 the daemon may hold
# that reply while it drains assistant audio that was already in playout,
# and a coordinator that gives up believes voice was never paused — it
# skips MEASURE_RESUME on the way out and leaves the speaker gated until
# the daemon's auto-clear. The daemon's drain bound
# (voice_daemon.MEASUREMENT_INFLIGHT_DRAIN_SEC) must therefore stay under
# this value; a test pins the pair. Lowering this needs the daemon-side
# bound lowered first, in an earlier release.
VOICE_MEASURE_PAUSE_TIMEOUT_SEC = 3.0
MEASUREMENT_FANIN_LABEL = "correction"
MEASUREMENT_GATE_OWNER = "correction-measurement"
MEASUREMENT_GATE_REFRESH_SEC = 20.0
# Inter-attempt sleep for _release_measurement_gate's confirm loop on the way
# OUT — not the refresh loop above, which backs off on MEASUREMENT_LEASE_RETRY_SEC.
# The adjacency makes that easy to misread; the abort ladder budgets the other one.
MEASUREMENT_GATE_RETRY_SEC = 0.1
# Deadline for one jasper-mux control round trip. _mux_socket_command wraps
# the whole exchange — connect, send, reply, close — so a wedged UDS cannot
# outlive it while mux's lease keeps ageing. Named because the abort ladder
# below budgets it: it is how far past MEASUREMENT_GATE_ABORT_SEC a failing
# acquire can carry the abort. This bounds jasper-mux's socket only; it is
# NOT the voice daemon's VOICE_MEASURE_PAUSE_TIMEOUT_SEC, a different
# contract on a different socket that merely shares a value today.
MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC = 3.0
# Abort the owning window before mux's availability lease
# (mux.FANIN_TEST_LEASE_SEC) can expire and reopen music into a live sweep.
# The refresh loop only CHECKS this deadline after an acquire attempt fails,
# so the abort lands on the first check at or past it — never exactly on it.
# The check before that one was under the deadline by definition, and the step
# that follows costs at most one MEASUREMENT_LEASE_RETRY_SEC back-off plus one
# MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC acquire, so the real worst case is
# bounded by the sum of those three — measured from OUR last_confirmed. mux's
# lease starts earlier than that: it stamps _test_fanin_expires_at before
# replying, so it is already ageing by up to one round trip when we stamp
# last_confirmed. What has to clear mux's lease therefore counts the command
# timeout twice, and that is what the test pins — bare ordering stays green
# while the abort lands after mux has already let music back in.
MEASUREMENT_GATE_ABORT_SEC = 40.0
# jasper-control's volume-observation hold — the window's THIRD lease. It stops
# jasper-control applying source-observed volume writes (a host moving its USB
# slider) into the fader a measurement is holding.
# `_measurement_hold_command` is a module attribute on purpose: tests
# monkeypatch it exactly the way they monkeypatch `_mux_socket_command`.
MEASUREMENT_HOLD_PATH = "/measurement/hold"
MEASUREMENT_HOLD_RELEASE_PATH = "/measurement/release"
# One jasper-control round trip. Loopback HTTP with no pooling, so this is a
# wedged-daemon bound, not a latency budget. Named for the same reason
# MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC is; it happens to share that value.
MEASUREMENT_HOLD_COMMAND_TIMEOUT_SEC = 3.0
# The isolation mode declared to jasper-control. Only `gate` exists today —
# voice keeps running and drops mic frames in-process. See
# jasper.control.measurement_hold.MEASUREMENT_HOLD_MODES.
MEASUREMENT_HOLD_MODE = "gate"

# Mutual-exclusion flag for measurement_window(). Only one window may be open
# at a time: a second concurrent window would let whichever exits FIRST send
# MEASURE_RESUME and release the mux gate while the other is still measuring,
# corrupting its capture. This flag is per-PROCESS and guards only the loop it
# is checked on: each caller runs its windows on ONE event loop (jasper-web's
# background loop; a CLI's own asyncio.run), so a plain check-and-set before
# the first await is atomic there — no asyncio.Lock, which would bind to one
# loop and break the per-test asyncio.run() loops. Cross-process exclusion is
# jasper-control's measurement hold, not this.
_window_active = False


class MeasurementWindowError(RuntimeError):
    """A precondition failed or isolation could not be proven/restored."""


class MeasurementAbortTarget:
    """Redirectable cancel target for the gate-lease abort (held windows).

    The refresh task's default isolation-loss abort cancels the task that
    ENTERED the window. That is right for the per-sweep flows (the entering
    task is the playing task), but a flow that holds one window for a whole
    multi-capture session (the v2 crossover session, W6.1) enters it from
    its long-lived session task while each play runs as its OWN task — the
    default cancel would not stop the actual in-flight sweep. Such a holder
    passes one of these to :func:`measurement_window`:

    * the per-play path ``register()``s the current play task while playing
      and ``clear()``s it after;
    * on a renew failure the refresh task calls :meth:`abort`, which latches
      ``failed`` (the holder's next play must check it and refuse honestly)
      and cancels the registered play task if one is live, else the fallback
      (the entering task — the pre-existing behavior).
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.failed = False

    def register(self, task: asyncio.Task) -> None:
        self._task = task

    def clear(self) -> None:
        self._task = None

    def abort(self, fallback: asyncio.Task | None) -> None:
        self.failed = True
        task = self._task if self._task is not None else fallback
        if task is not None:
            task.cancel()


def _measurement_gate_held(
    payload: object,
    *,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return (
        payload.get("test_source") == MEASUREMENT_FANIN_LABEL
        and payload.get("active_source") == MEASUREMENT_FANIN_LABEL
        and payload.get("test_owner") == gate_owner
    )


async def _acquire_measurement_gate(
    *,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
) -> None:
    """Ask mux to exclude every music lane and verify the landed state."""

    try:
        payload = await _mux_socket_command(
            "TEST_SELECT "
            f"{MEASUREMENT_FANIN_LABEL} {gate_owner}",
            timeout=MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        UnicodeError,
        asyncio.TimeoutError,
    ) as exc:
        raise MeasurementWindowError(
            f"Could not isolate the measurement lane: {exc}"
        ) from exc
    if not _measurement_gate_held(payload, gate_owner=gate_owner):
        raise MeasurementWindowError(
            "Mux did not confirm the isolated measurement lane."
        )
    log_event(
        logger,
        "correction.measurement_gate",
        action="acquire",
        owner=gate_owner,
        result="ok",
    )


async def _release_measurement_gate(
    *,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
    allow_other_owner: bool = False,
) -> None:
    """Release our mux gate, retrying and requiring owner-aware landed state.

    ``allow_other_owner`` is only for cleanup after an indeterminate acquire.
    It permits a concurrently held commissioning gate to remain untouched.
    """

    last_error = "mux did not confirm release"
    for attempt in range(3):
        try:
            payload = await _mux_socket_command(
                f"TEST_RELEASE {gate_owner}",
                timeout=MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC,
            )
            if isinstance(payload, Mapping) and (
                payload.get("test_source") is None
                and payload.get("test_owner") is None
            ):
                log_event(
                    logger,
                    "correction.measurement_gate",
                    action="release",
                    owner=gate_owner,
                    result="ok",
                )
                return
            last_error = "mux still reports a selected test lane"
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            UnicodeError,
            asyncio.TimeoutError,
        ) as exc:
            last_error = str(exc)
            # A lost RELEASE response may still have landed. STATUS also lets
            # indeterminate-acquire cleanup distinguish another feature's
            # owner without ever releasing that owner's gate.
            try:
                status = await _mux_socket_command(
                    "STATUS", timeout=MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC,
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                UnicodeError,
                asyncio.TimeoutError,
            ):
                status = None
            if isinstance(status, Mapping):
                owner = status.get("test_owner")
                if owner is None and status.get("test_source") is None:
                    return
                if allow_other_owner and owner != gate_owner:
                    return
        if attempt < 2:
            await asyncio.sleep(MEASUREMENT_GATE_RETRY_SEC)
    log_event(
        logger,
        "correction.measurement_gate",
        action="release",
        owner=gate_owner,
        result="failed",
        reason=last_error,
        level=logging.ERROR,
    )
    raise MeasurementWindowError(
        f"Could not release the isolated measurement lane: {last_error}"
    )


async def _measurement_hold_command(path: str, body: dict) -> tuple[int, dict]:
    """One POST to jasper-control's measurement-hold surface.

    Returns ``(status, decoded-body)``. Raises whatever the control client
    raises on a transport failure; the callers below own the policy.

    The control token is read from THIS box's own token file and presented as
    ``X-JTS-Token``. Both hold routes are gated (see
    ``server._TOKEN_GATED_ROUTES``), and the gate's job is to keep other LAN
    devices out — a process that can already read ``/var/lib/jasper/control_token``
    is inside the boundary the gate draws. ``current_token()`` returns ``""``
    when the gate is off or the file is unreadable; the header is then omitted
    and the request is either accepted (gate off) or refused 403 (gate on,
    unreadable file), which the fail-soft policy below downgrades to a warning.
    """
    from ..control import control_token
    from ..control.client import AsyncControlClient

    headers: dict[str, str] = {}
    token = control_token.current_token()
    if token:
        headers["X-JTS-Token"] = token
    client = AsyncControlClient(timeout=MEASUREMENT_HOLD_COMMAND_TIMEOUT_SEC)
    response = await client.post(path, body, headers=headers or None)
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        payload = None
    return response.status, payload if isinstance(payload, dict) else {}


async def _acquire_measurement_hold(owner: str) -> bool:
    """Take (or renew) jasper-control's volume-observation hold.

    Returns True when the hold is ours, False when it could not be taken.

    **Policy — fail-soft on unreachable, fail-CLOSED on 409.** The two existing
    leases already sit at opposite ends of that axis: the mux gate raises when
    it cannot be proven (music might still be in the mix), while the voice
    pause logs and proceeds under its default permissive policy. This lease
    takes the permissive end for reachability and the strict end for conflict,
    and the split is not a compromise — it follows from what each failure means:

    * **Unreachable / any non-409 refusal → warn and proceed.** The only writer
      this lease holds back is ``POST /volume/set`` with a ``source``, which is
      served BY jasper-control. At any instant when the lease is un-takeable
      because the daemon is down, that daemon is also not applying
      observations, so the hazard cannot occur *then*. Failing closed here
      would make ``jasper-seat-level`` refuse to run on a box whose control
      daemon is down — a nanny, and a refusal that buys nothing.

      That instant-by-instant argument does NOT cover the window as a whole:
      jasper-control comes back (a deploy restarts it, and it is not
      socket-activated), and a window that gave up after one attempt would then
      be running un-held against a live daemon. Which is why the caller's
      refresh task is started **unconditionally** and keeps retrying this
      function until it lands — see ``_refresh_measurement_hold``. Returning
      False here means "not yet", never "give up".
    * **409 → raise.** That is a DIFFERENT measurement already holding the
      speaker. This is the cross-process mutex ``_window_active`` cannot be
      (it is one process's module global), so a conflict must stop us.
    """
    try:
        status, payload = await _measurement_hold_command(
            MEASUREMENT_HOLD_PATH,
            {"owner": owner, "mode": MEASUREMENT_HOLD_MODE},
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        UnicodeError,
        asyncio.TimeoutError,
    ) as exc:
        # The same tuple _acquire_measurement_gate catches. RuntimeError covers
        # control.client's ControlError without importing it here.
        logger.warning(
            "jasper-control measurement hold unavailable (%s) — proceeding "
            "without it; host-slider volume observations are not held off",
            exc,
        )
        return False
    if status == 409:
        raise MeasurementWindowError(
            payload.get("error")
            or "a measurement is already in progress on this speaker"
        )
    if status < 200 or status >= 300:
        logger.warning(
            "jasper-control refused the measurement hold (HTTP %s: %s) — "
            "proceeding without it; host-slider volume observations are not "
            "held off",
            status,
            payload.get("error", ""),
        )
        return False
    return True


async def _release_measurement_hold(owner: str) -> None:
    """Release jasper-control's hold. Never raises.

    A failed release is recoverable without an operator: the hold self-expires
    after ``measurement_hold.MEASUREMENT_HOLD_TTL_SEC``. Same reasoning — and
    the same ERROR-log-and-move-on shape — as the ``MEASURE_RESUME`` failure
    path below, and deliberately unlike the mux gate, whose failed release
    leaves household music silent and therefore must be surfaced.
    """
    try:
        status, payload = await _measurement_hold_command(
            MEASUREMENT_HOLD_RELEASE_PATH, {"owner": owner},
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        UnicodeError,
        asyncio.TimeoutError,
    ) as exc:
        logger.error(
            "measurement hold release failed (%s) — jasper-control's %.0fs "
            "TTL will recover it",
            exc,
            _measurement_hold_ttl_sec(),
        )
        return
    if status < 200 or status >= 300:
        logger.error(
            "measurement hold release refused (HTTP %s: %s) — "
            "jasper-control's %.0fs TTL will recover it",
            status,
            payload.get("error", ""),
            _measurement_hold_ttl_sec(),
        )


async def _stop_lease_refresh(
    task: "asyncio.Task[None] | None", label: str,
) -> None:
    """Cancel one lease-renewal task and absorb whatever it died of.

    Renewal is resilience-only: a dead background task must never bypass
    MEASURE_RESUME, the mux-gate release, or the volume-hold release, so its
    failure is logged and swallowed rather than propagated out of ``finally``.
    One helper for all three leases — three literal copies of this drifted the
    moment a third lease arrived.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("%s refresh task failed", label)


def _measurement_hold_ttl_sec() -> float:
    """The registrar's TTL, read from its owner so the log cannot drift."""
    from ..control.measurement_hold import MEASUREMENT_HOLD_TTL_SEC

    return MEASUREMENT_HOLD_TTL_SEC


async def _voice_uds_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict:
    """Send one ASCII line to voice_daemon's control socket and
    parse the JSON response. Same wire format as
    jasper.control.server._voice_socket_command (which we don't
    import to avoid a circular dependency)."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        # asyncio.timeout(), NOT asyncio.wait_for(): on CPython <= 3.11
        # wait_for SWALLOWS a CancelledError that arrives in the same tick
        # its awaited future completes (Lib/asyncio/tasks.py: `except
        # CancelledError: if fut.done(): return fut.result()`). This call is
        # on the body path of _refresh_voice_lease's cancellation-only
        # `while True:` (below), which measurement_window()'s finally
        # cancels and then awaits unboundedly -- so a swallowed cancel here
        # makes that task immortal and wedges the whole window teardown
        # (#1952, same class as #1935's Mux.run() patrol wait). Do not
        # "simplify" this back to wait_for while 3.11 is supported.
        async with asyncio.timeout(timeout):
            line = await reader.readline()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError(f"voice_daemon returned no response for {cmd!r}")
    return json.loads(line.decode("utf-8"))


async def _check_no_active_voice_session(
    socket_path: str,
    *,
    require_voice_pause: bool = False,
) -> None:
    """Refuse to open a measurement window while a voice session is
    in progress. Yanking the session would orphan the user's turn
    and look like a hang.

    Returns silently on success. Raises MeasurementWindowError if a
    session is active. Treats UDS-unreachable as "voice daemon down,
    so no session" — the measurement can proceed.
    """
    try:
        status = await _voice_uds_command(socket_path, "STATUS", timeout=2.0)
    except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
        if require_voice_pause:
            raise MeasurementWindowError(
                f"Could not verify voice is idle: {e}"
            ) from e
        # No daemon → no session to interrupt. Log + proceed.
        logger.info(
            "voice daemon not reachable for STATUS check (%s) — "
            "assuming no active session",
            e,
        )
        return
    except (RuntimeError, ValueError, TypeError, UnicodeError) as e:
        if require_voice_pause:
            raise MeasurementWindowError(
                f"Could not verify voice is idle: {e}"
            ) from e
        raise
    if require_voice_pause and (
        not isinstance(status, dict)
        or status.get("state") not in {"WAKE", "SESSION"}
    ):
        raise MeasurementWindowError(
            "Voice STATUS did not provide a trustworthy WAKE/SESSION state."
        )
    if status.get("state") == "SESSION":
        raise MeasurementWindowError(
            "Voice session is currently active. End it (or wait for it "
            "to end) before starting a measurement."
        )


@asynccontextmanager
async def measurement_window(
    *,
    voice_socket_path: str = DEFAULT_VOICE_SOCKET_PATH,
    skip_voice_pause: bool = False,
    skip_music_isolation: bool = False,
    abort_target: MeasurementAbortTarget | None = None,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
    require_voice_pause: bool = False,
) -> AsyncIterator[None]:
    """Isolate fan-in's correction lane + pause voice, yield, restore.

    Args:
      voice_socket_path: voice_daemon's UDS path. Default matches
        what jasper-voice writes to.
      skip_voice_pause: don't send MEASURE_PAUSE/RESUME. For tests
        running without a voice daemon.
      skip_music_isolation: don't acquire mux's diagnostic gate. Tests only.
      abort_target: redirectable cancel target for the isolation-loss abort
        (see :class:`MeasurementAbortTarget`). ``None`` keeps the default —
        cancel the task that entered the window.
      gate_owner: mux diagnostic-gate owner. The correction owner remains the
        default; other callers must use their own owner registered by mux.
      require_voice_pause: fail closed unless voice STATUS and MEASURE_PAUSE
        are both trustworthy. The default remains correction's established
        fail-soft behavior.

    Raises:
      MeasurementWindowError: a precondition failed or mux isolation could not
        be proven/restored.
    """
    # Mutual exclusion (see _window_active). Check-and-set BEFORE the first
    # await so it's atomic on the single background loop. A second concurrent
    # window fails fast rather than queueing — it means a racing
    # /start /verify /next-position, not work to serialize.
    global _window_active
    if require_voice_pause and skip_voice_pause:
        raise MeasurementWindowError(
            "strict voice isolation cannot be combined with skip_voice_pause"
        )
    if _window_active:
        raise MeasurementWindowError(
            "a measurement is already in progress; wait for the current "
            "sweep to finish or reset before starting another"
        )
    _window_active = True

    measurement_gate_cleanup_required = False
    measurement_gate_acquired = False
    measurement_gate_refresh_task: asyncio.Task[None] | None = None
    measurement_gate_lease_error: MeasurementWindowError | None = None
    hold_acquired = False
    hold_refresh_task: asyncio.Task[None] | None = None
    voice_paused = False
    voice_pause_cleanup_required = False
    lease_refresh_task: asyncio.Task[None] | None = None
    voice_lease_error: MeasurementWindowError | None = None
    measurement_owner_task = asyncio.current_task()

    try:
        # Precondition: no active voice session. Inside the try so the
        # window flag is cleared even when this raises (nothing is paused
        # yet, so there is still nothing to restore — contract preserved).
        if not skip_voice_pause:
            await _check_no_active_voice_session(
                voice_socket_path,
                require_voice_pause=require_voice_pause,
            )

        # jasper-control's volume-observation hold — taken BEFORE the mux gate
        # and the voice pause so a conflicting second measurement is refused
        # (409 -> MeasurementWindowError) before this window disturbs anything,
        # and released LAST on the way out so nothing can start while we are
        # still restoring. `gate_owner` is reused as the hold owner on purpose:
        # ONE name identifies this measurement across mux, jasper-control,
        # /state.measurement, and every event= line.
        hold_acquired = await _acquire_measurement_hold(gate_owner)

        async def _refresh_measurement_hold() -> None:
            # Started UNCONDITIONALLY, including when the first acquire did not
            # land, because this loop both renews and RETRIES. jasper-control is
            # restarted by every deploy and is not socket-activated, so a single
            # connection-refused at the top of the window is an ordinary event —
            # and gating the loop on that one attempt would leave a window that
            # can legitimately run for MAX_WALL_CLOCK_CEILING_S with the hold
            # never taken, /state.measurement reporting nothing, and no
            # cross-process mutex, off ONE failed round trip. The fail-soft
            # policy in _acquire_measurement_hold is only honest instant by
            # instant; retrying is what makes it honest for the whole window.
            #
            # A renewal we cannot land retries on the shared back-off rather
            # than aborting the window, matching the voice lease's permissive
            # refresh. It does NOT clear `hold_acquired`: a lost response may
            # still have landed, so cleanup responsibility, once taken, is kept
            # (the same rule `measurement_gate_cleanup_required` follows above).
            # A 409 is the one definitive answer — our lease lapsed and a
            # DIFFERENT owner took it — so renewal stops AND cleanup
            # responsibility is dropped, because releasing from here would
            # un-gate their live capture.
            nonlocal hold_acquired
            delay = (
                MEASUREMENT_LEASE_REFRESH_SEC
                if hold_acquired
                else MEASUREMENT_LEASE_RETRY_SEC
            )
            while True:
                await asyncio.sleep(delay)
                try:
                    landed = await _acquire_measurement_hold(gate_owner)
                except MeasurementWindowError as exc:
                    logger.warning(
                        "measurement hold lost to another owner (%s); "
                        "stopping renewal", exc,
                    )
                    hold_acquired = False
                    return
                if landed:
                    hold_acquired = True
                delay = (
                    MEASUREMENT_LEASE_REFRESH_SEC
                    if landed
                    else MEASUREMENT_LEASE_RETRY_SEC
                )

        hold_refresh_task = asyncio.create_task(_refresh_measurement_hold())

        if not skip_music_isolation:
            # Gate first: even a renderer that races its subsequent stop cannot
            # enter the mix. Mux remains the single writer and reasserts this
            # diagnostic selection once per tick for the whole window.
            # Cleanup responsibility is established BEFORE the command: if the
            # selection lands but its response is lost, finally still releases
            # this exact owner and can never release commissioning's gate.
            measurement_gate_cleanup_required = True
            if gate_owner == MEASUREMENT_GATE_OWNER:
                # Preserve the established no-argument seam for correction
                # callers and their tests.
                await _acquire_measurement_gate()
            else:
                await _acquire_measurement_gate(gate_owner=gate_owner)
            measurement_gate_acquired = True

            async def _refresh_measurement_gate_lease() -> None:
                nonlocal measurement_gate_lease_error
                delay = MEASUREMENT_GATE_REFRESH_SEC
                last_confirmed = time.monotonic()
                while True:
                    await asyncio.sleep(delay)
                    try:
                        if gate_owner == MEASUREMENT_GATE_OWNER:
                            await _acquire_measurement_gate()
                        else:
                            await _acquire_measurement_gate(
                                gate_owner=gate_owner,
                            )
                    except MeasurementWindowError as exc:
                        logger.warning(
                            "measurement gate lease refresh failed: %s",
                            exc,
                        )
                        if (
                            time.monotonic() - last_confirmed
                            >= MEASUREMENT_GATE_ABORT_SEC
                        ):
                            measurement_gate_lease_error = MeasurementWindowError(
                                "Measurement isolation could not be renewed; "
                                "the sweep was stopped before household music "
                                "could re-enter the mix. Check System status "
                                "and try again."
                            )
                            if abort_target is not None:
                                # Held-window holder (v2 session): cancel the
                                # ACTUAL in-flight play task (or latch for the
                                # next play) — cancelling the entering task
                                # would not stop the sweep (W6.1 gate fix).
                                abort_target.abort(measurement_owner_task)
                            elif measurement_owner_task is not None:
                                measurement_owner_task.cancel()
                            return
                        delay = MEASUREMENT_LEASE_RETRY_SEC
                    else:
                        last_confirmed = time.monotonic()
                        delay = MEASUREMENT_GATE_REFRESH_SEC

            measurement_gate_refresh_task = asyncio.create_task(
                _refresh_measurement_gate_lease()
            )

        if not skip_voice_pause:
            # Establish cleanup responsibility before PAUSE. A lost response
            # may still have armed the voice gate.
            voice_pause_cleanup_required = require_voice_pause
            try:
                resp = await _voice_uds_command(
                    voice_socket_path,
                    "MEASURE_PAUSE",
                    timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
                )
                if not isinstance(resp, dict):
                    if require_voice_pause:
                        raise MeasurementWindowError(
                            "MEASURE_PAUSE returned a malformed response."
                        )
                    raise TypeError("MEASURE_PAUSE response is not an object")
                pause_result = resp.get("result")
                if pause_result == "ok":
                    voice_paused = True
                    voice_pause_cleanup_required = True
                    drained = resp.get("drained")
                    if require_voice_pause and drained is not True:
                        raise MeasurementWindowError(
                            "Voice pause was armed, but the daemon did not "
                            "prove prior assistant audio drained."
                        )

                    async def _refresh_voice_lease() -> None:
                        nonlocal voice_lease_error

                        def _abort_strict_window() -> None:
                            nonlocal voice_lease_error
                            voice_lease_error = MeasurementWindowError(
                                "Voice isolation could not be renewed; "
                                "the measurement was stopped."
                            )
                            if abort_target is not None:
                                abort_target.abort(measurement_owner_task)
                            elif measurement_owner_task is not None:
                                measurement_owner_task.cancel()

                        delay = MEASUREMENT_LEASE_REFRESH_SEC
                        while True:
                            await asyncio.sleep(delay)
                            try:
                                renewal = await _voice_uds_command(
                                    voice_socket_path,
                                    "MEASURE_PAUSE",
                                    timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
                                )
                            except (
                                FileNotFoundError,
                                OSError,
                                asyncio.TimeoutError,
                                RuntimeError,
                                ValueError,
                                TypeError,
                                UnicodeError,
                            ) as exc:
                                logger.warning(
                                    "measurement lease refresh failed: %s",
                                    exc,
                                )
                                if require_voice_pause:
                                    _abort_strict_window()
                                    return
                                delay = MEASUREMENT_LEASE_RETRY_SEC
                                continue
                            renewal_ok = (
                                isinstance(renewal, dict)
                                and renewal.get("result") == "ok"
                                and (
                                    not require_voice_pause
                                    or renewal.get("drained") is True
                                )
                            )
                            if not renewal_ok:
                                logger.warning(
                                    "measurement lease refresh returned non-ok: %s",
                                    renewal,
                                )
                                if require_voice_pause:
                                    _abort_strict_window()
                                    return
                                delay = MEASUREMENT_LEASE_RETRY_SEC
                            else:
                                delay = MEASUREMENT_LEASE_REFRESH_SEC

                    lease_refresh_task = asyncio.create_task(
                        _refresh_voice_lease()
                    )
                    if drained is False:
                        logger.warning(
                            "MEASURE_PAUSE timed out draining prior assistant "
                            "audio — proceeding under the historical permissive "
                            "correction policy; MEASURE_RESUME remains required"
                        )
                else:
                    if require_voice_pause:
                        raise MeasurementWindowError(
                            "Voice daemon refused MEASURE_PAUSE: "
                            f"{resp.get('result', 'missing result')}."
                        )
                    logger.warning(
                        "MEASURE_PAUSE returned non-ok: %s — proceeding "
                        "anyway, but the WakeLoop may still consume mic "
                        "during the sweep",
                        resp,
                    )
            except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                if require_voice_pause:
                    raise MeasurementWindowError(
                        f"Could not pause voice for measurement: {e}"
                    ) from e
                logger.warning(
                    "voice_daemon MEASURE_PAUSE failed (%s) — proceeding "
                    "without WakeLoop pause. The voice loop will probably "
                    "still work fine if the daemon is simply down.",
                    e,
                )
            except (RuntimeError, ValueError, TypeError, UnicodeError) as e:
                if require_voice_pause:
                    raise MeasurementWindowError(
                        f"Could not pause voice for measurement: {e}"
                    ) from e
                raise

        logger.info(
            "measurement window OPEN (voice_paused=%s, volume_hold=%s)",
            voice_paused,
            hold_acquired,
        )
        yield
    finally:
        # Release the mutex in an INNER finally — after the restore I/O, but
        # guaranteed even if it raises. Timing matters on the single
        # background loop: clearing the flag BEFORE these awaits would let a
        # queued second window run during gate/voice restoration (the
        # corruption the mutex exists to prevent). Clearing it AFTER but only
        # on the success
        # path would re-strand the flag True forever if a restore step raised
        # (e.g. systemctl missing). The inner finally gives both: serialized
        # against the restore, and never leaked.
        try:
            await _stop_lease_refresh(
                measurement_gate_refresh_task, "measurement gate",
            )
            await _stop_lease_refresh(
                hold_refresh_task, "measurement hold",
            )
            await _stop_lease_refresh(
                lease_refresh_task, "measurement lease",
            )
            # Restore voice first, then release mux's music-isolation gate.
            if voice_pause_cleanup_required:
                try:
                    # A plain read deadline, NOT
                    # VOICE_MEASURE_PAUSE_TIMEOUT_SEC: RESUME has no
                    # in-playout drain to wait out, and giving up here is
                    # recoverable (the daemon's auto-clear un-gates voice)
                    # rather than leaving the speaker gated. Equal values
                    # today, different contracts.
                    await _voice_uds_command(
                        voice_socket_path, "MEASURE_RESUME", timeout=3.0,
                    )
                except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                    logger.error(
                        "voice_daemon MEASURE_RESUME failed: %s — the "
                        "daemon's auto-clear safety timer will recover",
                        e,
                    )
                except (RuntimeError, ValueError, TypeError, UnicodeError) as e:
                    if not require_voice_pause:
                        raise
                    logger.error(
                        "voice_daemon MEASURE_RESUME returned malformed data: %s; "
                        "the daemon's auto-clear safety timer will recover",
                        e,
                    )
            gate_release_error: MeasurementWindowError | None = None
            if measurement_gate_cleanup_required:
                try:
                    if gate_owner == MEASUREMENT_GATE_OWNER:
                        await _release_measurement_gate(
                            allow_other_owner=not measurement_gate_acquired,
                        )
                    else:
                        await _release_measurement_gate(
                            gate_owner=gate_owner,
                            allow_other_owner=not measurement_gate_acquired,
                        )
                except MeasurementWindowError as exc:
                    # If release truly did not land, the still-held mux gate
                    # keeps music silent; surface the action required.
                    gate_release_error = exc
            # Hold last: LIFO against the acquire order above, so no second
            # measurement can take the speaker while this one is still
            # restoring voice and the mux gate.
            if hold_acquired:
                await _release_measurement_hold(gate_owner)
            logger.info("measurement window CLOSED")
            if measurement_gate_lease_error is not None:
                raise measurement_gate_lease_error
            if voice_lease_error is not None:
                raise voice_lease_error
            if gate_release_error is not None:
                raise gate_release_error
        finally:
            _window_active = False


class HeldWindow:
    """A ``measurement_window`` held open for a caller that is not on its loop.

    The handshake — capture the loop, publish a thread-safe releaser, signal a
    ``threading.Event`` once the window is up, then park on an ``asyncio.Event``
    until someone releases it — is the one ``jasper/web``'s sync and balance
    flows each keep privately in their own ``_session_window``. Owning it here
    means one copy of the ordering rules that are easy to get wrong: a release
    that arrives before the loop exists is honoured rather than lost, and
    ``entered`` is set in a ``finally`` so a caller blocked on it is never
    stranded by a window that failed to open.

    Which phase ``error`` belongs to is read off the flags: ``held`` False means
    the window never opened; ``lost`` set means it ended while held, before
    anyone released it; otherwise the release itself failed.
    """

    def __init__(self, **window_kwargs: Any) -> None:
        self._window_kwargs = window_kwargs
        #: Unblocks a waiting caller: the window is up, or holding it failed.
        self.entered = threading.Event()
        #: True once the window actually opened.
        self.held = False
        #: The window ended before anyone released it — its isolation is gone
        #: and whatever the caller is doing has to stop.
        self.lost = threading.Event()
        #: The one failure this hold produced, if any.
        self.error: BaseException | None = None
        self._releasing = threading.Event()
        self._release: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def release(self) -> None:
        """Ask the hold to exit. Thread-safe, and safe before ``hold`` runs."""

        # Ordered before the wake-up on purpose: `hold` re-checks this flag
        # once it owns the asyncio.Event, so a release arriving before that
        # assignment is honoured there instead of being dropped.
        self._releasing.set()
        if self._loop is None or self._release is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._release.set)
        except RuntimeError:
            pass  # the loop is already gone: the hold ended on its own

    async def hold(self) -> None:
        """Enter the window and park until released. Run this ON its loop."""

        try:
            self._loop = asyncio.get_running_loop()
            self._release = asyncio.Event()
            if self._releasing.is_set():
                self._release.set()
            async with measurement_window(**self._window_kwargs):
                self.held = True
                self.entered.set()
                await self._release.wait()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            if self.held and not self._releasing.is_set():
                self.lost.set()
        finally:
            self.entered.set()
