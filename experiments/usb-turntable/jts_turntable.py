#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Manual JTS adapter for the reusable ``usb_turntable`` controller.

This experiment owns only JTS-specific command names and Raspberry Pi power
preflight.  Device discovery, serial framing, response parsing, and controller
operations remain owned by the upstream ``usb_turntable`` package.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


POWER_FLAG_NAMES = (
    "under_voltage",
    "frequency_capped",
    "throttled",
    "soft_temperature_limit",
)
THROTTLED_RE = re.compile(r"\s*throttled=(0x[0-9a-fA-F]+)\s*")
EXPERIMENT_ROOT = Path(__file__).resolve().parent
VENDOR_ROOT = EXPERIMENT_ROOT / "vendor"
VENDOR_MANIFEST = VENDOR_ROOT / "UPSTREAM.json"
VENDOR_MANIFEST_SHA256 = "371ae63ab8ada0866b5efce93d013b5d592950066adb38d43e59b823d8c32fd3"
VENDOR_ROOT_ENTRIES = frozenset(
    {"LICENSE", "NOTICE.md", "README.md", "UPSTREAM.json", "usb_turntable"}
)


@dataclass(frozen=True)
class PowerStatus:
    """One bounded ``vcgencmd get_throttled`` reading."""

    available: bool
    safe_for_motion: bool
    raw: str | None
    current_flags: tuple[str, ...]
    history_flags: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class TurntableApi:
    """The narrow upstream API this JTS adapter consumes."""

    discover_devices: Callable[[], list[dict[str, str | None]]]
    controller: Any


def _flag_names(bits: int) -> tuple[str, ...]:
    return tuple(
        name for bit, name in enumerate(POWER_FLAG_NAMES) if bits & (1 << bit)
    )


