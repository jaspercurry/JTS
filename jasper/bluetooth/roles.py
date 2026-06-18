"""Minimal persistence — `{mac: handler_id}` at /var/lib/jasper/bt_roles.json.

BlueZ owns the pair database (link keys, alias, trust flag). The one
thing it doesn't track is which JTS-side handler should pick up a
device when it re-connects on its own (e.g., the rotary knob coming
back into range and showing up at `/dev/input/event*` — is it a HID
input we route to jasper-control, or a BT speaker source we let
bluez-alsa pick up?). That mapping lives here.

Atomic write via tmpfile + rename. Tiny — never larger than a few KB.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/var/lib/jasper/bt_roles.json"


class RoleStore:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, str]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            logger.warning("bt_roles: read failed (%s)", e)
            return {}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            return {
                str(k).upper(): str(v)
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str)
            }
        except json.JSONDecodeError as e:
            logger.warning("bt_roles: parse failed (%s)", e)
            return {}

    def get(self, mac: str) -> str | None:
        return self.load().get(mac.upper())

    def set(self, mac: str, handler_id: str) -> None:
        data = self.load()
        data[mac.upper()] = handler_id
        self._write(data)

    def remove(self, mac: str) -> None:
        data = self.load()
        if data.pop(mac.upper(), None) is not None:
            self._write(data)

    def _write(self, data: dict[str, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("bt_roles: mkdir failed (%s)", e)
            return
        body = json.dumps(data, indent=2, sort_keys=True)
        try:
            fd, tmp = tempfile.mkstemp(
                prefix=".bt_roles.", suffix=".tmp",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(body)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._path)
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning("bt_roles: write failed (%s)", e)
