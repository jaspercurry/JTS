#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Choose the fail-closed GitHub Actions lane for a JTS change.

Two deliberately narrow lanes exist; everything else keeps the complete
existing CI farm.  Both policies are data, not heuristics, and both require
a *subject* file to be present so a companion-test-only diff cannot select
a narrow lane on its own:

``fast-landing``
    ``deploy/index.html`` must be present and every companion path must be
    one of the registered tests that directly reads that page.

``docs``
    at least one prose document (or the doc routing map) must be present and
    every companion path must be either another such document or one of the
    registered tests that read documentation.

The landing check runs first, so a diff carrying both the landing page and a
document is mixed and selects ``full``.
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

# Prose documents outside docs/.  Root-level operational docs plus the PR
# template, which tests/test_ci_classifier.py asserts against.
DOCS_PROSE_FILES = frozenset((
    ".github/PULL_REQUEST_TEMPLATE.md",
    "AGENTS.md",
    "BRINGUP.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "PLAN.md",
    "PRIVACY.md",
    "QUICKSTART.md",
    "README.md",
    "SECURITY.md",
))
# The doc routing map is data for an informational, non-blocking PR comment
# (see its own header), not a safety policy — so it rides the docs lane.  The
# lane runs `docs-impact.py --validate-only` plus tests/test_docs_impact.py,
# which are the checks that actually constrain it.
DOCS_ROUTING_MAP = "docs/doc-map.toml"
# Registered tests that read documentation.  Keep sorted.
#
# tests/test_ci_classifier.py's docs guard auto-discovers every test whose
# source names a `*.md` path (or globs one) and fails when such a test is
# missing here, so literal-path readers cannot silently escape the bundle.
# The guard is a subset assertion: over-registering is safe (a few seconds of
# a ~30s bundle), under-registering would let a prose edit merge green past a
# contract it breaks.
#
# Static discovery is NOT sufficient on its own, so this list is the union of
# the guard's output and a runtime audit -- an audit hook that recorded every
# open() of a document across all 667 test files (17/17 batches, per-batch
# collection summing to the full 14679).  The audit found readers the guard
# structurally cannot see, in three classes:
#
#   - Transitive reads through imported production code.  The
#     tests/test_calibration_agent_*.py entries below import
#     jasper.calibration_agent, whose tools.py resolves
#     DEFAULT_CORPUS_CANDIDATES to `Path.cwd()/"docs"/"calibration-agent"` and
#     sweeps it with `corpus.rglob("*.md")` -- so docs/calibration-agent/** is
#     a RUNTIME corpus the product consumes.  Several of these tests write a
#     FAKE corpus into tmp_path, which makes them look isolated when reading
#     the source; they are not.
#   - Generic directory sweeps.  tests/test_env_vars_codified.py walks
#     `path.rglob("*")`, so no statically visible pattern names a document.
#   - Child-process reads.  tests/test_script_help_excludes_spdx.py runs
#     `bash scripts/doc-freshness.sh 90 --all`, which enumerates every doc in
#     a subprocess -- opens a Python audit hook can never observe.
#     doc-freshness.sh is the ONLY script under scripts/, deploy/, or .github/
#     that enumerates `*.md`, and this is its only test-side caller, so
#     registering it closes this class completely.
#
# Every hand-registered entry keeps a note saying why discovery misses it, and
# test_hand_registered_doc_readers_are_still_invisible_to_discovery pins that
# they really are invisible, so a refactor cannot leave these notes stale.
#
# tests/test_run_wake_training_phase0.py is registered for completeness but its
# read is INCIDENTAL: scripts/_run_wake_training_phase0.py records
# `artifacts["readme"] = "README.md"` as a bare relative name and hashes it
# via `Path(value)`, which resolves against CWD, so under pytest it hashes the
# repo README rather than the run's own.  The test never asserts on
# artifact_hashes, so a README edit cannot fail it; if that path bug is fixed
# the read disappears and this entry can go.
#
# Deliberately NOT registered despite opening a .md during the audit:
# tests/test_shell_awk_environ_convention.py shebang-probes every file under
# `deploy` and `scripts` only (SCAN_DIRS), so the one document it touched
# (scripts/ring-proto/README.md) is not a docs-lane subject -- no change this
# lane can carry is able to reach it.  That is a structural argument, not a
# judgement about whether its assertions happen to be strict today.
DOCS_TEST_FILES = (
    "tests/test_agents_md_toc.py",
    "tests/test_audio_slice_membership_docs.py",
    "tests/test_bass_extension_limiter_protocol.py",
    "tests/test_bass_extension_plan_status.py",
    "tests/test_calibration_agent_actions.py",
    "tests/test_calibration_agent_advisor_context.py",
    "tests/test_calibration_agent_response.py",
    "tests/test_calibration_agent_sound_actions.py",
    "tests/test_calibration_agent_tools.py",
    "tests/test_ci_classifier.py",
    "tests/test_dependency_groups.py",
    "tests/test_doc_staleness_sweep_20260604.py",
    "tests/test_docs_handoff_freshness.py",
    "tests/test_docs_impact.py",
    "tests/test_docs_linkcheck.py",
    "tests/test_env_vars_codified.py",
    "tests/test_launch_blocker_docs_exist.py",
    "tests/test_prepare_wake_livekit_smoke.py",
    "tests/test_prepare_wake_training_workdir.py",
    "tests/test_run_wake_training_phase0.py",
    "tests/test_script_help_excludes_spdx.py",
    "tests/test_system_supervisor.py",
    "tests/test_tool_failure_contract_doc.py",
    "tests/test_voice_eval_registry.py",
    "tests/test_wake_review.py",
    "tests/test_waveform_fusion_experiment.py",
    "tests/test_web_correction_setup.py",
)
DOCS_PYTEST_TARGETS = DOCS_TEST_FILES


def is_docs_subject(path: str) -> bool:
    """True for a document whose presence can select the ``docs`` lane."""

    if path in DOCS_PROSE_FILES or path == DOCS_ROUTING_MAP:
        return True
    return path.startswith("docs/") and path.endswith(".md")


def is_docs_lane_path(path: str) -> bool:
    """True for anything the ``docs`` lane is allowed to carry."""

    return is_docs_subject(path) or path in DOCS_TEST_FILES


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
    if LANDING_PAGE in changed_paths:
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

    subjects = sorted(path for path in changed_paths if is_docs_subject(path))
    if subjects:
        disallowed = sorted(
            path for path in changed_paths if not is_docs_lane_path(path)
        )
        if disallowed:
            return Decision(
                lane="full",
                reason=(
                    "path outside the documentation allowlist: "
                    + ", ".join(disallowed)
                ),
                changes=frozen_changes,
            )

        companions = sorted(changed_paths.difference(subjects))
        return Decision(
            lane="docs",
            reason=(
                f"{len(subjects)} document(s) plus "
                f"{len(companions)} registered companion test file(s)"
            ),
            changes=frozen_changes,
        )

    return Decision(
        lane="full",
        reason="no narrow lane subject in the pull-request diff",
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
    parser.add_argument(
        "--docs-pytest-targets",
        action="store_true",
        help="print the registered documentation-contract pytest targets and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.landing_pytest_targets:
        print("\n".join(LANDING_PYTEST_TARGETS))
        return 0
    if args.docs_pytest_targets:
        print("\n".join(DOCS_PYTEST_TARGETS))
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
