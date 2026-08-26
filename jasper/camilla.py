# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from .camilla_config_contract import DEFAULT_VOLUME_LIMIT_DB
from .log_event import log_event

if TYPE_CHECKING:
    from camilladsp import CamillaClient

    from .volume_owner import VolumeClaimHandle, VolumeOwner

# `camilladsp` is a Pi-side runtime dep (pycamilladsp wraps the Rust binary's
# websocket API). Lazy-imported in `CamillaController._ensure` — the only
# place it's used at runtime — so this module can be imported on a dev
# machine without camilladsp in the venv. Parallel to the sounddevice /
# openwakeword treatment in audio_io.py and wake.py. The `CamillaClient`
# type annotations on `_client` and `_ensure`'s return are strings thanks
# to `from __future__ import annotations`, so they need nothing at import
# time. (Production code instantiates CamillaController in voice_daemon /
# web setup / control server; tests use fakes.)

logger = logging.getLogger(__name__)

MIN_MAIN_VOLUME_DB = -150.0
MAX_MAIN_VOLUME_DB = DEFAULT_VOLUME_LIMIT_DB

# pycamilladsp's pinned websocket-client transport does not pass a timeout to
# ``create_connection``. websocket-client copies its process-wide default onto
# each new WebSocket's socket, so changing that default around connect is the
# narrowest seam that bounds both the handshake/GetVersion exchange and every
# later command/recv on that connection. The setting is global; serialize the
# temporary override across every CamillaController instance and restore it on
# every exit path.
CAMILLA_OPERATION_TIMEOUT_S = 2.0
CAMILLA_ATTEMPT_BUDGET_S = 5.0
_WEBSOCKET_DEFAULT_TIMEOUT_LOCK = threading.Lock()

# CamillaDSP ramps main volume changes over `volume_ramp_time`, which is
# 400 ms when a config leaves it unset — as every JTS config does, pinned by
# `tests/test_camilla_volume_ramp_default.py`. Hold the duck slightly past
# that so the fade has finished.
MAIN_VOLUME_RAMP_SETTLE_S = 0.45

# How far a graph mutation ducks the main fader before swapping. Deep enough
# that `volume_coordinator.RECONCILE_DUCK_SKIP_DB` (10 dB) reads it as
# somebody's duck and leaves it alone — the 1 Hz reconciler is cross-process
# and does not take the DSP writer lock, so riding that carve-out is what
# keeps it from writing the fader back up mid-swap. Also deeper than the
# largest headroom charge a swap has been measured to carry (22.5 dB), so the
# graph changes under something already inaudible.
GRAPH_SWAP_DUCK_DB = 40.0

_T = TypeVar("_T")

# "What the fader should read right now, ignoring any duck." `VolumeCoordinator`
# owns that fact and is not constructible from here (it needs persistence and
# the renderer backend), so a process that has one registers it. Per process
# rather than per controller: graph swaps run on ad-hoc `primary_controller()`
# instances no coordinator ever sees. Same callable `Ducker` already takes.
CanonicalTargetDbProvider = Callable[[], Awaitable[float]]

_canonical_target_db_provider: CanonicalTargetDbProvider | None = None

# "What level does this duck holder still own?", asked at the moment of
# release. SYNCHRONOUS and non-blocking by contract: it is called inside a
# shielded `finally`, so it must never await, and it must never wait on a lock
# — a wedged holder would otherwise strand a ducked (silent) speaker. `None`
# means "nothing owned any more", and the release falls back to canonical.
HeldTargetDbReader = Callable[[], "float | None"]

#: A held-target reader is a plain state read, so these are defects rather than
#: conditions; they are caught only so a broken reader cannot convert a duck
#: release into a stranded quiet fader.
_HELD_TARGET_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)


def set_canonical_target_db_provider(
    provider: CanonicalTargetDbProvider | None,
) -> None:
    """Register this process's canonical main_volume target."""
    global _canonical_target_db_provider
    _canonical_target_db_provider = provider


