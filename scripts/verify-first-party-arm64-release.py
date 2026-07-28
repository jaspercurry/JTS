#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Verify a staged JTS first-party ARM64 runtime bundle before installation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _first_party_arm64_release import (
    ReleaseError,
    find_repo_root,
    install_plan_lines,
    load_contract,
    verify_bundle,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify SHA256SUMS, BUILD-INFO, file modes, ARM64 ELF identity, "
            "dynamic linkage, and required symbols for a staged runtime bundle."
        )
    )
    parser.add_argument("bundle", type=Path, help="extracted bundle directory")
    parser.add_argument(
        "--print-install-plan",
        action="store_true",
        help="after verification, print tab-separated source/destination/mode/id rows",
    )
    parser.add_argument(
        "--expected-source-sha",
        metavar="SHA",
        help=(
            "require BUILD-INFO source.git_commit to equal this exact lowercase "
            "40-character Git SHA"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo_root = find_repo_root(Path(__file__))
        contract = load_contract(repo_root)
        bundle = args.bundle.resolve()
        info = verify_bundle(
            bundle,
            contract,
            expected_source_sha=args.expected_source_sha,
        )
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.print_install_plan:
        for line in install_plan_lines(info):
            print(line)
    else:
        print(
            "verified "
            f"{info['artifact_set']} {info['release_version']} "
            f"({info['source']['git_commit']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
