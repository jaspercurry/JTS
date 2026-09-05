#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Choose the fail-closed GitHub Actions lane for a JTS change.

Two narrow lanes exist; every other diff runs the complete CI farm.  Each
narrow lane needs a *subject* file present, so a companion-test-only diff
cannot select one on its own, and the lanes' allowlists are disjoint, so a
diff carrying both lanes' subjects is mixed and takes ``full``.
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
    "tests/test_system_setup.py",
    "tests/test_web_design_system.py",
)
LANDING_PYTEST_TARGETS = (
    *LANDING_TEST_FILES,
    "tests/test_install_helpers.py"
    "::test_landing_page_app_css_version_uses_resolved_build_sha",
)
FAST_LANDING_PATHS = frozenset((LANDING_PAGE, *LANDING_TEST_FILES))

ROUTING_POLICY_PYTEST_TARGETS = ("tests/test_ci_classifier.py",)

# Prose documents outside docs/: the root operational docs plus the PR
# template.
DOCS_PROSE_FILES = frozenset((
    ".github/PULL_REQUEST_TEMPLATE.md",
    "AGENTS.md",
    "BRINGUP.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE-third-party.md",
    "PLAN.md",
    "PRIVACY.md",
    "QUICKSTART.md",
    "README.md",
    "SECURITY.md",
))
# Prose routing data, so it rides the docs lane; the lane runs
# docs-impact.py --validate-only, which is what constrains it.
DOCS_ROUTING_MAP = "docs/doc-map.toml"
# Bundle members whose document read tests/test_ci_classifier.py's AST
# discovery structurally cannot see: a generic `rglob("*")` sweep, or a read
# in a child process.  The value is the reason, held as data so the guard can
# assert this set is exactly ``DOCS_TEST_FILES - discovered`` in BOTH
# directions -- an undiscoverable entry cannot arrive with no reason, and a
# note cannot outlive the entry becoming discoverable.
DOCS_HAND_REGISTERED_READERS = {
    "tests/test_env_vars_codified.py": "rglob('*') over non-docs surfaces",
    "tests/test_run_wake_training_phase0.py": (
        "CWD-relative README hashed by an importlib-loaded script"
    ),
    "tests/test_tuning_tool_menu_generator.py": (
        "RUNBOOK read via an attribute on an importlib-loaded script"
    ),
}
# Registered tests that read documentation.  Keep sorted.  Over-registering is
# safe (a few seconds of bundle runtime); under-registering would let a prose
# edit merge green past a contract it breaks.
DOCS_TEST_FILES = (
    "tests/test_bass_extension_limiter_protocol.py",
    "tests/test_build_and_ci_contracts.py",
    "tests/test_calibration_agent_advisor_context.py",
    "tests/test_calibration_agent_tools.py",
    "tests/test_check_rust_script.py",
    "tests/test_ci_classifier.py",
    "tests/test_crossover_v2_prescriber_status.py",
    "tests/test_docs_impact.py",
    "tests/test_docs_linkcheck.py",
    "tests/test_env_vars_codified.py",
    "tests/test_first_party_arm64_release.py",
    "tests/test_launch_blocker_docs_exist.py",
    "tests/test_prepare_wake_livekit_smoke.py",
    "tests/test_prepare_wake_training_workdir.py",
    "tests/test_run_wake_training_phase0.py",
    "tests/test_tuning_tool_menu_generator.py",
    "tests/test_usb_turntable_experiment.py",
    "tests/test_voice_eval_registry.py",
    "tests/test_wake_review.py",
    "tests/test_waveform_fusion_experiment.py",
    "tests/test_web_design_system.py",
)
# The bundle RUNS the routing-policy test, so every docs PR re-validates its
# own registration guard.  Admitting it as a COMPANION as well would let a
# docs PR weaken the guard with only the weakened guard running.
DOCS_POLICY_TEST_FILES = frozenset(ROUTING_POLICY_PYTEST_TARGETS)
DOCS_COMPANION_TEST_FILES = frozenset(DOCS_TEST_FILES) - DOCS_POLICY_TEST_FILES


def is_docs_subject(path: str) -> bool:
    if path in DOCS_PROSE_FILES or path == DOCS_ROUTING_MAP:
        return True
    return path.startswith("docs/") and path.endswith(".md")


