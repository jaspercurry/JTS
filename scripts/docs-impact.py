#!/usr/bin/env python3
"""Report documentation that a PR should consider based on changed files.

The map is intentionally advisory: a hit means "scan this canonical doc
and either update it or explain why it is unaffected." A map/schema error is
real, though, because stale routing is worse than no routing.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / "docs" / "doc-map.toml"


@dataclass(frozen=True)
class Subsystem:
    id: str
    title: str
    safety: str
    code: tuple[str, ...]
    docs: tuple[str, ...]
    requires_docs_when: tuple[str, ...]
    verification: tuple[str, ...]


DOCUMENT_CLASS_KEYS = ("session_artifacts",)


def repo_path(path: str) -> str:
    return path.strip().removeprefix("./")


def load_map(path: Path) -> tuple[Subsystem, ...]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rows = data.get("subsystem")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: expected at least one [[subsystem]] entry")

    seen: set[str] = set()
    subsystems: list[Subsystem] = []
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: subsystem #{idx} must be a table")
        sid = _required_str(path, idx, row, "id")
        if sid in seen:
            raise ValueError(f"{path}: duplicate subsystem id {sid!r}")
        seen.add(sid)
        subsystem = Subsystem(
            id=sid,
            title=_required_str(path, idx, row, "title"),
            safety=_required_str(path, idx, row, "safety"),
            code=_required_str_list(path, idx, row, "code"),
            docs=_required_str_list(path, idx, row, "docs"),
            requires_docs_when=_required_str_list(
                path, idx, row, "requires_docs_when"
            ),
            verification=_optional_str_list(row, "verification"),
        )
        subsystems.append(subsystem)
    return tuple(subsystems)


def load_classified_docs(path: Path) -> tuple[str, ...]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    table = data.get("document_classes", {})
    if table is None:
        return ()
    if not isinstance(table, dict):
        raise ValueError(f"{path}: document_classes must be a table")

    docs: list[str] = []
    for key, value in table.items():
        if key not in DOCUMENT_CLASS_KEYS:
            allowed = ", ".join(DOCUMENT_CLASS_KEYS)
            raise ValueError(
                f"{path}: unknown document_classes key {key!r}; expected one of {allowed}"
            )
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(
                f"{path}: document_classes.{key} must be a string list"
            )
        docs.extend(repo_path(item) for item in value)
    return tuple(docs)


def _required_str(path: Path, idx: int, row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: subsystem #{idx} missing string field {key!r}")
    return value.strip()


def _required_str_list(path: Path, idx: int, row: dict, key: str) -> tuple[str, ...]:
    value = row.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(
            f"{path}: subsystem #{idx} field {key!r} must be a non-empty string list"
        )
    return tuple(item.strip() for item in value)


def _optional_str_list(row: dict, key: str) -> tuple[str, ...]:
    value = row.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"optional field {key!r} must be a string list")
    return tuple(item.strip() for item in value)


def validate_map(subsystems: tuple[Subsystem, ...]) -> list[str]:
    errors: list[str] = []
    for subsystem in subsystems:
        for doc in subsystem.docs:
            doc_path = ROOT / doc
            if not doc_path.exists():
                errors.append(f"{subsystem.id}: mapped doc does not exist: {doc}")
            elif not doc_path.is_file():
                errors.append(f"{subsystem.id}: mapped doc is not a file: {doc}")
        for pattern in subsystem.code:
            if pattern.startswith("/"):
                errors.append(f"{subsystem.id}: code glob must be repo-relative: {pattern}")
        for pattern in subsystem.docs:
            if pattern.startswith("/"):
                errors.append(f"{subsystem.id}: doc path must be repo-relative: {pattern}")
    return errors


def validate_classified_docs(docs: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    for doc in docs:
        doc_path = ROOT / doc
        if doc.startswith("/"):
            errors.append(f"document_classes: doc path must be repo-relative: {doc}")
        elif not doc_path.exists():
            errors.append(f"document_classes: classified doc does not exist: {doc}")
        elif not doc_path.is_file():
            errors.append(f"document_classes: classified doc is not a file: {doc}")
    return errors


def changed_files_from_git(base: str | None, head: str | None) -> tuple[str, ...]:
    if base and head:
        args = ["git", "diff", "--name-only", f"{base}...{head}"]
    elif base:
        args = ["git", "diff", "--name-only", base]
    else:
        args = ["git", "diff", "--name-only", "HEAD"]
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return tuple(repo_path(line) for line in result.stdout.splitlines() if line.strip())


def pattern_matches(pattern: str, path: str) -> bool:
    path = repo_path(path)
    pattern = repo_path(pattern)
    return fnmatch.fnmatchcase(path, pattern)


def matching_patterns(patterns: tuple[str, ...], path: str) -> tuple[str, ...]:
    return tuple(pattern for pattern in patterns if pattern_matches(pattern, path))


def impact_report(
    subsystems: tuple[Subsystem, ...], changed_files: tuple[str, ...]
) -> list[dict]:
    report: list[dict] = []
    for subsystem in subsystems:
        matched_files = []
        matched_patterns: set[str] = set()
        for changed in changed_files:
            patterns = matching_patterns(subsystem.code, changed)
            if patterns:
                matched_files.append(changed)
                matched_patterns.update(patterns)
        if not matched_files:
            continue
        docs_touched = tuple(doc for doc in subsystem.docs if doc in changed_files)
        report.append(
            {
                "id": subsystem.id,
                "title": subsystem.title,
                "safety": subsystem.safety,
                "matched_files": tuple(sorted(set(matched_files))),
                "matched_patterns": tuple(sorted(matched_patterns)),
                "docs": subsystem.docs,
                "docs_touched": docs_touched,
                "requires_docs_when": subsystem.requires_docs_when,
                "verification": subsystem.verification,
            }
        )
    return report


def render_markdown(report: list[dict], changed_files: tuple[str, ...]) -> str:
    if not report:
        return "\n".join(
            [
                "<!-- docs-impact-bot -->",
                "## Docs impact: no mapped subsystem docs",
                "",
                "No changed files matched `docs/doc-map.toml`. If this PR still changes",
                "operator-visible behavior, update the canonical docs manually.",
            ]
        )

    lines = [
        "<!-- docs-impact-bot -->",
        f"## Docs impact: {len(report)} mapped subsystem(s)",
        "",
        "This is informational and non-blocking. For each mapped subsystem, scan",
        "the listed canonical docs and either update them or note why they are",
        "unaffected in the PR description.",
        "",
    ]
    for item in report:
        touched = set(item["docs_touched"])
        lines.extend(
            [
                f"### {item['title']} (`{item['id']}`)",
                "",
                f"- Safety class: `{item['safety']}`",
                "- Changed files:",
            ]
        )
        for changed in item["matched_files"]:
            lines.append(f"  - `{changed}`")
        lines.append("- Canonical docs to scan:")
        for doc in item["docs"]:
            suffix = " (changed in this PR)" if doc in touched else ""
            lines.append(f"  - `{doc}`{suffix}")
        lines.append("- Docs usually matter when:")
        for rule in item["requires_docs_when"]:
            lines.append(f"  - {rule}")
        if item["verification"]:
            lines.append("- Suggested verification:")
            for command in item["verification"]:
                lines.append(f"  - `{command}`")
        lines.append("")
    lines.extend(
        [
            "Changed-file count: "
            f"{len(changed_files)}. Map source: `docs/doc-map.toml`.",
        ]
    )
    return "\n".join(lines)


def render_text(report: list[dict]) -> str:
    if not report:
        return "Docs impact: no mapped subsystem docs"
    lines = [f"Docs impact: {len(report)} mapped subsystem(s)"]
    for item in report:
        docs = ", ".join(item["docs"])
        lines.append(f"- {item['id']} ({item['safety']}): {docs}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default=str(DEFAULT_MAP), help="doc map TOML path")
    parser.add_argument("--base", help="base ref/SHA for git diff")
    parser.add_argument("--head", help="head ref/SHA for git diff")
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="repo-relative changed file; may be repeated",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "text", "json", "count"),
        default="text",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the map and exit without reading changed files",
    )
    args = parser.parse_args(argv)

    try:
        map_path = Path(args.map)
        subsystems = load_map(map_path)
        classified_docs = load_classified_docs(map_path)
        errors = validate_map(subsystems) + validate_classified_docs(classified_docs)
        if errors:
            for error in errors:
                print(f"docs-impact: {error}", file=sys.stderr)
            return 2
        if args.validate_only:
            print(
                "docs-impact: "
                f"{len(subsystems)} subsystem mappings valid; "
                f"{len(classified_docs)} classified docs valid"
            )
            return 0

        changed_files = tuple(repo_path(path) for path in args.changed_file)
        if not changed_files:
            changed_files = changed_files_from_git(args.base, args.head)

        report = impact_report(subsystems, changed_files)
        if args.format == "count":
            print(len(report))
        elif args.format == "json":
            print(json.dumps({"changed_files": changed_files, "impact": report}, indent=2))
        elif args.format == "markdown":
            print(render_markdown(report, changed_files))
        else:
            print(render_text(report))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"docs-impact: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
