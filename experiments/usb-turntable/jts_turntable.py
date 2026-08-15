#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Manual JTS adapter for the bundled ``usb_turntable`` controller."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar


POWER_FLAG_NAMES = (
    "under_voltage",
    "frequency_capped",
    "throttled",
    "soft_temperature_limit",
)
MEASUREMENT_MIN_DEGREES = -45.0
MEASUREMENT_MAX_DEGREES = 45.0
AUTOSTOP_ATTEMPTS = 4
AUTOSTOP_RETRY_SECONDS = 1.5
AUTOSTOP_PRODUCT = "MT320RUBL40ProV3"
AUTOSTOP_IO_TIMEOUT = 1.5
THROTTLED_RE = re.compile(r"\s*throttled=(0x[0-9a-fA-F]+)\s*")
AUTOSTOP_PORT_RE = re.compile(r"/dev/ttyUSB\d+")
EXPERIMENT_ROOT = Path(__file__).resolve().parent
VENDOR_ROOT = EXPERIMENT_ROOT / "vendor"

# Commands that get one whole-operation retry, against a FRESH controller
# session, on the vendored transport's exact ProtocolError base class
# (issue #2516: the vendor parser occasionally raises "heartbeat byte
# appeared inside a protocol frame" when a periodic heartbeat byte lands
# mid-exchange -- a transport-layer parse race, not a real command
# failure). `run()` below is the ONLY place this set is consulted -- it is
# the literal branch condition that routes a command through
# `_run_with_session_retry` versus the plain single-open path, not a
# decorative label, so adding a command here without also handling it in
# that branch fails loudly (`_run_with_session_retry` -- see its docstring
# for why a fresh session, not an in-session re-call, and why only the
# EXACT base class retries, never a subclass).
#
# `offset`/`probe` send no motion; the guarded `position` always homes
# first and re-derives its move from the controller's own state, so a
# retried invocation cannot double-move. `detect` is excluded: discovery
# never touches the serial link, so it can't raise this error at all.
# `set-zero` (owner-only, destructive) and the unguarded `left`/`right`/
# `stop`/`home` motion commands are deliberately excluded and stay
# zero-retry.
RETRYABLE_COMMANDS = frozenset({"offset", "probe", "position"})

_T = TypeVar("_T")


@dataclass(frozen=True)
class PowerStatus:
    available: bool
    safe_for_motion: bool
    raw: str | None
    current_flags: tuple[str, ...]
    history_flags: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class TurntableApi:
    discover_devices: Callable[[], list[dict[str, str | None]]]
    controller: Any
    protocol_error: type[BaseException]


def _flag_names(bits: int) -> tuple[str, ...]:
    return tuple(
        name for bit, name in enumerate(POWER_FLAG_NAMES) if bits & (1 << bit)
    )


