# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Observe the host's volume slider via ALSA mixer, route to JTS.

The UAC2 gadget exposes the host's volume slider as ALSA mixer
controls on the Pi side:

  - "PCM Capture Volume"   (integer, range card-defined)
  - "PCM Capture Switch"   (bool — Mac mute toggle)

This module polls those controls at 4 Hz via `amixer cget`, maps the
raw value to JTS's 0-100 listening_level (amplitude-normalized over
the mixer's dB range — see `_raw_to_pct`), and POSTs to
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
from typing import Optional

from jasper.control.client import AsyncControlClient, ControlError
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


# Poll cadence. 250 ms = 4 Hz. Faster wastes CPU; slower introduces
# perceptible lag.
POLL_INTERVAL_SEC = 0.25

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


# `amixer cget` output format:
#
#     numid=1,iface=MIXER,name='PCM Capture Volume'
#       ; type=INTEGER,access=rw---R--,values=1,min=0,max=100,step=0
#       : values=50
#
# - First line: identifier
# - Second line: spec (type, min, max, step)
# - Third line: current value(s) — comma-separated for multi-channel
#
# Switch control output:
#     numid=2,iface=MIXER,name='PCM Capture Switch'
#       ; type=BOOLEAN,access=rw------,values=1
#       : values=on
_NUMID_RE = re.compile(r"numid=(\d+),iface=MIXER,name='([^']+)'")
_RANGE_RE = re.compile(r"min=(-?\d+),max=(-?\d+)")
_VALUES_RE = re.compile(r": values=([^\n]+)")


# Centi-dB per raw ALSA unit for "PCM Capture Volume". The u_audio gadget
# driver reports the control in 1/100 dB (centi-dB), matching the
# c_volume_min/max/res we advertise from deploy/usbsink/jasper-usbgadget-up
# (-5000 / 0 / 100 = -50 dB / 0 dB / 1 dB step). This is the single tunable
# knob for the raw->dB scale: amixer does not reliably expose the control's
# resolution (`step=0` in practice), so we assume 0.01 dB/unit rather than
# reading it back. If a future kernel reports the control in different units,
# adjust here and re-verify against a Mac slider (see _raw_to_pct).
_CENTI_DB_PER_UNIT = 0.01


class VolumeBridge:
    """Polls the gadget mixer at 4 Hz, POSTs changes to jasper-control.

    Lifecycle:
        bridge.run()  # async, blocks until cancelled

    The bridge does NOT cache jasper-control state. Every observed
    mixer change triggers one POST; the coordinator on the receiving
    side dedupes via observe_source_volume's echo logic. This keeps
    the bridge stateless and easy to restart.
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
        self._vol_min: int = 0
        self._vol_max: int = 100

        # Last value we POSTed — dedupes successive identical polls.
        # None until the first successful read; -1 sentinel would also
        # work but None is clearer for "no observation yet".
        self._last_published_pct: Optional[int] = None

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
            range=f"{self._vol_min}..{self._vol_max}",
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

        # Parse the volume control's range from a one-shot cget.
        cg = self._cget(self._vol_numid)
        m = _RANGE_RE.search(cg)
        if m:
            self._vol_min = int(m.group(1))
            self._vol_max = int(m.group(2))

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
        await self._post(pct)
        self._last_published_pct = pct

    def _raw_to_pct(self, raw: int) -> int:
        """THE volume curve: raw mixer value -> JTS 0-100 percent.

        Normalize in the AMPLITUDE domain, not linearly in dB. The UAC2
        "PCM Capture Volume" control is dB-scaled, and macOS maps its
        slider POSITION perceptually (~logarithmically) onto that dB
        range. So normalizing linearly in dB is NOT the inverse of
        Apple's taper — it over-reads the bottom half of the slider (a
        low-mid slider read ~73% before this fix; issue #1698). Converting
        the observed dB to a linear amplitude (10**(dB/20)) and normalizing
        THAT approximates the perceptual taper — the standard close
        approximation. Exact 25%<->25% tracking would need Apple's
        undocumented transfer curve; this, plus the narrow advertised
        range (-50..0 dB, written by jasper-usbgadget-up), is what lands a
        real mid-slider near mid. Final feel needs an on-Mac slider check.

        This is one END of a two-ended contract. The other end is
        jasper.volume_curve.percent_to_db, which turns the resulting
        listening_level back into a CamillaDSP output dB over the SAME
        -50 dB floor we advertise to the host. Keep the two aligned:
        host slider -> UAC2 dB (over -50..0) -> _raw_to_pct (here) ->
        listening_level -> percent_to_db.
        """
        if self._vol_max - self._vol_min <= 0:
            return 50  # degenerate range; pick something sane
        amp = self._raw_amplitude(raw)
        amp_min = self._raw_amplitude(self._vol_min)
        amp_max = self._raw_amplitude(self._vol_max)
        denom = amp_max - amp_min
        if denom <= 0:
            return 50  # degenerate amplitude range; pick something sane
        pct = (amp - amp_min) / denom * 100.0
        return max(0, min(100, round(pct)))

    @staticmethod
    def _raw_amplitude(raw: int) -> float:
        """Raw centi-dB mixer units -> linear amplitude (10**(dB/20))."""
        return 10.0 ** ((raw * _CENTI_DB_PER_UNIT) / 20.0)

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

    async def _post(self, pct: int) -> None:
        if self._control is None:
            return
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
            return
        if not resp.ok:
            log_event(
                logger,
                "usbsink.volume_post_bad_status",
                pct=pct,
                status=resp.status,
                level=logging.WARNING,
            )
            return
        log_event(
            logger,
            "usbsink.volume_observed",
            pct=pct,
            source="host_slider",
        )


class VolumeBridgeUnavailable(RuntimeError):
    """Raised by _discover() when the gadget mixer can't be read.
    The helper retries discovery with a bounded sleep; `jasper-doctor`'s
    usbsink card check surfaces the underlying cause when the card or
    descriptor stays broken."""
