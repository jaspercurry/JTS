"""Userspace-liveness supervisor — Tier 4.6 of the JTS resilience
ladder, closing the gap exposed by the 2026-05-23 incident.

Background
----------
systemd PID 1 patting `/dev/watchdog0` is a very weak liveness signal
— it just means PID 1's main loop got CPU at least once in the last
60 s. It does not confirm that sshd accepts connections, that
jasper-control answers HTTP, or that the kernel can satisfy a read
of `/proc/loadavg` within a reasonable time. On 2026-05-23 a PIO
compile pushed the 1 GB Pi 5 into zram-thrash for >2 minutes; PID 1
stayed alive enough to keep patting the hardware watchdog (Tier 5
never fired), but sshd banner exchange timed out — userspace was
effectively dead. Manual power-cycle was the only recovery.

What this supervisor does
-------------------------
Runs in `jasper-control`'s existing asyncio thread (no new daemon,
no new resident-RAM cost beyond ~0). Every 30 s ± jitter, probes:

  1. TCP connect to 127.0.0.1:22 (sshd accepting connections)
  2. HTTP GET 127.0.0.1:8780/healthz (jasper-control itself — yes,
     we probe ourselves; this catches the "we're hung in asyncio
     but systemd thinks we're alive" case)
  3. Read `/proc/loadavg` within 1 s (catches kernel I/O stall —
     even at heavy thrash, /proc reads should return)

After `failure_threshold` consecutive cycles where ANY probe fails,
rate-limited at 1 reboot per 24 hours, we issue `systemctl reboot`
(clean — filesystems unmount, journal flushes, dirty zram pages
sync). This is one tier *below* the kernel hardware watchdog (Tier
5) and one *above* per-service `Restart=on-failure` (Tier 2).

The 24-hour rate-limit is enforced against a WALL-CLOCK timestamp
persisted to `/var/lib/jasper/system_supervisor_reboot.json`, loaded
on construction. This is load-bearing: the rate-limit must survive the
reboot it just issued, or a *permanent* userspace wedge would
reboot-loop roughly every cold-start window (~3.5 min) forever and the
household could never reach jts.local to fix it. An in-memory or
monotonic-clock window would reset to nothing on the fresh post-reboot
process. The persisted read fails open — a missing or corrupt file
never blocks a genuinely-needed reboot.

Why not session-gated like ShairportSupervisor
-----------------------------------------------
A live voice session implies functioning mic capture + audio
playback + LLM session. `_run_all_probes` returns on the FIRST
failing probe (any-fail, not all-fail), so a cycle counts as failed
the moment any one of sshd / jasper-control / loadavg is unhealthy.
By the time even one probe has failed for 3 consecutive cycles, the
box is wedged badly enough that no live session is going through
anyway. The 24-hour rate-limit is the defensive guardrail; a
per-session gate would add complexity for no realistic payoff in
the failure-mode this supervisor targets.

Disable knob
------------
Set `JASPER_SYSTEM_SUPERVISOR=disabled` in /etc/jasper/jasper.env
and restart `jasper-control`. Mirrors the
`JASPER_SHAIRPORT_SUPERVISOR=disabled` pattern.

Design rationale
----------------
docs/HANDOFF-tier5-watchdog-liveness.md "Option A".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Wall-clock timestamp of the last supervisor-driven reboot, persisted so
# the 24-hour rate-limit survives the reboot it just issued. Without this,
# a *permanent* userspace wedge reboots ~every 3.5 min forever (cold-start
# 120 s + 3 failures × 30 s) and the household never reaches jts.local —
# the fresh post-reboot process starts with no in-memory record and
# CLOCK_MONOTONIC resets to ~0. Stored as wall-clock (time.time()), not
# monotonic, precisely because it must outlive the boot.
DEFAULT_REBOOT_STATE_PATH = Path("/var/lib/jasper/system_supervisor_reboot.json")


class SystemSupervisor:
    """Probes userspace liveness; clean-reboots on confident wedge.

    Override `probe_sshd`, `probe_jasper_control`, `probe_loadavg`, or
    `reboot_system` to inject test doubles. The policy in `_tick` is
    the unit under test."""

    DEFAULT_INTERVAL_SEC = 30.0
    DEFAULT_JITTER_SEC = 3.0
    DEFAULT_PROBE_TIMEOUT_SEC = 2.0
    DEFAULT_FAILURE_THRESHOLD = 3
    DEFAULT_RATE_LIMIT_SEC = 86_400.0   # 24 hours
    DEFAULT_COLD_START_SEC = 120.0      # 2 minutes — post-boot transient

    def __init__(
        self,
        *,
        sshd_host: str = "127.0.0.1",
        sshd_port: int = 22,
        control_host: str = "127.0.0.1",
        control_port: int = 8780,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        jitter_sec: float = DEFAULT_JITTER_SEC,
        probe_timeout_sec: float = DEFAULT_PROBE_TIMEOUT_SEC,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        rate_limit_sec: float = DEFAULT_RATE_LIMIT_SEC,
        cold_start_sec: float = DEFAULT_COLD_START_SEC,
        reboot_state_path: Path | str = DEFAULT_REBOOT_STATE_PATH,
    ) -> None:
        self._sshd_host = sshd_host
        self._sshd_port = sshd_port
        self._control_host = control_host
        self._control_port = control_port
        self._interval = interval_sec
        self._jitter = jitter_sec
        self._probe_timeout = probe_timeout_sec
        self._threshold = failure_threshold
        self._rate_limit = rate_limit_sec
        self._cold_start = cold_start_sec
        self._reboot_state_path = Path(reboot_state_path)
        # Observable state — read by snapshot() for /state.
        self.consecutive_failures: int = 0
        self.last_probe_at: float | None = None
        self.last_probe_ok: bool | None = None
        self.last_failed_probe: str | None = None
        self.reboot_count: int = 0
        self.suppressed_count: int = 0
        # Wall-clock timestamp of the last supervisor-driven reboot. The
        # rate-limit window is enforced against this. It is the same value
        # surfaced as last_reboot_at in snapshot(). Loaded from the persisted
        # state file on construction so the window survives reboots — the
        # whole point of T5.2's rate-limit hardening. Fail-open: a missing or
        # corrupt file leaves it None (a genuinely-needed reboot is never
        # blocked by a bad byte on disk).
        self.last_reboot_at: float | None = _read_reboot_state(
            self._reboot_state_path,
        )

    # ---- main loop ----

    async def run(self) -> None:
        logger.info(
            "event=system_supervisor.start "
            "interval=%.0fs threshold=%d rate_limit=%.0fs cold_start=%.0fs",
            self._interval, self._threshold, self._rate_limit,
            self._cold_start,
        )
        await asyncio.sleep(self._cold_start)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("event=system_supervisor.tick_crash")
            await asyncio.sleep(self._interval + random.uniform(
                -self._jitter, self._jitter,
            ))

    async def _tick(self) -> None:
        ok, failed_probe = await self._run_all_probes()
        self.last_probe_at = time.time()
        self.last_probe_ok = ok
        self.last_failed_probe = failed_probe
        if ok:
            if self.consecutive_failures > 0:
                logger.info(
                    "event=system_supervisor.probe_recovered "
                    "after_failures=%d",
                    self.consecutive_failures,
                )
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        logger.warning(
            "event=system_supervisor.probe_fail "
            "consecutive=%d threshold=%d failed_probe=%s",
            self.consecutive_failures, self._threshold, failed_probe,
        )
        if self.consecutive_failures < self._threshold:
            return
        # Rate-limit: at most one supervisor-driven reboot per window.
        # 24 h is the default — a wedge that recurs within a day is
        # not something a second reboot will fix; surface to the
        # operator instead. The clock is WALL-CLOCK (not monotonic) and
        # the last-reboot time is persisted to disk, so this window
        # holds across the reboot it just issued — otherwise a permanent
        # wedge would reboot-loop forever past the bare cold-start delay.
        now = self._now()
        if (
            self.last_reboot_at is not None
            and now - self.last_reboot_at < self._rate_limit
        ):
            self.suppressed_count += 1
            logger.warning(
                "event=system_supervisor.reboot_rate_limited "
                "since_last_reboot=%.0fs limit=%.0fs suppressed=%d",
                now - self.last_reboot_at, self._rate_limit,
                self.suppressed_count,
            )
            return
        self.last_reboot_at = now
        _write_reboot_state(self._reboot_state_path, now)
        self.reboot_count += 1
        self.consecutive_failures = 0
        logger.error(
            "event=system_supervisor.userspace_wedge action=reboot "
            "count=%d failed_probe=%s",
            self.reboot_count, failed_probe,
        )
        try:
            await self.reboot_system()
        except Exception:  # noqa: BLE001
            logger.exception("event=system_supervisor.reboot_failed")

    async def _run_all_probes(self) -> tuple[bool, str | None]:
        """Run all three probes. Returns (all_succeeded, name_of_first_failed).

        Returns on first failure — we don't need a full failure
        attribution, just "is the system healthy or not"."""
        try:
            ok = await self.probe_sshd()
        except Exception:  # noqa: BLE001
            logger.exception("event=system_supervisor.probe_crash probe=sshd")
            ok = False
        if not ok:
            return False, "sshd"
        try:
            ok = await self.probe_jasper_control()
        except Exception:  # noqa: BLE001
            logger.exception(
                "event=system_supervisor.probe_crash probe=jasper_control",
            )
            ok = False
        if not ok:
            return False, "jasper_control"
        try:
            ok = await self.probe_loadavg()
        except Exception:  # noqa: BLE001
            logger.exception(
                "event=system_supervisor.probe_crash probe=loadavg",
            )
            ok = False
        if not ok:
            return False, "loadavg"
        return True, None

    # ---- overridable IO ----

    async def probe_sshd(self) -> bool:
        """TCP connect to localhost sshd within timeout. We don't
        complete the SSH handshake — that requires a key + would
        leave a lingering audit trail. Just "can sshd accept the
        TCP connection." Banner exchange failure won't show here
        but probe_jasper_control catches a related failure shape."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._sshd_host, self._sshd_port),
                timeout=self._probe_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            # Optionally: read the SSH banner ("SSH-2.0-OpenSSH_..."
            # within ~1 s). On a wedged userspace, sshd accepts the
            # TCP connection (kernel-side) but doesn't write the
            # banner — which is exactly the 2026-05-23 shape.
            data = await asyncio.wait_for(
                reader.read(64), timeout=self._probe_timeout,
            )
            return data.startswith(b"SSH-")
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (OSError, asyncio.TimeoutError):
                pass

    async def probe_jasper_control(self) -> bool:
        """HTTP GET /healthz on localhost jasper-control. We're
        probing ourselves — yes, intentionally. This catches the case
        where our event loop is wedged (e.g. blocked in a sync
        operation holding the loop) while systemd still sees us as
        alive."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._control_host, self._control_port),
                timeout=self._probe_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.write(
                b"GET /healthz HTTP/1.0\r\n"
                b"Host: 127.0.0.1\r\n"
                b"User-Agent: jasper-control/system_supervisor\r\n"
                b"\r\n"
            )
            await asyncio.wait_for(
                writer.drain(), timeout=self._probe_timeout,
            )
            data = await asyncio.wait_for(
                reader.read(64), timeout=self._probe_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (OSError, asyncio.TimeoutError):
                pass
        # 200 OK from healthz — anything else (502, timeout, half-
        # closed connection) is a failure.
        return data.startswith(b"HTTP/1.0 200") or data.startswith(b"HTTP/1.1 200")

    async def probe_loadavg(self) -> bool:
        """Can we read /proc/loadavg within 1 s? Kernel I/O stall
        (the 2026-05-23 shape was zram thrash starving readers)
        manifests as ridiculously slow /proc reads. This catches
        that without trying to schedule actual work."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_read_loadavg),
                timeout=1.0,
            )
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def reboot_system(self) -> None:
        """Clean reboot via `systemctl reboot`. NOT `reboot-force` —
        we want filesystems unmounted and zram dirty pages synced.
        `--no-block` returns as soon as the job is enqueued; the
        actual reboot proceeds asynchronously."""
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--no-block", "reboot",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5.0)

    # ---- accessors ----

    def _now(self) -> float:
        # Seam for rate-limit testing — overridden in tests. Wall-clock,
        # not monotonic: the rate-limit window is enforced against a
        # persisted last-reboot time that must survive reboots, and
        # CLOCK_MONOTONIC resets to ~0 on every boot.
        return time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "last_probe_at": self.last_probe_at,
            "last_probe_ok": self.last_probe_ok,
            "last_failed_probe": self.last_failed_probe,
            "consecutive_failures": self.consecutive_failures,
            "reboot_count": self.reboot_count,
            "last_reboot_at": self.last_reboot_at,
            "suppressed_count": self.suppressed_count,
        }


