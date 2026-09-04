# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-run evidence cache for jasper-doctor (ADR-0232 rule 4).

Each evidence source is read once per run; checks are functions over the
cache. A reader here owns only the read and its fail-soft shape; the verdict
stays in the check. ``run_async`` resets the cache at the start of every
run, and a test fixture resets it between tests.

Test seams: patch this module's reader functions (``_run``,
``read_status_socket``, ``read_unit_states``) or ``evidence.seed(key, value)``
a memo entry directly; the keys are the ones the public methods build.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from ...route_latency.status_socket import (
    FANIN_STATUS_SOCKET,
    OUTPUTD_STATUS_SOCKET,
    read_status_socket,
)
from ...service_units import (
    DOCTOR_UNIT_ROSTER,
    parse_property_blocks,
    read_unit_states,
)
from ._shared import _run

T = TypeVar("T")

STATUS_TIMEOUT_SECONDS = 2.0
STATUS_RETRY_DELAY_SECONDS = 0.1
UNIT_SHOW_TIMEOUT_SECONDS = 10.0
_LOOPBACK_STATUS_GLOB = "/proc/asound/Loopback/pcm0p/sub*/status"


@dataclass(frozen=True)
class StatusRead:
    """One daemon's STATUS reply, or why it could not be read."""

    payload: dict[str, Any] | None
    error: BaseException | None = None

    @property
    def unreachable(self) -> bool:
        return isinstance(self.error, (OSError, TimeoutError))


def _read_status(path: str, timeout: float) -> StatusRead:
    """One STATUS read, retried once after ``STATUS_RETRY_DELAY_SECONDS`` when
    the socket is absent or refusing — a daemon restarting mid-run answers on
    the second attempt, and the memo hands that result to every consumer."""
    try:
        return StatusRead(read_status_socket(path, timeout=timeout))
    except (ConnectionRefusedError, FileNotFoundError):
        time.sleep(STATUS_RETRY_DELAY_SECONDS)
    except Exception as exc:  # noqa: BLE001 — classified by the caller
        return StatusRead(None, exc)
    try:
        return StatusRead(read_status_socket(path, timeout=timeout))
    except Exception as exc:  # noqa: BLE001 — classified by the caller
        return StatusRead(None, exc)


