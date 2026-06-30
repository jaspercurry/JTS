# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded Home Assistant status cache for the system dashboard."""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from typing import Any

from .. import home_assistant

logger = logging.getLogger(__name__)


DEFAULT_TTL_SEC = 60.0
DEFAULT_TIMEOUT_SEC = 8.0


def _unconfigured_status() -> dict[str, Any]:
    return {
        "configured": False,
        "connected": False,
        "url": "",
        "instance_name": None,
        "version": None,
        "error": None,
    }


def _pending_status(url: str) -> dict[str, Any]:
    return {
        "configured": True,
        "connected": None,
        "url": url,
        "instance_name": None,
        "version": None,
        "error": "probe pending",
        "pending": True,
    }


class HomeAssistantStatusCache:
    """Non-blocking HA status cache for `/system/snapshot`.

    The expensive HA probe imports httpx. Running it in a short-lived child
    keeps those pages out of the long-lived jasper-control process while still
    giving the dashboard a fresh-ish status.
    """

    def __init__(
        self,
        *,
        env_file_path: str = home_assistant.HA_ENV_FILE,
        ttl_sec: float = DEFAULT_TTL_SEC,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        python_executable: str | None = None,
    ) -> None:
        self._env_file_path = env_file_path
        self._ttl_sec = ttl_sec
        self._timeout_sec = timeout_sec
        self._python = python_executable or sys.executable
        self._lock = threading.Lock()
        self._key: tuple[str, str, bool] | None = None
        self._deadline = 0.0
        self._status: dict[str, Any] | None = None
        self._refreshing = False

    def snapshot(self) -> dict[str, Any]:
        state = home_assistant.read_ha_env_file(self._env_file_path)
        url = state.get(home_assistant.ENV_URL, "").strip()
        token = state.get(home_assistant.ENV_TOKEN, "").strip()
        verify_ssl = state.get(home_assistant.ENV_VERIFY_SSL, "1").strip() not in (
            "0",
            "false",
            "no",
        )
        if not url or not token:
            with self._lock:
                self._key = None
                self._deadline = 0.0
                self._status = None
                self._refreshing = False
            return _unconfigured_status()

        key = (
            url,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            verify_ssl,
        )
        now = time.monotonic()
        with self._lock:
            if self._key == key and self._status is not None and now < self._deadline:
                return dict(self._status)
            if not self._refreshing:
                self._refreshing = True
                thread = threading.Thread(
                    target=self._refresh,
                    args=(key,),
                    name="jasper-ha-status-refresh",
                    daemon=True,
                )
                thread.start()
            if self._key == key and self._status is not None:
                stale = dict(self._status)
                stale["stale"] = True
                return stale
        return _pending_status(url)

    def _refresh(self, key: tuple[str, str, bool]) -> None:
        status: dict[str, Any]
        try:
            proc = subprocess.run(
                [
                    self._python,
                    "-m",
                    "jasper.control.ha_probe_child",
                    self._env_file_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self._timeout_sec,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"probe exited {proc.returncode}")
            parsed = json.loads(proc.stdout)
            if not isinstance(parsed, dict):
                raise ValueError("probe returned non-object JSON")
            status = parsed
        except subprocess.TimeoutExpired:
            status = {
                "configured": True,
                "connected": False,
                "url": key[0],
                "instance_name": None,
                "version": None,
                "error": "probe timed out",
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("ha status child probe failed: %r", exc)
            status = {
                "configured": True,
                "connected": False,
                "url": key[0],
                "instance_name": None,
                "version": None,
                "error": "probe failed",
            }
        with self._lock:
            self._key = key
            self._deadline = time.monotonic() + self._ttl_sec
            self._status = status
            self._refreshing = False