def _read_loadavg() -> str:
    """Synchronous /proc/loadavg read — runs on a thread so a wedge
    of the read doesn't block the event loop."""
    with open("/proc/loadavg") as f:
        return f.read()


def _read_reboot_state(path: Path) -> float | None:
    """Return the persisted wall-clock last-reboot time, or None.

    Fail-open by design: a missing, unreadable, corrupt, or malformed
    file resolves to None so a genuinely-needed reboot is never blocked
    by one bad byte on disk. Mirrors the conservative read in
    `jasper/wifi_scan_repair.py`."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("last_reboot_at")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_reboot_state(path: Path, last_reboot_at: float) -> None:
    """Atomically persist the wall-clock last-reboot time.

    Tempfile + os.replace mirrors `jasper/wifi_scan_repair.py._write_state`.
    A write failure is logged and swallowed — losing the persisted
    timestamp degrades to today's in-memory-only behaviour rather than
    blocking the reboot we're about to issue."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(
            json.dumps({"last_reboot_at": last_reboot_at}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(
            "event=system_supervisor.reboot_state_write_failed path=%s err=%r",
            path, e,
        )


# Module-level supervisor instance, set by start_supervisor() when
# enabled. snapshot() reads it; the HTTP handler thread is read-only
# wrt the instance and Python attribute reads are atomic at the
# snapshot dict's resolution.
_supervisor: SystemSupervisor | None = None
_supervisor_thread: threading.Thread | None = None


def snapshot() -> dict[str, Any]:
    """Read-only state for /state. Returns `{"enabled": False}` when
    the supervisor is disabled or not yet running."""
    if _supervisor is None:
        return {"enabled": False}
    return _supervisor.snapshot()


def start_supervisor() -> threading.Thread | None:
    """Start the supervisor in a background thread. No-op when
    `JASPER_SYSTEM_SUPERVISOR=disabled` (exact match, case-
    insensitive). Idempotent under sequential calls — the sole
    caller is `jasper-control`'s single-threaded `main()`."""
    global _supervisor, _supervisor_thread
    if _supervisor_thread is not None:
        return _supervisor_thread
    mode = os.environ.get("JASPER_SYSTEM_SUPERVISOR", "auto").lower()
    if mode == "disabled":
        logger.info("event=system_supervisor.disabled")
        return None
    if mode != "auto":
        logger.warning(
            "JASPER_SYSTEM_SUPERVISOR=%r unrecognized; "
            "treating as 'auto'. Use 'disabled' to turn the supervisor off.",
            mode,
        )
    _supervisor = SystemSupervisor()

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_supervisor.run())
        except Exception:  # noqa: BLE001
            logger.exception("event=system_supervisor.thread_crash")
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    _supervisor_thread = threading.Thread(
        target=_run, name="system-supervisor", daemon=True,
    )
    _supervisor_thread.start()
    return _supervisor_thread
