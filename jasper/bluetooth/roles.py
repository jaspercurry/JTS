"""Minimal persistence — `{mac: handler_id}` at /var/lib/jasper/bt_roles.json.

BlueZ owns the pair database (link keys, alias, trust flag). The one
thing it doesn't track is which JTS-side handler should pick up a
device when it re-connects on its own (e.g., the rotary knob coming
back into range and showing up at `/dev/input/event*` — is it a HID
input we route to jasper-control, or a BT speaker source we let
bluez-alsa pick up?). That mapping lives here.

Atomic, group-readable write via :mod:`jasper.atomic_io`. Tiny — never
larger than a few KB.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from jasper.atomic_io import atomic_write_text

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
        body = json.dumps(data, indent=2, sort_keys=True)
        # 0640 group jasper: bt_roles.json lives in /var/lib/jasper, the shared
        # group-readable state tree under the WS1 non-root drop. Publish it
        # group-readable (NOT the hand-rolled NamedTemporaryFile/mkstemp default
        # 0600) so any non-root daemon in the jasper group can read it, matching
        # the rest of /var/lib/jasper. atomic_write_text creates the parent dir +
        # writes the right mode in one call; keep the write best-effort (a
        # role-map write must not crash the bluetooth handler).
        try:
            atomic_write_text(self._path, body, mode=0o640)
        except OSError as e:
            logger.warning("bt_roles: write failed (%s)", e)
