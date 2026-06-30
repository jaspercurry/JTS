# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded Home Assistant status cache for jasper-control."""
from __future__ import annotations

import json
import hashlib
import logging
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 15.0
DEFAULT_TIMEOUT_SEC = 9.0
DEFAULT_MIN_REFRESH_INTERVAL_SEC = 2.0
DEFAULT_HA_ENV_FILE = "/var/lib/jasper-intsecrets/home_assistant.env"
_SIGNATURE_KEYS = (
    "JASPER_HA_URL",
    "JASPER_HA_TOKEN",
    "JASPER_HA_VERIFY_SSL",
)


def _ha_env_signature(path: str = DEFAULT_HA_ENV_FILE) -> str:
    values = {key: "" for key in _SIGNATURE_KEYS}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in values:
                    values[key] = value.strip()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return "unreadable:" + type(exc).__name__
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checking_status(cached: dict[str, Any] | None = None) -> dict[str, Any]:
    if cached is None:
        return {
            "configured": False,
            "connected": False,
            "url": "",
            "instance_name": None,
            "version": None,
            "error": None,
            "checking": True,
        }
    out = dict(cached)
    out["checking"] = True
    out["stale"] = True
    return out


def _failed_status(error: str) -> dict[str, Any]:
    return {
        "configured": False,
        "connected": False,
        "url": "",
        "instance_name": None,
        "version": None,
        "error": error,
    }


class HomeAssistantStatusCache:
    """Non-blocking status cache backed by a short-lived Python child.

    The first stale read starts one background refresh and returns a
    checking/stale status immediately. The child owns the HA probe and its
    imports; jasper-control keeps only the small JSON result in memory.
    """

    def __init__(
        self,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        min_refresh_interval_sec: float = DEFAULT_MIN_REFRESH_INTERVAL_SEC,
        python_exe: str | None = None,
        env_file_path: str = DEFAULT_HA_ENV_FILE,
        clock: Callable[[], float] | None = None,
        thread_factory: Callable[[Callable[[], None]], object] | None = None,
        signature_reader: Callable[[], str] | None = None,
    ) -> None:
        self._ttl_sec = max(1.0, float(ttl_sec))
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._min_refresh_interval_sec = max(0.1, float(min_refresh_interval_sec))
        self._python_exe = python_exe or sys.executable
        self._env_file_path = env_file_path
        self._clock = clock or time.monotonic
        self._thread_factory = thread_factory or self._start_thread
        self._signature_reader = signature_reader
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_signature = ""
        self._expires_at = 0.0
        self._next_refresh_at = 0.0
        self._refreshing = False

    @staticmethod
    def _start_thread(target: Callable[[], None]) -> threading.Thread:
        thread = threading.Thread(
            target=target,
            name="ha-status-refresh",
            daemon=True,
        )
        thread.start()
        return thread

    def _current_signature(self) -> str:
        if self._signature_reader is not None:
            return self._signature_reader()
        return _ha_env_signature(self._env_file_path)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        signature = self._current_signature()
        with self._lock:
            cached = dict(self._cached) if self._cached is not None else None
            signature_changed = (
                cached is not None
                and signature != self._cached_signature
            )
            if (
                cached is not None
                and now < self._expires_at
                and not signature_changed
            ):
                return cached

            should_refresh = (
                not self._refreshing
                and (now >= self._next_refresh_at or signature_changed)
            )
            if should_refresh:
                self._refreshing = True
                self._next_refresh_at = now + self._min_refresh_interval_sec

        if should_refresh:
            try:
                self._thread_factory(lambda: self._refresh_worker(signature))
            except Exception as exc:  # noqa: BLE001
                self._record_failure(exc, signature)
                with self._lock:
                    return dict(self._cached or _failed_status("probe failed"))

        return _checking_status(cached)

    def refresh_now(self) -> dict[str, Any]:
        """Synchronously refresh the cache. Used by focused unit tests."""
        self._refresh_worker(self._current_signature())
        with self._lock:
            return dict(self._cached or _failed_status("probe failed"))

    def _refresh_worker(self, signature: str) -> None:
        try:
            status = self._run_child()
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc, signature)
            return

        status.pop("checking", None)
        status.pop("stale", None)
        now = self._clock()
        with self._lock:
            self._cached = dict(status)
            self._cached_signature = signature
            self._expires_at = now + self._ttl_sec
            self._refreshing = False

    def _record_failure(self, exc: Exception, signature: str) -> None:
        log_event(
            logger,
            "ha.status_probe_failed",
            error=type(exc).__name__,
            detail=str(exc)[:200],
            level=logging.WARNING,
        )
        now = self._clock()
        with self._lock:
            status = dict(self._cached) if self._cached is not None else None
            if status is None:
                status = _failed_status("probe failed")
            else:
                status["stale"] = True
                status["error"] = "probe failed"
            status.pop("checking", None)
            self._cached = status
            self._cached_signature = signature
            self._expires_at = now
            self._refreshing = False

    def _run_child(self) -> dict[str, Any]:
        proc = subprocess.run(
            [self._python_exe, "-m", "jasper.control.ha_probe_child"],
            capture_output=True,
            text=True,
            timeout=self._timeout_sec,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:200]
            raise RuntimeError(f"child exited {proc.returncode}: {stderr}")
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("child returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("child returned non-object JSON")
        return payload
