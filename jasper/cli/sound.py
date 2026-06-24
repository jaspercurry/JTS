# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-sound`` operational commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from jasper.log_event import log_event
from jasper.sound.profile import PROFILE_PATH
from jasper.sound.runtime import DEFAULT_CONFIG_DIR, reconcile_current_dsp

logger = logging.getLogger(__name__)


def _print_reconcile(payload: dict[str, Any]) -> None:
    status = payload.get("status")
    print(f"sound DSP reconcile: {status}")
    if payload.get("reason"):
        print(f"  reason: {payload['reason']}")
    if payload.get("carrier_kind"):
        print(f"  carrier: {payload['carrier_kind']}")
    if payload.get("current_config_path"):
        print(f"  current: {payload['current_config_path']}")
    if payload.get("candidate_config_path"):
        print(f"  candidate: {payload['candidate_config_path']}")
    if payload.get("active_config_path"):
        print(f"  active: {payload['active_config_path']}")
    if payload.get("apply"):
        print(f"  op_id: {payload['apply'].get('op_id')}")


def _cmd_reconcile_current_dsp(args: argparse.Namespace) -> int:
    try:
        payload = asyncio.run(
            reconcile_current_dsp(
                profile_path=args.profile,
                config_dir=args.config_dir,
                force=args.force,
            )
        )
    except Exception as exc:  # noqa: BLE001
        if not args.fail_open:
            raise
        payload = {
            "status": "failed",
            "reason": type(exc).__name__,
            "message": str(exc),
        }
        log_event(
            logger,
            "sound.reconcile_current_dsp",
            result="failed",
            reason=type(exc).__name__,
            message=str(exc),
            level=logging.WARNING,
        )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_reconcile(payload)
        if payload.get("status") == "failed":
            print(
                f"  warning: {payload.get('message') or payload.get('reason')}",
                file=sys.stderr,
            )
    if payload.get("status") == "failed" and not args.fail_open:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-sound",
        description="Inspect and reconcile Jasper sound-profile DSP state",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reconcile = sub.add_parser(
        "reconcile-current-dsp",
        help="re-render the current JTS-owned DSP graph from saved sound intent",
    )
    reconcile.add_argument(
        "--profile",
        default=PROFILE_PATH,
        help="saved sound profile JSON path (default: /var/lib/jasper/sound_profile.json)",
    )
    reconcile.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help="CamillaDSP generated config directory",
    )
    reconcile.add_argument(
        "--force",
        action="store_true",
        help="re-render even when the reconciler would otherwise skip a no-op",
    )
    reconcile.add_argument(
        "--fail-open",
        action="store_true",
        help="log failures but exit 0 so deploy/startup can continue",
    )
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(func=_cmd_reconcile_current_dsp)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
