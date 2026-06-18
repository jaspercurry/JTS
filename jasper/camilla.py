from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .camilla_config_contract import DEFAULT_VOLUME_LIMIT_DB
from .log_event import log_event

if TYPE_CHECKING:
    from camilladsp import CamillaClient

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


class CamillaUnavailable(Exception):
    """CamillaDSP websocket can't be reached after a reconnect attempt.

    Raised by CamillaController._call when both the initial attempt
    and the reconnect retry fail. Public methods accept ``best_effort=
    True`` to convert this into a None return / no-op so callers that
    should keep working through a camilla restart blip (cue playback,
    Ducker, volume coordinator dispatch) don't have to scatter
    try/except CamillaUnavailable boilerplate.
    """


class CamillaController:
    """Thin wrapper around pycamilladsp for ducking + volume tools.

    pycamilladsp is sync; we offload calls to a thread so we don't block the
    asyncio loop. Reconnect on failure rather than raising into the daemon.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: CamillaClient | None = None
        self._lock = asyncio.Lock()

    def _ensure(self) -> CamillaClient:
        from camilladsp import CamillaClient  # lazy, see module top.

        if self._client is None:
            client = CamillaClient(self._host, self._port)
            client.connect()
            self._client = client
        return self._client

    async def _call(self, fn):
        async with self._lock:
            try:
                return await asyncio.to_thread(fn, self._ensure())
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
                logger.debug(
                    "camilla first attempt failed; retrying: %s", e,
                )
                self._client = None
                try:
                    return await asyncio.to_thread(fn, self._ensure())
                except Exception as e2:  # noqa: BLE001
                    self._client = None
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
            except Exception:  # noqa: BLE001
                pass
            for key, command, coerce in (
                ("buffer_level", "GetBufferLevel", int),
                ("rate_adjust", "GetRateAdjust", float),
                ("capture_rate", "GetCaptureRate", int),
            ):
                try:
                    value = c.query(command)
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

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False,
    ) -> bool:
        """Tell CamillaDSP to load the YAML at `path` and reload the
        pipeline. Atomic on the CamillaDSP side — no audio dropout
        across the swap (same property the Ducker relies on for
        seamless main_volume changes mid-stream).

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
    ) -> bool:
        """Upload and apply a complete YAML config without changing the
        persisted config file path.

        This is intentionally separate from ``set_config_file_path``:
        live audition surfaces can change the running preference-EQ
        draft without writing files or changing the durable rollback
        anchor. Saved/apply flows should keep using the file-path
        loader so validation, state recording, and rollback stay
        boring and inspectable.
        """
        if not isinstance(config, str) or not config.strip():
            if best_effort:
                logger.warning("camilla active config rejected: empty config")
                return False
            raise ValueError("config must be a non-empty YAML string")
        try:
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
            await self._call(lambda c: c.general.reload())
            return True
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning("camilla unavailable; reload skipped: %s", e)
                return False
            raise


class CueDuck:
    """Snapshot-based duck for brief cue playback.

    Async context manager — `__aenter__` snapshots pre-duck camilla
    main_volume and drops by `duck_db` (additive); `__aexit__`
    writes the snapshot back. Distinct from `Ducker` (which restores
    to the live coordinator-canonical target so dial twists during a
    long voice turn win): cues are short and passive, the user
    isn't actively adjusting volume mid-cue, so simple snapshot
    semantics is more predictable than reading a target that may
    have shifted in the duck window from a 1 Hz source-state poll
    or other interleaved writer.

    Best-effort across the chain: if camilla is unreachable when we
    snapshot, we skip ducking entirely (nothing to restore to). If
    the duck write itself is dropped (camilla restarting), we still
    write the snapshot on exit — harmless in the common case where
    that's what camilla already shows.
    """

    def __init__(self, camilla: "CamillaController", duck_db: float) -> None:
        self._camilla = camilla
        self._duck_db = duck_db
        self._pre_db: float | None = None

    async def __aenter__(self) -> "CueDuck":
        self._pre_db = await self._camilla.get_volume_db(best_effort=True)
        if self._pre_db is None:
            # Camilla unreachable — don't pretend to duck. Exit will
            # also be a no-op since we have no snapshot to restore.
            return self
        await self._camilla.adjust_volume_db(
            self._duck_db, best_effort=True,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._pre_db is None:
            return
        await self._camilla.set_volume_db(
            self._pre_db, best_effort=True,
        )


class Ducker:
    """Voice-session ducking around CamillaDSP main_volume.

    `duck()` lowers camilla by `duck_db` (additive). `restore()` reads the
    coordinator's canonical target dB and writes it absolutely.

    Why asymmetric: at duck time nothing else is competing for camilla
    (the voice session is just opening); additive is fine. At restore
    time, anything could have happened during the ducked window —
    crucially, the dial / voice tools / external slider observers could
    have changed `listening_level`. The previous implementation used
    additive restore (`+= -duck_db`), which wedged camilla at
    `pre_duck_value + delta` if any other writer touched it during the
    duck. Real symptom: dial twist during a voice turn → restore
    overshoots by the duck delta → camilla pinned out-of-range positive
    → sustained clipping when the next source connects. Reading the
    canonical target on restore makes the behavior independent of any
    interleaved writes.
    """

    def __init__(
        self,
        camilla: CamillaController,
        duck_db: float,
        target_db_provider: Callable[[], Awaitable[float]],
    ) -> None:
        self._camilla = camilla
        self._duck_db = duck_db
        self._target_db_provider = target_db_provider
        self._ducked = False

    @property
    def is_ducked(self) -> bool:
        """True iff camilla's main_volume is currently held below the
        canonical listening_level target by this Ducker. Read by
        WakeLoop.session_status() so jasper-control can authoritatively
        gate its own camilla writes during a voice session — see
        docs/HANDOFF-volume.md "Cross-daemon defer signal"."""
        return self._ducked

    async def duck(self) -> None:
        if self._ducked:
            return
        # Best-effort: if camilla is restarting (Restart=always brings it
        # back in ~2s), skip the attenuation rather than raise into the
        # voice loop. Music isn't playing through camilla anyway when
        # camilla is down, so there's nothing to duck. Don't latch
        # _ducked when the write was skipped — that way restore() short-
        # circuits cleanly and the next duck() retries when camilla is
        # back.
        result = await self._camilla.adjust_volume_db(
            self._duck_db, best_effort=True,
        )
        if result is None:
            return
        self._ducked = True
        log_event(
            logger,
            "duck",
            on="true",
            new_db=f"{result:.1f}",
            duck_db=f"{self._duck_db:.1f}",
        )

    async def restore(self) -> None:
        if not self._ducked:
            return
        try:
            target_db = await self._target_db_provider()
            await self._camilla.set_volume_db(target_db, best_effort=True)
            log_event(
                logger,
                "duck",
                on="false",
                target_db=f"{target_db:.1f}",
            )
        finally:
            self._ducked = False