async def _duck_release_target_db(
    camilla: "CamillaController",
    *,
    snapshot_db: float,
    duck_depth_db: float,
    held_target_db: HeldTargetDbReader | None = None,
) -> float:
    """Where a duck holder should land the fader when it lets go.

    ``min(reference, current + duck_depth_db)`` — give back this holder's own
    attenuation and nothing else, and never end above the level that should be
    in effect. Both halves are load-bearing, and the two duck holders here
    (`CueDuck` and the graph-swap bracket) can interleave in either order:

    * replaying the entry snapshot strands the fader, because whichever holder
      exits last replays a value the other one had already ducked — and a deep
      drop is exactly what `maybe_reconcile_camilla` leaves alone;
    * a bare relative release fails the other way, clamping to 0 dB — loud —
      when a volume change lands inside the window.

    ``duck_depth_db`` is the positive attenuation this holder applied.

    The reference is the canonical household target, EXCEPT when a caller
    supplies ``held_target_db`` — a reader for the level that caller still owns
    (issue #2925 / #2929). Its answer replaces the canonical reference outright
    rather than joining the ``min``: the household level sits BELOW a
    measurement volume, so including it would win every time and pull the fader
    off the declared level, which is the whole defect this parameter exists to
    remove. A crossover-v2 session holds the speaker at a volume its
    excitation-safety ledger admitted the program against; releasing to the
    household level instead left every routed capture's fader wrong at the
    per-stimulus hold — which, since wave 5, refuses the capture rather than
    writing the level back.

    **It is a reader rather than a number because the answer can change while
    the swap is in flight.** The bracket spans seconds, and a measurement
    session's volume can be drained inside it by a peer task; a pre-resolved
    number would then put back a level whose owner had just given it up. Asked
    HERE, a caller that no longer owns a level answers ``None`` and this falls
    through to the canonical release — the same path every non-measurement swap
    takes. The reader must be synchronous and non-blocking: it runs inside a
    shielded ``finally``, where awaiting could strand a ducked speaker.

    Supplying it never raises the fader above the level its reader names (the
    ``min`` still bounds it by what this holder actually took away), and no
    caller that omits it changes behaviour by a single write.
    """
    current_db = await camilla.get_volume_db(best_effort=True)
    released_db = (
        snapshot_db if current_db is None else current_db + abs(duck_depth_db)
    )
    if held_target_db is not None:
        try:
            held_db = held_target_db()
        except _HELD_TARGET_ERRORS:
            # Named, not blind: the reader is a plain state read, so a raise is
            # a defect rather than a condition. Falling through to canonical
            # keeps a broken reader from turning a release into a stranded duck.
            logger.warning(
                "held volume target unreadable; releasing duck against the "
                "canonical target instead",
                exc_info=True,
            )
            held_db = None
        if held_db is not None:
            return min(float(held_db), released_db)
    provider = _canonical_target_db_provider
    if provider is None:
        return min(snapshot_db, released_db)
    try:
        canonical_db = await provider()
    except (CamillaUnavailable, OSError, RuntimeError, TimeoutError, ValueError):
        logger.warning(
            "canonical volume target unavailable; releasing duck against "
            "the entry snapshot instead",
            exc_info=True,
        )
        return min(snapshot_db, released_db)
    return min(canonical_db, released_db)


@dataclass
class _ThreadAttempt:
    """Cancellation bridge for one synchronous pycamilladsp operation."""

    cancelled: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)


def _coerce_main_volume_db(db: float) -> float:
    """Validate and clamp Camilla's process-wide main fader.

    CamillaDSP itself can accept positive gain unless the loaded YAML
    has `devices.volume_limit` set. This wrapper is the runtime
    defense-in-depth boundary for every Python caller.
    """
    try:
        value = float(db)
    except (TypeError, ValueError) as e:
        raise ValueError(f"main_volume_db must be numeric, got {db!r}") from e
    if not math.isfinite(value):
        raise ValueError(f"main_volume_db must be finite, got {db!r}")
    clamped = max(MIN_MAIN_VOLUME_DB, min(MAX_MAIN_VOLUME_DB, value))
    if clamped != value:
        logger.warning(
            "camilla main_volume clamped: requested %.1f dB -> %.1f dB",
            value, clamped,
        )
    return clamped


def _level_pair(levels: Sequence[float | None] | None) -> tuple[float, float]:
    """Normalize Camilla's channel-meter return shape."""
    if not levels:
        return float("-inf"), float("-inf")
    left = float(levels[0]) if levels[0] is not None else float("-inf")
    right = (
        float(levels[1])
        if len(levels) > 1 and levels[1] is not None
        else left
    )
    return left, right


def _level_list(levels: Sequence[float | None] | None) -> list[float]:
    """List analog of `_level_pair`: normalizes Camilla's full per-channel
    meter return shape without truncating or mirroring down to a stereo
    pair. Every channel Camilla reports is preserved, in order.

    Per-element ``None`` (an individual channel Camilla reports as silent
    or unavailable) normalizes to ``float("-inf")``, exactly like
    `_level_pair`. An empty or missing ``levels`` (no channel data at all)
    returns an empty list — the list analog of `_level_pair`'s
    ``(-inf, -inf)`` sentinel pair.
    """
    if not levels:
        return []
    return [float(v) if v is not None else float("-inf") for v in levels]


class CamillaUnavailable(Exception):
    """CamillaDSP websocket can't be reached after a reconnect attempt.

    Raised by CamillaController._call when both the initial attempt
    and the reconnect retry fail. Public methods accept ``best_effort=
    True`` to convert this into a None return / no-op so callers that
    should keep working through a camilla restart blip (cue playback,
    Ducker, volume coordinator dispatch) don't have to scatter
    try/except CamillaUnavailable boilerplate.
    """


class CamillaConfigRejected(CamillaUnavailable):
    """CamillaDSP was reachable and answered, but refused the config itself.

    A ``CamillaUnavailable`` subclass (not a bare sibling) so every existing
    ``except CamillaUnavailable`` call site keeps working unchanged — this is
    a journal-honesty distinction, not a new control-flow branch (W6 hardware
    run 4 finding J). Before this class existed, ``_call`` folded pycamilladsp's
    ``camilladsp.exceptions.ConfigValidationError`` (raised by a live, healthy
    CamillaDSP daemon that parsed ``SetConfig``'s payload and rejected it —
    e.g. "Use of missing mixer 'split_active_2way'") into the same
    ``CamillaUnavailable`` a dead/unreachable daemon raises, so the journal
    logged ``reason=CamillaUnavailable`` while Camilla was up and answering.
    Both generic failure loggers key off ``reason=type(exc).__name__``
    (``jasper.capture_relay.session._run_with_failure_cues`` and
    ``jasper.web.correction_setup._relay_failure_reason``), so this class name
    alone gets an honest ``reason=CamillaConfigRejected`` in both places — no
    call site needed to change.
    """