def read_power_status(
    run_command: Callable[..., Any] = subprocess.run,
) -> PowerStatus:
    """Read Pi power state, failing closed when motion safety is unknown.

    Current flags block motion.  Since-boot flags are retained as a warning,
    but do not permanently block a recovered Pi.
    """

    try:
        result = run_command(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return PowerStatus(
            available=False,
            safe_for_motion=False,
            raw=None,
            current_flags=(),
            history_flags=(),
            detail=f"power status unavailable: {type(exc).__name__}: {exc}",
        )

    stdout = str(getattr(result, "stdout", ""))
    returncode = int(getattr(result, "returncode", 0))
    if returncode != 0:
        stderr = str(getattr(result, "stderr", "")).strip()
        suffix = f": {stderr}" if stderr else ""
        return PowerStatus(
            available=False,
            safe_for_motion=False,
            raw=None,
            current_flags=(),
            history_flags=(),
            detail=f"vcgencmd exited {returncode}{suffix}",
        )

    match = THROTTLED_RE.fullmatch(stdout)
    if match is None:
        return PowerStatus(
            available=False,
            safe_for_motion=False,
            raw=None,
            current_flags=(),
            history_flags=(),
            detail=f"unexpected vcgencmd output: {stdout.strip()!r}",
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
        available=True,
        safe_for_motion=not current_flags,
        raw=raw,
        current_flags=current_flags,
        history_flags=history_flags,
        detail=detail,
    )


def verify_vendor(
    vendor_root: Path = VENDOR_ROOT,
    manifest_path: Path = VENDOR_MANIFEST,
) -> dict[str, Any]:
    """Verify the anchored manifest and exact vendor inventory before import."""

    expected_manifest_path = vendor_root / "UPSTREAM.json"
    if manifest_path != expected_manifest_path:
        raise RuntimeError(
            "vendor manifest must be the UPSTREAM.json inside the vendor root"
        )
    if vendor_root.is_symlink() or not vendor_root.is_dir():
        raise RuntimeError("vendor root is missing, not a directory, or a symlink")
    try:
        root_entries = {entry.name for entry in vendor_root.iterdir()}
    except OSError as exc:
        raise RuntimeError(f"could not inventory vendor root {vendor_root}: {exc}") from exc
    if root_entries != VENDOR_ROOT_ENTRIES:
        raise RuntimeError(
            "vendor root inventory does not match the pinned layout: "
            f"expected={sorted(VENDOR_ROOT_ENTRIES)!r} actual={sorted(root_entries)!r}"
        )
    for entry in vendor_root.iterdir():
        if entry.is_symlink():
            raise RuntimeError(f"vendor root entry must not be a symlink: {entry.name}")

    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read vendor manifest {manifest_path}: {exc}") from exc
    actual_manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_digest != VENDOR_MANIFEST_SHA256:
        raise RuntimeError(
            "vendor manifest trust-root mismatch: "
            f"expected={VENDOR_MANIFEST_SHA256} actual={actual_manifest_digest}"
        )
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not parse vendor manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise RuntimeError("vendor manifest must be a schema-1 JSON object")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("vendor manifest has no file hashes")

    expected_paths = sorted(str(path) for path in files)
    if any(
        len(Path(path).parts) != 2
        or Path(path).parts[0] != "usb_turntable"
        or Path(path).suffix != ".py"
        for path in expected_paths
    ):
        raise RuntimeError("vendor manifest source paths must be package-root Python files")
    package_root = vendor_root / "usb_turntable"
    if package_root.is_symlink() or not package_root.is_dir():
        raise RuntimeError("vendored package is missing, not a directory, or a symlink")
    actual_paths = sorted(
        str(path.relative_to(vendor_root)) for path in package_root.iterdir()
    )
    if actual_paths != expected_paths:
        raise RuntimeError(
            "vendored package inventory does not match UPSTREAM.json: "
            f"expected={expected_paths!r} actual={actual_paths!r}"
        )

    digest_lines = []
    root_resolved = vendor_root.resolve()
    for relative_path in expected_paths:
        expected_digest = files[relative_path]
        if not isinstance(expected_digest, str):
            raise RuntimeError(f"invalid digest for {relative_path}")
        source = vendor_root / relative_path
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"vendored source is missing or a symlink: {relative_path}")
        resolved = source.resolve()
        if not resolved.is_relative_to(root_resolved):
            raise RuntimeError(f"vendored source escapes vendor root: {relative_path}")
        actual_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise RuntimeError(
                f"vendored source hash mismatch for {relative_path}: "
                f"expected={expected_digest} actual={actual_digest}"
            )
        digest_lines.append(f"{actual_digest}  {relative_path}\n")

    actual_aggregate = hashlib.sha256("".join(digest_lines).encode("ascii")).hexdigest()
    expected_aggregate = manifest.get("aggregate_sha256")
    if actual_aggregate != expected_aggregate:
        raise RuntimeError(
            "vendored aggregate hash mismatch: "
            f"expected={expected_aggregate} actual={actual_aggregate}"
        )

    license_files = manifest.get("license_files")
    if not isinstance(license_files, dict) or sorted(license_files) != [
        "LICENSE",
        "NOTICE.md",
    ]:
        raise RuntimeError("vendor manifest must pin LICENSE and NOTICE.md")
    for relative_path, expected_digest in license_files.items():
        if not isinstance(expected_digest, str):
            raise RuntimeError(f"invalid digest for {relative_path}")
        source = vendor_root / relative_path
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"vendored license file is missing or a symlink: {relative_path}")
        resolved = source.resolve()
        if not resolved.is_relative_to(root_resolved):
            raise RuntimeError(f"vendored license file escapes vendor root: {relative_path}")
        actual_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise RuntimeError(
                f"vendored license hash mismatch for {relative_path}: "
                f"expected={expected_digest} actual={actual_digest}"
            )
    return manifest


def _validate_loaded_upstream_modules(
    allowed_sources: frozenset[Path],
    package_root: Path,
) -> None:
    """Reject import-cache entries that did not come from the verified sources."""

    package_root_resolved = package_root.resolve()
    for name, module in tuple(sys.modules.items()):
        if name != "usb_turntable" and not name.startswith("usb_turntable."):
            continue
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise RuntimeError(f"loaded {name} module has no verifiable source path")
        module_path = Path(module_file).resolve()
        if module_path not in allowed_sources:
            raise RuntimeError(
                f"loaded {name} module resolved outside the pinned sources: {module_path}"
            )
        if name == "usb_turntable":
            package_paths = getattr(module, "__path__", None)
            if package_paths is None or tuple(
                Path(path).resolve() for path in package_paths
            ) != (package_root_resolved,):
                raise RuntimeError("loaded usb_turntable package has an unexpected import path")


