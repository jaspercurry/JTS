from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

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
            except Exception as e:
                # First-attempt failure is normal during a transient
                # outage (e.g. camilla restart blip) — we always retry
                # once. DEBUG, not WARNING: the eventual outcome is
                # what callers care about. If the retry succeeds, the
                # call is transparent recovery. If the retry also
                # fails, CamillaUnavailable is raised and best_effort
                # call sites log their own warning at the action level
                # ("set_volume_db skipped", etc). Without this demote,
                # a sustained camilla-down window floods the journal at
                # ~4 Hz from the TtsVolumeTracker poll alone.
                logger.debug(
                    "camilla first attempt failed; retrying: %s", e,
                )
                self._client = None
                try:
                    return await asyncio.to_thread(fn, self._ensure())
                except Exception as e2:
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
        """Single round-trip read of main_volume + main_mute. Used by the
        TTS-gain tracker, which needs to honor mute as well as volume —
        if the user has muted the speaker, TTS shouldn't talk over the
        silence they asked for."""
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
            levels = c.levels.playback_rms()
            l = float(levels[0]) if levels and levels[0] is not None else float("-inf")
            r = float(levels[1]) if len(levels) > 1 and levels[1] is not None else l
            return l, r
        try:
            return await self._call(read)
        except CamillaUnavailable as e:
            if best_effort:
                logger.debug(
                    "camilla unavailable; get_playback_rms → None: %s", e,
                )
                return None
            raise

    async def set_volume_db(
        self, db: float, *, best_effort: bool = False,
    ) -> bool:
        try:
            await self._call(lambda c: c.volume.set_main_volume(float(db)))
            return True
        except CamillaUnavailable as e:
            if best_effort:
                logger.warning(
                    "camilla unavailable; set_volume_db(%.1f) skipped: %s",
                    db, e,
                )
                return False
            raise

    async def adjust_volume_db(
        self, delta_db: float, *, best_effort: bool = False,
    ) -> float | None:
        current = await self.get_volume_db(best_effort=best_effort)
        if current is None:
            return None
        target = current + float(delta_db)
        if not await self.set_volume_db(target, best_effort=best_effort):
            return None
        return target

    async def get_config_file_path(
        self, *, best_effort: bool = False,
    ) -> str | None:
        """Currently-loaded YAML path, e.g. `/etc/camilladsp/v1.yml`
        on a fresh boot or `/var/lib/camilladsp/configs/correction_*.yml`
        after the room-correction wizard applied a profile."""
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

    async def reload(self, *, best_effort: bool = False) -> bool:
        """Reload the currently-set config file path. Used by the
        room-correction wizard's 'Reset to flat' action when the path
        is already pointed at /etc/camilladsp/v1.yml — saves a
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
        logger.info(
            "event=duck on=true new_db=%.1f duck_db=%.1f",
            result, self._duck_db,
        )

    async def restore(self) -> None:
        if not self._ducked:
            return
        try:
            target_db = await self._target_db_provider()
            await self._camilla.set_volume_db(target_db, best_effort=True)
            logger.info("event=duck on=false target_db=%.1f", target_db)
        finally:
            self._ducked = False