def _is_config_validation_error(exc: BaseException) -> bool:
    """True iff ``exc`` is pycamilladsp's ``ConfigValidationError``.

    Lazy, defensive import mirroring ``CamillaController._ensure``'s own
    lazy ``camilladsp`` import: by the time this runs, a call reached
    ``fn(client)`` (or failed inside ``_ensure`` after already importing
    ``camilladsp``), so the module is already loaded in every real failure
    path. The ``ImportError`` guard only protects a dev machine without
    ``camilladsp`` installed at all, where ``exc`` could never legitimately be
    this type anyway.
    """
    try:
        from camilladsp.exceptions import ConfigValidationError
    except ImportError:
        return False
    return isinstance(exc, ConfigValidationError)


class CamillaController:
    """Thin wrapper around pycamilladsp for ducking + volume tools.

    pycamilladsp is sync; we offload calls to a thread so we don't block the
    asyncio loop. Reconnect on failure rather than raising into the daemon.
    """

    def __init__(self, host: str, port: int) -> None:
        from jasper.dsp_apply import CANONICAL_DSP_WRITER_LOCK_PATH

        self._host = host
        self._port = port
        self._client: CamillaClient | None = None
        self._lock = asyncio.Lock()
        # One fixed production lock; tests may replace this instance attribute
        # with a temporary path (there is intentionally no env/config override).
        self._graph_mutation_lock_path = CANONICAL_DSP_WRITER_LOCK_PATH

    def _ensure(
        self,
        cancelled: threading.Event | None = None,
    ) -> CamillaClient:
        if self._client is None:
            from camilladsp import CamillaClient  # lazy, see module top.
            import websocket

            client = CamillaClient(self._host, self._port)
            # Publish before connect: cancellation must be able to reach the
            # WebSocket while connect's GetVersion recv is in flight.
            self._client = client
            try:
                with _WEBSOCKET_DEFAULT_TIMEOUT_LOCK:
                    if cancelled is not None and cancelled.is_set():
                        raise asyncio.CancelledError
                    previous_timeout = websocket.getdefaulttimeout()
                    websocket.setdefaulttimeout(CAMILLA_OPERATION_TIMEOUT_S)
                    try:
                        client.connect()
                    finally:
                        websocket.setdefaulttimeout(previous_timeout)
            except BaseException:  # noqa: BLE001
                if self._client is client:
                    self._client = None
                raise
        return self._client

    def _invoke(
        self,
        fn: Callable[[CamillaClient], _T],
        attempt: _ThreadAttempt,
    ) -> _T:
        """Run one operation wholly in a worker thread.

        In particular, ``_ensure`` belongs here rather than in the event-loop
        argument evaluation for ``asyncio.to_thread``: a cold websocket
        handshake must never block the loop.
        """
        attempt.started.set()
        if attempt.cancelled.is_set():
            raise asyncio.CancelledError
        client = self._ensure(attempt.cancelled)
        if attempt.cancelled.is_set():
            raise asyncio.CancelledError
        return fn(client)

    def _abort_active_websocket(self) -> None:
        """Wake a pinned pycamilladsp worker blocked in send/recv.

        ``disconnect()`` takes pycamilladsp's own query lock, so calling it
        while another thread is blocked in ``recv`` can deadlock. The
        underlying websocket-client ``abort`` method is explicitly designed
        to wake a recv in another thread and does not take that lock.
        """
        client = self._client
        websocket = getattr(client, "_ws", None)
        abort = getattr(websocket, "abort", None)
        if abort is not None:
            try:
                abort()
            except Exception:  # noqa: BLE001
                # The worker may have cleared/replaced _ws between the lookup
                # and abort. Its fixed socket timeout remains the backstop.
                pass

    async def _run_attempt(
        self,
        fn: Callable[[CamillaClient], _T],
    ) -> _T:
        """Run, shield, and cancellation-drain one worker attempt."""
        attempt = _ThreadAttempt()
        worker = asyncio.create_task(asyncio.to_thread(self._invoke, fn, attempt))
        loop = asyncio.get_running_loop()
        budget: asyncio.Future[None] = loop.create_future()

        def expire_budget() -> None:
            if not budget.done():
                budget.set_result(None)

        budget_handle = loop.call_later(
            CAMILLA_ATTEMPT_BUDGET_S,
            expire_budget,
        )

        async def drain_worker() -> None:
            """Drain despite repeated cancellation of this coroutine."""
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    if not worker.done():
                        self._abort_active_websocket()
                except BaseException:  # noqa: BLE001
                    break
            if worker.done() and not worker.cancelled():
                # Retrieve a failed worker's exception to avoid an asyncio
                # "exception was never retrieved" diagnostic.
                try:
                    worker.result()
                except BaseException:  # noqa: BLE001
                    pass

        def stop_worker() -> None:
            attempt.cancelled.set()
            if attempt.started.is_set():
                self._abort_active_websocket()
            else:
                # A to_thread coroutine still queued in asyncio's executor can
                # be cancelled before it owns a thread. This avoids waiting for
                # unrelated executor work merely to run the cancellation check.
                worker.cancel()

        try:
            done, _pending = await asyncio.wait(
                {worker, budget}, return_when=asyncio.FIRST_COMPLETED,
            )
            if worker in done:
                return worker.result()

            # A socket timeout bounds each individual recv. This watchdog
            # initiates abort for a composite operation (cold connect plus
            # multiple commands) at five seconds; the worker is then drained
            # before the controller lock is released.
            task = asyncio.current_task()
            cancellations_before_drain = task.cancelling() if task else 0
            stop_worker()
            await drain_worker()
            self._client = None
            if task and task.cancelling() > cancellations_before_drain:
                raise asyncio.CancelledError
            log_event(
                logger,
                "camilla.operation_timeout",
                level=logging.DEBUG,
                host=self._host,
                port=self._port,
                budget_s=CAMILLA_ATTEMPT_BUDGET_S,
            )
            raise TimeoutError(
                f"CamillaDSP operation exceeded {CAMILLA_ATTEMPT_BUDGET_S:.1f}s"
            )
        except asyncio.CancelledError as cancelled:
            stop_worker()
            log_event(
                logger,
                "camilla.operation_cancelled",
                level=logging.DEBUG,
                host=self._host,
                port=self._port,
            )

            # asyncio cancellation does not stop a running thread. Keep the
            # controller lock until that worker has exited; otherwise a later
            # caller could overlap a mutation with the abandoned one.
            await drain_worker()
            self._client = None
            raise cancelled
        finally:
            budget_handle.cancel()
            budget.cancel()

    async def close(self) -> None:
        """Disconnect the cached client without reconnecting it.

        Ephemeral probes use this for deterministic file-descriptor cleanup.
        Long-running controllers intentionally keep their websocket cached.
        """
        async with self._lock:
            if self._client is None:
                return
            try:
                await self._run_attempt(lambda client: client.disconnect())
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    "camilla.disconnect_failed",
                    level=logging.DEBUG,
                    host=self._host,
                    port=self._port,
                    error=type(exc).__name__,
                )
            finally:
                self._client = None

    async def _call(self, fn: Callable[[CamillaClient], _T]) -> _T:
        async with self._lock:
            try:
                return await self._run_attempt(fn)
            except Exception as e:  # noqa: BLE001
                # First-attempt failure is normal during a transient
                # outage (e.g. camilla restart blip) — we always retry
                # once. DEBUG, not WARNING: the eventual outcome is
                # what callers care about. If the retry succeeds, the
                # call is transparent recovery. If the retry also
                # fails, CamillaUnavailable is raised and best_effort
                # call sites log their own warning at the action level
                # ("set_volume_db skipped", etc). Without this demote,
                # a sustained camilla-down window floods the journal at
                # ~4 Hz from old voice-side polling alone.
                log_event(
                    logger,
                    "camilla.operation_retry",
                    level=logging.DEBUG,
                    host=self._host,
                    port=self._port,
                    error=type(e).__name__,
                )
                self._client = None
                try:
                    return await self._run_attempt(fn)
                except Exception as e2:  # noqa: BLE001
                    self._client = None
                    if _is_config_validation_error(e2):
                        # Camilla answered and rejected the config itself
                        # (e.g. "Use of missing mixer '...'") — a distinct
                        # failure from an unreachable/dead daemon (W6
                        # hardware run 4 finding J). See CamillaConfigRejected.
                        raise CamillaConfigRejected(str(e2)) from e2
                    raise CamillaUnavailable(str(e2)) from e2

    async def get_volume_db(
        self, *, best_effort: bool = False,
    ) -> float | None:
        try:
            return float(await self._call(lambda c: c.volume.main_volume()))
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug("camilla unavailable; get_volume_db → None: %s", e)
                return None
            raise

    async def get_volume_and_mute(
        self, *, best_effort: bool = False,
    ) -> tuple[float, bool] | None:
        """Single round-trip read of main_volume + main_mute.

        Used by VolumeCoordinator to reconcile the content/music carrier:
        the dB value alone is not converged at 0% unless Camilla's final
        mute flag is asserted too.
        """
        def read(c):
            return float(c.volume.main_volume()), bool(c.volume.main_mute())
        try:
            return await self._call(read)
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_volume_and_mute → None: %s", e,
                )
                return None
            raise

    async def get_playback_rms(
        self, *, best_effort: bool = False,
    ) -> tuple[float, float] | None:
        """Per-channel RMS of CamillaDSP's playback signal in dBFS — the
        level just before the DAC, AFTER every attenuation stage on the
        music chain (source track loudness, AirPlay sender volume,
        Spotify Connect sender volume, Camilla main_volume,
        room correction filters, etc). This is what the TTS gain
        tracker uses to size TTS to the actual perceived music level
        instead of guessing at any single attenuation stage.

        Returns (left_db, right_db). Returns (-inf, -inf) on silence
        — pycamilladsp may report None / very negative numbers when
        the chunk has no signal. Returns None if ``best_effort=True``
        and camilla is unreachable."""
        def read(c):
            return _level_pair(c.levels.playback_rms())
        try:
            return await self._call(read)
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_playback_rms → None: %s", e,
                )
                return None
            raise

    async def get_playback_peak(
        self, *, best_effort: bool = False,
    ) -> tuple[float, float] | None:
        """Per-channel playback peak in dBFS for the last processed chunk."""
        def read(c):
            return _level_pair(c.levels.playback_peak())
        try:
            return await self._call(read)
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_playback_peak -> None: %s", e,
                )
                return None
            raise

    async def get_playback_rms_all(
        self, *, best_effort: bool = False,
    ) -> list[float] | None:
        """Full per-channel playback RMS in dBFS — the list analog of
        `get_playback_rms`, exactly as `get_playback_peak_all` is to
        `get_playback_peak`, with the same no-truncation / no-mirroring
        contract and the same `c.levels.playback_rms()` websocket call.

        `get_playback_rms` stays the stereo-master surface the TTS gain
        tracker reads; this is for readers that must see every driver on an
        active-crossover box (`/state`'s per-driver level readout).
        """
        def read(c):
            return _level_list(c.levels.playback_rms())
        try:
            return await self._call(read)
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_playback_rms_all -> None: %s",
                    e,
                )
                return None
            raise

    async def get_playback_peak_all(
        self, *, best_effort: bool = False,
    ) -> list[float] | None:
        """Full per-channel playback peak in dBFS for the last processed
        chunk.

        `get_playback_peak` is the stereo-master metering surface: it
        truncates/mirrors CamillaDSP's per-channel meter down to a 2-tuple
        via `_level_pair` and remains the surface for main L/R metering.
        This method is additive to it, not a replacement — it serves the
        readers that must see a channel beyond index 0/1: multi-channel
        owner metering (the bass-extension bench R10 live cross-check
        reads this once per owner channel — see `cross_check.py` under
        `jasper/bass_extension/bench/`, whose admissibility rule is a
        per-entry index check, not a topology restriction) and
        `/state`'s per-driver
        playback level readout on an active-crossover box. It returns every channel
        CamillaDSP reports, in channel order, with no truncation and no
        mirroring. It reuses the exact same `c.levels.playback_peak()`
        websocket call `get_playback_peak` uses — no new websocket surface.

        Returns an empty list when Camilla reports no channel data at all
        (the list analog of `_level_pair`'s ``(-inf, -inf)`` sentinel pair
        used by `get_playback_peak`); an individual channel Camilla reports
        as ``None`` normalizes to ``float("-inf")``, same as
        `get_playback_peak`. Returns None if ``best_effort=True`` and
        camilla is unreachable.
        """
        def read(c):
            return _level_list(c.levels.playback_peak())
        try:
            return await self._call(read)
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_playback_peak_all -> None: %s",
                    e,
                )
                return None
            raise

    async def get_clipped_samples(
        self, *, best_effort: bool = False,
    ) -> int | None:
        """Number of clipped samples since the current config was loaded."""
        try:
            return int(await self._call(lambda c: c.status.clipped_samples()))
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_clipped_samples -> None: %s", e,
                )
                return None
            raise

    async def get_runtime_status(
        self, *, best_effort: bool = False,
    ) -> dict[str, Any] | None:
        """Small CamillaDSP health snapshot for measurement evidence.

        Correction bundles use this around sweeps to preserve the DSP
        state that is cheap and useful to know later. Missing fields are
        omitted rather than treated as failures because CamillaDSP
        command availability varies across versions.
        """

        def read(c):
            out: dict[str, Any] = {}
            try:
                out["clipped_samples"] = int(c.status.clipped_samples())
            except OSError:
                # Transport loss must escape so _call reconnects once. Only
                # command/version differences are optional in this snapshot.
                raise
            except Exception:  # noqa: BLE001
                pass
            for key, command, coerce in (
                ("buffer_level", "GetBufferLevel", int),
                ("rate_adjust", "GetRateAdjust", float),
                ("capture_rate", "GetCaptureRate", int),
            ):
                try:
                    value = c.query(command)
                except OSError:
                    raise
                except Exception:  # noqa: BLE001
                    continue
                try:
                    out[key] = coerce(value)
                except (TypeError, ValueError):
                    continue
            return out

        try:
            return await self._call(read)
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_runtime_status -> None: %s", e,
                )
                return None
            raise

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        try:
            target = _coerce_main_volume_db(db)
        except ValueError as e:
            if best_effort:
                logger.warning("camilla main_volume rejected: %s", e)
                return False
            raise
        try:
            await self._call(lambda c: c.volume.set_main_volume(target))
            return True
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning(
                    "camilla unavailable; set_volume_db(%.1f) skipped: %s",
                    target, e,
                )
                return False
            raise

    async def set_main_mute(
        self, muted: bool, *, best_effort: bool = False,
    ) -> bool:
        """Set CamillaDSP's process-wide main mute flag.

        This is separate from `main_volume`: `0%` content/music volume
        uses this flag for a true final-output mute while keeping the
        normal 1-100% listening curve intact.
        """
        target = bool(muted)
        try:
            await self._call(lambda c: c.volume.set_main_mute(target))
            return True
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning(
                    "camilla unavailable; set_main_mute(%s) skipped: %s",
                    target, e,
                )
                return False
            raise

    async def adjust_volume_db(
        self, delta_db: float, *, best_effort: bool = False,
    ) -> float | None:
        current = await self.get_volume_db(best_effort=best_effort)
        if current is None:
            return None
        try:
            target = _coerce_main_volume_db(current + float(delta_db))
        except ValueError as e:
            if best_effort:
                logger.warning("camilla main_volume adjust rejected: %s", e)
                return None
            raise
        if not await self.set_volume_db(target, best_effort=best_effort):
            return None
        return target

    async def get_config_file_path(
        self, *, best_effort: bool = False,
    ) -> str | None:
        """Currently-loaded YAML path, e.g. the branch base config or
        `/var/lib/camilladsp/configs/correction_*.yml` after the
        room-correction wizard applied a profile."""
        try:
            return str(await self._call(lambda c: c.config.file_path()))
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug("camilla unavailable; get_config_file_path → None: %s", e)
                return None
            raise

    @contextlib.asynccontextmanager
    async def _graph_mutation(
        self, source: str, *, held_target_db: HeldTargetDbReader | None = None,
    ):
        """Admit one CamillaDSP graph mutation, ducked across the swap.

        A mutation can move the graph's own gain by tens of dB while the
        volume setting is unchanged — loudest when a boosted correction is
        removed and the headroom attenuation it carried goes away. Ducking the
        main fader by :data:`GRAPH_SWAP_DUCK_DB` turns that instant step into a
        fade down and a fade back up, because CamillaDSP ramps a volume change
        instead of applying it at once and keeps the fader as process state
        that survives the reload.

        The duck deliberately rides ``main_volume`` rather than ``main_mute``:
        a mute reads to `VolumeCoordinator.maybe_reconcile_camilla` as mute
        drift, which bypasses both of its skip paths, so its 1 Hz tick would
        clear the bracket mid-swap. A drop this deep reads as somebody's duck
        and is left alone. It also keeps the two existing ``main_mute``
        writers — the coordinator and the floor-tone audition — the only ones.

        ``held_target_db`` rides through to :func:`_duck_release_target_db` as
        the release reference; the duck's DEPTH and dwell are untouched by it,
        only where the release lands. ``None`` (every caller but the
        measurement path) is today's canonical-target behaviour exactly.
        """
        from jasper.dsp_apply import camilla_graph_mutation

        async with camilla_graph_mutation(
            source=source,
            lock_path=self._graph_mutation_lock_path,
        ):
            before_db = await self.get_volume_db()
            if (
                before_db is None
                or before_db - GRAPH_SWAP_DUCK_DB <= MIN_MAIN_VOLUME_DB
            ):
                # Unreadable (only a test double returns None from a strict
                # read), or already so quiet the duck would clamp: nothing
                # audible can step, so swap without one.
                yield
                return
            await self.set_volume_db(before_db - GRAPH_SWAP_DUCK_DB)
            try:
                await asyncio.sleep(MAIN_VOLUME_RAMP_SETTLE_S)
                yield
            finally:
                # Shielded because an interrupted release leaves the speaker
                # ducked — quiet, but the reconciler reads that as somebody's
                # duck and will not undo it.
                await asyncio.shield(
                    self._release_graph_swap_duck(
                        before_db, held_target_db=held_target_db,
                    )
                )

    async def _release_graph_swap_duck(
        self, before_db: float, *, held_target_db: HeldTargetDbReader | None = None,
    ) -> None:
        """Let the swap duck go, best-effort, and say so when it does not."""
        target_db = await _duck_release_target_db(
            self, snapshot_db=before_db, duck_depth_db=GRAPH_SWAP_DUCK_DB,
            held_target_db=held_target_db,
        )
        # Best-effort so a release failure cannot mask the mutation's own
        # error; the event is what keeps a stranded quiet fader visible.
        if not await self.set_volume_db(target_db, best_effort=True):
            log_event(
                logger,
                "camilla.graph_swap_duck_restore_failed",
                target_db=f"{target_db:.1f}",
                level=logging.WARNING,
            )

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False,
    ) -> bool:
        """Tell CamillaDSP to load the YAML at `path` and reload the
        pipeline.

        The two-step `set_file_path` + `reload` is what camillagui-
        backend does and what every CamillaDSP downstream uses for
        config swap. Bundling them here keeps the call site simple
        and ensures the order is correct (path before reload).
        """
        def write_and_reload(c):
            c.config.set_file_path(path)
            c.general.reload()
            return True

        try:
            async with self._graph_mutation("camilla.set_config_file_path"):
                return bool(await self._call(write_and_reload))
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning(
                    "camilla unavailable; set_config_file_path(%s) skipped: %s",
                    path, e,
                )
                return False
            raise

    async def set_active_config_raw(
        self, config: str, *, best_effort: bool = False,
        held_target_db: HeldTargetDbReader | None = None,
    ) -> bool:
        """Upload and apply a complete YAML config without changing the
        persisted config file path.

        This is intentionally separate from ``set_config_file_path``:
        live audition surfaces can change the running preference-EQ
        draft without writing files or changing the durable rollback
        anchor. Saved/apply flows should keep using the file-path
        loader so validation, state recording, and rollback stay
        boring and inspectable.

        ``held_target_db`` is the swap duck's release reference for callers
        that own a declared level across the swap — today only the crossover-v2
        measurement path, which passes the volume its session plan currently
        owns (see :func:`_duck_release_target_db`). Omitted everywhere else,
        and omitting it is byte-for-byte today's behaviour.
        """
        if not isinstance(config, str) or not config.strip():
            if best_effort:
                logger.warning("camilla active config rejected: empty config")
                return False
            raise ValueError("config must be a non-empty YAML string")

        try:
            async with self._graph_mutation(
                "camilla.set_active_config_raw", held_target_db=held_target_db,
            ):
                await self._call(lambda c: c.config.set_active_raw(config))
                return True
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning(
                    "camilla unavailable; set_active_config_raw skipped: %s",
                    e,
                )
                return False
            raise

    async def get_active_config_raw(
        self, *, best_effort: bool = False,
    ) -> str | None:
        """Return the RUNNING CamillaDSP graph as a raw YAML string.

        The read-back counterpart to :meth:`set_active_config_raw`: it reports
        the config CamillaDSP is actually running right now (CamillaDSP's own
        re-serialization of the active graph), not the persisted file path. Use
        this — not :meth:`get_config_file_path` — to verify a live audition that
        was applied with ``set_active_config_raw``, because that loader
        deliberately leaves the persisted ``config_file_path`` unchanged, so the
        path would still report the durable anchor rather than what is running.
        """
        try:
            raw = await self._call(lambda c: c.config.active_raw())
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_active_config_raw → None: %s", e,
                )
                return None
            raise
        return str(raw) if raw is not None else None

    async def normalize_config_raw(
        self, config: str, *, best_effort: bool = False,
    ) -> str | None:
        """Return ``config`` as CamillaDSP itself canonicalizes it.

        ``ReadConfig`` parses, validates, and default-fills WITHOUT applying, so
        a live load compares THIS against :meth:`get_active_config_raw` rather
        than the caller's own text (the readback is a normalized superset).

        **Must NOT take ``camilla_graph_mutation``.** Its neighbours
        :meth:`set_active_config_raw` and :meth:`patch_config` both do, through
        :meth:`_graph_mutation`, so "make it match its siblings" is the tempting
        wrong edit — but this call mutates nothing, and the live-graph boundary
        (``runtime_contract.classify_active_bass_extension_graph``) invokes it
        from *inside* that lock on live paths — among them
        ``commissioning_apply._apply_measured_candidate_owned`` (the candidate
        apply) and ``multiroom.follower_config``'s
        ``apply_prebuilt_follower_config`` / ``restore_active_camilla_solo``.
        Those are examples, not an exhaustive set.

        What taking the lock would actually cost, stated precisely because an
        earlier version of this note overstated it as a hang: the writer lock is
        re-entrant per *task*, and the boundary reaches this method through an
        ``asyncio.gather`` child — a NEW task — so re-entry does not apply and
        it contends on the underlying flock instead. That is bounded by
        ``dsp_apply.DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S`` (10 s), after which
        ``DspWriterLockTimeout`` is raised and logged at WARNING. So the real
        damage is a ~10 s stall mid-commissioning ending in a spurious
        canonicalization failure — loud and bounded, not a deadlock. Still
        worth forbidding: it turns a working live re-proof into a timeout on a
        box that is fine. Pinned by ``tests/test_camilla_controller.py``.
        """
        import yaml

        if not isinstance(config, str) or not config.strip():
            raise ValueError("config must be a non-empty YAML string")
        try:
            parsed = await self._call(lambda c: c.config.parse_yaml(config))
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug("camilla unavailable; normalize_config_raw: %s", e)
                return None
            raise
        return parsed if isinstance(parsed, str) else yaml.safe_dump(parsed)

    async def patch_config(
        self, patch: dict[str, Any], *, best_effort: bool = False,
    ) -> bool:
        """Apply a CamillaDSP partial-config patch to the active config.

        CamillaDSP 4.1 exposes ``PatchConfig`` for focused updates such
        as changing a filter gain/frequency. pyCamillaDSP does not wrap
        that command as a first-class helper in the pinned version, but
        its client exposes the underlying ``query`` call. Keeping that
        escape hatch here prevents raw websocket command names from
        spreading through product code.
        """
        if not isinstance(patch, dict) or not patch:
            if best_effort:
                logger.warning("camilla config patch rejected: empty patch")
                return False
            raise ValueError("patch must be a non-empty mapping")

        try:
            async with self._graph_mutation("camilla.patch_config"):
                await self._call(lambda c: c.query("PatchConfig", arg=patch))
                return True
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning("camilla unavailable; patch_config skipped: %s", e)
                return False
            raise

    async def reload(self, *, best_effort: bool = False) -> bool:
        """Reload the currently-set config file path. Used by the
        room-correction wizard's 'Reset to flat' action when the path
        is already pointed at the branch's flat base config — saves a
        redundant set_file_path call."""
        try:
            async with self._graph_mutation("camilla.reload"):
                await self._call(lambda c: c.general.reload())
                return True
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning("camilla unavailable; reload skipped: %s", e)
                return False
            raise


