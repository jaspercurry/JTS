# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Observe the host's volume slider via ALSA mixer, route to JTS.

The UAC2 gadget exposes the host's volume slider as ALSA mixer
controls on the Pi side:

  - "PCM Capture Volume"   (integer, range card-defined)
  - "PCM Capture Switch"   (bool — Mac mute toggle)

This module maps the raw value to JTS's 0-100 listening_level by
inverting macOS's observed square-root step transfer (see
`_raw_to_pct`), and POSTs to jasper-control's /volume/set endpoint with
source="usbsink". The endpoint routes through
VolumeCoordinator.observe_source_volume(), which goes through echo
prevention — so a remote twist that triggered an outbound write to the
gadget mixer (we don't actually do this — see
docs/historical/usbsink-implementation-appendix.md §3.2 "Why no outbound
write back to the host") wouldn't bounce back as a phantom user-side change.

Reads are event-driven: `alsaaudio.Mixer.polldescriptors()` hands over
the control FDs and `asyncio.add_reader` wakes the loop, so nothing runs
between host slider moves. Discovery still shells out to `amixer` once —
the simple-mixer API exposes neither the numids nor the DB_MINMAX TLV.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess
import time
from types import ModuleType
from typing import Any, Optional

from jasper.control.client import CONTROL_PORT, AsyncControlClient, ControlError
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


# jasper-control declines an observation while USB is not the active source,
# and while a measurement holds the fader. A declined host slider MOVE is
# re-presented on a capped exponential backoff, so a host that starts playback
# right after moving its slider plays at the stale canonical level for at most
# one ceiling-length window rather than until the next move. A 30 s ceiling was
# rejected: a jts3 measurement (21%->70%) implies a +24.75 dB unattributed jump
# held that much longer. The STARTUP snapshot is deliberately not retried —
# it is not proof of a host action, and re-presenting it cost a measured 708
# declined POSTs/hour on an idle jts3 with nothing to publish.
POST_RETRY_INTERVAL_SEC = 1.0
POST_RETRY_BACKOFF_FACTOR = 2.0
POST_RETRY_CEILING_SEC = 5.0
# Equals jasper.active_speaker.session_volume_plan.MAX_WALL_CLOCK_CEILING_S
# (not imported here — a test pin ties the two): the hard ceiling of any
# guided measurement, so a slider move during one is still re-presented once
# the hold lifts. The OTHER decline producer — jasper.volume_coordinator's
# inactive-source gate — has no ceiling of its own; this cap is what bounds
# it too. Remove when the coordinator answers a decline with a reason code
# the bridge can act on.
POST_RETRY_MAX_SEC = 3600.0

# Mixer control names as the u_audio gadget driver exposes them.
# These are fixed by the kernel module, not by our gadget descriptor —
# `c_fu_vol_name` in the gadget script sets the host-visible label
# but the ALSA mixer name stays "PCM Capture Volume".
VOL_CONTROL_NAME = "PCM Capture Volume"
SWITCH_CONTROL_NAME = "PCM Capture Switch"
# ALSA's simple-mixer layer merges the two controls above into one element.
# `alsaaudio.mixers(cardindex=...)` reports exactly ["PCM"] for this card.
MIXER_ELEMENT_NAME = "PCM"


def default_control_url() -> str:
    """Where jasper-control listens.

    The /volume/set endpoint accepts an optional `source` field; with
    source="usbsink" the coordinator routes through observe_source_volume
    (echo-prevented) rather than set_listening_level (authoritative). See
    jasper.control.server for the handler."""
    return f"http://127.0.0.1:{CONTROL_PORT}"


# `amixer cget` output format for the UAC2 gadget volume control. CRITICAL:
# the kernel `u_audio` driver does NOT expose the control in dB. It exposes a
# 0-based STEP INDEX (u_audio_volume_info: min=0, max=(vmax-vmin+res-1)/res,
# step=1; the value is (volume-vmin)/res). It also attaches a DB_MINMAX TLV
# giving the physical dB endpoints. So for our advertised -50..0 dB / 1 dB-step
# range the control reports:
#
#     numid=1,iface=MIXER,name='PCM Capture Volume'
#       ; type=INTEGER,access=rw---R--,values=1,min=0,max=50,step=1
#       : values=25
#       | dBminmax-min=-50.00dB,max=0.00dB
#
# - Second line: step-index spec (min/max are STEP INDICES, not dB)
# - Third line: current step index
# - Fourth line: the decoded DB_MINMAX TLV (physical dB endpoints)
#
# Switch control output:
#     numid=2,iface=MIXER,name='PCM Capture Switch'
#       ; type=BOOLEAN,access=rw------,values=1
#       : values=on
_NUMID_RE = re.compile(r"numid=(\d+),iface=MIXER,name='([^']+)'")
# Step-index range from the `; type=...,min=..,max=..,step=..` spec line. The
# TLV dB line uses `min=-50.00dB` (decimal + `dB`), which this integer-only,
# comma-terminated pattern deliberately does not match.
_RANGE_RE = re.compile(r"min=(-?\d+),max=(-?\d+)")
# DB_MINMAX TLV: `dBminmax-min=<X>dB,max=<Y>dB` (amixer also prints
# `dBminmaxmute-` for the MUTE variant — same min/max fields). This is the
# kernel's ground-truth physical dB scale; preferred over reconstruction.
_TLV_MINMAX_RE = re.compile(
    r"dBminmax(?:mute)?-min=(-?\d+\.\d+)dB,max=(-?\d+\.\d+)dB"
)
# DB_SCALE fallback (`dBscale-min=<X>dB,step=<Y>dB`) in case a future kernel
# switches TLV types; recovers min + per-step dB.
_TLV_SCALE_RE = re.compile(
    r"dBscale-min=(-?\d+\.\d+)dB,step=(\d+\.\d+)dB"
)


# Single source of truth for the advertised capture-volume dB range. WHY these
# numbers: macOS maps its slider POSITION perceptually onto the host-advertised
# dB range, and the kernel's wide ~-128..0 dB default compressed the whole Mac
# slider into the top few dB (issue #1698: a low-mid slider read ~73%). We
# advertise a narrow -50..0 dB span aligned with jasper.volume_curve's -50 dB
# floor. gadget-up (deploy/usbsink/jasper-usbgadget-up) writes these to configfs
# in 1/256 dB units — c_volume_min/max/res = round(const*256) = -12800/0/256 —
# and tests/test_usbsink_volume_bridge.py pins the two ends to these constants
# so the bash literals and this Python can never drift. The bridge uses these to
# reconstruct physical dB only when the control's DB_MINMAX TLV can't be parsed.
USBSINK_VOLUME_DB_MIN = -50.0
USBSINK_VOLUME_DB_MAX = 0.0
USBSINK_VOLUME_STEP_DB = 1.0
# configfs unit note: gadget-up converts these to the kernel's 1/256-dB
# c_volume_* units as round(dB*256); that derivation + its contract test live
# on the bash side (deploy/usbsink/jasper-usbgadget-up) and in
# tests/test_usbsink_volume_bridge.py — the bridge only reads physical dB back.


def _load_alsaaudio() -> ModuleType:
    """Import the Linux-only mixer binding without breaking non-Linux tooling."""

    import alsaaudio  # type: ignore[import-not-found]

    return alsaaudio


class VolumeBridge:
    """Watches the gadget mixer's control FDs, POSTs changes to jasper-control.

    Lifecycle:
        bridge.run()  # async, blocks until cancelled

    The bridge does NOT cache jasper-control state. Every observed
    mixer change triggers one POST; a declined value (e.g. the
    active-source gate — USB isn't the active source — or a recent
    cross-process write within the persistence echo window; NOT the
    coordinator's own-echo window, which is never stamped for USB) is
    retried with a capped exponential backoff until the controller
    acknowledges it. Accepted values are deduplicated locally, while the
    coordinator owns source and echo policy.
    """

    def __init__(
        self,
        card_name: str = "UAC2Gadget",
        control_url: str | None = None,
        *,
        discovery_retry_interval_sec: float = 5.0,
        http_timeout_sec: float = 2.0,
        alsaaudio_module: ModuleType | None = None,
    ) -> None:
        self._card_name = card_name
        self._control_url = (control_url or default_control_url()).rstrip("/")
        self._discovery_retry_interval = discovery_retry_interval_sec
        self._http_timeout = http_timeout_sec
        self._alsa = alsaaudio_module

        # Cached lookups, populated in _discover().
        self._vol_numid: Optional[int] = None
        self._switch_numid: Optional[int] = None
        # ALSA step-index range for the volume control (min is 0 on real
        # hardware; max is the kernel's step count). These are NOT dB.
        self._vol_min: int = 0
        self._vol_max: int = 0
        # Physical dB endpoints for the two step-index bounds above, recovered
        # from the control's DB_MINMAX TLV (preferred) or reconstructed from the
        # advertised range (fallback). `_db_source` records which, for the log.
        self._db_min: float = USBSINK_VOLUME_DB_MIN
        self._db_max: float = USBSINK_VOLUME_DB_MAX
        self._db_source: str = "reconstructed"

        # Last value we POSTed — dedupes repeated observations.
        # None until jasper-control confirms that the observation was accepted;
        # transport success alone is insufficient because the controller
        # deliberately declines observations from an inactive source.
        self._last_published_pct: Optional[int] = None
        # Last value the mixer reported, and whether the host has moved the
        # slider since startup. Until it has, what we read is state discovery
        # rather than a host action — a restarted bridge must not erase a mute
        # asserted by another surface. The latch stays set once armed, so a
        # move back to the startup value is still intent.
        self._last_observed_pct: Optional[int] = None
        self._host_moved: bool = False
        # Raw step index + mute state behind the most recent _last_observed_pct,
        # carried to _post() purely for the usbsink.volume_observed log fields.
        self._last_raw: Optional[int] = None
        self._last_muted: bool = False
        # Re-presents one declined host slider move; see the retry constants.
        self._retry_task: Optional[asyncio.Task[None]] = None

        # Simple-mixer handle for the card, opened after discovery.
        self._mixer: Any | None = None

        # Bound once `run()` clears mixer discovery (mirrors where the old
        # httpx client was opened). None means discovery has not succeeded
        # yet. The client carries no connection pool, so there's nothing
        # to close on shutdown.
        self._control: Optional[AsyncControlClient] = None

    async def run(self) -> None:
        """Discover the gadget mixer's numids + range, then serve its
        control-FD events forever. Cancellable from the daemon's shutdown
        path. A mixer that breaks under us (the gadget function
        re-enumerated) falls back into discovery rather than exiting."""
        try:
            while True:
                # Defer mixer discovery until run() — at __init__ time the
                # gadget card may not have enumerated yet (init.service has
                # only just returned).
                try:
                    self._discover()
                    self._open_mixer()
                except VolumeBridgeUnavailable as e:
                    log_event(
                        logger,
                        "usbsink.volume_bridge_unavailable",
                        reason=e,
                        retry_sec=self._discovery_retry_interval,
                        level=logging.WARNING,
                    )
                    await asyncio.sleep(self._discovery_retry_interval)
                    continue
                if self._control is None:
                    self._control = AsyncControlClient(
                        self._control_url, timeout=self._http_timeout,
                    )
                log_event(
                    logger,
                    "usbsink.volume_bridge_started",
                    card=self._card_name,
                    vol_numid=self._vol_numid,
                    switch_numid=self._switch_numid,
                    # Step-index range AND the resolved physical dB range, so an
                    # operator can see e.g. `range=0..50 db=-50.0..0.0
                    # db_source=tlv` and tell whether the advertised range
                    # actually stuck.
                    range=f"{self._vol_min}..{self._vol_max}",
                    db=f"{self._db_min:.1f}..{self._db_max:.1f}",
                    db_source=self._db_source,
                )
                if await self._watch_mixer():
                    await asyncio.sleep(self._discovery_retry_interval)
        except asyncio.CancelledError:
            log_event(logger, "usbsink.volume_bridge_stopping")
            raise
        finally:
            self._cancel_retry()
            self._close_mixer()

    # ------------------------------------------------------------------
    # Mixer event loop
    # ------------------------------------------------------------------

    def _open_mixer(self) -> None:
        """Open the card's simple-mixer element, or raise Unavailable."""
        try:
            if self._alsa is None:
                self._alsa = _load_alsaaudio()
            indexes = dict(
                zip(self._alsa.cards(), self._alsa.card_indexes()),
            )
            # `cards()` returns ALSA card IDs (the `amixer -c` argument);
            # `card_name()` returns the driver's pretty name and does NOT
            # match, so it cannot be used to resolve the index.
            index = indexes.get(self._card_name)
            if index is None:
                raise VolumeBridgeUnavailable(
                    f"card {self._card_name!r} not in {sorted(indexes)}",
                )
            self._mixer = self._alsa.Mixer(
                control=MIXER_ELEMENT_NAME, cardindex=index,
            )
        except VolumeBridgeUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise VolumeBridgeUnavailable(f"mixer open failed: {e}") from e

    def _close_mixer(self) -> None:
        mixer, self._mixer = self._mixer, None
        if mixer is not None:
            with contextlib.suppress(Exception):
                mixer.close()

    async def _watch_mixer(self) -> bool:
        """Publish the current value, then one more on every mixer event.

        Returns True (rather than raising) when the control FDs stop working,
        so run() can back off and rediscover the card.
        """
        mixer = self._mixer
        assert mixer is not None
        loop = asyncio.get_running_loop()
        woken = asyncio.Event()
        broken: list[BaseException] = []

        def _on_readable() -> None:
            # handleevents() drains the control FD. Skipping it would leave
            # the descriptor readable and spin the loop.
            try:
                mixer.handleevents()
            except Exception as e:  # noqa: BLE001
                broken.append(e)
            woken.set()

        fds = [int(fd) for fd, _mask in mixer.polldescriptors()]
        for fd in fds:
            loop.add_reader(fd, _on_readable)
        try:
            while True:
                try:
                    await self._observe()
                except Exception as e:  # noqa: BLE001
                    broken.append(e)
                if broken:
                    log_event(
                        logger,
                        "usbsink.volume_mixer_reset",
                        card=self._card_name,
                        error=broken[0],
                        level=logging.WARNING,
                    )
                    return True
                await woken.wait()
                woken.clear()
        finally:
            for fd in fds:
                loop.remove_reader(fd)
            self._close_mixer()

    # ------------------------------------------------------------------
    # Discovery: find numids + range
    # ------------------------------------------------------------------

    def _discover(self) -> None:
        # `amixer -c <card> controls` lists all controls on the card.
        try:
            out = subprocess.run(
                ["amixer", "-c", self._card_name, "controls"],
                capture_output=True, text=True, timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise VolumeBridgeUnavailable(
                f"amixer controls failed: {e}",
            ) from e
        if out.returncode != 0:
            raise VolumeBridgeUnavailable(
                f"amixer -c {self._card_name} controls rc={out.returncode} "
                f"stderr={out.stderr.strip()!r}",
            )
        for m in _NUMID_RE.finditer(out.stdout):
            numid = int(m.group(1))
            name = m.group(2)
            if name == VOL_CONTROL_NAME:
                self._vol_numid = numid
            elif name == SWITCH_CONTROL_NAME:
                self._switch_numid = numid
        if self._vol_numid is None:
            raise VolumeBridgeUnavailable(
                f"{VOL_CONTROL_NAME!r} not exposed by card {self._card_name!r} — "
                f"is the gadget descriptor missing c_volume_present=1?",
            )

        # Parse the volume control's step-index range from a one-shot cget.
        # On real hardware this is `min=0, max=<step-count>` (see the format
        # comment above) — NOT dB.
        cg = self._cget(self._vol_numid)
        m = _RANGE_RE.search(cg)
        if m:
            self._vol_min = int(m.group(1))
            self._vol_max = int(m.group(2))

        # Recover the physical dB endpoints. Prefer the control's own
        # DB_MINMAX TLV (kernel ground truth — reflects whatever range
        # actually stuck); fall back to the advertised range constants when
        # the TLV isn't present/parseable.
        db_scale = self._parse_tlv_db(cg, self._vol_min, self._vol_max)
        if db_scale is not None:
            self._db_min, self._db_max = db_scale
            self._db_source = "tlv"
        else:
            self._db_min = USBSINK_VOLUME_DB_MIN
            self._db_max = USBSINK_VOLUME_DB_MAX
            self._db_source = "reconstructed"

    @staticmethod
    def _parse_tlv_db(
        cget_out: str, idx_min: int, idx_max: int,
    ) -> Optional[tuple[float, float]]:
        """Recover (min_db, max_db) from a decoded dB TLV line, or None.

        The kernel u_audio driver attaches a DB_MINMAX TLV, which amixer
        prints as `dBminmax-min=<X>dB,max=<Y>dB`. A DB_SCALE variant
        (`dBscale-min=<X>dB,step=<Y>dB`) is also handled defensively; its
        max is derived from the step-index span.
        """
        m = _TLV_MINMAX_RE.search(cget_out)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = _TLV_SCALE_RE.search(cget_out)
        if m:
            min_db = float(m.group(1))
            step_db = float(m.group(2))
            return min_db, min_db + max(0, idx_max - idx_min) * step_db
        return None

    # ------------------------------------------------------------------
    # Observation: read both controls, post if changed
    # ------------------------------------------------------------------

    async def _observe(self) -> None:
        """Read the mixer once and publish the value if it is new."""
        mixer = self._mixer
        assert mixer is not None and self._alsa is not None
        # The merged "PCM" simple element also has a playback half whenever
        # the USB mic export is on (p_chmask=1); an unqualified getvolume()
        # then defaults to PCM_PLAYBACK and returns the kernel's playback
        # default (80/100) instead of this capture control's value.
        values = mixer.getvolume(
            units=self._alsa.VOLUME_UNITS_RAW, pcmtype=self._alsa.PCM_CAPTURE,
        )
        if not values:
            return
        raw = int(values[0])
        muted = False
        if self._switch_numid is not None:
            # getrec() reports the capture switch: 0 on a muted channel, and
            # raises on an element that has none. Mute overrides to 0% —
            # better to underrepresent volume than to let a channel through
            # when the user expected silence.
            muted = any(int(v) == 0 for v in mixer.getrec())
        pct = 0 if muted else self._raw_to_pct(raw)
        if pct == self._last_observed_pct:
            return
        if self._last_observed_pct is not None:
            self._host_moved = True
        self._last_observed_pct = pct
        self._last_raw = raw
        self._last_muted = muted
        await self._publish(pct)

    async def _publish(self, pct: int) -> None:
        await self._cancel_retry_and_wait()
        if pct == self._last_published_pct:
            return
        initial = self._last_published_pct is None and not self._host_moved
        outcome = await self._post(pct, initial=initial)
        if outcome is True:
            self._last_published_pct = pct
        elif outcome is None or not initial:
            # Only an explicit decline of the startup snapshot is dropped. No
            # answer at all is the boot race — jasper-control is still coming
            # up (observed on jts3: the first POST after a reboot timed out) —
            # and the snapshot is still the only thing that will sync the host
            # slider until the user next touches it.
            self._retry_task = asyncio.create_task(self._retry_declined(pct))

    async def _retry_declined(self, pct: int) -> None:
        """Re-present one unacknowledged value until the controller takes it,
        or the cap elapses — see POST_RETRY_MAX_SEC."""
        started = time.monotonic()
        delay = POST_RETRY_INTERVAL_SEC
        attempts = 0
        while time.monotonic() - started < POST_RETRY_MAX_SEC:
            await asyncio.sleep(delay)
            attempts += 1
            if await self._post(pct) is True:
                self._last_published_pct = pct
                return
            delay = min(delay * POST_RETRY_BACKOFF_FACTOR, POST_RETRY_CEILING_SEC)
        log_event(
            logger,
            "usbsink.volume_retry_abandoned",
            pct=pct,
            attempts=attempts,
            level=logging.DEBUG,
        )

    async def _cancel_retry_and_wait(self) -> None:
        task, self._retry_task = self._retry_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _cancel_retry(self) -> None:
        task, self._retry_task = self._retry_task, None
        if task is not None:
            task.cancel()

    def _raw_to_pct(self, raw: int) -> int:
        """THE volume curve: raw mixer STEP INDEX -> JTS 0-100 percent.

        The kernel u_audio control reports a 0-based step index, not dB (see
        the amixer-format comment above). On the live Mac/UAC2 path, macOS
        maps its visible slider fraction to approximately the square root of
        that normalized step index. The bridge must invert that transfer:

            visible_fraction = normalized_step_fraction ** 2

        Hardware points pin the contract: Mac 13% -> step 18/50 -> 12.96%;
        Mac 25% -> step 25/50 -> 25%; and Mac 64% -> step 40/50 -> 64%.
        The prior physical-dB-to-amplitude conversion double-applied a
        perceptual correction: step 40 is -10 dB and became 31.6%, producing
        the observed Mac 64% / JTS 31% mismatch.

        This is one END of a two-ended contract. The other end is
        jasper.volume_curve.percent_to_db, which turns the resulting
        listening_level back into a CamillaDSP output dB over the SAME
        -50 dB floor we advertise to the host. Keep the two aligned:
        host slider -> UAC2 step index (over the advertised -50..0 dB) ->
        _raw_to_pct (here) ->
        listening_level -> percent_to_db.

        The DB_MINMAX TLV remains load-bearing observability: it proves that
        configfs accepted the intended physical range. It is not the Mac
        slider's displayed percent and must not be amplitude-normalized.
        """
        span = self._vol_max - self._vol_min
        if span <= 0:
            return 50  # degenerate step range; pick something sane
        step_fraction = (raw - self._vol_min) / span
        step_fraction = max(0.0, min(1.0, step_fraction))
        pct = step_fraction * step_fraction * 100.0
        return max(0, min(100, round(pct)))

    def _cget(self, numid: int) -> str:
        """Synchronous cget — used at discovery time only."""
        proc = subprocess.run(
            ["amixer", "-c", self._card_name, "cget", f"numid={numid}"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        return proc.stdout

    # ------------------------------------------------------------------
    # jasper-control POST
    # ------------------------------------------------------------------

    async def _post(self, pct: int, *, initial: bool = False) -> Optional[bool]:
        """True when jasper-control applied the observation, False when it
        deliberately declined it, None when no usable answer came back."""
        if self._control is None:
            return None
        try:
            resp = await self._control.set_volume(
                pct,
                source="usbsink",
                observation_initial=initial,
            )
        except ControlError as e:
            log_event(
                logger,
                "usbsink.volume_post_failed",
                pct=pct,
                error=e,
                level=logging.WARNING,
            )
            return None
        if not resp.ok:
            log_event(
                logger,
                "usbsink.volume_post_bad_status",
                pct=pct,
                status=resp.status,
                level=logging.WARNING,
            )
            return None
        try:
            payload = resp.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            accepted = payload.get("observation_applied")
            if accepted is None:
                # Rolling-upgrade compatibility with an older control daemon:
                # its response lacks the explicit acknowledgement, but an
                # echoed canonical percent still proves the value landed.
                accepted = payload.get("percent") == pct
            if not bool(accepted):
                log_event(
                    logger,
                    "usbsink.volume_observation_deferred",
                    pct=pct,
                    canonical_pct=payload.get("percent", "unknown"),
                    level=logging.DEBUG,
                )
                return False
        else:
            # A successful but unparseable response cannot prove that the
            # source-active gate accepted the observation.
            return None
        log_event(
            logger,
            "usbsink.volume_observed",
            pct=pct,
            source="host_slider",
            raw=self._last_raw,
            muted=self._last_muted,
        )
        return True


class VolumeBridgeUnavailable(RuntimeError):
    """Raised by _discover() when the gadget mixer can't be read.
    The helper retries discovery with a bounded sleep; `jasper-doctor`'s
    usbsink card check surfaces the underlying cause when the card or
    descriptor stays broken."""
