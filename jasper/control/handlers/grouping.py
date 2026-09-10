# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for the grouping control-plane concern."""

from __future__ import annotations

import asyncio
import logging
import math
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ...atomic_io import locked_update_env_file
from ...env_load import GROUPING_ENV_FILE
from ...log_event import log_event
from ...multiroom.config import (
    BondMember,
    GroupingConfig,
    format_roster,
    load_config as load_grouping_config,
    validate_grouping,
    validate_roster,
)
from ...multiroom.runtime_balance import apply_local_trim as apply_live_grouping_trim
from ...multiroom.state import grouping_response, read_grouping_state
from .. import grouping_supervisor
from .. import household_credential
from .. import restart_broker
from .. import server as _server
from ._base import ControlHandlerMixin, logger

_GROUPING_RECONCILE_TRAILING_UNIT = "jasper-grouping-reconcile-trailing.service"
_GROUPING_RECONCILE_TRAILING_DELAY_FILE = (
    "/run/jasper-control/grouping-reconcile-trailing-delay"
)
_GROUPING_RECONCILE_KICK_MIN_INTERVAL_SECONDS = 60.0


def _launch_grouping_reconciler_kick(reason: str) -> None:
    log_event(
        logger,
        "grouping.reconciler_kick",
        reason=reason,
    )
    subprocess.Popen(
        [grouping_supervisor.RECONCILE_KICK_HELPER],
    )


def _cancel_grouping_reconciler_trailing_service() -> None:
    try:
        subprocess.Popen(
            [
                "systemctl",
                "stop",
                "--no-block",
                _GROUPING_RECONCILE_TRAILING_UNIT,
            ],
        )
    except OSError:
        logger.debug("grouping reconciler trailing service cancel failed", exc_info=True)


def _write_grouping_reconciler_trailing_delay(delay_s: float) -> None:
    delay_seconds = max(
        0,
        min(
            math.ceil(delay_s),
            math.ceil(_GROUPING_RECONCILE_KICK_MIN_INTERVAL_SECONDS),
        ),
    )
    path = Path(_GROUPING_RECONCILE_TRAILING_DELAY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{delay_seconds}\n", encoding="ascii")


def _arm_grouping_reconciler_trailing_service(delay_s: float) -> dict[str, Any]:
    """Arm the packaged trailing kick; returns the broker result.

    Bounded because the coalescer lock is held across this call: every worker
    reaching /grouping/set queues behind it.
    """
    _write_grouping_reconciler_trailing_delay(delay_s)
    return restart_broker.manage_units(
        _GROUPING_RECONCILE_TRAILING_UNIT,
        verb="restart",
        reason="grouping trailing kick",
        no_block=True,
        timeout=5.0,
    )


class _ThreadingTrailingKickHandle:
    def __init__(
        self,
        delay_s: float,
        callback: Callable[[], None],
        timer_factory: Callable[[float, Callable[[], None]], Any],
    ) -> None:
        self._timer = timer_factory(delay_s, callback)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self) -> None:
        self._timer.cancel()


class _SystemdServiceTrailingKickHandle:
    def __init__(
        self,
        delay_s: float,
        mark_applied: Callable[[], None],
        timer_factory: Callable[[float, Callable[[], None]], Any],
    ) -> None:
        mark_timer = timer_factory(delay_s, mark_applied)
        mark_timer.daemon = True
        mark_timer.start()
        self._mark_timer = mark_timer

    def cancel(self) -> None:
        self._mark_timer.cancel()
        _cancel_grouping_reconciler_trailing_service()


def _schedule_grouping_reconciler_trailing_kick(
    delay_s: float,
    run_trailing: Callable[[], None],
    mark_applied: Callable[[], None],
    *,
    timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
) -> _SystemdServiceTrailingKickHandle | _ThreadingTrailingKickHandle:
    error: str | None
    try:
        result = _arm_grouping_reconciler_trailing_service(delay_s)
    except OSError as exc:
        error = str(exc)
    else:
        error = (
            None
            if result.get("ok")
            else str(result.get("error") or f"rc={result.get('rc')}")
        )
    if error is not None:
        # The broker reports a failure without knowing whether PID 1 took the
        # restart anyway, so stop the unit before the in-process timer owns
        # the kick — otherwise both could fire.
        _cancel_grouping_reconciler_trailing_service()
        log_event(
            logger,
            "grouping.reconciler_trailing_schedule_fallback",
            delay_s=f"{delay_s:.3f}",
            scheduler="threading.Timer",
            error=error,
            level=logging.WARNING,
        )
        return _ThreadingTrailingKickHandle(delay_s, run_trailing, timer_factory)

    log_event(
        logger,
        "grouping.reconciler_trailing_scheduled",
        delay_s=f"{delay_s:.3f}",
        scheduler="systemd-service",
        unit=_GROUPING_RECONCILE_TRAILING_UNIT,
    )
    return _SystemdServiceTrailingKickHandle(delay_s, mark_applied, timer_factory)