def primary_controller() -> CamillaController:
    """Return the always-on primary CamillaDSP controller (camilla#1).

    Read only the two controller-specific environment values rather than
    constructing :class:`jasper.config.Config`, whose unrelated provider
    validation must not block low-level DSP recovery and setup paths.
    """
    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


def crossover_controller() -> CamillaController:
    """A :class:`CamillaController` bound to camilla#2 — the endpoint-crossover
    CamillaDSP instance (``:1235``) on an active leader.

    This is :func:`primary_controller`'s camilla#2 analogue: it reads the
    matching ``JASPER_CAMILLA2_HOST`` / ``JASPER_CAMILLA2_PORT`` values
    (defaults ``127.0.0.1`` / ``1235``) without constructing ``Config``.

    Constructed in production by the live pair-balance-trim path
    (:func:`jasper.multiroom.runtime_balance._active_endpoint_camilla`, when
    ``cfg.role == "leader"``), reached from ``apply_local_trim`` /
    ``apply_live_grouping_trim`` in ``jasper/control/server.py``.
    ``jasper-camilla-crossover.service`` itself is not boot-enabled — the
    multiroom reconciler arms/tears it down per-reconcile
    (``_systemctl_crossover_unit`` in ``jasper/multiroom/reconcile.py``) as an
    active-speaker leader gains or loses that role."""
    host = os.environ.get("JASPER_CAMILLA2_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA2_PORT", "1235"))
    return CamillaController(host, port)


