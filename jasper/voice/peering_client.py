# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's client for jasper-control's multi-device peering UDS.

Asks whether this Pi should take a wake (`arbitrate`), and tells the
peering daemon when a session opens/closes so peers stay suppressed for
its duration. Fail-open on every path — see ADR-0128.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("jasper.voice_daemon")


class PeeringClient:
    def __init__(self, *, enabled: bool, socket_path: str) -> None:
        self._enabled = enabled
        self._socket_path = socket_path
        # Empty string means "no peer-tracked session": peering
        # disabled, or a remote-driven session that skipped arbitration.
        self._epoch: str = ""

    async def _send(
        self, cmd: str, *, timeout: float = 0.5,
    ) -> dict | None:
        """Send one command to jasper-control's peering UDS.

        Returns the parsed JSON response, or None if peering is
        disabled / the daemon is unreachable / any error occurs.

        This is the only place that touches the peering UDS — every
        caller is fail-open by construction (no exception escapes,
        no peering issue can silence the speaker). Callers
        differentiate "WIN-by-default" semantics by treating None
        as the no-op response."""
        if not self._enabled:
            return None
        try:
            from ..peering.uds import send_request
        except ImportError:
            # peering package not installed — keep wake working.
            return None
        try:
            return await send_request(
                self._socket_path, cmd, timeout=timeout,
            )
        except FileNotFoundError:
            # Peering daemon isn't running (mode=on in voice config
            # but mode=off / failed in jasper-control). Fall back to
            # solo behavior silently — this isn't an error condition.
            return None
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("peering %s failed: %s; treating as solo",
                           cmd.split(maxsplit=1)[0], e)
            return None
        except Exception:  # noqa: BLE001
            logger.exception(
                "peering %s raised; treating as solo",
                cmd.split(maxsplit=1)[0],
            )
            return None

    async def arbitrate(
        self,
        *,
        score: float,
        snr_db: float | None,
        rms_dbfs: float | None,
        can_serve: bool,
    ) -> str:
        """Ask jasper-control's peering daemon whether this Pi should
        take the turn. Returns "WIN" or "LOSE".

        Side effect: sets `self._epoch` from the
        daemon's response so `session_*` can reference
        the same arbitration round.

        Fast-path: when peering is disabled OR no peering daemon is
        running OR the UDS errors, returns "WIN" immediately, and
        `_send` short-circuits before any I/O.
        """
        self._epoch = ""
        import json as _json  # noqa: PLC0415
        payload = _json.dumps({
            "score": float(score),
            "snr_db": snr_db,
            "rms_dbfs": rms_dbfs,
            "can_serve": bool(can_serve),
        })
        resp = await self._send(f"ARBITRATE {payload}")
        if resp is None:
            return "WIN"  # peering disabled or daemon unreachable
        self._epoch = str(resp.get("epoch") or "")
        result = (resp.get("result") or "").upper()
        if result not in ("WIN", "LOSE"):
            logger.warning(
                "peer arbitrate returned %r; defaulting to WIN", result,
            )
            return "WIN"
        return result

    async def session_started(self, has_turn: bool) -> None:
        """Fire-and-forget notice that this speaker opened a session.

        The peering daemon transitions WINNER → ACTIVE and broadcasts
        heartbeats so peers stay suppressed for the session. No-op when
        peering is disabled; errors are swallowed so voice keeps going.
        """
        if not has_turn:
            return  # no active turn to announce
        await self._send(
            f"SESSION_STARTED {self._epoch}",
        )

    async def session_ended(self, reason: str) -> None:
        """Fire-and-forget notice. Mirrors session_started."""
        await self._send(
            f"SESSION_ENDED {self._epoch} {reason}",
        )
        self._epoch = ""