class _GroupingReconcilerKickCoalescer:
    """Leading-edge rate limit with a trailing guarantee for /grouping/set.

    The HTTP handler writes grouping.env before calling this, and the oneshot
    reconciler re-reads grouping.env when it finally runs, so the last write
    wins without restarting outputd for every trim/delay sweep step.
    The packaged trailing service survives a jasper-control restart.
    """

    def __init__(
        self,
        *,
        cooldown_s: float,
        launch: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        trailing_scheduler: Callable[
            [float, Callable[[], None], Callable[[], None]],
            Any,
        ] = _schedule_grouping_reconciler_trailing_kick,
        cancel_external_trailing: Callable[
            [], None
        ] = _cancel_grouping_reconciler_trailing_service,
    ) -> None:
        self._cooldown_s = float(cooldown_s)
        self._launch = launch
        self._clock = clock
        self._trailing_scheduler = trailing_scheduler
        self._cancel_external_trailing = cancel_external_trailing
        self._lock = threading.Lock()
        self._last_kick_at: float | None = None
        self._trailing_handle: Any | None = None

    def reset_for_tests(self) -> None:
        with self._lock:
            if self._trailing_handle is not None:
                self._trailing_handle.cancel()
            self._trailing_handle = None
            self._last_kick_at = None

    def kick(self) -> None:
        """Kick now if the cooldown is clear, else arm one trailing kick."""
        reason: str | None = None
        launched_at: float | None = None
        with self._lock:
            now = self._clock()
            elapsed = (
                None if self._last_kick_at is None else now - self._last_kick_at
            )
            if elapsed is None or elapsed >= self._cooldown_s:
                if self._trailing_handle is not None:
                    self._trailing_handle.cancel()
                    self._trailing_handle = None
                else:
                    self._cancel_external_trailing()
                self._last_kick_at = now
                launched_at = now
                reason = "leading"
            else:
                remaining = max(0.0, self._cooldown_s - elapsed)
                if self._trailing_handle is None:
                    self._trailing_handle = self._trailing_scheduler(
                        remaining,
                        self._run_trailing,
                        self._mark_trailing_applied,
                    )
                    log_event(
                        logger,
                        "grouping.reconciler_kick_coalesced",
                        delay_s=f"{remaining:.3f}",
                        cooldown_s=f"{self._cooldown_s:.3f}",
                    )
                else:
                    log_event(
                        logger,
                        "grouping.reconciler_kick_already_pending",
                        cooldown_s=f"{self._cooldown_s:.3f}",
                        level=logging.DEBUG,
                    )
                return
        assert reason is not None
        try:
            self._launch(reason)
        except OSError:
            with self._lock:
                if (
                    launched_at is not None
                    and self._last_kick_at == launched_at
                    and self._trailing_handle is None
                ):
                    self._last_kick_at = None
            raise

    def _run_trailing(self) -> None:
        with self._lock:
            self._trailing_handle = None
            self._last_kick_at = self._clock()
        try:
            self._launch("trailing")
        except OSError:
            logger.exception("grouping reconciler trailing kick failed")

    def _mark_trailing_applied(self) -> None:
        with self._lock:
            self._trailing_handle = None
            self._last_kick_at = self._clock()


_grouping_reconciler_kick_coalescer = _GroupingReconcilerKickCoalescer(
    cooldown_s=_GROUPING_RECONCILE_KICK_MIN_INTERVAL_SECONDS,
    launch=_launch_grouping_reconciler_kick,
)


def _reset_grouping_reconciler_kick_coalescer_for_tests() -> None:
    _grouping_reconciler_kick_coalescer.reset_for_tests()