class CueDuck:
    """Transient-duck claim for brief cue playback.

    Async context manager — `__aenter__` takes the claim, `__aexit__` gives it
    back. The owner lands the release at ``min(reference, current + depth)``:
    this duck's own attenuation back and nothing else, never above the level in
    effect.

    It used to replay a pre-duck snapshot instead, on the reasoning that a cue
    is short and passive. That holds while a cue is the only duck, and fails as
    soon as it interleaves with a graph-swap duck: whichever of the two exits
    last replays a value the other had already ducked, and the fader is
    stranded tens of dB quiet somewhere the reconciler's duck carve-out will
    not heal.

    Best-effort: if the attenuation cannot be established (camilla restarting)
    the claim is refused, the cue plays unducked, and exit has nothing to undo.
    """

    def __init__(self, owner: "VolumeOwner", duck_db: float) -> None:
        self._owner = owner
        self._duck_db = duck_db
        self._claim: "VolumeClaimHandle | None" = None

    async def __aenter__(self) -> "CueDuck":
        from .volume_owner import VolumeClaimRefused

        try:
            self._claim = await self._owner.acquire_duck(self._duck_db)
        except VolumeClaimRefused:
            # Don't pretend to duck. Exit is a no-op: nothing was taken.
            self._claim = None
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        claim, self._claim = self._claim, None
        if claim is None:
            return
        await self._owner.release(claim)


