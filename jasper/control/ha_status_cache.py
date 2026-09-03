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
from collections.abc import Mapping
from typing import Any, Callable

from jasper import home_assistant
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 15.0
DEFAULT_TIMEOUT_SEC = 9.0
DEFAULT_MIN_REFRESH_INTERVAL_SEC = 2.0
_SIGNATURE_KEYS = (
    home_assistant.ENV_URL,
    home_assistant.ENV_TOKEN,
    home_assistant.ENV_VERIFY_SSL,
)


def _env_signature(values: Mapping[str, str]) -> str:
    payload = json.dumps(
        {key: values.get(key, "") for key in _SIGNATURE_KEYS},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unconfigured_status(values: Mapping[str, str]) -> dict[str, Any] | None:
    return home_assistant.unconfigured_status(
        values.get(home_assistant.ENV_URL, "").strip(),
        values.get(home_assistant.ENV_TOKEN, "").strip(),
    )


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
    An env file without usable credentials is answered by the parent, with
    no child at all.
    """

    def __init__(
        self,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        min_refresh_interval_sec: float = DEFAULT_MIN_REFRESH_INTERVAL_SEC,
        python_exe: str | None = None,
        env_file_path: str = home_assistant.HA_ENV_FILE,
        clock: Callable[[], float] | None = None,
        thread_factory: Callable[[Callable[[], None]], object] | None = None,
        env_reader: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._ttl_sec = max(1.0, float(ttl_sec))
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._min_refresh_interval_sec = max(0.1, float(min_refresh_interval_sec))
        self._python_exe = python_exe or sys.executable
        self._env_file_path = env_file_path
        self._clock = clock or time.monotonic
        self._thread_factory = thread_factory or self._start_thread
        self._env_reader = env_reader
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_signature = ""
        self._expires_at = 0.0
        self._next_refresh_at = 0.0
        self._refreshing = False
        self._last_state: tuple[bool, bool] | None = None

    @staticmethod
    def _start_thread(target: Callable[[], None]) -> threading.Thread:
        thread = threading.Thread(
            target=target,
            name="ha-status-refresh",
            daemon=True,
        )
        thread.start()
        return thread

    def _read_env(self) -> Mapping[str, str]:
        if self._env_reader is not None:
            return self._env_reader()
        return home_assistant.read_ha_env_file(self._env_file_path)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        values = self._read_env()
        signature = _env_signature(values)
        unconfigured = _unconfigured_status(values)
        if unconfigured is not None:
            # A resident daemon must not fork an interpreter on a poll
            # loop; with no credentials the child can only echo this.
            self._store(unconfigured, signature, now, skip_if_refreshing=True)
            return dict(unconfigured)

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

    def _refresh_worker(self, signature: str) -> None:
        try:
            status = self._run_child()
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc, signature)
            return

        status.pop("checking", None)
        status.pop("stale", None)
        self._store(status, signature, self._clock())

    def _store(
        self,
        status: dict[str, Any],
        signature: str,
        now: float,
        *,
        skip_if_refreshing: bool = False,
    ) -> None:
        with self._lock:
            if skip_if_refreshing and self._refreshing:
                return
            self._cached = dict(status)
            self._cached_signature = signature
            self._expires_at = now + self._ttl_sec
            self._refreshing = False
        self._log_transition(status)

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

    def _log_transition(self, status: dict[str, Any]) -> None:
        """Emit parent-owned HA reachability transitions from child results."""

        new_state = (
            bool(status.get("configured")),
            bool(status.get("connected")),
        )
        with self._lock:
            old_state = self._last_state
            self._last_state = new_state

        if old_state is None:
            if not new_state[0]:
                return
        elif old_state == new_state:
            return

        if new_state[0] and new_state[1]:
            log_event(
                logger,
                "ha.reachable",
                url=status.get("url") or "",
                instance=status.get("instance_name") or "?",
                version=status.get("version") or "?",
            )
        elif new_state[0] and not new_state[1]:
            log_event(
                logger,
                "ha.unreachable",
                url=status.get("url") or "",
                error=status.get("error") or "unknown",
                level=logging.WARNING,
            )
        else:
            log_event(logger, "ha.unconfigured")
