# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Observe the host's volume slider via ALSA mixer, route to JTS.

The UAC2 gadget exposes the host's volume slider as ALSA mixer
controls on the Pi side:

  - "PCM Capture Volume"   (integer, range card-defined)
  - "PCM Capture Switch"   (bool — Mac mute toggle)

This module polls those controls at 4 Hz via `amixer cget`, maps the
raw value to JTS's 0-100 listening_level by inverting macOS's observed
square-root step transfer (see `_raw_to_pct`), and POSTs to
jasper-control's /volume/set endpoint with
source="usbsink". The endpoint routes through
VolumeCoordinator.observe_source_volume(), which goes through echo
prevention — so a dial twist that triggered an outbound write to the
gadget mixer (we don't actually do this, see HANDOFF-usbsink.md §3.2
"Why no outbound write back to the host") wouldn't bounce back as a
phantom user-side change.

Polling vs event-driven. The naive choice would be pyalsaaudio's
event API + asyncio.add_reader on the mixer FD. We picked polling
for production-grade reasons:

  - 250 ms latency is imperceptible. Mac slider moves are sparse user
    actions (a few per minute at most); the response feels instant.
  - One fewer system dependency. `amixer` is in alsa-utils which
    install.sh already requires; pyalsaaudio would mean a new apt
    package, a new Python dep, and one more thing to be wrong on
    upgrade.
  - Robust under daemon restart. Mixer-event subscribers have to
    handle the FD lifecycle (re-subscribe after card hot-unplug,
    drain events on resume). Polling is stateless.

We DO subprocess `amixer` rather than directly hitting /dev/snd/
controlC* — wrapping the C API via ctypes was the other alternative
considered, but rejected as RAM-equivalent and harder to debug.
`amixer cget` output is stable and easy to parse with a regex.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from typing import Optional

from jasper.control.client import AsyncControlClient, ControlError
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


# Poll cadence. 250 ms = 4 Hz. Faster wastes CPU; slower introduces
# perceptible lag.
POLL_INTERVAL_SEC = 0.25
# When jasper-control declines an observation because USB is not the active
# source yet, retry at a bounded cadence. This closes the boot/deploy race where
# the first mixer read arrived before source activation and was then cached
# forever. A changed host value bypasses the delay and publishes immediately.
POST_RETRY_INTERVAL_SEC = 1.0

# Mixer control names as the u_audio gadget driver exposes them.
# These are fixed by the kernel module, not by our gadget descriptor —
# `c_fu_vol_name` in the gadget script sets the host-visible label
# but the ALSA mixer name stays "PCM Capture Volume".
VOL_CONTROL_NAME = "PCM Capture Volume"
SWITCH_CONTROL_NAME = "PCM Capture Switch"

# Where jasper-control listens. The /volume/set endpoint accepts an
# optional `source` field; with source="usbsink" the coordinator
# routes through observe_source_volume (echo-prevented) rather than
# set_listening_level (authoritative). See jasper.control.server for
# the handler.
DEFAULT_CONTROL_URL = "http://127.0.0.1:8780"


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
_VALUES_RE = re.compile(r": values=([^\n]+)")
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


