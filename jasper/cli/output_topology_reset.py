# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Clear saved speaker intent and leave the audio path parked.

This is the command-line recovery counterpart of ``/sound/setup/``.  It has
one safe meaning: replace the saved output topology with zero speaker groups.
The runtime-convergence seam parks audio before the write; the root hardware
reconciler owns rendering and final outputd convergence after it.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from jasper.output_topology_runtime import (
    RECONCILE_UNIT,
    read_before,
    reset_to_unconfigured,
    topology_summary,
)
from jasper.output_topology import (
    OutputTopologyError,
    new_topology_draft,
    topology_path,
)


def _describe(summary: dict[str, Any]) -> str:
    if not summary.get("readable"):
        return f"<unreadable: {summary.get('error')}>"
    groups = summary.get("speaker_groups") or []
    return f"{summary['name']!r}, groups={groups!r}"


def _print_summary(result: dict[str, Any], *, dry_run: bool) -> None:
    print(f"{'WOULD RESET' if dry_run else 'RESET'} output topology: "
          f"{result['topology_path']}")
    print(f"  BEFORE: {_describe(result['before'])}")
    print(f"  AFTER:  {_describe(result['after'])}")
    if dry_run:
        print("  (dry run — nothing written or parked)")
        return
    print("  audio: parked")
    reconcile = result["reconcile"]
    if reconcile.get("skipped"):
        print("  reconcile: skipped (--no-reconcile)")
    elif reconcile.get("ok"):
        print(f"  reconcile: completed {RECONCILE_UNIT}")
    elif reconcile.get("converging"):
        # Past trigger_reconcile's wait budget but still running underneath
        # (#3094) -- not a failure, so it must not print as one.
        print("  reconcile: still converging; audio should come up shortly")
    else:
        print("  reconcile: did not complete; audio remains parked")


def _confirm(target: str, before: dict[str, Any], *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "jasper-output-topology-reset: refusing to reset without "
            "confirmation. Re-run with --yes for non-interactive use.",
            file=sys.stderr,
        )
        return False
    description = before.get("name") if before.get("readable") else "the current topology"
    print(
        f"This clears {description!r} at {target}. Audio remains off until "
        "you choose a speaker layout."
    )
    return input("Proceed? [y/N] ").strip().lower() in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-output-topology-reset",
        description="Clear the saved speaker layout and park audio.",
    )
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-reconcile", action="store_true")
    parser.add_argument("--path")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    path = args.path
    if args.dry_run:
        result: dict[str, Any] = {
            "topology_path": str(topology_path(path)),
            "before": read_before(path),
            "after": topology_summary(new_topology_draft()),
            "reconcile": {"ok": None, "skipped": True},
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_summary(result, dry_run=True)
        return 0

    before = read_before(path)
    if not _confirm(str(topology_path(path)), before, assume_yes=args.yes):
        return 1
    try:
        result = reset_to_unconfigured(path=path, reconcile=not args.no_reconcile)
    except (OutputTopologyError, OSError, RuntimeError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_summary(result, dry_run=False)
    reconcile = result["reconcile"]
    return 0 if reconcile.get("skipped") or reconcile.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