class Ducker:
    """Voice-session ducking as a transient-duck claim on the volume owner.

    `duck()` takes `duck_db` of attenuation off whatever level is in effect;
    `restore()` gives exactly that back.

    Why the restore re-declares the household level first: anything could have
    happened during the ducked window — crucially, the remote / voice tools /
    external slider observers could have changed `listening_level`, and they do
    it from ANOTHER daemon, so this process's copy is stale until it re-reads.
    An additive give-back that ignored that wedged camilla at
    `pre_duck_value + delta`. Real symptom: remote twist during a voice turn →
    restore overshoots by the duck delta → camilla pinned out-of-range positive
    → sustained clipping when the next source connects. Handing the fresh level
    to `release` makes the outcome independent of any interleaved write, and
    keeps it to ONE fader move rather than a dip and a recovery.
    """

    def __init__(
        self,
        owner: "VolumeOwner",
        duck_db: float,
        target_db_provider: Callable[[], Awaitable[float]],
    ) -> None:
        self._owner = owner
        self._duck_db = duck_db
        self._target_db_provider = target_db_provider
        self._claim: "VolumeClaimHandle | None" = None

    @property
    def is_ducked(self) -> bool:
        """True iff this Ducker holds a claim on the main fader. Read by
        WakeLoop.session_status() so jasper-control can authoritatively
        gate its own camilla writes during a voice session — see
        docs/HANDOFF-volume.md "Cross-daemon Camilla ownership signal"."""
        return self._claim is not None

    @property
    def locks_camilla_volume(self) -> bool:
        """This transport temporarily owns Camilla's main volume."""
        return True

    async def duck(self) -> None:
        if self._claim is not None:
            return
        # Best-effort: if camilla is restarting (Restart=always brings it
        # back in ~2s), skip the attenuation rather than raise into the
        # voice loop. Music isn't playing through camilla anyway when
        # camilla is down, so there's nothing to duck. The claim is REFUSED
        # rather than held when the write was skipped — that way restore()
        # short-circuits cleanly and the next duck() retries when camilla is
        # back.
        from .volume_owner import VolumeClaimRefused

        try:
            self._claim = await self._owner.acquire_duck(self._duck_db)
        except VolumeClaimRefused:
            return
        landed = self._owner.target_db()
        log_event(
            logger,
            "duck",
            on="true",
            new_db="" if landed is None else f"{landed:.1f}",
            duck_db=f"{self._duck_db:.1f}",
        )

    async def restore(self) -> None:
        claim = self._claim
        if claim is None:
            return
        target_db: float | None = None
        try:
            target_db = await self._target_db_provider()
        finally:
            # Release even when the provider raised. Today's code cleared the
            # latch in a finally but never gave the attenuation back, so a
            # broken provider left the speaker quiet with nothing tracking it.
            # ``None`` leaves the standing level as it was and still hands this
            # duck's own depth back.
            self._claim = None
            await self._owner.release(claim, household_level_db=target_db)
        log_event(
            logger,
            "duck",
            on="false",
            target_db=f"{target_db:.1f}",
        )
