#!/usr/bin/env python3
"""Check local Markdown links and anchors in changed Markdown files.

This is a PR-fast check. It intentionally ignores external URLs and only
checks links in Markdown files touched by the diff unless --all is passed.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_SUFFIXES = {".md", ".markdown"}

INLINE_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>]*>|[^)\s]+)")
REF_LINK_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|[^\s]+)")
HTML_LINK_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HTML_ANCHOR_RE = re.compile(r"""<(?:a|[^>]+)\s+[^>]*(?:id|name)=["']([^"']+)["']""", re.I)
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass(frozen=True)
class Link:
    file: Path
    line: int
    target: str


@dataclass(frozen=True)
class Issue:
    file: Path
    line: int
    target: str
    message: str

    def format(self) -> str:
        rel = self.file.relative_to(ROOT)
        return f"{rel}:{self.line}: {self.message}: {self.target}"


def repo_path(path: str) -> str:
    return path.strip().removeprefix("./")


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


def markdown_files(paths: tuple[str, ...], *, include_deleted: bool = False) -> tuple[Path, ...]:
    files: list[Path] = []
    for raw in paths:
        path = ROOT / repo_path(raw)
        if path.suffix.lower() not in MARKDOWN_SUFFIXES:
            continue
        if path.exists() and path.is_file():
            files.append(path)
        elif include_deleted:
            files.append(path)
    return tuple(sorted(set(files)))


def all_markdown_files() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in ROOT.rglob("*")
            if path.is_file()
            and path.suffix.lower() in MARKDOWN_SUFFIXES
            and ".git" not in path.parts
        )
    )


def iter_non_fenced_lines(path: Path):
    in_fence = False
    fence_marker = ""
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if not in_fence:
            yield line_no, line


def collect_links(path: Path) -> tuple[Link, ...]:
    links: list[Link] = []
    for line_no, line in iter_non_fenced_lines(path):
        for regex in (INLINE_LINK_RE, REF_LINK_RE, HTML_LINK_RE):
            for match in regex.finditer(line):
                target = clean_target(match.group(1))
                if target:
                    links.append(Link(path, line_no, target))
    return tuple(links)


def clean_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    return html.unescape(target)


def is_external_or_special(target: str) -> bool:
    if not target:
        return True
    if target.startswith("<") or target.startswith("{"):
        return True
    if SCHEME_RE.match(target):
        return True
    return target.startswith("//")


def split_target(target: str) -> tuple[str, str]:
    before_hash, sep, after_hash = target.partition("#")
    path = before_hash.split("?", 1)[0]
    fragment = after_hash if sep else ""
    return unquote(path), unquote(fragment)


def strip_line_suffix(path_text: str) -> str:
    # Repo docs sometimes use GitHub-style `path/to/file.py:123` references.
    # Treat the line suffix as metadata after confirming the file exists.
    if re.search(r":\d+$", path_text):
        candidate = re.sub(r":\d+$", "", path_text)
        if candidate:
            return candidate
    return path_text


def resolve_target_file(source: Path, path_text: str) -> Path:
    if not path_text:
        return source
    if path_text.startswith("/"):
        return (ROOT / path_text.lstrip("/")).resolve()
    return (source.parent / path_text).resolve()


def markdown_anchor_slug(text: str) -> str:
    # Approximate GitHub's generated heading IDs closely enough for repo docs:
    # lowercase, strip formatting/punctuation, spaces become hyphens, and
    # repeated spaces remain repeated hyphens.
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = html.unescape(text).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    chars: list[str] = []
    for char in text:
        if char.isalnum() or char in {" ", "-", "_"}:
            chars.append(char)
    return "".join(chars).replace(" ", "-").strip("-")


def anchors_for(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for _line_no, line in iter_non_fenced_lines(path):
        heading = HEADING_RE.match(line)
        if heading:
            slug = markdown_anchor_slug(heading.group(2))
            if slug:
                count = counts.get(slug, 0)
                counts[slug] = count + 1
                anchors.add(slug if count == 0 else f"{slug}-{count}")
        for match in HTML_ANCHOR_RE.finditer(line):
            anchor = html.unescape(match.group(1))
            anchors.add(anchor)
            anchors.add(anchor.lower())
    return anchors


def check_file(path: Path) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    anchor_cache: dict[Path, set[str]] = {}
    for link in collect_links(path):
        if is_external_or_special(link.target):
            continue
        target_path_text, fragment = split_target(link.target)
        target_path_text = strip_line_suffix(target_path_text)
        target_file = resolve_target_file(path, target_path_text)
        try:
            target_file.relative_to(ROOT.resolve())
        except ValueError:
            issues.append(Issue(path, link.line, link.target, "local link escapes repo"))
            continue
        if not target_file.exists():
            issues.append(Issue(path, link.line, link.target, "local link target missing"))
            continue
        if fragment and (not target_path_text or target_file.suffix.lower() in MARKDOWN_SUFFIXES):
            anchors = anchor_cache.setdefault(target_file, anchors_for(target_file))
            normalized = unquote(fragment).lstrip("#").lower()
            if normalized not in anchors:
                issues.append(Issue(path, link.line, link.target, "markdown anchor missing"))
    return tuple(issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base ref/SHA for git diff")
    parser.add_argument("--head", help="head ref/SHA for git diff")
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="repo-relative changed file; may be repeated",
    )
    parser.add_argument("--all", action="store_true", help="check every Markdown file")
    args = parser.parse_args(argv)

    try:
        if args.all:
            files = all_markdown_files()
        else:
            changed = tuple(repo_path(path) for path in args.changed_file)
            if not changed:
                changed = changed_files_from_git(args.base, args.head)
            files = markdown_files(changed)

        if not files:
            print("docs-linkcheck: no changed Markdown files to check")
            return 0

        issues: list[Issue] = []
        for file in files:
            issues.extend(check_file(file))

        if issues:
            print("docs-linkcheck: broken local Markdown links found", file=sys.stderr)
            for issue in issues:
                print(issue.format(), file=sys.stderr)
            return 1

        print(f"docs-linkcheck: checked {len(files)} Markdown file(s)")
        return 0
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError) as exc:
        print(f"docs-linkcheck: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
