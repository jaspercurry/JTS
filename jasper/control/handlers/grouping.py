# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for the grouping control-plane concern."""

from __future__ import annotations

import asyncio
from typing import Any

from ...log_event import log_event
from .. import server as _server
from ._base import ControlHandlerMixin, logger


class GroupingRoutes(ControlHandlerMixin):
    def _get_grouping(self) -> None:
        # Multiroom grouping block + the small member-local readiness
        # verdict used before a bond writes any member. Both are nested
        # under stable keys so either read can fail soft to null without
        # becoming indistinguishable from a real disabled/blocked value.
        # Read SERVER-SIDE by another speaker's /rooms /unbond
        # fan-out (rooms_setup._get_member_grouping) to discover which
        # siblings share a bond_id; /rooms bond preflight reads readiness
        # from this SAME lightweight endpoint instead of downloading the
        # catch-all /state aggregate. The browser's landing-page
        # stereo-pair banner also polls it every 10 s through nginx's
        # exact-match /grouping proxy.
        # NO CSRF: a read on the same no-auth LAN surface as /state
        # and /healthz. Each block fails soft independently; the response
        # remains 200 so one broken read does not hide the other.
        try:
            grouping = _server.read_grouping_state()
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            logger.exception("grouping state read failed")
            grouping = None
        try:
            readiness, _blocked = _server._active_speaker_grouping_evaluation()
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            logger.exception("grouping readiness read failed")
            readiness = None
        # grouping_response is the ONE home for the envelope shape; the
        # /rooms consumers parse it via the paired parse functions in
        # jasper/multiroom/state.py, so producer and consumers cannot
        # drift (the C4 regression).
        self._send_json(
            _server.grouping_response(
                grouping,
                readiness=readiness,
            )
        )

    def _post_grouping_set(self) -> None:
        # Set this speaker's grouping role. /grouping/set is token-gated
        # (_TOKEN_GATED_ROUTES); the cross-device bond-forming UI on speaker A
        # configures speaker B by POSTing here on B's port, authenticated by the
        # household credential (Phase C). The reconciler (kicked below) is the single
        # applier of the snapcast units + the outputd tap.
        body = self._read_json()
        enabled = bool(body.get("enabled"))
        role = str(body.get("role", "")).strip()
        channel = str(body.get("channel", "")).strip()
        bond_id = str(body.get("bond_id", "")).strip()
        leader_addr = str(body.get("leader_addr", "")).strip()
        optional_fields, parse_error = _server._parse_grouping_optional_fields(body)
        if parse_error is not None:
            self._send_json({"error": parse_error}, status=400)
            return
        assert optional_fields is not None
        trim_db = optional_fields.trim_db
        client_latency_ms = optional_fields.client_latency_ms
        left_delay_ms = optional_fields.left_delay_ms
        right_delay_ms = optional_fields.right_delay_ms
        peer_addr: str | None = None
        if "peer_addr" in body:
            peer_addr = str(body.get("peer_addr") or "").strip()
        peer_name: str | None = None
        if "peer_name" in body:
            peer_name = str(body.get("peer_name") or "").strip()
        # Full bond roster (leader only): a list of {addr,name,channel}.
        # Build a BondMember tuple (for the shared validator) and the
        # serialized env string (for the writer). Omitted -> preserve;
        # an explicit [] serializes to "" which clears it (same contract
        # as peer_addr/peer_name).
        roster_members: tuple[_server.BondMember, ...] = ()
        roster_str: str | None = None
        if "roster" in body:
            raw_roster = body.get("roster")
            if not isinstance(raw_roster, list):
                self._send_json(
                    {"error": "roster must be a list"},
                    status=400,
                )
                return
            roster_members = tuple(
                _server.BondMember(
                    addr=str((m or {}).get("addr") or ""),
                    name=str((m or {}).get("name") or ""),
                    channel=str((m or {}).get("channel") or ""),
                )
                for m in raw_roster
                if isinstance(m, dict)
            )
            roster_str = _server.format_roster(roster_members)
            # Validate the roster whenever it is present — INCLUDING a
            # disabled request, which skips validate_grouping below. The
            # persisted roster is the _unbond disable list, so a member with
            # an injected foreign addr or a malformed channel must never land
            # on disk (it would become an unbond disable target / orphan).
            # The enabled path re-checks via validate_grouping (idempotent).
            roster_err = _server.validate_roster(roster_members)
            if roster_err:
                self._send_json({"error": roster_err}, status=400)
                return
        # Validate an ENABLED request up front via the SHARED
        # validate_grouping (same rule the config loader applies on
        # read) so we never persist a fail-loud config. A disabled
        # request needs no fields.
        if enabled:
            err = _server.validate_grouping(
                role=role,
                channel=channel,
                bond_id=bond_id,
                leader_addr=leader_addr,
                trim_db=trim_db if trim_db is not None else 0.0,
                client_latency_ms=(
                    client_latency_ms if client_latency_ms is not None else 0
                ),
                left_delay_ms=left_delay_ms if left_delay_ms is not None else 0.0,
                right_delay_ms=(right_delay_ms if right_delay_ms is not None else 0.0),
                peer_addr=peer_addr or "",
                peer_name=peer_name or "",
                roster=roster_members,
            )
            if err:
                self._send_json({"error": err}, status=400)
                return
            blocked = (
                _server._active_speaker_grouping_block()
                if body.get("enabled")
                else None
            )
            if blocked is not None:
                self._send_json(
                    {
                        "error": (
                            blocked.get("detail")
                            or "active speaker setup is not ready for grouping"
                        ),
                        "active_speaker_setup": blocked,
                    },
                    status=409,
                )
                return
        before_grouping = _server.load_grouping_config(_server.GROUPING_ENV_FILE)
        live_apply_payload: dict[str, Any] | None = None
        reconciler_kicked = False
        try:
            _server._write_grouping(
                enabled=enabled,
                role=role,
                channel=channel,
                bond_id=bond_id,
                leader_addr=leader_addr,
                trim_db=trim_db,
                client_latency_ms=client_latency_ms,
                left_delay_ms=left_delay_ms,
                right_delay_ms=right_delay_ms,
                peer_addr=peer_addr,
                peer_name=peer_name,
                roster=roster_str,
            )
            after_grouping = _server.load_grouping_config(_server.GROUPING_ENV_FILE)
            if enabled and trim_db is not None and before_grouping == after_grouping:
                live_apply_payload = {
                    "applied": True,
                    "mode": "noop",
                    "trim_db": round(float(after_grouping.trim_db), 1),
                }
            elif trim_db is not None and _server._is_trim_only_grouping_change(
                before_grouping, after_grouping
            ):
                live_apply = asyncio.run(
                    _server.apply_live_grouping_trim(
                        after_grouping.trim_db,
                        cfg=after_grouping,
                    )
                )
                live_apply_payload = live_apply.to_dict()
                if not live_apply.applied:
                    _server._kick_grouping_reconciler()
                    reconciler_kicked = True
            else:
                _server._kick_grouping_reconciler()
                reconciler_kicked = True
        except Exception as e:  # noqa: BLE001
            logger.exception("grouping set failed")
            self._send_json({"error": str(e)}, status=502)
            return
        # Persist / drop the household credential as the bond forms or
        # dissolves (control-plane-auth §6). A bond fan-out (enabled) carries
        # the leader's X-JTS-Household; an unpaired member adopts it
        # (trust-on-first-use over the trusted LAN) so every subsequent
        # cross-device /grouping/set verifies against it. An unbond
        # (disabled) clears it so the speaker can later re-pair. The leader
        # reads its secret ONCE before the unbond fan-out, so this clear
        # can't race the concurrent peer POSTs out of their credential. The
        # secret value is never logged — only the transition.
        if enabled:
            if _server.household_credential.adopt(self.headers.get("X-JTS-Household")):
                log_event(
                    logger,
                    "household_credential.adopted",
                    bond=bond_id or "(none)",
                )
        elif _server.household_credential.is_paired():
            _server.household_credential.clear()
            log_event(logger, "household_credential.cleared")
        log_event(
            logger,
            "grouping.set",
            enabled=enabled,
            role=role or "(none)",
            channel=channel or "(none)",
            bond=bond_id or "(none)",
            live_applied=(
                None
                if live_apply_payload is None
                else live_apply_payload.get("applied")
            ),
            reconciler_kicked=reconciler_kicked,
            client=self.address_string(),
        )
        response = {
            "ok": True,
            "enabled": enabled,
            "role": role,
            "channel": channel,
            "bond_id": bond_id,
            "leader_addr": leader_addr,
            "reconciler_kicked": reconciler_kicked,
        }
        if live_apply_payload is not None:
            response["live_apply"] = live_apply_payload
        self._send_json(response)
        return