def is_docs_lane_path(path: str) -> bool:
    return is_docs_subject(path) or path in DOCS_COMPANION_TEST_FILES


# Disjoint by construction: no path is both a landing-lane path and a docs
# subject, so a diff carrying subjects from both lanes hits the other lane's
# path in `disallowed` and takes `full` whichever entry is tried first.
NARROW_LANES: tuple[tuple[str, Callable[[str], bool], Callable[[str], bool]], ...] = (
    (
        "fast-landing",
        lambda path: path == LANDING_PAGE,
        lambda path: path in FAST_LANDING_PATHS,
    ),
    ("docs", is_docs_subject, is_docs_lane_path),
)

TARGET_REGISTRIES = {
    "landing-pytest-targets": (
        LANDING_PYTEST_TARGETS,
        "registered fast-landing pytest targets",
    ),
    "docs-pytest-targets": (
        DOCS_TEST_FILES,
        "registered documentation-contract pytest targets",
    ),
    "routing-policy-pytest-targets": (
        ROUTING_POLICY_PYTEST_TARGETS,
        "CI-routing policy pytest targets",
    ),
}


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
            # Empty passes isascii()/isprintable(), so reject it here rather
            # than letting Change raise a ValueError past this contract.
            if not status or not status.isascii() or not status.isprintable():
                raise ChangedFileError("unsafe or invalid change status")
            path_count = 2 if status.startswith(("R", "C")) else 1
            raw_paths = fields[cursor : cursor + path_count]
            if len(raw_paths) != path_count:
                raise ChangedFileError(f"incomplete name-status record for {status!r}")
            cursor += path_count
            paths = tuple(path.decode("utf-8") for path in raw_paths)
            # `isprintable()` also excludes the newline that would otherwise
            # let a path forge extra GITHUB_OUTPUT keys or summary lines.
            if any(
                not path or path.startswith("/") or not path.isprintable()
                for path in paths
            ):
                raise ChangedFileError(f"unsafe or invalid path for {status!r}")
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
            ["git", "diff", "--name-status", "-z", "--find-renames",
             f"{base}...{head}", "--"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return parse_name_status_z(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # First line only: the reason becomes a single GITHUB_OUTPUT value.
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ChangedFileError(detail) from exc


def classify(event_name: str, changes: Sequence[Change]) -> Decision:
    """Apply the deterministic allowlist; every ambiguity selects ``full``."""

    frozen = tuple(changes)
    if event_name != "pull_request":
        return Decision(
            "full", f"{event_name or 'unknown'} event runs the complete CI farm", frozen
        )
    if not frozen:
        return Decision("full", "empty pull-request diff", frozen)
    for change in frozen:
        if change.status in {"A", "M"}:
            continue
        # See #4036.
        if (change.status.startswith("R") or change.status == "D") and all(
            is_docs_lane_path(path) for path in change.paths
        ):
            continue
        return Decision(
            "full",
            f"change status {change.status!r} is not safe for a narrow lane",
            frozen,
        )

    paths = frozenset(path for change in frozen for path in change.paths)
    for lane, is_subject, is_lane_path in NARROW_LANES:
        subjects = sorted(path for path in paths if is_subject(path))
        if not subjects:
            continue
        disallowed = sorted(path for path in paths if not is_lane_path(path))
        if disallowed:
            return Decision(
                "full",
                f"path outside the {lane} allowlist: " + ", ".join(disallowed),
                frozen,
            )
        return Decision(
            lane,
            f"{len(subjects)} {lane} subject(s) plus "
            f"{len(paths) - len(subjects)} registered companion path(s)",
            frozen,
        )

    return Decision("full", "no narrow lane subject in the pull-request diff", frozen)


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
        return Decision("full", f"changed-file comparison failed closed: {exc}")
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
    for change in decision.changes:
        rendered = " → ".join(
            f"<code>{html.escape(path)}</code>" for path in change.paths
        )
        lines.append(f"  - <code>{html.escape(change.status)}</code> {rendered}")
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
    for flag, (_, label) in TARGET_REGISTRIES.items():
        parser.add_argument(
            f"--{flag}", action="store_true", help=f"print the {label} and exit"
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for flag, (targets, _) in TARGET_REGISTRIES.items():
        if getattr(args, flag.replace("-", "_")):
            print("\n".join(targets))
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
