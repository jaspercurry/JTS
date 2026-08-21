# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for the measurement-hold concern.

Two routes, deliberately shaped like jasper-mux's ``TEST_SELECT`` /
``TEST_RELEASE`` pair rather than as a REST resource: acquire doubles as
renew for the incumbent, so the owning window's refresh loop re-issues one
command instead of needing a separate verb.

The registrar itself — TTL, expiry, owner scoping — is
:mod:`jasper.control.measurement_hold`. These handlers own only the wire
shape and the status codes.
"""

from __future__ import annotations

from .. import measurement_hold
from ._base import ControlHandlerMixin


class MeasurementRoutes(ControlHandlerMixin):
    def _get_measurement(self) -> None:
        # The same projection /state.measurement carries, on its own cheap
        # route. Operator doors (jasper-seat-level, jasper-angle-capture stage)
        # ask "is a measurement live?" before they act, and /state is the heavy
        # aggregate — camilla, renderers, mux, a 20 s budget. This is one
        # in-memory dict. Ungated: it is a read, and it reveals only what
        # /state already does.
        self._send_json({"measurement": measurement_hold.snapshot()})

    def _post_measurement_hold(self) -> None:
        # POST /measurement/hold body: {"owner": "<name>", "mode": "gate"}.
        # Idempotent for the incumbent (that is the renewal path); a DIFFERENT
        # owner gets 409 naming the incumbent, which is the cross-process
        # mutex jasper-control exists to provide here.
        body = self._read_json()
        owner = str(body.get("owner") or "").strip()
        if not owner:
            self._send_json({"error": "missing owner"}, status=400)
            return
        mode = str(body.get("mode") or "gate").strip()
        try:
            state = measurement_hold.acquire(owner, mode)
        except measurement_hold.MeasurementHoldConflict as e:
            self._send_json(
                {
                    "error": str(e),
                    "owner": e.owner,
                    "measurement": measurement_hold.snapshot(),
                },
                status=409,
            )
            return
        except measurement_hold.MeasurementHoldModeError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        self._send_json({"measurement": state})

    def _post_measurement_release(self) -> None:
        # POST /measurement/release body: {"owner": "<name>"}. Owner-scoped:
        # releasing a hold somebody else owns is a 409, never a silent
        # un-gate of their live capture.
        body = self._read_json()
        owner = str(body.get("owner") or "").strip()
        if not owner:
            self._send_json({"error": "missing owner"}, status=400)
            return
        try:
            state = measurement_hold.release(owner)
        except measurement_hold.MeasurementHoldConflict as e:
            self._send_json(
                {
                    "error": str(e),
                    "owner": e.owner,
                    "measurement": measurement_hold.snapshot(),
                },
                status=409,
            )
            return
        self._send_json({"measurement": state})
