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
# Hardware-protection cap on how far from saved zero the arm may be
# commanded, in degrees. Past it the arm fouls the rig and wraps its own
# cable, which is component damage, not a measurement error. This is the
# ONE number for the whole tool: `position`'s argument bound and the
# `left`/`right` endpoint gate both derive from it, so they can never
# drift apart. It changes only by an owner-approved PR -- there is
# deliberately NO runtime override: no flag, no environment variable, no
# config key widens it.
#
# What it does not cover: the gate reasons about the CONTROLLER'S
# BELIEVED offset from its saved zero. A power event can silently re-seat
# that belief (README, "Detect and probe"), so this caps commanded
# runaway, not a corrupted zero. Confirming the physical zero is still
# the acoustic axis remains a human bench task.
TRAVEL_ENVELOPE_DEGREES = 45.0
MEASUREMENT_MIN_DEGREES = -TRAVEL_ENVELOPE_DEGREES
MEASUREMENT_MAX_DEGREES = TRAVEL_ENVELOPE_DEGREES
TRAVEL_ENVELOPE_EXCEEDED = "travel_envelope_exceeded"
TRAVEL_OFFSET_UNREADABLE = "travel_offset_unreadable"
# Worst case per attempt is open()'s startup_timeout plus 4 independent
# response_timeout round trips (probe's connection/firmware/product + stop),
# each capped at AUTOSTOP_IO_TIMEOUT: 5 * 1.5s = 7.5s. 8 attempts * 7.5s + 7
# inter-attempt sleeps * AUTOSTOP_RETRY_SECONDS = 70.5s, under the unit's
# TimeoutStartSec=90s (deploy/systemd/jasper-turntable-autostop@.service).
# More attempts buys more real settling time, not a longer per-attempt
# window: an unapproved startup byte fails synchronize() immediately, it
# never times out, so a controller still emitting post-power-on noise needs
# repeated tries rather than a longer wait per try.
AUTOSTOP_ATTEMPTS = 8
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
# `set-zero` (owner-only, destructive) and the `left`/`right`/`stop`/
# `home` motion commands are deliberately excluded and stay zero-retry --
# including the envelope gate's pre-move offset read, which rides inside
# `left`/`right`'s single session (`_travel_refusal`).
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
            f"position must be between {MEASUREMENT_MIN_DEGREES:g} "
            f"and +{MEASUREMENT_MAX_DEGREES:g} degrees"
        )
    return degrees


def travel_endpoint(command: str, degrees: float, current_offset: float) -> float:
    """Offset from saved zero this relative move ends at.

    Verified on hardware (jts3, 2026-08-28): vendor ``left`` INCREASES the
    offset reading -- ``left 5`` moved -80.87 to -75.87 -- so ``right``
    decreases it. Note this is the opposite sign to ``position``'s own
    argument axis, where negative means left; the envelope is symmetric,
    so both scales share one cap.
    """

    return current_offset + (degrees if command == "left" else -degrees)


def travel_is_allowed(endpoint: float, current_offset: float) -> bool:
    """Whether a move ending at ``endpoint`` may be commanded.

    Inside the envelope, yes. Outside it, only when the move stays on the
    SAME side of saved zero and strictly reduces the distance from it: a
    platform already stranded beyond the cap (a corrupted zero, a move
    made before this gate existed) must always be recoverable inward,
    never driven further out.

    The same-sign term is load-bearing, not a tidier way to say the
    second: a move that crosses zero and lands outside the FAR side can
    still reduce the absolute distance -- from -80.87, ``left 161.7``
    ends at +80.83 -- which is a full swing through the envelope to a
    position just as far out as it started. Distance alone admits it.
    """

    if abs(endpoint) <= TRAVEL_ENVELOPE_DEGREES:
        return True
    return endpoint * current_offset > 0 and abs(endpoint) < abs(current_offset)


def _believed_offset(reading: Any) -> float | None:
    """The controller's believed offset, or ``None`` when it isn't usable.

    A non-finite reading must never reach :func:`travel_is_allowed`: every
    comparison against NaN is false, so an unparseable offset would sail
    through the cap instead of tripping it.
    """

    if not getattr(reading, "acknowledged", False):
        return None
    raw_degrees = getattr(reading, "degrees", None)
    if raw_degrees is None:
        return None
    try:
        degrees = float(raw_degrees)
    except (TypeError, ValueError):
        return None
    return degrees if math.isfinite(degrees) else None


