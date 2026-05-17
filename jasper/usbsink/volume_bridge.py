"""Observe the host's volume slider via ALSA mixer, route to JTS.

The UAC2 gadget exposes the host's volume slider as ALSA mixer
controls on the Pi side:

  - "PCM Capture Volume"   (integer, range card-defined)
  - "PCM Capture Switch"   (bool — Mac mute toggle)

This module polls those controls at 4 Hz via `amixer cget`, maps the
raw value to JTS's 0-100 listening_level (linear over the mixer
range), and POSTs to jasper-control's /volume/set endpoint with
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
import os
import re
import subprocess
from typing import Optional

import httpx

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
        http_timeout_sec: float = 2.0,
    ) -> None:
        self._card_name = card_name
        self._control_url = control_url.rstrip("/")
        self._poll_interval = poll_interval_sec
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

        self._http: Optional[httpx.AsyncClient] = None

    async def run(self) -> None:
        """Discover the gadget mixer's numids + range, then poll for
        changes forever. Cancellable from the daemon's shutdown path."""
        # Defer mixer discovery until run() — at __init__ time the
        # gadget card may not have enumerated yet (init.service has
        # only just returned).
        try:
            self._discover()
        except VolumeBridgeUnavailable as e:
            logger.warning(
                "event=usbsink.volume_bridge_disabled reason=%s", e,
            )
            # Mixer isn't present. We'll quietly sit here waiting for
            # cancellation rather than spinning on discovery retries —
            # if the gadget card never enumerated, something at the
            # init.service level is broken and that's where to fix it.
            return

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            self._http = client
            logger.info(
                "event=usbsink.volume_bridge_started "
                "card=%s vol_numid=%d switch_numid=%d range=%d..%d",
                self._card_name, self._vol_numid, self._switch_numid,
                self._vol_min, self._vol_max,
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
                        logger.debug(
                            "event=usbsink.volume_tick_error error=%s",
                            e,
                        )
            except asyncio.CancelledError:
                logger.info("event=usbsink.volume_bridge_stopping")
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
        span = self._vol_max - self._vol_min
        if span <= 0:
            return 50  # degenerate; pick something sane
        pct = (raw - self._vol_min) / span * 100.0
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

    async def _post(self, pct: int) -> None:
        if self._http is None:
            return
        url = f"{self._control_url}/volume/set"
        try:
            resp = await self._http.post(
                url, json={"percent": pct, "source": "usbsink"},
            )
        except httpx.HTTPError as e:
            logger.warning(
                "event=usbsink.volume_post_failed pct=%d error=%s",
                pct, e,
            )
            return
        if resp.status_code != 200:
            logger.warning(
                "event=usbsink.volume_post_bad_status pct=%d status=%d",
                pct, resp.status_code,
            )
            return
        logger.info(
            "event=usbsink.volume_observed pct=%d source=host_slider",
            pct,
        )


class VolumeBridgeUnavailable(RuntimeError):
    """Raised by _discover() when the gadget mixer can't be read.
    The daemon's run() catches this and idles — `jasper-doctor`'s
    usbsink card check is responsible for surfacing the underlying
    cause (no card, descriptor missing controls, etc.)."""