def _systemctl_show_property(prop: str, units: list[str]) -> list[str] | None:
    """One value of ``prop`` per unit, in input order; None when systemctl is
    unavailable (dev host) or the reply is not one block per unit.

    One subprocess per property rather than per unit: unbatched, a property
    outside ``SHOW_PROPERTIES`` would cost N invocations, a large
    constant-factor loss on the Pi.
    """
    try:
        out = _run(
            ["systemctl", "show", "--no-page", f"--property={prop}", *units],
            timeout=UNIT_SHOW_TIMEOUT_SECONDS,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    values = parse_property_blocks(out, prop)
    if len(values) != len(units):
        return None
    return values


def _loopback_substreams() -> dict[int, str]:
    """Full status text per snd-aloop playback substream index, keyed by
    substream number; a closed substream reads ``closed``.

    Full text (not just the first ``state:``/``closed`` line) so a caller
    that needs the second ``owner_pid`` line — the aloop ownership gate in
    ``renderers._fanin_lane_busy_owner_matches`` — can read it from the same
    cached fetch; a caller that only wants open/closed reads
    ``text.splitlines()[0]`` off the same value."""
    import glob
    import re

    out: dict[int, str] = {}
    for status_path in glob.glob(_LOOPBACK_STATUS_GLOB):
        m = re.search(r"/sub(\d+)/status$", status_path)
        if not m:
            continue
        try:
            with open(status_path, encoding="utf-8") as f:
                out[int(m.group(1))] = f.read()
        except OSError:
            continue
    return out


class Evidence:
    def __init__(self) -> None:
        self._memo: dict[str, Any] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def reset(self) -> None:
        with self._guard:
            self._memo.clear()
            self._locks.clear()

    def seed(self, key: str, value: Any) -> None:
        with self._guard:
            self._memo[key] = value

    def get(self, key: str, read: Callable[[], T]) -> T:
        """The memoized value for ``key``, reading it once on first use. Checks
        that run concurrently on one key wait for the single read."""
        with self._guard:
            if key in self._memo:
                return self._memo[key]
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                if key in self._memo:
                    return self._memo[key]
            value = read()
            with self._guard:
                self._memo[key] = value
            return value

    # -- daemon STATUS sockets ------------------------------------------

    def daemon_status(
        self, path: str, *, timeout: float = STATUS_TIMEOUT_SECONDS,
    ) -> StatusRead:
        return self.get(f"status:{path}", lambda: _read_status(path, timeout))

    def fanin_status(self) -> StatusRead:
        return self.daemon_status(FANIN_STATUS_SOCKET)

    def outputd_status(self) -> StatusRead:
        return self.daemon_status(OUTPUTD_STATUS_SOCKET)

    # -- systemd -----------------------------------------------------------

    def unit_states(self) -> dict[str, dict[str, Any]] | None:
        """Every rostered unit's state from one ``systemctl show``; None when
        systemctl is unavailable on this host."""
        return self.get(
            "units",
            lambda: read_unit_states(
                DOCTOR_UNIT_ROSTER, timeout=UNIT_SHOW_TIMEOUT_SECONDS,
            ),
        )

    def unit_state(self, unit: str) -> dict[str, Any] | None:
        """One unit's state (see ``service_units.parse_systemctl_show_units``
        for the keys); a unit off the roster costs one extra read.

        None means UNKNOWN, never "not installed": systemctl unavailable, or a
        reply that carries no record under this name (an alias whose canonical
        ``Id`` differs). A unit systemd genuinely does not know answers with a
        record of its own carrying ``load_state == "not-found"``, so callers
        can tell the two apart."""
        states = self.unit_states()
        if states is None:
            return None
        if unit in states:
            return states[unit]
        extra = self.get(
            f"unit:{unit}",
            lambda: read_unit_states((unit,), timeout=UNIT_SHOW_TIMEOUT_SECONDS),
        )
        if extra is None:
            return None
        return extra.get(unit)

    def unit_active(self, unit: str) -> bool | None:
        """True/False from ActiveState; None when systemctl is unavailable."""
        state = self.unit_state(unit)
        if state is None:
            return None
        return state.get("active_state") == "active"

    def unit_property(self, prop: str, units: tuple[str, ...]) -> list[str] | None:
        """A property outside SHOW_PROPERTIES (OOMScoreAdjust, StartLimitAction,
        User, ...) for several full unit names in one call, in input order;
        None when systemctl is unavailable or the reply shape is not one value
        per unit."""
        return self.get(
            f"prop:{prop}:{','.join(units)}",
            lambda: _systemctl_show_property(prop, list(units)),
        )

    # -- files and readers shared by several checks ----------------------

    def loopback_substreams(self) -> dict[int, str]:
        return self.get("loopback_substreams", _loopback_substreams)

    def parked_bonded_follower(self) -> bool:
        from ._shared import _parked_as_bonded_follower

        return self.get("parked_bonded_follower", _parked_as_bonded_follower)

    def camilla_config_path(self) -> str | None:
        """The config path CamillaDSP's statefile names, read once; None when
        the statefile is unreadable or names nothing."""
        from .correction import _active_camilla_config_path

        _statefile, path = self.get("camilla_config", _active_camilla_config_path)
        return path

    def camilla_config_text(self) -> str | None:
        """The loaded CamillaDSP config's text, read once; None when there is
        no config path or it cannot be read."""

        def read() -> str | None:
            path = self.camilla_config_path()
            if not path:
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return None

        return self.get("camilla_config_text", read)

    def output_hardware_state(self) -> Any:
        from ...output_hardware import load_state

        return self.get("output_hardware_state", load_state)

    def output_topology(self) -> Any:
        from ...output_topology import load_output_topology

        return self.get("output_topology", load_output_topology)

    def mic_presence(self) -> Any:
        from ...mic_presence import read_mic_presence

        return self.get("mic_presence", read_mic_presence)

    def active_speaker_setup_status(self) -> Any:
        from ...active_speaker.setup_status import read_active_speaker_setup_status

        return self.get(
            "active_speaker_setup_status", read_active_speaker_setup_status,
        )

    def control_state(self) -> StatusRead:
        """jasper-control's /state, fetched once per run."""

        def read() -> StatusRead:
            from ...control.client import get_state

            try:
                return StatusRead(get_state())
            except Exception as exc:  # noqa: BLE001
                return StatusRead(None, exc)

        return self.get("control_state", read)

    def control_system_snapshot(self) -> StatusRead:
        """jasper-control's /system/snapshot, fetched once per run."""

        def read() -> StatusRead:
            from ...control.client import get_system_snapshot

            try:
                return StatusRead(get_system_snapshot())
            except Exception as exc:  # noqa: BLE001
                return StatusRead(None, exc)

        return self.get("control_system_snapshot", read)


evidence = Evidence()

__all__ = ["Evidence", "StatusRead", "evidence"]