def load_upstream_api() -> TurntableApi:
    """Verify and load the canonical controller package without reimplementing it."""

    manifest = verify_vendor(VENDOR_ROOT, VENDOR_MANIFEST)
    package_root = VENDOR_ROOT / "usb_turntable"
    allowed_sources = frozenset(
        (VENDOR_ROOT / relative_path).resolve()
        for relative_path in manifest["files"]
    )
    _validate_loaded_upstream_modules(allowed_sources, package_root)

    vendor_path = str(VENDOR_ROOT)
    previous_sys_path = list(sys.path)
    sys.path[:] = [vendor_path, *(path for path in sys.path if path != vendor_path)]
    previous_dont_write_bytecode = sys.dont_write_bytecode
    previous_pycache_prefix = sys.pycache_prefix
    try:
        sys.dont_write_bytecode = True
        # The absent path is guaranteed by the exact root inventory above.
        # Redirecting cache lookup there prevents a process-level cache prefix
        # from supplying bytecode while ``dont_write_bytecode`` prevents creation.
        sys.pycache_prefix = str(VENDOR_ROOT / ".verified-import-cache-disabled")
        importlib.invalidate_caches()
        package = importlib.import_module("usb_turntable")
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
        sys.pycache_prefix = previous_pycache_prefix
        sys.path[:] = previous_sys_path

    _validate_loaded_upstream_modules(allowed_sources, package_root)

    return TurntableApi(
        discover_devices=package.discover_devices,
        controller=package.TurntableController,
    )


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual JTS USB turntable proof-of-concept",
    )
    parser.add_argument("--port", help="serial path; default uses upstream discovery")
    parser.add_argument(
        "--response-timeout",
        type=float,
        default=3.0,
        help="seconds to wait for a command response (default: 3)",
    )
    parser.add_argument(
        "--motion-timeout",
        type=float,
        default=30.0,
        help=(
            "seconds to wait for a completion report; expiry does not stop "
            "platform motion (default: 30)"
        ),
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=3.0,
        help="seconds allowed for startup-frame quarantine (default: 3)",
    )
    parser.add_argument(
        "--allow-power-risk",
        action="store_true",
        help="allow motion despite current or unreadable Pi power status",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit compact JSON (default is indented JSON)",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("detect", help="list matching USB serial devices")
    commands.add_parser("power", help="show the Raspberry Pi power preflight")
    commands.add_parser("probe", help="query turntable identity and firmware")
    left = commands.add_parser(
        "left",
        help="vendor Left direction (clockwise viewed from above)",
    )
    left.add_argument("degrees", type=_positive_degrees)
    right = commands.add_parser(
        "right",
        help="vendor Right direction (counterclockwise viewed from above)",
    )
    right.add_argument("degrees", type=_positive_degrees)
    commands.add_parser("set-zero", help="mark the current position as zero")
    commands.add_parser("home", help="return to the saved zero position")
    commands.add_parser("stop", help="send the vendor stop request")
    return parser


def _open_controller(api: TurntableApi, args: argparse.Namespace) -> Any:
    return api.controller.open(
        port=args.port,
        response_timeout=args.response_timeout,
        motion_timeout=args.motion_timeout,
        startup_timeout=args.startup_timeout,
    )


def _operation_succeeded(result: Any) -> bool:
    return bool(
        getattr(result, "acknowledged", False)
        and getattr(result, "completed", False)
    )


def _power_payload(status: PowerStatus, override: bool) -> dict[str, Any]:
    return {
        "status": status,
        "override": override,
        "motion_allowed": status.safe_for_motion or override,
    }


def run(
    args: argparse.Namespace,
    *,
    api: TurntableApi | None = None,
    run_command: Callable[..., Any] = subprocess.run,
) -> int:
    if args.command == "power":
        status = read_power_status(run_command)
        _emit(
            {
                "ok": status.available and status.safe_for_motion,
                "power": _power_payload(status, args.allow_power_risk),
            },
            compact=args.json,
        )
        return 0 if status.available and status.safe_for_motion else 1

    resolved_api = api or load_upstream_api()
    if args.command == "detect":
        devices = resolved_api.discover_devices()
        _emit(
            {"ok": bool(devices), "devices": devices},
            compact=args.json,
        )
        return 0 if devices else 1

    motion_commands = {"left", "right", "home"}
    power_status: PowerStatus | None = None
    if args.command in motion_commands:
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

    with _open_controller(resolved_api, args) as controller:
        if args.command == "probe":
            result = controller.probe()
            ok = bool(getattr(result, "connected", False))
        elif args.command == "left":
            result = controller.turn_relative("cw", args.degrees)
            ok = _operation_succeeded(result)
        elif args.command == "right":
            result = controller.turn_relative("ccw", args.degrees)
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
    _emit(payload, compact=args.json)
    return 0 if ok else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    api: TurntableApi | None = None,
    run_command: Callable[..., Any] = subprocess.run,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args, api=api, run_command=run_command)
    except Exception as exc:  # noqa: BLE001 - manual CLI must report boundary failures
        _emit(
            {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            compact=args.json,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
