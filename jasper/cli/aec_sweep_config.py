# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Validate/apply runtime AEC3 sweep configs.

This CLI is intentionally small and stdlib-only. It gives operators
and agents a fast path for changing corpus sweep knobs on a deployed Pi:
write a validated JSON file under /var/lib/jasper, then restart only
jasper-aec-bridge.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from jasper.aec_sweep import (
    Aec3SweepConfigError,
    aec3_sweep_config_payload,
    load_aec3_sweep_config,
    validate_aec3_sweep_config_payload,
    write_aec3_sweep_config,
)


BRIDGE_UNIT = "jasper-aec-bridge.service"


def _read_payload(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def _restart_bridge() -> None:
    subprocess.run(
        ["systemctl", "reset-failed", BRIDGE_UNIT],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    subprocess.run(
        ["systemctl", "restart", BRIDGE_UNIT],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-aec-sweep-config",
        description="Inspect, validate, or apply AEC3 corpus sweep configs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="Print the effective sweep config JSON.")
    show.add_argument("--path", type=Path, default=None)

    defaults = sub.add_parser(
        "defaults",
        help="Print the built-in default sweep config JSON.",
    )
    defaults.set_defaults(command="defaults")

    validate = sub.add_parser("validate", help="Validate a sweep config JSON file.")
    validate.add_argument("file", type=Path)

    apply = sub.add_parser(
        "apply",
        help="Validate and install a sweep config JSON file.",
    )
    apply.add_argument("file", type=Path)
    apply.add_argument("--path", type=Path, default=None)
    apply.add_argument(
        "--restart-bridge",
        action="store_true",
        help="Restart jasper-aec-bridge after installing the config.",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            config = load_aec3_sweep_config(args.path, strict=True)
            print(json.dumps(aec3_sweep_config_payload(config.variants), indent=2))
            return 0
        if args.command == "defaults":
            print(json.dumps(aec3_sweep_config_payload(), indent=2))
            return 0
        if args.command == "validate":
            validate_aec3_sweep_config_payload(_read_payload(args.file))
            print(f"ok: {args.file}")
            return 0
        if args.command == "apply":
            config = write_aec3_sweep_config(_read_payload(args.file), args.path)
            print(f"installed {config.path} hash={config.config_hash}")
            if args.restart_bridge:
                _restart_bridge()
                print(f"restarted {BRIDGE_UNIT}")
            return 0
    except (
        Aec3SweepConfigError,
        OSError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    parser.error("unreachable command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