def _travel_refusal(controller: Any, args: argparse.Namespace) -> dict[str, Any] | None:
    """Gate one ``left``/``right`` move, returning a refusal or ``None``.

    The offset is read in the SAME session that will issue the motion, so
    the belief the gate decides on is the one the controller holds
    immediately before the move -- no teardown and re-synchronize between
    them. The read is deliberately not retried: ``left``/``right`` are
    zero-retry by contract, and a raced read refuses the move, which is
    already the fail-closed outcome.
    """

    current = _believed_offset(controller.offset_angle())
    if current is None:
        return {
            "reason": TRAVEL_OFFSET_UNREADABLE,
            "error": (
                f"{args.command} refused: the controller's offset from saved zero "
                "could not be read, so the travel envelope cannot be checked"
            ),
            "travel_envelope_degrees": TRAVEL_ENVELOPE_DEGREES,
        }

    endpoint = travel_endpoint(args.command, args.degrees, current)
    if travel_is_allowed(endpoint, current):
        return None

    stranded = abs(current) > TRAVEL_ENVELOPE_DEGREES
    inward = ("left" if current < 0 else "right") if stranded else None
    # The ceiling, not "whatever ends nearer zero": a big enough inward
    # move swings through the envelope and back out the far side, which
    # the gate refuses even though it does end nearer zero.
    remedy = (
        f"the platform is already outside it, so only a `{inward}` move of at "
        f"most {abs(current) + TRAVEL_ENVELOPE_DEGREES:g} deg, or `home`, is allowed"
        if inward is not None
        else "command a smaller move or `home`"
    )
    return {
        "reason": TRAVEL_ENVELOPE_EXCEEDED,
        "error": (
            f"{args.command} {args.degrees:g} would leave the platform "
            f"{endpoint:+.2f} deg from saved zero, outside the "
            f"+/-{TRAVEL_ENVELOPE_DEGREES:g} deg travel envelope (hardware "
            f"protection: cable wrap and rig clearance; no override exists). "
            f"Current offset is {current:+.2f} deg; {remedy}"
        ),
        "travel_envelope_degrees": TRAVEL_ENVELOPE_DEGREES,
        "current_offset_degrees": current,
        "predicted_offset_degrees": endpoint,
        "inward_command": inward,
    }


#: Lets ``--json`` land either before or after the subcommand
#: (``jts_turntable.py --json left 5`` and ``jts_turntable.py left 5 --json``
#: both work). ``default=SUPPRESS`` is load-bearing: a subparser namespace is
#: built separately and copied onto the top-level one (argparse's own
#: ``_SubParsersAction``), so an ordinary default would overwrite a ``--json``
#: already set before the subcommand with this subparser's unset default.
#: SUPPRESS means "not passed here" adds no key, so the copy has nothing to
#: overwrite with. Previously ``--json`` worked only before the subcommand;
#: placing it after was an argparse "unrecognized arguments" error.
_JSON_PARENT = argparse.ArgumentParser(add_help=False)
_JSON_PARENT.add_argument(
    "--json", action="store_true", default=argparse.SUPPRESS, help="emit compact JSON"
)


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

    def add_sub(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        """One subcommand, always carrying _JSON_PARENT so --json works in
        both positions on every one of them, not just a hand-picked subset."""
        return commands.add_parser(name, parents=[_JSON_PARENT], **kwargs)

    add_sub("detect", help="list matching USB serial devices")
    add_sub("power", help="show the Raspberry Pi power preflight")
    add_sub("probe", help="query turntable identity and firmware")
    left = add_sub("left", help="vendor Left (clockwise from above)")
    left.add_argument("degrees", type=_positive_degrees)
    right = add_sub("right", help="vendor Right (counterclockwise)")
    right.add_argument("degrees", type=_positive_degrees)
    set_zero = add_sub(
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
    add_sub("home", help="return to the saved zero position")
    add_sub(
        "offset",
        help="query the signed offset from saved zero; sends no motion command",
    )
    position = add_sub(
        "position", help="home, then move to a guarded signed measurement angle"
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
    add_sub(
        "hotplug-stop",
        help="internal udev add-hook: probe and stop the matching turntable",
    )
    add_sub("stop", help="send the vendor stop request")
    return parser


def _operation_succeeded(result: Any) -> bool:
    return bool(
        getattr(result, "acknowledged", False)
        and getattr(result, "completed", False)
    )


class _RetryExhausted(Exception):
    """A retry was attempted (the first attempt raced with the exact
    ``ProtocolError`` base class) and the second, fresh-session attempt
    also failed -- with ANY exception, not necessarily the same type.

    The fresh ``.open()`` itself can fail for an unrelated reason (e.g. a
    real ``StartupSynchronizationError`` if the second session genuinely
    can't synchronize). That must still be reported as a retry: attempt 1
    may already have moved the platform (home + relative move both ran
    before it raced), so silently reporting only the second failure --
    indistinguishable from a cold failure where nothing moved -- would
    withhold exactly the fact an operator needs before deciding whether to
    retry by hand. Carries both underlying exceptions so ``main()`` can
    report the real ``error_type`` (the second attempt's class, whatever
    it is) and build the combined message from an explicit "a retry
    happened" fact -- never by inspecting ``__cause__``, which the
    vendored package also sets internally in several places unrelated to
    this wrapper's own retry (e.g. ``ProtocolSession.synchronize``
    re-raising a parse error as ``StartupSynchronizationError ... from
    exc``). Inferring "was this retried" from ``__cause__`` alone would
    misreport an ordinary, never-retried failure as one.
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
    command raised it. This exact-class gate applies ONLY to whether a
    retry is attempted at all (the first attempt) -- once a retry is
    underway, ANY failure on the second, fresh-session attempt (including
    the fresh ``.open()`` itself failing, e.g. a real
    ``StartupSynchronizationError``) is reported as an exhausted retry
    (fix-round should-fix, second delta): the retry already happened and
    attempt 1's operation may already have moved the platform, so that
    fact must never be silently dropped just because the second failure
    isn't the bare base class either.
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
        except Exception as second_exc:
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
    # single-open, zero-retry path.
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
                refusal = _travel_refusal(controller, args)
                if refusal is not None:
                    payload = {"ok": False, **refusal}
                    if power_status is not None:
                        payload["power"] = _power_payload(
                            power_status, args.allow_power_risk
                        )
                    _emit(payload, compact=args.json)
                    return 1
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

    response: dict[str, Any] = {"ok": ok, "result": result}
    if power_status is not None:
        response["power"] = _power_payload(power_status, args.allow_power_risk)
    if retried:
        response["retried"] = True
    _emit(response, compact=args.json)
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