class VolumeBridge:
    """Polls the gadget mixer at 4 Hz, POSTs changes to jasper-control.

    Lifecycle:
        bridge.run()  # async, blocks until cancelled

    The bridge does NOT cache jasper-control state. Every observed
    mixer change triggers one POST; a value declined because USB is
    not active yet is retried at a bounded cadence until the controller
    acknowledges it. Accepted values are deduplicated locally, while
    the coordinator owns source and echo policy.
    """

    def __init__(
        self,
        card_name: str = "UAC2Gadget",
        control_url: str = DEFAULT_CONTROL_URL,
        *,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
        discovery_retry_interval_sec: float = 5.0,
        http_timeout_sec: float = 2.0,
    ) -> None:
        self._card_name = card_name
        self._control_url = control_url.rstrip("/")
        self._poll_interval = poll_interval_sec
        self._discovery_retry_interval = discovery_retry_interval_sec
        self._http_timeout = http_timeout_sec

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

        # Last value we POSTed — dedupes successive identical polls.
        # None until jasper-control confirms that the observation was accepted;
        # transport success alone is insufficient because the controller
        # deliberately declines observations from an inactive source.
        self._last_published_pct: Optional[int] = None
        self._last_attempted_pct: Optional[int] = None
        self._retry_not_before: float = 0.0

        # Bound once `run()` clears mixer discovery (mirrors where the old
        # httpx client was opened). None means discovery has not succeeded
        # yet. The client carries no connection pool, so there's nothing
        # to close on shutdown.
        self._control: Optional[AsyncControlClient] = None

    async def run(self) -> None:
        """Discover the gadget mixer's numids + range, then poll for
        changes forever. Cancellable from the daemon's shutdown path."""
        # Defer mixer discovery until run() — at __init__ time the
        # gadget card may not have enumerated yet (init.service has
        # only just returned).
        while True:
            try:
                self._discover()
                break
            except VolumeBridgeUnavailable as e:
                log_event(
                    logger,
                    "usbsink.volume_bridge_unavailable",
                    reason=e,
                    retry_sec=self._discovery_retry_interval,
                    level=logging.WARNING,
                )
                await asyncio.sleep(self._discovery_retry_interval)

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
            # operator can see e.g. `range=0..50 db=-50.0..0.0 db_source=tlv`
            # and tell whether the advertised range actually stuck.
            range=f"{self._vol_min}..{self._vol_max}",
            db=f"{self._db_min:.1f}..{self._db_max:.1f}",
            db_source=self._db_source,
        )
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    # One bad poll shouldn't kill the loop — log
                    # and continue. Persistent failures get caught
                    # by the daemon's diagnostic log.
                    log_event(
                        logger,
                        "usbsink.volume_tick_error",
                        error=e,
                        level=logging.DEBUG,
                    )
        except asyncio.CancelledError:
            log_event(logger, "usbsink.volume_bridge_stopping")
            raise

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
    # Per-tick: read both controls, post if changed
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        raw_vol = await asyncio.to_thread(self._read_int_value, self._vol_numid)
        if raw_vol is None:
            return
        muted = False
        if self._switch_numid is not None:
            muted = await asyncio.to_thread(
                self._read_switch_value, self._switch_numid,
            )

        # Map raw → percent. Mute overrides to 0.
        pct = 0 if muted else self._raw_to_pct(raw_vol)
        if pct == self._last_published_pct:
            return
        now = time.monotonic()
        if (
            pct == self._last_attempted_pct
            and now < self._retry_not_before
        ):
            return
        self._last_attempted_pct = pct
        accepted = await self._post(pct)
        if accepted:
            self._last_published_pct = pct
            self._retry_not_before = 0.0
        else:
            self._retry_not_before = now + POST_RETRY_INTERVAL_SEC

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

    # ------------------------------------------------------------------
    # amixer subprocess helpers
    # ------------------------------------------------------------------

    def _cget(self, numid: int) -> str:
        """Synchronous cget — used at discovery time. The async path
        wraps this in asyncio.to_thread()."""
        proc = subprocess.run(
            ["amixer", "-c", self._card_name, "cget", f"numid={numid}"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        return proc.stdout

    def _read_int_value(self, numid: int) -> Optional[int]:
        out = self._cget(numid)
        m = _VALUES_RE.search(out)
        if m is None:
            return None
        # values can be "50" or "50,50" for stereo. Take first.
        first = m.group(1).split(",")[0].strip()
        try:
            return int(first)
        except ValueError:
            return None

    def _read_switch_value(self, numid: int) -> bool:
        """Switch controls report 'on' / 'off' rather than int."""
        out = self._cget(numid)
        m = _VALUES_RE.search(out)
        if m is None:
            return False
        # Stereo switch sometimes reports "on,on" or "off,off"; if
        # either channel is off, treat as muted.
        parts = [p.strip() for p in m.group(1).split(",")]
        if not parts:
            return False
        return any(p != "on" for p in parts)

    # ------------------------------------------------------------------
    # jasper-control POST
    # ------------------------------------------------------------------

    async def _post(self, pct: int) -> bool:
        if self._control is None:
            return False
        try:
            resp = await self._control.set_volume(pct, source="usbsink")
        except ControlError as e:
            log_event(
                logger,
                "usbsink.volume_post_failed",
                pct=pct,
                error=e,
                level=logging.WARNING,
            )
            return False
        if not resp.ok:
            log_event(
                logger,
                "usbsink.volume_post_bad_status",
                pct=pct,
                status=resp.status,
                level=logging.WARNING,
            )
            return False
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
            return False
        log_event(
            logger,
            "usbsink.volume_observed",
            pct=pct,
            source="host_slider",
        )
        return True


class VolumeBridgeUnavailable(RuntimeError):
    """Raised by _discover() when the gadget mixer can't be read.
    The helper retries discovery with a bounded sleep; `jasper-doctor`'s
    usbsink card check surfaces the underlying cause when the card or
    descriptor stays broken."""