def _kick_grouping_reconciler() -> None:
    """Apply a persisted grouping change through jasper-grouping-reconcile.

    The reconciler is the single applier of snapcast state and outputd grouping
    env. A fixed helper performs a blocking ``systemctl start`` so an active
    Type=oneshot pass drains before one fresh pass launches. Rapid
    /grouping/set bursts coalesce, and a skipped kick always arms one trailing
    retry, so the final grouping.env write is always applied.
    """
    _grouping_reconciler_kick_coalescer.kick()


def _is_trim_only_grouping_change(before: GroupingConfig, after: GroupingConfig) -> bool:
    """True when the persisted grouping diff is only pair-balance trim."""
    return (
        before.enabled
        and after.enabled
        and before.error is None
        and after.error is None
        and before.role == after.role
        and before.channel == after.channel
        and before.bond_id == after.bond_id
        and before.leader_addr == after.leader_addr
        and before.buffer_ms == after.buffer_ms
        and before.codec == after.codec
        and before.client_latency_ms == after.client_latency_ms
        and math.isclose(before.left_delay_ms, after.left_delay_ms, abs_tol=0.0005)
        and math.isclose(before.right_delay_ms, after.right_delay_ms, abs_tol=0.0005)
        and before.peer_addr == after.peer_addr
        and before.peer_name == after.peer_name
        and before.roster == after.roster
        and not math.isclose(before.trim_db, after.trim_db, abs_tol=0.0005)
    )


@dataclass(frozen=True)
class _GroupingOptionalFields:
    trim_db: float | None
    client_latency_ms: int | None
    left_delay_ms: float | None
    right_delay_ms: float | None


def _parse_grouping_optional_fields(
    body: dict[str, Any],
) -> tuple[_GroupingOptionalFields | None, str | None]:
    """Parse optional ``/grouping/set`` scalars without HTTP side effects.

    Fields intentionally retain Python ``int``/``float`` coercion.
    """
    parsed: dict[str, Any] = {}
    for key, caster, error in (
        ("trim_db", float, "trim_db must be a number"),
        (
            "client_latency_ms",
            int,
            "client_latency_ms must be an integer",
        ),
        ("left_delay_ms", float, "left_delay_ms must be a number"),
        ("right_delay_ms", float, "right_delay_ms must be a number"),
    ):
        if key not in body:
            continue
        try:
            parsed[key] = caster(body[key])
        except (TypeError, ValueError):
            return None, error

    return _GroupingOptionalFields(
        trim_db=parsed.get("trim_db"),
        client_latency_ms=parsed.get("client_latency_ms"),
        left_delay_ms=parsed.get("left_delay_ms"),
        right_delay_ms=parsed.get("right_delay_ms"),
    ), None


def _write_grouping(
    *, enabled: bool, role: str, channel: str, bond_id: str, leader_addr: str,
    trim_db: "float | None" = None,
    client_latency_ms: "int | None" = None,
    left_delay_ms: "float | None" = None,
    right_delay_ms: "float | None" = None,
    peer_addr: "str | None" = None,
    peer_name: "str | None" = None,
    roster: "str | None" = None,
) -> None:
    """Persist a grouping role into the wizard-owned grouping.env.

    Read-modify-write (via locked_update_env_file) so operator-tuned
    JASPER_GROUPING_BUFFER_MS / _CODEC survive a role change. This is the
    single control-plane WRITER of grouping.env; jasper-grouping-reconcile is
    the single READER->action. The endpoint that calls this (/grouping/set) is
    token-gated; the cross-device bond-forming flow — one speaker POSTing to
    another's :PORT/grouping/set — authenticates with the household
    credential.
    """
    updates = {
        "JASPER_GROUPING": "on" if enabled else "off",
        "JASPER_GROUPING_ROLE": role,
        "JASPER_GROUPING_CHANNEL": channel,
        "JASPER_GROUPING_BOND_ID": bond_id,
        "JASPER_GROUPING_LEADER_ADDR": leader_addr,
    }
    if trim_db is not None:
        # Settable like the role fields, preserved like codec when the
        # caller omits it. Existing-bond structural edits omit trim so a
        # calibrated balance survives role/channel changes; fresh bond and
        # unbond flows send trim=0 to clear stale balance state.
        updates["JASPER_GROUPING_TRIM_DB"] = f"{trim_db:.1f}"
    if client_latency_ms is not None:
        updates["JASPER_GROUPING_CLIENT_LATENCY_MS"] = str(int(client_latency_ms))
    if left_delay_ms is not None:
        updates["JASPER_GROUPING_LEFT_DELAY_MS"] = f"{left_delay_ms:.3f}"
    if right_delay_ms is not None:
        updates["JASPER_GROUPING_RIGHT_DELAY_MS"] = f"{right_delay_ms:.3f}"
    # Peer and roster (leader only): same preserved-when-omitted contract as
    # trim, and an EXPLICIT empty string clears — the bond flow clears both on
    # non-leader members so a role flip can't leave a stale roster behind.
    if peer_addr is not None:
        updates["JASPER_GROUPING_PEER_ADDR"] = peer_addr
    if peer_name is not None:
        updates["JASPER_GROUPING_PEER_NAME"] = peer_name
    # `roster` is the already SERIALIZED env string (callers build it via
    # config.format_roster).
    if roster is not None:
        updates["JASPER_GROUPING_ROSTER"] = roster
    locked_update_env_file(
        GROUPING_ENV_FILE, updates, mode=0o644, owner="JTS /rooms grouping control",
    )


