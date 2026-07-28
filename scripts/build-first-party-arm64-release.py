#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build a verified, deterministic first-party JTS ARM64 runtime bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _first_party_arm64_release import ReleaseError, build_release, find_repo_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build jasper-fanin, jasper-outputd, and the JTS ALSA ring plugin "
            "on native Debian Trixie ARM64; verify their ELF contract and emit "
            "a deterministic bundle/archive."
        )
    )
    parser.add_argument(
        "--version",
        help="release version used in output names (default: git-<12-char SHA>)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist/first-party-arm64"),
        help="output directory (default: dist/first-party-arm64)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing bundle/archive with the same version",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="emit the verified bundle directory without the deterministic tar.xz",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo_root = find_repo_root(Path(__file__))
        output_dir = args.output_dir
        if not output_dir.is_absolute():
            output_dir = repo_root / output_dir
        bundle, archive = build_release(
            repo_root,
            output_dir=output_dir,
            release_version=args.version,
            force=args.force,
            no_archive=args.no_archive,
        )
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"bundle: {bundle}")
    if archive is not None:
        print(f"archive: {archive}")
        print(f"archive checksum: {archive}.sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
