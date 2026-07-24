#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Choose the fail-closed GitHub Actions lane for a JTS change.

The only deliberately narrow lane is the static management landing page.
Everything else keeps the complete existing CI farm.  The policy is data,
not a heuristic: ``deploy/index.html`` must be present and every companion
path must be one of the registered tests that directly reads that page.
"""

from __future__ import annotations

import argparse
import html
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LANDING_PAGE = "deploy/index.html"
LANDING_TEST_FILES = (
    "tests/test_chat_plumbing.py",
    "tests/test_landing_control_token.py",
    "tests/test_landing_page_html.py",
    "tests/test_sound_plumbing.py",
    "tests/test_web_design_system.py",
)
LANDING_INSTALL_CONTRACTS = (
    "tests/test_install_helpers.py"
    "::test_landing_page_app_css_version_uses_resolved_build_sha",
)
LANDING_PYTEST_TARGETS = (*LANDING_TEST_FILES, *LANDING_INSTALL_CONTRACTS)
FAST_LANDING_PATHS = frozenset((LANDING_PAGE, *LANDING_TEST_FILES))


class ChangedFileError(RuntimeError):
    """The PR changed-file comparison could not be trusted."""


@dataclass(frozen=True)
class Change:
    status: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.status or not self.paths:
            raise ValueError("a change needs a status and at least one path")


@dataclass(frozen=True)
class Decision:
    lane: str
    reason: str
    changes: tuple[Change, ...] = ()


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def parse_name_status_z(payload: bytes) -> tuple[Change, ...]:
    """Parse ``git diff --name-status -z`` without losing rename sources."""

    fields = payload.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()

    changes: list[Change] = []
    cursor = 0
    try:
        while cursor < len(fields):
            status = fields[cursor].decode("utf-8")
            cursor += 1
            if not status.isascii() or not status.isprintable():
                raise ChangedFileError("unsafe or invalid change status")
            path_count = 2 if status.startswith(("R", "C")) else 1
            raw_paths = fields[cursor : cursor + path_count]
            if len(raw_paths) != path_count:
                raise ChangedFileError(
                    f"incomplete name-status record for {status!r}"
                )
            cursor += path_count
            paths = tuple(path.decode("utf-8") for path in raw_paths)
            if any(
                not path
                or path.startswith("/")
                or not path.isprintable()
                for path in paths
            ):
                raise ChangedFileError(
                    f"unsafe or invalid path in name-status record for {status!r}"
                )
            changes.append(Change(status=status, paths=paths))
    except UnicodeDecodeError as exc:
        raise ChangedFileError("changed paths are not valid UTF-8") from exc
    return tuple(changes)


def changed_files_from_git(
    base: str,
    head: str,
    *,
    runner: Runner = subprocess.run,
) -> tuple[Change, ...]:
    """Read a PR's complete merge-base diff or raise ``ChangedFileError``."""

    if not base or not head:
        raise ChangedFileError("pull-request base/head SHA is missing")
    try:
        result = runner(
            [
                "git",
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                f"{base}...{head}",
                "--",
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return parse_name_status_z(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        if isinstance(exc, ChangedFileError):
            raise
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ChangedFileError(detail) from exc


def classify(event_name: str, changes: Sequence[Change]) -> Decision:
    """Apply the deterministic allowlist; every ambiguity selects ``full``."""

    frozen_changes = tuple(changes)
    if event_name != "pull_request":
        return Decision(
            lane="full",
            reason=f"{event_name or 'unknown'} event runs the complete CI farm",
            changes=frozen_changes,
        )
    if not frozen_changes:
        return Decision(
            lane="full",
            reason="empty pull-request diff cannot use the narrow lane",
            changes=frozen_changes,
        )

    for change in frozen_changes:
        if change.status not in {"A", "M"}:
            return Decision(
                lane="full",
                reason=(
                    f"change status {change.status!r} is not safe for the "
                    "landing-page lane"
                ),
                changes=frozen_changes,
            )

    changed_paths = frozenset(
        path for change in frozen_changes for path in change.paths
    )
    if LANDING_PAGE not in changed_paths:
        return Decision(
            lane="full",
            reason="deploy/index.html is absent from the pull-request diff",
            changes=frozen_changes,
        )

    disallowed = sorted(changed_paths - FAST_LANDING_PATHS)
    if disallowed:
        return Decision(
            lane="full",
            reason=(
                "path outside the landing-page allowlist: "
                + ", ".join(disallowed)
            ),
            changes=frozen_changes,
        )

    companions = sorted(changed_paths - {LANDING_PAGE})
    return Decision(
        lane="fast-landing",
        reason=(
            "deploy/index.html plus "
            f"{len(companions)} registered companion test file(s)"
        ),
        changes=frozen_changes,
    )


def decision_from_git(
    event_name: str,
    base: str,
    head: str,
    *,
    runner: Runner = subprocess.run,
) -> Decision:
    """Choose a lane, converting comparison failures into a full decision."""

    if event_name != "pull_request":
        return classify(event_name, ())
    try:
        changes = changed_files_from_git(base, head, runner=runner)
    except ChangedFileError as exc:
        return Decision(
            lane="full",
            reason=f"changed-file comparison failed closed: {exc}",
        )
    return classify(event_name, changes)


def render_summary(decision: Decision) -> str:
    """Render the visible Actions summary without trusting path markup."""

    lines = [
        "## CI lane",
        "",
        f"- Lane: **{html.escape(decision.lane)}**",
        f"- Reason: <code>{html.escape(decision.reason)}</code>",
        "- Changed paths:",
    ]
    if not decision.changes:
        lines.append("  - _(unavailable or not needed for this event)_")
    else:
        for change in decision.changes:
            rendered = " → ".join(
                f"<code>{html.escape(path)}</code>" for path in change.paths
            )
            lines.append(
                f"  - <code>{html.escape(change.status)}</code> {rendered}"
            )
    return "\n".join(lines) + "\n"


def _write_github_files(decision: Decision) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"lane={decision.lane}\n")
            output.write(f"reason={decision.reason}\n")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(render_summary(decision))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--base", default=os.environ.get("GITHUB_BASE_SHA", ""))
    parser.add_argument("--head", default=os.environ.get("GITHUB_HEAD_SHA", ""))
    parser.add_argument(
        "--landing-pytest-targets",
        action="store_true",
        help="print the registered fast-landing pytest targets and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.landing_pytest_targets:
        print("\n".join(LANDING_PYTEST_TARGETS))
        return 0

    decision = decision_from_git(args.event, args.base, args.head)
    print(f"lane={decision.lane}")
    print(f"reason={decision.reason}")
    for change in decision.changes:
        print(f"change={change.status} {' -> '.join(change.paths)}")
    _write_github_files(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