class GroupingRoutes(ControlHandlerMixin):
    def _get_grouping(self) -> None:
        # Multiroom grouping block + the small member-local readiness
        # verdict used before a bond writes any member. Both are nested
        # under stable keys so either read can fail soft to null without
        # becoming indistinguishable from a real disabled/blocked value.
        # Read SERVER-SIDE by another speaker's /rooms /unbond
        # fan-out (rooms_peers._get_member_grouping) to discover which
        # siblings share a bond_id; /rooms bond preflight reads readiness
        # from this SAME lightweight endpoint instead of downloading the
        # catch-all /state aggregate. The browser's landing-page
        # stereo-pair banner also polls it every 10 s through nginx's
        # exact-match /grouping proxy.
        # NO CSRF: a read on the same no-auth LAN surface as /state
        # and /healthz. Each block fails soft independently; the response
        # remains 200 so one broken read does not hide the other.
        try:
            grouping = read_grouping_state()
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
            grouping_response(
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
        optional_fields, parse_error = _parse_grouping_optional_fields(body)
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
        roster_members: tuple[BondMember, ...] = ()
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
                BondMember(
                    addr=str((m or {}).get("addr") or ""),
                    name=str((m or {}).get("name") or ""),
                    channel=str((m or {}).get("channel") or ""),
                )
                for m in raw_roster
                if isinstance(m, dict)
            )
            roster_str = format_roster(roster_members)
            # Validate the roster whenever it is present — INCLUDING a
            # disabled request, which skips validate_grouping below. The
            # persisted roster is the _unbond disable list, so a member with
            # an injected foreign addr or a malformed channel must never land
            # on disk (it would become an unbond disable target / orphan).
            # The enabled path re-checks via validate_grouping (idempotent).
            roster_err = validate_roster(roster_members)
            if roster_err:
                self._send_json({"error": roster_err}, status=400)
                return
        # Validate an ENABLED request up front via the SHARED
        # validate_grouping (same rule the config loader applies on
        # read) so we never persist a fail-loud config. A disabled
        # request needs no fields.
        if enabled:
            err = validate_grouping(
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
        before_grouping = load_grouping_config(GROUPING_ENV_FILE)
        live_apply_payload: dict[str, Any] | None = None
        reconciler_kicked = False
        try:
            _write_grouping(
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
            after_grouping = load_grouping_config(GROUPING_ENV_FILE)
            if enabled and trim_db is not None and before_grouping == after_grouping:
                live_apply_payload = {
                    "applied": True,
                    "mode": "noop",
                    "trim_db": round(float(after_grouping.trim_db), 1),
                }
            elif trim_db is not None and _is_trim_only_grouping_change(
                before_grouping, after_grouping
            ):
                live_apply = asyncio.run(
                    apply_live_grouping_trim(
                        after_grouping.trim_db,
                        cfg=after_grouping,
                    )
                )
                live_apply_payload = live_apply.to_dict()
                if not live_apply.applied:
                    _kick_grouping_reconciler()
                    reconciler_kicked = True
            else:
                _kick_grouping_reconciler()
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
            if household_credential.adopt(self.headers.get("X-JTS-Household")):
                log_event(
                    logger,
                    "household_credential.adopted",
                    bond=bond_id or "(none)",
                )
        elif household_credential.is_paired():
            household_credential.clear()
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