def read_power_status(
    run_command: Callable[..., Any] = subprocess.run,
) -> PowerStatus:
    """Read the bounded Pi power preflight, failing closed when unknown."""

    try:
        result = run_command(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return PowerStatus(
            False,
            False,
            None,
            (),
            (),
            f"power status unavailable: {type(exc).__name__}: {exc}",
        )

    stdout = str(getattr(result, "stdout", ""))
    returncode = int(getattr(result, "returncode", 0))
    if returncode != 0:
        stderr = str(getattr(result, "stderr", "")).strip()
        suffix = f": {stderr}" if stderr else ""
        return PowerStatus(
            False,
            False,
            None,
            (),
            (),
            f"vcgencmd exited {returncode}{suffix}",
        )

    match = THROTTLED_RE.fullmatch(stdout)
    if match is None:
        return PowerStatus(
            False,
            False,
            None,
            (),
            (),
            f"unexpected vcgencmd output: {stdout.strip()!r}",
        )

    raw = match.group(1).lower()
    bits = int(raw, 16)
    current_flags = _flag_names(bits & 0xF)
    history_flags = _flag_names((bits >> 16) & 0xF)
    if current_flags:
        detail = "current power/throttle flags: " + ", ".join(current_flags)
    elif history_flags:
        detail = "power is currently healthy; since-boot flags: " + ", ".join(
            history_flags
        )
    else:
        detail = "power is healthy"
    return PowerStatus(
        True,
        not current_flags,
        raw,
        current_flags,
        history_flags,
        detail,
    )


def load_upstream_api() -> TurntableApi:
    """Load the bundled package in this fresh manual CLI process."""

    vendor_path = str(VENDOR_ROOT)
    if not sys.path or sys.path[0] != vendor_path:
        sys.path.insert(0, vendor_path)

    from usb_turntable import ProtocolError, TurntableController, discover_devices

    module_root = Path(sys.modules["usb_turntable"].__file__ or "").resolve().parent
    expected_root = (VENDOR_ROOT / "usb_turntable").resolve()
    if module_root != expected_root:
        raise RuntimeError(f"usb_turntable did not load from the bundle: {module_root}")
    return TurntableApi(discover_devices, TurntableController, ProtocolError)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _emit(payload: Mapping[str, Any], *, compact: bool) -> None:
    print(json.dumps(_jsonable(payload), sort_keys=True, indent=None if compact else 2))


def _positive_degrees(value: str) -> float:
    try:
        degrees = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("degrees must be a number") from exc
    if not math.isfinite(degrees) or degrees <= 0:
        raise argparse.ArgumentTypeError("degrees must be a finite number above zero")
    return degrees


def _measurement_position(value: str) -> float:
    try:
        degrees = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("position must be a number") from exc
    if not math.isfinite(degrees):
        raise argparse.ArgumentTypeError("position must be finite")
    if not MEASUREMENT_MIN_DEGREES <= degrees <= MEASUREMENT_MAX_DEGREES:
        raise argparse.ArgumentTypeError(
            "position must be between -45 and +45 degrees"
        )
    return degrees


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual JTS USB turntable experiment")
    parser.add_argument("--port", help="serial path; default uses bundled discovery")
    parser.add_argument(
        "--allow-power-risk",
        action="store_true",
        help="allow motion despite current or unreadable Pi power status",
    )
    parser.add_argument("--json", action="store_true", help="emit compact JSON")

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("detect", help="list matching USB serial devices")
    commands.add_parser("power", help="show the Raspberry Pi power preflight")
    commands.add_parser("probe", help="query turntable identity and firmware")
    left = commands.add_parser("left", help="vendor Left (clockwise from above)")
    left.add_argument("degrees", type=_positive_degrees)
    right = commands.add_parser("right", help="vendor Right (counterclockwise)")
    right.add_argument("degrees", type=_positive_degrees)
    set_zero = commands.add_parser(
        "set-zero",
        help="destructively redefine the saved acoustic-axis zero (requires confirmation)",
    )
    set_zero.add_argument(
        "--confirm-redefine-zero",
        action="store_true",
        help=(
            "confirm this permanently redefines the saved acoustic-axis zero; "
            "required because set-zero cannot be undone"
        ),
    )
    commands.add_parser("home", help="return to the saved zero position")
    commands.add_parser(
        "offset",
        help="query the signed offset from saved zero; sends no motion command",
    )
    position = commands.add_parser(
        "position",
        help="home, then move to a guarded signed measurement angle",
    )
    position.add_argument("degrees", type=_measurement_position)
    position.add_argument(
        "--confirm-rig-clear",
        action="store_true",
        help="confirm the arm's full travel path is physically clear",
    )
    position.add_argument(
        "--confirm-zero-valid",
        action="store_true",
        help="confirm saved zero is on-axis and valid since the latest power-on",
    )
    commands.add_parser(
        "hotplug-stop",
        help=argparse.SUPPRESS,
    )
    commands.add_parser("stop", help="send the vendor stop request")
    return parser


def _operation_succeeded(result: Any) -> bool:
    return bool(
        getattr(result, "acknowledged", False)
        and getattr(result, "completed", False)
    )


class _RetryExhausted(Exception):
    """Both attempts of a session-retried operation failed with the
    vendored transport's exact ``ProtocolError`` base class.

    Carries both underlying exceptions so ``main()`` can report the real
    ``error_type`` (the second attempt's class) and build the combined
    message from an explicit "a retry happened" fact -- never by
    inspecting ``__cause__``, which the vendored package also sets
    internally in several places unrelated to this wrapper's own retry
    (e.g. ``ProtocolSession.synchronize`` re-raising a parse error as
    ``StartupSynchronizationError ... from exc``). Inferring "was this
    retried" from ``__cause__`` alone would misreport an ordinary,
    never-retried failure as one.
    """

    def __init__(self, first: BaseException, second: BaseException) -> None:
        super().__init__(str(second))
        self.first = first
        self.second = second


def _run_with_session_retry(
    resolved_api: TurntableApi,
    args: argparse.Namespace,
    operation: Callable[[Any], _T],
) -> tuple[_T, bool]:
    """Run ``operation(controller)`` inside a freshly opened controller
    session, retrying once against a BRAND-NEW session when the vendored
    transport raises its own ``ProtocolError`` base class EXACTLY (never a
    subclass). See ``RETRYABLE_COMMANDS`` for which commands use this and
    why each is safe to retry as a whole operation.

    Why a fresh session, not an in-session re-call (issue #2516 fix-round
    blocker 1): the vendor parser's heartbeat-mid-frame race leaves its
    ``FrameParser`` buffer non-empty (``session.parser.pending``). The
    vendor's own ``_prepare_command`` then fails closed on the very next
    command with "pre-command receive was not quiescent" -- ALSO the bare
    ``ProtocolError`` type, so an in-session retry cannot recover; it just
    fails a second time for a different reason. Only a brand-new
    controller (a fresh ``.open()``, which includes the vendor's own
    ``synchronize()``) starts with a clean parser -- exactly what a
    brand-new CLI invocation does, which is what recovered live both times
    this race was observed on jts3.

    Why the EXACT base class only, never a subclass (fix-round blocker 2):
    ``CompletionTimeout``, ``CommandRejected``, ``CommunicationTimeout``,
    and ``StartupSynchronizationError`` are ``ProtocolError`` subclasses
    that report a REAL command outcome (a motion acknowledged but never
    confirmed complete, a rejected command, a genuine link timeout) -- not
    a parser parse race. Retrying those could silently re-issue an
    unconfirmed motion command. Only the bare base class -- which the
    vendor reserves for "malformed, duplicate, or unexpected frame"
    parser-level anomalies -- is retried; every subclass instance
    propagates on the very first attempt, no matter which retryable
    command raised it.
    """

    protocol_error = resolved_api.protocol_error

    def _attempt() -> _T:
        with resolved_api.controller.open(port=args.port) as controller:
            return operation(controller)

    try:
        return _attempt(), False
    except protocol_error as first_exc:
        if type(first_exc) is not protocol_error:
            raise
        try:
            return _attempt(), True
        except protocol_error as second_exc:
            if type(second_exc) is not protocol_error:
                raise
            raise _RetryExhausted(first_exc, second_exc) from second_exc


def _power_payload(status: PowerStatus, override: bool) -> dict[str, Any]:
    return {
        "status": status,
        "override": override,
        "motion_allowed": status.safe_for_motion or override,
    }


def _emit_autostop(
    args: argparse.Namespace,
    event: str,
    *,
    ok: bool,
    **detail: Any,
) -> None:
    _emit(
        {
            "ok": ok,
            "event": f"turntable_autostop.{event}",
            "device": args.port,
            **detail,
        },
        compact=args.json,
    )


def _run_hotplug_stop(
    args: argparse.Namespace,
    api: TurntableApi,
    sleep: Callable[[float], None],
) -> int:
    """Probe and stop one exact hot-plug tty with a small retry budget."""

    if args.port is None or AUTOSTOP_PORT_RE.fullmatch(args.port) is None:
        _emit_autostop(
            args,
            "rejected",
            ok=False,
            error="hotplug-stop requires an exact /dev/ttyUSB<number> port",
        )
        return 1

    last_error = "not attempted"
    for attempt in range(1, AUTOSTOP_ATTEMPTS + 1):
        try:
            with api.controller.open(
                port=args.port,
                response_timeout=AUTOSTOP_IO_TIMEOUT,
                startup_timeout=AUTOSTOP_IO_TIMEOUT,
            ) as controller:
                probe = controller.probe()
                product = getattr(probe, "product", None)
                if getattr(probe, "connected", False) and product == AUTOSTOP_PRODUCT:
                    result = controller.stop()
                    if _operation_succeeded(result):
                        _emit_autostop(
                            args,
                            "stopped",
                            ok=True,
                            attempt=attempt,
                            product=product,
                            result=result,
                        )
                        return 0
                    last_error = "stop was not acknowledged and completed"
                elif product is not None and product != AUTOSTOP_PRODUCT:
                    _emit_autostop(
                        args,
                        "ignored",
                        ok=True,
                        attempt=attempt,
                        product=product,
                        expected_product=AUTOSTOP_PRODUCT,
                    )
                    return 0
                else:
                    last_error = "turntable identity probe did not complete"
        except Exception as exc:  # noqa: BLE001 - bounded hardware boundary
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < AUTOSTOP_ATTEMPTS:
            _emit_autostop(
                args,
                "retry",
                ok=False,
                attempt=attempt,
                error=last_error,
                retry_seconds=AUTOSTOP_RETRY_SECONDS,
            )
            sleep(AUTOSTOP_RETRY_SECONDS)

    _emit_autostop(
        args,
        "exhausted",
        ok=False,
        attempts=AUTOSTOP_ATTEMPTS,
        error=last_error,
    )
    return 1


def run(
    args: argparse.Namespace,
    *,
    api: TurntableApi | None = None,
    run_command: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if args.command == "power":
        status = read_power_status(run_command)
        ok = status.available and status.safe_for_motion
        _emit(
            {"ok": ok, "power": _power_payload(status, args.allow_power_risk)},
            compact=args.json,
        )
        return 0 if ok else 1

    if args.command == "set-zero" and not getattr(args, "confirm_redefine_zero", False):
        print(
            "set-zero requires --confirm-redefine-zero: it permanently redefines "
            "the turntable's saved acoustic-axis zero and cannot be undone",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if args.command == "position" and not (
        args.confirm_rig_clear and args.confirm_zero_valid
    ):
        missing = []
        if not args.confirm_rig_clear:
            missing.append("--confirm-rig-clear")
        if not args.confirm_zero_valid:
            missing.append("--confirm-zero-valid")
        _emit(
            {
                "ok": False,
                "error": "position requires " + " and ".join(missing),
                "target_degrees": args.degrees,
            },
            compact=args.json,
        )
        return 1

    resolved_api = api or load_upstream_api()
    if args.command == "hotplug-stop":
        return _run_hotplug_stop(args, resolved_api, sleep)
    if args.command == "detect":
        # Discovery never opens the serial link, so it can't raise the
        # vendored ProtocolError at all -- no retry wrapping here.
        devices = resolved_api.discover_devices()
        _emit({"ok": bool(devices), "devices": devices}, compact=args.json)
        return 0 if devices else 1

    power_status: PowerStatus | None = None
    if args.command in {"left", "right", "home", "position"}:
        power_status = read_power_status(run_command)
        if not power_status.safe_for_motion and not args.allow_power_risk:
            _emit(
                {
                    "ok": False,
                    "error": (
                        "motion blocked by Pi power preflight; resolve the power "
                        "condition or pass --allow-power-risk for this manual run"
                    ),
                    "power": _power_payload(power_status, False),
                },
                compact=args.json,
            )
            return 1

    retried = False
    # RETRYABLE_COMMANDS is consulted exactly once, right here -- this branch
    # IS the dispatch gate (not a decoration around it): a command in the set
    # goes through the fresh-session retry helper, everything else keeps the
    # single-open, zero-retry path unchanged from before #2516.
    if args.command in RETRYABLE_COMMANDS:
        if args.command == "probe":
            result, retried = _run_with_session_retry(
                resolved_api, args, lambda controller: controller.probe()
            )
            ok = bool(getattr(result, "connected", False))
        elif args.command == "offset":
            offset_result, retried = _run_with_session_retry(
                resolved_api, args, lambda controller: controller.offset_angle()
            )
            ok = bool(offset_result.acknowledged)
            result = {
                "offset_degrees": float(offset_result.degrees),
                "frames": list(offset_result.frames),
            }
        elif args.command == "position":

            def _do_position(controller: Any) -> tuple[Any, Any | None]:
                home_result = controller.return_to_zero()
                move_result = None
                if _operation_succeeded(home_result) and args.degrees != 0:
                    direction = "cw" if args.degrees < 0 else "ccw"
                    move_result = controller.turn_relative(direction, abs(args.degrees))
                return home_result, move_result

            (home_result, move_result), retried = _run_with_session_retry(
                resolved_api, args, _do_position
            )
            if not _operation_succeeded(home_result):
                payload = {
                    "ok": False,
                    "target_degrees": args.degrees,
                    "home_result": home_result,
                    "error": "home did not complete; target move was not attempted",
                }
                if power_status is not None:
                    payload["power"] = _power_payload(
                        power_status, args.allow_power_risk
                    )
                if retried:
                    payload["retried"] = True
                _emit(payload, compact=args.json)
                return 1

            ok = move_result is None or _operation_succeeded(move_result)
            result = {
                "target_degrees": args.degrees,
                "home": home_result,
                "move": move_result,
            }
        else:
            raise AssertionError(f"unhandled retryable command: {args.command}")
    else:
        with resolved_api.controller.open(port=args.port) as controller:
            if args.command in {"left", "right"}:
                direction = "cw" if args.command == "left" else "ccw"
                result = controller.turn_relative(direction, args.degrees)
                ok = _operation_succeeded(result)
            elif args.command == "set-zero":
                result = controller.set_zero()
                ok = _operation_succeeded(result)
            elif args.command == "home":
                result = controller.return_to_zero()
                ok = _operation_succeeded(result)
            elif args.command == "stop":
                result = controller.stop()
                ok = _operation_succeeded(result)
            else:
                raise AssertionError(f"unhandled command: {args.command}")

    payload: dict[str, Any] = {"ok": ok, "result": result}
    if power_status is not None:
        payload["power"] = _power_payload(power_status, args.allow_power_risk)
    if retried:
        payload["retried"] = True
    _emit(payload, compact=args.json)
    return 0 if ok else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    api: TurntableApi | None = None,
    run_command: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args, api=api, run_command=run_command, sleep=sleep)
    except _RetryExhausted as exc:
        # Both attempts of a retryable operation failed (see
        # `_run_with_session_retry`). This is the ONLY signal for "a retry
        # happened" -- never `exc.__cause__`, which the vendored package
        # also sets internally in places unrelated to this wrapper's own
        # retry, so it can't reliably tell a retried failure from an
        # ordinary one. `error_type` reports the real second-attempt
        # exception class, matching what an unretried failure of the same
        # kind would report.
        _emit(
            {
                "ok": False,
                "error": f"{exc.second} (after one retry; first attempt: {exc.first})",
                "error_type": type(exc.second).__name__,
                "retried": True,
            },
            compact=args.json,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - manual CLI reports boundary failures
        _emit(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            compact=args.json,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
