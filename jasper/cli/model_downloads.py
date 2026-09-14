# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI entry point for install.sh's model staging.

jasper.model_downloads is a leaf: it owns the generic download/hash/retry
primitives but knows nothing about wake models, openWakeWord assets, or
DTLN bundles. This module is the composition root that wires a
`--registry` name to the registry that builds its `StageAsset` list —
`jasper.cli` sits above every registry, so it is free to import them
where `jasper.model_downloads` is not. See #4726.
"""
from __future__ import annotations

import argparse
import os
import sys

from jasper.aec_engines.dtln_models import dtln_stage_assets
from jasper.model_downloads import (
    DEFAULT_MAX_BYTES,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    StageAsset,
    active_wake_model,
    stage_model_assets,
)
from jasper.wake_models import (
    openwakeword_stage_assets,
    seed_default_wake_model_env,
    wake_model_stage_assets,
)


def _stage_cli(args: argparse.Namespace) -> int:
    required = args.required
    assets: list[StageAsset]
    if args.registry == "openwakeword":
        models_dir = os.environ.get("OPENWAKEWORD_MODELS_DIR", "").strip()
        if not models_dir:
            raise SystemExit("OPENWAKEWORD_MODELS_DIR is required for openwakeword staging")
        assets = openwakeword_stage_assets(models_dir, active_model=active_wake_model())
    elif args.registry == "wake":
        assets = wake_model_stage_assets(required=required)
    elif args.registry == "dtln":
        assets = dtln_stage_assets(required=required)
    else:  # pragma: no cover - argparse choices prevent this.
        raise SystemExit(f"unknown registry: {args.registry}")

    result = stage_model_assets(
        assets,
        required_timeout_seconds=args.required_timeout,
        required_retries=args.required_retries,
        optional_timeout_seconds=args.optional_timeout,
        optional_retries=args.optional_retries,
        max_bytes=args.max_bytes,
    )
    if args.registry == "openwakeword" and result.optional_failures:
        print(
            f"  warning: {result.optional_failures} inactive openWakeWord stock "
            "asset(s) failed to download; unavailable rows will be disabled in /assistant/wake/.",
            file=sys.stderr,
        )
    if required:
        return 1 if result.required_failures else 0
    return 1 if result.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage JTS model assets")
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--registry", choices=("openwakeword", "wake", "dtln"), required=True)
    mode = stage.add_mutually_exclusive_group(required=True)
    mode.add_argument("--required", action="store_true")
    mode.add_argument("--optional", action="store_true")
    stage.add_argument("--required-timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    stage.add_argument("--required-retries", type=int, default=DEFAULT_RETRIES)
    stage.add_argument("--optional-timeout", type=float, default=20.0)
    stage.add_argument("--optional-retries", type=int, default=1)
    stage.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    subparsers.add_parser("seed-wake-default")
    args = parser.parse_args(argv)
    if args.command == "stage":
        return _stage_cli(args)
    if args.command == "seed-wake-default":
        seed_default_wake_model_env()
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
